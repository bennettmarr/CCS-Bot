"""
Pulling per-player, per-game stats out of a ballchasing group.

Raw per-game rows are what get stored - one row per player per game. Totals
and per-game rates are then worked out by the sheet, not here, so they
recalculate the moment anything changes and nobody has to trust the bot to
have added up correctly.

Team attribution comes free: the bot already knows which CCS team won the
series, and ballchasing says which colour won each game, so the winning
colour's players are the winning team's players. No mapping of Rocket League
display names to rosters is needed, which was the thing that made this look
hard.

Identity is the platform id, not the display name. People rename themselves
mid-season; the id doesn't move, and it's also what builds a tracker link.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger("ccs.stats")

API = "https://ballchasing.com/api"

# ballchasing asks for roughly two calls a second on a free key
THROTTLE = 0.55

TRACKER = "https://rocketleague.tracker.network/rocket-league/profile/{plat}/{pid}/overview"

PLATFORM_FIX = {"ps4": "psn", "xbox": "xbl", "epic": "epic", "steam": "steam"}


def tracker_url(platform: str, pid: str) -> str:
    plat = PLATFORM_FIX.get((platform or "").lower(), (platform or "").lower())
    return TRACKER.format(plat=plat, pid=pid)


class Stats:
    def __init__(self, key: str):
        self.key = key

    async def _get(self, session, url) -> Tuple[Optional[dict], str]:
        try:
            async with session.get(
                url, headers={"Authorization": self.key}, timeout=25
            ) as r:
                if r.status == 429:
                    await asyncio.sleep(2)
                    return None, "rate limited"
                if r.status != 200:
                    return None, f"http {r.status}"
                return await r.json(), ""
        except Exception as e:
            return None, f"{type(e).__name__}"

    async def group_games(self, group_id: str):
        """
        Every player's line for every game, and the game scorelines themselves.

        Returns (player_rows, game_rows, error). The game scores are taken
        from ballchasing's own team totals, never summed from the player
        rows - if one player's line is missing or a replay is odd, a summed
        score would be quietly wrong, and a wrong scoreline feeds straight
        into the ratings.

        A partial result is still returned: three games of a five-game
        series beats nothing.
        """
        rows: List[dict] = []
        games: List[dict] = []
        async with aiohttp.ClientSession() as session:
            listing, err = await self._get(
                session, f"{API}/replays?group={group_id}&count=20"
            )
            if listing is None:
                return [], [], f"couldn't list the group ({err})"
            replays = listing.get("list", [])
            if not replays:
                return [], [], "that group has no replays"

            for n, rep in enumerate(sorted(replays, key=lambda r: r.get("date") or "")):
                rid = rep.get("id")
                if not rid:
                    continue
                detail, err = await self._get(session, f"{API}/replays/{rid}")
                await asyncio.sleep(THROTTLE)
                if detail is None:
                    log.warning("replay %s: %s", rid, err)
                    continue
                rows.extend(self._rows_for(detail, rid, n + 1))
                games.append(self._game_for(detail, rid, n + 1))
        return rows, games, ""

    @staticmethod
    def cap(a: int, b: int, cap_margin: int = 5, loser_max: int = 1):
        """
        Trim a scoreline that stopped being a contest.

        Past about five goals with the loser on none or one, the margin is
        measuring when the losing team gave up rather than how much better
        the winner was - a 9-0 and a 5-0 are the same result. So the margin
        is capped, but only when the loser scored at most one: a 9-3 means
        both sides were still playing and stands as it is.
        """
        hi, lo = (a, b) if a >= b else (b, a)
        if lo <= loser_max and hi - lo > cap_margin:
            hi = lo + cap_margin
        return (hi, lo) if a >= b else (lo, hi)

    @staticmethod
    def _game_for(detail: dict, replay_id: str, game_no: int) -> dict:
        """One game's scoreline, straight from ballchasing's team totals."""
        blue = detail.get("blue") or {}
        orange = detail.get("orange") or {}
        bg = (blue.get("stats") or {}).get("core", {}).get("goals", 0) or 0
        og = (orange.get("stats") or {}).get("core", {}).get("goals", 0) or 0
        cb, co = Stats.cap(bg, og)
        return {
            "replay_id": replay_id,
            "game_no": game_no,
            "date": detail.get("date", ""),
            "blue_goals": bg,
            "orange_goals": og,
            "capped_blue": cb,
            "capped_orange": co,
            "overtime": bool(detail.get("overtime")),
            "duration": detail.get("duration", ""),
        }

    @staticmethod
    def _rows_for(detail: dict, replay_id: str, game_no: int) -> List[dict]:
        out = []
        blue = detail.get("blue") or {}
        orange = detail.get("orange") or {}
        bg = (blue.get("stats") or {}).get("core", {}).get("goals", 0)
        og = (orange.get("stats") or {}).get("core", {}).get("goals", 0)

        for colour, side in (("blue", blue), ("orange", orange)):
            won = (bg > og) if colour == "blue" else (og > bg)
            for p in side.get("players", []) or []:
                core = (p.get("stats") or {}).get("core", {}) or {}
                if not any((core.get(k) or 0) > 0 for k in
                           ("score", "goals", "assists", "saves", "shots")):
                    continue    # spectator / never played - no stats, no flag
                ident = p.get("id") or {}
                out.append(
                    {
                        "replay_id": replay_id,
                        "game_no": game_no,
                        "date": detail.get("date", ""),
                        "colour": colour,
                        "won": 1 if won else 0,
                        "name": p.get("name", ""),
                        "platform": ident.get("platform", ""),
                        "platform_id": str(ident.get("id", "")),
                        "score": core.get("score", 0) or 0,
                        "goals": core.get("goals", 0) or 0,
                        "assists": core.get("assists", 0) or 0,
                        "saves": core.get("saves", 0) or 0,
                        "shots": core.get("shots", 0) or 0,
                        "mvp": 1 if core.get("mvp") else 0,
                    }
                )
        return out

    @staticmethod
    def attribute_game_teams(games: List[dict], player_rows: List[dict],
                             winner: str, loser: str) -> None:
        """
        Put team names on each game scoreline.

        Which colour a team wore changes between games, so this reads it off
        the player rows for that same replay rather than assuming.
        """
        from collections import Counter
        counts = {}
        for r in player_rows:
            counts.setdefault((r["replay_id"], r["colour"]), Counter())[r.get("team")] += 1
        # a colour's team is what most of its players are labelled - one
        # player rostered elsewhere doesn't rename the whole side
        colour_of = {k: c.most_common(1)[0][0] for k, c in counts.items() if c}
        for g in games:
            b = colour_of.get((g["replay_id"], "blue"))
            o = colour_of.get((g["replay_id"], "orange"))
            g["blue_team"], g["orange_team"] = b or "", o or ""

    @staticmethod
    def apply_roster(rows: List[dict], winner: str, loser: str, team_of) -> None:
        """
        The Roster Sheet decides who plays for whom.

        1. Each side is named by its players' roster teams - the side whose
           rostered players belong to the winner is the winner - instead of
           by who won more games. Only when the roster can't tell (nobody on
           it, or a tie) does the game-count guess from attribute_teams stand.
        2. Any player the roster puts on one of these two teams gets that
           team, whichever side the grouping put them on.
        3. Subs and unrostered players keep their side's team.
        team_of(platform, platform_id, name) -> roster team or None.
        """
        from .replayfile import _who
        from .roster import norm
        teams = {winner, loser}
        votes = {"A": {}, "B": {}}
        seen = set()
        roster = getattr(team_of, "__self__", None)

        def weak(r):
            # a Steam player matched only by NAME: Steam names aren't unique,
            # so that match may not move them across to the other side
            if roster is None or (r.get("platform") or "").lower() != "steam":
                return False
            key = (r.get("platform", "").lower(), norm(r.get("platform_id", "")))
            if key in roster.by_identity:
                return False
            return f"{key[0]}:{key[1]}" not in getattr(roster, "manual", set())

        for r in rows:
            try:
                t = team_of(r.get("platform", ""), r.get("platform_id", ""), r.get("name", ""),
                            teams=teams)
            except TypeError:
                t = team_of(r.get("platform", ""), r.get("platform_id", ""), r.get("name", ""))
            r["roster_team"] = t
            r["weak_match"] = bool(t) and weak(r)
            key = (r.get("side"), _who(r))
            if t in teams and not r["weak_match"] and key not in seen and r.get("side") in votes:
                seen.add(key)
                votes[r["side"]][t] = votes[r["side"]].get(t, 0) + 1
        a_is_winner = (votes["A"].get(winner, 0) - votes["A"].get(loser, 0)
                       + votes["B"].get(loser, 0) - votes["B"].get(winner, 0))
        side_team = {}
        if a_is_winner > 0:
            side_team = {"A": winner, "B": loser}
        elif a_is_winner < 0:
            side_team = {"A": loser, "B": winner}
        for r in rows:
            base = side_team.get(r.get("side")) or r.get("team")
            if r["roster_team"] in teams and not (r["weak_match"] and base in teams):
                r["team"] = r["roster_team"]
            else:
                r["team"] = base

    @staticmethod
    def attribute_teams(rows: List[dict], winner: str, loser: str,
                        win_side: str = None) -> None:
        """
        Put the CCS team name on every player line.

        The two teams are worked out from the players themselves (see
        replayfile.assign_sides) - the same accounts game after game - not
        from colour, which swaps, and not from who won each game, which
        would hand the series loser's players to the winner in every game
        the loser took. Whichever side won more games is the series winner.
        """
        from .replayfile import assign_sides

        assign_sides(rows)
        wins = {"A": set(), "B": set()}
        for r in rows:
            if r.get("won"):
                wins[r["side"]].add(r["replay_id"])
        if win_side not in ("A", "B"):
            # a full series: whoever won more games won it. With replays
            # missing that can be wrong, so the backfill passes the side in
            win_side = "A" if len(wins["A"]) >= len(wins["B"]) else "B"
        for r in rows:
            r["team"] = winner if r["side"] == win_side else loser
