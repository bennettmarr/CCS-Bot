"""
/backfill - read an existing results channel and record every series in it.

Teams already post "UC 3-0 Xavier" with the replay files attached. This
walks that channel from a start date, places each post on its fixture (see
bot/backfill.py for how), and reports what it found as a text file. Nothing
is written until it's run again with apply:true.

Replays are downloaded once and kept in memory, so the apply run after a dry
run doesn't fetch them all again. Already-reported matches are skipped, so
running it twice is harmless. If a match was posted twice, the later post
wins and the earlier one is listed.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime, time as dtime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..backfill import finished, merge_parsed, place
from ..matching import is_admin
from ..ballchasing import GROUP_RE
from ..replayfile import games_from_files
from ..store import iso, now_utc
from . import private

EASTERN_HINT = "YYYY-MM-DD"


LIVE_WAIT = 600        # seconds to wait for the rest of a series split over posts
REACT = {"ok": "\u2705", "check": "\u26a0\ufe0f", "manual": "\u2753", "dupe": "\U0001f501"}


class Backfill(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache = {}        # message id -> (games, rows, problems)
        self.running = False
        self.pending = {}      # author id -> unfinished series waiting for more posts
        self.deferred = []     # posts that landed while a catch-up was running
        self.live_lock = asyncio.Lock()

    # ------------------------------------------------------------ live feed
    def _live_channel(self) -> int:
        cid = self.bot.store.cfg("results_channel_id", 1155041977222570035)
        return int(cid) if cid else 0

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        """
        A result posted in the results channel is recorded as it lands - the
        same matching /backfill uses (poster role, "A 3-0 B" text, players in
        the replays), the same writes, and a reaction so everyone can see it
        was picked up:
          \u2705 recorded   \u26a0\ufe0f recorded, but flagged (see the log)
          \u2753 couldn't place it - enter by hand   \U0001f501 already recorded
        A series split over several posts by the same person is joined up
        (up to 10 minutes apart) before anything is written.
        """
        store = self.bot.store
        if (msg.author.bot or not msg.guild
                or not store.cfg("live_results", True)
                or msg.channel.id != self._live_channel()):
            return
        if self.running:
            # a catch-up or backfill is reading the channel - handle this one after
            self.deferred.append(msg)
            return
        has_files = any(a.filename.lower().endswith(".replay") for a in msg.attachments)
        if not has_files and not GROUP_RE.search(msg.content or ""):
            return
        try:
            if has_files:
                games, rows, problems = await self._parse(msg)
                link = ""
            else:
                games, rows, problems, link = await self._parse_group(msg)
        except Exception as e:
            return await self.bot.log(f"live result — couldn't read {msg.jump_url}: {type(e).__name__}: {e}")

        need = int(store.cfg("wins_needed", 3))
        item = {"msgs": [msg], "games": games, "rows": rows,
                "problems": list(problems), "link": link}
        prev = self.pending.pop(msg.author.id, None)
        if prev and (msg.created_at - prev["msgs"][-1].created_at).total_seconds() <= LIVE_WAIT:
            prev["task"].cancel()
            prev["games"], prev["rows"] = merge_parsed(
                [(prev["games"], prev["rows"]), (games, rows)])
            prev["msgs"].append(msg)
            prev["problems"] += item["problems"]
            prev["link"] = prev.get("link") or link
            item = prev
        elif prev:
            # an older unfinished post from the same person - settle it first
            prev["task"].cancel()
            asyncio.create_task(self._live_place(prev))

        if finished(item["games"], item["rows"], need) or not item["games"]:
            await self._live_place(item)
        else:
            # not a full series yet - give them time to post the rest
            item["task"] = asyncio.create_task(self._live_later(msg.author.id, item))
            self.pending[msg.author.id] = item

    async def _live_later(self, author_id, item):
        try:
            await asyncio.sleep(LIVE_WAIT)
        except asyncio.CancelledError:
            return
        if self.pending.get(author_id) is item:
            self.pending.pop(author_id, None)
        await self._live_place(item)

    async def _live_place(self, item):
        store, bot = self.bot.store, self.bot
        msg = item["msgs"][0]
        async with self.live_lock:
            roster = bot.roster if getattr(bot, "roster_loaded", False) else None
            member = msg.author if isinstance(msg.author, discord.Member) else msg.guild.get_member(msg.author.id)
            poster_teams = [store.team_for_role(r.id) for r in getattr(member, "roles", [])]
            post = {
                "week": store.week_for(msg.created_at),
                "poster_teams": [t for t in poster_teams if t],
                "text": "\n".join(m.content for m in item["msgs"] if m.content),
                "games": item["games"], "rows": item["rows"], "link": item.get("link", ""),
            }
            v = place(store, post, roster) if item["games"] else {
                "status": "unplaced", "why": "; ".join(item["problems"]) or "no readable replays"}
            v["msg"], v["msgs"], v["post"], v["problems"] = msg, item["msgs"], post, item["problems"]

            status = v["status"]
            if status == "matched":
                try:
                    await self._record(v)
                except Exception as e:
                    await bot.log(f"live result — write failed for {msg.jump_url}: {type(e).__name__}: {e}")
                    return await self._react(item["msgs"][-1], "manual")
                fx = v["fx"]
                line = (f"**live** — wk{fx['week']} {fx['home']} {v['home']}-{v['away']} {fx['away']}"
                        f" recorded · {msg.jump_url}")
                if v.get("flags"):
                    line += "\n" + "\n".join(f"• {t}" for _, t in v["flags"][:8])
                await bot.log(line)
                await self._react(item["msgs"][-1], "check" if v.get("flags") else "ok")
            elif status == "already":
                await self._react(item["msgs"][-1], "dupe")
                await bot.log(f"live — already recorded, skipped · {msg.jump_url}")
            else:
                why = {"far": "placed far from its week", "mismatch": "score or winner disagrees",
                       "ambiguous": "could be more than one match",
                       "unplaced": "couldn't place it"}.get(status, status)
                detail = "\n".join(self._report_lines(v))
                admin = store.cfg("admin_role_id")
                await bot.log(
                    (f"<@&{admin}> " if store.cfg("ping_admin_in_threads", False) and admin else "")
                    + f"**live — enter by hand:** {why} · {msg.jump_url}\n```{detail[:1500]}```")
                await self._react(item["msgs"][-1], "manual")

    # -------------------------------------------------------- startup catch-up
    CHECKPOINT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "catchup.json")

    def _checkpoint(self):
        """Where the last catch-up got to; else the newest recorded result."""
        try:
            with open(self.CHECKPOINT, encoding="utf-8") as f:
                return datetime.fromisoformat(json.load(f)["last_message"])
        except Exception:
            pass
        times = [st.get("reported_at") for st in self.bot.store.state.values() if st.get("reported_at")]
        if times:
            # first ever catch-up: go back a few days past the newest result,
            # so anything posted between the last /backfill and now is found
            return datetime.fromisoformat(max(times)) - timedelta(days=3)
        return datetime.combine(
            datetime.fromisoformat(self.bot.store.cfg("week1_start", "2026-09-20")).date(),
            dtime(0), tzinfo=now_utc().tzinfo)

    def _save_checkpoint(self, when):
        try:
            tmp = self.CHECKPOINT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_message": when.isoformat()}, f)
            os.replace(tmp, self.CHECKPOINT)
        except Exception:
            pass

    async def auto_catch_up(self):
        """
        Run at every startup: read the results channel from where the bot left
        off (with a few hours' overlap - anything already recorded is skipped),
        record what's new, react to each post, and log a summary. So the bot
        can be off overnight and catch itself up when it's started.
        """
        bot, store = self.bot, self.bot.store
        await bot.wait_until_ready()
        if not store.cfg("auto_catch_up", True) or not bot.intents.message_content:
            return
        if self.running:
            return
        ch = bot.get_channel(self._live_channel())
        if ch is None:
            try:
                ch = await bot.fetch_channel(self._live_channel())
            except discord.HTTPException:
                return await bot.log("startup catch-up skipped - can't see the results channel")
        start = self._checkpoint() - timedelta(hours=6)
        if start.tzinfo is None:
            start = start.replace(tzinfo=now_utc().tzinfo)
        await bot.log(f"startup catch-up — reading #{ch.name} since {start:%a %b %d %H:%M} UTC")
        self.running = True
        began = now_utc()
        try:
            newest = await self._run(None, ch, start, apply=True, react=True)
            self._save_checkpoint(newest or began)
        except Exception as e:
            await bot.log(f"**startup catch-up failed** — {type(e).__name__}: {e}. "
                          "Run /backfill by hand.")
        finally:
            self.running = False
            await self._flush_deferred()

    async def _flush_deferred(self):
        held, self.deferred = self.deferred, []
        for m in held:
            try:
                await self.on_message(m)
            except Exception:
                pass

    @staticmethod
    async def _react(msg, kind):
        try:
            await msg.add_reaction(REACT[kind])
        except discord.HTTPException:
            pass

    def _report_lines(self, v) -> list:
        out = []
        if v.get("fx"):
            fx = v["fx"]
            out.append(f"wk{fx['week']} {fx['home']} {v.get('home', '?')}-{v.get('away', '?')} {fx['away']}")
        for o in v.get("options", []):
            out.append(f"could be wk{o['week']} {o['home']} vs {o['away']}")
        for key in ("note", "why"):
            if v.get(key):
                out.append(f"{key}: {v[key]}")
        out += [f"FLAG: {t}" for _, t in v.get("flags", [])]
        out += [ln.strip() for ln in self._players(v)]
        return out

    async def _parse(self, msg):
        if msg.id in self.cache:
            return self.cache[msg.id]
        blobs = []
        for a in msg.attachments:
            if a.filename.lower().endswith(".replay"):
                try:
                    blobs.append((a.filename, await a.read()))
                except discord.HTTPException:
                    pass
        out = games_from_files(blobs) if blobs else ([], [], ["couldn't download"])
        self.cache[msg.id] = out
        return out

    async def _parse_group(self, msg):
        """A post with a ballchasing group link instead of files."""
        m = GROUP_RE.search(msg.content or "")
        if not m:
            return None
        key = ("bc", m.group(1))
        if key not in self.cache:
            try:
                rows, games, err = await self.bot.stats.group_games(m.group(1))
            except Exception as e:
                rows, games, err = [], [], f"{type(e).__name__}: {e}"
            probs = [f"ballchasing group: {err}"] if err else []
            if not err and not games:
                probs = ["ballchasing group is empty"]
            self.cache[key] = (games or [], rows or [], probs,
                               f"https://ballchasing.com/group/{m.group(1)}")
        return self.cache[key]

    @app_commands.command(
        name="rerecord",
        description="Record a results post again from its replays - replaces that series' stats (admin).",
    )
    @app_commands.describe(
        link="The results post: right-click it > Copy Message Link",
        link2="A second post, if the series was split over two",
        link3="A third post",
        winner="Only if the bot can't place it on its own: the team that won",
        loser="...and the team that lost",
    )
    async def rerecord(self, interaction: discord.Interaction, link: str, link2: str = None,
                       link3: str = None, winner: str = None, loser: str = None):
        """
        For a series whose sheet writes failed, that was recorded from the
        wrong replays, or that the bot couldn't place: read the post(s) again,
        wipe whatever is saved for that series, and write it fresh - result,
        schedule grid, game scores, stats. Same as /clear + /report with the
        files, without downloading and re-attaching anything.
        """
        import re
        from ..replayfile import series_score
        from .reporting import teams_named
        store, bot = self.bot.store, self.bot
        if not is_admin(store, interaction.user):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=private())

        msgs = []
        for ln in (link, link2, link3):
            if not ln:
                continue
            m = re.search(r"channels/\d+/(\d+)/(\d+)", ln)
            if not m:
                return await interaction.followup.send(
                    f"`{ln[:80]}` isn't a message link. Right-click the post > **Copy Message Link**.",
                    ephemeral=private())
            try:
                ch = bot.get_channel(int(m.group(1))) or await bot.fetch_channel(int(m.group(1)))
                msgs.append(await ch.fetch_message(int(m.group(2))))
            except discord.HTTPException as e:
                return await interaction.followup.send(
                    f"Couldn't open that post ({type(e).__name__}) - is it in a channel the bot can see?",
                    ephemeral=private())

        parts, problems, group_link = [], [], ""
        for msg in msgs:
            if any(a.filename.lower().endswith(".replay") for a in msg.attachments):
                self.cache.pop(msg.id, None)            # read the files fresh
                g, r, p = await self._parse(msg)
            elif GROUP_RE.search(msg.content or ""):
                g, r, p, lk = await self._parse_group(msg)
                group_link = group_link or lk
            else:
                return await interaction.followup.send(
                    f"No replay files or ballchasing link in {msg.jump_url}.", ephemeral=private())
            parts.append((g, r))
            problems += list(p)
        games, rows = merge_parsed(parts)
        if not games:
            return await interaction.followup.send(
                "Couldn't read any replays from that post: " + ("; ".join(problems) or "nothing readable"),
                ephemeral=private())

        msg = msgs[0]
        member = msg.author if isinstance(msg.author, discord.Member) else msg.guild.get_member(msg.author.id)
        poster_teams = [t for t in (store.team_for_role(r.id) for r in getattr(member, "roles", [])) if t]
        text = "\n".join(m.content for m in msgs if m.content)
        if winner or loser:
            names = list(store.teams)
            w, l_ = teams_named(winner or "", names), teams_named(loser or "", names)
            if len(w) != 1 or len(l_) != 1:
                bad = winner if len(w) != 1 else loser
                return await interaction.followup.send(
                    f"**{bad}** isn't one team - use more of the name (give both winner and loser).",
                    ephemeral=private())
            b, o = series_score(games, rows)
            text = f"{w[0]} {max(b, o)}-{min(b, o)} {l_[0]}"
            poster_teams = []           # the admin's word replaces the poster's role
        post = {"week": store.week_for(msg.created_at), "poster_teams": poster_teams,
                "text": text, "games": games, "rows": rows, "link": group_link}
        roster = bot.roster if getattr(bot, "roster_loaded", False) else None
        v = place(store, post, roster)
        v["msg"], v["msgs"], v["post"], v["problems"] = msg, msgs, post, problems

        if v.get("status") not in ("matched", "already", "far"):
            detail = "\n".join(self._report_lines(v))
            hint = ("" if (winner and loser) else
                    "\nIf you know who played, run it again with `winner:` and `loser:`.")
            return await interaction.followup.send(
                f"Couldn't place it: {v.get('why') or v.get('status')}.{hint}\n```{detail[:1500]}```",
                ephemeral=private())

        fx = v["fx"]
        wiped = []
        if bot.sheets:
            for fn, label in ((bot.sheets.clear_game_stats, "stat lines"),
                              (bot.sheets.clear_game_scores, "game scores")):
                try:
                    ok, m_ = await fn(fx)
                    if ok and m_.startswith("removed"):
                        wiped.append(m_.replace("removed ", ""))
                except Exception:
                    pass
        async with self.live_lock:
            try:
                await self._record(v)
            except Exception as e:
                return await interaction.followup.send(
                    f"Write failed ({type(e).__name__}: {e}). Nothing else changed - try again in a minute.",
                    ephemeral=private())
        await self._react(msgs[-1], "check" if v.get("flags") else "ok")
        out = [f"**Re-recorded** wk{fx['week']} {fx['home']} {v['home']}-{v['away']} {fx['away']} "
               f"from {len(games)} replay(s)"
               + (f" · replaced {', '.join(wiped)}" if wiped else "")]
        out += [f"• {t}" for _, t in v.get("flags", [])[:8]]
        out.append("Check the log for the stats line; the website sheet updates in about a minute.")
        await bot.log(f"re-recorded by {interaction.user.display_name} — wk{fx['week']} "
                      f"{fx['home']} vs {fx['away']} · {msg.jump_url}")
        await interaction.followup.send("\n".join(out), ephemeral=private())

    @app_commands.command(
        name="backfill",
        description="Record results already posted in a channel (admin).",
    )
    @app_commands.describe(
        channel="The channel teams post results in",
        since=f"Start date, {EASTERN_HINT} (defaults to week 1)",
        apply="Write the results — otherwise it only reports what it found",
    )
    async def backfill(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        since: str = None,
        apply: bool = False,
    ):
        store = self.bot.store
        if not is_admin(store, interaction.user):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        if not self.bot.intents.message_content:
            return await interaction.response.send_message(
                "I can't read old messages yet. Two switches, both needed:\n"
                "1. Developer Portal → the bot → **Message Content Intent** on\n"
                '2. `"message_content_intent": true` in config.json\n'
                "Then restart me.",
                ephemeral=private(),
            )
        if self.running:
            return await interaction.response.send_message(
                "A backfill (or the startup catch-up) is already running.", ephemeral=private()
            )
        try:
            start = datetime.fromisoformat(since or store.cfg("week1_start", "2026-09-20"))
        except ValueError:
            return await interaction.response.send_message(
                f"I couldn't read **{since}** as a date — use {EASTERN_HINT}.", ephemeral=private()
            )
        start = datetime.combine(start.date(), dtime(0), tzinfo=now_utc().tzinfo)

        await interaction.response.defer(ephemeral=private())
        self.running = True
        try:
            await self._run(interaction, channel, start, apply)
        finally:
            self.running = False
            await self._flush_deferred()

    async def _run(self, interaction, channel, start, apply, react=False):
        """
        interaction is None when the bot runs this itself at startup - the
        report then goes to the log channel, and with react=True each post
        gets the same reaction the live feed would have given it.
        Returns the time of the newest message read.
        """
        store, bot = self.bot.store, self.bot
        roster = bot.roster if getattr(bot, "roster_loaded", False) else None
        guild = channel.guild

        raw, scanned, newest = [], 0, None
        async for msg in channel.history(after=start, oldest_first=True, limit=None):
            scanned += 1
            newest = msg.created_at
            link = ""
            if any(a.filename.lower().endswith(".replay") for a in msg.attachments):
                games, rows, problems = await self._parse(msg)
            else:
                got = await self._parse_group(msg)
                if got is None:
                    continue
                games, rows, problems, link = got
                await asyncio.sleep(1)      # ballchasing's rate limit
            raw.append({"msgs": [msg], "games": games, "rows": rows,
                        "problems": list(problems), "link": link})

        # a series split over several messages - one replay per post, or a
        # second post with the game that didn't fit - is joined back up: same
        # poster, within 30 minutes, and only while what's collected so far
        # isn't a finished series, so two separate results never merge
        need = int(store.cfg("wins_needed", 3))
        groups_raw = []
        for item in raw:
            last = groups_raw[-1] if groups_raw else None
            if (last is not None
                    and last["msgs"][-1].author.id == item["msgs"][0].author.id
                    and (item["msgs"][0].created_at - last["msgs"][-1].created_at).total_seconds() <= 1800
                    and not finished(last["games"], last["rows"], need)):
                last["games"], last["rows"] = merge_parsed(
                    [(last["games"], last["rows"]), (item["games"], item["rows"])])
                last["msgs"] += item["msgs"]
                last["problems"] += item["problems"]
                last["link"] = last.get("link") or item.get("link", "")
            else:
                groups_raw.append(item)

        posts = []
        for item in groups_raw:
            msg = item["msgs"][0]
            member = msg.author if isinstance(msg.author, discord.Member) else guild.get_member(msg.author.id)
            poster_teams = [store.team_for_role(r.id) for r in getattr(member, "roles", [])]
            text = "\n".join(m.content for m in item["msgs"] if m.content)
            post = {
                "week": store.week_for(msg.created_at),
                "poster_teams": [t for t in poster_teams if t],
                "text": text,
                "games": item["games"],
                "rows": item["rows"],
                "link": item.get("link", ""),
            }
            v = place(store, post, roster) if item["games"] else {
                "status": "unplaced", "why": "; ".join(item["problems"]) or "no readable replays"}
            v["msg"], v["msgs"], v["post"], v["problems"] = msg, item["msgs"], post, item["problems"]
            posts.append(v)

        # the same match posted twice: the later post stands
        latest = {}
        for v in posts:
            if v["status"] in ("matched", "already"):
                fid = v["fx"]["id"]
                if fid in latest:
                    latest[fid]["status"] = "superseded"
                latest[fid] = v

        groups = {}
        for v in posts:
            groups.setdefault(v["status"], []).append(v)
        report = self._report(channel, start, scanned, posts, groups)

        written = failed = 0
        if apply and groups.get("matched"):
            todo = groups["matched"]
            await bot.log(f"**backfill** — writing {len(todo)} results from #{channel.name}")
            pace = float(store.cfg("backfill_pace_seconds", 5))
            for n, v in enumerate(todo, 1):
                try:
                    await self._record(v)
                    written += 1
                    if react:
                        await self._react(v["msgs"][-1], "check" if v.get("flags") else "ok")
                except Exception as e:
                    failed += 1
                    await bot.log(f"backfill failed on {v['msg'].jump_url}: {type(e).__name__}: {e}")
                    if react:
                        await self._react(v["msgs"][-1], "manual")
                if n % 10 == 0:
                    await bot.log(f"backfill — {n}/{len(todo)}")
                await asyncio.sleep(pace)

        if apply and react:
            for key in ("far", "mismatch", "ambiguous", "unplaced"):
                for v in groups.get(key, []):
                    await self._react(v["msgs"][-1], "manual")

        counts = " · ".join(f"{k} {len(v)}" for k, v in sorted(groups.items()))
        flagged = sum(1 for v in groups.get("matched", []) if v.get("note"))
        if flagged:
            counts += f" ({flagged} matched far from their week — marked CHECK)"
        head = (
            f"**Backfill {'applied' if apply else 'dry run'}** — #{channel.name} since "
            f"{start.date()}\n{scanned} messages, {len(posts)} results found · {counts}"
        )
        if apply:
            head += f"\nWritten {written}" + (f", failed {failed}" if failed else "")
        else:
            head += "\nNothing written. Check the file, then run again with `apply:true`."
        if interaction is None:
            head = head.replace("**Backfill applied**", "**Startup catch-up**")
        file = discord.File(io.BytesIO(report.encode()), filename="backfill.txt")
        sent = False
        if interaction is not None:
            try:
                await interaction.followup.send(head, file=file, ephemeral=private())
                sent = True
            except discord.HTTPException:
                pass    # a long apply outlives the interaction
        if not sent and (interaction is not None or groups.get("matched") or
                         any(groups.get(k) for k in ("far", "mismatch", "ambiguous", "unplaced"))):
            ch = bot.get_channel(int(store.cfg("log_channel_id")))
            if ch:
                try:
                    await ch.send(head, file=discord.File(io.BytesIO(report.encode()),
                                                          filename="backfill.txt"))
                except discord.HTTPException:
                    pass
        await bot.log(head.replace("\n", " · "))
        return newest

    async def _record(self, v):
        store, fx, msg = self.bot.store, v["fx"], v["msg"]
        store.set(
            fx["id"],
            status="reported",
            result={
                "winner": v["winner"], "loser": v["loser"],
                "home": v["home"], "away": v["away"],
                # a ballchasing group if that's what they posted - casters can
                # rewatch it there - otherwise the Discord post with the files
                "link": v["post"].get("link") or msg.jump_url,
                "from_files": True,
                "by": msg.author.id,
                "by_name": msg.author.display_name,
                "by_team": next(iter(v["post"]["poster_teams"]), None),
                "backfilled": True,
                "win_side": v.get("win_side"),
                "flags": [t for _, t in v.get("flags", [])],
                # red: just a roster entry to add. yellow: needs looking into
                "flag_level": ("" if not v.get("flags")
                               else "red" if all(k == "unrostered" for k, _ in v["flags"])
                               else "yellow"),
            },
            reported_at=iso(msg.created_at),
        )
        games, rows = v["post"]["games"], v["post"]["rows"]
        await self.bot.on_result(fx, None, parsed=(games, rows), wait_stats=True)

    @staticmethod
    def _report(channel, start, scanned, posts, groups) -> str:
        order = [
            ("matched", "WILL BE WRITTEN"),
            ("far", "PLACED FAR FROM ITS WEEK — not written; check, then enter by hand"),
            ("mismatch", "SCORE OR WINNER DISAGREES — check by hand"),
            ("ambiguous", "COULD BE MORE THAN ONE MATCH — enter by hand"),
            ("unplaced", "COULDN'T PLACE — enter by hand"),
            ("superseded", "POSTED AGAIN LATER — the later post is used"),
            ("already", "ALREADY RECORDED — skipped"),
        ]
        out = [f"Backfill of #{channel.name} since {start.date()}",
               f"{scanned} messages scanned, {len(posts)} results found "
               "(replay files or a ballchasing group)", ""]
        for key, title in order:
            items = groups.get(key, [])
            if not items:
                continue
            out += [f"== {title} ({len(items)}) ==", ""]
            for v in items:
                m = v["msg"]
                first = (m.content or "").splitlines()[0][:80] if m.content else "(no text)"
                out.append(f"[{m.created_at:%a %b %d %H:%M}] {m.author.display_name}: {first}"
                           + (f"  (+{len(v['msgs']) - 1} more message(s) joined)"
                              if len(v.get("msgs", [])) > 1 else ""))
                if v.get("fx"):
                    fx = v["fx"]
                    line = f"   wk{fx['week']} {fx['home']} {v.get('home', '?')}-{v.get('away', '?')} {fx['away']}"
                    out.append(line + f"   ({' + '.join(v.get('clues', []))})")
                for o in v.get("options", []):
                    out.append(f"   could be wk{o['week']} {o['home']} vs {o['away']}")
                if v.get("note"):
                    out.append(f"   CHECK: {v['note']}")
                for kind, text in v.get("flags", []):
                    out.append(f"   FLAG ({kind}): {text}")
                if v.get("why"):
                    out.append(f"   why: {v['why']}")
                if v.get("problems"):
                    out.append("   skipped files: " + "; ".join(v["problems"]))
                if v["post"].get("link"):
                    out.append(f"   ballchasing: {v['post']['link']}")
                if key != "matched":
                    out += Backfill._players(v)
                    out.append(f"   {m.jump_url}")
                out.append("")
        return "\n".join(out)


    @staticmethod
    def _players(v) -> list:
        """Who played on each side and what the Roster Sheet says about them."""
        from ..replayfile import series_score

        post = v.get("post") or {}
        games, rows = post.get("games") or [], post.get("rows") or []
        info = v.get("sides")
        if not rows or not info:
            return []
        a, b = series_score(games, rows)
        undecided = sum(1 for g in games if g["blue_goals"] == g["orange_goals"])
        head = f"   replays: {len(games)} game(s), side A won {a}, side B won {b}"
        if undecided:
            head += f", {undecided} with no winner in the file"
        out = [head]
        for side in ("A", "B"):
            si = info.get(side) or {}
            names = ", ".join(f"{p[0]} [{p[1] or '—'}]" for p in si.get("players", []))
            verdict = si.get("team") or "no team"
            out.append(f"     {side}: {verdict} ({si.get('votes', 0)} rostered) — {names}")

        # game by game, with team names where the bot knows which side is who
        label = {"A": "side A", "B": "side B"}
        fx = v.get("fx")
        if fx:
            sides = {fx["home"], fx["away"]}
            for sd in ("A", "B"):
                t = (info.get(sd) or {}).get("team")
                if t in sides:
                    label[sd] = t
            if v.get("winner") and v.get("win_side"):
                ws = v["win_side"]
                ls = "B" if ws == "A" else "A"
                label.setdefault(ws, v["winner"])
                if label[ws] == f"side {ws}":
                    label[ws] = v["winner"]
                if label[ls] == f"side {ls}":
                    label[ls] = v["loser"] if v.get("loser") else label[ls]
        from ..replayfile import assign_sides
        blue_side = assign_sides(rows)
        for g in sorted(games, key=lambda g: g.get("game_no", 0)):
            bs = blue_side.get(g.get("replay_id"), "A")
            os_ = "B" if bs == "A" else "A"
            goals = {bs: g["blue_goals"], os_: g["orange_goals"]}
            ot = " OT" if g.get("overtime") else ""
            if goals["A"] == goals["B"]:
                res = "no winner in the file"
            else:
                w = "A" if goals["A"] > goals["B"] else "B"
                res = f"{label[w]} won"
            out.append(f"     G{g.get('game_no', '?')}: {label['A']} {goals['A']}-{goals['B']} "
                       f"{label['B']}{ot} ({res})")
        return out


async def setup(bot):
    await bot.add_cog(Backfill(bot))
