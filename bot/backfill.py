"""
Placing an old results post on its fixture.

A post looks like "UC 3-0 Xavier" with the replay files attached. Three
independent clues say which match it is:

  poster   the team role of whoever posted it
  text     the two names either side of the score
  replays  the players in the files, looked up on the Roster Sheet

The whole season is searched - the schedule is loose, and a week 1 series
can be played weeks late. The poster's role is what keeps that workable: it
narrows things to that team's handful of fixtures, which is what lets "UC"
work - it doesn't have to mean Cincinnati among 202 teams, only among
Cincinnati's own opponents.

A post is placed when both teams of exactly one fixture are named by some
clue and at least two clues agree (or the text alone names both teams and
the winner unambiguously). A team the replays identify rules out every
fixture it isn't in. Anything less is listed for a person.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional

from .cogs.reporting import initials
from .replayfile import series_score

LINE_RE = re.compile(r"^\s*(.+?)\s+(\d)\s*[-–—:]\s*(\d)\s+(.+?)\s*$")
# "Creighton vs UNT 3-2" - names first, score after, in the same order
VS_RE = re.compile(r"^\s*(.+?)\s+(?:vs\.?|v\.?|versus)\s+(.+?)\s+(\d)\s*[-–—:]\s*(\d)\s*$", re.I)
FILLER = {"vs", "v", "versus", "ff", "gg", "ggs", "def", "beat", "over", "final", "score",
          "university", "univeristy", "universtiy", "univ", "college", "of", "the"}


def _strength(t: str, side: str) -> int:
    """
    How well a typed name fits a team: 2 when it spells the name or its
    start or a whole word of it ("VCU" -> VCU Rams, "Xavier"), 1 when it only
    fits as initials or a longer substring ("VCU" -> Vermont Catamounts),
    0 otherwise. Stricter than /report's matcher, which only ever picks
    between two teams: here "UC" would be found inside "Kentucky", so
    substrings only count from four letters up.
    """
    s = side.lower()
    if not t:
        return 0
    if t == s or s.startswith(t) or t in s.split():
        return 2
    if t in initials(side) or (len(t) >= 4 and t in s):
        return 1
    return 0


def _hits(t: str, side: str) -> bool:
    return _strength(t, side) > 0


# on their own these say almost nothing about which team is meant
GENERIC = {"state", "st", "north", "south", "east", "west", "central", "northern",
           "southern", "eastern", "western", "northeast", "northwest", "southeast",
           "southwest", "new", "san", "saint", "st.", "u", "a&m", "golden"}


def _clean(text: str) -> list:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text or "")
    text = re.sub(r"[^A-Za-z0-9&' ]", " ", text)
    return [w for w in text.lower().split() if w not in FILLER]


def _name_strength(text: str, side: str, from_end: bool) -> float:
    """
    How much of a team's name the text actually spells out: the number of
    words in the longest run that fits, plus one for the full name. Initials
    or a bare substring count half. So "CSU Northridge Matadors" scores 4 for
    CSU Northridge but 1 for CSU Fullerton, and "VCU" beats Vermont
    Catamounts for VCU Rams.

    People add things around the name - "UC 3-0 Xavier (reupload)", "gg UC
    3-0 ...". The name nearest the score is the one that counts, so the runs
    tried are those touching the score: for the name after it its opening
    words, for the name before it its closing words.
    """
    words = _clean(text)
    school = " ".join(side.lower().split()[:-1])
    for n in range(len(words), 0, -1):
        chunk = " ".join(words[-n:] if from_end else words[:n])
        g = _strength(chunk, side)
        if g == 2:
            if n == 1 and chunk in GENERIC:
                return 0.25
            # "Colorado" is Colorado Buffaloes, not Colorado State: a name
            # that is the whole school part of a team's name counts extra
            bonus = 1 if chunk == side.lower() or (school and chunk == school) else 0
            return n + bonus
        if g == 1:
            # initials ("BGSU", "UNCW") are how people write; a name only
            # found inside another ("south" in "Georgia Southern") isn't
            return 0.5 if chunk in initials(side) else 0.25
    return 0


def merge_parsed(parts: list) -> tuple:
    """
    Join the replays from several messages into one series: drop repeats,
    order by the date inside each replay, renumber games 1..n.
    """
    games, rows, seen = [], [], set()
    for g_list, r_list in parts:
        for g in g_list:
            if g["replay_id"] in seen:
                continue
            seen.add(g["replay_id"])
            games.append(dict(g))
            rows.extend(dict(r) for r in r_list if r["replay_id"] == g["replay_id"])
    games.sort(key=lambda g: g.get("date") or "")
    num = {g["replay_id"]: n for n, g in enumerate(games, 1)}
    for g in games:
        g["game_no"] = num[g["replay_id"]]
    for r in rows:
        r["game_no"] = num[r["replay_id"]]
    return games, rows


def finished(games: list, rows: list, need: int = 3) -> bool:
    if not games:
        return False
    return max(series_score(games, rows)) >= need


def parse_line(text: str):
    """First line that looks like 'A 3-0 B' -> (A, a, b, B), else None."""
    for line in (text or "").splitlines():
        line = re.sub(r"[*_`|]", "", line).strip()
        m = VS_RE.match(line)
        if m:
            return m.group(1), int(m.group(3)), int(m.group(4)), m.group(2)
        m = LINE_RE.match(line)
        if m:
            return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    return None


def side_teams(rows: List[dict], roster) -> Dict[str, dict]:
    """
    What the roster says about each side (A/B, from assign_sides):
      {"A": {"team": name_or_None, "votes": n, "players": [(name, team_or_None), ...]}, ...}
    A side's team is the one most of its known players are rostered to, if
    that's a clear lead. One known player is enough to name a team - the
    Roster Sheet's tracker links are sparse - but only two or more can veto
    a fixture (see place), so one sub rostered elsewhere can't.
    """
    from .replayfile import _who

    out = {}
    for side in ("A", "B"):
        seen = {}
        for r in rows:
            if r.get("side") != side:
                continue
            k = _who(r)
            if k not in seen:
                t = roster.team_of(r["platform"], r["platform_id"], r["name"]) if roster else None
                seen[k] = (r["name"], t, r["platform"], r["platform_id"])
        votes = Counter(v[1] for v in seen.values() if v[1])
        team, n = None, 0
        if votes:
            (top, n), *rest = votes.most_common()
            if not rest or n > rest[0][1]:
                team = top
            else:
                n = 0
        out[side] = {"team": team, "votes": n, "players": list(seen.values())}
    return out


def _text_sides(line, fx, poster=()):
    """
    Assign the two names in the text to home/away. Returns
    (sides_named, winner_or_None, grade). Only an unambiguous assignment
    gives a winner; grade is how well the best name fitted (see _strength).

    Short forms collide - "VCU" is also the initials of Virginia Cavaliers -
    so when the names alone can't be ordered, the poster's own team breaks
    the tie: if one name clearly means the opponent, the other is the poster.
    """
    if not line:
        return set(), None, 0
    a, sa, sb, b = line
    h, w = fx["home"], fx["away"]
    ga = {x: _name_strength(a, x, from_end=True) for x in (h, w)}
    gb = {x: _name_strength(b, x, from_end=False) for x in (h, w)}
    # one typed name points at one team: if it fits both teams in this
    # fixture ("Colorado" in Colorado vs Colorado State), it means the one
    # it fits clearly better, or neither if that's a tie
    for g in (ga, gb):
        if g[h] and g[w]:
            strong, weak = (h, w) if g[h] > g[w] else (w, h)
            if g[h] == g[w] or (weak in poster and g[weak] >= 1):
                # a tie, or the weaker reading is the poster's own team by a
                # real word of its name - a Colorado State player writing
                # "Colorado" could mean either side, so it names neither
                g[h] = g[w] = 0
            else:
                g[weak] = 0
    # a name has to fit at least as well as initials to count as naming a
    # team; weaker fits ("South") only help rank a fixture already named
    ha = {x for x, g in ga.items() if g >= 0.5}
    hb = {x for x, g in gb.items() if g >= 0.5}
    orders = [(x, y) for x, y in ((h, w), (w, h)) if x in ha and y in hb]
    if len(orders) == 2:
        # both names fit both teams a little ("South Dakota State" vs
        # "Southeast Missouri State") - take the reading that fits better,
        # if one clearly does
        s1 = ga[h] + gb[w]
        s2 = ga[w] + gb[h]
        orders = [(h, w)] if s1 > s2 else [(w, h)] if s2 > s1 else orders
    if len(orders) != 1:
        orders = []
        mine = [x for x in (h, w) if x in poster]
        if len(mine) == 1:
            me = mine[0]
            them = w if me == h else h
            # the leftover name has to fit the poster's team or nothing at
            # all ("UVA") - not some third team. The text has then only
            # named the opponent; the poster's side comes from the role,
            # which is already counted as its own clue
            if them in hb and them not in ha and (me in ha or not ha):
                winner = me if sa > sb else them if sb > sa else None
                return {them}, winner, gb[them]
            if them in ha and them not in hb and (me in hb or not hb):
                winner = them if sa > sb else me if sb > sa else None
                return {them}, winner, ga[them]
    if orders:
        first, second = orders[0]
        winner = first if sa > sb else second if sb > sa else None
        grade = ga[first] + gb[second]
        return {first, second}, winner, grade
    named = ha | hb
    # one name fits exactly one side and the other name fits nothing
    # ("Marquette 3-2 Madison"): that's enough to say who won once the
    # fixture itself is settled by other clues - but it only names the one
    # side, so it can't help place the post
    winner = None
    if len(ha) == 1 and not hb:
        first = next(iter(ha))
        second = w if first == h else h
        winner = first if sa > sb else second if sb > sa else None
    elif len(hb) == 1 and not ha:
        second = next(iter(hb))
        first = w if second == h else h
        winner = first if sa > sb else second if sb > sa else None
    return named, winner, max([ga[x] for x in ha] + [gb[x] for x in hb] + [0])


def _lateness(posted: int, fixture_week: int) -> int:
    """0 = its own week, 1 = a week late, 2 = a week early, then further out."""
    d = posted - fixture_week
    return {0: 0, 1: 1, -1: 2}.get(d, 3 + abs(d))


def place(store, post: dict, roster=None) -> dict:
    """
    post: {week, poster_teams, text, games, rows}
    Returns a verdict dict: status is one of
      matched | ambiguous | unplaced | mismatch | already
    """
    line = parse_line(post["text"])
    rows = post["rows"]
    b, o = series_score(post["games"], rows) if post["games"] else (0, 0)
    sides_info = side_teams(rows, roster) if rows else {
        "A": {"team": None, "votes": 0, "players": []},
        "B": {"team": None, "votes": 0, "players": []}}
    colours = {k: v["team"] for k, v in sides_info.items()}
    strong = {v["team"] for v in sides_info.values() if v["team"] and v["votes"] >= 2}
    replay_score = (max(b, o), min(b, o))
    win_colour = "A" if b > o else "B" if o > b else None

    # every week, not just around the post date - the schedule is loose and
    # a week 1 series can be played in week 4. Weeks 1-6 never repeat a
    # pairing; conference play meets twice, and the first unplayed meeting is
    # the one a result belongs to.
    by_pair = {}
    for fx in store.fixtures:
        sides = {fx["home"], fx["away"]}
        known = {t for t in colours.values() if t}
        poster = sides & set(post["poster_teams"])
        tnamed, twinner, grade = _text_sides(line, fx, post["poster_teams"])
        rn = sides & known
        if strong - sides and not (
                (poster and tnamed and (poster | tnamed) == sides)
                # the text names both teams in order and the replays back up
                # one of them - an admin reposting, say - so it's the
                # roster that's wrong about the other side
                or (tnamed == sides and twinner and rn)):
            # two or more players rostered to a team that isn't in this
            # fixture - the files are the one clue nobody typed. But when the
            # poster's role and the text together name this fixture, the
            # roster is the thing that's wrong: write it and flag the roster
            continue
        clues, named, weight = [], set(), 0
        if poster:
            clues.append("poster")
            named |= poster
            weight += len(poster)
        if tnamed:
            clues.append("text")
            named |= tnamed
            weight += len(tnamed) + (1 if twinner and len(tnamed) == 2 else 0) + grade
        rnamed = sides & known
        if rnamed:
            clues.append("replays")
            named |= rnamed
            weight += 2 * len(rnamed)
        # text that names both teams in a clear order stands on its own -
        # it's what an admin reposting a result would type
        text_alone = clues == ["text"] and twinner and tnamed == sides and grade >= 4
        # both teams identified from the replays, each by two or more
        # rostered players, is the strongest single clue there is
        replays_alone = clues == ["replays"] and rnamed == sides
        if not (named == sides and (len(clues) >= 2 or text_alone or replays_alone)):
            continue
        pair = frozenset(sides)
        open_ = store.get(fx["id"])["status"] not in ("reported", "archived")
        prev = by_pair.get(pair)
        # per pairing: the earliest unplayed meeting, else the latest played
        better = (prev is None
                  or (open_ and not prev[1])
                  or (open_ == prev[1] and (fx["week"] < prev[2]["week"]) == open_))
        if better:
            by_pair[pair] = (weight, open_, fx, clues, twinner)

    if not by_pair:
        return {"status": "unplaced", "line": line, "colours": colours, "sides": sides_info,
                "replay_score": replay_score, "why": "no fixture fits two clues"}
    scored = sorted(by_pair.values(), key=lambda x: x[0], reverse=True)
    best = scored[0]
    tied = [s for s in scored if s[0] == best[0]]
    if len(tied) > 1:
        # nearly every series is played in its own week, a few days late at
        # most - so between equally good fits, the one in the posting week
        # (or the week before) wins, but only if it's clearly the closest
        ranked = sorted(tied, key=lambda s: _lateness(post["week"], s[2]["week"]))
        near = _lateness(post["week"], ranked[0][2]["week"])
        if near <= 1 and _lateness(post["week"], ranked[1][2]["week"]) > near:
            best = ranked[0]
        else:
            return {"status": "ambiguous", "line": line, "colours": colours, "sides": sides_info,
                    "replay_score": replay_score, "options": [s[2] for s in tied]}

    _, _, fx, clues, twinner = best
    verdict = {"fx": fx, "clues": clues, "line": line, "colours": colours,
               "sides": sides_info, "replay_score": replay_score,
               "win_side": win_colour, "flags": []}
    fx_teams = {fx["home"], fx["away"]}
    # who the Roster Sheet doesn't agree with - written anyway, flagged
    elsewhere, unrostered = {}, []
    for side in ("A", "B") if roster is not None else ():
        for name, t, plat, pid in sides_info[side]["players"]:
            if t is None and roster is not None:
                # now the two teams are known, Steam players can be matched by
                # name within them (a Steam name is only unique inside a match)
                try:
                    t = roster.team_of(plat, pid, name, teams=fx_teams)
                except TypeError:
                    pass
            if t is None:
                unrostered.append(name)
            elif t not in fx_teams:
                elsewhere.setdefault(t, []).append(name)
    for t, names in sorted(elsewhere.items()):
        verdict["flags"].append(("roster", f"roster lists {', '.join(names)} under {t}"))
    if unrostered:
        verdict["flags"].append(("unrostered", "not on roster: " + ", ".join(unrostered)))
    off = post["week"] - fx["week"]
    far = off > 1 or off < 0
    if far:
        verdict["note"] = (f"posted in week {post['week']}, fixture is week {fx['week']}"
                           " — unusual, check it")
    confirmed = {colours.get("A"), colours.get("B")} == {fx["home"], fx["away"]}

    # the winner: the text says it directly; the replays say it if the
    # roster knows who was on the winning colour
    rwinner, rvotes = None, 0
    if win_colour:
        lose_side = "B" if win_colour == "A" else "A"
        wt, lt = colours.get(win_colour), colours.get(lose_side)
        if wt and wt == lt:
            wt = lt = None          # both sides point at one team - no signal
        if wt in (fx["home"], fx["away"]):
            rwinner, rvotes = wt, sides_info[win_colour]["votes"]
        elif lt in (fx["home"], fx["away"]):
            rwinner = fx["away"] if lt == fx["home"] else fx["home"]
            rvotes = sides_info[lose_side]["votes"]
    if twinner and rwinner and twinner != rwinner:
        if rvotes >= 2:
            return {**verdict, "status": "mismatch",
                    "why": f"text says {twinner} won, replays say {rwinner} "
                           f"({rvotes} rostered players agree)"}
        # one rostered player isn't enough to overrule what the team posted
        verdict["note"] = ((verdict.get("note", "") + "; ") if verdict.get("note") else "") + (
            f"one rostered player suggests {rwinner} won — went with the post ({twinner})")
        rwinner = twinner
    winner = twinner or rwinner
    if not winner:
        return {**verdict, "status": "unplaced", "why": "can't tell who won"}

    undecided = sum(1 for g in post["games"] if g["blue_goals"] == g["orange_goals"])
    if line:
        claimed = (max(line[1], line[2]), min(line[1], line[2]))
        if (undecided and twinner and claimed[1] == replay_score[1]
                and claimed[0] == replay_score[0] + undecided
                and (rwinner in (None, twinner))):
            # a replay with no winner in it (forfeited or remade game) - the
            # rest of the series fits the post exactly, so count it their way
            verdict["note"] = ((verdict.get("note", "") + "; ") if verdict.get("note") else "") + (
                f"{undecided} replay(s) had no winner in the file (forfeit or remake?) "
                f"— counted for {twinner} per the post")
            verdict["flags"].append(("incomplete", f"{undecided} replay(s) with no winner in the file"))
            replay_score = claimed
        need_ = int(store.cfg("wins_needed", 3))
        cw = twinner or rwinner     # the post's winner, else the replays' (roster-known)
        if (claimed != replay_score and cw and claimed[0] == need_
                and claimed[1] < need_ and post["games"]):
            # fewer replays than games played. Save the series as posted and
            # the stats that exist - but only if the replays fit the score,
            # and only once we know which side is the winner's
            wins = {"A": b, "B": o}
            tloser = fx["away"] if cw == fx["home"] else fx["home"]
            fits = [x for x in ("A", "B")
                    if wins[x] <= claimed[0] and wins["B" if x == "A" else "A"] <= claimed[1]]
            known_side = None
            for x in ("A", "B"):
                if colours.get(x) == cw or colours.get("B" if x == "A" else "A") == tloser:
                    known_side = x if known_side in (None, x) else "?"
            side = (known_side if known_side in fits
                    else fits[0] if len(fits) == 1 and known_side is None else None)
            if side and sum(wins.values()) < sum(claimed):
                got = len(post["games"])
                verdict["flags"].append(("missing", f"missing replay(s): {got} of {sum(claimed)} games"))
                verdict["win_side"] = side
                rwinner = cw
                replay_score = claimed
        if claimed != replay_score:
            return {**verdict, "status": "mismatch",
                    "why": f"text says {claimed[0]}-{claimed[1]}, "
                           f"{len(post['games'])} replay(s) give "
                           f"{replay_score[0]}-{replay_score[1]}"}
    need = int(store.cfg("wins_needed", 3))
    if replay_score[0] != need or replay_score[1] >= need:
        return {**verdict, "status": "mismatch",
                "why": f"replays give {replay_score[0]}-{replay_score[1]}, "
                       f"not a finished best of {store.cfg('series_length', 5)}"}

    loser = fx["away"] if winner == fx["home"] else fx["home"]
    hi, lo = replay_score
    held = far and not confirmed
    verdict.update(
        status=("already" if store.get(fx["id"])["status"] in ("reported", "archived")
                else "far" if held else "matched"),
        winner=winner, loser=loser,
        home=hi if winner == fx["home"] else lo,
        away=lo if winner == fx["home"] else hi,
    )
    return verdict
