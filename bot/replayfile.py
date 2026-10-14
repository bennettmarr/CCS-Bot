"""
Reading a Rocket League .replay file's header.

A replay is a header followed by a compressed network stream. The stream is
the hard part - it's what you'd need for boost paths and positioning, it
changes shape most patches, and parsing it is why libraries like carball are
heavy and break often.

The header is none of that. It's a flat list of properties, and it already
holds everything the league needs:

    Team0Score, Team1Score        the scoreline
    PlayerStats[]                 per player: name, platform, online id,
                                  team, score, goals, assists, saves, shots
    Date, MatchType, ...          metadata

That format has been stable for years, because it's just metadata the game
writes for its own replay browser. So this reads the header and stops - no
Rust bindings, no ballchasing account, no upload quota, and nothing to
re-fix every patch.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional


class ReplayError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.i)[0]
        self.i += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.i)[0]
        self.i += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.d, self.i)[0]
        self.i += 8
        return v

    def f32(self) -> float:
        v = struct.unpack_from("<f", self.d, self.i)[0]
        self.i += 4
        return v

    def byte(self) -> int:
        v = self.d[self.i]
        self.i += 1
        return v

    def text(self) -> str:
        """
        Length-prefixed string. A negative length means UTF-16, which shows up
        for names with non-Latin characters - and those are common enough in
        Rocket League that ignoring the case would break real replays.
        """
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            raw = self.d[self.i : self.i + (-n * 2)]
            self.i += -n * 2
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        raw = self.d[self.i : self.i + n]
        self.i += n
        return raw.decode("latin-1", errors="replace").rstrip("\x00")


def _property(r: _Reader) -> tuple[Optional[str], Any]:
    name = r.text()
    if name == "None" or name == "":
        return None, None
    kind = r.text()
    size = r.u64()

    if kind == "IntProperty":
        return name, r.i32()
    if kind == "BoolProperty":
        return name, bool(r.byte())
    if kind == "FloatProperty":
        return name, r.f32()
    if kind == "StrProperty" or kind == "NameProperty":
        return name, r.text()
    if kind == "QWordProperty":
        return name, r.u64()
    if kind == "ByteProperty":
        # an enum written as two strings, except OnlinePlatform which is one
        k = r.text()
        if k == "OnlinePlatform":
            return name, r.text()
        v = r.text()
        return name, v
    if kind == "ArrayProperty":
        count = r.u32()
        out = []
        for _ in range(count):
            entry: Dict[str, Any] = {}
            while True:
                n2, v2 = _property(r)
                if n2 is None:
                    break
                entry[n2] = v2
            out.append(entry)
        return name, out
    if kind == "StructProperty":
        # the declared size covers the payload only, not the struct's own name
        sname = r.text()
        blob = r.d[r.i : r.i + size]
        r.i += size
        if sname == "UniqueNetId":
            return name, _identity(blob)
        return name, None
    # unknown type: skip its payload rather than derailing the whole file
    r.i += size
    return name, None


def _identity(blob: bytes) -> Dict[str, str]:
    """
    Pull a player's real account id out of the UniqueNetId struct.

    Steam players carry it as Uid, a plain number. Epic players have Uid 0
    and their id sits in a nested EpicAccountId string - the same 32-hex
    value ballchasing reports for them, which is what makes roster matching
    work for Epic at all.

    This scans for the two fields by name rather than walking the struct,
    because the struct nests differently per platform (SceNpId for
    PlayStation, and so on) and a full walk would break on the next console
    Psyonix adds.
    """
    out: Dict[str, str] = {}
    key = b"EpicAccountId\x00"
    k = blob.find(key)
    if k >= 0:
        r = _Reader(blob)
        r.i = k + len(key)
        r.text()            # "StrProperty"
        r.u64()             # size
        val = r.text()
        if val:
            out["epic"] = val
    key = b"Uid\x00"
    k = blob.find(key)
    if k >= 0:
        r = _Reader(blob)
        r.i = k + len(key)
        kind = r.text()
        if kind == "QWordProperty":
            r.u64()
            v = r.u64()
            if v:
                out["uid"] = str(v)
    return out


def read_header(data: bytes) -> Dict[str, Any]:
    r = _Reader(data)
    r.u32()          # header size
    r.u32()          # crc
    r.u32()          # engine version
    r.u32()          # licensee version
    net = r.u32()    # net version
    if net >= 6:
        pass
    r.text()         # class name, e.g. TAGame.Replay_Soccar_TA

    props: Dict[str, Any] = {}
    while True:
        name, val = _property(r)
        if name is None:
            break
        props[name] = val
    return props


def played(s: dict) -> bool:
    """
    Did this person actually play? Coaches, managers and streamers can sit
    in a match lobby and end up in the replay with nothing recorded; anyone
    who played a real game has some score. Works on replay-header stats
    (Score, Goals...) and on the bot's own rows (score, goals...).
    """
    keys = ("Score", "Goals", "Assists", "Saves", "Shots",
            "score", "goals", "assists", "saves", "shots")
    for k in keys:
        try:
            if float(s.get(k) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def game_from_header(data: bytes) -> Dict[str, Any]:
    """
    The bits the league cares about, in the same shape the bot already uses
    for ballchasing rows.
    """
    p = read_header(data)
    blue = int(p.get("Team0Score", 0) or 0)
    orange = int(p.get("Team1Score", 0) or 0)

    players = []
    for s in p.get("PlayerStats") or []:
        team = s.get("Team", 0)
        team = int(team) if team is not None else -1
        if team not in (0, 1):
            continue            # a spectator - coach, manager, streamer
        if not played(s):
            continue            # in the lobby but never touched the game
        ident = s.get("PlayerID") or {}
        platform = _platform(s.get("Platform", ""))
        # OnlineID is 0 for Epic players; their id lives in the struct
        pid = str(s.get("OnlineID", "") or "")
        if pid in ("", "0"):
            pid = ident.get("epic") or ident.get("uid") or ""
        players.append(
            {
                "name": s.get("Name", ""),
                "platform": platform,
                "platform_id": pid,
                "colour": "blue" if team == 0 else "orange",
                "score": int(s.get("Score", 0) or 0),
                "goals": int(s.get("Goals", 0) or 0),
                "assists": int(s.get("Assists", 0) or 0),
                "saves": int(s.get("Saves", 0) or 0),
                "shots": int(s.get("Shots", 0) or 0),
                "mvp": 1 if s.get("bMVP") else 0,
            }
        )
    if blue == orange and players:
        # the header's team scores are level - a forfeit, a remake, or a
        # replay saved oddly. The players' own goal counts sometimes still
        # say who won; if they're level too, the game stays undecided and
        # the caller decides what to do with it
        pb = sum(x["goals"] for x in players if x["colour"] == "blue")
        po = sum(x["goals"] for x in players if x["colour"] == "orange")
        if pb != po:
            blue, orange = pb, po
    return {
        "blue_goals": blue,
        "orange_goals": orange,
        "date": p.get("Date", ""),
        "id": p.get("Id", "") or p.get("ReplayName", ""),
        "overtime": bool(p.get("bOverTime", False)),
        "players": players,
    }


def games_from_files(files: list) -> tuple[list, list, list]:
    """
    Turn a set of (filename, bytes) into the same shapes the ballchasing path
    produces, so everything downstream is identical either way.

    Returns (game_rows, player_rows, problems). Games are ordered by the date
    in the replay, which is how you get game 1 to 5 in the order they were
    played rather than the order they were attached.
    """
    parsed = []
    problems = []
    seen = set()
    for fname, blob in files:
        try:
            g = game_from_header(blob)
        except Exception as e:
            problems.append(f"{fname}: couldn't read it ({type(e).__name__})")
            continue
        if not g["players"]:
            problems.append(f"{fname}: no players in it")
            continue
        rid = g.get("id") or fname
        if rid in seen:
            problems.append(f"{fname}: same replay attached twice")
            continue
        seen.add(rid)
        parsed.append((g, rid))

    parsed.sort(key=lambda x: x[0].get("date") or "")
    games, rows = [], []
    for n, (g, rid) in enumerate(parsed, 1):
        from .stats import Stats

        cb, co = Stats.cap(g["blue_goals"], g["orange_goals"])
        games.append({
            "replay_id": rid, "game_no": n, "date": g.get("date", ""),
            "blue_goals": g["blue_goals"], "orange_goals": g["orange_goals"],
            "capped_blue": cb, "capped_orange": co,
            "overtime": g.get("overtime", False),
        })
        won_blue = g["blue_goals"] > g["orange_goals"]
        for p in g["players"]:
            rows.append({
                "replay_id": rid, "game_no": n, "date": g.get("date", ""),
                "colour": p["colour"],
                "won": 1 if (p["colour"] == "blue") == won_blue else 0,
                "name": p["name"], "platform": p["platform"],
                "platform_id": p["platform_id"],
                "score": p["score"], "goals": p["goals"],
                "assists": p["assists"], "saves": p["saves"],
                "shots": p["shots"], "mvp": p["mvp"],
            })
    return games, rows, problems


def _who(r) -> tuple:
    pid = str(r.get("platform_id") or "").strip().lower()
    if pid:
        return (str(r.get("platform") or "").lower(), pid)
    return ("name", str(r.get("name") or "").strip().lower())


def assign_sides(rows: list) -> dict:
    """
    Split a series' players into its two teams, A and B, by who they are -
    not by colour, because teams swap colours between games all the time.

    Side A is whoever was blue in the first game. Each later game, the
    colour whose players overlap more with side A is side A. New faces (a
    sub) join the side they played on. Tags every row with r["side"] and
    returns {replay_id: side_of_blue}.
    """
    by_game = {}
    for r in rows:
        by_game.setdefault(r["replay_id"], {"n": r.get("game_no", 0), "blue": set(), "orange": set()})
        by_game[r["replay_id"]][r["colour"]].add(_who(r))
    order = sorted(by_game, key=lambda k: (by_game[k]["n"], str(k)))
    a, b, blue_side = set(), set(), {}
    for rid in order:
        g = by_game[rid]
        if not a and not b:
            side = "A"
        else:
            score = (len(g["blue"] & a) - len(g["blue"] & b)
                     - len(g["orange"] & a) + len(g["orange"] & b))
            side = "A" if score >= 0 else "B"
        blue_side[rid] = side
        (a if side == "A" else b).update(g["blue"])
        (b if side == "A" else a).update(g["orange"])
    for r in rows:
        bs = blue_side.get(r["replay_id"], "A")
        r["side"] = bs if r["colour"] == "blue" else ("B" if bs == "A" else "A")
    return blue_side


def series_score(games: list, rows: list = None) -> tuple:
    """
    Games won by each side. With the player rows, sides are the two teams
    (see assign_sides), so a series where the teams swapped colours still
    counts right. Without them - only the scorelines - it falls back to
    counting by colour, which is only right if nobody swapped.
    """
    if rows:
        blue_side = assign_sides(rows)
        a = bb = 0
        for g in games:
            bs = blue_side.get(g.get("replay_id"))
            if bs is None or g["blue_goals"] == g["orange_goals"]:
                continue
            won = bs if g["blue_goals"] > g["orange_goals"] else ("B" if bs == "A" else "A")
            if won == "A":
                a += 1
            else:
                bb += 1
        return a, bb
    b = sum(1 for g in games if g["blue_goals"] > g["orange_goals"])
    o = sum(1 for g in games if g["orange_goals"] > g["blue_goals"])
    return b, o


def _platform(raw: str) -> str:
    raw = (raw or "").replace("OnlinePlatform_", "").lower()
    return {"steam": "steam", "ps4": "ps4", "psynet": "psn",
            "dingo": "xbox", "epic": "epic", "switch": "switch"}.get(raw, raw)
