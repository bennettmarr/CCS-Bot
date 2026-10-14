# CCS Season 9 Bot

Scheduling, score reporting and thread lifecycle for the Collegiate Conference
Series. Runs on the Windows box; everything it needs to remember is on disk, so
a reboot or a power cut loses nothing.

---

## Getting it running

**1. Install Python 3.11 or newer** from python.org. Tick *Add Python to PATH*.

**2. Unzip this folder** somewhere sensible, e.g. `C:\ccsbot`.

**3. Install the two libraries:**

    pip install -r requirements.txt

**4. Make your config:** copy `config.example.json` to `config.json` and put the
bot token in it. Everything else is already filled in — your server, channel,
admin role and log channel IDs, and the ballchasing key slot.

**5. Start it:**

    python main.py

Or double-click `run.bat`, which restarts the bot automatically if it ever
stops.

You should see `logged in as CCS Bot#5350` and a "bot started" line in your log
channel.

---

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/schedule <time>` | either team | Propose a time. The other team gets pinged with a Confirm button. |
| `/report <winner> <score>` | either team | Submit the result. Needs a confirmed time first. Attach the `.replay` files **or** give a ballchasing group link — one or the other is required. |
| Wrong time button | either team | Declines the proposal, or withdraws your own. Clears it so a new one can be proposed. |
| Dispute button | either team | Pings admins, flags the match. Result stays in standings until ruled on. |
| `/overflow [week]` | admin | Create threads for matches with no channel. |
| `/overdue [week]` | admin | Find matches past their deadline, open threads for any with nowhere to talk, ping both teams, post a summary. |
| `/override <winner> <score> <reason>` | admin | Correct a reported result. Also clears any dispute. |
| `/forfeit <winner> <reason> <proof>` | admin | Record a forfeit. The screenshot is required. |
| `/resolve <reason>` | admin | Clear a dispute and let the result stand. |
| `/clear [what]` | admin | Wipe a match back to unscheduled — time, result, or both. Un-archives the thread if it had locked. |
| `/extend <deadline> <reason>` | admin | Give one match a later deadline. |
| `/week [number]` | admin | Show or set the current week. |
| `/status [week]` | anyone | Counts of not scheduled / scheduled / reported. |

Everything is free text. `purdue`, `Purdue`, `boilermakers` all work for the
winner because it only ever matches against the two teams in that match.
Scores take `3-1`, `3 - 1`, `3:1`. Times take `tuesday 8pm`, `tomorrow 7:30pm`,
`sat 10 pm` and are always Eastern.

---

## The weekly rhythm

1. Nation makes as many match channels by hand as he wants.
2. An admin runs `/overflow`. The bot creates a private thread for every match
   that doesn't already have a channel. Safe to run twice — it skips anything
   already covered.
3. Teams schedule and report in whichever they got. Both work identically.
4. 24 hours after a report the thread archives and locks.
5. Seven days later it's deleted, after the result is copied to the log channel.

Thread names carry the status: 🔴 not scheduled, 🟡 time set, 🟢 finished.

---

## How it finds the match

Threads are recorded when the bot makes them. Hand-made channels aren't, so the
bot reads the channel's permissions instead: if exactly two roles with access
map to teams, and those teams play each other this week, that's the match.

This means **Nation's channels must be locked to the two team roles**. Admin,
caster and staff roles are ignored, so they can be on there too.

---

## Running it as a service

So it comes back after a reboot without anyone logging in:

1. Download NSSM from nssm.cc and unzip it.
2. In an admin Command Prompt:

        nssm install CCSBot

3. Path: your `python.exe`. Startup directory: the bot folder.
   Arguments: `main.py`.
4. On the *Exit actions* tab leave it set to restart.
5. `nssm start CCSBot`

Also set the BIOS to power on after AC loss, so the machine itself comes back
after an outage.

---

## Resetting a match

`/clear` puts a match back how it started, on the sheets as well as in
Discord — the score cells and the caster time are blanked, so a cleared match
looks like one that was never played.

It `everything` wipes the time, the
result, any dispute and any extension; `time only` and `result only` do what
they say. The thread is renamed to match and un-archived if the lifecycle loop
had already locked it.

Built for testing, but it's also the honest answer when a match gets reported
into the wrong thread and you want it genuinely blank rather than overridden.

---

## Reporting with replay files

`/report` takes the replay files directly. Attach up to five as `game1` to
`game5` and the bot reads them itself — scoreline, and every player's goals,
assists, saves, shots and score.

Everything it needs lives in the replay's header, which is just metadata the
game writes for its own replay browser. The network stream — boost, positioning,
demos — is never touched, which is why this is fast and why it doesn't break
every patch.

That means no ballchasing account, no upload quota and no subscription. Uploading
~800 replays a week centrally would need a paid tier; reading the files costs
nothing. It also removes a dependency: reporting keeps working when ballchasing
is down.

Steam players are identified by their Steam ID, Epic players by their Epic
account ID — which is the same 32-character value ballchasing reports, so roster
matching works the same either way.

A ballchasing group link still works and is still worth having for watching a
series back. One or the other is required; the bot checks the score against
whichever it gets.

---

## Order of play

A match can't be reported until a time has been confirmed by both teams. A
result with no agreed time means nobody can check the series happened when
both sides expected it, which is the shape most scheduling arguments take.

Admins can report without one — forfeits, and matches sorted out elsewhere —
and the log line and the embed both say when that happened.

---

## Forfeits

`/forfeit` awards the win (3-0 by default) and records **why**, with a
screenshot that is not optional. Forfeits are what gets argued about weeks
later, and a reason with no evidence behind it is worth little when it is.

The screenshot is copied into the log channel and the result stores that copy's
link, not the original — a Discord attachment dies with the message it was
posted in, and match threads are deleted a week after archiving.

A forfeit is marked as one everywhere: `(FF)` in the thread name, `FORFEIT:`
with the reason and proof link on the Bot Results tab, and a distinct line in
the log. No stats are pulled, because no games were played. Both teams are
pinged and the Dispute button is there as usual.

---

## Disputes

A disputed match is frozen: it never archives and never gets deleted, so the
thread stays open while it's looked at. Two ways out —

    /resolve   the result stands, with your ruling recorded
    /override  the result was wrong, replace it

Either way the 24-hour archive clock restarts from the ruling, so the teams get
a window to see it before the thread locks. `/status` lists how many are
outstanding.

---

## Chasing a proposal

A proposed time nobody has answered gets a nudge every 2 hours, pinging only
the team that owes a reply. It stops after 3 nudges so it doesn't nag all week.
Both figures are config — `proposal_nudge_hours` and `proposal_nudge_max`.

If the proposed time has itself passed by then, the nudge says so and asks for
a new one instead.

---

## The calendar

Weeks run **Sunday to Saturday**. Week 1 is 20–26 September, week 2 is
27 September – 3 October, and so on from `week1_start`.

The bot works out the current week from the date, so nobody has to remember to
change it. `/week 3` pins it if you ever need to override; setting
`current_week` back to `null` in the config returns it to automatic.

**1am Sunday** the bot sweeps the week that just ended. Every match without a
score is flagged overdue: a thread is opened for any that has nowhere to talk,
both teams get pinged, and admins get the full list. Nothing is forfeited.

---

## Deadlines

`week1_start` in the config is the Sunday week 1 begins — currently
`2026-09-20`. The deadline for week N is the Saturday of that week at 11:59pm
Eastern. `/extend` overrides it for a single match.

Nothing is ever forfeited automatically. `/overdue` reports and nudges; the
ruling stays with admins.

---

## Files

    config.json        your settings and keys - never share this
    data/teams.json    202 teams -> Discord role ids
    data/fixtures.json 1010 matches with their sheet cells
    data/state.json    live progress, written after every change
    ccsbot.log         rolling log

`state.json` is the one that matters. Back it up if you're nervous; deleting it
makes the bot forget which thread belongs to which match.

---

## Replay links on the sheet

When a result is written, the dash between the two scores becomes a link to
that series' ballchasing group — click it and the replays open. Clearing a
match puts a plain dash back.

The dash is used rather than the score cells because nothing reads it. Putting
a formula in a score cell would put it in front of the standings formulas,
which is not a risk worth taking for a hyperlink. Turn it off with
`link_scores: false` if it ever gets in the way.

---

## Fixtures

The bot holds 1,892 fixtures: 1,010 non-conference across weeks 1-6, and 882
conference games across weeks 7-16 for the 21 smaller conferences.

The four power conferences' schedules already live in the main sheet's Group
Stage and aren't loaded here, so `/fixtures` shows a Big 12 or ACC team's
non-conference games only. They'll appear once those schedules exist as data.

Conference fixtures also carry no sheet cells yet, because the week blocks
haven't been built in the main sheet. Threads, scheduling and reporting all
work for them; the score just can't be written to the grid until placement is
done. The Bot Results and Game Scores tabs record it either way.

---

## Roster checking

The Roster Sheet is read at startup. Every stat line is then checked against
it and flagged on the Game Stats tab:

    UNROSTERED   that identity isn't on the roster sheet at all
    WRONG TEAM   rostered to one team, played for another
    AMBIGUOUS    a display name matching more than one roster entry

Flags are reported in the log with an admin ping when a report brings one in,
so you hear about it that night rather than in week 9. Nothing is blocked —
eligibility is a human call.

Steam links carry the numeric id ballchasing also reports, so those match
exactly. Epic and PSN tracker links carry a display name instead, so those
fall back to matching on name and are marked as the weaker match they are.

`/roster` reloads the sheet and lists teams with fewer than three tracker
links, which is the list of people to chase.

---

## The results tab

Every reported result also lands on a **Bot Results** tab in the non-conference
sheet, made automatically on first use. One row per match, updated in place
rather than appended, so a corrected result replaces its own row instead of
doubling up.

Columns: fixture id, week, both teams, both scores, winner, the ballchasing
group link, who reported it and for which team, the agreed time, when it was
last touched, and a note for overrides and clears.

It's written even when the write into the schedule grid fails — that's exactly
when having the result recorded somewhere matters. It's also where per-player
stats will be built from, since it keeps every group link.

---

## Not wired up yet

**Google Sheets.** `sheets_enabled` is `false`. Until a service account exists,
confirmed times and scores are stored in `state.json` but not written to the
sheets. Turning it on is a config change, not a rewrite — every fixture already
carries the exact cells its scores belong in.

**FPI.** Ratings come once results are flowing.

## /backfill — recording results already posted

`/backfill channel:#report-scores` reads every post in the channel since week 1
(or `since:YYYY-MM-DD`), finds the ones with `.replay` files attached or a ballchasing group link, and works
out which fixture each is from three clues: the poster's team role, the names
either side of the score ("UC 3-0 Xavier"), and the players in the replays
looked up on the Roster Sheet. Two have to agree. It checks the replays add up
to the score in the text, then sends back `backfill.txt` listing what will be
written and what needs doing by hand. Nothing is written until you run it again
with `apply:true`.

