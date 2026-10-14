# Test server run-through

Your test server, four teams, five fixtures. Everything else identical to
production.

## Start it

    copy config.test.json config.json

Paste your bot token into `config.json`, then:

    python main.py

You should see `logged in as CCS Bot#5350` and a "bot started" line in your
bot-log channel. Slash commands appear within a few seconds.

Ballchasing verification is **off** in this config, so you can report any
score with any link. Turn `verify_ballchasing` back on when you want to test
that part, and paste a real group link.

## The fixtures

| Week | Match | Why it's there |
| --- | --- | --- |
| 1 | Michigan State vs Purdue | deadline already passed — use this for `/overdue` |
| 2 | Baylor vs Wichita State | a real CCS fixture, real sheet cells |
| 2 | Michigan State vs Baylor | current week |
| 2 | Purdue vs Wichita State | current week |
| 3 | Baylor vs Purdue | future |

Today falls in week 2, so week 1 is already overdue.

## Run through it

**1. Threads**

    /overflow

Four threads appear under your scheduling channel, all named `🔴 A vs B`
(the week 3 one is skipped — it only does the current week). You get added to
all of them, since you hold all four roles.

**2. Scheduling**

In one thread:

    /schedule tuesday 8pm

Confirm button appears, other team pinged. Press Confirm — it should
**refuse**, because you hold the proposing team's role too. That's the check
working. To confirm properly, use `/override`-style admin rights: you're an
admin, so pressing it as admin is allowed only if you're not on the proposing
side — easiest test is to temporarily remove one team role from yourself.

Once confirmed, the thread renames to `🟡 A vs B · Tue 8pm`.

Try junk too: `/schedule blah` should refuse, `/schedule yesterday 8pm`
should refuse as being in the past.

**3. Reporting**

    /report winner:purdue score:3-1 ballchasing:https://ballchasing.com/group/test

Thread renames to `🟢 Purdue 3-1 ...`. A Dispute button appears.

Try the refusals: `winner:ohio state` (not in this match), `score:4-1` and
`score:2-1` (not a best of five), `score:abc`.

**4. Overdue**

    /overdue

Finds Michigan State vs Purdue from week 1, opens a thread if it doesn't have
one, pings both teams, posts the summary to your bot-log channel.

**5. Admin bits**

    /status
    /extend deadline:monday 11pm reason:testing
    /override winner:baylor score:3-0 reason:testing
    /week

**6. Restart test**

Schedule something, stop the bot with Ctrl-C, start it again, then press the
Confirm button on the old message. It should still work — that's the
persistent view doing its job.

## What you can't test here

The Sunday 1am sweep only fires on a Sunday. To force it early, set
`week1_start` forward a week so week 1 looks like the week that just ended,
then restart and run `/overdue`, which does the same work.

## The test spreadsheet

`CCS_TEST_Sheet.xlsx` is a throwaway sheet built to the same geometry as the
real Non-Conference tab — same column bands, same score cells. Upload it to
Google Sheets as your own copy.

The fixtures in `fixtures.test.json` point at the real cells in it, so when
Sheets is switched on the bot writes into your copy exactly the way it would
write into the league's.

**Both config files now have empty sheet IDs and `sheets_enabled: false`.** The
league's sheet IDs have been removed entirely, so there's nothing to write to
by accident. When you're ready, paste *your copy's* ID into
`non_conference_sheet_id` and nothing else.

Yellow cells are the ones the bot writes. Everything else is scenery.

---

## Going live

Change these three back in `config.json`:

    "guild_id"                the real CCS server
    "teams_file"              "teams.json"
    "fixtures_file"           "fixtures.json"

plus the real channel and admin role ids, `verify_ballchasing: true`, and
`delete_days_after_archive: 7`. Delete `data/state.json` so nothing from
testing carries over.
