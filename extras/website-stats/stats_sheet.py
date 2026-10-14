"""
stats_sheet.py - writes every /report series to the CCS website stats sheet.

The sheet is a set of plain tables (one header row, one record per row) so the
website team can pull any tab straight into a database / API.

  Raw tabs (bot appends on every /report):
    Series       one row per series
    Games        one row per game
    PlayerGames  one row per player per game
    TeamGames    one row per team per game

  Built tabs (fully rewritten after every /report, never edit by hand):
    PlayerTotals season totals + per-game + shooting % per player
    TeamTotals   season totals + per-game per team
    Leaders      top 20 per category: Overall, per Conference, and per Team
    Meta         last updated, season, latest week, counts

Usage from the /report handler (run off the event loop - gspread is blocking):

    from stats_sheet import StatsSheet, SeriesReport, GameReport, PlayerLine

    stats = StatsSheet.from_service_account("service_account.json", STATS_SHEET_ID)
    report = SeriesReport(week=3, match_type="Non-Conference",
                          team_a="Cincinnati Bearcats", team_a_conf="Big 12",
                          team_b="Xavier Musketeers",   team_b_conf="Big East",
                          games=[GameReport(...), ...],
                          ballchasing_url="https://ballchasing.com/group/...",
                          reported_by="someone#0001")
    await asyncio.to_thread(stats.record_series, report)

Re-reporting the same series (same series_id) replaces its old rows, so
corrections and disputes don't double-count.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SEASON = 9
# "CCS Season 9 Website Stats" in Bennett's Drive. Share it (Editor) with the
# bot's service-account email, and (Viewer) with the website team.
STATS_SHEET_ID = "1r9hW82xMx6dTq4fWnUVM75w0-vmU5Ji116Mot7ME8OE"
TOP_N = 20                 # leaderboard depth for Overall and Conference scopes
MIN_GP_RATE_STATS = 5      # games needed to qualify for per-game / shooting % boards
EASTERN = ZoneInfo("America/New_York")

# Leaderboard categories: (column in PlayerTotals, is_rate_stat)
LEADER_CATEGORIES = [
    ("goals", False),
    ("assists", False),
    ("saves", False),
    ("shots", False),
    ("score", False),
    ("shooting_pct", True),
    ("goals_pg", True),
    ("assists_pg", True),
    ("saves_pg", True),
    ("shots_pg", True),
    ("score_pg", True),
]

# ----------------------------------------------------------------------------
# Tab layouts (header rows). The website team builds against these names.
# ----------------------------------------------------------------------------
HEADERS = {
    "Series": [
        "series_id", "season", "week", "match_type", "reported_at",
        "team_a", "team_a_conference", "team_b", "team_b_conference",
        "team_a_wins", "team_b_wins", "winner", "loser", "games_played",
        "ballchasing_url", "reported_by",
    ],
    "Games": [
        "game_id", "series_id", "season", "week", "game_number",
        "blue_team", "orange_team", "blue_goals", "orange_goals",
        "winner", "overtime", "duration_sec", "map", "replay_id",
    ],
    "PlayerGames": [
        "series_id", "game_id", "season", "week", "game_number", "match_type",
        "player", "player_id", "team", "conference", "opponent",
        "opponent_conference", "win", "score", "goals", "assists", "saves",
        "shots", "demos", "mvp",
    ],
    "TeamGames": [
        "series_id", "game_id", "season", "week", "game_number", "match_type",
        "team", "conference", "opponent", "opponent_conference", "win",
        "goals", "goals_against", "assists", "saves", "shots", "shots_against",
        "demos", "score",
    ],
    "PlayerTotals": [
        "player", "player_id", "team", "conference", "gp", "wins", "losses",
        "goals", "assists", "saves", "shots", "score", "demos", "mvps",
        "shooting_pct", "goals_pg", "assists_pg", "saves_pg", "shots_pg",
        "score_pg", "demos_pg",
    ],
    "TeamTotals": [
        "team", "conference", "series_played", "series_wins", "series_losses",
        "gp", "game_wins", "game_losses", "goals", "goals_against", "goal_diff",
        "assists", "saves", "shots", "shots_against", "demos", "shooting_pct",
        "goals_pg", "goals_against_pg", "assists_pg", "saves_pg", "shots_pg",
        "shots_against_pg",
    ],
    "Leaders": [
        "scope_type", "scope", "category", "rank", "player", "player_id",
        "team", "conference", "gp", "value",
    ],
    "Meta": ["key", "value"],
}
RAW_TABS = ["Series", "Games", "PlayerGames", "TeamGames"]
BUILT_TABS = ["PlayerTotals", "TeamTotals", "Leaders", "Meta"]


# ----------------------------------------------------------------------------
# Input data - fill these from whatever the bot already parses from replays
# ----------------------------------------------------------------------------
@dataclass
class PlayerLine:
    name: str
    team: str                       # must equal team_a or team_b of the series
    goals: int = 0
    assists: int = 0
    saves: int = 0
    shots: int = 0
    score: int = 0
    demos: int = 0
    mvp: bool = False
    player_id: str = ""             # platform:id (e.g. "steam:7656..."), falls back to name


@dataclass
class GameReport:
    game_number: int
    players: list[PlayerLine]
    blue_team: str = ""             # optional; team_a assumed blue if blank
    overtime: bool = False
    duration_sec: Optional[int] = None
    map: str = ""
    replay_id: str = ""             # ballchasing replay id if you have it


@dataclass
class SeriesReport:
    week: int
    match_type: str                 # "Conference" / "Non-Conference" / "Playoffs" ...
    team_a: str
    team_a_conf: str
    team_b: str
    team_b_conf: str
    games: list[GameReport]
    ballchasing_url: str = ""
    reported_by: str = ""
    series_id: str = ""             # leave blank to auto-build from season/week/teams
    reported_at: Optional[datetime] = None


# ----------------------------------------------------------------------------
# Sheet backends
# ----------------------------------------------------------------------------
class GspreadBackend:
    """Thin wrapper so the logic can be tested without Google."""

    def __init__(self, spreadsheet):
        self.ss = spreadsheet
        self._ws = {}

    def _sheet(self, tab):
        if tab not in self._ws:
            try:
                self._ws[tab] = self.ss.worksheet(tab)
            except Exception:  # gspread.WorksheetNotFound
                self._ws[tab] = self.ss.add_worksheet(tab, rows=1000, cols=len(HEADERS[tab]))
        return self._ws[tab]

    def read(self, tab) -> list[list[str]]:
        return self._sheet(tab).get_all_values()

    def append(self, tab, rows):
        if rows:
            self._sheet(tab).append_rows(rows, value_input_option="RAW")

    def overwrite(self, tab, rows):
        ws = self._sheet(tab)
        ws.clear()
        need = max(len(rows) + 50, 100)
        if ws.row_count < need:
            ws.resize(rows=need)
        ws.update(range_name="A1", values=rows, value_input_option="RAW")


class MemoryBackend:
    def __init__(self):
        self.tabs = {t: [list(h)] for t, h in HEADERS.items()}

    def read(self, tab):
        return [list(r) for r in self.tabs[tab]]

    def append(self, tab, rows):
        self.tabs[tab].extend([list(r) for r in rows])

    def overwrite(self, tab, rows):
        self.tabs[tab] = [list(r) for r in rows]


# ----------------------------------------------------------------------------
# Main class
# ----------------------------------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _r(x: float, nd=2):
    return round(x, nd)


class StatsSheet:
    def __init__(self, backend):
        self.b = backend

    @classmethod
    def from_service_account(cls, creds_path: str, sheet_id: str) -> "StatsSheet":
        import gspread
        gc = gspread.service_account(filename=creds_path)
        return cls(GspreadBackend(gc.open_by_key(sheet_id)))

    # ---- setup ------------------------------------------------------------
    def ensure_headers(self):
        """Write header rows to any empty tab. Safe to call on every startup."""
        for tab, header in HEADERS.items():
            rows = self.b.read(tab)
            if not rows or not any(rows[0]):
                self.b.overwrite(tab, [header])

    # ---- public entry point ---------------------------------------------
    def record_series(self, rep: SeriesReport) -> str:
        """Write one series (replacing any earlier report of it) and rebuild totals."""
        sid = rep.series_id or f"S{SEASON}-W{rep.week}-" + "-vs-".join(
            sorted([_slug(rep.team_a), _slug(rep.team_b)]))
        when = (rep.reported_at or datetime.now(EASTERN)).astimezone(EASTERN)
        conf = {rep.team_a: rep.team_a_conf, rep.team_b: rep.team_b_conf}
        other = {rep.team_a: rep.team_b, rep.team_b: rep.team_a}

        games_rows, pg_rows, tg_rows = [], [], []
        wins = {rep.team_a: 0, rep.team_b: 0}

        for g in sorted(rep.games, key=lambda x: x.game_number):
            gid = f"{sid}-G{g.game_number}"
            tot = {t: defaultdict(int) for t in conf}
            for p in g.players:
                if p.team not in conf:
                    raise ValueError(f"Player {p.name} has team '{p.team}', not in this series")
                for k in ("goals", "assists", "saves", "shots", "score", "demos"):
                    tot[p.team][k] += int(getattr(p, k))
            a_goals, b_goals = tot[rep.team_a]["goals"], tot[rep.team_b]["goals"]
            if a_goals == b_goals:
                raise ValueError(f"Game {g.game_number} is tied {a_goals}-{b_goals}; check the stats")
            winner = rep.team_a if a_goals > b_goals else rep.team_b
            wins[winner] += 1

            blue = g.blue_team or rep.team_a
            orange = other[blue]
            games_rows.append([
                gid, sid, SEASON, rep.week, g.game_number, blue, orange,
                tot[blue]["goals"], tot[orange]["goals"], winner,
                int(bool(g.overtime)), g.duration_sec if g.duration_sec is not None else "",
                g.map, g.replay_id,
            ])
            for p in g.players:
                pg_rows.append([
                    sid, gid, SEASON, rep.week, g.game_number, rep.match_type,
                    p.name, p.player_id or p.name, p.team, conf[p.team],
                    other[p.team], conf[other[p.team]], int(p.team == winner),
                    p.score, p.goals, p.assists, p.saves, p.shots, p.demos, int(bool(p.mvp)),
                ])
            for t in (rep.team_a, rep.team_b):
                o = other[t]
                tg_rows.append([
                    sid, gid, SEASON, rep.week, g.game_number, rep.match_type,
                    t, conf[t], o, conf[o], int(t == winner),
                    tot[t]["goals"], tot[o]["goals"], tot[t]["assists"], tot[t]["saves"],
                    tot[t]["shots"], tot[o]["shots"], tot[t]["demos"], tot[t]["score"],
                ])

        if wins[rep.team_a] == wins[rep.team_b]:
            raise ValueError(f"Series is tied {wins[rep.team_a]}-{wins[rep.team_b]}")
        s_win = rep.team_a if wins[rep.team_a] > wins[rep.team_b] else rep.team_b
        series_row = [
            sid, SEASON, rep.week, rep.match_type, when.strftime("%Y-%m-%d %H:%M:%S"),
            rep.team_a, rep.team_a_conf, rep.team_b, rep.team_b_conf,
            wins[rep.team_a], wins[rep.team_b], s_win, other[s_win], len(rep.games),
            rep.ballchasing_url, rep.reported_by,
        ]

        # Replace any earlier report of this series, then append the new rows.
        self._remove_series(sid)
        self.b.append("Series", [series_row])
        self.b.append("Games", games_rows)
        self.b.append("PlayerGames", pg_rows)
        self.b.append("TeamGames", tg_rows)

        self.rebuild()
        return sid

    def delete_series(self, series_id: str):
        """Admin helper: remove a series entirely (e.g. reported by mistake)."""
        self._remove_series(series_id)
        self.rebuild()

    # ---- internals ------------------------------------------------------
    def _remove_series(self, sid: str):
        for tab in RAW_TABS:
            rows = self.b.read(tab)
            if not rows:
                continue
            col = rows[0].index("series_id")
            kept = [rows[0]] + [r for r in rows[1:] if len(r) > col and r[col] != sid]
            if len(kept) != len(rows):
                self.b.overwrite(tab, kept)

    def _records(self, tab) -> list[dict]:
        rows = self.b.read(tab)
        if not rows:
            return []
        h = rows[0]
        return [dict(zip(h, r)) for r in rows[1:] if any(r)]

    def rebuild(self):
        """Recompute PlayerTotals, TeamTotals, Leaders and Meta from the raw tabs."""
        pg = self._records("PlayerGames")
        tg = self._records("TeamGames")
        series = self._records("Series")

        # -- players: keyed by (player_id, team) so a transfer shows as two rows
        P = {}
        for r in pg:
            key = (r["player_id"], r["team"])
            d = P.setdefault(key, defaultdict(float))
            d["_name"] = r["player"]          # latest name wins
            d["_conf"] = r["conference"]
            d["gp"] += 1
            d["wins"] += _num(r["win"])
            for k in ("goals", "assists", "saves", "shots", "score", "demos"):
                d[k] += _num(r[k])
            d["mvps"] += _num(r["mvp"])

        player_rows = []
        player_recs = []
        for (pid, team), d in P.items():
            gp = d["gp"]
            rec = {
                "player": d["_name"], "player_id": pid, "team": team,
                "conference": d["_conf"], "gp": int(gp), "wins": int(d["wins"]),
                "losses": int(gp - d["wins"]),
                "goals": int(d["goals"]), "assists": int(d["assists"]),
                "saves": int(d["saves"]), "shots": int(d["shots"]),
                "score": int(d["score"]), "demos": int(d["demos"]), "mvps": int(d["mvps"]),
                "shooting_pct": _r(100 * d["goals"] / d["shots"], 1) if d["shots"] else 0.0,
                "goals_pg": _r(d["goals"] / gp), "assists_pg": _r(d["assists"] / gp),
                "saves_pg": _r(d["saves"] / gp), "shots_pg": _r(d["shots"] / gp),
                "score_pg": _r(d["score"] / gp, 1), "demos_pg": _r(d["demos"] / gp),
            }
            player_recs.append(rec)
        player_recs.sort(key=lambda x: (x["conference"], x["team"], x["player"].lower()))
        player_rows = [HEADERS["PlayerTotals"]] + [
            [rec[c] for c in HEADERS["PlayerTotals"]] for rec in player_recs]

        # -- teams
        T = {}
        for r in tg:
            d = T.setdefault(r["team"], defaultdict(float))
            d["_conf"] = r["conference"]
            d["gp"] += 1
            d["game_wins"] += _num(r["win"])
            for k in ("goals", "goals_against", "assists", "saves", "shots",
                      "shots_against", "demos"):
                d[k] += _num(r[k])
        for s in series:
            for side in ("team_a", "team_b"):
                d = T.setdefault(s[side], defaultdict(float))
                d["series_played"] += 1
                d["series_wins"] += 1 if s["winner"] == s[side] else 0

        team_recs = []
        for team, d in T.items():
            gp = d["gp"] or 1
            team_recs.append({
                "team": team, "conference": d["_conf"],
                "series_played": int(d["series_played"]),
                "series_wins": int(d["series_wins"]),
                "series_losses": int(d["series_played"] - d["series_wins"]),
                "gp": int(d["gp"]), "game_wins": int(d["game_wins"]),
                "game_losses": int(d["gp"] - d["game_wins"]),
                "goals": int(d["goals"]), "goals_against": int(d["goals_against"]),
                "goal_diff": int(d["goals"] - d["goals_against"]),
                "assists": int(d["assists"]), "saves": int(d["saves"]),
                "shots": int(d["shots"]), "shots_against": int(d["shots_against"]),
                "demos": int(d["demos"]),
                "shooting_pct": _r(100 * d["goals"] / d["shots"], 1) if d["shots"] else 0.0,
                "goals_pg": _r(d["goals"] / gp), "goals_against_pg": _r(d["goals_against"] / gp),
                "assists_pg": _r(d["assists"] / gp), "saves_pg": _r(d["saves"] / gp),
                "shots_pg": _r(d["shots"] / gp), "shots_against_pg": _r(d["shots_against"] / gp),
            })
        team_recs.sort(key=lambda x: (x["conference"], -x["series_wins"], -x["goal_diff"], x["team"]))
        team_rows = [HEADERS["TeamTotals"]] + [
            [rec[c] for c in HEADERS["TeamTotals"]] for rec in team_recs]

        # -- leaders
        leader_rows = [HEADERS["Leaders"]]
        scopes = [("Overall", "Overall", player_recs, TOP_N, True)]
        by_conf, by_team = defaultdict(list), defaultdict(list)
        for rec in player_recs:
            by_conf[rec["conference"]].append(rec)
            by_team[rec["team"]].append(rec)
        for c in sorted(by_conf):
            scopes.append(("Conference", c, by_conf[c], TOP_N, True))
        for t in sorted(by_team):
            # team pages show the whole roster, no games-played minimum
            scopes.append(("Team", t, by_team[t], None, False))

        for scope_type, scope, recs, limit, apply_min in scopes:
            for cat, is_rate in LEADER_CATEGORIES:
                pool = [x for x in recs
                        if not (is_rate and apply_min and x["gp"] < MIN_GP_RATE_STATS)]
                pool.sort(key=lambda x: (-x[cat], x["gp"], x["player"].lower()))
                rank, prev = 0, None
                for i, x in enumerate(pool):
                    if x[cat] != prev:
                        rank, prev = i + 1, x[cat]
                    if limit and rank > limit:
                        break
                    leader_rows.append([scope_type, scope, cat, rank, x["player"],
                                        x["player_id"], x["team"], x["conference"],
                                        x["gp"], x[cat]])

        # -- meta
        weeks = [int(_num(s["week"])) for s in series] or [0]
        meta_rows = [HEADERS["Meta"],
                     ["last_updated", datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S ET")],
                     ["season", SEASON],
                     ["latest_week", max(weeks)],
                     ["series_count", len(series)],
                     ["game_count", len(self._records("Games"))],
                     ["player_count", len(player_recs)],
                     ["team_count", len(team_recs)],
                     ["top_n", TOP_N],
                     ["min_gp_rate_stats", MIN_GP_RATE_STATS]]

        self.b.overwrite("PlayerTotals", player_rows)
        self.b.overwrite("TeamTotals", team_rows)
        self.b.overwrite("Leaders", leader_rows)
        self.b.overwrite("Meta", meta_rows)


# ----------------------------------------------------------------------------
# Optional: build a GameReport straight from a ballchasing replay JSON
# (GET https://ballchasing.com/api/replays/{id}). Adjust if your bot already
# parses replays differently.
# ----------------------------------------------------------------------------
def game_from_ballchasing(replay: dict, game_number: int, blue_team: str,
                          orange_team: str) -> GameReport:
    players = []
    for color, team in (("blue", blue_team), ("orange", orange_team)):
        for p in replay.get(color, {}).get("players", []):
            core = p.get("stats", {}).get("core", {})
            demo = p.get("stats", {}).get("demo", {})
            pid = p.get("id", {})
            players.append(PlayerLine(
                name=p.get("name", "?"), team=team,
                goals=core.get("goals", 0), assists=core.get("assists", 0),
                saves=core.get("saves", 0), shots=core.get("shots", 0),
                score=core.get("score", 0), demos=demo.get("inflicted", 0),
                mvp=bool(core.get("mvp", False)),
                player_id=f'{pid.get("platform", "")}:{pid.get("id", "")}' if pid else "",
            ))
    return GameReport(game_number=game_number, players=players, blue_team=blue_team,
                      overtime=bool(replay.get("overtime", False)),
                      duration_sec=replay.get("duration"), map=replay.get("map_name", ""),
                      replay_id=replay.get("id", ""))
