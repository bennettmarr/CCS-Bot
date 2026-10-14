"""
/report - submit the result, verified against the ballchasing group.

Everything is typed. The winner is matched against the two teams in *this*
match only, so "purdue", "Purdue", "boilermakers" all resolve and anything
else is refused by name. No autocomplete, no dropdowns.

The score is parsed loosely (3-1, 3 - 1, 3:1) then checked as a best of five.
The ballchasing group is fetched and compared before anything is written.
"""
from __future__ import annotations

import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..matching import check, is_admin, resolve
from ..replayfile import games_from_files, series_score
from ..store import iso, now_utc
from . import private

SCORE_RE = re.compile(r"^\s*(\d)\s*[-:to ]+\s*(\d)\s*$", re.I)


def parse_score(text: str):
    m = SCORE_RE.match(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


_ALIASES = None


def aliases() -> dict:
    """
    data/aliases.json - short forms that follow no rule ("UVA" is Virginia,
    "UNCO" is Northern Colorado). {"uva": "Virginia Cavaliers", ...}. Edit it
    by hand; it's re-read on restart.
    """
    global _ALIASES
    if _ALIASES is None:
        import json
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "aliases.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            _ALIASES = {k.strip().lower(): v for k, v in raw.items() if not k.startswith("_")}
        except (OSError, ValueError):
            _ALIASES = {}
    return _ALIASES


def initials(name: str) -> set:
    """
    The short forms people actually type for a school.

    "Middle Tennessee State Blue Raiders" gives MTSBR from every word, but
    nobody types that - they type MTSU or MTS, built from the school part
    before the mascot. So this generates the initials of each leading run of
    words, and adds a U-suffixed version, which is how American schools get
    abbreviated.

    It also adds "University of ..." forms from the school part - the name
    without its mascot: Texas -> UT, Houston -> UH, Rhode Island -> URI,
    New Hampshire -> UNH, St Thomas -> UST, Indiana -> IU. Words already
    written as capitals ("UC Irvine", "UT Arlington") are kept whole, so
    those give UCI and UTA.
    """
    words = [w for w in name.split() if w]
    out = set()
    for n in range(2, len(words) + 1):
        acro = "".join(w[0] for w in words[:n]).lower()
        out.add(acro)
        out.add(acro + "u")
    school = words[:-1] if len(words) > 1 else words
    parts = [w.lower() if (w.isupper() and len(w) <= 4) else w[0].lower() for w in school]
    s = "".join(parts)
    for form in (s, "u" + s, s + "u"):
        if len(form) >= 2:
            out.add(form)
    out |= {k for k, v in aliases().items() if v == name}
    return out


def _hits(t: str, side: str) -> bool:
    s = side.lower()
    if t == s or s.startswith(t) or t in s:
        return True
    # last word is usually the mascot: "boilermakers" -> Purdue Boilermakers
    if t in s.split():
        return True
    return t in initials(side)


def match_team(text: str, fixture: dict):
    """Resolve free text to one of the two teams in this match."""
    if not text:
        return None
    t = text.strip().lower()
    for side in (fixture["home"], fixture["away"]):
        if _hits(t, side):
            return side
    return None


def teams_named(text: str, names) -> list:
    """
    Resolve free text against the whole league. An exact name wins outright;
    otherwise every team it could mean is returned, so the caller can refuse
    an ambiguous one by listing them rather than guessing.
    """
    if not text:
        return []
    t = text.strip().lower()
    exact = [n for n in names if n.lower() == t]
    if exact:
        return exact
    return [n for n in names if _hits(t, n)]


def find_by_teams(store, a: str, b: str = None):
    """
    The match between a and b, from any week - the schedule is loose and a
    week 1 series can be played weeks late (or early). Weeks 1-6 never repeat
    a pairing; conference teams meet twice, and the first unplayed meeting is
    the one a result belongs to.

    With only one team known, its unplayed matches from this week and last
    are what count - nearly everything is played in its own week, a few days
    late at most. Exactly one and that's it; otherwise ask for the opponent.
    Returns (fixture, None) or (None, reason).
    """
    def played(f):
        return store.get(f["id"])["status"] in ("reported", "archived")

    if b is not None:
        cands = sorted((f for f in store.fixtures if {f["home"], f["away"]} == {a, b}),
                       key=lambda f: f["week"])
        if not cands:
            return None, f"**{a}** and **{b}** don't play each other this season."
        open_ = [f for f in cands if not played(f)]
        return (open_[0] if open_ else cands[-1]), None

    cands = [f for f in store.fixtures
             if a in (f["home"], f["away"]) and store.week - 1 <= f["week"] <= store.week
             and not played(f)]
    if len(cands) == 1:
        return cands[0], None
    if not cands:
        return None, (f"**{a}** has nothing unplayed in weeks {store.week - 1}–{store.week}"
                      " — add `opponent:`.")
    opps = ", ".join(f"{f['away'] if f['home'] == a else f['home']} (wk{f['week']})"
                     for f in sorted(cands, key=lambda f: f["week"])[:6])
    return None, (f"**{a}** has {len(cands)} unplayed matches ({opps}"
                  + ("…" if len(cands) > 6 else "") + ") — add `opponent:`.")


def resolve_loose(store, member, winner: str, opponent: str):
    """
    For /report run outside a recognised match channel. The teams come from
    the reporter's team role and the winner/opponent text. Returns
    (fixture, None) or (None, reason).
    """
    names = list(store.teams)
    mine = [store.team_for_role(r.id) for r in member.roles]
    mine = [t for t in mine if t]

    def one(text, label):
        hits = teams_named(text, names)
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"I don't know a team called **{text}** ({label})."
        return None, (f"**{text}** ({label}) could be: " + ", ".join(hits[:8])
                      + ("…" if len(hits) > 8 else "") + ". Be more specific.")

    teams = []
    for text, label in ((winner, "winner"), (opponent, "opponent")):
        if text:
            t, err = one(text, label)
            if err:
                return None, err
            if t not in teams:
                teams.append(t)
    # fill in the reporter's own team only when they named fewer than two -
    # an admin typing both teams is reporting someone else's match
    if len(teams) < 2 and len(mine) == 1 and mine[0] not in teams:
        teams.insert(0, mine[0])
    if not teams:
        return None, "Tell me the teams — `winner:` and `opponent:`."
    if len(teams) > 2:
        return None, ("That's more than two teams: " + ", ".join(teams)
                      + ". Check the winner and opponent.")
    return find_by_teams(store, teams[0], teams[1] if len(teams) > 1 else None)


async def team_options(bot, interaction, current: str):
    """Offer the two teams in this match, for the winner autocomplete."""
    fx = resolve(bot.store, interaction.channel)
    if fx is None:
        return []
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=t, value=t)
        for t in (fx["home"], fx["away"])
        if cur in t.lower() or not cur
    ]


