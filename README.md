# Garfie

Discord scheduled-image bot with a local administration dashboard.

## Setup (Raspberry Pi / Linux)

1. Install Python 3.11+ and create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env`, fill in the Discord bot token, destination channel ID, and a long random dashboard token. Keep `.env` private.
3. Run from the repository root: `python bot.py`.
4. The dashboard binds to `127.0.0.1:8080` by default. Access locally or via an authenticated SSH tunnel. Do not expose port 8080 directly to the internet.
5. The dashboard currently authenticates via a URL query token. Avoid sharing dashboard URLs, and prefer a private SSH tunnel. Consider replacing query-string authentication with secure sessions before public deployment.

## Included schedule

A fresh database automatically seeds **Monday** (`images/monday.jpg`) and **Friday** (`images/friday.jpg`) with the original **09:00–14:59 America/New_York** posting windows. Their most recently recorded selected times are preserved for reference in `config/weekly_seed.json`; those dates are historical, so future selections remain random. The `images/default.jpg` fallback is also included.

Other weekday entries, recurring rules, special-day overrides, messages, and Discord IDs from the original database were intentionally excluded from the public release. Existing local databases are not overwritten by the seed.

## Security and operations

- Never commit `.env`, `schedule.db`, or `garfie.log`.
- Rotate any credentials previously shared or exposed.
- Dashboard form endpoints modify schedules and can trigger Discord posts. Keep the dashboard private.
- The current URL query-token scheme can leak tokens via copied URLs and logs; replace with a session-based login before internet-facing deployment.
- A full production deployment should add a systemd unit with `WorkingDirectory` set to this project directory.

## Source layout

- `bot.py`: Discord startup and cog loading
- `cogs/recurring_image.py`: scheduler and slash commands
- `webserver.py`: dashboard API and HTML rendering
- `template.html`: dashboard layout
- `config/weekly_seed.json`: public starter schedule
- `images/`: starter image assets
