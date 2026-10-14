"""
Writing to the Google Sheets.

Two jobs:
  confirmed time  -> the caster sheet's "Date and Time" column
  final score     -> the two score cells on the Non-Conference tab

Standings are NOT written. The sheet already works them out from the score
cells via NCRaw Data and NCSorting, so the bot puts the score where the
formulas are looking and leaves the arithmetic to the sheet. Writing
standings directly would mean two sources of truth that could disagree.

Everything here verifies before it writes. The bot reads the team names at
the cells it's about to touch and refuses if they aren't the two teams in
the fixture. A row that has shifted is then a logged refusal rather than a
score quietly written into someone else's game - which nobody would notice
until standings were wrong.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

log = logging.getLogger("ccs.sheets")

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # the bot runs fine without sheets switched on
    gspread = None
    Credentials = None

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def normalise(name: str) -> str:
    """Loose match - the sheets have odd spacing and the odd apostrophe."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _delete_runs(ws, rows):
    """Delete these row numbers, one request per run of consecutive rows (bottom up)."""
    rows = sorted(set(rows))
    runs, start = [], None
    for i, r in enumerate(rows):
        if start is None:
            start = prev = r
        elif r == prev + 1:
            prev = r
        else:
            runs.append((start, prev))
            start = prev = r
    if start is not None:
        runs.append((start, prev))
    for a, b in reversed(runs):
        ws.delete_rows(a, b)


