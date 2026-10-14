# CCS Bot

A Discord bot that runs match scheduling, results and stats for the **Collegiate Conference Series (CCS)**, a college Rocket League league with roughly 200 teams across multiple conferences.

## What it does

- **Scheduling.** Opens a private thread for each weekly match. Teams propose a time with `/schedule`, the other team confirms with a button, and thread names show status at a glance (🔴 not scheduled, 🟡 time set, 🟢 finished).
- **Score reporting.** Teams report with `/report`, backed by `.replay` files or a ballchasing.com group link. Admins can override, forfeit, extend deadlines and resolve disputes.
- **Live results feed.** Anything posted in the results channel is picked up automatically, matched to the right series and recorded. The bot reacts ✅ / ⚠️ / ❓ / 🔁 so posters know what happened. If the bot was offline, it catches up on startup.
- **Stats.** Pulls player stats from ballchasing.com and writes them to Google Sheets: game stats, leaderboards and a website stats sheet that rebuilds after every result.
- **Roster checks.** Compares who actually played against the official roster sheet and flags wrong links, wrong teams and unrostered players, with suggested fixes.
- **Housekeeping.** Archives and cleans up threads on a timer, sweeps overdue matches and pings teams that haven't played.

## Built with

- Python 3.11, [discord.py](https://discordpy.readthedocs.io/)
- Google Sheets API (gspread + a Google service account)
- [ballchasing.com](https://ballchasing.com) API for replay stats

## Running it yourself

1. Install Python 3.11+.
2. `pip install -r requirements.txt`
3. Copy `config.example.json` to `config.json` and fill in your Discord bot token, server and channel IDs, and (optionally) a ballchasing API key.
4. For Google Sheets features, put a Google service account key at `service_account.json` and share your sheets with it.
5. `python main.py` (or `run.bat` on Windows, which auto-restarts the bot).

`config.json` and `service_account.json` hold secrets and are excluded from this repo by `.gitignore`.

## More detail

- [`REFERENCE.md`](REFERENCE.md): full command list and config reference
- [`TESTING.md`](TESTING.md): how to test against a test server
- [`START_HERE.txt`](START_HERE.txt): operator notes
- `extras/`: Google Apps Scripts for the league sheets and project notes

## About

Built by Bennett Marr using AI-assisted development.
