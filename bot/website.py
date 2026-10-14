"""
The CCS Season 9 Website Stats sheet.

A separate Google Sheet the website team pulls from: plain tables, one header
row, one record per row, no formulas or merged cells.

It is rebuilt in full from the bot's own records every time it syncs:
  results    state.json (every reported series)
  games      the Game Scores tab
  players    the Game Stats tab (already roster-labelled, so /stats relabel
             fixes flow straight through)
So past games are included automatically, a /clear removes a series from the
website on the next sync, and nothing can be double-counted.

A sync runs ~90 seconds after any result is recorded (results that land
together share one sync), after /stats relabel, and on demand with
/stats action:website.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
SEASON = 9
TOP_N = 20
DEFAULT_SHEET = "1r9hW82xMx6dTq4fWnUVM75w0-vmU5Ji116Mot7ME8OE"

HEADERS = {
    "Series": ["series_id", "season", "week", "match_type", "reported_at",
               "home_team", "home_conference", "away_team", "away_conference",
               "home_wins", "away_wins", "winner", "loser", "games_with_stats",
               "link", "reported_by"],
    "Games": ["game_id", "series_id", "season", "week", "game_number", "date",
              "winner", "loser", "winner_goals", "loser_goals", "overtime", "replay_id"],
    "PlayerGames": ["series_id", "game_id", "season", "week", "game_number", "match_type",
                    "player", "player_id", "tracker", "team", "conference", "opponent",
                    "opponent_conference", "win", "score", "goals", "assists", "saves",
                    "shots", "mvp"],
    "TeamGames": ["series_id", "game_id", "season", "week", "game_number", "match_type",
                  "team", "conference", "opponent", "opponent_conference", "win",
                  "goals", "goals_against", "assists", "saves", "shots", "shots_against",
                  "score"],
    "PlayerTotals": ["player", "player_id", "tracker", "team", "conference", "gp",
                     "team_gp", "wins", "losses", "win_pct", "goals", "assists", "saves",
                     "shots", "score", "mvps", "shooting_pct", "goals_pg", "assists_pg",
                     "saves_pg", "shots_pg", "score_pg", "qualified"],
    "TeamTotals": ["team", "conference", "series_played", "series_wins", "series_losses",
                   "gp", "game_wins", "game_losses", "goals", "goals_against", "goal_diff",
                   "assists", "saves", "shots", "shots_against", "shooting_pct",
                   "goals_pg", "goals_against_pg", "assists_pg", "saves_pg", "shots_pg",
                   "shots_against_pg"],
    "Leaders": ["scope_type", "scope", "category", "rank", "player", "player_id",
                "team", "conference", "gp", "value"],
    "Meta": ["key", "value"],
}
TAB_ORDER = ["README", "Series", "Games", "PlayerGames", "TeamGames",
             "PlayerTotals", "TeamTotals", "Leaders", "Meta"]

LEADER_CATEGORIES = [   # (column, is a rate stat needing the games-played minimum)
    ("goals", False), ("assists", False), ("saves", False), ("shots", False),
    ("score", False), ("shooting_pct", True), ("goals_pg", True), ("assists_pg", True),
    ("saves_pg", True), ("shots_pg", True), ("score_pg", True),
]


def _readme(min_share: float) -> List[List[str]]:
    pct = int(round(min_share * 100))
    return [
        ["CCS Season 9 - Website Stats", ""],
        ["", ""],
        ["How it works", "The CCS bot rebuilds every tab here from its own records about a "
                         "minute after each result is reported. Do not edit these tabs by hand."],
        ["Format", "Every data tab: row 1 = column names, one record per row, no merged cells "
                   "or formulas. Pull any tab directly."],
        ["", ""],
        ["TAB", "WHAT IT IS"],
        ["Series", "One row per reported series: teams, conferences, series score, winner, "
                   "week, conference / non-conference, replay link."],
        ["Games", "One row per game: winner, loser, goals, OT."],
        ["PlayerGames", "One row per player per game: score, goals, assists, saves, shots, MVP. "
                        "Filter by series_id or game_id for box scores."],
        ["TeamGames", "One row per team per game, incl. goals and shots against."],
        ["PlayerTotals", "Season totals, per-game averages, shooting % and win % per player "
                         "(per team - a transfer shows as two rows)."],
        ["TeamTotals", "Season team totals: series W-L, game W-L, goal diff, per-game stats."],
        ["Leaders", f"Leaderboards in one table. scope_type = Overall | Conference | Team; "
                    f"scope = 'Overall', a conference name, or a team name. Top {TOP_N} for "
                    f"Overall and Conference, whole roster for Team. Ties share a rank."],
        ["Meta", "last_updated, season, latest_week, counts."],
        ["", ""],
        ["Categories", ", ".join(c for c, _ in LEADER_CATEGORIES)],
        ["Qualifying", f"Per-game and shooting % boards (Overall / Conference) need at least "
                       f"{pct}% of the team's games played (the 'qualified' column)."],
        ["Keys", "series_id links Series -> Games -> PlayerGames / TeamGames. game_id = "
                 "series_id + '-G#'. player_id = platform:id, stable through name changes."],
        ["Times", "Eastern time."],
    ]


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _truthy(v) -> int:
    return 1 if str(v).strip().upper() in ("1", "1.0", "TRUE", "YES") else 0


def _conferences() -> Dict[str, str]:
    try:
        with open(os.path.join(DATA, "conferences.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _match_type(fx: dict) -> str:
    kind = (fx.get("kind") or "").lower()
    if kind.startswith("conf") or str(fx.get("id", "")).startswith("c-"):
        return "Conference"
    if "play" in kind:
        return "Playoffs"
    return "Non-Conference"


def build(store, game_stats: List[List[str]], game_scores: List[List[str]]) -> Dict[str, list]:
    """
    Every tab's rows (header first), from state.json plus the bot's Game
    Stats and Game Scores tabs as read off the sheet (header row included).
    Pure - no Google calls - so it can be tested on its own.
    """
    from .cleanup import clean_stats, clean_scores
    game_stats, _, _ = clean_stats(game_stats)
    game_scores, _ = clean_scores(game_scores)
    conf = _conferences()
    min_share = float(store.cfg("rate_minimum_share", 0.25))
    fixtures = {f["id"]: f for f in store.fixtures}

    # ---- series: every reported result
    series, reported = [], {}
    for fid, st in store.state.items():
        res = st.get("result") or {}
        fx = fixtures.get(fid)
        if st.get("status") not in ("reported", "archived") or not res or not fx:
            continue
        reported[fid] = (fx, st)
    weeks_seen = []

    # ---- games
    games_rows = [HEADERS["Games"]]
    for v in game_scores[1:]:
        v = list(v) + [""] * (14 - len(v))
        fid = v[0]
        if fid not in reported:
            continue
        games_rows.append([f"{fid}-G{v[3]}", fid, SEASON, int(_num(v[1])), int(_num(v[3])),
                           v[4], v[5], v[7], int(_num(v[6])), int(_num(v[8])),
                           _truthy(v[13]), v[2]])

    # ---- player games
    pg_rows = [HEADERS["PlayerGames"]]
    per_team_game = defaultdict(lambda: defaultdict(float))   # (fid, game, team) -> totals
    stats_games = defaultdict(set)
    for v in game_stats[1:]:
        v = list(v) + [""] * (20 - len(v))
        fid = v[0]
        if fid not in reported or not v[2]:
            continue
        fx, st = reported[fid]
        team = v[5]
        if team not in (fx["home"], fx["away"]):
            continue
        if not any(_num(v[i]) > 0 for i in (12, 13, 14, 15, 16)):
            continue            # spectator rows written before the bot skipped them
        opp = fx["away"] if team == fx["home"] else fx["home"]
        game = int(_num(v[3]))
        mt = _match_type(fx)
        pid = f"{(v[9] or '').lower()}:{v[10]}" if v[10] else f"name:{v[7]}"
        win = _truthy(v[6])
        vals = {k: int(_num(v[i])) for k, i in
                (("score", 12), ("goals", 13), ("assists", 14), ("saves", 15), ("shots", 16))}
        pg_rows.append([fid, f"{fid}-G{game}", SEASON, fx["week"], game, mt,
                        v[8] or v[7], pid, v[11], team, conf.get(team, ""), opp,
                        conf.get(opp, ""), win, vals["score"], vals["goals"],
                        vals["assists"], vals["saves"], vals["shots"], _truthy(v[17])])
        t = per_team_game[(fid, game, team)]
        t["win"] = max(t["win"], win)
        for k, x in vals.items():
            t[k] += x
        stats_games[fid].add(v[2])

    # ---- team games (needs both sides to fill in the "against" columns)
    tg_rows = [HEADERS["TeamGames"]]
    for (fid, game, team), t in sorted(per_team_game.items()):
        fx, _ = reported[fid]
        opp = fx["away"] if team == fx["home"] else fx["home"]
        o = per_team_game.get((fid, game, opp), {})
        tg_rows.append([fid, f"{fid}-G{game}", SEASON, fx["week"], game, _match_type(fx),
                        team, conf.get(team, ""), opp, conf.get(opp, ""), int(t["win"]),
                        int(t["goals"]), int(o.get("goals", 0)), int(t["assists"]),
                        int(t["saves"]), int(t["shots"]), int(o.get("shots", 0)),
                        int(t["score"])])

    series_rows = [HEADERS["Series"]]
    for fid, (fx, st) in sorted(reported.items(), key=lambda kv: (kv[1][0]["week"], kv[0])):
        res = st["result"]
        weeks_seen.append(fx["week"])
        series_rows.append([fid, SEASON, fx["week"], _match_type(fx),
                            (st.get("reported_at") or "")[:19].replace("T", " "),
                            fx["home"], conf.get(fx["home"], ""), fx["away"],
                            conf.get(fx["away"], ""), res.get("home", ""), res.get("away", ""),
                            res.get("winner", ""), res.get("loser", ""),
                            len(stats_games.get(fid, ())), res.get("link", ""),
                            res.get("by_name", "")])

    # ---- team totals
    T = defaultdict(lambda: defaultdict(float))
    for r in tg_rows[1:]:
        d = T[r[6]]
        d["gp"] += 1
        d["game_wins"] += r[10]
        for k, i in (("goals", 11), ("goals_against", 12), ("assists", 13), ("saves", 14),
                     ("shots", 15), ("shots_against", 16)):
            d[k] += r[i]
    for r in series_rows[1:]:
        for team in (r[5], r[7]):
            T[team]["series_played"] += 1
            T[team]["series_wins"] += 1 if r[11] == team else 0
    team_rows = [HEADERS["TeamTotals"]]
    team_recs = []
    for team, d in T.items():
        gp = d["gp"] or 1
        team_recs.append([
            team, conf.get(team, ""), int(d["series_played"]), int(d["series_wins"]),
            int(d["series_played"] - d["series_wins"]), int(d["gp"]), int(d["game_wins"]),
            int(d["gp"] - d["game_wins"]), int(d["goals"]), int(d["goals_against"]),
            int(d["goals"] - d["goals_against"]), int(d["assists"]), int(d["saves"]),
            int(d["shots"]), int(d["shots_against"]),
            round(100 * d["goals"] / d["shots"], 1) if d["shots"] else 0.0,
            round(d["goals"] / gp, 2), round(d["goals_against"] / gp, 2),
            round(d["assists"] / gp, 2), round(d["saves"] / gp, 2),
            round(d["shots"] / gp, 2), round(d["shots_against"] / gp, 2)])
    team_recs.sort(key=lambda x: (x[1], -x[3], -x[10], x[0]))
    team_rows += team_recs
    team_gp = {r[0]: r[5] for r in team_recs}

    # ---- player totals
    P = {}
    for r in pg_rows[1:]:
        key = (r[7], r[9])
        d = P.setdefault(key, defaultdict(float))
        d["_name"], d["_tracker"], d["_conf"] = r[6], r[8] or d.get("_tracker", ""), r[10]
        d["gp"] += 1
        d["wins"] += r[13]
        for k, i in (("score", 14), ("goals", 15), ("assists", 16), ("saves", 17),
                     ("shots", 18), ("mvps", 19)):
            d[k] += r[i]
    recs = []
    for (pid, team), d in P.items():
        gp = d["gp"]
        tgp = team_gp.get(team, 0)
        recs.append({
            "player": d["_name"], "player_id": pid, "tracker": d["_tracker"], "team": team,
            "conference": d["_conf"], "gp": int(gp), "team_gp": int(tgp),
            "wins": int(d["wins"]), "losses": int(gp - d["wins"]),
            "win_pct": round(100 * d["wins"] / gp, 1),
            "goals": int(d["goals"]), "assists": int(d["assists"]), "saves": int(d["saves"]),
            "shots": int(d["shots"]), "score": int(d["score"]), "mvps": int(d["mvps"]),
            "shooting_pct": round(100 * d["goals"] / d["shots"], 1) if d["shots"] else 0.0,
            "goals_pg": round(d["goals"] / gp, 2), "assists_pg": round(d["assists"] / gp, 2),
            "saves_pg": round(d["saves"] / gp, 2), "shots_pg": round(d["shots"] / gp, 2),
            "score_pg": round(d["score"] / gp, 1),
            "qualified": int(tgp > 0 and gp >= min_share * tgp),
        })
    recs.sort(key=lambda x: (x["conference"], x["team"], x["player"].lower()))
    player_rows = [HEADERS["PlayerTotals"]] + [[x[c] for c in HEADERS["PlayerTotals"]] for x in recs]

    # ---- leaders
    leader_rows = [HEADERS["Leaders"]]
    by_conf, by_team = defaultdict(list), defaultdict(list)
    for x in recs:
        by_conf[x["conference"]].append(x)
        by_team[x["team"]].append(x)
    scopes = [("Overall", "Overall", recs, TOP_N, True)]
    scopes += [("Conference", c, by_conf[c], TOP_N, True) for c in sorted(by_conf) if c]
    scopes += [("Team", t, by_team[t], None, False) for t in sorted(by_team)]
    for stype, scope, pool_all, limit, need_q in scopes:
        for cat, is_rate in LEADER_CATEGORIES:
            pool = [x for x in pool_all if not (is_rate and need_q and not x["qualified"])]
            pool.sort(key=lambda x: (-x[cat], x["gp"], x["player"].lower()))
            rank, prev = 0, None
            for i, x in enumerate(pool):
                if x[cat] != prev:
                    rank, prev = i + 1, x[cat]
                if limit and rank > limit:
                    break
                leader_rows.append([stype, scope, cat, rank, x["player"], x["player_id"],
                                    x["team"], x["conference"], x["gp"], x[cat]])

    now = datetime.now(ZoneInfo(store.cfg("timezone", "America/New_York")))
    meta_rows = [HEADERS["Meta"],
                 ["last_updated", now.strftime("%Y-%m-%d %H:%M:%S ET")],
                 ["season", SEASON],
                 ["latest_week", max(weeks_seen) if weeks_seen else 0],
                 ["series_count", len(series_rows) - 1],
                 ["series_with_stats", sum(1 for r in series_rows[1:] if r[13])],
                 ["game_count", len(games_rows) - 1],
                 ["player_count", len(player_rows) - 1],
                 ["team_count", len(team_rows) - 1],
                 ["top_n", TOP_N],
                 ["min_share_of_team_games", min_share]]

    return {"README": _readme(min_share), "Series": series_rows, "Games": games_rows,
            "PlayerGames": pg_rows, "TeamGames": tg_rows, "PlayerTotals": player_rows,
            "TeamTotals": team_rows, "Leaders": leader_rows, "Meta": meta_rows}


def push(gc, sheet_id: str, tabs: Dict[str, list]) -> Tuple[bool, str]:
    """Write every tab: make/resize it as needed, clear it, then one write per tab."""
    try:
        ss = gc.open_by_key(sheet_id)
    except Exception as e:
        return False, (f"couldn't open the website sheet ({type(e).__name__}) - share it "
                       f"with the bot's service account as Editor")
    existing = {ws.title: ws for ws in ss.worksheets()}
    for name in TAB_ORDER:
        rows = tabs[name]
        width = max(len(r) for r in rows)
        need_rows = len(rows) + 200
        ws = existing.get(name)
        if ws is None:
            ws = ss.add_worksheet(name, rows=need_rows, cols=width)
        elif ws.row_count < need_rows or ws.col_count < width:
            ws.resize(rows=max(ws.row_count, need_rows), cols=max(ws.col_count, width))
        ws.batch_clear(["A:ZZ"])
        ws.update(range_name="A1", values=rows, value_input_option="RAW")
    return True, (f"{len(tabs['Series']) - 1} series, {len(tabs['Games']) - 1} games, "
                  f"{len(tabs['PlayerTotals']) - 1} players")
