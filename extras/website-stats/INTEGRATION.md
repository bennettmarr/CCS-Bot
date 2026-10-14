# Hooking stats_sheet.py into /report

1. Copy `stats_sheet.py` into the bot folder (both installs: laptop + desktop).
2. Share **CCS Season 9 Website Stats** (Google Drive) as *Editor* with the bot's
   service-account email (the `client_email` in its JSON key). Share it as *Viewer*
   with the website person.
3. `pip install gspread` if the bot doesn't already use it.
4. In the bot startup:

```python
from stats_sheet import StatsSheet, STATS_SHEET_ID
stats = StatsSheet.from_service_account("service_account.json", STATS_SHEET_ID)
stats.ensure_headers()
```

5. At the end of `/report`, after the replays are parsed and the existing sheet write succeeds:

```python
from stats_sheet import SeriesReport, GameReport, PlayerLine

report = SeriesReport(
    week=week, match_type="Non-Conference",        # or "Conference"
    team_a=home, team_a_conf=conf_of[home],
    team_b=away, team_b_conf=conf_of[away],
    games=[GameReport(game_number=i + 1, players=[
              PlayerLine(name=p.name, team=p.team, goals=p.goals, assists=p.assists,
                         saves=p.saves, shots=p.shots, score=p.score, demos=p.demos,
                         mvp=p.mvp, player_id=p.platform_id)
              for p in game.players])
           for i, game in enumerate(parsed_games)],
    ballchasing_url=group_url, reported_by=str(interaction.user),
)
try:
    await asyncio.to_thread(stats.record_series, report)
except Exception as e:
    log.exception("website stats write failed: %s", e)   # never block the report
```

If you already have ballchasing replay JSON, `game_from_ballchasing(replay, n, blue, orange)`
builds each `GameReport` for you.

## Notes
- Re-reporting a series (same week + same two teams) replaces the old rows.
  Pass `series_id=` yourself if two teams can meet twice in one week.
- `stats.delete_series(series_id)` removes a bad report.
- Backfill: loop over already-reported series and call `record_series` for each.
- Tunables at the top of the file: `TOP_N` (20), `MIN_GP_RATE_STATS` (5 games to
  qualify for per-game / shooting % leaderboards).