class Sheets:
    def __init__(self, store):
        self.store = store
        self.gc = None
        self.nc = None          # non-conference spreadsheet
        self.caster = None
        self.conf = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> Tuple[bool, str]:
        if gspread is None:
            return False, "gspread isn't installed — run: pip install gspread google-auth"
        path = self.store.cfg("service_account_file", "service_account.json")
        try:
            creds = Credentials.from_service_account_file(path, scopes=SCOPES)
            # Google allows ~60 requests a minute. When a burst goes over
            # (catch-up + relabel + website sync at once), back off and retry
            # instead of failing the write - that's what lost the 10/9 results
            backoff = getattr(gspread, "BackOffHTTPClient", None)
            self.gc = (gspread.authorize(creds, http_client=backoff) if backoff
                       else gspread.authorize(creds))
            self.sa_email = getattr(creds, "service_account_email", "")
        except FileNotFoundError:
            return False, f"no key file at {path}"
        except Exception as e:
            return False, f"couldn't read the key file ({type(e).__name__})"

        opened = []
        for attr, key in (
            ("nc", "non_conference_sheet_id"),
            ("caster", "caster_sheet_id"),
            ("conf", "conference_sheet_id"),
        ):
            sid = self.store.cfg(key)
            if not sid:
                continue
            try:
                setattr(self, attr, self.gc.open_by_key(sid))
                opened.append(key.replace("_sheet_id", ""))
            except Exception as e:
                return False, (
                    f"couldn't open {key} — check the id, and that the sheet is "
                    f"shared with the service account ({type(e).__name__})"
                )
        if not opened:
            return False, "no sheet ids configured"
        return True, "opened " + ", ".join(opened)

    # -------------------------------------------------------------- the score
    async def write_score(self, fixture: dict, result: dict) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._write_score, fixture, result)

    async def clear_score(self, fixture: dict) -> Tuple[bool, str]:
        """Blank both score cells - used when a match is reset."""
        return await asyncio.to_thread(self._write_score, fixture, None)

    def _write_score(self, fixture: dict, result: dict) -> Tuple[bool, str]:
        if self.nc is None:
            return False, "non-conference sheet not open"
        tab_name = self.store.cfg("non_conference_tab", "Non-Conference")
        try:
            ws = self.nc.worksheet(tab_name)
        except Exception:
            return False, f"no tab called {tab_name}"

        if not fixture.get("row"):
            return False, "this match isn't in the real schedule (test-only fixture)"
        row = fixture["row"]
        hc, ac = fixture["cell_home"], fixture["cell_away"]
        sh, sa = fixture["score_home"], fixture["score_away"]

        # check we're looking at the right game before touching anything
        try:
            names = ws.batch_get(
                [f"{col_letter(hc)}{row}", f"{col_letter(ac)}{row}"]
            )
        except Exception as e:
            return False, f"couldn't read the row back ({type(e).__name__})"
        got_home = names[0][0][0] if names[0] and names[0][0] else ""
        got_away = names[1][0][0] if names[1] and names[1][0] else ""

        if normalise(got_home) != normalise(fixture["home"]) or normalise(
            got_away
        ) != normalise(fixture["away"]):
            return False, (
                f"row {row} holds '{got_home}' vs '{got_away}', "
                f"expected '{fixture['home']}' vs '{fixture['away']}' — not writing"
            )

        home_val = "" if result is None else result["home"]
        away_val = "" if result is None else result["away"]

        # The dash between the scores becomes a link to the replays. It's the
        # one cell on the row nothing reads, so the standings formulas are
        # untouched - putting a formula in a score cell would risk them.
        dash_col = 23 + 43 * int(fixture.get("band", 0))
        link = "" if result is None else (result.get("link") or "").strip()
        if link and self.store.cfg("link_scores", True):
            dash_val = f'=HYPERLINK("{link}","-")'
        else:
            dash_val = "-"

        try:
            ws.batch_update(
                [
                    {"range": f"{col_letter(sh)}{row}", "values": [[home_val]]},
                    {"range": f"{col_letter(sa)}{row}", "values": [[away_val]]},
                    {"range": f"{col_letter(dash_col)}{row}", "values": [[dash_val]]},
                ],
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            return False, f"write failed ({type(e).__name__})"
        verb = "cleared" if result is None else "wrote"
        return True, f"{verb} {col_letter(sh)}{row} / {col_letter(sa)}{row}"

    # --------------------------------------------------------------- the time
    async def write_time(self, fixture: dict, when: datetime) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._write_time, fixture, when)

    async def clear_time(self, fixture: dict) -> Tuple[bool, str]:
        """Blank the caster sheet's time cell."""
        return await asyncio.to_thread(self._write_time, fixture, None)

    def _write_time(self, fixture: dict, when) -> Tuple[bool, str]:
        if self.caster is None:
            return False, "caster sheet not open"

        # the caster sheet has a tab per week, so search them all rather than
        # assuming the first one. A match appears on exactly one.
        want = {normalise(fixture["home"]), normalise(fixture["away"])}
        try:
            tabs = self.caster.worksheets()
        except Exception as e:
            return False, f"couldn't list the caster tabs ({type(e).__name__})"

        hits = []
        for ws in tabs:
            try:
                rows = ws.get_all_values()
            except Exception:
                continue
            for i, r in enumerate(rows, start=1):
                if len(r) < 2:
                    continue
                if {normalise(r[0]), normalise(r[1])} == want:
                    hits.append((ws, i))
                    break

        if not hits:
            names = ", ".join(w.title for w in tabs)
            return False, (
                f"no row for this match on any caster tab (looked in: {names})"
            )
        if len(hits) > 1:
            where = ", ".join(f"{w.title}!D{i}" for w, i in hits)
            return False, (
                f"this match appears on more than one caster tab ({where}) — "
                "not writing, since I can't tell which is current"
            )

        ws, target = hits[0]
        value = "" if when is None else self.format_time(when)
        try:
            ws.update_acell(f"D{target}", value)
        except Exception as e:
            return False, f"write failed ({type(e).__name__})"
        verb = "cleared" if when is None else "wrote"
        return True, f"{verb} {ws.title}!D{target}"

    # ------------------------------------------------------------ results log
    RESULT_HEADERS = [
        "Fixture ID", "Week", "Home", "Away", "Home Score", "Away Score",
        "Winner", "Ballchasing Group", "Reported By", "Reporter Team",
        "Agreed Time (ET)", "Last Updated (ET)", "Note",
    ]

    def _results_tab(self):
        """The bot's own tab. Made on first use so nothing has to be set up."""
        return self._tab(
            self.store.cfg("results_tab", "Bot Results"),
            self.RESULT_HEADERS,
            1200,
        )

    async def log_result(self, fixture, result, state, note="") -> Tuple[bool, str]:
        return await asyncio.to_thread(self._log_result, fixture, result, state, note)

    def _log_result(self, fixture, result, state, note="") -> Tuple[bool, str]:
        """
        One row per match, updated in place rather than appended. An append-only
        log would double up every time a result is corrected; keyed on the
        fixture id, the tab always reads as the current truth and stays at one
        row per game.
        """
        if self.nc is None:
            return False, "non-conference sheet not open"
        try:
            ws = self._results_tab()
        except Exception as e:
            return False, f"couldn't open the results tab ({type(e).__name__})"

        from .times import EASTERN

        agreed = ""
        if state.get("scheduled_iso"):
            agreed = self.format_time(datetime.fromisoformat(state["scheduled_iso"]))
        n = datetime.now(EASTERN)
        # "%-m" style formats are glibc-only and raise on Windows, which is
        # where this runs - so build it by hand
        hour = n.strftime("%I").lstrip("0") or "12"
        now = f"{n.month}/{n.day} {hour}:{n.minute:02d}{n.strftime('%p').lower()}"

        if result is None:
            row_values = [
                fixture["id"], fixture["week"], fixture["home"], fixture["away"],
                "", "", "", "", "", "", agreed, now, note or "cleared",
            ]
        else:
            row_values = [
                fixture["id"], fixture["week"], fixture["home"], fixture["away"],
                result["home"], result["away"], result["winner"],
                result.get("link", ""), result.get("by_name", ""),
                result.get("by_team", "") or "admin",
                agreed, now,
                "; ".join(([note] if note else []) + list(result.get("flags") or []))
                or (
                    "FORFEIT: " + result.get("forfeit_reason", "")
                    + (f" · proof: {result['proof']}" if result.get("proof") else "")
                    if result.get("forfeit")
                    else result.get("override_reason", "")
                ),
            ]

        try:
            ids = ws.col_values(1)
        except Exception as e:
            return False, f"couldn't read the results tab ({type(e).__name__})"

        try:
            target = ids.index(fixture["id"]) + 1
        except ValueError:
            target = len(ids) + 1

        try:
            ws.update(f"A{target}", [row_values], value_input_option="USER_ENTERED")
        except Exception as e:
            return False, f"write failed ({type(e).__name__})"
        # colour the note: red is a quick roster fix (unrostered players),
        # yellow needs a closer look (missing replays, roster disagreements)
        level = (result or {}).get("flag_level", "") if result else ""
        colour = {"red": {"red": 0.96, "green": 0.6, "blue": 0.6},
                  "yellow": {"red": 1.0, "green": 0.9, "blue": 0.5}}.get(
            level, {"red": 1, "green": 1, "blue": 1})
        try:
            ws.format(f"M{target}", {"backgroundColor": colour})
        except Exception:
            pass
        return True, f"{ws.title}!A{target}"

    # -------------------------------------------------------------- game stats
    # Roster Name sits beside Player: Player is the tag they used that night,
    # Roster Name is who the sheet says that is. Stats read off the roster
    # name so a mid-season rename doesn't split someone in two.
    GAME_HEADERS = [
        "Fixture ID", "Week", "Replay ID", "Game", "Date", "Team", "Won",
        "Player", "Roster Name", "Platform", "Platform ID", "Tracker",
        "Score", "Goals", "Assists", "Saves", "Shots", "MVP",
        "Flag", "Flag Detail",
    ]

    async def read_roster(self):
        return await asyncio.to_thread(self._read_roster)

    # Nation's live Non Conference sheet - the roster everyone edits. Only
    # ever READ. Falls back to the Roster Sheet tab on our own copy if the
    # bot can't open it (not shared with the service account yet).
    LIVE_ROSTER_SHEET = "10romEaYscfRso9Px8rOO_5vYWXe3nbihDBL5CedoZTk"

    def _read_roster(self):
        """
        The Roster Sheet as a raw grid, for bot/roster.py to make sense of.

        By default it's the league's live sheet (read-only), so roster fixes
        count the moment they're made. "roster_sheet_id" in config overrides
        it; set it to "" to use the copy the bot writes to instead.
        """
        name = self.store.cfg("roster_tab", "Roster Sheet")
        sid = self.store.cfg("roster_sheet_id", self.LIVE_ROSTER_SHEET)
        note = ""
        if sid:
            try:
                ws = self.gc.open_by_key(sid).worksheet(name)
                self.roster_source = "the league's live roster"
                return ws.get_all_values(), ""
            except Exception as e:
                email = getattr(self, "sa_email", "") or \
                    "the bot's service account (client_email in service_account.json)"
                note = (f"couldn't read the live roster ({type(e).__name__}) - share that sheet with "
                        f"{email} as Viewer. Using the Roster Sheet tab on our copy for now")
        if self.nc is None:
            return None, note or "non-conference sheet not open"
        try:
            ws = self.nc.worksheet(name)
            self.roster_source = "the copy's Roster Sheet tab" + (f" ({note})" if note else "")
            return ws.get_all_values(), ""
        except Exception as e:
            tabs = ""
            try:
                tabs = " - tabs there: " + ", ".join(w.title for w in self.nc.worksheets()[:15])
            except Exception:
                pass
            return None, (note + "; " if note else "") + f"couldn't read a tab called '{name}' ({type(e).__name__}){tabs}"

    def _stats_tab(self):
        return self._tab(
            self.store.cfg("game_stats_tab", "Game Stats"),
            self.GAME_HEADERS,
            20000,
        )

    async def relabel_game_stats(self, team_of, roster=None) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._relabel_game_stats, team_of, roster)

    def _relabel_game_stats(self, team_of, roster=None) -> Tuple[bool, str]:
        """
        Re-decide the Team on every line already on Game Stats, roster first.

        For each series: rebuild the two sides from who won each game
        together, name the sides by their players' roster teams, then give
        every rostered player their roster team (if it's one of the two in
        the match). Subs and unrostered players keep their side's team.
        Flags are recomputed to match. Only columns F (Team) and S:T (flags)
        are written, in one request.
        """
        from collections import Counter, defaultdict
        from .replayfile import assign_sides
        from .stats import Stats
        if self.nc is None:
            return False, "non-conference sheet not open"
        ws = self._stats_tab()
        vals = ws.get_values("A2:T")
        if not vals:
            return True, "Game Stats is empty"

        # Tidy the tab before relabelling:
        #  - spectators (coach, manager, streamer) written before the bot
        #    learned to skip them: no score, goals, assists, saves or shots
        #  - the same player in the same game written twice
        #  - Steam ids Sheets rounded to "7.66E+16", put back from the roster
        from .cleanup import clean_stats, sheet_row

        def _z(x):
            try:
                return float(str(x).replace(",", "") or 0)
            except ValueError:
                return 0.0
        played = [v for v in vals
                  if not v or not v[0]
                  or any(_z((v + [""] * 20)[i]) > 0 for i in (12, 13, 14, 15, 16))]
        dropped = len(vals) - len(played)
        cleaned, dup_lines, fixed_ids = clean_stats([[]] + played, roster)
        cleaned = cleaned[1:]
        if dropped or dup_lines or fixed_ids:
            kept = [sheet_row(v) for v in cleaned]
            ws.batch_clear([f"A2:T{len(vals) + 1}"])
            ws.update(range_name="A2", values=kept, value_input_option="USER_ENTERED")
            vals = ws.get_values("A2:T")
        fixtures = {f["id"]: f for f in self.store.fixtures}
        groups = defaultdict(list)
        for i, v in enumerate(vals):
            v = v + [""] * (20 - len(v))
            if not v[0] or not v[2]:
                continue
            groups[v[0]].append((i, v))

        new_team = {}
        new_flag = {}
        changed_rows, changed_series = 0, set()
        for fid, items in groups.items():
            fx = fixtures.get(fid)
            labels = Counter(v[5] for _, v in items if v[5])
            teams = [fx["home"], fx["away"]] if fx else [t for t, _ in labels.most_common(2)]
            if len(teams) < 2:
                continue
            rows = []
            for i, v in items:
                try:
                    game_no = int(v[3])
                except ValueError:
                    game_no = 0
                rows.append({
                    "i": i, "replay_id": v[2], "game_no": game_no,
                    # winners and losers of each game stand in for colours
                    # Won holds 1/0 (written by the bot) or TRUE/FALSE (if formatted
                    # as a checkbox) - accept either
                    "colour": "blue" if str(v[6]).strip().upper() in ("TRUE", "1", "1.0") else "orange",
                    "name": v[7], "platform": v[9], "platform_id": v[10],
                    "old_team": v[5], "old_flag": v[18], "old_detail": v[19],
                })
            assign_sides(rows)
            # starting point: whatever most of each side is labelled now
            for side in ("A", "B"):
                c = Counter(r["old_team"] for r in rows if r["side"] == side and r["old_team"])
                guess = c.most_common(1)[0][0] if c else ""
                for r in rows:
                    if r["side"] == side:
                        r["team"] = guess
            Stats.apply_roster(rows, teams[0], teams[1], team_of)
            for r in rows:
                rt = r.get("roster_team")
                if rt is None:
                    flag = r["old_flag"] if r["old_flag"] == "AMBIGUOUS" else "UNROSTERED"
                    detail = r["old_detail"] if flag == r["old_flag"] else f"{r['name']} isn't on the roster sheet"
                elif rt != r["team"]:
                    flag, detail = "WRONG TEAM", f"{r['name']} is rostered to {rt}, played for {r['team']}"
                else:
                    flag, detail = "", ""
                new_team[r["i"]] = r["team"]
                new_flag[r["i"]] = (flag, detail)
                if r["team"] != r["old_team"]:
                    changed_rows += 1
                    changed_series.add(fid)

        n = len(vals)
        col_f = [[new_team.get(i, (vals[i] + [""] * 6)[5])] for i in range(n)]
        col_st = []
        for i in range(n):
            v = vals[i] + [""] * (20 - len(vals[i]))
            col_st.append(list(new_flag.get(i, (v[18], v[19]))))
        ws.batch_update([
            {"range": f"F2:F{n + 1}", "values": col_f},
            {"range": f"S2:T{n + 1}", "values": col_st},
        ], value_input_option="RAW")
        return True, (f"re-labelled {changed_rows} stat line(s) across {len(changed_series)} "
                      f"series, out of {n} lines in {len(groups)} series"
                      + (f" · removed {dropped} spectator line(s)" if dropped else "")
                      + (f" · removed {dup_lines} duplicate line(s)" if dup_lines else "")
                      + (f" · repaired {fixed_ids} Steam id(s)" if fixed_ids else ""))

    async def read_game_stats(self):
        return await asyncio.to_thread(lambda: self._stats_tab().get_values("A1:T"))

    async def read_game_scores(self):
        return await asyncio.to_thread(lambda: self._scores_tab().get_values("A1:N"))

    async def sync_website(self, roster=None) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._sync_website, roster)

    def _write_roster_issues(self, stats, roster) -> str:
        """The Roster Issues tab on the Non Conference sheet, for staff."""
        from . import roster_issues
        rows = roster_issues.build(stats, {f["id"]: f for f in self.store.fixtures}, roster)
        try:
            ws = self.nc.worksheet(roster_issues.TAB)
            first = ws.row_values(1)
            if first and first[0] != roster_issues.HEADERS[0]:
                # never write over a tab somebody else made
                return f"skipped - a '{roster_issues.TAB}' tab already exists that the bot didn't make"
        except Exception:
            ws = self.nc.add_worksheet(roster_issues.TAB, rows=len(rows) + 200,
                                       cols=len(roster_issues.HEADERS))
            try:
                ws.freeze(rows=1)
                ws.format("A1:O1", {"textFormat": {"bold": True}})
            except Exception:
                pass
        if ws.row_count < len(rows) + 50:
            ws.resize(rows=len(rows) + 200)
        if ws.col_count < len(roster_issues.HEADERS):
            ws.resize(cols=len(roster_issues.HEADERS))
        ws.batch_clear(["A:Z"])
        ws.update(range_name="A1", values=rows, value_input_option="RAW")
        counts = {}
        for r in rows[1:]:
            counts[r[2]] = counts.get(r[2], 0) + 1
        return f"{len(rows) - 1} players to check (" + ", ".join(
            f"{v} {k.lower()}" for k, v in sorted(counts.items())) + ")"

    def _sync_website(self, roster=None) -> Tuple[bool, str]:
        """Rebuild the whole website stats sheet from the bot's own records."""
        from . import website
        if self.gc is None or self.nc is None:
            return False, "sheets not connected"
        sid = self.store.cfg("website_sheet_id", website.DEFAULT_SHEET)
        if not sid:
            return False, "website_sheet_id is blank"
        try:
            stats = self._stats_tab().get_values("A1:T")
            scores = self._scores_tab().get_values("A1:N")
        except Exception as e:
            return False, f"couldn't read Game Stats / Game Scores ({type(e).__name__})"
        # duplicates and rounded Steam ids never reach the website or the
        # issues tab, even before a relabel has tidied the tab itself
        from .cleanup import clean_stats, clean_scores
        stats, _, _ = clean_stats(stats, roster)
        scores, _ = clean_scores(scores)
        issues = ""
        if roster is not None and getattr(roster, "players", None):
            try:
                issues = " · roster issues: " + self._write_roster_issues(stats, roster)
            except Exception as e:
                issues = f" · roster issues tab failed ({type(e).__name__}: {e})"
        tabs = website.build(self.store, stats, scores)
        try:
            ok, msg = website.push(self.gc, sid, tabs)
        except Exception as e:
            ok, msg = False, f"write failed ({type(e).__name__}: {e})"
        return ok, msg + issues

    async def write_game_stats(self, fixture, rows) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._write_game_stats, fixture, rows)

    def _write_game_stats(self, fixture, rows) -> Tuple[bool, str]:
        """
        Append this series' per-game lines, skipping any already present.

        Keyed on replay id plus platform id, so re-running a series - after a
        correction, or a retry - adds nothing twice. That matters because the
        totals are SUMIFs over this tab: a duplicated row would quietly
        inflate someone's season.
        """
        if self.nc is None:
            return False, "non-conference sheet not open"
        if not rows:
            return False, "no stats to write"
        try:
            ws = self._stats_tab()
        except Exception as e:
            return False, f"couldn't open the stats tab ({type(e).__name__})"

        try:
            existing = ws.get_values("C2:K")
        except Exception as e:
            return False, f"couldn't read the stats tab ({type(e).__name__})"
        from .cleanup import is_mangled, sheet_id, sheet_text
        from .roster import norm as _norm
        seen = {
            (r[0], r[8]) for r in existing if len(r) >= 9 and r[0]
        }
        # a Steam id Sheets already rounded to "7.66E+16" can't be compared,
        # so those lines are recognised by replay + platform + name instead
        seen_names = {
            (r[0], (r[7] or "").lower(), _norm(r[5]))
            for r in existing if len(r) >= 9 and r[0] and is_mangled(r[8])
        }

        # winners' three rows, then the losers', game by game - so a glance
        # down the tab tells you who took each game without reading scores
        rows = sorted(
            rows,
            key=lambda r: (
                r.get("game_no", 0),
                0 if r.get("won") else 1,
                -int(r.get("score", 0) or 0),
            ),
        )

        fresh = []
        for r in rows:
            if (r["replay_id"], r["platform_id"]) in seen:
                continue
            if (r["replay_id"], (r["platform"] or "").lower(), _norm(r["name"])) in seen_names:
                continue
            fresh.append(
                [
                    fixture["id"], fixture["week"], r["replay_id"], r["game_no"],
                    r["date"], r.get("team", ""), r["won"],
                    sheet_text(r["name"]), sheet_text(r.get("roster_name", "") or r["name"]),
                    r["platform"], sheet_id(r["platform_id"]),
                    r.get("tracker", ""),
                    r["score"], r["goals"], r["assists"], r["saves"],
                    r["shots"], r["mvp"],
                    r.get("flag", ""), r.get("flag_detail", ""),
                ]
            )
        if not fresh:
            return True, "already recorded"

        start = len(existing) + 2
        try:
            ws.update(f"A{start}", fresh, value_input_option="USER_ENTERED")
        except Exception as e:
            return False, f"write failed ({type(e).__name__})"
        return True, f"{len(fresh)} lines from {len({r[2] for r in fresh})} games"

    async def clear_game_stats(self, fixture) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._clear_game_stats, fixture)

    def _clear_game_stats(self, fixture) -> Tuple[bool, str]:
        """Drop every row for this fixture - used when a match is reset."""
        if self.nc is None:
            return False, "not open"
        try:
            ws = self._stats_tab()
            ids = ws.col_values(1)
        except Exception as e:
            return False, f"{type(e).__name__}"
        doomed = [i + 1 for i, v in enumerate(ids) if v == fixture["id"]]
        if not doomed:
            return True, "no stats rows"
        try:
            _delete_runs(ws, doomed)
        except Exception as e:
            return False, f"delete failed ({type(e).__name__})"
        return True, f"removed {len(doomed)} stat lines"

    # ----------------------------------------------------------- game scores
    SCORE_HEADERS = [
        "Fixture ID", "Week", "Replay ID", "Game", "Date",
        "Winner", "Winner Goals", "Loser", "Loser Goals",
        "Winner Capped", "Loser Capped", "Margin", "Capped Margin", "OT",
    ]

    def _tab(self, name: str, headers: list, rows: int):
        """
        Fetch a tab, creating it if needed - and correct its header row if the
        columns have changed since it was made.

        Without that, a tab created by an older build keeps its old headings
        while new rows are written in the new order, and the sheet quietly
        lies about what each column holds.
        """
        try:
            ws = self.nc.worksheet(name)
        except Exception:
            ws = self.nc.add_worksheet(title=name, rows=rows, cols=len(headers))
            ws.update("A1", [headers])
            ws.freeze(rows=1)
            return ws
        try:
            current = ws.row_values(1)
            if current != headers:
                ws.update("A1", [headers])
        except Exception:
            pass
        return ws

    def _scores_tab(self):
        return self._tab(
            self.store.cfg("game_scores_tab", "Game Scores"),
            self.SCORE_HEADERS,
            12000,
        )

    async def write_game_scores(self, fixture, games) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._write_game_scores, fixture, games)

    def _write_game_scores(self, fixture, games) -> Tuple[bool, str]:
        """
        One row per game, from ballchasing's team totals.

        Written winner-first, so a row reads the way anyone would say it out
        loud. The capped scores sit alongside the real ones rather than
        replacing them, so the cap is visible and anyone can check what it did.
        """
        if self.nc is None:
            return False, "non-conference sheet not open"
        if not games:
            return False, "no games to write"
        try:
            ws = self._scores_tab()
            existing = ws.get_values("C2:C")
        except Exception as e:
            return False, f"couldn't open the scores tab ({type(e).__name__})"
        seen = {r[0] for r in existing if r and r[0]}

        fresh = []
        for g in games:
            if g["replay_id"] in seen:
                continue
            blue_won = g["blue_goals"] > g["orange_goals"]
            if blue_won:
                wt = g.get("blue_team") or fixture["home"]
                lt = g.get("orange_team") or fixture["away"]
                wg, lg = g["blue_goals"], g["orange_goals"]
                wc, lc = g["capped_blue"], g["capped_orange"]
            else:
                wt = g.get("orange_team") or fixture["away"]
                lt = g.get("blue_team") or fixture["home"]
                wg, lg = g["orange_goals"], g["blue_goals"]
                wc, lc = g["capped_orange"], g["capped_blue"]
            fresh.append([
                fixture["id"], fixture["week"], g["replay_id"], g["game_no"],
                g.get("date", ""), wt, wg, lt, lg, wc, lc,
                wg - lg, wc - lc, "yes" if g.get("overtime") else "",
            ])
        if not fresh:
            return True, "already recorded"
        try:
            ws.update(f"A{len(existing) + 2}", fresh, value_input_option="USER_ENTERED")
        except Exception as e:
            return False, f"write failed ({type(e).__name__})"
        capped = sum(1 for r in fresh if r[11] != r[12])
        return True, (
            f"{len(fresh)} games" + (f", {capped} capped" if capped else "")
        )

    async def clear_game_scores(self, fixture) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._clear_game_scores, fixture)

    def _clear_game_scores(self, fixture) -> Tuple[bool, str]:
        if self.nc is None:
            return False, "not open"
        try:
            ws = self._scores_tab()
            ids = ws.col_values(1)
        except Exception as e:
            return False, f"{type(e).__name__}"
        doomed = [i + 1 for i, v in enumerate(ids) if v == fixture["id"]]
        if not doomed:
            return True, "no score rows"
        try:
            _delete_runs(ws, doomed)
        except Exception as e:
            return False, f"delete failed ({type(e).__name__})"
        return True, f"removed {len(doomed)} game scores"

    # ------------------------------------------------------ players & leaders
    def build_stat_tabs(self) -> Tuple[bool, str]:
        try:
            return self._build_stat_tabs()
        except Exception as e:
            if type(e).__name__ == "NotOurs":
                return False, (f"stopped: the sheet already has a tab called '{e}' that the "
                               "bot didn't make. Rename one of them and run it again — "
                               "nothing was overwritten.")
            raise

    def _build_stat_tabs(self) -> Tuple[bool, str]:
        """
        Lay out the stat tabs as formulas: Teams, Players, Leaderboard,
        Team Stats, Player Search and Leaders.

        Formulas rather than bot writes, so the tables recalculate the instant
        a result lands and stay right even if the bot is off. It also means
        anyone can see how a number was arrived at instead of taking the
        bot's word for it.
        """
        if self.nc is None:
            return False, "non-conference sheet not open"
        g = self.store.cfg("game_stats_tab", "Game Stats")
        G = f"'{g}'!"
        minimum = float(self.store.cfg("rate_minimum_share", 0.25))
        team_size = int(self.store.cfg("team_size", 3))
        pct = int(minimum * 100)

        import json, os
        from .store import DATA
        try:
            with open(os.path.join(DATA, "conferences.json"), encoding="utf-8") as f:
                conf_of = json.load(f)
        except Exception:
            conf_of = {}
        teams = sorted(self.store.teams) if getattr(self.store, "teams", None) else sorted(conf_of)
        conferences = sorted(set(conf_of.values()))

        # what A1 holds on each tab the bot builds. A tab with this name that
        # doesn't start this way belongs to someone else - leave it alone
        ours = {
            "Bot Teams": ("Team",), "Players": ("Platform ID",),
            "Leaderboard": ("CCS Leaderboard",), "Team Stats": ("CCS Leaderboard",),
            "Player Search": ("Player Search",),
            "Leaders": ("Goals",),
        }

        class NotOurs(Exception):
            pass

        def tab(name, rows, cols):
            try:
                ws = self.nc.worksheet(name)
            except Exception:
                return self.nc.add_worksheet(title=name, rows=rows, cols=cols)
            try:
                a1 = str(ws.acell("A1").value or "")
            except Exception:
                a1 = ""
            if a1 and not a1.startswith(ours.get(name, ("\0",))):
                raise NotOurs(name)
            if ws.col_count < cols or ws.row_count < rows:
                ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
            return ws

        def dropdowns(ws, items):
            try:
                from gspread.utils import ValidationConditionType as VCT
            except Exception as e:
                log.warning("couldn't add dropdowns: %s", e)
                return
            for cell, options in items:
                # the API wants every option as text, even 10/25/50; and one
                # bad list shouldn't stop the rest
                try:
                    ws.add_validation(cell, VCT.one_of_list, [str(o) for o in options],
                                      strict=True, showCustomUi=True)
                except Exception as e:
                    log.warning("couldn't add the %s dropdown on %s: %s", cell, ws.title, e)

        # ---- Teams: one row per team, conference plus team totals
        # A Team  B Conference  C Games  D Wins  E Win %  F-J totals
        # (Goals Assists Saves Shots Score)  K-O per game  P Shooting %
        tm = tab("Bot Teams", len(teams) + 10, 18)
        tm.clear()
        rows = [[
            "Team", "Conference", "Games", "Wins", "Win %",
            "Goals", "Assists", "Saves", "Shots", "Score",
            "Goals/G", "Assists/G", "Saves/G", "Shots/G", "Score/G", "Shooting %",
        ]]
        for i, t in enumerate(teams, start=2):
            rows.append([
                t, conf_of.get(t, ""),
                f"=COUNTUNIQUEIFS({G}$C$2:$C,{G}$F$2:$F,$A{i})",
                f"=COUNTUNIQUEIFS({G}$C$2:$C,{G}$F$2:$F,$A{i},{G}$G$2:$G,TRUE)",
                f'=IF($C{i}=0,"",ROUND($D{i}/$C{i}*100,1))',
                f"=SUMIF({G}$F$2:$F,$A{i},{G}$N$2:$N)",
                f"=SUMIF({G}$F$2:$F,$A{i},{G}$O$2:$O)",
                f"=SUMIF({G}$F$2:$F,$A{i},{G}$P$2:$P)",
                f"=SUMIF({G}$F$2:$F,$A{i},{G}$Q$2:$Q)",
                f"=SUMIF({G}$F$2:$F,$A{i},{G}$M$2:$M)",
                f'=IF($C{i}=0,"",ROUND(F{i}/$C{i},2))',
                f'=IF($C{i}=0,"",ROUND(G{i}/$C{i},2))',
                f'=IF($C{i}=0,"",ROUND(H{i}/$C{i},2))',
                f'=IF($C{i}=0,"",ROUND(I{i}/$C{i},2))',
                f'=IF($C{i}=0,"",ROUND(J{i}/$C{i},0))',
                f'=IF(I{i}=0,"",ROUND(F{i}/I{i}*100,1))',
            ])
        tm.update("A1", rows, value_input_option="USER_ENTERED")
        tm.freeze(rows=1)
        tm.format("A1:P1", {"textFormat": {"bold": True}})

        # ---- Players
        # A-Q as before; R Conference  S Wins  T Win %  U Shooting %
        pl = tab("Players", 1500, 22)
        pl.clear()
        formulas = {
            "A2": f"=UNIQUE(FILTER({G}K2:K,{G}K2:K<>\"\"))",
            "B2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(VLOOKUP(A2:A,{{{G}K2:K,{G}I2:I}},2,FALSE),\"\")))",
            # the team they've played for most (a QUERY sorted by count, so
            # VLOOKUP's first hit is the most frequent), not the first line found
            "C2": (f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(VLOOKUP(A2:A,"
                   f"QUERY({{{G}K2:K,{G}F2:F}},\"select Col1, Col2, count(Col1) where Col1 is not null "
                   f"group by Col1, Col2 order by count(Col1) desc label count(Col1) ''\",0),"
                   f"2,FALSE),\"\")))"),
            "D2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",COUNTIF({G}K2:K,A2:A)))",
            "E2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",COUNTIF({G}F2:F,C2:C)/{team_size}))",
            "F2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",SUMIF({G}K2:K,A2:A,{G}N2:N)))",
            "G2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",SUMIF({G}K2:K,A2:A,{G}O2:O)))",
            "H2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",SUMIF({G}K2:K,A2:A,{G}P2:P)))",
            "I2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",SUMIF({G}K2:K,A2:A,{G}Q2:Q)))",
            "J2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",SUMIF({G}K2:K,A2:A,{G}M2:M)))",
            "K2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(F2:F/D2:D,2),0)))",
            "L2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(G2:G/D2:D,2),0)))",
            "M2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(H2:H/D2:D,2),0)))",
            "N2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(I2:I/D2:D,2),0)))",
            "O2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(J2:J/D2:D,0),0)))",
            "P2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",D2:D>=({minimum}*E2:E)))",
            "Q2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(VLOOKUP(A2:A,{{{G}K2:K,{G}L2:L}},2,FALSE),\"\")))",
            "R2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(VLOOKUP(C2:C,'Bot Teams'!A2:B,2,FALSE),\"\")))",
            "S2": f"=ARRAYFORMULA(IF(A2:A=\"\",\"\",COUNTIFS({G}K2:K,A2:A,{G}G2:G,TRUE)))",
            "T2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(S2:S/D2:D*100,1),0)))",
            "U2": "=ARRAYFORMULA(IF(A2:A=\"\",\"\",IFERROR(ROUND(F2:F/I2:I*100,1),0)))",
        }
        pl_writes = [{"range": "A1", "values": [[
            "Platform ID", "Player", "Team", "Games", "Team Games",
            "Goals", "Assists", "Saves", "Shots", "Score",
            "Goals/G", "Assists/G", "Saves/G", "Shots/G", "Score/G",
            "Qualified", "Tracker", "Conference", "Wins", "Win %", "Shooting %",
        ]]}]
        pl_writes += [{"range": c, "values": [[f]]} for c, f in formulas.items()]
        pl.batch_update(pl_writes, value_input_option="USER_ENTERED")
        pl.freeze(rows=1)

        # shared pieces for the two boards
        stat_names = ["Goals", "Assists", "Saves", "Shots", "Score", "Shooting %", "Win %"]
        conf_list = ["All"] + conferences
        stat_list = '{"Goals";"Assists";"Saves";"Shots";"Score";"Shooting %";"Win %"}'

        def filters(conf_col, team_col=None):
            # values are wrapped in doubled quotes so names like
            # "Louisiana Ragin' Cajuns" don't break the query
            out = (f'&IF($B$6="All",""," and {conf_col} = """&$B$6&"""")')
            if team_col:
                out += f'&IF($B$7="All",""," and {team_col} = """&$B$7&"""")'
            return out

        # ---- Leaderboard (players)
        lb = tab("Leaderboard", 140, 10)
        lb.clear()
        lb_col = (
            f'=IF(MATCH($B$3,{stat_list},0)=6,"U",IF(MATCH($B$3,{stat_list},0)=7,"T",'
            f'IF($B$4="Per game",CHOOSE(MATCH($B$3,{stat_list},0),"K","L","M","N","O"),'
            f'CHOOSE(MATCH($B$3,{stat_list},0),"F","G","H","I","J"))))'
        )
        # rates and percentages only count regulars, totals count everyone
        lb_qual = '=OR($B$4="Per game",$B$3="Shooting %",$B$3="Win %")'
        lb_query = (
            '=IFERROR(QUERY(Players!$A$2:$U,"select B,C,R,D,"&$H$1&'
            '" where A is not null and "&$H$1&" > 0"&'
            'IF($H$2," and P = TRUE","")'
            + filters("R", "C") +
            '&" order by "&$H$1&" desc limit "&$B$5&'
            '" label B \'Player\',C \'Team\',R \'Conference\',D \'Games\',"&$H$1&" \'"&$B$3&'
            'IF(AND($B$4="Per game",$H$1<>"T",$H$1<>"U")," per game","")&"\'",0),"no players match")'
        )
        lb.batch_update([
            {"range": "A1", "values": [["CCS Leaderboard — players"]]},
            {"range": "A3", "values": [["Stat", "Goals"], ["View", "Total"], ["Show", 25],
                                       ["Conference", "All"], ["Team", "All"]]},
            {"range": "H1", "values": [[lb_col]]},
            {"range": "H2", "values": [[lb_qual]]},
            {"range": "D3", "values": [[
                f'=IF($H$2,"Rates and percentages only include players who appeared in '
                f'at least {pct}% of their team\'s games.","Season totals — everyone who has played.")'
            ]]},
            {"range": "A9", "values": [[lb_query]]},
        ], value_input_option="USER_ENTERED")
        dropdowns(lb, [
            ("B3", stat_names), ("B4", ["Total", "Per game"]), ("B5", [10, 25, 50, 100]),
            ("B6", conf_list), ("B7", ["All"] + teams),
        ])
        lb.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
        lb.format("A3:A7", {"textFormat": {"bold": True}})
        lb.format("A9:F9", {"textFormat": {"bold": True}})

        # ---- Team Stats (teams)
        ts = tab("Team Stats", 240, 10)
        ts.clear()
        ts_col = (
            f'=IF(MATCH($B$3,{stat_list},0)=6,"P",IF(MATCH($B$3,{stat_list},0)=7,"E",'
            f'IF($B$4="Per game",CHOOSE(MATCH($B$3,{stat_list},0),"K","L","M","N","O"),'
            f'CHOOSE(MATCH($B$3,{stat_list},0),"F","G","H","I","J"))))'
        )
        ts_query = (
            '=IFERROR(QUERY(\'Bot Teams\'!$A$2:$P,"select A,B,C,D,"&$H$1&'
            '" where C > 0"'
            + filters("B") +
            '&" order by "&$H$1&" desc limit "&$B$5&'
            '" label A \'Team\',B \'Conference\',C \'Games\',D \'Wins\',"&$H$1&" \'"&$B$3&'
            'IF(AND($B$4="Per game",$H$1<>"E",$H$1<>"P")," per game","")&"\'",0),"no teams match")'
        )
        ts.batch_update([
            {"range": "A1", "values": [["CCS Leaderboard — teams"]]},
            {"range": "A3", "values": [["Stat", "Goals"], ["View", "Per game"], ["Show", 25],
                                       ["Conference", "All"]]},
            {"range": "H1", "values": [[ts_col]]},
            {"range": "D3", "values": [["Games and wins count individual games, not series."]]},
            {"range": "A9", "values": [[ts_query]]},
        ], value_input_option="USER_ENTERED")
        dropdowns(ts, [
            ("B3", stat_names), ("B4", ["Total", "Per game"]),
            ("B5", [10, 25, 50, 100, 250]), ("B6", conf_list),
        ])
        ts.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
        ts.format("A3:A6", {"textFormat": {"bold": True}})
        ts.format("A9:E9", {"textFormat": {"bold": True}})

        # ---- Player search
        ps = tab("Player Search", 40, 18)
        ps.clear()
        ps.batch_update([
            {"range": "A1", "values": [["Player Search"]]},
            {"range": "A3", "values": [["Type a name", ""]]},
            {"range": "A5", "values": [[
                '=IFERROR(QUERY(Players!$A$2:$U,'
                '"select B,C,R,D,S,T,F,G,H,I,J,U,K,L,M,N,O where lower(B) '
                'contains \'"&LOWER($B$3)&"\' order by J desc limit 25 '
                'label B \'Player\',C \'Team\',R \'Conference\',D \'Games\',S \'Wins\','
                'T \'Win %\',F \'Goals\',G \'Assists\',H \'Saves\',I \'Shots\',J \'Score\','
                'U \'Shooting %\',K \'G/G\',L \'A/G\',M \'Sv/G\',N \'Sh/G\',O \'Sc/G\'",1),'
                '"no player matching that")'
            ]]},
        ], value_input_option="USER_ENTERED")
        ps.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
        ps.format("A3", {"textFormat": {"bold": True}})

        # ---- Leaders: a top 10 for every stat, total and per game
        ld = tab("Leaders", 200, 14)
        ld.clear()
        stats = [("Goals", "F", "K"), ("Assists", "G", "L"), ("Saves", "H", "M"),
                 ("Shots", "I", "N"), ("Score", "J", "O"),
                 ("Shooting %", None, "U"), ("Win %", None, "T")]
        blocks = []
        row = 1
        for label, tot_col, rate_col in stats:
            if tot_col:
                blocks.append((f"A{row}", [[f"{label} — total", "", ""]]))
                blocks.append((f"A{row + 1}", [["Player", "Team", label]]))
                blocks.append((
                    f"A{row + 2}",
                    [[f"=QUERY(Players!$A$2:$U,\"select B,C,{tot_col} where A is not null and {tot_col} > 0 order by {tot_col} desc limit 10 label B '',C '',{tot_col} ''\",0)"]],
                ))
                head = f"{label} per game — min {pct}% of team's games"
                lab = f"{label}/G"
            else:
                head = f"{label} — min {pct}% of team's games"
                lab = label
            blocks.append((f"E{row}", [[head, "", ""]]))
            blocks.append((f"E{row + 1}", [["Player", "Team", lab]]))
            blocks.append((
                f"E{row + 2}",
                [[f"=QUERY(Players!$A$2:$U,\"select B,C,{rate_col} where P = TRUE and {rate_col} > 0 order by {rate_col} desc limit 10 label B '',C '',{rate_col} ''\",0)"]],
            ))
            row += 14
        ld.batch_update(
            [{"range": c, "values": v} for c, v in blocks],
            value_input_option="USER_ENTERED",
        )
        # ---- colour the roster flags on Game Stats: red = not on the roster
        # (quick fix), yellow = rostered somewhere else or ambiguous
        try:
            gs = self.nc.worksheet(g)
            meta = self.nc.fetch_sheet_metadata(
                {"fields": "sheets(properties.sheetId,conditionalFormats)"})
            has_rules = any(
                sh_["properties"]["sheetId"] == gs.id and sh_.get("conditionalFormats")
                for sh_ in meta.get("sheets", []))
            if not has_rules:
                rng = {"sheetId": gs.id, "startRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": 20}
                def rule(formula, colour, i):
                    return {"addConditionalFormatRule": {"index": i, "rule": {
                        "ranges": [rng],
                        "booleanRule": {
                            "condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": formula}]},
                            "format": {"backgroundColor": colour}}}}}
                self.nc.batch_update({"requests": [
                    rule('=$S2="UNROSTERED"', {"red": 0.96, "green": 0.6, "blue": 0.6}, 0),
                    rule('=OR($S2="WRONG TEAM",$S2="AMBIGUOUS")',
                         {"red": 1.0, "green": 0.9, "blue": 0.5}, 1),
                ]})
        except Exception as e:
            log.warning("couldn't colour Game Stats flags: %s", e)
        return True, "Bot Teams, Players, Leaderboard, Team Stats, Player Search and Leaders built"

    @staticmethod
    def format_time(when: datetime) -> str:
        """Match what's already in the sheet: '9/24 10pm EST', '9/26 12AM EST'."""
        from .times import EASTERN

        e = when.astimezone(EASTERN)
        hour = e.strftime("%I").lstrip("0") or "12"
        mins = "" if e.minute == 0 else f":{e.minute:02d}"
        ampm = e.strftime("%p")
        # the sheet writes midnight as 12AM, everything else lowercase
        ampm = ampm if hour == "12" and ampm == "AM" else ampm.lower()
        return f"{e.month}/{e.day} {hour}{mins}{ampm} EST"
