# CCS Bot — Handoff (Oct 5, 2026)

Paste this into a new chat to pick up where we left off. The latest bot code is `ccsbot.zip`; the sheet scripts are `conference_schedules.gs` and `rebuild_raw_data.gs`. Re-upload any of these if needed.

## What this is
Bennett (Discord: Marr, MSU role) builds a from-scratch Discord bot for the **Collegiate Conference Series (CCS)**, a 202-team, 25-conference collegiate Rocket League league run by commissioner **Nation**. The bot handles match threads, scheduling, score reporting, replay/ballchasing parsing, roster checks, Google Sheets writes, and player/team stats.

## Where things run
- **Laptop (current):** `C:\path\to\old\ccsbot`
- **Desktop:** `D:\CCS Bot\ccsbot`
- Only run **one** at a time. When switching machines, copy `config.json`, `service_account.json`, `data\state.json` and `data\identities.json` across. The zip never contains these, so updates are always safe to unzip over the folder.
- The bot token was reset on the laptop, so the desktop's `config.json` needs the new token before it's used again.
- Start with `python main.py`. Every command is run in Discord, not cmd.

## Current mode
- **Stats-only** on the real CCS server (guild `1151295157363290125`, admin role `1151296718571319416`), writing to **Bennett's own "Copy of…" sheets**: Non Conference, Caster Sheet, Collegiate Conference Series Season 9 (Group Stage). The log channel is still on the test server.
- Results channel: `#stats-replays-or-bcgroup` (`1155041977222570035`).
- Command replies are now **public** by default; "Admins only." refusals stay private. To change this, set `"public_replies": false` in config.

## Bot commands that matter
- `/backfill channel:#stats-replays-or-bcgroup` runs a dry run and writes `backfill.txt`. Add `apply:true` to write. It's idempotent: already-recorded results are skipped.
- `/report winner: opponent: score: [ballchasing:]` works from any channel, and admins can report for any two teams.
- `/clear team: opponent: what:result` undoes a result.
- `/stats action:build` builds the stat tabs. `/stats action:relabel` re-decides every stat line's team from the Roster Sheet.
- `/sheets check` confirms which sheets the bot is on.

## Done this session
- **Backfill:** 247 series recorded, weeks 1–3. Matching rules: write when any two of poster role / post text / replay roster agree; a disagreeing roster gets **flagged, not blocked**. Missing replays are saved and flagged when the replays fit the score. Text naming both teams plus replays confirming one side overrides a wrong roster. New aliases: `unt`, `uark`.
- **Flags:** a red Note on Bot Results means only unrostered players (quick roster fix). Yellow means it needs a look (missing replay, roster disagreement, incomplete replay). Game Stats rows are red for UNROSTERED and yellow for WRONG TEAM / AMBIGUOUS.
- **Stat tabs** (on the Non Conference copy): Bot Teams, Players, Leaderboard (Stat / Total or Per game / Show / Conference / Team dropdowns), Team Stats, Player Search, Leaders. Shooting % and Win % (games won ÷ games played) are added. Rate and percent boards require ≥25% of team games. The bot **never overwrites a tab it didn't make**; after a "Teams" collision, which was restored from version history, its own tab is now "Bot Teams".
- **Roster-first team labelling (latest zip):** a player rostered to one of the two teams in a match gets that team; sides are named by their rostered players; subs take their side's team. The Players tab shows the team played for most. `/stats action:relabel` fixes existing lines (it fixed the Pyro→BYU and TeddyZio→Maryland cases in testing).
- **Conference schedules:** all 21 remaining conferences transcribed from Nation's screenshots and verified (each pair meets the right number of times, max 2 games per team per week). Start weeks: most at **7**; Horizon, Summit, Ohio Valley and Missouri Valley at **9**; Patriot and West Coast at **11**. All end at 16. The bot's `fixtures.json` conference games were replaced to match (only 185 of 882 had matched before). `data/conferences.json` maps all 202 teams to conferences.
- **Group Stage (main sheet), done and audited clean:** week boxes built (`conference_schedules.gs` → `buildAll`). Raw Data rebuilt as one table (Week | ID | Team | Wins | Losses | Series Result | Opponent | Conference), 1,354 games. Sorting formulas reset on all 202 team rows. Standings links realigned (including the UAC `#REF!` cells). `auditGroupStage` reports **No issues found**. If boxes are rebuilt, re-run in order: `rebuildRawData` → `fixSortingFormulas` → `fixStandingsLinks` → `auditGroupStage`.

## Still to do (manual)
- Enter: **Kentucky 3-0 Miami** (wk1: 2-0, 2-1, 4-1), **Liberty 0-3 Rhode Island** (wk2: 1-5, 0-2, 1-3), **WSU 3-0 Houston Christian** (wk4 fixture played early: 5-3, 4-0, 4-3).
- Ask the teams: **Tennessee vs UTA** (posted 3-1 but the replays are 2-2, likely 3-2 with game 5 missing); **UND's no-text post** (week 2 opponent: San Diego State or Utah Tech?); **Boston–Butler game 4 score** (series is saved, one replay missing).
- **Roster Sheet fixes** (players listed under the wrong team): UTA's under Arkansas Little Rock, Charlotte's under Creighton, Houston's under Cincinnati, Akron's under Old Dominion, Loyola's under Army, Rhode Island's under Dayton, BYU's under UCF, Robert Morris's under Towson, one Central Arkansas player under Binghamton. 15 teams have no players on the roster.
- After installing the latest zip: run `/stats action:relabel`, then `/stats action:build`, then have Nation re-check Players (Pyro, TeddyZio, HCU, UCI, Michigan).

## Next build items
1. **Discord names on stats:** read the Discord name that sits next to each tracker on the Roster Sheet and add a Discord column to Game Stats, Players, Player Search and Leaderboard. **Waiting on** a Roster Sheet screenshot with column letters, to confirm which side of the tracker the player name and the Discord name are on. The parser currently takes the nearest text to the **left** of the tracker as the player name.
2. **Conference scheduling and reporting from week 7** (stats-only stays for weeks 1–6, stats for everything). The bot reads score-cell positions off Group Stage (offsets from a box's home-name cell: home score +19, dash +21, away score +22, away name +29; first header row 108). **Decisions needed from Bennett/Nation:** match-thread channel(s) on the real server (about 135 conference matches a week; one channel or one per conference); auto-open each week or admin-triggered; deadlines and overdue rules (rulebook: pings vs forfeits); the 19 teams with no Discord role (Bowling Green, CSU Fullerton, Cal, Connecticut, BYU/Cougars, Georgia State, Houston, Loyola, Charlotte/Niner, Pitt, South Dakota State, St Joseph's, St Louis, Tulsa, UCSD, UIC, UMBC, UNM, WSU); **live-sheet cutover before week 7** (needs Nation's OK); admin pings and log channel on the real server.
3. **Optional:** a live results feed or reactions on results-channel posts (✅/❓/⚠️ as they're posted); a minimum-games floor on per-game leaderboards; `/report` posts for the 3 manual entries.

## Related projects (separate chats/memory)
- **CCS website:** reads straight from the Google Sheet, with tabs shaped for the site.
- **KenPom-style CRL ratings:** feeds a Top 25.
