"""
Reading the Roster Sheet.

The sheet is a visual layout, not a table: two team blocks side by side, each
a logo, a team name across the top, then a row per player holding a name and a
tracker link. Merged cells everywhere.

So this doesn't hardcode columns. It works on shape instead:

  a row with a tracker link in it   -> a player, name taken from the nearest
                                       text cell to the left
  a row with text but no link       -> a team name, which applies to the
                                       players below it in the same block

Blocks are told apart by which side of the sheet a cell sits on, so the layout
can shift left or right without breaking this.

Identity is (platform, id) pulled from the tracker URL, because that is
exactly what ballchasing reports. Steam links carry the numeric id and match
perfectly. Epic and PSN links carry a display name, which ballchasing may or
may not agree with - those fall back to comparing names, and are marked as the
softer match they are.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

LINK_RE = re.compile(
    # tracker.network and tracker.gg both turn up on the sheet; the trailing
    # slash is optional so a link cut off after the id still counts
    r"tracker\.(?:network|gg)/rocket-league/profile/([a-z0-9]+)/([^/\s?#]+)", re.I
)

ALIASES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "aliases.json")

# words too common to tell two schools apart on their own
_GENERIC = {"state", "university", "the", "of", "at", "and", "st", "saint", "esports",
            "college", "u"}


def _load_aliases() -> Dict[str, str]:
    try:
        with open(ALIASES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {norm(k): v for k, v in raw.items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def resolve_team(text: str, teams_known, aliases: Dict[str, str] = None) -> Optional[str]:
    """
    A team header as written on the Roster Sheet -> the team's name in
    teams.json, or None.

    The sheet's spellings drift from teams.json ("BYU Cougars" vs "Cougars",
    "Robert Morris Colones", "Rhode Island"), so in order:
      1. exact, then ignoring case/spacing/punctuation
      2. data/aliases.json (same file /report uses)
      3. word overlap - only when exactly one team shares the most
         distinctive words, and at least two of them. One shared word
         ("Mavericks", "Cougars") is never enough on its own.
    """
    if not text or not teams_known:
        return None
    text = text.strip()
    if text in teams_known:
        return text
    n = norm(text)
    for t in teams_known:
        if norm(t) == n:
            return t
    aliases = _load_aliases() if aliases is None else aliases
    hit = aliases.get(n)
    if hit in teams_known:
        return hit

    def words(s):
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _GENERIC}

    mine = words(text)
    if not mine:
        return None
    scored = []
    for t in teams_known:
        theirs = words(t)
        shared = mine & theirs
        if not shared:
            continue
        if len(shared) >= 2:
            scored.append((len(shared), t))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None   # a tie is a guess - leave it for a human
    return scored[0][1]

PLATFORM_FIX = {"psn": "ps4", "xbl": "xbox", "steam": "steam", "epic": "epic"}

# steam ids are numeric and stable; the others are display names on the
# tracker side, so a match there is a weaker claim
EXACT_PLATFORMS = {"steam"}


def parse_link(url: str) -> Optional[Tuple[str, str]]:
    """('steam', '76561198...') or ('epic', 'Zeek ') - None if it isn't one."""
    if not url:
        return None
    m = LINK_RE.search(url)
    if not m:
        return None
    platform = PLATFORM_FIX.get(m.group(1).lower(), m.group(1).lower())
    ident = urllib.parse.unquote(m.group(2)).strip()
    return platform, ident


def _fetch_steam64(vanity: str, timeout: float = 6.0) -> Optional[str]:
    """steam64 id for a Steam custom URL, from the public profile XML. None if not found."""
    import urllib.request
    url = ("https://steamcommunity.com/id/"
           + urllib.parse.quote(vanity, safe="") + "/?xml=1")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ccsbot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200_000).decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"<steamID64>\s*(\d{17})\s*</steamID64>", body)
    return m.group(1) if m else None


def parse_links(text: str) -> List[Tuple[str, str]]:
    """Every tracker link in one cell, as parse_link would read each."""
    out = []
    for m in LINK_RE.finditer(text or ""):
        platform = PLATFORM_FIX.get(m.group(1).lower(), m.group(1).lower())
        out.append((platform, urllib.parse.unquote(m.group(2)).strip()))
    return out


def norm(s: str) -> str:
    # letters and digits in any alphabet - stripping to a-z only turned
    # names like "ヤップ小屋" into "" and made unrelated players collide
    return re.sub(r"[\W_]+", "", (s or "").lower())


