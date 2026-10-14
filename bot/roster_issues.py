"""
The Roster Issues tab: one row per player whose replays don't line up with
the Roster Sheet, with the tracker link the replays actually show and a
suggested fix - so staff can work down the list team by team.

Checked fresh against the Roster Sheet as it stands now (not the flags saved
when each game was written), so the list shrinks as the sheet gets fixed.
Rebuilt on every website sync, after /stats relabel, and with
/stats action:website.
"""
from __future__ import annotations

import difflib
import json
import os
from collections import defaultdict
from typing import Dict, List

from .roster import norm
from .stats import tracker_url

TAB = "Roster Issues"
HEADERS = ["Team", "Conference", "Issue", "Player (in replays)", "Platform",
           "Tracker link from replays", "Roster Sheet says", "Roster Sheet link",
           "Suggested fix", "Probably the same person as", "Account (for /link)",
           "Games", "Series", "Last week", "Example series"]

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
ORDER = {"WRONG LINK?": 0, "WRONG TEAM": 1, "AMBIGUOUS": 2, "UNROSTERED": 3}


def _similar(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _n(x) -> float:
    try:
        return float(str(x).replace(",", "") or 0)
    except ValueError:
        return 0.0


def _link(rec: dict) -> str:
    from urllib.parse import quote
    return tracker_url(rec.get("platform", ""), quote(rec.get("id", ""), safe="")) if rec else ""


def build(game_stats: List[List[str]], fixtures: Dict[str, dict], roster) -> List[List]:
    """game_stats: the Game Stats tab as read (header row first)."""
    game_stats = _clean(game_stats, roster)
    try:
        with open(os.path.join(DATA, "conferences.json"), encoding="utf-8") as f:
            conf = json.load(f)
    except (OSError, ValueError):
        conf = {}

    def resolve(plat, pid, name, team):
        rec = roster.by_identity.get((plat.lower(), norm(pid))) or \
            roster._learned_for(plat.lower(), pid, {team})
        if rec is None:
            c = roster._name_candidates(plat.lower(), name, {team})
            if len(c) == 1:
                rec = c[0]
        return rec

    # which rostered players of each team turned up in each series
    present = defaultdict(set)   # (fixture, team) -> {(team, roster name)}
    seen = {}   # (platform:id, team) -> info
    for v in game_stats[1:]:
        v = list(v) + [""] * (20 - len(v))
        fid, team, name, plat, pid = v[0], v[5], v[7], v[9], v[10]
        if not fid or not team:
            continue
        if not any(_n(v[i]) > 0 for i in (12, 13, 14, 15, 16)):
            continue            # spectator - nothing to check
        key = (f"{plat.lower()}:{pid}" if pid else f"name:{norm(name)}", team)
        d = seen.setdefault(key, {"team": team, "names": [], "platform": plat, "pid": pid,
                                  "tracker": v[11], "games": set(), "series": set(),
                                  "week": 0, "example": fid})
        if name and name not in d["names"]:
            d["names"].append(name)
        r0 = resolve(plat, pid, name, team)
        if r0 is not None and r0["team"] == team:
            present[(fid, team)].add((r0["team"], norm(r0["name"])))
        d["games"].add(v[2])
        d["series"].add(fid)
        try:
            w = int(float(v[1] or 0))
        except ValueError:
            w = 0
        if w >= d["week"]:
            d["week"], d["example"] = w, fid

    out = []
    for (who, team), d in seen.items():
        name = d["names"][-1] if d["names"] else ""
        plat, pid = d["platform"], d["pid"]
        rec = roster.by_identity.get((plat.lower(), norm(pid))) or \
            roster._learned_for(plat.lower(), pid, {team})
        cands = [] if rec else roster._name_candidates(plat.lower(), name, {team})
        if rec is None and len(cands) == 1:
            rec = cands[0]

        issue = says = rlink = fix = ""
        if rec is not None:
            if rec["team"] == team:
                continue                       # all good
            issue = "WRONG TEAM"
            says = f"{rec['name']} under {rec['team']}"
            rlink = _link(rec)
            fix = (f"Rostered to {rec['team']}. If they've moved, list them under {team} "
                   f"instead - if they only subbed, leave it.")
        elif len(cands) > 1:
            issue = "AMBIGUOUS"
            says = "; ".join(f"{c['name']} under {c['team']}" for c in cands[:4])
            fix = ("Same name as more than one roster entry - make sure the right entry "
                   "has this tracker link so they can be told apart.")
        else:
            def like(r):
                # the name column, or the in-game name inside an Epic/PSN link
                return _similar(r["name"], name) or (not r.get("exact") and _similar(r["id"], name))
            same_team = [r for r in roster.teams.get(team, []) if like(r)]
            elsewhere = [r for r in roster.players if r["team"] != team and like(r)]
            if same_team:
                r = same_team[0]
                issue = "WRONG LINK?"
                says = f"{r['name']} under {team}"
                rlink = _link(r)
                fix = (f"Roster has {r['name']} on {team} but with a different link - "
                       f"probably old or wrong. Replace it with the tracker link from replays.")
            elif elsewhere:
                r = elsewhere[0]
                issue = "UNROSTERED"
                says = f"similar name: {r['name']} under {r['team']}"
                rlink = _link(r)
                fix = (f"Not on the {team} roster. A {r['name']} is listed under {r['team']} - "
                       f"if that's them, fix that entry; otherwise add them to the {team} roster.")
            else:
                issue = "UNROSTERED"
                says = "not on the Roster Sheet" + (
                    f" ({team} has nobody listed)" if not roster.teams.get(team) else "")
                fix = f"Add to the {team} roster with the tracker link from replays."

        # the one rostered player of this team who never showed up in any of
        # the series this account played - very likely the same person on a
        # different account (console players often appear as Epic on PC)
        likely = ""
        if issue in ("UNROSTERED", "WRONG LINK?") and roster.teams.get(team):
            people = {}
            for r in roster.teams[team]:
                people.setdefault((r["team"], norm(r["name"])), r)
            absent = None
            for fid in d["series"]:
                missing = set(people) - present.get((fid, team), set())
                absent = missing if absent is None else absent & missing
            if absent and len(absent) == 1:
                r = people[next(iter(absent))]
                likely = f"{r['name']} ({_link(r)})"
        account = f"{plat.lower()}:{pid}" if pid else ""

        names = ", ".join(d["names"][-3:])
        out.append([team, conf.get(team, ""), issue, names, plat,
                    d["tracker"] or (tracker_url(plat, pid) if pid else ""),
                    says, rlink, fix, likely, account, len(d["games"]), len(d["series"]),
                    d["week"], d["example"]])

    out.sort(key=lambda r: (r[0], ORDER.get(r[2], 9), -r[11], r[3].lower()))
    return [HEADERS] + out


def report(game_stats: List[List[str]], fixtures: Dict[str, dict], roster,
           state: Dict[str, dict] = None, team_filter: str = None) -> str:
    """
    The same check as the Roster Issues tab, written out as a text file for
    staff: per team, every account that doesn't line up with the Roster
    Sheet, the series it played, and - the useful part for working out who
    it is - the correctly rostered teammates who were in those same games.
    """
    state = state or {}
    game_stats = _clean(game_stats, roster)
    rows = build(game_stats, fixtures, roster)
    h = rows[0]
    col = {name: i for i, name in enumerate(h)}

    def resolve(plat, pid, name, team):
        rec = roster.by_identity.get((plat.lower(), norm(pid))) or \
            roster._learned_for(plat.lower(), pid, {team})
        if rec is None:
            c = roster._name_candidates(plat.lower(), name, {team})
            if len(c) == 1:
                rec = c[0]
        return rec

    # per game: who played for which team, and who that resolves to
    game_team = defaultdict(list)          # (replay, team) -> [(account, name, rec)]
    acct_games = defaultdict(set)          # (account, team) -> {replay}
    acct_series = defaultdict(set)         # (account, team) -> {fixture}
    for v in game_stats[1:]:
        v = list(v) + [""] * (20 - len(v))
        fid, rid, team, name, plat, pid = v[0], v[2], v[5], v[7], v[9], v[10]
        if not fid or not team or not rid:
            continue
        if not any(_n(v[i]) > 0 for i in (12, 13, 14, 15, 16)):
            continue
        acct = f"{plat.lower()}:{pid}" if pid else f"name:{norm(name)}"
        game_team[(rid, team)].append((acct, name, resolve(plat, pid, name, team)))
        acct_games[(acct, team)].add(rid)
        acct_series[(acct, team)].add(fid)

    by_team = defaultdict(list)
    for r in rows[1:]:
        if team_filter and norm(team_filter) not in norm(r[col["Team"]]):
            continue
        by_team[r[col["Team"]]].append(r)

    out = ["CCS ROSTER REPORT - accounts that don't match the Roster Sheet",
           "For each one: the account, the team it played for, and the correctly",
           "rostered teammates who were in the same games - ask them who it is.",
           f"{sum(len(v) for v in by_team.values())} accounts across {len(by_team)} teams", ""]
    for team in sorted(by_team):
        items = by_team[team]
        roster_names = ", ".join(r["name"] for r in roster.teams.get(team, [])) or "nobody listed"
        out += ["=" * 72, f"{team}  ({items[0][col['Conference']] or 'no conference'})"
                f"  -  {len(items)} to check", f"Roster Sheet: {roster_names}", "=" * 72]
        for r in items:
            acct = r[col["Account (for /link)"]] or ""
            names = r[col["Player (in replays)"]]
            out += ["", f"[{r[col['Issue']]}]  {names}",
                    f"  Account:      {acct or '(no id)'}",
                    f"  Tracker:      {r[col['Tracker link from replays']]}"]
            # series played, with opponent and the post
            lines = []
            for fid in sorted(acct_series.get((acct, team), ()),
                              key=lambda f: (fixtures.get(f, {}).get("week", 0), f)):
                fx = fixtures.get(fid, {})
                opp = fx.get("away") if fx.get("home") == team else fx.get("home", "?")
                link = ((state.get(fid) or {}).get("result") or {}).get("link", "")
                lines.append(f"wk{fx.get('week', '?')} vs {opp}" + (f"  {link}" if link else ""))
            games = len(acct_games.get((acct, team), ()))
            out.append(f"  Played:       {games} game(s) in {len(lines)} series")
            out += [f"                {ln}" for ln in lines[:6]]
            # rostered teammates in those same games
            mates = defaultdict(int)
            for rid in acct_games.get((acct, team), ()):
                for a2, n2, rec in game_team.get((rid, team), []):
                    if a2 != acct and rec is not None and rec["team"] == team:
                        mates[rec["name"]] += 1
            if mates:
                out.append("  Played with:  " + ", ".join(
                    f"{n} ({c} game{'s' if c != 1 else ''})" for n, c in
                    sorted(mates.items(), key=lambda x: (-x[1], x[0].lower()))))
            else:
                out.append("  Played with:  no correctly rostered teammates in those games")
            missing = [x["name"] for x in roster.teams.get(team, []) if x["name"] not in mates]
            if missing and roster.teams.get(team):
                out.append(f"  Not seen:     {', '.join(missing)}  (rostered but not in those games)")
            if r[col["Roster Sheet says"]]:
                out.append(f"  Roster says:  {r[col['Roster Sheet says']]}"
                           + (f"  {r[col['Roster Sheet link']]}" if r[col["Roster Sheet link"]] else ""))
            if r[col["Probably the same person as"]]:
                out.append(f"  Probably:     {r[col['Probably the same person as']]}")
            out.append(f"  Fix:          {r[col['Suggested fix']]}")
            if acct and not acct.startswith("name:"):
                out.append(f"  /link account:{acct} team:{team} player:<roster name>")
        out.append("")
    if not by_team:
        out.append("Nothing to check" + (f" for '{team_filter}'" if team_filter else "") + ".")
    return "\n".join(out)


# what each audit category means, for the CSV and the Discord message.
# Priority 1 = a real problem, 2 = needs a look, 3 = almost certainly fine.
AUDIT = {
    "NOT ON ROSTER": (1, "This account isn't on the Roster Sheet, and nobody on the team's roster "
                         "looks like it. An unrostered sub or ringer until someone says otherwise."),
    "NOT ON ROSTER - PROBABLY": (2, "This account isn't on the Roster Sheet, but its name looks like the "
                                    "roster player shown under 'Probably', and that player wasn't in "
                                    "these games - so it's very likely them on an account the roster "
                                    "doesn't list. Ask for this tracker."),
    "WRONG TEAM": (1, "This account is rostered to another team."),
    "ONE ACCOUNT, TWO TEAMS": (1, "The same account played for two different teams. Check "
                                  "both series - a ringer, or a game recorded on the wrong side."),
    "AMBIGUOUS": (2, "The name matches more than one roster entry - add this tracker to the right one."),
    "DIFFERENT NAME - CHECK": (2, "Tied to the roster player shown because this account filled their "
                                  "spot before, but the names don't match. A renamed account, or "
                                  "someone else playing on their spot?"),
    "SECOND STEAM ACCOUNT?": (2, "The roster has this player's Steam account, but they played on a "
                                 "different Steam account with the same name. A second account - or a "
                                 "wrong link on the sheet."),
    "EPIC NAME DOESN'T MATCH LINK": (2, "Played on Epic as the roster player, but the roster's Epic link "
                                        "names a different account. Either they renamed (fine - update "
                                        "the link) or it's a second account."),
    "SAME NAME, OTHER ACCOUNT": (3, "Same name as the roster player, but the roster lists a different "
                                    "account (usually their Epic while they play on Steam). Almost "
                                    "certainly them - ask for this tracker so it's on file."),
    "CONSOLE - EPIC ON ROSTER": (3, "A PlayStation/Xbox player whose roster link is their Epic (as "
                                    "asked). Replays only show the console account, so the two can't "
                                    "be tied automatically. Fine unless it isn't them."),
}
CONSOLE = {"ps4", "xbox"}


def _nice_date(d: str) -> str:
    """'2026-09-25 20-19-46' or '2026-10-03T00:07:52Z' -> '2026-09-25 20:19'."""
    import re as _re
    m = _re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2})[-:](\d{2})", d or "")
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else (d or "")