Needs Message Content Intent switched on in the Developer Portal **and**
`"message_content_intent": true` in config.json. Portal first — if the config
asks for it and the portal hasn't granted it, the bot won't connect.

The whole season is searched, so a straggler finished weeks late is still found — but nearly every series is played in its own week, so when two fixtures fit a post equally well the one in the posting week (or the week before) wins. Anything placed more than a week from when it was posted is marked CHECK in the report. Where two teams meet twice (conference play), the first unplayed meeting is filled first.

Safe to run twice: recorded matches are skipped. A match posted twice uses the
later post. Writes are paced (`backfill_pace_seconds`, default 5) to stay inside
Google's quota, so 150 results takes about 15 minutes; progress goes to the log
channel.

Teams swap colours between games, so everything that counts games — the series
score, which team each player line belongs to, the ballchasing check — works
out the two sides from the players themselves (the same accounts game after
game), never from blue/orange. A series posted across several messages (one
replay per post) is joined back up when it's the same poster within 30 minutes
and what came before wasn't already a finished series.

### data/aliases.json

Short forms that follow no rule — "UVA" is Virginia, "UNCO" is Northern
Colorado, "UNCC" is Charlotte. Lower-case key, exact team name from teams.json.
Rule-following ones (UT, UH, URI, UNH, IU, UCI, UTA, BGSU, MTSU...) are worked
out automatically and don't need listing. Add anything the backfill report
shows it didn't understand, then restart.

### Undoing a result outside a match channel

`/clear team:Utah opponent:Texas` resets the most recent recorded meeting of
those two teams and wipes it from every tab it touched.
