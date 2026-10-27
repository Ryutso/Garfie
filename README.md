# Garfie Baby!
Friday again, Garfie Baby!

Modeled after the Facebook page "Friday again, Garfie Baby!", this self-hosted Discord bot gives you the Garfie Friday and Monday treatment in your own Discord.

https://www.facebook.com/GarfieBaby

Garfie is a self-hosted Discord bot that posts images on a configurable schedule. It includes a browser-based administration dashboard for managing weekly posting days, time windows, messages, and schedule overrides.

Designed for Raspberry Pi and other Linux systems, Garfie can also run on any compatible machine with Python installed.

## Features

- Automatically posts configured images to a Discord channel
- Selects a random posting time within each configured weekday's time window
- Supports per-day images and accompanying messages
- Provides a local web dashboard to view and manage schedules
- Supports specific-date overrides and monthly recurring weekday rules
- Includes Discord slash commands for configuration and previews
- Stores schedules and posting state in a local SQLite database
- Retains scheduled times and posting state across restarts

## Requirements

- Python 3.11 or newer
- A Discord account with permission to create and invite a bot
- A Raspberry Pi, Linux server, or other computer capable of running Python
- Network access to Discord

Python dependencies are listed in [`requirements.txt`](requirements.txt).

## 1. Create a Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. Open its **Bot** settings and create or reset the bot token. Keep this token secret.
3. Enable **Message Content Intent** in the bot settings, as the current bot code requests this intent.
4. Under **Installation** or **OAuth2**, configure an invite with the `bot` and `applications.commands` scopes.
5. Give the bot permissions appropriate to the target channel, including **View Channel**, **Send Messages**, and **Attach Files**.
6. Invite the bot to your Discord server.

To find a channel ID, enable **Developer Mode** in Discord, right-click the destination channel, and choose **Copy Channel ID**.

## 2. Download and install

Clone or download this repository onto the machine that will run the bot. In a terminal, open the project directory and create a virtual environment:

```bash
git clone https://github.com/Ryutso/Garfie.git
cd Garfie

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If you downloaded the repository as a ZIP instead, extract it and run the commands beginning with `python3 -m venv .venv` inside the extracted folder. Git is not required to run the bot.

## 3. Configure environment variables

Copy the sample environment file:

```bash
cp .env.example .env
```

Edit `.env` with your own settings:

```dotenv
DISCORD_TOKEN=your-discord-bot-token
GARFIE_CHANNEL_ID=your-discord-channel-id
GARFIE_WEB_TOKEN=your-long-random-dashboard-secret
GARFIE_WEB_HOST=127.0.0.1
```

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord bot authentication token |
| `GARFIE_CHANNEL_ID` | ID of the channel where images will be posted |
| `GARFIE_WEB_TOKEN` | Secret token used to access the administration dashboard |
| `GARFIE_WEB_HOST` | Dashboard bind address; defaults to `127.0.0.1` |

Never publish the real `.env` file or share your bot token.

## 4. Start the bot

Run the following command from the repository root, with the virtual environment activated:

```bash
python bot.py
```

On startup, Garfie loads its cogs, synchronizes Discord slash commands, initializes its SQLite database if needed, and starts its scheduler and web dashboard.

Keep the process running for scheduled posts to occur. For an always-on installation, use a process manager such as systemd; configure its working directory to the Garfie repository directory and use the virtual environment's Python executable.

## 5. Open the dashboard

By default, the dashboard listens on port **8080** at `127.0.0.1`.

On the host machine, open `http://127.0.0.1:8080` and authenticate using the configured dashboard token as required by the application.

For remote administration, use a private SSH tunnel instead of exposing the dashboard to the public internet. For example, from another computer:

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

Then open `http://127.0.0.1:8080` on that computer.

**Security note:** The current dashboard uses a URL query token for authentication. Such tokens can appear in browser history or logs. Do not expose port 8080 directly to the internet; use a secure private connection.

## Scheduling

The scheduler uses the **America/New_York** time zone, as currently defined in `cogs/recurring_image.py`. Change that constant in the source code if your deployment needs another time zone.

On a fresh installation, `config/weekly_seed.json` provides a sample schedule:

| Day | Example image | Posting window |
| --- | --- | --- |
| Monday | `images/monday.jpg` | 09:00–14:59 |
| Friday | `images/friday.jpg` | 09:00–14:59 |

These are starter settings, not required posting days. You can configure the days, images, messages, and posting windows through the dashboard.

The scheduler selects posting times randomly within the configured window. The seed file also contains historical example selections for reference; they do not force future posting times. Existing databases are not overwritten by the starter schedule.

The application can also use specific-date overrides and monthly recurring rules to choose which image and message to post.

## Project structure

```text
Garfie/
├── bot.py                   # Discord bot startup and cog loading
├── cogs/
│   └── recurring_image.py  # Scheduler and Discord commands
├── webserver.py             # Web dashboard server and routes
├── template.html            # Dashboard interface
├── config/
│   └── weekly_seed.json     # Initial example schedule
├── images/
│   ├── default.jpg          # Fallback image
│   ├── monday.jpg           # Example weekday image
│   └── friday.jpg           # Example weekday image
├── requirements.txt         # Python dependencies
├── .env.example             # Configuration template
└── .gitignore               # Local/private files excluded from Git
```

Garfie creates `schedule.db` for local configuration and posting state and writes runtime logs to `garfie.log`. These files are local runtime data and should not be committed to a public repository.

## Troubleshooting

- **Bot does not connect:** Verify `DISCORD_TOKEN`, network connectivity, and the enabled Discord gateway intents.
- **No images are posted:** Check `GARFIE_CHANNEL_ID`, channel permissions, image paths, configured posting days, and the selected time window.
- **Dashboard cannot be reached:** Confirm the bot is running, port 8080 is available, and the dashboard is being accessed on the correct host or through an SSH tunnel.
- **Slash commands are missing:** Verify that the bot was invited with the `applications.commands` scope and allow time for command synchronization.
- **Existing schedule differs from the examples:** An existing `schedule.db` takes precedence over the starter seed; remove or modify local configurations only if you intend to reset them.

## Security

Keep tokens and local data private. Do not commit `.env`, `schedule.db`, logs, or user-uploaded content. Before making a deployment internet-accessible, review the dashboard's authentication and network exposure.

## License and assets

No license is specified in this repository. The included images may have separate copyright or redistribution restrictions; verify your rights before reusing or distributing them.
