"""
Admin commands. Everything here is gated on the admin role.

/overflow  create private threads for every match this week that doesn't
           already have a channel. Run it after Nation has made as many
           channels as he's going to. Safe to run twice - it skips matches
           that already have a thread or a channel.
/override  correct an already-reported result
/extend    give a match a later deadline
/week      change which week is current
/status    what's outstanding
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..matching import is_admin, resolve, teams_from_overwrites
from ..roster import PLATFORM_FIX
from . import private


DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _variants(team: str):
    """
    Names the same team might carry as a Discord role.

    Role names tend to be shorter than the sheet's - "Purdue" for "Purdue
    Boilermakers" - and occasionally carry an Esports suffix instead of a
    mascot.
    """
    words = team.split()
    out = []
    for n in range(len(words) - 1, 0, -1):
        out.append(" ".join(words[:n]))
    if words:
        out.append(words[0] + " Esports")
        out.append(words[-1])
    return out


def _tracker(p) -> str:
    """Rebuild the tracker URL from the identity we parsed off the sheet."""
    plat = {"ps4": "psn", "xbox": "xbl"}.get(p["platform"], p["platform"])
    return (
        "https://rocketleague.tracker.network/rocket-league/profile/"
        f"{plat}/{p['id']}/overview"
    )
from ..store import iso, now_utc
from ..times import discord_ts, parse, short


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return is_admin(self.bot.store, ctx.author)

    def _guard(self, interaction) -> bool:
        return is_admin(self.bot.store, interaction.user)

    # ---------------------------------------------------------------- overflow
    @app_commands.command(
        name="overflow",
        description="Create threads for this week's matches that have no channel.",
    )
    @app_commands.describe(week="Week number (defaults to the current week)")
    async def overflow(self, interaction: discord.Interaction, week: int = None):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Admins only.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=private())
        week = week or store.week
        created, skipped, failed = await self.bot.create_threads(week, interaction.guild)
        await interaction.followup.send(
            f"**Week {week}**\n"
            f"threads created: {created}\n"
            f"already had a channel or thread: {skipped}\n"
            f"failed: {failed}",
            ephemeral=private(),
        )

    # ---------------------------------------------------------------- overdue
    @app_commands.command(
        name="overdue",
        description="Find matches past their deadline and open threads for them.",
    )
    @app_commands.describe(
        upto_week="Check every week up to this one (defaults to current)",
        notify="Ping both teams in each overdue thread (default yes)",
    )
    async def overdue(
        self,
        interaction: discord.Interaction,
        upto_week: int = None,
        notify: bool = True,
    ):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=private())

        items = store.overdue(upto_week)
        if not items:
            return await interaction.followup.send(
                "Nothing is overdue.", ephemeral=private()
            )

        created, existing = await self.bot.open_overdue(items, interaction.guild, notify)

        lines = []
        for fx, st, due in items[:40]:
            where = ""
            if st.get("channel_id"):
                where = f" <#{st['channel_id']}>"
            elif st.get("thread_id"):
                where = f" <#{st['thread_id']}>"
            flag = "scheduled but not played" if st["status"] == "scheduled" else "never scheduled"
            lines.append(
                f"• W{fx['week']} **{fx['home']}** vs **{fx['away']}** — {flag}{where}"
            )
        more = f"\n…and {len(items) - 40} more" if len(items) > 40 else ""

        embed = discord.Embed(
            title=f"{len(items)} overdue match{'es' if len(items) != 1 else ''}",
            description="\n".join(lines) + more,
            colour=0xE74C3C,
        )
        embed.set_footer(
            text=f"{created} new thread(s) opened, {existing} already had somewhere to talk"
        )

        target_id = store.cfg("overdue_channel_id") or store.cfg("log_channel_id")
        ch = interaction.guild.get_channel(int(target_id)) if target_id else None
        if ch:
            await ch.send(embed=embed)
            await interaction.followup.send(
                f"{len(items)} overdue, {created} thread(s) opened. Posted in {ch.mention}.",
                ephemeral=private(),
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=private())

    # ---------------------------------------------------------------- override
    @app_commands.command(
        name="override", description="Correct the result of a match (admin)."
    )
    @app_commands.describe(winner="Team that won", score="e.g. 3-1", reason="Why")
    async def override(
        self, interaction: discord.Interaction, winner: str, score: str, reason: str
    ):
        from .reporting import match_team, parse_score

        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "Run this in the match's thread or channel.", ephemeral=private()
            )
        win = match_team(winner, fx)
        if win is None:
            return await interaction.response.send_message(
                f"**{winner}** isn't in this match.", ephemeral=private()
            )
        parsed = parse_score(score)
        if parsed is None:
            return await interaction.response.send_message(
                "Couldn't read that score.", ephemeral=private()
            )
        hi, lo = max(parsed), min(parsed)
        loser = fx["away"] if win == fx["home"] else fx["home"]
        home_score = hi if win == fx["home"] else lo
        away_score = lo if win == fx["home"] else hi

        st = store.get(fx["id"])
        before = st.get("result")
        store.set(
            fx["id"],
            status="reported",
            disputed=False,
            result={
                "winner": win,
                "loser": loser,
                "home": home_score,
                "away": away_score,
                "link": (before or {}).get("link", ""),
                "by": interaction.user.id,
                "override_reason": reason,
            },
            reported_at=iso(now_utc()),
            sheet_written=False,
        )  # reported_at reset restarts the 24h archive window
        was = (
            f"{before['home']}–{before['away']}" if before else "nothing recorded"
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Result corrected",
                description=f"**{fx['home']}** {home_score} – {away_score} **{fx['away']}**\n\n"
                f"was: {was}\nreason: {reason}",
                colour=0xE67E22,
            )
        )
        await self.bot.on_result(fx, interaction, corrected=True)
        await self.bot.log(
            f"**OVERRIDE** {fx['home']} {home_score}-{away_score} {fx['away']} "
            f"(was {was}) by {interaction.user} — {reason}"
        )

    # ------------------------------------------------------------------ sheets
    @app_commands.command(
        name="sheets",
        description="Check the Google Sheets link, or push this match again (admin).",
    )
    @app_commands.describe(action="check = test the connection, push = rewrite this match")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="check the connection", value="check"),
            app_commands.Choice(name="push this match again", value="push"),
        ]
    )
    async def sheets(self, interaction: discord.Interaction, action: str = "check"):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=private())

        if self.bot.sheets is None:
            why = getattr(self.bot, "_sheets_error", None)
            return await interaction.followup.send(
                "Sheets isn't connected.\n"
                + (
                    f"reason: {why}"
                    if why
                    else "`sheets_enabled` is false in config.json."
                ),
                ephemeral=private(),
            )

        if action == "check":
            names = []
            for label, ss in (
                ("non-conference", self.bot.sheets.nc),
                ("caster", self.bot.sheets.caster),
                ("conference", self.bot.sheets.conf),
            ):
                names.append(f"{label}: " + (ss.title if ss else "not configured"))
            return await interaction.followup.send(
                "Connected.\n" + "\n".join(names), ephemeral=private()
            )

        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.followup.send(
                "Run this in the match's thread or channel.", ephemeral=private()
            )
        st = store.get(fx["id"])
        lines = []
        if st.get("scheduled_iso"):
            ok, msg = await self.bot.sheets.write_time(
                fx, datetime.fromisoformat(st["scheduled_iso"])
            )
            store.set(fx["id"], caster_written=ok)
            lines.append(("caster ok — " if ok else "caster failed — ") + msg)
        if st.get("result"):
            ok, msg = await self.bot.sheets.write_score(fx, st["result"])
            store.set(fx["id"], sheet_written=ok)
            lines.append(("score ok — " if ok else "score failed — ") + msg)
        if not lines:
            lines = ["nothing to push — no time and no result on this match"]
        await interaction.followup.send("\n".join(lines), ephemeral=private())

    # ------------------------------------------------------------------- roles
    @app_commands.command(
        name="roles",
        description="Match this server's roles to the team list (admin).",
    )
    @app_commands.describe(
        apply="Write the mapping to teams.json — otherwise it just reports"
    )
    async def roles(self, interaction: discord.Interaction, apply: bool = False):
        """
        Builds teams.json from the roles that actually exist in the server, so
        nobody copies 202 role ids by hand.

        Matching is deliberately conservative: exact name first, then a few
        obvious variants. Anything it isn't sure about is listed rather than
        guessed, because a role mapped to the wrong team puts one side of a
        match in the wrong thread and nobody notices until they meet.
        """
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=private())

        guild = interaction.guild
        roles = [r for r in guild.roles if not r.is_default() and not r.managed]
        by_norm = {}
        for r in roles:
            by_norm.setdefault(_norm(r.name), []).append(r)

        full = json.load(
            open(os.path.join(DATA_DIR, "teams.json"), encoding="utf-8")
        )
        # start from what's already there, so a team whose role this server
        # doesn't have keeps whatever mapping it had rather than vanishing
        mapped = {k: dict(v) for k, v in full.items()}
        found, ambiguous, unmatched = {}, [], []

        for team in sorted(full):
            hits = by_norm.get(_norm(team))
            if not hits:
                # the sheet name and the role name often differ by the mascot
                for cand in _variants(team):
                    hits = by_norm.get(_norm(cand))
                    if hits:
                        break
            if not hits:
                unmatched.append(team)
            elif len(hits) > 1:
                ambiguous.append((team, [r.name for r in hits]))
            else:
                found[team] = {"role_id": str(hits[0].id), "role_name": hits[0].name}
                mapped[team] = found[team]

        changed = sum(
            1
            for t, v in found.items()
            if full.get(t, {}).get("role_id") != v["role_id"]
        )
        lines = [
            f"**{len(found)}** of {len(full)} teams matched a role in this server"
            + (f" — {changed} would change." if changed else " — none would change.")
        ]
        if ambiguous:
            lines.append(f"\n**{len(ambiguous)} ambiguous** — more than one role fits:")
            lines += [f"• {t} → {', '.join(rs)}" for t, rs in ambiguous[:10]]
        if unmatched:
            lines.append(f"\n**{len(unmatched)} with no role found:**")
            lines += [f"• {t}" for t in unmatched[:20]]
            if len(unmatched) > 20:
                lines.append(f"…and {len(unmatched) - 20} more")

        if not apply:
            lines.append("\nRun `/roles apply:true` to write this to `teams.json`.")
            return await interaction.followup.send(
                "\n".join(lines)[:1900], ephemeral=private()
            )

        path = os.path.join(DATA_DIR, "teams.json")
        backup = path + ".bak"
        try:
            with open(backup, "w", encoding="utf-8") as f:
                json.dump(full, f, indent=1, sort_keys=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(mapped, f, indent=1, sort_keys=True)
        except Exception as e:
            return await interaction.followup.send(
                f"Couldn't write teams.json — {type(e).__name__}: {e}", ephemeral=private()
            )
        lines.append(
            f"\n**Written.** {len(found)} teams mapped from this server, "
            f"{len(full) - len(found)} left as they were. Old file at "
            "`teams.json.bak`. Restart the bot to load it."
        )
        await self.bot.log(
            f"/roles apply — {len(found)} teams mapped to roles by "
            f"{interaction.user.display_name}"
        )
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=private())

    # ------------------------------------------------------------ accountaudit
    @app_commands.command(
        name="accountaudit",
        description="Spreadsheet of every account that isn't exactly on the roster, with every game it played (admin).",
    )
    async def accountaudit(self, interaction: discord.Interaction):
        import io
        from .. import roster_issues
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if self.bot.sheets is None:
            return await interaction.response.send_message("Sheets isn't connected.", ephemeral=private())
        await interaction.response.defer(ephemeral=private())
        await self.bot.load_roster()
        if not getattr(self.bot, "roster_loaded", False):
            return await interaction.followup.send("Couldn't read the Roster Sheet - check the log.",
                                                   ephemeral=private())
        try:
            stats = await self.bot.sheets.read_game_stats()
            scores = await self.bot.sheets.read_game_scores()
        except Exception as e:
            return await interaction.followup.send(
                f"Couldn't read Game Stats / Game Scores ({type(e).__name__}).", ephemeral=private())
        fixtures = {f["id"]: f for f in store.fixtures}
        acc_csv, games_csv, n_acc, n_games, counts = await asyncio.to_thread(
            roster_issues.audit, stats, scores, fixtures, self.bot.roster, store.state)
        stamp = datetime.now().strftime("%Y-%m-%d")
        had_last, changes, fixed, new = roster_issues.compare_with_last(acc_csv)
        n_fixed, n_new = len(fixed), len(new)
        lines = []
        if had_last:
            lines.append(f"**Since the last audit:** {n_fixed} fixed · {n_new} new")
        lines += roster_issues.summary_lines(counts)
        await interaction.followup.send(
            f"**Account audit** - {n_acc} accounts not exactly on the roster, across {n_games} games.\n"
            + "\n".join(lines) + "\n"
            "`accounts` = one row per account, sorted by priority · `games` = every game each one "
            "played. The last column says what each status means.",
            ephemeral=private(),
            files=[discord.File(io.BytesIO(acc_csv.encode("utf-8")), filename=f"account_audit_accounts_{stamp}.csv"),
                   discord.File(io.BytesIO(games_csv.encode("utf-8")), filename=f"account_audit_games_{stamp}.csv")]
            + ([discord.File(io.BytesIO(changes.encode("utf-8")), filename=f"account_audit_changes_{stamp}.txt")]
               if changes else []))

    # ------------------------------------------------------------- rostercheck
    @app_commands.command(
        name="rostercheck",
        description="Re-check every player against the roster now - shows what got fixed (admin).",
    )
    @app_commands.describe(team="Show one team's players (leave blank for the whole league)")
    async def rostercheck(self, interaction: discord.Interaction, team: str = None):
        import io
        from .. import roster_issues
        from .reporting import teams_named
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if self.bot.sheets is None:
            return await interaction.response.send_message("Sheets isn't connected.", ephemeral=private())
        t = None
        if team:
            hits = teams_named(team, list(self.bot.store.teams))
            if len(hits) != 1:
                return await interaction.response.send_message(
                    f"**{team}** " + (f"could be {', '.join(hits[:6])}" if hits else "isn't a team I know")
                    + " - use more of the name.", ephemeral=private())
            t = hits[0]
        await interaction.response.defer(ephemeral=private())
        try:
            res = await self.bot.roster_check(relabel=True)
        except Exception as e:
            return await interaction.followup.send(f"Check failed ({type(e).__name__}: {e}).", ephemeral=private())
        if res is None:
            return await interaction.followup.send("Couldn't read the Roster Sheet - check the log.",
                                                   ephemeral=private())
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        files = []
        if t:
            rows = roster_issues.team_rows(res["acc_csv"], t)
            fixed = [v for v in res["fixed"] if v[0] == t]
            out = [f"**Roster check — {t}**"]
            if fixed:
                out.append(f"✅ Fixed since last check: " + ", ".join(f"{v[1]} ({v[2]})" for v in fixed))
            if not rows:
                out.append("✅ Every account that has played for this team matches the roster.")
            else:
                label = {"1": "❌", "2": "⚠️", "3": "🔹"}
                for r in sorted(rows):
                    hint = f" — probably/roster: {r[4]}" if r[4] else ""
                    out.append(f"{label.get(r[0], '•')} **{r[2]}** ({r[3]}) — {r[1].lower()}{hint}\n   <{r[5]}>")
                out.append("❌ real problem · ⚠️ needs a look · 🔹 right person, add the account they play on")
            text = "\n".join(out)
            if len(text) > 1990:
                files.append(discord.File(io.BytesIO(text.encode("utf-8")), filename=f"rostercheck_{stamp}.txt"))
                text = text[:1900] + "\n…full list attached"
        else:
            text = "**Roster check — whole league**\n" + self.bot.check_summary(res)
            if res["changes"]:
                files.append(discord.File(io.BytesIO(res["changes"].encode("utf-8")),
                                          filename=f"roster_changes_{stamp}.txt"))
            files.append(discord.File(io.BytesIO(res["acc_csv"].encode("utf-8")),
                                      filename=f"account_audit_accounts_{stamp}.csv"))
        await interaction.followup.send(text, ephemeral=private(), files=files)

    # ------------------------------------------------------------- rosterwatch
    @app_commands.command(
        name="rosterwatch",
        description="Where the bot posts what got fixed after each Roster Sheet edit (admin).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="post updates in this channel", value="here"),
        app_commands.Choice(name="back to the log channel", value="log"),
        app_commands.Choice(name="turn off", value="off"),
    ])
    async def rosterwatch(self, interaction: discord.Interaction, action: str):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if action == "here":
            store.set_cfg("roster_check_channel_id", interaction.channel.id)
            store.set_cfg("roster_watch", True)
            msg = (f"Got it - after any Roster Sheet edit I'll re-check everyone and post what got fixed "
                   f"in {interaction.channel.mention} (checks every "
                   f"{store.cfg('roster_watch_minutes', 10)} min).")
        elif action == "log":
            store.set_cfg("roster_check_channel_id", None)
            store.set_cfg("roster_watch", True)
            msg = "Roster updates go to the log channel again."
        else:
            store.set_cfg("roster_watch", False)
            msg = "Roster watching is off. `/rostercheck` still works any time."
        w = self.bot.roster_watch
        if store.cfg("roster_watch", True) and not w.is_running():
            w.start()
        elif not store.cfg("roster_watch", True) and w.is_running():
            w.cancel()
        await interaction.response.send_message(msg, ephemeral=private())

    # ------------------------------------------------------------ rosterreport
    @app_commands.command(
        name="rosterreport",
        description="Text file of every account that doesn't match the Roster Sheet, with who they played with (admin).",
    )
    @app_commands.describe(team="Only this team (leave blank for every team)")
    async def rosterreport(self, interaction: discord.Interaction, team: str = None):
        import io
        from .. import roster_issues
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if self.bot.sheets is None:
            return await interaction.response.send_message("Sheets isn't connected.", ephemeral=private())
        await interaction.response.defer(ephemeral=private())
        await self.bot.load_roster()            # the Roster Sheet as it is right now
        if not getattr(self.bot, "roster_loaded", False):
            return await interaction.followup.send("Couldn't read the Roster Sheet - check the log.",
                                                   ephemeral=private())
        try:
            stats = await self.bot.sheets.read_game_stats()
        except Exception as e:
            return await interaction.followup.send(f"Couldn't read Game Stats ({type(e).__name__}).",
                                                   ephemeral=private())
        fixtures = {f["id"]: f for f in store.fixtures}
        text = await asyncio.to_thread(roster_issues.report, stats, fixtures, self.bot.roster,
                                       store.state, team)
        count = text.split("\n")[3] if text.count("\n") > 3 else ""
        name = "roster_report" + (f"_{re.sub(r'[^A-Za-z0-9]+', '_', team)}" if team else "") + ".txt"
        await interaction.followup.send(
            f"Roster report - {count}", ephemeral=private(),
            file=discord.File(io.BytesIO(text.encode("utf-8")), filename=name))

    # -------------------------------------------------------------------- link
    @app_commands.command(
        name="link",
        description="Tie a replay account (e.g. a console player showing as Epic) to a roster player (admin).",
    )
    @app_commands.describe(
        account="From the Roster Issues tab's Account column, e.g. epic:9b2fb9c2e46045... - or that tracker link",
        team="The team they're rostered on",
        player="Their name on the Roster Sheet",
    )
    async def link(self, interaction: discord.Interaction, account: str, team: str, player: str):
        from ..roster import norm, parse_link
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=private())
        if not getattr(self.bot, "roster_loaded", False):
            await self.bot.load_roster()
        r = self.bot.roster
        acct = account.strip()
        parsed = parse_link(acct)
        if parsed:
            plat, pid = parsed
        elif ":" in acct:
            plat, pid = acct.split(":", 1)
        else:
            return await interaction.followup.send(
                "Give the account as `platform:id` (the Account column on Roster Issues) "
                "or paste the tracker link.", ephemeral=private())
        plat = {"psn": "ps4", "xbl": "xbox"}.get(plat.strip().lower(), plat.strip().lower())
        teams = r.find_team(team, known=set(store.teams))
        if len(teams) != 1:
            return await interaction.followup.send(
                f"**{team}** matches {len(teams)} teams" + (": " + ", ".join(teams[:6]) if teams else "")
                + " - be more specific.", ephemeral=private())
        t = teams[0]
        recs = [x for x in r.teams.get(t, []) if norm(x["name"]) == norm(player)] or \
               [x for x in r.teams.get(t, []) if norm(player) in norm(x["name"])]
        if not recs:
            names = ", ".join(x["name"] for x in r.teams.get(t, [])) or "nobody listed"
            return await interaction.followup.send(
                f"No **{player}** on the {t} roster ({names}).", ephemeral=private())
        rec = recs[0]
        key = f"{plat}:{norm(pid)}"
        r.learned[key] = rec
        r.manual.add(key)       # an admin's link always counts, any team
        r.save_learned(os.path.join(os.path.dirname(__file__), "..", "..", "data", "identities.json"))
        await self.bot.log(f"linked by {interaction.user.display_name} — {key} → {rec['name']} ({t})")
        await interaction.followup.send(
            f"Linked **{plat}:{pid}** to **{rec['name']}** ({t}). Run `/stats action:relabel` "
            "to move their past games onto the team.", ephemeral=private())

    # ------------------------------------------------------------------ roster
    @app_commands.command(
        name="roster",
        description="Look up a team's roster and tracker links.",
    )
    @app_commands.describe(
        team="Team name — leave blank for your own team",
        gaps="Admin: show which teams are missing tracker links",
    )
    async def roster(
        self, interaction: discord.Interaction, team: str = None, gaps: bool = False
    ):
        store = self.bot.store
        if self.bot.sheets is None:
            return await interaction.response.send_message(
                "Sheets isn't connected, so I can't read the Roster Sheet.",
                ephemeral=private(),
            )
        await interaction.response.defer(ephemeral=private())

        if not self.bot.roster_loaded:
            await self.bot.load_roster()
        r = self.bot.roster
        if not self.bot.roster_loaded:
            return await interaction.followup.send(
                "Couldn't read the Roster Sheet — check the log.", ephemeral=private()
            )

        # the gaps report is asked for explicitly, so it never depends on
        # which roles the person happens to hold
        if gaps:
            if not self._guard(interaction):
                return await interaction.followup.send(
                    "The gaps report is admins only.", ephemeral=True
                )
            return await interaction.followup.send(
                embed=self._roster_gaps(r, store), ephemeral=private()
            )

        if not team:
            mine = [
                store.team_for_role(role.id)
                for role in interaction.user.roles
                if store.team_for_role(role.id)
            ]
            if mine:
                team = mine[0]
            else:
                return await interaction.followup.send(
                    "Name a team — `/roster team:purdue`."
                    + (
                        "\nOr `/roster gaps:true` for the missing-tracker report."
                        if self._guard(interaction)
                        else ""
                    ),
                    ephemeral=private(),
                )

        matches = r.find_team(team, known=set(store.teams) | set(r.teams))
        if not matches:
            return await interaction.followup.send(
                f"No team matching **{team}**.", ephemeral=private()
            )
        if len(matches) > 1:
            return await interaction.followup.send(
                f"**{team}** could be a few teams:\n"
                + "\n".join(f"• {m}" for m in matches[:10]),
                ephemeral=private(),
            )

        name = matches[0]
        players = r.teams.get(name, [])
        if not players:
            return await interaction.followup.send(
                f"**{name}** has nobody with a tracker link on the Roster Sheet yet.",
                ephemeral=private(),
            )

        lines = []
        for p in players:
            lines.append(f"**{p['name']}** — [tracker]({_tracker(p)})")
        embed = discord.Embed(
            title=name,
            description="\n".join(lines),
            colour=0x3498DB,
        )
        embed.set_footer(
            text=f"{len(players)} players"
            + ("" if len(players) >= 3 else " — fewer than 3 with tracker links")
        )
        await interaction.followup.send(embed=embed, ephemeral=private())

    @staticmethod
    def _roster_gaps(r, store):
        gaps = r.missing_trackers(expected_teams=set(store.teams))
        exact = sum(1 for p in r.players if p["exact"])
        soft = len(r.players) - exact
        desc = (
            f"**{len(r.players)} players** across **{len(r.teams)} teams**\n"
            f"{exact} matched exactly (Steam), {soft} by name only (Epic/PSN)\n"
        )
        if not gaps:
            desc += "\nEvery team has at least three tracker links."
        else:
            shown = gaps[:25]
            desc += f"\n**{len(gaps)} to chase:**\n" + "\n".join(
                f"• {g}" for g in shown
            )
            if len(gaps) > 25:
                desc += f"\n…and {len(gaps) - 25} more"
        return discord.Embed(title="Roster Sheet", description=desc, colour=0xE67E22)

    # ---------------------------------------------------------------- fixtures
    @app_commands.command(
        name="fixtures",
        description="A team's whole season — non-conference and conference.",
    )
    @app_commands.describe(team="Team name — leave blank for your own team")
    async def fixtures(self, interaction: discord.Interaction, team: str = None):
        store = self.bot.store
        await interaction.response.defer(ephemeral=private())

        if not team:
            mine = [
                store.team_for_role(role.id)
                for role in interaction.user.roles
                if store.team_for_role(role.id)
            ]
            if not mine:
                return await interaction.followup.send(
                    "Name a team — `/fixtures team:purdue`.", ephemeral=private()
                )
            team = mine[0]

        matches = self.bot.roster.find_team(team, known=set(store.teams))
        if not matches:
            return await interaction.followup.send(
                f"No team matching **{team}**.", ephemeral=private()
            )
        if len(matches) > 1:
            return await interaction.followup.send(
                f"**{team}** could be a few teams:\n"
                + "\n".join(f"• {m}" for m in matches[:10]),
                ephemeral=private(),
            )
        name = matches[0]

        slate = store.team_fixtures(name)
        if not slate:
            return await interaction.followup.send(
                f"No fixtures for **{name}**.", ephemeral=private()
            )

        blocks = {"non-conference": [], "conference": []}
        for f in slate:
            st = store.get(f["id"])
            opp = f["away"] if f["home"] == name else f["home"]
            if st["status"] in ("reported", "archived") and st.get("result"):
                r = st["result"]
                mine_, theirs = (
                    (r["home"], r["away"]) if f["home"] == name else (r["away"], r["home"])
                )
                mark = "🟢" if r["winner"] == name else "🔴"
                line = f"{mark} W{f['week']} vs {opp} — **{mine_}-{theirs}**"
            elif st.get("scheduled_iso"):
                dt = datetime.fromisoformat(st["scheduled_iso"])
                line = f"🟡 W{f['week']} vs {opp} — {discord_ts(dt, 'f')}"
            elif st.get("proposed"):
                line = f"⏳ W{f['week']} vs {opp} — time proposed, not confirmed"
            else:
                line = f"⚪ W{f['week']} vs {opp}"
            blocks[f.get("kind", "non-conference")].append(line)

        embed = discord.Embed(title=f"{name} — schedule", colour=0x3498DB)
        if blocks["non-conference"]:
            embed.add_field(
                name=f"Non-conference ({len(blocks['non-conference'])})",
                value="\n".join(blocks["non-conference"])[:1024],
                inline=False,
            )
        if blocks["conference"]:
            conf = next(
                (f.get("conference") for f in slate if f.get("conference")), None
            )
            chunk = "\n".join(blocks["conference"])
            embed.add_field(
                name=f"{conf or 'Conference'} ({len(blocks['conference'])})",
                value=chunk[:1024],
                inline=False,
            )
            if len(chunk) > 1024:
                embed.add_field(
                    name="…continued", value=chunk[1024:2048], inline=False
                )
        played = sum(
            1 for f in slate if store.get(f["id"])["status"] in ("reported", "archived")
        )
        embed.set_footer(
            text=f"{len(slate)} matches · {played} played · "
            "🟢 won  🔴 lost  🟡 scheduled  ⏳ awaiting confirmation  ⚪ not scheduled"
        )
        await interaction.followup.send(embed=embed, ephemeral=private())

    # ------------------------------------------------------------------- stats
    @app_commands.command(
        name="stats",
        description="Build the stat tabs, or pull stats for this match (admin).",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="build the Players and Leaders tabs", value="build"),
            app_commands.Choice(name="pull stats for this match", value="pull"),
            app_commands.Choice(name="re-label every stat line by the Roster Sheet", value="relabel"),
            app_commands.Choice(name="rebuild the website stats sheet now", value="website"),
        ]
    )
    async def stats(self, interaction: discord.Interaction, action: str = "pull"):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if self.bot.sheets is None:
            return await interaction.response.send_message(
                "Sheets isn't connected.", ephemeral=private()
            )
        await interaction.response.defer(ephemeral=private())

        if action == "build":
            # Google allows ~60 writes a minute; if something else (a backfill)
            # is using them, wait out the minute and go again
            for attempt in range(3):
                try:
                    ok, msg = await asyncio.to_thread(self.bot.sheets.build_stat_tabs)
                    break
                except Exception as e:
                    ok, msg = False, f"{type(e).__name__}: {e}"
                    if "429" not in str(e) or attempt == 2:
                        break
                    await asyncio.sleep(65)
            return await interaction.followup.send(
                ("Built. " if ok else "Failed — ") + msg, ephemeral=private()
            )

        if action == "website":
            ok, msg = await self.bot.sync_website_now()
            return await interaction.followup.send(
                ("Website stats synced — " if ok else "Failed — ") + msg, ephemeral=private()
            )

        if action == "relabel":
            # re-read the Roster Sheet first, so fixes made since startup count
            await self.bot.load_roster()
            if not getattr(self.bot, "roster_loaded", False):
                return await interaction.followup.send(
                    "The Roster Sheet isn't loaded yet — try again in a minute.", ephemeral=private()
                )
            try:
                ok, msg = await self.bot.sheets.relabel_game_stats(self.bot.roster.team_of, self.bot.roster)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            if ok:
                self.bot.request_website_sync(delay=5)
            return await interaction.followup.send(
                ("Done — " if ok else "Failed — ") + msg, ephemeral=private()
            )

        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.followup.send(
                "Run this in the match's thread or channel.", ephemeral=private()
            )
        st = store.get(fx["id"])
        if not st.get("result"):
            return await interaction.followup.send(
                "This match hasn't been reported yet.", ephemeral=private()
            )
        await self.bot.collect_stats(fx, st)
        await interaction.followup.send(
            "Pulled — check the log for what landed.", ephemeral=private()
        )

    # ------------------------------------------------------------------- clear
    @app_commands.command(
        name="clear",
        description="Wipe this match back to unscheduled (admin).",
    )
    @app_commands.describe(
        what="all = time and result, time = just the time, result = just the result",
        team="Outside a match channel: one of the two teams",
        opponent="Outside a match channel: the other team",
    )
    @app_commands.choices(
        what=[
            app_commands.Choice(name="everything", value="all"),
            app_commands.Choice(name="time only", value="time"),
            app_commands.Choice(name="result only", value="result"),
        ]
    )
    async def clear(
        self, interaction: discord.Interaction, what: str = "all",
        team: str = None, opponent: str = None,
    ):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        fx = resolve(store, interaction.channel)
        if fx is None and team and opponent:
            from .reporting import teams_named
            names = list(store.teams)
            ta, tb = teams_named(team, names), teams_named(opponent, names)
            if len(ta) != 1 or len(tb) != 1:
                bad = team if len(ta) != 1 else opponent
                opts = ta if len(ta) != 1 else tb
                return await interaction.response.send_message(
                    f"**{bad}** isn't one team" + (f" — could be {', '.join(opts[:6])}" if opts else "")
                    + ". Use more of the name.", ephemeral=private())
            pair = {ta[0], tb[0]}
            meetings = [f for f in store.fixtures if {f["home"], f["away"]} == pair]
            played = [f for f in meetings if store.get(f["id"]).get("result")]
            # clearing is for undoing a result, so take the latest one recorded
            fx = (sorted(played, key=lambda f: f["week"])[-1] if played
                  else (meetings[0] if meetings else None))
            if fx is None:
                return await interaction.response.send_message(
                    f"**{ta[0]}** and **{tb[0]}** don't play each other.", ephemeral=private())
        if fx is None:
            return await interaction.response.send_message(
                "Run this in the match's thread or channel, or give `team:` and `opponent:`.",
                ephemeral=private()
            )
        # unarchiving and renaming can both stall on rate limits, so claim the
        # interaction before doing any of it
        await interaction.response.defer(ephemeral=False)  # the reset is posted for everyone
        st = store.get(fx["id"])
        wiped = []

        if what in ("all", "time"):
            if st.get("scheduled_iso") or st.get("proposed"):
                wiped.append("time")
            st["scheduled_iso"] = None
            st["proposed"] = None
        if what in ("all", "result"):
            if st.get("result"):
                wiped.append("result")
            st["result"] = None
            st["reported_at"] = None
            st["disputed"] = False
            st["dispute_ruling"] = None
            st["sheet_written"] = False
        if what == "all":
            st["extended_to"] = None

        # status follows whatever is left
        if st.get("result"):
            st["status"] = "reported"
        elif st.get("scheduled_iso"):
            st["status"] = "scheduled"
        else:
            st["status"] = "unscheduled"
        st["archived_at"] = None
        store.save()

        # un-archive the thread if the lifecycle loop already locked it
        tid = st.get("thread_id")
        if tid:
            th = self.bot.get_channel(tid)
            if th is None:
                try:
                    th = await interaction.guild.fetch_channel(tid)
                except discord.HTTPException:
                    th = None
            if th is not None:
                try:
                    await asyncio.wait_for(
                        th.edit(archived=False, locked=False), timeout=3
                    )
                except (asyncio.TimeoutError, discord.HTTPException):
                    pass
        await self.bot.rename(fx, st)

        # put the sheet back how it was too, or the cells keep a score for a
        # match that no longer has one
        # clear the sheet whatever state thought it held. If the two have
        # drifted - a result cleared before, or a score typed in by hand -
        # the sheet is the thing that's visibly wrong, so make it match.
        sheet_lines = []
        if self.bot.sheets:
            if what in ("all", "result"):
                try:
                    ok, msg = await self.bot.sheets.clear_score(fx)
                except Exception as e:
                    ok, msg = False, f"{type(e).__name__}: {e}"
                sheet_lines.append(("sheet " if ok else "sheet FAILED — ") + msg)
            if what in ("all", "time"):
                try:
                    ok, msg = await self.bot.sheets.clear_time(fx)
                except Exception as e:
                    ok, msg = False, f"{type(e).__name__}: {e}"
                sheet_lines.append(("caster " if ok else "caster FAILED — ") + msg)
            if what in ("all", "result"):
                try:
                    await self.bot.sheets.log_result(
                        fx, None, st, note=f"cleared by {interaction.user.display_name}"
                    )
                except Exception:
                    pass
                try:
                    ok, msg = await self.bot.sheets.clear_game_stats(fx)
                    if ok and "no stats" not in msg:
                        sheet_lines.append("stats " + msg)
                except Exception:
                    pass
                try:
                    ok, msg = await self.bot.sheets.clear_game_scores(fx)
                    if ok and "no score" not in msg:
                        sheet_lines.append("scores " + msg)
                except Exception:
                    pass
                self.bot.request_website_sync()

        await interaction.followup.send(
            embed=discord.Embed(
                title="Match reset",
                description=f"**{fx['home']}** vs **{fx['away']}**\n\n"
                + (
                    f"cleared: {', '.join(wiped)}"
                    if wiped
                    else "there was nothing to clear"
                )
                + f"\nnow: {st['status']}"
                + ("\n\n" + "\n".join(sheet_lines) if sheet_lines else ""),
                colour=0x95A5A6,
            ).set_footer(text=f"cleared by {interaction.user.display_name}"),
        )
        await self.bot.log(
            f"CLEARED ({what}) {fx['home']} vs {fx['away']} "
            f"by {interaction.user.display_name}"
            + (" · " + "; ".join(sheet_lines) if sheet_lines else "")
        )

    # ----------------------------------------------------------------- resolve
    @app_commands.command(
        name="resolve",
        description="Clear a dispute, leaving the result as it stands (admin).",
    )
    @app_commands.describe(reason="What you decided and why")
    async def resolve(self, interaction: discord.Interaction, reason: str):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "Run this in the match's thread or channel.", ephemeral=private()
            )
        st = store.get(fx["id"])
        if not st.get("disputed"):
            return await interaction.response.send_message(
                "This match isn't disputed.", ephemeral=private()
            )
        r = st.get("result") or {}

        # the archive clock restarts from now, so the teams get a fresh window
        # to react to the ruling before the thread locks
        store.set(
            fx["id"],
            disputed=False,
            reported_at=iso(now_utc()),
            dispute_ruling={"by": interaction.user.id, "reason": reason},
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Dispute resolved",
                description=f"**{fx['home']}** {r.get('home', '?')} – "
                f"{r.get('away', '?')} **{fx['away']}**\n\n"
                f"The result stands.\nruling: {reason}",
                colour=0x2ECC71,
            ).add_field(
                name="Ruled by",
                value=f"{interaction.user.mention} ({interaction.user.display_name})",
                inline=False,
            )
        )
        await self.bot.log(
            f"**RESOLVED** {fx['home']} vs {fx['away']} — result stands, "
            f"by {interaction.user.display_name} — {reason}"
        )

    @override.autocomplete("winner")
    async def override_winner_ac(self, interaction: discord.Interaction, current: str):
        from .reporting import team_options

        return await team_options(self.bot, interaction, current)

    # ------------------------------------------------------------------ extend
    @app_commands.command(name="extend", description="Give this match a later deadline.")
    @app_commands.describe(deadline="e.g. 'monday 11pm'", reason="Why")
    async def extend(self, interaction: discord.Interaction, deadline: str, reason: str):
        store = self.bot.store
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "Run this in the match's thread or channel.", ephemeral=private()
            )
        dt = parse(deadline)
        if dt is None:
            return await interaction.response.send_message(
                "Couldn't read that date.", ephemeral=private()
            )
        store.set(fx["id"], extended_to=iso(dt))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Deadline extended",
                description=f"**{fx['home']}** vs **{fx['away']}**\n\n"
                f"new deadline {discord_ts(dt)}\nreason: {reason}",
                colour=0x3498DB,
            )
        )
        await self.bot.log(
            f"**EXTEND** {fx['home']} vs {fx['away']} -> {short(dt)} "
            f"by {interaction.user} — {reason}"
        )

    # -------------------------------------------------------------------- week
    @app_commands.command(name="week", description="Set or show the current week.")
    async def week(self, interaction: discord.Interaction, number: int = None):
        store = self.bot.store
        if number is None:
            return await interaction.response.send_message(
                f"Current week is **{store.week}**.", ephemeral=private()
            )
        if not self._guard(interaction):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        store.set_week(number)
        await interaction.response.send_message(
            f"Current week set to **{number}**.", ephemeral=private()
        )
        await self.bot.log(f"week set to {number} by {interaction.user}")

    # ------------------------------------------------------------------ status
    @app_commands.command(name="status", description="What's outstanding this week.")
    async def status(self, interaction: discord.Interaction, week: int = None):
        store = self.bot.store
        week = week or store.week
        fxs = store.week_fixtures(week)
        counts = {"unscheduled": 0, "scheduled": 0, "reported": 0, "archived": 0}
        disputed = 0
        for f in fxs:
            s = store.get(f["id"])
            counts[s["status"]] = counts.get(s["status"], 0) + 1
            if s.get("disputed"):
                disputed += 1
        await interaction.response.send_message(
            f"**Week {week}** — {len(fxs)} matches\n"
            f"🔴 not scheduled: {counts['unscheduled']}\n"
            f"🟡 scheduled: {counts['scheduled']}\n"
            f"🟢 reported: {counts['reported'] + counts['archived']}\n"
            + (
                f"⚠️ disputed: {disputed} — these never archive until "
                f"`/resolve` or `/override`\n"
                if disputed
                else ""
            ),
            ephemeral=private(),
        )


async def setup(bot):
    await bot.add_cog(Admin(bot))
