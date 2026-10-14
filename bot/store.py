"""
Everything the bot needs to remember, and the only place that touches disk.

Three static files loaded at startup:
  data/teams.json     202 teams -> Discord role id
  data/fixtures.json  1010 matches, each with the sheet cells for its scores
  config.json         ids, timings, keys

One live file:
  data/state.json     per-match progress, written after every change

State is on disk rather than in memory so a restart, a power cut or a Windows
update never loses a pending schedule or a pending archive. The RLPC bot kept
approvals in memory and lost them on restart; this one does not.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

_lock = threading.Lock()


def _load(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Store:
    def __init__(self, config_path: str = None):
        self.config_path = config_path or os.path.join(ROOT, "config.json")
        self.config: Dict[str, Any] = _load(self.config_path)
        if self.config is None:
            raise SystemExit(
                "No config.json found. Copy config.example.json to config.json "
                "and fill in the token."
            )
        self.teams: Dict[str, Dict] = _load(
            os.path.join(DATA, self.config.get("teams_file", "teams.json")), {}
        )
        self.fixtures: List[Dict] = _load(
            os.path.join(DATA, self.config.get("fixtures_file", "fixtures.json")), []
        )
        self.state: Dict[str, Dict] = _load(os.path.join(DATA, "state.json"), {}) or {}

        # lookups
        self.by_id = {f["id"]: f for f in self.fixtures}
        self.role_to_team = {
            int(v["role_id"]): k for k, v in self.teams.items()
        }

    # ---------- config helpers ----------
    def cfg(self, key, default=None):
        return self.config.get(key, default)

    @property
    def week(self) -> int:
        """
        Which match week we're in. Derived from the calendar unless
        current_week is set to a number in config, which pins it.
        Weeks run Sunday to Saturday; week 1 starts on week1_start.
        """
        pinned = self.config.get("current_week")
        if pinned:
            return int(pinned)
        return self.week_for(now_utc())

    def week_for(self, when) -> int:
        from datetime import date, timedelta
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo(self.cfg("timezone", "America/New_York"))
        start = date.fromisoformat(self.cfg("week1_start", "2026-09-20"))
        today = when.astimezone(eastern).date()
        if today < start:
            return 1
        return (today - start).days // 7 + 1

    def set_week(self, w: int):
        self.config["current_week"] = int(w)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    # ---------- fixtures ----------
    def set_cfg(self, key: str, value) -> None:
        """Change one setting and save config.json (same way /week does)."""
        self.config[key] = value
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def week_fixtures(self, week: int = None) -> List[Dict]:
        week = self.week if week is None else week
        return [f for f in self.fixtures if f["week"] == week]

    def find_fixture(self, team_a: str, team_b: str, week: int = None) -> Optional[Dict]:
        pair = {team_a, team_b}
        for f in self.week_fixtures(week):
            if {f["home"], f["away"]} == pair:
                return f
        return None

    def team_fixtures(self, team: str) -> List[Dict]:
        """Every match this team plays, in week order."""
        out = [f for f in self.fixtures if team in (f["home"], f["away"])]
        return sorted(out, key=lambda f: (f["week"], f["id"]))

    def team_role_id(self, team: str) -> Optional[int]:
        t = self.teams.get(team)
        return int(t["role_id"]) if t else None

    def team_for_role(self, role_id: int) -> Optional[str]:
        return self.role_to_team.get(int(role_id))

    # ---------- per-match state ----------
    def get(self, fixture_id: str) -> Dict:
        return self.state.setdefault(
            fixture_id,
            {
                "status": "unscheduled",   # unscheduled | scheduled | reported | archived
                "thread_id": None,
                "channel_id": None,
                "proposed": None,          # {"by_team":..., "iso":..., "message_id":...}
                "scheduled_iso": None,
                "result": None,            # {"winner":..., "home":n, "away":n, "link":..., "by":id, "iso":...}
                "reported_at": None,
                "archived_at": None,
                "disputed": False,
                "extended_to": None,
                "sheet_written": False,
                "caster_written": False,
            },
        )

    def save(self):
        with _lock:
            tmp = os.path.join(DATA, "state.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, os.path.join(DATA, "state.json"))

    def set(self, fixture_id: str, **kw):
        s = self.get(fixture_id)
        s.update(kw)
        self.save()
        return s

    def by_thread(self, thread_id: int) -> Optional[Dict]:
        for fid, s in self.state.items():
            if s.get("thread_id") == thread_id:
                return self.by_id.get(fid)
        return None

    def by_channel(self, channel_id: int) -> Optional[Dict]:
        for fid, s in self.state.items():
            if s.get("channel_id") == channel_id:
                return self.by_id.get(fid)
        return None


    # ---------- deadlines ----------
    def deadline(self, week: int) -> datetime:
        """
        End of the match week: the Saturday of that week at 11:59pm Eastern.
        Week 1 starts on the Sunday given by week1_start in config.
        """
        from datetime import date, timedelta
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo(self.cfg("timezone", "America/New_York"))
        start = date.fromisoformat(self.cfg("week1_start", "2026-09-20"))
        saturday = start + timedelta(days=7 * (int(week) - 1) + 6)
        return datetime.combine(saturday, datetime.min.time(), eastern).replace(
            hour=23, minute=59
        )

    def overdue(self, upto_week: int = None) -> list:
        """Matches whose deadline has passed with no result recorded."""
        now = now_utc()
        upto = upto_week if upto_week is not None else self.week
        out = []
        for f in self.fixtures:
            if f["week"] > upto:
                continue
            st = self.get(f["id"])
            if st["status"] in ("reported", "archived"):
                continue
            due = self.deadline(f["week"])
            ext = st.get("extended_to")
            if ext:
                due = datetime.fromisoformat(ext)
            if now > due:
                out.append((f, st, due))
        return out


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
