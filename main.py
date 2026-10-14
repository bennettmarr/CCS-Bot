"""
CCS Season 9 bot.

Run:  python main.py

Nothing is held only in memory. Every timer is derived from timestamps on disk,
so a restart, a power cut or a Windows update reboot loses nothing - on startup
the bot works out what it missed and catches up.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, time as dtime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot.ballchasing import Ballchasing
from bot.sheets import Sheets
from bot.roster import Roster
from bot.stats import Stats, tracker_url
from bot.store import Store, iso, now_utc
from bot.times import EASTERN, discord_ts, short

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("ccsbot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ccs")

RED, YELLOW, GREEN = "🔴", "🟡", "🟢"


class CCSBot(commands.Bot):
    def __init__(self, store: Store):
        intents = discord.Intents.default()
        intents.members = True          # needed to read team roles
        # reading old results posts (/backfill) needs this, and it must also
        # be switched on in the Developer Portal or the bot won't connect
        intents.message_content = bool(store.cfg("message_content_intent", False))
        super().__init__(command_prefix="!ccs ", intents=intents)
        self.store = store
        from bot.cogs import set_public
        set_public(store.cfg("public_replies", True))
        self.bc = Ballchasing(store.cfg("ballchasing_key", ""))
        self.stats = Stats(store.cfg("ballchasing_key", ""))
        self.roster = Roster()
        self.roster_loaded = False
        self.sheets = None
        if store.cfg("sheets_enabled", False):
            sh = Sheets(store)
            ok, msg = sh.connect()
            if ok:
                self.sheets = sh
                log.info("sheets: %s", msg)
            else:
                log.warning("sheets disabled — %s", msg)
                self._sheets_error = msg

    # ------------------------------------------------------------------ setup
    async def setup_hook(self):
        from bot.cogs.reporting import DisputeView
        from bot.cogs.scheduling import ConfirmView

        for ext in ("bot.cogs.scheduling", "bot.cogs.reporting", "bot.cogs.admin",
                    "bot.cogs.backfill"):
            await self.load_extension(ext)

        # persistent buttons - these keep working after a restart
        self.add_view(ConfirmView(self))
        self.add_view(DisputeView(self))

        guild = discord.Object(id=int(self.store.cfg("guild_id")))
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("commands synced")

    async def on_ready(self):
        log.info("logged in as %s", self.user)
        await self.log(
            f"bot started — week {self.store.week}"
            + (
                " · sheets on"
                if self.sheets
                else (
                    f" · sheets OFF ({getattr(self, '_sheets_error', 'not enabled')})"
                    if self.store.cfg("sheets_enabled", False)
                    else ""
                )
            )
        )
        if not self.lifecycle.is_running():
            self.lifecycle.start()
        if not self.role_sweep.is_running():
            self.role_sweep.start()
        if not self.sunday_sweep.is_running():
            self.sunday_sweep.start()
        if not self.name_sweep.is_running():
            hrs = float(self.store.cfg("name_sweep_hours", 4))
            self.name_sweep.change_interval(hours=hrs)
            self.name_sweep.start()
        await self.load_roster()
        await self.catch_up()

    # ------------------------------------------------------- website sheet
    def request_website_sync(self, delay: float = 90):
        """
        Rebuild the website stats sheet shortly. Results that land close
        together (a backfill, a busy night) share one rebuild.
        """
        if not self.sheets or not self.store.cfg("website_sync", True):
            return
        t = getattr(self, "_web_task", None)
        if t and not t.done():
            t.cancel()
        self._web_task = asyncio.create_task(self._website_later(delay))

    async def _website_later(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self.sync_website_now()

    async def sync_website_now(self):
        lock = getattr(self, "_web_lock", None)
        if lock is None:
            lock = self._web_lock = asyncio.Lock()
        async with lock:
            try:
                roster = self.roster if getattr(self, "roster_loaded", False) else None
                ok, msg = await self.sheets.sync_website(roster)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            await self.log(("website stats synced — " if ok else "**website stats sync failed** — ") + msg)
            return ok, msg

    # -------------------------------------------------------------- utilities
    async def log(self, msg: str):
        log.info(msg)
        cid = self.store.cfg("log_channel_id")
        if not cid:
            return
        ch = self.get_channel(int(cid))
        if ch:
            try:
                await ch.send(msg[:1990])
            except discord.HTTPException:
                pass

    def guild(self):
        return self.get_guild(int(self.store.cfg("guild_id")))

    def thread_name(self, fx, state) -> str:
        if state["status"] in ("reported", "archived") and state.get("result"):
            r = state["result"]
            ff = " (FF)" if r.get("forfeit") else ""
            return f"{GREEN} {fx['home']} {r['home']}-{r['away']} {fx['away']}{ff}"[:100]
        if state["status"] == "scheduled" and state.get("scheduled_iso"):
            dt = datetime.fromisoformat(state["scheduled_iso"])
            return f"{YELLOW} {fx['home']} vs {fx['away']} · {short(dt)}"[:100]
        return f"{RED} {fx['home']} vs {fx['away']}"[:100]

    # ------------------------------------------------------- thread creation
    async def create_threads(self, week: int, guild: discord.Guild):
        store = self.store
        parent = guild.get_channel(int(store.cfg("scheduling_channel_id")))
        if parent is None:
            raise RuntimeError("scheduling channel not found")

        # every team role that already has a hand-made channel this week
        taken = set()
        for ch in guild.text_channels:
            from bot.matching import teams_from_overwrites

            t = teams_from_overwrites(store, ch)
            if len(t) == 2:
                fx = store.find_fixture(t[0], t[1], week)
                if fx:
                    store.set(fx["id"], channel_id=ch.id)
                    taken.add(fx["id"])

        created = skipped = failed = 0
        for fx in store.week_fixtures(week):
            st = store.get(fx["id"])

            # a recorded thread that no longer exists shouldn't block a rebuild -
            # threads get deleted by hand, and the record outliving the thread
            # would leave a match with nowhere to talk and no way to fix it
            tid = st.get("thread_id")
            if tid and self.get_channel(tid) is None:
                try:
                    await guild.fetch_channel(tid)
                except discord.NotFound:
                    store.set(fx["id"], thread_id=None)
                    st = store.get(fx["id"])
                except discord.HTTPException:
                    pass
            cid = st.get("channel_id")
            if cid and guild.get_channel(cid) is None:
                store.set(fx["id"], channel_id=None)
                st = store.get(fx["id"])

            if fx["id"] in taken or st.get("thread_id") or st.get("channel_id"):
                skipped += 1
                continue
            try:
                th = await parent.create_thread(
                    name=self.thread_name(fx, st),
                    type=discord.ChannelType.private_thread,
                    invitable=False,          # players can't pull in outsiders
                    auto_archive_duration=10080,
                )
                store.set(fx["id"], thread_id=th.id)
                await self.post_opener(th, fx)
                created += 1
                await asyncio.sleep(1.2)      # stay well inside the rate limit
            except discord.HTTPException as e:
                failed += 1
                log.warning("thread failed for %s: %s", fx["id"], e)
                await asyncio.sleep(3)
        await self.log(
            f"/overflow week {week}: created {created}, skipped {skipped}, failed {failed}"
        )
        return created, skipped, failed

    async def ensure_members(self, thread, fx):
        """
        The role mention normally pulls everyone in. If the bot lacks Mention
        All Roles on the channel it silently doesn't, and nobody gets added -
        so add the role holders directly as well. Discord ignores duplicates.
        """
        store = self.store
        guild = thread.guild
        wanted = []
        roles = [store.team_role_id(fx["home"]), store.team_role_id(fx["away"])]
        if store.cfg("ping_admin_in_threads", True) and store.cfg("admin_role_id"):
            roles.append(store.cfg("admin_role_id"))
        for rid in roles:
            role = guild.get_role(int(rid)) if rid else None
            if role:
                wanted += [m for m in role.members if not m.bot]
        seen = set()
        for m in wanted:
            if m.id in seen:
                continue
            seen.add(m.id)
            try:
                await thread.add_user(m)
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                pass

    async def open_overdue(self, items, guild, notify: bool = True):
        """Open a thread for each overdue match that has nowhere to talk."""
        store = self.store
        parent = guild.get_channel(int(store.cfg("scheduling_channel_id")))
        created = existing = 0
        for fx, st, due in items:
            if st.get("thread_id") or st.get("channel_id"):
                existing += 1
                if notify:
                    ch = self.get_channel(st.get("thread_id") or st.get("channel_id"))
                    if ch:
                        try:
                            await self.nudge(ch, fx, due)
                        except discord.HTTPException:
                            pass
                continue
            if parent is None:
                continue
            try:
                th = await parent.create_thread(
                    name=self.thread_name(fx, st),
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    auto_archive_duration=10080,
                )
                store.set(fx["id"], thread_id=th.id)
                await self.post_opener(th, fx)
                if notify:
                    await self.nudge(th, fx, due)
                created += 1
                await asyncio.sleep(1.2)
            except discord.HTTPException as e:
                log.warning("overdue thread failed for %s: %s", fx["id"], e)
                await asyncio.sleep(3)
        await self.log(f"/overdue — {len(items)} overdue, {created} threads opened")
        return created, existing

    async def nudge(self, channel, fx, due):
        store = self.store
        hr = store.team_role_id(fx["home"])
        ar = store.team_role_id(fx["away"])
        await channel.send(
            content=f"<@&{hr}> <@&{ar}>",
            embed=discord.Embed(
                title="This match is overdue",
                description=(
                    f"The deadline was {discord_ts(due)}.\n\n"
                    "Get it played as soon as you can, or ask an admin for an "
                    "extension. Admins have been notified."
                ),
                colour=0xE74C3C,
            ),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    async def post_opener(self, thread: discord.Thread, fx: dict):
        store = self.store
        hr = store.team_role_id(fx["home"])
        ar = store.team_role_id(fx["away"])
        pings = f"<@&{hr}> <@&{ar}>"
        if store.cfg("ping_admin_in_threads", True):
            admin = store.cfg("admin_role_id")
            if admin:
                pings += f" <@&{admin}>"
        deadline = store.cfg("play_deadline", "Saturday 11:59pm ET")
        embed = discord.Embed(
            title=f"{fx['home']} vs {fx['away']}",
            description=(
                f"Week {fx['week']} · best of {store.cfg('series_length', 5)}\n\n"
                "**Agree a time here.**\n"
                "`/schedule tuesday 8pm` — the other team then confirms.\n"
                "Also fine: `9/27 9pm`, `sept 27 8pm`, `tomorrow 7:30pm`.\n"
                "Times are Eastern and show in your own timezone.\n\n"
                "**After you play**\n"
                "`/report` with the winner and the score, then either attach the "
                "replay files (`game1`…`game5`) or give a ballchasing group link.\n\n"
                f"Played by {deadline}, or ask an admin for an extension."
            ),
            colour=0xE74C3C,
        )
        await thread.send(
            content=pings,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await self.ensure_members(thread, fx)

    # ------------------------------------------------------------- lifecycle
    async def on_time_confirmed(
        self,
        fx,
        dt,
        interaction,
        by_admin: bool = False,
        proposal: dict = None,
        confirmed_by=None,
        confirming_team: str = None,
    ):
        store = self.store
        st = store.get(fx["id"])
        await self.rename(fx, st)
        if self.sheets:
            try:
                ok, msg = await self.sheets.write_time(fx, dt)
            except Exception as e:
                log.exception("caster write blew up")
                ok, msg = False, f"unexpected error: {type(e).__name__}: {e}"
            store.set(fx["id"], caster_written=ok)
            await self.log(
                (f"caster sheet updated ({msg}) — " if ok else "CASTER WRITE FAILED — ")
                + f"{fx['home']} vs {fx['away']}"
                + ("" if ok else f": {msg}")
            )
        hr = store.team_role_id(fx["home"])
        ar = store.team_role_id(fx["away"])
        await interaction.followup.send(
            content=f"<@&{hr}> <@&{ar}>",
            embed=discord.Embed(
                title="Time confirmed",
                description=(
                    f"**{fx['home']}** vs **{fx['away']}**\n\n"
                    f"{discord_ts(dt)}\n({discord_ts(dt, 'R')})\n\n"
                    "When you've played, report it with `/report` and your "
                    "ballchasing group link."
                ),
                colour=0xF1C40F,
            )
            .add_field(
                name="Proposed by",
                value=(
                    f"<@{proposal['by_user']}> ({proposal.get('by_name', 'unknown')}) "
                    f"— **{proposal['by_team']}**"
                    + ("  ·  admin" if proposal.get("by_admin") else "")
                )
                if proposal
                else "unknown",
                inline=False,
            )
            .add_field(
                name="Confirmed by",
                value=(
                    f"{confirmed_by.mention} ({confirmed_by.display_name})"
                    f" — **{confirming_team}**"
                    + ("  ·  admin override" if by_admin else "")
                )
                if confirmed_by
                else "unknown",
                inline=False,
            ),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await self.log(
            f"CONFIRMED {fx['home']} vs {fx['away']} — {short(dt)} · "
            f"proposed by {(proposal or {}).get('by_name', '?')} "
            f"({(proposal or {}).get('by_team', '?')}), "
            f"confirmed by {getattr(confirmed_by, 'display_name', '?')} ({confirming_team})"
            + (" [admin override]" if by_admin else "")
        )

    async def on_result(self, fx, interaction, corrected: bool = False, parsed=None,
                        wait_stats: bool = False):
        store = self.store
        st = store.get(fx["id"])
        try:
            await self.rename(fx, st)
        except Exception:
            log.exception("rename failed after result")
        if not self.sheets:
            # nothing below runs without a connection, and a silent no-op is
            # how a whole night's results end up missing from the sheets with
            # nobody noticing
            await self.log(
                f"**result recorded but NOT written to the sheets** — "
                f"{fx['home']} {st['result']['home']}-{st['result']['away']} "
                f"{fx['away']}. Sheets isn't connected"
                + (
                    f" ({self._sheets_error})"
                    if getattr(self, "_sheets_error", None)
                    else " (sheets_enabled is false)"
                )
                + ". Run `/sheets push` here once it is."
            )
            return

        if self.sheets:
            try:
                if store.cfg("write_grid_scores", True):
                    ok, msg = await self.sheets.write_score(fx, st["result"])
                else:
                    ok, msg = True, "grid write off"
            except Exception as e:
                log.exception("sheet write blew up")
                ok, msg = False, f"unexpected error: {type(e).__name__}: {e}"
            # the results tab is written either way - it's the bot's own record,
            # and it's most useful precisely when the schedule write failed
            try:
                lok, lmsg = await self.sheets.log_result(fx, st["result"], st)
                if not lok:
                    await self.log(f"results tab failed — {lmsg}")
            except Exception as e:
                log.exception("results tab blew up")
                await self.log(
                    f"**results tab failed** — {fx['home']} vs {fx['away']}: "
                    f"{type(e).__name__}: {e}"
                )
            if not (st.get("result") or {}).get("forfeit"):
                if wait_stats:
                    # a backfill writes dozens of results in a row; detached
                    # stats tasks would all hit the Sheets quota at once
                    await self.collect_stats(fx, st, parsed=parsed)
                else:
                    asyncio.create_task(self.collect_stats(fx, st, parsed=parsed))
            store.set(fx["id"], sheet_written=ok)
            if ok:
                await self.log(
                    f"sheet updated ({msg}) — {fx['home']} "
                    f"{st['result']['home']}-{st['result']['away']} {fx['away']}"
                )
            else:
                # the result is safe in state.json either way, so this is a
                # "fix the sheet" warning, not a lost result
                await self.log(
                    f"**SHEET WRITE FAILED** {fx['home']} vs {fx['away']}: {msg}"
                )
                admin = store.cfg("admin_role_id")
                ch = self.get_channel(int(store.cfg("log_channel_id")))
                if ch and admin:
                    try:
                        await ch.send(
                            f"<@&{admin}> the score above needs entering by hand.",
                            allowed_mentions=discord.AllowedMentions(roles=True),
                        )
                    except discord.HTTPException:
                        pass

    async def load_roster(self):
        """Read the Roster Sheet once at startup so stat lines can be checked."""
        if not self.sheets or not self.store.cfg("check_rosters", True):
            return
        grid, err = await self.sheets.read_roster()
        if grid is None:
            await self.log(f"roster not loaded — {err}")
            return
        try:
            self._roster_sig = hash(repr(grid))
            self.roster.load_grid(grid, teams_known=set(self.store.teams))
            import os as _os

            # Steam custom-URL links -> real ids (cached; only new ones go to Steam)
            try:
                got, left = await asyncio.to_thread(
                    self.roster.resolve_vanity,
                    _os.path.join(_os.path.dirname(__file__), "data", "steam_vanity.json"),
                )
                if got or left:
                    await self.log(
                        f"steam custom links — {got} turned into real ids"
                        + (f", {left} couldn't be looked up (matched by name instead)" if left else "")
                    )
            except Exception:
                log.exception("steam custom link lookup failed")

            path = _os.path.join(_os.path.dirname(__file__), "data", "identities.json")
            restored = self.roster.load_learned(path)
            self.roster_loaded = True
            r = self.roster
            await self.log(
                f"roster loaded from {getattr(self.sheets, 'roster_source', 'the Roster Sheet')} — "
                f"{len(r.players)} players across "
                f"{len(r.teams)} teams"
                + (f", {restored} remembered ids" if restored else "")
            )
            if r.header_fixes:
                await self.log(
                    "roster headers matched loosely — "
                    + "; ".join(f"{a} → {b}" for a, b in r.header_fixes)
                )
            # the sheet's own title is the one unknown header that's expected
            odd = [h for h in r.unknown_headers if "roster sheet" not in h.lower()]
            if odd:
                await self.log(
                    "roster headers I couldn't match to a team (add them to "
                    "data/aliases.json) — " + "; ".join(odd)
                    + (f". Players under them left unplaced: {', '.join(r.unplaced[:20])}"
                       if r.unplaced else "")
                )
        except Exception:
            log.exception("roster parse failed")

    async def collect_stats(self, fx, st, parsed=None):
        """
        Record per-game player stats for a finished series.

        Two sources, same shapes downstream: the replay files the teams
        attached, already parsed by the time we get here, or a ballchasing
        group we go and fetch. Files are preferred when present - they cost
        nothing and can't be rate-limited.

        Runs detached from the command so nobody waits on it to see their
        result confirmed. If it fails the result is untouched and
        `/stats pull` retries.
        """
        if not self.sheets or not self.store.cfg("collect_stats", True):
            return
        result = st.get("result") or {}
        try:
            if parsed:
                game_rows, rows = parsed
            else:
                link = result.get("link", "")
                gid = self.bc.group_id(link)
                if not gid:
                    return
                rows, game_rows, err = await self.stats.group_games(gid)
                if err or not rows:
                    await self.log(
                        f"stats — nothing pulled for {fx['id']}: {err or 'empty'}"
                    )
                    return
            Stats.attribute_teams(rows, result["winner"], result["loser"], result.get("win_side"))
            if self.roster_loaded:
                Stats.apply_roster(rows, result["winner"], result["loser"], self.roster.team_of)
            Stats.attribute_game_teams(
                game_rows, rows, result["winner"], result["loser"]
            )
            flagged = []
            for r in rows:
                r["tracker"] = tracker_url(r["platform"], r["platform_id"])
                if self.roster_loaded:
                    flag, detail, rec = self.roster.check(
                        r["platform"], r["platform_id"], r["name"], r.get("team", "")
                    )
                    r["flag"], r["flag_detail"] = flag, detail
                    # the roster's spelling of the name is the one stats use
                    if rec:
                        r["roster_name"] = rec["name"]
                    if flag:
                        flagged.append(f"{flag}: {detail}")
            # one series at a time: two writers (live feed + catch-up) reading
            # the tab at once both think a game is new and write it twice
            lock = getattr(self, "_stats_lock", None)
            if lock is None:
                lock = self._stats_lock = asyncio.Lock()
            async with lock:
                sok, smsg = await self.sheets.write_game_scores(fx, game_rows)
                ok, msg = await self.sheets.write_game_stats(fx, rows)
            await self.log(
                (f"game scores — {smsg}" if sok else f"game scores failed — {smsg}")
                + f" ({fx['home']} vs {fx['away']})"
            )
            await self.log(
                (f"stats — {msg}" if ok else f"stats write failed — {msg}")
                + f" ({fx['home']} vs {fx['away']})"
            )
            if ok:
                self.request_website_sync()
            if self.roster.newly_learned:
                import os as _os

                self.roster.save_learned(
                    _os.path.join(_os.path.dirname(__file__), "data", "identities.json")
                )
                learned = sorted(set(self.roster.newly_learned))
                self.roster.newly_learned.clear()
                await self.log("linked — " + "; ".join(learned[:10]))
            if flagged:
                # one line per distinct problem, not per game
                unique = sorted(set(flagged))
                admin = self.store.cfg("admin_role_id")
                ch = self.get_channel(int(self.store.cfg("log_channel_id")))
                if ch:
                    try:
                        await ch.send(
                            content=f"<@&{admin}>" if admin else None,
                            embed=discord.Embed(
                                title=f"Roster check — {fx['home']} vs {fx['away']}",
                                description="\n".join(f"• {u}" for u in unique[:15]),
                                colour=0xE67E22,
                            ),
                            allowed_mentions=discord.AllowedMentions(roles=True),
                        )
                    except discord.HTTPException:
                        pass
        except Exception:
            log.exception("stats collection blew up")

    async def rename(self, fx, st):
        """
        Discord allows about two thread renames per ten minutes. When that
        limit is hit, discord.py waits it out - which would block whatever
        called us, and a slash command that doesn't answer within three
        seconds is dead. So the rename gets a short leash and is dropped if
        it can't happen now. The name is cosmetic; the state is what matters,
        and the next state change renames it anyway.
        """
        name = self.thread_name(fx, st)
        tid = st.get("thread_id")
        if not tid:
            return
        th = self.get_channel(tid)
        if th is None:
            return
        try:
            await asyncio.wait_for(th.edit(name=name), timeout=3)
        except asyncio.TimeoutError:
            log.warning("rename rate-limited, skipped: %s", name)
        except discord.HTTPException as e:
            log.warning("rename failed: %s", e)
        except Exception as e:
            log.warning("rename errored: %s", e)

    async def nag_proposals(self):
        """
        A proposal nobody has confirmed goes stale quietly. Every couple of
        hours, ping the team that owes an answer - capped, so it nudges rather
        than nags forever.
        """
        store = self.store
        gap = timedelta(hours=float(store.cfg("proposal_nudge_hours", 2)))
        cap = int(store.cfg("proposal_nudge_max", 3))
        now = now_utc()
        for fid, st in list(store.state.items()):
            prop = st.get("proposed")
            if not prop or st["status"] == "reported":
                continue
            fx = store.by_id.get(fid)
            if not fx:
                continue
            nudges = int(prop.get("nudges", 0))
            if nudges >= cap:
                continue
            since = prop.get("at")
            if not since:
                continue
            last = datetime.fromisoformat(since) + gap * (nudges + 1)
            if now < last:
                continue

            # if the proposed time itself has passed, say so instead
            when = datetime.fromisoformat(prop["iso"])
            owed = fx["away"] if prop["by_team"] == fx["home"] else fx["home"]
            rid = store.team_role_id(owed)
            ch = self.get_channel(st.get("thread_id") or st.get("channel_id") or 0)
            if ch is None:
                continue
            stale = when < now
            try:
                await ch.send(
                    content=f"<@&{rid}>",
                    embed=discord.Embed(
                        title="Still waiting on a time",
                        description=(
                            f"**{prop['by_team']}** proposed {discord_ts(when)}"
                            + (
                                " — that time has now passed, so someone needs to "
                                "propose a new one with `/schedule`."
                                if stale
                                else f" ({discord_ts(when, 'R')}).\n\n"
                                f"**{owed}** still needs to confirm or decline it."
                            )
                        ),
                        colour=0xF39C12,
                    ),
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except discord.HTTPException:
                continue
            prop["nudges"] = nudges + 1
            store.save()
            await self.log(
                f"nudge {nudges + 1}/{cap} — {fx['home']} vs {fx['away']}, "
                f"waiting on {owed}"
            )

    @tasks.loop(minutes=10)
    async def lifecycle(self):
        """Archive 24h after a report, delete 7 days after archiving."""
        await self.nag_proposals()
        store = self.store
        hours = int(store.cfg("archive_hours_after_report", 24))
        days = int(store.cfg("delete_days_after_archive", 7))
        now = now_utc()
        for fid, st in list(store.state.items()):
            fx = store.by_id.get(fid)
            if not fx:
                continue
            tid = st.get("thread_id")
            if not tid:
                continue

            if st["status"] == "reported" and st.get("reported_at"):
                due = datetime.fromisoformat(st["reported_at"]) + timedelta(hours=hours)
                if now >= due and not st.get("disputed"):
                    th = self.get_channel(tid)
                    if th:
                        try:
                            await th.edit(archived=True, locked=True)
                        except discord.HTTPException:
                            pass
                    store.set(fid, status="archived", archived_at=iso(now))
                    await self.log(f"archived {fx['home']} vs {fx['away']}")

            elif st["status"] == "archived" and st.get("archived_at") and days > 0:
                due = datetime.fromisoformat(st["archived_at"]) + timedelta(days=days)
                if now >= due:
                    r = st.get("result") or {}
                    await self.log(
                        f"deleting thread — {fx['home']} {r.get('home','?')}-"
                        f"{r.get('away','?')} {fx['away']} · {r.get('link','no link')}"
                    )
                    th = self.get_channel(tid)
                    if th:
                        try:
                            await th.delete()
                        except discord.HTTPException:
                            pass
                    store.set(fid, thread_id=None)

    @tasks.loop(time=dtime(hour=1, tzinfo=EASTERN))
    async def sunday_sweep(self):
        """
        1am Sunday: the week just ended at 11:59pm Saturday. Anything from it
        without a score is overdue - open a thread for any that has nowhere to
        talk, ping both teams, and post the list for admins.
        """
        now = now_utc().astimezone(EASTERN)
        if now.weekday() != 6:          # 6 = Sunday
            return
        if self.store.cfg("stats_only", False):
            # nobody schedules through the bot in this mode, so every match
            # would look overdue - opening 200 threads and pinging everyone
            return
        await self.run_overdue_sweep()

    async def run_overdue_sweep(self):
        store = self.store
        guild = self.guild()
        if guild is None:
            return
        just_ended = store.week_for(now_utc()) - 1
        if just_ended < 1:
            return
        items = [x for x in store.overdue(just_ended) if x[0]["week"] == just_ended]
        if not items:
            await self.log(f"Sunday sweep — week {just_ended} all reported, nothing overdue")
            return
        created, existing = await self.open_overdue(items, guild, notify=True)

        lines = [
            f"• **{fx['home']}** vs **{fx['away']}** — "
            + ("scheduled but not played" if st["status"] == "scheduled" else "never scheduled")
            for fx, st, _ in items[:40]
        ]
        more = f"\n…and {len(items) - 40} more" if len(items) > 40 else ""
        embed = discord.Embed(
            title=f"Week {just_ended} overdue — {len(items)} match"
            + ("es" if len(items) != 1 else ""),
            description="\n".join(lines) + more,
            colour=0xE74C3C,
        )
        embed.set_footer(text=f"{created} thread(s) opened, {existing} already had one")
        target = store.cfg("overdue_channel_id") or store.cfg("log_channel_id")
        ch = guild.get_channel(int(target)) if target else None
        if ch:
            await ch.send(
                content=f"<@&{store.cfg('admin_role_id')}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

    @tasks.loop(hours=4)
    async def name_sweep(self):
        """
        Renames get dropped when Discord rate-limits them, so a thread can sit
        with a stale name after a time or a result landed. Every few hours,
        walk the open threads and fix any whose name no longer matches their
        state. Only mismatches are touched, so on a quiet run this costs
        nothing.
        """
        store = self.store
        fixed = skipped = 0
        for fid, st in list(store.state.items()):
            tid = st.get("thread_id")
            if not tid or st["status"] == "archived":
                continue
            fx = store.by_id.get(fid)
            if not fx:
                continue
            th = self.get_channel(tid)
            if th is None:
                continue
            want = self.thread_name(fx, st)
            if th.name == want:
                continue
            try:
                await asyncio.wait_for(th.edit(name=want), timeout=10)
                fixed += 1
                await asyncio.sleep(2)
            except asyncio.TimeoutError:
                skipped += 1
            except discord.HTTPException:
                skipped += 1
            except Exception as e:
                log.warning("name sweep: %s", e)
                skipped += 1
        if fixed or skipped:
            await self.log(
                f"name sweep — {fixed} renamed"
                + (f", {skipped} still rate-limited" if skipped else "")
            )

    @tasks.loop(time=dtime(hour=5, tzinfo=EASTERN))
    async def role_sweep(self):
        """Every 5am, add anyone who has joined a team role since."""
        await self.do_role_sweep()

    async def do_role_sweep(self):
        store = self.store
        guild = self.guild()
        if guild is None:
            return
        added = checked = 0
        for fid, st in list(store.state.items()):
            tid = st.get("thread_id")
            if not tid or st["status"] == "archived":
                continue
            fx = store.by_id.get(fid)
            th = self.get_channel(tid)
            if th is None or fx is None:
                continue
            checked += 1
            want = set()
            rids = [store.team_role_id(fx["home"]), store.team_role_id(fx["away"])]
            if store.cfg("ping_admin_in_threads", True) and store.cfg("admin_role_id"):
                rids.append(store.cfg("admin_role_id"))
            for rid in rids:
                role = guild.get_role(int(rid)) if rid else None
                if role:
                    want |= {m.id for m in role.members if not m.bot}
            try:
                have = {m.id for m in await th.fetch_members()}
            except discord.HTTPException:
                continue
            for uid in want - have:
                try:
                    await th.add_user(discord.Object(id=uid))
                    added += 1
                    await asyncio.sleep(0.6)
                except discord.HTTPException:
                    pass
        if checked:
            await self.log(f"role sweep — {checked} threads checked, {added} members added")

    async def on_member_update(self, before, after):
        """Live version of the sweep: a role change is picked up immediately."""
        if before.roles == after.roles:
            return
        gained = {r.id for r in after.roles} - {r.id for r in before.roles}
        if not gained:
            return
        store = self.store
        for rid in gained:
            team = store.team_for_role(rid)
            if not team:
                continue
            for fid, st in store.state.items():
                fx = store.by_id.get(fid)
                if not fx or st["status"] == "archived" or not st.get("thread_id"):
                    continue
                if team in (fx["home"], fx["away"]):
                    th = self.get_channel(st["thread_id"])
                    if th:
                        try:
                            await th.add_user(after)
                        except discord.HTTPException:
                            pass

    async def catch_up(self):
        """On startup, do anything the bot slept through."""
        await self.lifecycle()
        await self.do_role_sweep()
        await self.log("startup catch-up done")
        # brings the website sheet up to date with everything recorded so far
        self.request_website_sync(delay=30)
        # record anything posted in the results channel while the bot was off
        cog = self.get_cog("Backfill")
        asyncio.create_task(self._after_startup(cog))

    async def _after_startup(self, cog):
        """Catch up on results, then re-check every stat line against the roster."""
        if cog is not None and hasattr(cog, "auto_catch_up"):
            try:
                await cog.auto_catch_up()
            except Exception:
                log.exception("auto catch-up failed")
        if self.store.cfg("startup_roster_check", True):
            try:
                await self.startup_roster_check()
            except Exception:
                log.exception("startup roster check failed")
        if self.store.cfg("roster_watch", True) and not self.roster_watch.is_running():
            self.roster_watch.change_interval(minutes=float(self.store.cfg("roster_watch_minutes", 10)))
            self.roster_watch.start()

    async def roster_check(self, relabel: bool = True):
        """
        Re-check every recorded player against the roster as it is right now.
        Returns a dict with the audit files, counts, and what changed since the
        last check (fixed / new), or None if the roster couldn't be read.
        """
        from bot import roster_issues

        lock = getattr(self, "_check_lock", None)
        if lock is None:
            lock = self._check_lock = asyncio.Lock()
        async with lock:
            await self.load_roster()
            if not self.roster_loaded:
                return None
            relabel_msg = ""
            if relabel:
                ok, relabel_msg = await self.sheets.relabel_game_stats(self.roster.team_of, self.roster)
            stats = await self.sheets.read_game_stats()
            scores = await self.sheets.read_game_scores()
            fixtures = {f["id"]: f for f in self.store.fixtures}
            acc_csv, games_csv, n_acc, n_games, counts = await asyncio.to_thread(
                roster_issues.audit, stats, scores, fixtures, self.roster, self.store.state)
            had_last, changes, fixed, new = roster_issues.compare_with_last(acc_csv)
            if relabel:
                self.request_website_sync(delay=30)
            return {"acc_csv": acc_csv, "games_csv": games_csv, "n_acc": n_acc, "n_games": n_games,
                    "counts": counts, "had_last": had_last, "changes": changes,
                    "fixed": fixed, "new": new, "relabel": relabel_msg}

    @staticmethod
    def check_summary(res, limit: int = 1500) -> str:
        """Short Discord text for a roster check: totals plus who got fixed / newly broken."""
        from bot import roster_issues
        by_prio = {1: 0, 2: 0, 3: 0}
        for k, n in res["counts"].items():
            by_prio[roster_issues.AUDIT.get(k, (2, ""))[0]] += n
        lines = [f"**{res['n_acc']}** accounts still not exactly on the roster "
                 f"(1 real problems: {by_prio[1]} · 2 need a look: {by_prio[2]} · 3 fine: {by_prio[3]})"]
        if res["had_last"]:
            lines.append(f"✅ **Fixed since last check: {len(res['fixed'])}**")
            for v in res["fixed"]:
                lines.append(f"  • {v[0]} — {v[1]} ({v[2]})")
            if res["new"]:
                lines.append(f"⚠️ **New since last check: {len(res['new'])}** (new games, or a roster edit broke a match)")
                for v in res["new"]:
                    lines.append(f"  • {v[0]} — {v[1]} ({v[2]}) — {v[3].lower()}")
        text, used = [], 0
        for ln in lines:
            if used + len(ln) > limit:
                text.append("  …more in the attached file")
                break
            text.append(ln)
            used += len(ln) + 1
        return "\n".join(text)

    @tasks.loop(minutes=10)
    async def roster_watch(self):
        """
        Every few minutes, look at the roster. If anyone has edited it since the
        last check, re-check everything and post what got fixed - so whoever is
        fixing trackers sees it work without running anything.
        """
        import io

        if not self.sheets or not self.roster_loaded:
            return
        try:
            grid, err = await self.sheets.read_roster()
        except Exception:
            return
        if grid is None:
            return
        sig = hash(repr(grid))
        last_poll, self._roster_poll_sig = getattr(self, "_roster_poll_sig", None), sig
        if sig == getattr(self, "_roster_sig", None):
            return
        # still being edited (changed since the last look) - wait until it
        # sits still for one interval, so a busy editing session triggers one
        # re-check instead of one every 10 minutes
        if sig != last_poll:
            return
        try:
            res = await self.roster_check(relabel=True)
        except Exception:
            # never let one bad check (a Google hiccup) stop the watcher
            log.exception("roster re-check failed - will try again next time")
            return
        if res is None or not (res["fixed"] or res["new"]):
            return
        text = "**Roster edited — re-checked**\n" + self.check_summary(res)
        log.info(text.replace("**", ""))
        cid = self.store.cfg("roster_check_channel_id") or self.store.cfg("log_channel_id")
        ch = self.get_channel(int(cid)) if cid else None
        if ch:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            try:
                await ch.send(text[:1990], files=[discord.File(
                    io.BytesIO(res["changes"].encode("utf-8")), filename=f"roster_changes_{stamp}.txt")])
            except discord.HTTPException:
                pass

    async def startup_roster_check(self):
        """
        What /stats relabel and /accountaudit do, run by itself at startup so
        roster fixes show up without anyone typing a command. Results (with the
        audit files) go to the log channel.
        """
        import io
        from bot import roster_issues

        if not self.sheets:
            return
        await self.load_roster()
        if not self.roster_loaded:
            return
        ok, msg = await self.sheets.relabel_game_stats(self.roster.team_of, self.roster)
        await self.log(("roster re-check — " if ok else "roster re-check failed — ") + msg)
        stats = await self.sheets.read_game_stats()
        scores = await self.sheets.read_game_scores()
        fixtures = {f["id"]: f for f in self.store.fixtures}
        acc_csv, games_csv, n_acc, n_games, counts = await asyncio.to_thread(
            roster_issues.audit, stats, scores, fixtures, self.roster, self.store.state)
        had_last, changes, fixed, new = roster_issues.compare_with_last(acc_csv)
        n_fixed, n_new = len(fixed), len(new)
        stamp = datetime.now().strftime("%Y-%m-%d")
        lines = [f"**Account audit** — {n_acc} accounts not exactly on the roster, across {n_games} games."]
        if had_last:
            lines.append(f"**Since the last audit:** {n_fixed} fixed · {n_new} new")
        lines += roster_issues.summary_lines(counts)
        gaps = roster_issues.missing_stats(self.store.state, fixtures, stats, scores)
        if gaps:
            lines.append(f"⚠️ **{len(gaps)} reported series are missing stats or game scores** "
                         "— redo each with `/rerecord link:<results post>`:")
            for fx, link, hs, hc in gaps[:12]:
                what = "no stats or scores" if not (hs or hc) else "no stats" if not hs else "no game scores"
                lines.append(f"  • wk{fx['week']} {fx['home']} vs {fx['away']} — {what}"
                             + (f" · <{link}>" if link else ""))
            if len(gaps) > 12:
                lines.append(f"  …and {len(gaps) - 12} more")
        text = "\n".join(lines)
        log.info(text.replace("**", ""))
        cid = self.store.cfg("log_channel_id")
        ch = self.get_channel(int(cid)) if cid else None
        if ch:
            files = [discord.File(io.BytesIO(acc_csv.encode("utf-8")),
                                  filename=f"account_audit_accounts_{stamp}.csv")]
            if changes:
                files.append(discord.File(io.BytesIO(changes.encode("utf-8")),
                                          filename=f"account_audit_changes_{stamp}.txt"))
            try:
                await ch.send(text[:1990], files=files)
            except discord.HTTPException:
                pass
        self.request_website_sync(delay=30)


def main():
    store = Store()
    token = store.cfg("token")
    if not token or token.startswith("PASTE"):
        raise SystemExit("Put your bot token in config.json first.")
    bot = CCSBot(store)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
