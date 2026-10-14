"""
Ballchasing group verification.

A team reports "we won 3-1" and pastes a group link. We fetch the group, count
the games and see who actually won each one, then compare. A mismatch is
refused with the numbers, so a wrong score never reaches the sheet.

Deliberately forgiving about *which* side is which: ballchasing team names are
whatever the players had set, so we match on the reported game count and the
winner's share rather than trying to map "blue" to a school.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

import aiohttp

API = "https://ballchasing.com/api"
GROUP_RE = re.compile(r"ballchasing\.com/group/([A-Za-z0-9\-_]+)")


class Ballchasing:
    def __init__(self, key: str):
        self.key = key

    def group_id(self, link: str) -> Optional[str]:
        if not link:
            return None
        m = GROUP_RE.search(link.strip())
        if m:
            return m.group(1)
        # someone pasted the bare id
        if re.fullmatch(r"[A-Za-z0-9\-_]{4,}", link.strip()):
            return link.strip()
        return None

    async def game_wins(self, link: str) -> Tuple[Optional[Tuple[int, int]], str]:
        """
        Returns ((wins_for_side_a, wins_for_side_b), "") on success,
        or (None, reason) if it can't be checked.
        Side A is whichever ballchasing team won more; the caller only needs
        the shape of the result, e.g. 3-1.
        """
        gid = self.group_id(link)
        if not gid:
            return None, "that isn't a ballchasing group link"
        if not self.key:
            return None, "no ballchasing key configured"

        headers = {"Authorization": self.key}
        url = f"{API}/replays?group={gid}&count=20"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers, timeout=20) as r:
                    if r.status == 401:
                        return None, "ballchasing rejected the API key"
                    if r.status == 404:
                        return None, "that group doesn't exist or isn't shared"
                    if r.status != 200:
                        return None, f"ballchasing returned {r.status}"
                    data = await r.json()
        except Exception as e:  # network down, timeout, ballchasing offline
            return None, f"couldn't reach ballchasing ({type(e).__name__})"

        replays = data.get("list", [])
        if not replays:
            return None, "that group has no replays in it"

        # Who won each game is worked out from the players - the same
        # accounts game after game - because ballchasing's team names are
        # just "blue"/"orange" unless someone set them, and colours swap.
        from .replayfile import assign_sides, series_score

        rows, games = [], []
        for n, rep in enumerate(sorted(replays, key=lambda r: r.get("date") or ""), 1):
            rid = rep.get("id") or str(n)
            bg = (rep.get("blue") or {}).get("goals", 0) or 0
            og = (rep.get("orange") or {}).get("goals", 0) or 0
            games.append({"replay_id": rid, "blue_goals": bg, "orange_goals": og})
            for colour in ("blue", "orange"):
                for p in (rep.get(colour) or {}).get("players", []) or []:
                    pid = p.get("id") or {}
                    rows.append({"replay_id": rid, "game_no": n, "colour": colour,
                                 "platform": pid.get("platform", ""),
                                 "platform_id": pid.get("id", ""),
                                 "name": p.get("name", "")})
        if rows:
            a, b = sorted(series_score(games, rows), reverse=True)
            if a == 0:
                return None, "couldn't work out a winner from those replays"
            return (a, b), ""

        # no player lists - fall back to team names
        tally = {}
        for rep in replays:
            blue = (rep.get("blue") or {}).get("goals", 0) or 0
            orange = (rep.get("orange") or {}).get("goals", 0) or 0
            if blue == orange:
                continue
            bn = ((rep.get("blue") or {}).get("name") or "blue").lower()
            on = ((rep.get("orange") or {}).get("name") or "orange").lower()
            winner = bn if blue > orange else on
            tally[winner] = tally.get(winner, 0) + 1

        if not tally:
            return None, "couldn't work out a winner from those replays"
        counts = sorted(tally.values(), reverse=True)
        a = counts[0]
        b = counts[1] if len(counts) > 1 else 0
        return (a, b), ""