def _xl(pid: str) -> str:
    """A long numeric id as ="..." so Excel/Sheets don't show it as 7.66E+16."""
    s = str(pid or "")
    return f'="{s}"' if s.isdigit() and len(s) >= 12 else s


def _clean(game_stats, roster):
    from .cleanup import clean_stats
    return clean_stats(game_stats, roster)[0]


def audit(game_stats: List[List[str]], game_scores: List[List[str]], fixtures: Dict[str, dict],
          roster, state: Dict[str, dict] = None):
    """
    A full record for the commissioner: every account that played for a team
    without being that exact account on the Roster Sheet - plus every game it
    played. Exact matches (Steam id, or the account a link names) are left
    out; everything else is in, sorted by how much it matters (see AUDIT).
    Spectators and duplicate lines are not counted.

    Returns (accounts_csv, games_csv, accounts, games, counts by category).
    """
    import csv as _csv
    import io as _io
    from urllib.parse import quote
    from .cleanup import clean_scores
    state = state or {}
    game_stats = _clean(game_stats, roster)
    game_scores, _ = clean_scores(game_scores)
    try:
        with open(os.path.join(DATA, "conferences.json"), encoding="utf-8") as f:
            conf = json.load(f)
    except (OSError, ValueError):
        conf = {}

    # game scores by replay id
    gscore = {}
    for v in game_scores[1:]:
        v = list(v) + [""] * (14 - len(v))
        if v[2]:
            gscore[v[2]] = v

    def classify(plat, pid, name, team):
        p = plat.lower()
        rec = roster.by_identity.get((p, norm(pid)))
        if rec is not None:             # exact steam id, or the account a link names
            return ("OK", rec) if rec["team"] == team else ("WRONG TEAM", rec)
        rec = roster._learned_for(p, pid, {team})
        how = "remembered"
        if rec is None:
            c = roster._name_candidates(p, name, {team})
            if len(c) == 1:
                rec, how = c[0], "name"
            elif len(c) > 1:
                return "AMBIGUOUS", None
        if rec is None and p == "steam":
            # a same-named player whose roster link is a different steam id
            twins = [r for r in roster.teams.get(team, [])
                     if r.get("exact") and r.get("platform") == "steam" and _similar(name, r["name"])]
            if len(twins) == 1:
                return "SECOND STEAM ACCOUNT?", twins[0]
        if rec is None:
            return "NOT ON ROSTER", None
        if rec["team"] != team:
            return "WRONG TEAM", rec
        rp = rec.get("platform", "")
        if rp == p and norm(rec.get("id", "")) == norm(name):
            return "OK", rec            # epic/psn link that names this exact account
        if (p in CONSOLE and rp == "epic") or (p == "epic" and rp in CONSOLE):
            return "CONSOLE - EPIC ON ROSTER", rec
        if rp == p == "steam" and rec.get("exact"):
            return "SECOND STEAM ACCOUNT?", rec
        if rp == p == "epic":
            return "EPIC NAME DOESN'T MATCH LINK", rec
        if how == "name" or _similar(name, rec.get("name", "")) or \
                (not rec.get("exact") and _similar(name, rec.get("id", ""))):
            return "SAME NAME, OTHER ACCOUNT", rec
        return "DIFFERENT NAME - CHECK", rec

    def tracker(plat, pid, name):
        p = plat.lower()
        if p == "steam" and pid:
            return tracker_url("steam", pid)
        if p in ("epic", "ps4", "xbox") and name:
            return tracker_url(p, quote(name, safe=""))   # tracker uses the display name
        return ""

    def ballchasing(plat, pid):
        return f"https://ballchasing.com/player/{plat.lower()}/{pid}" if pid else ""

    def closest(team, names):
        best = []
        for r in roster.teams.get(team, []):
            for n in names:
                for k in (r["name"], "" if r.get("exact") else r.get("id", "")):
                    a, b = norm(n), norm(k)
                    if not a or not b:
                        continue
                    sc = difflib.SequenceMatcher(None, a, b).ratio()
                    if min(len(a), len(b)) >= 3 and (a in b or b in a):
                        sc = max(sc, 0.75)
                    if sc >= 0.6:
                        best.append((sc, r["name"]))
        best.sort(reverse=True)
        out = []
        for _, n in best:
            if n not in out:
                out.append(n)
        return out[:2]

    # pass 1: which rostered players of each team were in each game
    in_game = defaultdict(set)          # (replay, team) -> {roster name}
    rows = []
    for v in game_stats[1:]:
        fid, rid, team, name, plat, pid = v[0], v[2], v[5], v[7], v[9], v[10]
        if not fid or not team or not rid:
            continue
        if not any(_n(v[i]) > 0 for i in (12, 13, 14, 15, 16)):
            continue                                    # spectator
        status, rec = classify(plat, pid, name, team)
        if rec is not None and rec["team"] == team:
            in_game[(rid, team)].add(rec["name"])
        rows.append((v, status, rec))

    accounts = {}
    teams_of = defaultdict(set)        # account -> teams it played for
    for v, status, rec in rows:
        fid, week, rid, gno, date, team, won, name = v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]
        plat, pid = v[9], v[10]
        acct = f"{plat.lower()}:{pid}" if pid else f"name:{norm(name)}"
        teams_of[acct].add(team)
        if status == "OK":
            continue
        fx = fixtures.get(fid, {})
        opp = fx.get("away") if fx.get("home") == team else fx.get("home", "")
        res = (state.get(fid) or {}).get("result") or {}
        sc = gscore.get(rid)
        if sc:
            score = f"{sc[5]} {sc[6]}-{sc[8]} {sc[7]}" + (" (OT)" if str(sc[13]).upper() in ("TRUE", "1") else "")
        else:
            score = ""
        a = accounts.setdefault((acct, team), {
            "team": team, "status": status, "plat": plat, "pid": pid, "names": [],
            "rec": rec, "games": set(), "series": set(), "first": "", "last": "",
            "tracker": tracker(plat, pid, name), "bc": ballchasing(plat, pid), "log": []})
        if name and name not in a["names"]:
            a["names"].append(name)
        # an account first seen under a tag ("king poofles") and later under
        # its roster name ("poofles") takes the more specific answer
        if a["rec"] is None and rec is not None:
            a["status"], a["rec"] = status, rec
        a["games"].add(rid)
        a["series"].add(fid)
        d = _nice_date(date)
        if d and (not a["first"] or d < a["first"]):
            a["first"] = d
        if d and (not a["last"] or d > a["last"]):
            a["last"] = d
        a["log"].append([name, d, fx.get("week", week), opp, gno, score,
                         "W" if str(won).strip().upper() in ("1", "TRUE") else "L",
                         v[12], v[13], v[14], v[15], v[16], fid, res.get("link", "")])

    # an account seen on two teams outranks whatever else it is
    for (acct, team), a in accounts.items():
        if len(teams_of[acct]) > 1:
            a["status"] = "ONE ACCOUNT, TWO TEAMS"
            a["other"] = ", ".join(sorted(teams_of[acct] - {team}))

    acc_rows, games = [], []
    counts = defaultdict(int)
    for (acct, team), a in accounts.items():
        rec = a["rec"]
        roster_names = [r["name"] for r in roster.teams.get(team, [])]
        seen_names = set()
        for rid in a["games"]:
            seen_names |= in_game[(rid, team)]
        missing = [n for n in dict.fromkeys(roster_names) if n not in seen_names]
        hint = ""
        if a["status"] == "NOT ON ROSTER":
            guesses = closest(team, a["names"])
            # a lookalike who was absent from every one of these games is
            # almost certainly the same person on another account
            likely = [g for g in guesses if g in missing]
            if likely:
                a["status"] = "NOT ON ROSTER - PROBABLY"
                hint = likely[0]
            elif guesses:
                hint = f"(not {', '.join(guesses)} - they played in these games too)"
        elif a["status"] == "ONE ACCOUNT, TWO TEAMS":
            hint = "also played for " + a["other"]
        prio, meaning = AUDIT.get(a["status"], (2, ""))
        counts[a["status"]] += 1
        acc_rows.append([
            prio, a["status"], team, conf.get(team, ""), ", ".join(a["names"]), a["plat"],
            _xl(a["pid"]), a["tracker"], a["bc"],
            (f"{rec['name']} ({rec['team']})" if rec else ""),
            (_link(rec) if rec else ""),
            hint, ", ".join(missing),
            len(a["games"]), len(a["series"]), a["first"], a["last"], meaning])
        for g in a["log"]:
            games.append([prio, a["status"], team, conf.get(team, ""), g[0], a["plat"], _xl(a["pid"]),
                          a["tracker"], a["bc"], *g[1:]])
    acc_rows.sort(key=lambda r: (r[0], r[2], r[4].lower()))
    games.sort(key=lambda r: (r[0], r[2], r[4].lower(), r[9]))

    def to_csv(header, rows):
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return "\ufeff" + buf.getvalue()      # BOM so Excel shows names correctly

    acc_csv = to_csv(["Priority", "Status", "Team", "Conference", "In-game name(s)", "Platform",
                      "Account ID", "Tracker", "Ballchasing", "Roster says", "Roster link",
                      "Probably", "Rostered players missing from those games",
                      "Games", "Series", "First game", "Last game", "What it means"], acc_rows)
    games_csv = to_csv(["Priority", "Status", "Team", "Conference", "In-game name", "Platform",
                        "Account ID", "Tracker", "Ballchasing", "Date", "Week", "Opponent", "Game #",
                        "Game score", "W/L", "Score", "Goals", "Assists", "Saves", "Shots",
                        "Series ID", "Results post"], games)
    return acc_csv, games_csv, len(acc_rows), len(games), dict(counts)


