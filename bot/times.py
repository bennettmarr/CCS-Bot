"""
Times are typed freely by players and always mean Eastern.

Everything is stored in UTC and shown to Discord as a <t:...> timestamp, which
renders in each viewer's own timezone automatically - so a team in California
sees their own clock without anyone converting anything.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

DAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse(text: str, now: datetime = None) -> Optional[datetime]:
    """
    Accepts the sort of thing people actually type:
      "tuesday 8pm"  "tue 8:30 pm"  "8pm friday"  "today 9pm"
      "tomorrow 7:30pm"  "sat 10 pm"  "20:00 thursday"
    Returns an aware UTC datetime, or None if it can't be read.
    """
    if not text:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    s = text.strip().lower().replace(".", "")

    # time of day
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s)
    if m:
        hour = int(m.group(1)) % 12
        minute = int(m.group(2) or 0)
        if m.group(3) == "pm":
            hour += 12
    else:
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None

    # which day - an explicit date wins over everything else
    target = None
    MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    # 9/27, 9-27, 9/27/26, 9/27/2026
    dm = re.search(r"\b(\d{1,2})\s*[/-]\s*(\d{1,2})(?:\s*[/-]\s*(\d{2,4}))?\b", s)
    if dm:
        mth, day = int(dm.group(1)), int(dm.group(2))
        yr = dm.group(3)
        if yr:
            yr = int(yr)
            yr += 2000 if yr < 100 else 0
        else:
            yr = now.year
        try:
            cand = now.date().replace(year=yr, month=mth, day=day)
            # "1/3" typed in December means next year
            if not dm.group(3) and cand < now.date() - timedelta(days=180):
                cand = cand.replace(year=yr + 1)
            target = cand
        except ValueError:
            return None            # 13/45 and friends

    # sept 27, 27 sept, september 27th
    if target is None:
        mm = re.search(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\.?\s+"
            r"(\d{1,2})(?:st|nd|rd|th)?\b",
            s,
        ) or re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\.?\b",
            s,
        )
        if mm:
            a, b = mm.group(1), mm.group(2)
            mth = MONTHS[a[:4] if a[:4] in MONTHS else a[:3]] if a.isalpha() else MONTHS[b[:4] if b[:4] in MONTHS else b[:3]]
            day = int(b) if a.isalpha() else int(a)
            try:
                cand = now.date().replace(month=mth, day=day)
                if cand < now.date() - timedelta(days=180):
                    cand = cand.replace(year=cand.year + 1)
                target = cand
            except ValueError:
                return None

    if target is not None:
        pass
    elif "tomorrow" in s:
        target = now.date() + timedelta(days=1)
    elif "today" in s or "tonight" in s:
        target = now.date()
    else:
        for word, idx in DAYS.items():
            if re.search(rf"\b{word}\b", s):
                ahead = (idx - now.weekday()) % 7
                cand = now.date() + timedelta(days=ahead)
                # "monday 8pm" said on monday morning means today, not next week
                if ahead == 0:
                    trial = datetime.combine(cand, datetime.min.time(), EASTERN).replace(
                        hour=hour, minute=minute
                    )
                    if trial < now:
                        cand = cand + timedelta(days=7)
                target = cand
                break
    if target is None:
        target = now.date()
        trial = datetime.combine(target, datetime.min.time(), EASTERN).replace(
            hour=hour, minute=minute
        )
        if trial < now:
            target = target + timedelta(days=1)

    dt = datetime.combine(target, datetime.min.time(), EASTERN).replace(
        hour=hour, minute=minute
    )
    return dt.astimezone(timezone.utc)


def pretty(dt: datetime) -> str:
    """Windows has no %-d / %-I, so assemble it rather than format it."""
    e = dt.astimezone(EASTERN)
    hour = e.strftime("%I").lstrip("0") or "12"
    return (
        f"{e.strftime('%a %b')} {e.day}, {hour}:{e.minute:02d} "
        f"{e.strftime('%p')} ET"
    )


def discord_ts(dt: datetime, style: str = "F") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def short(dt: datetime) -> str:
    """Compact label for a thread name, e.g. 'Tue 8pm'."""
    e = dt.astimezone(EASTERN)
    hour = e.strftime("%I").lstrip("0") or "12"
    mins = "" if e.minute == 0 else f":{e.minute:02d}"
    return f"{e.strftime('%a')} {hour}{mins}{e.strftime('%p').lower()}"