class DisputeView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Dispute this result",
        style=discord.ButtonStyle.danger,
        custom_id="ccs:dispute",
    )
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        store = self.bot.store
        fx = resolve(store, interaction.channel)
        if fx is None:
            return await interaction.response.send_message(
                "I can't tell which match this is.", ephemeral=private()
            )
        ok, team, why = check(store, interaction.user, fx)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=private())

        store.set(fx["id"], disputed=True)
        admin_role = store.cfg("admin_role_id")
        await interaction.response.send_message(
            content=f"<@&{admin_role}>",
            embed=discord.Embed(
                title="Result disputed",
                description=f"**{fx['home']}** vs **{fx['away']}**\n\n"
                f"Raised by {interaction.user.mention} "
                f"({interaction.user.display_name})"
                + (f" — **{team}**" if team else "  ·  admin")
                + "\n\nThe result stays in the standings until an admin rules on it.",
                colour=0xE74C3C,
            ),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await self.bot.log(
            f"**DISPUTE** {fx['home']} vs {fx['away']} raised by {interaction.user}"
        )


class Reporting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="report", description="Report the result of this match.")
    @app_commands.describe(
        winner="Which team won — just type the name",
        opponent="The other team — only needed outside a match channel",
        score="e.g. 3-1",
        ballchasing="Ballchasing group link (or attach the replay files instead)",
        game1="Replay file for game 1",
        game2="Replay file for game 2",
        game3="Replay file for game 3",
        game4="Replay file for game 4",
        game5="Replay file for game 5",
    )
    async def report(
        self,
        interaction: discord.Interaction,
        winner: str,
        score: str,
        ballchasing: str = None,
        opponent: str = None,
        game1: discord.Attachment = None,
        game2: discord.Attachment = None,
        game3: discord.Attachment = None,
        game4: discord.Attachment = None,
        game5: discord.Attachment = None,
    ):
        store = self.bot.store
        fx = resolve(store, interaction.channel)
        if fx is None:
            # not a match channel or thread the bot recognises - work it out
            # from the reporter's team and the names they typed
            fx, why = resolve_loose(store, interaction.user, winner, opponent)
            if fx is None:
                return await interaction.response.send_message(why, ephemeral=private())
        ok, team, why = check(store, interaction.user, fx)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=private())

        st = store.get(fx["id"])
        admin = is_admin(store, interaction.user)
        stats_only = bool(store.cfg("stats_only", False))

        # a result with no agreed time means nobody can check the series was
        # played when both teams expected it. Admins can still enter one, for
        # forfeits and matches sorted out elsewhere.
        if not st.get("scheduled_iso") and not admin and not stats_only:
            return await interaction.response.send_message(
                "This match doesn't have a confirmed time yet, so it can't be "
                "reported.\n\nAgree one first with `/schedule` — the other team "
                "confirms it — then report the result. If you've already played, "
                "ask an admin to enter it.",
                ephemeral=private(),
            )

        if st["status"] == "reported" and not admin:
            return await interaction.response.send_message(
                "This match has already been reported. If it's wrong, use the "
                "Dispute button or ask an admin to override it.",
                ephemeral=private(),
            )

        win = match_team(winner, fx)
        if win is None:
            return await interaction.response.send_message(
                f"**{winner}** isn't in this match. It's {fx['home']} vs {fx['away']}.",
                ephemeral=private(),
            )

        parsed = parse_score(score)
        if parsed is None:
            return await interaction.response.send_message(
                "I couldn't read that score. Try `3-1`.", ephemeral=private()
            )
        hi, lo = max(parsed), min(parsed)
        need = int(store.cfg("wins_needed", 3))
        if hi != need or lo >= need:
            return await interaction.response.send_message(
                f"That isn't a best of {store.cfg('series_length', 5)} score — "
                f"the winner needs exactly {need} and the loser fewer. You gave {score}.",
                ephemeral=private(),
            )

        attached = [a for a in (game1, game2, game3, game4, game5) if a]
        if not attached and not ballchasing:
            return await interaction.response.send_message(
                "I need the replays. Either attach the replay files "
                "(`game1`…`game5`) or give a ballchasing group link — "
                "one or the other.",
                ephemeral=private(),
            )

        await interaction.response.defer()

        note = ""
        # local, not on self: two teams reporting at once must not swap replays
        pending_files = None

        if attached:
            # the replay files themselves - read straight from the header, so
            # this needs no ballchasing account and no upload quota
            bad = [a.filename for a in attached
                   if not a.filename.lower().endswith(".replay")]
            if bad:
                return await interaction.followup.send(
                    "These aren't replay files: " + ", ".join(bad)
                    + "\nAttach the `.replay` files from "
                    "`Documents/My Games/Rocket League/TAGame/Demos`.",
                    ephemeral=private(),
                )
            blobs = []
            for a in attached:
                try:
                    blobs.append((a.filename, await a.read()))
                except Exception:
                    return await interaction.followup.send(
                        f"Couldn't download {a.filename} from Discord — try again.",
                        ephemeral=private(),
                    )
            games, prows, problems = games_from_files(blobs)
            if not games:
                return await interaction.followup.send(
                    "I couldn't read any of those replays.\n"
                    + "\n".join(f"• {p}" for p in problems),
                    ephemeral=private(),
                )
            b, o = series_score(games, prows)
            got = (max(b, o), min(b, o))
            if got != (hi, lo):
                return await interaction.followup.send(
                    f"That doesn't match the replays. You reported **{hi}-{lo}**, "
                    f"but the {len(games)} attached game(s) give **{got[0]}-{got[1]}**. "
                    "Check the score, or whether a game is missing.",
                    ephemeral=private(),
                )
            note = f"Read from {len(games)} replay file(s)."
            if problems:
                note += " Skipped: " + "; ".join(problems) + "."
            pending_files = (games, prows)

        elif store.cfg("verify_ballchasing", True):
            got, reason = await self.bot.bc.game_wins(ballchasing)
            if got is None:
                return await interaction.followup.send(
                    f"I couldn't verify that group — {reason}. "
                    "Make sure the link is the ballchasing **group** for this series "
                    "and that it's public or unlisted — or just attach the replay "
                    "files instead.",
                    ephemeral=private(),
                )
            if got != (hi, lo):
                return await interaction.followup.send(
                    f"That doesn't match the replays. You reported **{hi}-{lo}**, "
                    f"but the group shows **{got[0]}-{got[1]}**. "
                    "Check the score, or that the group only holds this series.",
                    ephemeral=private(),
                )
            note = "Verified against ballchasing."

        loser = fx["away"] if win == fx["home"] else fx["home"]
        home_score = hi if win == fx["home"] else lo
        away_score = lo if win == fx["home"] else hi

        store.set(
            fx["id"],
            status="reported",
            result={
                "winner": win,
                "loser": loser,
                "home": home_score,
                "away": away_score,
                "link": (ballchasing or "").strip(),
                "from_files": bool(attached),
                "by": interaction.user.id,
                "by_name": interaction.user.display_name,
                "by_team": team,
            },
            reported_at=iso(now_utc()),
        )

        embed = discord.Embed(
            title=f"{win} win {hi}–{lo}",
            description=f"**{fx['home']}** {home_score} – {away_score} **{fx['away']}**",
            colour=0x2ECC71,
        )
        embed.add_field(
            name="Replays",
            value=(ballchasing.strip() if ballchasing
                   else f"{len(attached)} replay file(s) attached"),
            inline=False,
        )
        embed.add_field(
            name="Reported by",
            value=f"{interaction.user.mention} ({interaction.user.display_name})"
            + (f" — **{team}**" if team else "  ·  admin"),
            inline=False,
        )
        embed.set_footer(
            text=(note + " " if note else "")
            + (f"Week {fx['week']}. " if fx["week"] != store.week else "")
            + ("No time was agreed beforehand. "
               if not st.get("scheduled_iso") and not stats_only else "")
            + f"Dispute window closes in {store.cfg('archive_hours_after_report', 24)}h."
        )
        await interaction.followup.send(embed=embed, view=DisputeView(self.bot))

        try:
            await self.bot.on_result(
            fx, interaction, parsed=pending_files
        )
        except Exception as e:
            import traceback
            traceback.print_exc()
            await self.bot.log(
                f"**on_result failed** for {fx['home']} vs {fx['away']}: "
                f"{type(e).__name__}: {e} — the result is saved, but the sheet "
                "may need `/sheets push`."
            )
        await self.bot.log(
            ("" if st.get("scheduled_iso") or stats_only else "**[no agreed time]** ")
            + f"REPORTED {fx['home']} {home_score}-{away_score} {fx['away']} · "
            f"by {interaction.user.display_name} "
            f"({team or 'admin'}) — "
            + (ballchasing.strip() if ballchasing else f"{len(attached)} replay files")
        )


    @report.autocomplete("winner")
    async def winner_autocomplete(self, interaction: discord.Interaction, current: str):
        return await team_options(self.bot, interaction, current)


async def setup(bot):
    await bot.add_cog(Reporting(bot))