def compare(acc_csv: str, last: Dict[str, list] = None):
    """
    What changed since the previous /accountaudit.

    Returns (snapshot to save, changes text, fixed, new) - fixed and new are
    lists of [team, in-game names, platform, status].
    An account counts as fixed when it was on the last audit and isn't now
    - its roster link (or name) was corrected, so it matches exactly.
    """
    import csv as _csv
    rows = list(_csv.reader(acc_csv.lstrip("\ufeff").splitlines()))
    now = {}
    for r in rows[1:]:
        if len(r) < 7:
            continue
        pid = r[6].strip('="')
        now[f"{r[2]}|{r[5]}|{pid}"] = [r[2], r[4], r[5], r[1]]
    if last is None:
        return now, "", [], []
    fixed = sorted((v for k, v in last.items() if k not in now), key=lambda v: (v[0], v[1].lower()))
    new = sorted((v for k, v in now.items() if k not in last), key=lambda v: (v[0], v[1].lower()))
    moved = sorted(((last[k], v) for k, v in now.items() if k in last and last[k][3] != v[3]),
                   key=lambda x: (x[1][0], x[1][1].lower()))
    out = [f"Changes since the last account audit",
           f"Fixed: {len(fixed)}   New: {len(new)}   Status changed: {len(moved)}   "
           f"Still open: {len(now) - len(new)}", ""]
    out.append(f"FIXED ({len(fixed)}) - now match the roster exactly")
    out += [f"  {v[0]} - {v[1]} ({v[2]})   was: {v[3]}" for v in fixed] or ["  none"]
    out += ["", f"NEW ({len(new)}) - not on the last audit (new games, or a roster edit broke a match)"]
    out += [f"  {v[0]} - {v[1]} ({v[2]})   {v[3]}" for v in new] or ["  none"]
    out += ["", f"STATUS CHANGED ({len(moved)})"]
    out += [f"  {v[0]} - {v[1]} ({v[2]})   {a[3]} -> {v[3]}" for a, v in moved] or ["  none"]
    return now, "\n".join(out) + "\n", fixed, new


