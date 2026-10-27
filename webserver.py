import os
import html
import hmac
import uuid
import calendar
import logging
import sqlite3
import re

from pathlib import Path
from string import Template
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import discord
from aiohttp import web


# =========================================================
# PATHS AND SETTINGS
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parent

IMAGE_ROOT = PROJECT_ROOT / "images"
TEMPLATE_FILE = PROJECT_ROOT / "template.html"
DATABASE_FILE = PROJECT_ROOT / "schedule.db"
LOG_FILE = PROJECT_ROOT / "garfie.log"

TIMEZONE_NAME = "America/New_York"

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

logger = logging.getLogger("garfie.webserver")
logger.setLevel(logging.INFO)
logger.propagate = True
if not any(getattr(h, "baseFilename", None) == str(LOG_FILE) for h in logger.handlers):
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)



# =========================================================
# GARFIE WEB SERVER
# =========================================================

class GarfieWebServer:

    def __init__(
        self,
        bot=None,
        cog=None,
        host="0.0.0.0",
        port=8080,
    ):
        # Compatible with the existing recurring_image.py:
        #
        # GarfieWebServer(
        #     bot=self.bot,
        #     cog=self,
        #     host=WEB_HOST,
        #     port=WEB_PORT,
        # )

        self.bot = bot or (cog.bot if cog else None)
        self.cog = cog

        self.host = host
        self.port = int(port)

        self.web_token = os.getenv(
            "GARFIE_WEB_TOKEN",
            "",
        ).strip()

        self.timezone = ZoneInfo(TIMEZONE_NAME)

        self.runner = None
        self.site = None

        IMAGE_ROOT.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.app = web.Application(
            client_max_size=MAX_UPLOAD_BYTES + 1024 * 1024,
            middlewares=[
                self.activity_error_middleware,
                self.authentication_middleware,
            ],
        )

        self.ensure_time_columns()
        self.register_routes()

    def audit(self, action, **fields):
        """Write one searchable activity entry with old/new values when available."""
        import json
        details = json.dumps(fields, ensure_ascii=False, default=str)
        self.log(f"[AUDIT] {action}: {details}")

    def describe(self, row):
        return dict(row) if row is not None else None

    # =====================================================
    # DATABASE
    # =====================================================

    @contextmanager
    def database(self):
        connection = sqlite3.connect(
            str(DATABASE_FILE)
        )

        connection.row_factory = sqlite3.Row

        try:
            yield connection
            connection.commit()

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def ensure_time_columns(self):
        with self.database() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(day_config)")}
            for column, declaration in (
                ("start_time", "TEXT NOT NULL DEFAULT '09:00'"),
                ("end_time", "TEXT NOT NULL DEFAULT '14:59'"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE day_config ADD COLUMN {column} {declaration}")
            db.execute("""CREATE TABLE IF NOT EXISTS weekly_picks (
                post_date TEXT PRIMARY KEY,
                weekday INTEGER NOT NULL,
                chosen_clock TEXT NOT NULL
            )""")
            posts_columns = {r[1] for r in db.execute("PRAGMA table_info(posts)")}
            if "scheduled_date" not in posts_columns:
                db.execute("ALTER TABLE posts ADD COLUMN scheduled_date TEXT")

    @staticmethod
    def validate_time_range(start, end):
        try:
            start_time = datetime.strptime(str(start), "%H:%M").time()
            end_time = datetime.strptime(str(end), "%H:%M").time()
        except ValueError:
            raise web.HTTPBadRequest(text="Time range must use HH:MM (24-hour format).")
        if start_time > end_time:
            raise web.HTTPBadRequest(text="Start time must be before or equal to end time.")
        return start_time.strftime("%H:%M"), end_time.strftime("%H:%M")

    def get_post_status(self):
        with self.database() as db:
            row = db.execute(
                """
                SELECT last_posted, scheduled_clock
                FROM posts
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            return {
                "last_posted": None,
                "scheduled_clock": None,
            }

        return {
            "last_posted": row["last_posted"],
            "scheduled_clock": row["scheduled_clock"],
        }

    # =====================================================
    # AUTHENTICATION
    # =====================================================

    @web.middleware
    async def activity_error_middleware(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if request.method == "POST" and exc.status >= 400:
                logger.warning("[AUDIT] DASHBOARD_ACTION_REJECTED path=%s status=%s reason=%s",
                               request.path, exc.status, exc.text)
            raise
        except Exception:
            logger.exception("[AUDIT] DASHBOARD_ACTION_FAILED method=%s path=%s",
                             request.method, request.path)
            raise

    @web.middleware
    async def authentication_middleware(
        self,
        request,
        handler,
    ):
        """
        Authentication is handled using the URL query string.

        This intentionally avoids reading POST form data in
        middleware, because doing so can consume multipart
        upload data before the upload handler receives it.
        """

        if not self.web_token:
            return await handler(request)

        supplied_token = request.query.get(
            "token",
            "",
        )

        if not hmac.compare_digest(
            str(supplied_token),
            self.web_token,
        ):
            return web.Response(
                text="Unauthorized. Check your dashboard access token.",
                status=401,
                content_type="text/plain",
            )

        return await handler(request)

    def dashboard_url(self, path="/"):
        if not self.web_token:
            return path

        separator = "&" if "?" in path else "?"

        return (
            path
            + separator
            + urlencode({
                "token": self.web_token
            })
        )

    def redirect_dashboard(self):
        raise web.HTTPSeeOther(
            self.dashboard_url("/")
        )

    # =====================================================
    # IMAGE LIBRARY
    # =====================================================

    def get_available_images(self):
        """
        Scan images/ and every subfolder for supported images.

        Returns project-relative paths, for example:
            images/default.jpg
            images/weekly/example.png
        """

        IMAGE_ROOT.mkdir(
            parents=True,
            exist_ok=True,
        )

        root = IMAGE_ROOT.resolve()
        available = []

        for path in root.rglob("*"):

            if not path.is_file():
                continue

            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue

            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root)

            except (ValueError, OSError):
                continue

            project_relative = (
                Path("images") / relative
            ).as_posix()

            available.append(project_relative)

        return sorted(
            set(available),
            key=str.lower,
        )

    def image_options_html(self):
        options = [
            '<option value="">-- Choose an existing image --</option>'
        ]

        for image_path in self.get_available_images():

            safe_value = html.escape(
                image_path,
                quote=True,
            )

            safe_label = html.escape(
                image_path
            )

            options.append(
                f'<option value="{safe_value}">'
                f'{safe_label}'
                f'</option>'
            )

        return "\n".join(options)

    def resolve_existing_image(self, selected):
        if not selected:
            return None

        selected = str(selected).strip()

        if Path(selected).is_absolute():
            raise ValueError(
                "Invalid image selection."
            )

        if "\\" in selected:
            raise ValueError(
                "Invalid image selection."
            )

        root = IMAGE_ROOT.resolve()

        candidate = (
            PROJECT_ROOT / selected
        ).resolve()

        try:
            candidate.relative_to(root)

        except ValueError:
            raise ValueError(
                "Invalid image selection."
            )

        if not candidate.is_file():
            raise ValueError(
                "The selected image no longer exists."
            )

        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(
                "Unsupported image format."
            )

        return candidate.relative_to(
            PROJECT_ROOT
        ).as_posix()

    async def read_upload(
        self,
        request,
        destination_folder,
    ):
        """
        Accept either an existing image selection or a new upload.

        If the user supplies both, the uploaded image takes
        priority.
        """

        reader = await request.multipart()

        values = {}
        uploaded_image = None

        destination_folder = (
            PROJECT_ROOT / destination_folder
        )

        destination_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        while True:
            part = await reader.next()

            if part is None:
                break

            if part.name == "image":

                original_filename = Path(
                    part.filename or ""
                ).name

                if not original_filename:
                    continue

                extension = Path(
                    original_filename
                ).suffix.lower()

                if extension not in IMAGE_EXTENSIONS:
                    raise ValueError(
                        "Unsupported image format. "
                        "Use JPG, JPEG, PNG, GIF, WEBP, or BMP."
                    )

                filename = (
                    uuid.uuid4().hex + extension
                )

                destination = (
                    destination_folder / filename
                )

                size = 0

                try:
                    with destination.open("wb") as output:

                        while True:
                            chunk = await part.read_chunk(
                                64 * 1024
                            )

                            if not chunk:
                                break

                            size += len(chunk)

                            if size > MAX_UPLOAD_BYTES:
                                raise ValueError(
                                    "The uploaded image exceeds "
                                    "the 10 MB upload limit."
                                )

                            output.write(chunk)

                    uploaded_image = (
                        destination.relative_to(
                            PROJECT_ROOT
                        ).as_posix()
                    )
                    self.audit("IMAGE_UPLOADED", original_name=original_filename,
                               stored_path=uploaded_image, bytes=size)

                except Exception:
                    destination.unlink(
                        missing_ok=True
                    )
                    raise

            else:
                values[part.name] = await part.text()

        if uploaded_image:
            values["image_path"] = uploaded_image

        else:
            values["image_path"] = (
                self.resolve_existing_image(
                    values.get("existing_image")
                )
            )

        if not values.get("image_path"):
            raise ValueError(
                "Choose an existing image or upload a new one."
            )

        return values

    # =====================================================
    # TEMPLATE
    # =====================================================

    def render_template(self, **values):
        if not TEMPLATE_FILE.is_file():
            raise FileNotFoundError(
                f"Dashboard template not found: {TEMPLATE_FILE}"
            )

        template_text = TEMPLATE_FILE.read_text(
            encoding="utf-8"
        )

        template = Template(template_text)

        defaults = {
            "weekly_rows": "",
            "special_rows": "",
            "recurring_rows": "",

            "last_posted": "No posts recorded",
            "scheduled_clock": "Automatic",
            "scheduled_time_value": "",

            "timezone": TIMEZONE_NAME,

            "image_options": self.image_options_html(),

            "post_now_url": self.dashboard_url("/post-now"),
            "settime_url": self.dashboard_url("/settime"),
            "logs_url": self.dashboard_url("/logs"),

            "weekly_save_url": self.dashboard_url("/weekly/save"),
            "special_save_url": self.dashboard_url("/special/save"),
            "recurring_save_url": self.dashboard_url("/recurring/save"),

            "weekly_delete_url": self.dashboard_url("/weekly/delete"),
            "special_delete_url": self.dashboard_url("/special/delete"),
            "recurring_delete_url": self.dashboard_url("/recurring/delete"),
        }

        defaults.update(values)

        rendered = template.safe_substitute(defaults)
        # Add range inputs to the existing weekly creation form. No template replacement needed.
        pattern = r'(<form\b[^>]*\baction=["\']([^"\']*?/weekly/save(?:\?[^"\']*)?)["\'][^>]*>)(.*?)(</form>)'
        def add_weekly_time_fields(match):
            body = match.group(3)
            if 'name="start_time"' in body:
                return match.group(0)
            controls = (
                '<div class="weekly-time-range" style="display:flex;gap:12px;flex-wrap:wrap;margin:12px 0">'
                '<label>Earliest posting time <input type="time" name="start_time" value="09:00" required></label>'
                '<label>Latest posting time <input type="time" name="end_time" value="14:59" required></label>'
                '</div>'
            )
            return match.group(1) + body + controls + match.group(4)
        return re.sub(pattern, add_weekly_time_fields, rendered, flags=re.IGNORECASE | re.DOTALL)

    # =====================================================
    # WEEKLY TABLE
    # =====================================================

    def next_weekly_date(self, weekday, start_clock, end_clock, now):
        from datetime import timedelta
        days = (weekday - now.weekday()) % 7
        target = now.date() + timedelta(days=days)
        if days == 0:
            end_dt = datetime.combine(target, datetime.strptime(end_clock, "%H:%M").time(), self.timezone)
            status = self.get_post_status()
            if now > end_dt or str(status["last_posted"] or "")[:10] == target.isoformat():
                target += timedelta(days=7)
        return target

    def ensure_weekly_pick(self, weekday, start_clock, end_clock, now=None):
        import random
        now = now or datetime.now(self.timezone)
        target = self.next_weekly_date(weekday, start_clock, end_clock, now)
        date_string = target.isoformat()
        start_minutes = int(start_clock[:2]) * 60 + int(start_clock[3:5])
        end_minutes = int(end_clock[:2]) * 60 + int(end_clock[3:5])
        if target == now.date():
            start_minutes = max(start_minutes, now.hour * 60 + now.minute + 1)
        if start_minutes > end_minutes:
            from datetime import timedelta
            target += timedelta(days=7)
            date_string = target.isoformat()
            start_minutes = int(start_clock[:2]) * 60 + int(start_clock[3:5])
        with self.database() as db:
            row = db.execute("SELECT chosen_clock FROM weekly_picks WHERE post_date=?", (date_string,)).fetchone()
            if row:
                minute = int(row["chosen_clock"][:2]) * 60 + int(row["chosen_clock"][3:5])
                if start_minutes <= minute <= end_minutes:
                    return date_string, row["chosen_clock"]
            minute = random.randint(start_minutes, end_minutes)
            chosen = f"{minute // 60:02d}:{minute % 60:02d}"
            db.execute("INSERT OR REPLACE INTO weekly_picks(post_date, weekday, chosen_clock) VALUES (?,?,?)",
                       (date_string, weekday, chosen))
        self.audit("WEEKLY_TIME_PICKED", weekday=weekday, date=date_string, time=chosen)
        return date_string, chosen

    async def weekly_reroll(self, request):
        import random
        form = await request.post()
        try:
            weekday = int(form.get("weekday", ""))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid weekday")
        with self.database() as db:
            row = db.execute("SELECT start_time, end_time FROM day_config WHERE weekday=?", (weekday,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(text="Weekly configuration not found")
        now = datetime.now(self.timezone)
        target = self.next_weekly_date(weekday, row["start_time"], row["end_time"], now)
        date_string = target.isoformat()
        first = int(row["start_time"][:2])*60 + int(row["start_time"][3:5])
        last = int(row["end_time"][:2])*60 + int(row["end_time"][3:5])
        if target == now.date():
            first = max(first, now.hour*60 + now.minute + 1)
        if first > last:
            raise web.HTTPBadRequest(text="No time remains in today's posting window")
        with self.database() as db:
            previous = db.execute("SELECT chosen_clock FROM weekly_picks WHERE post_date=?", (date_string,)).fetchone()
            old = previous["chosen_clock"] if previous else None
            minute = random.randint(first, last)
            if old and last > first:
                options = [m for m in range(first, last+1) if f"{m//60:02d}:{m%60:02d}" != old]
                if options:
                    minute = random.choice(options)
            chosen = f"{minute//60:02d}:{minute%60:02d}"
            db.execute("INSERT OR REPLACE INTO weekly_picks(post_date,weekday,chosen_clock) VALUES (?,?,?)",
                       (date_string, weekday, chosen))
            if target == now.date():
                db.execute("UPDATE posts SET scheduled_clock=?,scheduled_date=? WHERE id=1", (chosen, date_string))
        self.audit("WEEKLY_TIME_REROLLED", weekday=weekday, date=date_string, old=old, new=chosen)
        self.redirect_dashboard()

    def build_weekly_rows(self):
        weekday_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]

        with self.database() as db:
            rows = db.execute(
                """
                SELECT weekday, image_path, message, start_time, end_time
                FROM day_config
                ORDER BY weekday
                """
            ).fetchall()

        if not rows:
            return (
                '<tr><td colspan="5" class="empty">'
                'No weekly configurations have been added.'
                '</td></tr>'
            )

        output = []

        for row in rows:
            weekday = int(row["weekday"])

            name = (
                weekday_names[weekday]
                if weekday in range(7)
                else str(weekday)
            )

            picked_date, picked_clock = self.ensure_weekly_pick(
                weekday, row["start_time"] or "09:00", row["end_time"] or "14:59"
            )
            display_clock = datetime.strptime(picked_clock, "%H:%M").strftime("%I:%M %p").lstrip("0")
            picked_html = (
                '<div class="weekly-picked">'
                '<span class="weekly-picked-label">Next selected time</span>'
                f'<strong>{html.escape(display_clock)}</strong>'
                f'<span class="weekly-picked-date">{html.escape(picked_date)}</span>'
                '</div>'
                f'<form class="weekly-reroll-form" method="post" action="{html.escape(self.dashboard_url("/weekly/reroll"), quote=True)}">'
                f'<input type="hidden" name="weekday" value="{weekday}">'
                '<button type="submit" class="small-button">Pick another random time</button>'
                '</form>'
            )

            output.append(
                "<tr>"
                f"<td>{html.escape(name)}</td>"
                f"<td>{html.escape(str(row['image_path'] or ''))}</td>"
                '<td class="weekly-message-cell">'
                f'<form class="weekly-message-form" method="post" action="{html.escape(self.dashboard_url("/weekly/message"), quote=True)}">'
                f'<input type="hidden" name="weekday" value="{weekday}">'
                f'<textarea name="message" aria-label="Message for {html.escape(name, quote=True)}" placeholder="Optional Discord message">{html.escape(str(row["message"] or ""))}</textarea>'
                '<button type="submit" class="small-button">Save message</button>'
                '</form>'
                '</td>'
                '<td class="weekly-window-cell">'
                f'<form class="weekly-time-form" method="post" action="{html.escape(self.dashboard_url("/weekly/time"), quote=True)}">'
                f'<input type="hidden" name="weekday" value="{weekday}">'
                '<div class="weekly-time-pair">'
                '<label>From'
                f'<input type="time" name="start_time" value="{html.escape(row["start_time"] or "09:00", quote=True)}" required>'
                '</label>'
                '<label>To'
                f'<input type="time" name="end_time" value="{html.escape(row["end_time"] or "14:59", quote=True)}" required>'
                '</label>'
                '</div>'
                '<button type="submit" class="small-button">Save times</button>'
                '</form>'
                f'{picked_html}'
                '</td>'
                '<td class="weekly-action-cell">'
                f'<form method="post" action="{html.escape(self.dashboard_url("/weekly/delete"), quote=True)}">'
                f'<input type="hidden" name="weekday" value="{weekday}">'
                '<button class="danger small-button" type="submit">Delete</button>'
                '</form>'
                "</td>"
                "</tr>"
            )

        return "\n".join(output)

    # =====================================================
    # SPECIFIC DATE TABLE
    # =====================================================

    def build_special_rows(self):
        with self.database() as db:
            rows = db.execute(
                """
                SELECT month, day, image_path, message
                FROM special_days
                ORDER BY month, day
                """
            ).fetchall()

        if not rows:
            return (
                '<tr><td colspan="4" class="empty">'
                'No specific-date overrides have been added.'
                '</td></tr>'
            )

        output = []

        for row in rows:
            month = int(row["month"])
            day = int(row["day"])

            if month in range(1, 13):
                date_name = (
                    f"{calendar.month_name[month]} {day}"
                )
            else:
                date_name = f"{month}/{day}"

            output.append(
                "<tr>"
                f"<td>{html.escape(date_name)}</td>"
                f"<td>{html.escape(str(row['image_path'] or ''))}</td>"
                f"<td>{html.escape(str(row['message'] or ''))}</td>"
                "<td>"
                f'<form method="post" action="{self.dashboard_url("/special/delete")}">'
                f'<input type="hidden" name="month" value="{month}">'
                f'<input type="hidden" name="day" value="{day}">'
                '<button class="danger small-button" type="submit">Delete</button>'
                "</form>"
                "</td>"
                "</tr>"
            )

        return "\n".join(output)

    # =====================================================
    # RECURRING TABLE
    # =====================================================

    def build_recurring_rows(self):
        weekday_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]

        with self.database() as db:
            rows = db.execute(
                """
                SELECT id, month, weekday, image_path, message
                FROM recurring_rules
                ORDER BY month, weekday
                """
            ).fetchall()

        if not rows:
            return (
                '<tr><td colspan="5" class="empty">'
                'No recurring monthly rules have been added.'
                '</td></tr>'
            )

        output = []

        for row in rows:
            rule_id = int(row["id"])
            month = int(row["month"])
            weekday = int(row["weekday"])

            month_name = (
                calendar.month_name[month]
                if month in range(1, 13)
                else str(month)
            )

            weekday_name = (
                weekday_names[weekday]
                if weekday in range(7)
                else str(weekday)
            )

            output.append(
                "<tr>"
                f"<td>{html.escape(month_name)}</td>"
                f"<td>{html.escape(weekday_name)}</td>"
                f"<td>{html.escape(str(row['image_path'] or ''))}</td>"
                f"<td>{html.escape(str(row['message'] or ''))}</td>"
                "<td>"
                f'<form method="post" action="{self.dashboard_url("/recurring/delete")}">'
                f'<input type="hidden" name="id" value="{rule_id}">'
                '<button class="danger small-button" type="submit">Delete</button>'
                "</form>"
                "</td>"
                "</tr>"
            )

        return "\n".join(output)

    # =====================================================
    # DASHBOARD
    # =====================================================

    async def dashboard(self, request):
        status = self.get_post_status()

        last_posted = (
            status["last_posted"]
            or "No posts recorded"
        )

        scheduled_clock = (
            status["scheduled_clock"]
            or "Automatic"
        )

        scheduled_time_value = (
            status["scheduled_clock"]
            or ""
        )

        page = self.render_template(
            weekly_rows=self.build_weekly_rows(),
            special_rows=self.build_special_rows(),
            recurring_rows=self.build_recurring_rows(),

            last_posted=html.escape(
                str(last_posted)
            ),

            scheduled_clock=html.escape(
                str(scheduled_clock)
            ),

            scheduled_time_value=html.escape(
                str(scheduled_time_value)
            ),
        )

        return web.Response(
            text=page,
            content_type="text/html",
        )

    # =====================================================
    # SET POSTING TIME
    # =====================================================

    async def settime(self, request):
        form = await request.post()

        clock = str(
            form.get("scheduled_clock", "")
        ).strip()

        if clock:
            try:
                datetime.strptime(
                    clock,
                    "%H:%M",
                )
            except ValueError:
                raise web.HTTPBadRequest(
                    text="Invalid time. Use HH:MM."
                )

        old_clock = self.get_post_status()["scheduled_clock"]
        with self.database() as db:
            db.execute(
                """
                UPDATE posts
                SET scheduled_clock = ?, scheduled_date = ?
                """,
                (clock or None, datetime.now(self.timezone).date().isoformat() if clock else None),
            )

        self.audit("SCHEDULE_TIME_CHANGED", old=old_clock, new=clock or None)
        self.redirect_dashboard()

    # =====================================================
    # GET IMAGE FOR TODAY
    # =====================================================

    def get_image_for_today(self):
        now = datetime.now(self.timezone)

        month = now.month
        day = now.day
        weekday = now.weekday()

        with self.database() as db:

            # 1. Specific date override
            row = db.execute(
                """
                SELECT image_path, message
                FROM special_days
                WHERE month = ? AND day = ?
                LIMIT 1
                """,
                (month, day),
            ).fetchone()

            if row:
                return (
                    row["image_path"],
                    row["message"],
                )

            # 2. Monthly recurring weekday rule
            row = db.execute(
                """
                SELECT image_path, message
                FROM recurring_rules
                WHERE month = ? AND weekday = ?
                ORDER BY id
                LIMIT 1
                """,
                (month, weekday),
            ).fetchone()

            if row:
                return (
                    row["image_path"],
                    row["message"],
                )

            # 3. Weekly configuration
            row = db.execute(
                """
                SELECT image_path, message
                FROM day_config
                WHERE weekday = ?
                LIMIT 1
                """,
                (weekday,),
            ).fetchone()

            if row:
                return (
                    row["image_path"],
                    row["message"],
                )

        # 4. Default image
        return (
            "images/default.jpg",
            "",
        )

    # =====================================================
    # POST NOW
    # =====================================================

    async def post_now(self, request):
        image_path, message = self.get_image_for_today()

        if not image_path:
            raise web.HTTPBadRequest(
                text="No image is configured."
            )

        image_file = (
            PROJECT_ROOT / image_path
        ).resolve()

        try:
            image_file.relative_to(
                IMAGE_ROOT.resolve()
            )
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid image path."
            )

        if not image_file.is_file():
            raise web.HTTPBadRequest(
                text=f"Image file not found: {image_path}"
            )

        channel_id = os.getenv(
            "GARFIE_CHANNEL_ID",
            "",
        ).strip()

        if not channel_id:
            raise web.HTTPInternalServerError(
                text="GARFIE_CHANNEL_ID is not configured."
            )

        try:
            channel_id = int(channel_id)
        except ValueError:
            raise web.HTTPInternalServerError(
                text="GARFIE_CHANNEL_ID must be a Discord channel ID."
            )

        bot = self.bot

        if bot is None:
            raise web.HTTPInternalServerError(
                text="The Discord bot instance is unavailable."
            )

        channel = bot.get_channel(channel_id)

        if channel is None:
            channel = await bot.fetch_channel(
                channel_id
            )

        await channel.send(
            content=message or None,
            file=discord.File(str(image_file)),
        )

        with self.database() as db:
            db.execute(
                """
                UPDATE posts
                SET last_posted = ?
                """,
                (
                    datetime.now(
                        self.timezone
                    ).isoformat(),
                ),
            )

        self.audit("MANUAL_POST", source="dashboard", channel_id=channel_id,
                   image=image_path, message=message, posted_at=datetime.now(self.timezone).isoformat())
        self.redirect_dashboard()

    # =====================================================
    # WEEKLY CONFIGURATION
    # =====================================================

    async def weekly_save(self, request):
        try:
            values = await self.read_upload(
                request,
                Path("images") / "weekly",
            )
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=str(error)
            )

        try:
            weekday = int(
                values.get("weekday", "")
            )
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid weekday."
            )

        if weekday not in range(7):
            raise web.HTTPBadRequest(
                text="Weekday must be between 0 and 6."
            )

        start_time, end_time = self.validate_time_range(
            values.get("start_time", "09:00"), values.get("end_time", "14:59")
        )
        with self.database() as db:
            old_config = self.describe(db.execute(
                "SELECT image_path, message, start_time, end_time FROM day_config WHERE weekday=?", (weekday,)
            ).fetchone())
        with self.database() as db:
            db.execute(
                """
                DELETE FROM day_config
                WHERE weekday = ?
                """,
                (weekday,),
            )

            db.execute(
                """
                INSERT INTO day_config
                    (weekday, image_path, message, start_time, end_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    weekday,
                    values["image_path"],
                    values.get("message", ""),
                    start_time, end_time,
                ),
            )

        with self.database() as db:
            db.execute("DELETE FROM weekly_picks WHERE weekday=? AND post_date>=?", (weekday, datetime.now(self.timezone).date().isoformat()))
        self.audit("WEEKLY_SAVED", weekday=weekday, old=old_config,
                   new={"image_path": values["image_path"], "message": values.get("message", ""),
                        "start_time": start_time, "end_time": end_time})
        self.redirect_dashboard()

    async def weekly_message(self, request):
        form = await request.post()
        try:
            weekday = int(form.get("weekday", ""))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid weekday.")
        if weekday not in range(7):
            raise web.HTTPBadRequest(text="Invalid weekday.")

        message = str(form.get("message", ""))
        if len(message) > 2000:
            raise web.HTTPBadRequest(text="Discord messages must be 2000 characters or fewer.")

        with self.database() as db:
            previous = db.execute(
                "SELECT message FROM day_config WHERE weekday=?", (weekday,)
            ).fetchone()
            if previous is None:
                raise web.HTTPNotFound(text="Weekly configuration does not exist.")
            old_message = previous["message"] or ""
            if old_message != message:
                db.execute(
                    "UPDATE day_config SET message=? WHERE weekday=?",
                    (message, weekday),
                )

        if old_message != message:
            self.audit("WEEKLY_MESSAGE_UPDATED", weekday=weekday,
                       old_message=old_message, new_message=message)
        self.redirect_dashboard()

    async def weekly_time(self, request):
        form = await request.post()
        try:
            weekday = int(form.get("weekday", ""))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid weekday.")
        if weekday not in range(7):
            raise web.HTTPBadRequest(text="Invalid weekday.")
        start_time, end_time = self.validate_time_range(
            form.get("start_time", ""), form.get("end_time", "")
        )
        with self.database() as db:
            old = db.execute(
                "SELECT start_time, end_time FROM day_config WHERE weekday=?", (weekday,)
            ).fetchone()
            if old is None:
                raise web.HTTPNotFound(text="Weekly configuration does not exist.")
            db.execute(
                "UPDATE day_config SET start_time=?, end_time=? WHERE weekday=?",
                (start_time, end_time, weekday),
            )
        with self.database() as db:
            db.execute("DELETE FROM weekly_picks WHERE weekday=? AND post_date>=?", (weekday, datetime.now(self.timezone).date().isoformat()))
        self.audit("WEEKLY_TIME_UPDATED", weekday=weekday,
                   old={"start_time": old["start_time"], "end_time": old["end_time"]},
                   new={"start_time": start_time, "end_time": end_time})
        self.redirect_dashboard()

    async def weekly_delete(self, request):
        form = await request.post()

        try:
            weekday = int(
                form.get("weekday", "")
            )
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid weekday."
            )

        with self.database() as db:
            old_config = self.describe(db.execute(
                "SELECT image_path, message FROM day_config WHERE weekday=?", (weekday,)
            ).fetchone())
        with self.database() as db:
            db.execute(
                """
                DELETE FROM day_config
                WHERE weekday = ?
                """,
                (weekday,),
            )

        # Image files are intentionally retained.
        self.audit("WEEKLY_DELETED", weekday=weekday, previous=old_config)
        self.redirect_dashboard()

    # =====================================================
    # SPECIFIC DATE CONFIGURATION
    # =====================================================

    async def special_save(self, request):
        try:
            values = await self.read_upload(
                request,
                Path("images") / "special",
            )
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=str(error)
            )

        try:
            month = int(
                values.get("month", "")
            )

            day = int(
                values.get("day", "")
            )

        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid month or day."
            )

        if month not in range(1, 13):
            raise web.HTTPBadRequest(
                text="Month must be between 1 and 12."
            )

        if day not in range(1, 32):
            raise web.HTTPBadRequest(
                text="Day must be between 1 and 31."
            )

        max_day = calendar.monthrange(
            2024,
            month,
        )[1]

        if day > max_day:
            raise web.HTTPBadRequest(
                text="That date does not exist."
            )

        with self.database() as db:
            old_config = self.describe(db.execute(
                "SELECT image_path, message FROM special_days WHERE month=? AND day=?", (month, day)
            ).fetchone())
        with self.database() as db:
            db.execute(
                """
                DELETE FROM special_days
                WHERE month = ? AND day = ?
                """,
                (month, day),
            )

            db.execute(
                """
                INSERT INTO special_days
                    (month, day, image_path, message)
                VALUES (?, ?, ?, ?)
                """,
                (
                    month,
                    day,
                    values["image_path"],
                    values.get("message", ""),
                ),
            )

        self.audit("DATE_SAVED", month=month, day=day, old=old_config,
                   new={"image_path": values["image_path"], "message": values.get("message", "")})
        self.redirect_dashboard()

    async def special_delete(self, request):
        form = await request.post()

        try:
            month = int(form.get("month", ""))
            day = int(form.get("day", ""))

        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid date."
            )

        with self.database() as db:
            old_config = self.describe(db.execute(
                "SELECT image_path, message FROM special_days WHERE month=? AND day=?", (month, day)
            ).fetchone())
        with self.database() as db:
            db.execute(
                """
                DELETE FROM special_days
                WHERE month = ? AND day = ?
                """,
                (month, day),
            )

        # Image files are intentionally retained.
        self.audit("DATE_DELETED", month=month, day=day, previous=old_config)
        self.redirect_dashboard()

    # =====================================================
    # RECURRING MONTHLY CONFIGURATION
    # =====================================================

    async def recurring_save(self, request):
        try:
            values = await self.read_upload(
                request,
                Path("images") / "recurring",
            )
        except ValueError as error:
            raise web.HTTPBadRequest(
                text=str(error)
            )

        try:
            month = int(
                values.get("month", "")
            )

            weekday = int(
                values.get("weekday", "")
            )

        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid month or weekday."
            )

        if month not in range(1, 13):
            raise web.HTTPBadRequest(
                text="Month must be between 1 and 12."
            )

        if weekday not in range(7):
            raise web.HTTPBadRequest(
                text="Weekday must be between 0 and 6."
            )

        with self.database() as db:
            cursor = db.execute(
                """
                INSERT INTO recurring_rules
                    (month, weekday, image_path, message)
                VALUES (?, ?, ?, ?)
                """,
                (
                    month,
                    weekday,
                    values["image_path"],
                    values.get("message", ""),
                ),
            )

        rule_id = cursor.lastrowid
        self.audit("RECURRING_CREATED", rule_id=rule_id, month=month, weekday=weekday,
                   image=values["image_path"], message=values.get("message", ""))
        self.redirect_dashboard()

    async def recurring_delete(self, request):
        form = await request.post()

        try:
            rule_id = int(
                form.get("id", "")
            )

        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid recurring rule."
            )

        with self.database() as db:
            old_config = self.describe(db.execute(
                "SELECT month, weekday, image_path, message FROM recurring_rules WHERE id=?", (rule_id,)
            ).fetchone())
        with self.database() as db:
            db.execute(
                """
                DELETE FROM recurring_rules
                WHERE id = ?
                """,
                (rule_id,),
            )

        # Image files are intentionally retained.
        self.audit("RECURRING_DELETED", rule_id=rule_id, previous=old_config)
        self.redirect_dashboard()

    # =====================================================
    # LOG VIEWER
    # =====================================================

    async def logs(self, request):
        if LOG_FILE.is_file():
            try:
                log_text = LOG_FILE.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                log_text = "\n".join(
                    log_text.splitlines()[-500:]
                )

            except OSError as error:
                log_text = (
                    f"Unable to read log: {error}"
                )

        else:
            log_text = (
                "No log file has been created yet."
            )

        page = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">

            <title>Garfie Logs</title>

            <style>
                body {{
                    background: #17191f;
                    color: #eeeeee;
                    font-family: Arial, sans-serif;
                    padding: 24px;
                }}

                pre {{
                    background: #22252d;
                    padding: 18px;
                    border-radius: 8px;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    line-height: 1.45;
                }}

                a {{
                    color: #8ab4ff;
                }}
            </style>
        </head>

        <body>
            <h1>Garfie Log</h1>

            <p>
                <a href="{html.escape(self.dashboard_url('/'), quote=True)}">
                    Back to dashboard
                </a>
            </p>

            <pre>{html.escape(log_text)}</pre>
        </body>
        </html>
        """

        return web.Response(
            text=page,
            content_type="text/html",
        )

    # =====================================================
    # ROUTES
    # =====================================================

    def register_routes(self):
        self.app.router.add_get(
            "/",
            self.dashboard,
        )

        self.app.router.add_post(
            "/settime",
            self.settime,
        )

        self.app.router.add_post(
            "/post-now",
            self.post_now,
        )

        self.app.router.add_post(
            "/weekly/save",
            self.weekly_save,
        )

        self.app.router.add_post("/weekly/time", self.weekly_time)
        self.app.router.add_post("/weekly/message", self.weekly_message)
        self.app.router.add_post("/weekly/reroll", self.weekly_reroll)

        self.app.router.add_post(
            "/weekly/delete",
            self.weekly_delete,
        )

        self.app.router.add_post(
            "/special/save",
            self.special_save,
        )

        self.app.router.add_post(
            "/special/delete",
            self.special_delete,
        )

        self.app.router.add_post(
            "/recurring/save",
            self.recurring_save,
        )

        self.app.router.add_post(
            "/recurring/delete",
            self.recurring_delete,
        )

        self.app.router.add_get(
            "/logs",
            self.logs,
        )
        
    # =====================================================
    # ERROR LOGGING
    # =====================================================
    def log(self, message, *args, **kwargs):
        if args:
            try:
                message = message % args
            except (TypeError, ValueError):
                message = f"{message} {' '.join(map(str, args))}"

        if kwargs:
            message = f"{message} " + " ".join(
                f"{key}={value}" for key, value in kwargs.items()
            )

        logger.info("%s", message)

    def log_exception(self, *args, **kwargs):
        """
        Compatibility method used by recurring_image.py.
        Logs the active exception and any supplied context.
        """

        details = " ".join(
            str(value) for value in args
        )

        if kwargs:
            details += " " + " ".join(
                f"{key}={value}"
                for key, value in kwargs.items()
            )

        logger.exception(
            "Garfie exception: %s",
            details or "Unspecified error",
        )

    # =====================================================
    # START / STOP
    # =====================================================

    async def start(self):
        if not self.web_token:
            raise RuntimeError(
                "GARFIE_WEB_TOKEN is required to start the administrative dashboard. "
                "Set it in .env before enabling the dashboard."
            )

        if self.runner is not None:
            return

        self.runner = web.AppRunner(
            self.app
        )

        await self.runner.setup()

        self.site = web.TCPSite(
            self.runner,
            self.host,
            self.port,
        )

        await self.site.start()

        logger.info(
            "Garfie dashboard started at http://%s:%s",
            self.host,
            self.port,
        )

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()

        self.runner = None
        self.site = None

        logger.info(
            "Garfie dashboard stopped."
        )
