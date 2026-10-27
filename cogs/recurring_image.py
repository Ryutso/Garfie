# cogs/recurring_image.py
#
# Garfie recurring image scheduler.
# Web server/UI is kept separately in ../webserver.py

import discord
from discord.ext import commands, tasks
from discord import app_commands

import os
import logging
from pathlib import Path
import random
import sqlite3
import threading
import pytz

from datetime import datetime, time, timedelta


DB_FILE = "schedule.db"
audit_logger = logging.getLogger("garfie.scheduler")
audit_logger.setLevel(logging.INFO)
_log_path = str(Path(__file__).resolve().parent.parent / "garfie.log")
if not any(getattr(h, "baseFilename", None) == _log_path for h in audit_logger.handlers):
    _handler = logging.FileHandler(_log_path, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    audit_logger.addHandler(_handler)

TIMEZONE = pytz.timezone("America/New_York")

# Garfie's normal posting days:
# Monday = 0
# Tuesday = 1
# Friday = 4
POST_DAYS = {0, 1, 4}  # Legacy constant; active days now come from day_config.

DEFAULT_IMAGE = "images/default.jpg"

WEB_HOST = os.getenv("GARFIE_WEB_HOST", "127.0.0.1")
WEB_PORT = 8080


WEEKDAY_CHOICES = [
    app_commands.Choice(name="Monday", value=0),
    app_commands.Choice(name="Tuesday", value=1),
    app_commands.Choice(name="Wednesday", value=2),
    app_commands.Choice(name="Thursday", value=3),
    app_commands.Choice(name="Friday", value=4),
    app_commands.Choice(name="Saturday", value=5),
    app_commands.Choice(name="Sunday", value=6),
]


class RecurringImage(commands.Cog):

    garfie = app_commands.Group(
        name="garfie",
        description="Garfie commands",
    )

    config = app_commands.Group(
        name="config",
        description="Configure Garfie",
        parent=garfie,
    )

    preview = app_commands.Group(
        name="preview",
        description="Preview Garfie posts",
        parent=garfie,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.db = sqlite3.connect(
            DB_FILE,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row

        self.lock = threading.Lock()

        self.webserver = None

        self.setup_db()

        self.check_schedule.start()

        # Start the web server after the cog has been initialized.
        self.bot.loop.create_task(
            self.start_webserver()
        )

    # ------------------------------------------------------------------
    # DATABASE
    # ------------------------------------------------------------------

    def setup_db(self):
        with self.lock:

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS posts(
                    id INTEGER PRIMARY KEY,
                    last_posted TEXT,
                    scheduled_clock TEXT
                )
                """
            )

            self.db.execute(
                """
                INSERT OR IGNORE INTO posts(id)
                VALUES(1)
                """
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS day_config(
                    weekday INTEGER PRIMARY KEY,
                    image_path TEXT,
                    message TEXT
                )
                """
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS special_days(
                    month INTEGER,
                    day INTEGER,
                    image_path TEXT,
                    message TEXT,
                    PRIMARY KEY(month, day)
                )
                """
            )

            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_rules(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month INTEGER,
                    weekday INTEGER,
                    image_path TEXT,
                    message TEXT
                )
                """
            )

            for table, column, declaration in (
                ("day_config", "start_time", "TEXT NOT NULL DEFAULT '09:00'"),
                ("day_config", "end_time", "TEXT NOT NULL DEFAULT '14:59'"),
                ("posts", "scheduled_date", "TEXT"),
            ):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS weekly_picks (
                post_date TEXT PRIMARY KEY,
                weekday INTEGER NOT NULL,
                chosen_clock TEXT NOT NULL
            )""")
            # Initialize public, non-sensitive default schedule on a fresh install.
            # Existing databases/configurations are never overwritten.
            if not self.db.execute("SELECT 1 FROM day_config LIMIT 1").fetchone():
                import json
                from pathlib import Path
                seed_file = Path(__file__).resolve().parent.parent / "config" / "weekly_seed.json"
                if seed_file.is_file():
                    for entry in json.loads(seed_file.read_text(encoding="utf-8"))["weekly"]:
                        self.db.execute(
                            "INSERT OR IGNORE INTO day_config "
                            "(weekday, image_path, message, start_time, end_time) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (entry["weekday"], entry["image_path"], "",
                             entry["start_time"], entry["end_time"]),
                        )

            self.db.commit()

    def q(
        self,
        sql,
        args=(),
        fetch=False,
    ):
        with self.lock:
            cur = self.db.execute(sql, args)

            if fetch:
                return cur.fetchall()

            self.db.commit()

    # ------------------------------------------------------------------
    # SCHEDULER STATE
    # ------------------------------------------------------------------

    def get_post_state(self):
        rows = self.q(
            """
            SELECT last_posted, scheduled_clock, scheduled_date
            FROM posts
            WHERE id=1
            """,
            fetch=True,
        )

        if rows:
            return dict(rows[0])

        return {
            "last_posted": None,
            "scheduled_clock": None,
            "scheduled_date": None,
        }

    def set_scheduled_time(self, value):
        self.q(
            """
            UPDATE posts
            SET scheduled_clock=?, scheduled_date=?
            WHERE id=1
            """,
            (value, datetime.now(TIMEZONE).date().isoformat() if value else None),
        )

    def choose_random_time(self):
        """
        Choose a random posting time between 9:00 AM and 2:59 PM.
        """
        return time(
            random.randint(9, 14),
            random.randint(0, 59),
        )

    # ------------------------------------------------------------------
    # CONFIGURATION RESOLUTION
    # ------------------------------------------------------------------

    def resolve_config(self, target_date):
        """
        Configuration priority:

        1. Specific date override
        2. Monthly recurring weekday rule
        3. Normal weekly weekday configuration
        4. Default image
        """

        # Specific date
        rows = self.q(
            """
            SELECT image_path, message
            FROM special_days
            WHERE month=? AND day=?
            """,
            (
                target_date.month,
                target_date.day,
            ),
            fetch=True,
        )

        if rows:
            return dict(rows[0])

        # Monthly recurring weekday
        rows = self.q(
            """
            SELECT image_path, message
            FROM recurring_rules
            WHERE month=? AND weekday=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                target_date.month,
                target_date.weekday(),
            ),
            fetch=True,
        )

        if rows:
            return dict(rows[0])

        # Normal weekly configuration
        rows = self.q(
            """
            SELECT image_path, message
            FROM day_config
            WHERE weekday=?
            """,
            (
                target_date.weekday(),
            ),
            fetch=True,
        )

        if rows:
            return dict(rows[0])

        # Default
        return {
            "image_path": DEFAULT_IMAGE,
            "message": None,
        }

    # ------------------------------------------------------------------
    # WEB SERVER
    # ------------------------------------------------------------------

    async def start_webserver(self):
        try:
            from webserver import GarfieWebServer

            self.webserver = GarfieWebServer(
                bot=self.bot,
                cog=self,
                host=WEB_HOST,
                port=WEB_PORT,
            )

            await self.webserver.start()

        except Exception:
            # The bot should still be able to run if the web server
            # fails to start.
            import traceback

            print("Failed to start Garfie web server:")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # COG UNLOAD
    # ------------------------------------------------------------------

    async def cog_unload(self):
        self.check_schedule.cancel()

        if self.webserver:
            try:
                await self.webserver.stop()
            except Exception:
                pass

        self.db.close()

    # ------------------------------------------------------------------
    # SCHEDULE LOOP
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def check_schedule(self):
        await self.bot.wait_until_ready()
        now = datetime.now(TIMEZONE)
        today = now.date()
        date_string = today.isoformat()

        # The dashboard's weekly configurations determine active posting days.
        rows = self.q(
            "SELECT start_time, end_time FROM day_config WHERE weekday=?",
            (today.weekday(),), fetch=True,
        )
        if not rows:
            return
        try:
            start_clock = time.fromisoformat(rows[0]["start_time"])
            end_clock = time.fromisoformat(rows[0]["end_time"])
            if start_clock > end_clock:
                raise ValueError("start_time is after end_time")
        except (ValueError, TypeError):
            audit_logger.exception("[AUDIT] INVALID_WEEKLY_TIME_RANGE weekday=%s", today.weekday())
            return

        state = self.get_post_state()
        if str(state["last_posted"] or "")[:10] == date_string:
            return

        start_dt = TIMEZONE.localize(datetime.combine(today, start_clock))
        end_dt = TIMEZONE.localize(datetime.combine(today, end_clock))
        if now > end_dt:
            # Do not post outside today's allowed time window.
            return

        picks = self.q("SELECT chosen_clock FROM weekly_picks WHERE post_date=?", (date_string,), fetch=True)
        scheduled_clock = picks[0]["chosen_clock"] if picks else state["scheduled_clock"]
        if not picks and state.get("scheduled_date") != date_string:
            scheduled_clock = None

        if scheduled_clock:
            try:
                scheduled = time.fromisoformat(scheduled_clock)
                if not (start_clock <= scheduled <= end_clock):
                    raise ValueError("scheduled time outside today's window")
            except (ValueError, TypeError):
                audit_logger.warning("[AUDIT] INVALID_SCHEDULE_RESET date=%s old=%r", date_string, scheduled_clock)
                scheduled_clock = None

        if not scheduled_clock:
            # Choose a future minute in today's configured window.
            first_minute = max(
                start_clock.hour * 60 + start_clock.minute,
                now.hour * 60 + now.minute + (1 if now.second or now.microsecond else 0),
            )
            last_minute = end_clock.hour * 60 + end_clock.minute
            if first_minute > last_minute:
                return
            selected_minute = random.randint(first_minute, last_minute)
            scheduled = time(selected_minute // 60, selected_minute % 60)
            self.set_scheduled_time(scheduled.isoformat())
            self.q("INSERT OR REPLACE INTO weekly_picks(post_date,weekday,chosen_clock) VALUES (?,?,?)",
                   (date_string, today.weekday(), scheduled.strftime("%H:%M")))
            audit_logger.info(
                "[AUDIT] RANDOM_TIME_SELECTED date=%s weekday=%s range=%s-%s time=%s",
                date_string, today.weekday(), start_clock, end_clock, scheduled,
            )
            return

        scheduled_dt = TIMEZONE.localize(datetime.combine(today, scheduled))
        if now < scheduled_dt:
            return

        try:
            config = self.resolve_config(today)
            image_path = config["image_path"]
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"Configured image does not exist: {image_path}")
            channel_id = int(os.getenv("GARFIE_CHANNEL_ID", "0"))
            if not channel_id:
                raise RuntimeError("GARFIE_CHANNEL_ID is not configured.")
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            await channel.send(
                content=config["message"] or None,
                file=discord.File(image_path),
            )
            self.q(
                "UPDATE posts SET last_posted=?, scheduled_clock=NULL, scheduled_date=NULL WHERE id=1",
                (date_string,),
            )
            audit_logger.info(
                "[AUDIT] SCHEDULED_POST date=%s channel=%s image=%r message=%r",
                date_string, channel_id, image_path, config["message"],
            )
        except Exception:
            audit_logger.exception("[AUDIT] SCHEDULED_POST_FAILED date=%s", date_string)

    # ------------------------------------------------------------------
    # /garfie post
    # ------------------------------------------------------------------

    @garfie.command(
        name="post",
        description="Post Garfie's configured image now.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def post(
        self,
        interaction: discord.Interaction,
    ):

        today = datetime.now(
            TIMEZONE
        ).date()

        config = self.resolve_config(today)

        if not os.path.exists(
            config["image_path"]
        ):
            await interaction.response.send_message(
                f"Image not found: `{config['image_path']}`",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content=config["message"] or None,
            file=discord.File(
                config["image_path"]
            ),
        )
        audit_logger.info("[AUDIT] MANUAL_POST source=discord user=%s channel=%s image=%r message=%r",
                          interaction.user.id, interaction.channel_id, config["image_path"], config["message"])

    # ------------------------------------------------------------------
    # /garfie status
    # ------------------------------------------------------------------

    @garfie.command(
        name="status",
        description="Show Garfie's scheduler status.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def status(
        self,
        interaction: discord.Interaction,
    ):

        state = self.get_post_state()

        await interaction.response.send_message(
            f"Last posted: "
            f"`{state['last_posted'] or 'Never'}`\n"
            f"Scheduled time: "
            f"`{state['scheduled_clock'] or 'Not set'}`\n"
            f"Timezone: `{TIMEZONE.zone}`",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie next
    # ------------------------------------------------------------------

    @garfie.command(
        name="next",
        description="Show Garfie's next scheduled post.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def next_post(
        self,
        interaction: discord.Interaction,
    ):

        now = datetime.now(
            TIMEZONE
        )

        state = self.get_post_state()

        for offset in range(8):

            target = (
                now.date()
                + timedelta(days=offset)
            )

            day_rows = self.q(
                "SELECT start_time, end_time FROM day_config WHERE weekday=?",
                (target.weekday(),), fetch=True,
            )
            if not day_rows:
                continue

            if (
                state["last_posted"]
                == target.isoformat()
            ):
                continue

            if (
                offset == 0
                and state["scheduled_clock"]
                and state.get("scheduled_date") == target.isoformat()
            ):

                try:
                    scheduled = time.fromisoformat(
                        state["scheduled_clock"]
                    )

                except ValueError:
                    scheduled = (
                        self.choose_random_time()
                    )

            else:
                start = time.fromisoformat(day_rows[0]["start_time"])
                end = time.fromisoformat(day_rows[0]["end_time"])
                if start > end:
                    continue
                low = start.hour * 60 + start.minute
                high = end.hour * 60 + end.minute
                selected = random.randint(low, high)
                scheduled = time(selected // 60, selected % 60)

            scheduled_dt = TIMEZONE.localize(
                datetime.combine(
                    target,
                    scheduled,
                )
            )

            if scheduled_dt <= now:
                continue

            formatted = scheduled_dt.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ).replace(
                " 0",
                " ",
            )

            await interaction.response.send_message(
                "Next Garfie post: "
                f"`{formatted}` "
                f"({TIMEZONE.zone})",
                ephemeral=True,
            )

            return

        await interaction.response.send_message(
            "No upcoming posting date was found.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie settime
    # ------------------------------------------------------------------

    @garfie.command(
        name="settime",
        description="Choose a new random posting time.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def settime(
        self,
        interaction: discord.Interaction,
    ):

        scheduled = self.choose_random_time()

        previous = self.get_post_state()["scheduled_clock"]
        self.set_scheduled_time(
            scheduled.isoformat()
        )
        audit_logger.info("[AUDIT] DISCORD_TIME_CHANGED user=%s old=%r new=%s",
                          interaction.user.id, previous, scheduled.isoformat())

        await interaction.response.send_message(
            "Next post time set to "
            f"`{scheduled.strftime('%I:%M %p')}`.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie config setday
    # ------------------------------------------------------------------

    @config.command(
        name="setday",
        description="Configure a normal weekly posting day.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    @app_commands.choices(
        weekday=WEEKDAY_CHOICES
    )
    async def setday(
        self,
        interaction: discord.Interaction,
        weekday: app_commands.Choice[int],
        image: discord.Attachment,
        message: str | None = None,
    ):

        os.makedirs(
            "images/weekly",
            exist_ok=True,
        )

        path = os.path.join(
            "images",
            "weekly",
            f"{weekday.value}_{image.filename}",
        )

        await image.save(path)

        self.q(
            """
            INSERT OR REPLACE INTO day_config
            (weekday, image_path, message)
            VALUES(?,?,?)
            """,
            (
                weekday.value,
                path,
                message,
            ),
        )

        audit_logger.info("[AUDIT] DISCORD_WEEKLY_SAVED user=%s weekday=%s image=%r message=%r",
                          interaction.user.id, weekday.name, path, message)

        await interaction.response.send_message(
            f"Saved weekly configuration "
            f"for {weekday.name}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie config setdate
    # ------------------------------------------------------------------

    @config.command(
        name="setdate",
        description="Configure a specific calendar date.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def setdate(
        self,
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12],
        day: app_commands.Range[int, 1, 31],
        image: discord.Attachment,
        message: str | None = None,
    ):

        os.makedirs(
            "images/special",
            exist_ok=True,
        )

        path = os.path.join(
            "images",
            "special",
            f"{month}_{day}_{image.filename}",
        )

        await image.save(path)

        self.q(
            """
            INSERT OR REPLACE INTO special_days
            (month, day, image_path, message)
            VALUES(?,?,?,?)
            """,
            (
                month,
                day,
                path,
                message,
            ),
        )

        audit_logger.info("[AUDIT] DISCORD_DATE_SAVED user=%s month=%s day=%s image=%r message=%r",
                          interaction.user.id, month, day, path, message)

        await interaction.response.send_message(
            f"Saved override for "
            f"{month}/{day}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie config recurring
    # ------------------------------------------------------------------

    @config.command(
        name="recurring",
        description="Configure a monthly recurring weekday rule.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    @app_commands.choices(
        weekday=WEEKDAY_CHOICES
    )
    async def recurring(
        self,
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12],
        weekday: app_commands.Choice[int],
        image: discord.Attachment,
        message: str | None = None,
    ):

        os.makedirs(
            "images/recurring",
            exist_ok=True,
        )

        path = os.path.join(
            "images",
            "recurring",
            f"{month}_{weekday.value}_{image.filename}",
        )

        await image.save(path)

        self.q(
            """
            INSERT INTO recurring_rules
            (month, weekday, image_path, message)
            VALUES(?,?,?,?)
            """,
            (
                month,
                weekday.value,
                path,
                message,
            ),
        )

        audit_logger.info("[AUDIT] DISCORD_RECURRING_CREATED user=%s month=%s weekday=%s image=%r message=%r",
                          interaction.user.id, month, weekday.name, path, message)

        await interaction.response.send_message(
            "Saved recurring rule for "
            f"month {month}, {weekday.name}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /garfie preview day
    # ------------------------------------------------------------------

    @preview.command(
        name="day",
        description="Preview a configured weekly day.",
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    @app_commands.choices(
        weekday=WEEKDAY_CHOICES
    )
    async def previewday(
        self,
        interaction: discord.Interaction,
        weekday: app_commands.Choice[int],
    ):

        rows = self.q(
            """
            SELECT image_path, message
            FROM day_config
            WHERE weekday=?
            """,
            (
                weekday.value,
            ),
            fetch=True,
        )

        if not rows:

            await interaction.response.send_message(
                "No configuration exists for "
                "that weekday.",
                ephemeral=True,
            )

            return

        row = rows[0]

        if not os.path.exists(
            row["image_path"]
        ):

            await interaction.response.send_message(
                f"Image not found: "
                f"`{row['image_path']}`",
                ephemeral=True,
            )

            return

        await interaction.response.send_message(
            content=row["message"] or None,
            file=discord.File(
                row["image_path"]
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(
        RecurringImage(bot)
    )