def compare_with_last(acc_csv: str):
    """compare() against data/audit_last.json, then save this run as the new last one."""
    path = os.path.join(DATA, "audit_last.json")
    try:
        with open(path, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = None
    snap, changes, fixed, new = compare(acc_csv, last)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=0)
    except OSError:
        pass
    return last is not None, changes, fixed, new


def summary_lines(counts: Dict[str, int]) -> List[str]:
    lines = []
    for prio, label in ((1, "real problems"), (2, "need a look"), (3, "almost certainly fine")):
        part = [f"{counts[k]} {k.lower()}" for k, (p, _) in AUDIT.items() if p == prio and counts.get(k)]
        if part:
            lines.append(f"**{prio} - {label}:** " + " · ".join(part))
    return lines


def team_rows(acc_csv: str, team: str) -> List[List[str]]:
    """The audit's account rows for one team: [priority, status, names, platform, probably/roster]."""
    import csv as _csv
    rows = list(_csv.reader(acc_csv.lstrip("\ufeff").splitlines()))
    out = []
    for r in rows[1:]:
        if len(r) >= 12 and r[2] == team:
            who = r[11] if r[1].startswith("NOT ON ROSTER") else (r[9].rsplit(" (", 1)[0] if r[9] else r[11])
            out.append([r[0], r[1], r[4], r[5], who, r[7]])
    return out


def missing_stats(state: Dict[str, dict], fixtures: Dict[str, dict],
                  game_stats: List[List[str]], game_scores: List[List[str]]) -> List[tuple]:
    """
    Results the bot has as reported but with no stat lines or no game scores
    on the sheet - a write that failed (Google rate limit, say). Forfeits are
    left out. Returns [(fixture, result link, has stats, has scores)].
    """
    with_stats = {v[0] for v in game_stats[1:] if v and v[0]}
    with_scores = {v[0] for v in game_scores[1:] if v and v[0]}
    out = []
    for fid, st in state.items():
        res = st.get("result") or {}
        fx = fixtures.get(fid)
        if not fx or st.get("status") not in ("reported", "archived") or not res or res.get("forfeit"):
            continue
        hs, hc = fid in with_stats, fid in with_scores
        if not (hs and hc):
            out.append((fx, res.get("link", ""), hs, hc))
    out.sort(key=lambda x: (x[0]["week"], x[0]["home"]))
    return out