class Roster:
    """
    Plus a learned identity map.

    Ballchasing identifies an Epic player by account id - cbd7f7eb595f... -
    while the tracker links on the Roster Sheet carry the display name. Those
    never match directly, so the first time an Epic id turns up alongside a
    display name that matches exactly one roster entry, the pairing is
    remembered. After that the account id is the key, and the player can
    rename themselves as often as they like without falling off their team.

    The map lives on disk and is safe to hand-edit if one is ever learned
    wrong.
    """

    def __init__(self):
        self.players: List[dict] = []          # {team, name, platform, id, exact}
        self.by_identity: Dict[Tuple[str, str], dict] = {}
        self.by_name: Dict[str, List[dict]] = {}
        self.teams: Dict[str, List[dict]] = {}
        self.missing: List[Tuple[str, str]] = []   # (team, player) with no link
        self.learned: Dict[str, dict] = {}         # "epic:<id>" -> roster entry
        self.newly_learned: List[str] = []
        self.manual: set = set()                        # learned keys set by /link
        self.header_fixes: List[Tuple[str, str]] = []   # (as written, team used)
        self.unknown_headers: List[str] = []            # headers matching no team
        self.unplaced: List[str] = []                   # players under those

    # ------------------------------------------------------------------ parse
    def load_grid(self, rows: List[List[str]], teams_known=None) -> None:
        """
        rows is the raw Roster Sheet as get_all_values() gives it.
        teams_known, if passed, restricts team headers to real team names so a
        stray title like "Roster Sheet" can't be mistaken for one.
        """
        self.__init__()
        if not rows:
            return
        aliases = _load_aliases()
        width = max(len(r) for r in rows)
        midpoint = width // 2
        current = {0: None, 1: None}   # team name per block

        for row in rows:
            cells = list(row) + [""] * (width - len(row))
            # where the links are on this row tells us which blocks are in play
            link_cols = [i for i, c in enumerate(cells) if parse_link(c)]

            if link_cols:
                # a cell can hold more than one link ("steam/... , epic/...")
                # - every one of them is this player
                found = [(col, ident) for col in link_cols for ident in parse_links(cells[col])]
                for col, ident in found:
                    block = 0 if col < midpoint else 1
                    name = self._name_left_of(cells, col, block, midpoint)
                    team = current[block]
                    if not ident:
                        continue
                    if not team:
                        self.unplaced.append(name or ident[1])
                        continue
                    platform, pid = ident
                    rec = {
                        "team": team,
                        "name": name,
                        "platform": platform,
                        "id": pid,
                        # a Steam link by custom URL ("steam/LutheMonkey") has
                        # no steam64 id to compare, so it matches by name instead
                        "exact": platform in EXACT_PLATFORMS and pid.strip().isdigit(),
                    }
                    self.players.append(rec)
                    self.by_identity[(platform, norm(pid))] = rec
                    if norm(name):
                        self.by_name.setdefault(norm(name), []).append(rec)
                    # Epic/PSN/Xbox links carry the in-game name ("epic/champiwnl.")
                    # while the name column often holds a nickname ("Champ") -
                    # so the link's name counts as a name for this player too
                    if not rec["exact"] and norm(pid) and norm(pid) != norm(name):
                        self.by_name.setdefault(norm(pid), []).append(rec)
                    self.teams.setdefault(team, []).append(rec)
                continue

            # no links: any substantial text is a team header for its block.
            # Team headers sit side by side, so a row that starts a team in
            # one block starts a new team in the other block too - even if
            # that cell is blank (being retyped, say). Without this, the
            # players under a blank header were filed under the team above
            # them (UTA's players under Arkansas Little Rock, 10/10)
            if teams_known:
                starts = set()
                for i, c in enumerate(cells):
                    text = (c or "").strip()
                    if len(text) >= 3 and resolve_team(text, teams_known, aliases):
                        starts.add(0 if i < midpoint else 1)
                if starts:
                    for blk in (0, 1):
                        if blk not in starts:
                            lo, hi = (0, midpoint) if blk == 0 else (midpoint, width)
                            if not any((cells[j] or "").strip() for j in range(lo, hi)):
                                if current[blk] is not None:
                                    self.unknown_headers.append(f"(blank header after {current[blk]})")
                                current[blk] = None
            for i, c in enumerate(cells):
                text = (c or "").strip()
                if not text or len(text) < 3:
                    continue
                block = 0 if i < midpoint else 1
                if not teams_known:
                    current[block] = text
                    continue
                team = resolve_team(text, teams_known, aliases)
                if team:
                    current[block] = team
                    if team != text:
                        self.header_fixes.append((text, team))
                elif self._looks_like_header(rows, row, i):
                    # an unknown header ends the previous team's block, so its
                    # players are reported as unplaced instead of quietly
                    # landing on whoever was above them
                    current[block] = None
                    self.unknown_headers.append(text)

    @staticmethod
    def _looks_like_header(rows, row, col) -> bool:
        """
        A team header has blank rows under it before its players start; a
        player who just has no tracker link sits directly between other
        player rows. Only the first should end a block.
        """
        try:
            idx = next(i for i, r in enumerate(rows) if r is row)
        except StopIteration:
            return False
        nxt = rows[idx + 1] if idx + 1 < len(rows) else []
        below = (nxt[col] if col < len(nxt) else "").strip()
        return not below

    @staticmethod
    def _name_left_of(cells, col, block, midpoint) -> str:
        """The player's name is the nearest text to the left, inside the block."""
        floor = 0 if block == 0 else midpoint
        for i in range(col - 1, floor - 1, -1):
            text = (cells[i] or "").strip()
            if text and not parse_link(text):
                return text
        return ""

    # ------------------------------------------------------------------ check
    def check(self, platform: str, pid: str, name: str, team: str):
        """
        Compare one line from a replay against the roster.

        Returns (flag, detail, record). An empty flag means it looks right;
        record is the roster entry it matched, or None. The caller wants the
        record so stats can carry the roster name rather than whatever tag
        the player happened to be using that night.
        """
        key = (platform, norm(pid))
        rec = self.by_identity.get(key)

        # an account id we've tied to someone before
        learned_key = f"{platform}:{norm(pid)}"
        if rec is None:
            rec = self._learned_for(platform, pid, {team} if team else None)

        if rec is None:
            # first sighting: tracker links hold a display name, so go on that,
            # and remember the account id for next time. Never onto a roster
            # entry with a real steam64 id - a different id there is a
            # different account
            candidates = self._name_candidates(platform, name, {team} if team else None)
            if len(candidates) == 1:
                rec = candidates[0]
                self.learned[learned_key] = rec
                self.newly_learned.append(
                    f"{name} ({platform}) -> {rec['team']}"
                )
            elif len(candidates) > 1:
                return "AMBIGUOUS", f"{name} matches {len(candidates)} roster entries", None

        if rec is None:
            return "UNROSTERED", f"{name} ({platform}) isn't on the roster sheet", None
        if rec["team"] != team:
            return (
                "WRONG TEAM",
                f"{name} is rostered to {rec['team']}, played for {team}",
                rec,
            )
        if learned_key in self.learned:
            return "", f"matched {name} by remembered {platform} id", rec
        if not rec["exact"]:
            return "", f"matched {name} by name ({platform})", rec
        return "", "", rec

    def _name_candidates(self, platform: str, name: str, teams=None) -> List[dict]:
        """
        Roster entries this in-game name could be - excluding exact Steam ones.

        Epic, PlayStation and Xbox names are unique, so they can match a roster
        name anywhere. Steam names are NOT unique (two different people can
        both be "luca"), so a Steam player only matches by name to someone on
        one of the teams actually in the match - and with no teams given, not
        at all.
        """
        n = norm(name)
        if not n:
            return []
        steam = (platform or "").lower() == "steam"
        if steam and not teams:
            return []
        out, seen = [], set()
        for rec in self.by_name.get(n, []):
            if steam and rec["team"] not in teams:
                continue
            # one person with two links (Xbox + Epic on the same roster row)
            # is still one person, not two candidates
            who = (rec["team"], norm(rec["name"]))
            if rec["exact"] or who in seen:
                continue
            seen.add(who)
            out.append(rec)
        return out

    def _learned_for(self, platform: str, pid: str, teams=None):
        """
        A remembered account. Steam ones the bot learned from a name only
        count when that player is in a match involving the team it was learned
        for - Steam names aren't unique, so a name-learned Steam id could be
        somebody else. /link entries (made by an admin) always count.
        """
        key = f"{platform}:{norm(pid)}"
        rec = self.learned.get(key)
        if rec is None:
            return None
        if (platform or "").lower() == "steam" and key not in self.manual:
            if not teams or rec["team"] not in teams:
                return None
        return rec

    def team_of(self, platform: str, pid: str, name: str, teams=None):
        """
        Which team a replay line belongs to, or None. Read-only - unlike
        check() this never learns an id, so guessing at a match can't
        teach the roster something wrong. teams = the two teams in the match
        (needed before a Steam player can be matched by name).
        """
        rec = self.by_identity.get((platform, norm(pid)))
        if rec is None:
            rec = self._learned_for(platform, pid, teams)
        if rec is None:
            cands = self._name_candidates(platform, name, teams)
            if len(cands) == 1:
                rec = cands[0]
        return rec["team"] if rec else None

    # ------------------------------------------------------- steam custom urls
    def resolve_vanity(self, cache_path: str, fetch: bool = True, timeout: float = 6.0) -> Tuple[int, int]:
        """
        Turn Steam custom-URL links ("steam/LutheMonkey") into real steam64 ids.

        Those links carry no id, so until now the player could only be matched
        by name - and anyone playing under a slightly different name showed up
        as a "different account". Steam's public profile XML gives the id for
        a custom URL with no API key. Answers are cached on disk, so this only
        reaches Steam for links it hasn't seen before.

        Returns (resolved, still unresolved).
        """
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        changed = False
        resolved = missing = 0
        for rec in self.players:
            if rec["platform"] != "steam" or rec["exact"]:
                continue
            vanity = rec["id"].strip()
            key = vanity.lower()
            sid = cache.get(key)
            if not sid and fetch:
                # only successes are cached, so a Steam hiccup gets retried
                # on the next load instead of sticking
                sid = _fetch_steam64(vanity, timeout)
                if sid:
                    cache[key] = sid
                    changed = True
            if sid:
                rec["vanity"] = vanity
                rec["id"] = sid
                rec["exact"] = True
                self.by_identity[("steam", norm(sid))] = rec
                resolved += 1
            else:
                missing += 1
        if changed:
            try:
                tmp = cache_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=1, sort_keys=True)
                os.replace(tmp, cache_path)
            except OSError:
                pass
        return resolved, missing

    # --------------------------------------------------------------- on disk
    def save_learned(self, path: str) -> None:
        try:
            out = {
                k: dict({"team": v["team"], "name": v["name"]},
                        **({"manual": True} if k in self.manual else {}))
                for k, v in self.learned.items()
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            pass

    def load_learned(self, path: str) -> int:
        """
        Re-attach saved ids to the roster as it stands now.

        Matching by team and name rather than restoring the old record, so a
        player who has genuinely moved teams since picks up their new team
        instead of being pinned to a stale one.
        """
        if not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return 0
        n = 0
        for key, meta in saved.items():
            if meta.get("manual"):
                self.manual.add(key)
            for rec in self.teams.get(meta.get("team", ""), []):
                if norm(rec["name"]) == norm(meta.get("name", "")):
                    self.learned[key] = rec
                    n += 1
                    break
            else:
                cands = self.by_name.get(norm(meta.get("name", "")), [])
                if len(cands) == 1:
                    self.learned[key] = cands[0]
                    n += 1
        return n

    def find_team(self, text: str, known=None) -> List[str]:
        """
        Resolve free text to team names. Returns every plausible match, so the
        caller can show a short list rather than guessing between "Michigan
        State Spartans" and "Michigan Wolverines".
        """
        pool = list(known) if known else list(self.teams)
        t = norm(text)
        if not t:
            return []
        exact = [x for x in pool if norm(x) == t]
        if exact:
            return exact
        starts = [x for x in pool if norm(x).startswith(t)]
        if starts:
            return sorted(starts)
        words = [x for x in pool if t in [norm(w) for w in x.split()]]
        if words:
            return sorted(words)
        return sorted(x for x in pool if t in norm(x))

    def missing_trackers(self, expected_teams=None) -> List[str]:
        """Teams with fewer than three rostered players - i.e. gaps to chase."""
        out = []
        for team, players in sorted(self.teams.items()):
            if len(players) < 3:
                out.append(f"{team}: only {len(players)} with tracker links")
        if expected_teams:
            for t in sorted(expected_teams):
                if t not in self.teams:
                    out.append(f"{t}: no players found at all")
        return out
