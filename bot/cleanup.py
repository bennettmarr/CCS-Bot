"""
Tidying the Game Stats tab as read off the sheet, before anything counts it.

Two problems found in the 10/6 account audit:

* Duplicate lines. The same player in the same game written twice, so their
  season totals counted that game twice. Mostly from a series being recorded
  by two paths at once (live feed + catch-up).
* Mangled Steam ids. A 17-digit Steam id written into a cell Sheets treated
  as a number comes back as "7.66E+16". The id is lost, so the line can't be
  matched to the roster and the duplicate check can't see it either.

Everything here is pure - lists in, lists out - so website.py, roster_issues.py
and the relabel can all share it.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .roster import norm

_MANGLED = re.compile(r"^\s*(\d)(?:\.(\d+))?E\+(\d+)\s*$", re.I)


def is_mangled(pid: str) -> bool:
    return bool(_MANGLED.match(str(pid or "")))


def _close(mangled: str, real: str) -> bool:
    """Could `real` have been rounded to `mangled` (e.g. 7.66E+16)?"""
    m = _MANGLED.match(mangled or "")
    if not m or not str(real).isdigit():
        return False
    lead, frac, exp = m.group(1), m.group(2) or "", int(m.group(3))
    approx = float(f"{lead}.{frac or '0'}e{exp}")
    slack = 0.5 * 10 ** (exp - len(frac))
    return abs(int(real) - approx) <= slack


def repair_id(roster, team: str, names: List[str], mangled: str) -> Optional[str]:
    """
    The real Steam id behind "7.66E+16": the one exact Steam entry on that
    team's roster whose name matches and whose id rounds to the mangled value.
    None if there isn't exactly one.
    """
    if roster is None:
        return None
    wanted = {norm(n) for n in names if norm(n)}
    hits = [r for r in roster.teams.get(team, [])
            if r.get("exact") and r.get("platform") == "steam"
            and _close(mangled, r.get("id", ""))
            and (not wanted or norm(r.get("name", "")) in wanted)]
    if len(hits) != 1:
        # name didn't line up - fall back to the id alone, still on that team
        hits = [r for r in roster.teams.get(team, [])
                if r.get("exact") and r.get("platform") == "steam"
                and _close(mangled, r.get("id", ""))]
    return hits[0]["id"] if len(hits) == 1 else None


def _who(v) -> str:
    plat = (v[9] or "").lower()
    pid = v[10]
    if pid and not is_mangled(pid):
        return f"{plat}:{norm(pid)}"
    return f"{plat}:name:{norm(v[7])}"


def clean_stats(game_stats: List[List[str]], roster=None) -> Tuple[List[List[str]], int, int]:
    """
    game_stats: the tab as read, header first. Returns (rows, duplicates
    dropped, ids repaired). Rows are padded to 20 columns.

    A line is a duplicate of an earlier one when it's the same account in the
    same replay - or in the same series at the same game time, which catches
    one game uploaded twice under two replay ids.
    """
    if not game_stats:
        return [], 0, 0
    out = [list(game_stats[0])]
    seen = set()
    dups = fixed = 0
    for v in game_stats[1:]:
        v = list(v) + [""] * (20 - len(v))
        if not v[0]:
            out.append(v)
            continue
        if (v[9] or "").lower() == "steam" and is_mangled(v[10]):
            real = repair_id(roster, v[5], [v[7], v[8]], v[10])
            if real:
                v[10] = real
                fixed += 1
        who = _who(v)
        keys = [("r", v[2], who)]
        if v[4]:
            keys.append(("d", v[0], v[4], who))
        if any(k in seen for k in keys):
            dups += 1
            continue
        seen.update(keys)
        out.append(v)
    return out, dups, fixed


def clean_scores(game_scores: List[List[str]]) -> Tuple[List[List[str]], int]:
    """Game Scores with any replay listed twice (or the same game twice) dropped."""
    if not game_scores:
        return [], 0
    out = [list(game_scores[0])]
    seen = set()
    dups = 0
    for v in game_scores[1:]:
        v = list(v) + [""] * (14 - len(v))
        keys = [("r", v[2])] if v[2] else []
        if v[0] and v[4]:
            keys.append(("d", v[0], v[4]))
        if keys and any(k in seen for k in keys):
            dups += 1
            continue
        seen.update(keys)
        out.append(v)
    return out, dups


def sheet_id(pid: str) -> str:
    """
    An account id as it should be written with USER_ENTERED: a leading
    apostrophe keeps a long number as text, so Sheets can't round a Steam id
    into 7.66E+16. The apostrophe isn't stored - reading it back gives the
    plain id.
    """
    s = str(pid or "")
    return "'" + s if s.isdigit() else s


def sheet_text(s: str) -> str:
    """
    A player name as it should be written with USER_ENTERED, so "-", "+1" or
    "=)" stay text instead of turning into a number or a formula.
    """
    s = str(s or "")
    if s[:1] in ("=", "+", "-", "@") or re.fullmatch(r"[\d.,eE+-]+", s or "x"):
        return "'" + s
    return s


def sheet_row(v: List[str]) -> List[str]:
    """A whole Game Stats line ready to write back with USER_ENTERED."""
    v = list(v) + [""] * (20 - len(v))
    return [*v[:7], sheet_text(v[7]), sheet_text(v[8]), v[9], sheet_id(v[10]), *v[11:20]]
