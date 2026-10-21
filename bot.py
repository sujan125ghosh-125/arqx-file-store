"""ARQX File Store Bot.

Python 3.11+ | python-telegram-bot v22 | Supabase PostgreSQL | Vercel webhook
Regular users see only /start and /help in the command menu.
Owners/admins receive a per-chat command menu in their private chat.
"""

import logging
import secrets
import os
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

from telegram import (
    CopyTextButton,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_IDS = {int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
STORAGE_CHAT_ID = int(os.environ.get("STORAGE_CHAT_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("arqx_file_store")

# Telegram command names must be lowercase and contain only supported characters.
USER_COMMANDS = [
    BotCommand("start", "Start the bot or open a file link"),
    BotCommand("help", "Show help and available commands"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("addfile", "Add a file to private storage"),
    BotCommand("cancel", "Cancel the pending file upload"),
    BotCommand("listfiles", "List stored files and share links"),
    BotCommand("share", "Create a new share link by file code"),
    BotCommand("remove", "Remove a file by name or code"),
    BotCommand("rename", "Rename a file in the catalog"),
    BotCommand("addchannel", "Add a Force Join channel"),
    BotCommand("removechannel", "Remove a Force Join channel"),
    BotCommand("listchannel", "List Force Join channels"),
    BotCommand("forcejoin", "Enable or disable Force Join"),
]

OWNER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand("addadmin", "Grant admin access to a user ID"),
    BotCommand("removeadmin", "Revoke admin access from a user ID"),
    BotCommand("listadmins", "List owners and admins"),
]

# -----------------------------------------------------------------------------
# DATABASE
# -----------------------------------------------------------------------------

class PostgresCompatConnection:
    """Small adapter for the original bot's qmark SQL placeholders."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        return self._connection.execute(query, params or ())

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not configured.")
    raw = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10, sslmode="require", prepare_threshold=None)
    conn = PostgresCompatConnection(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                storage_chat_id INTEGER NOT NULL,
                storage_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                share_code TEXT PRIMARY KEY,
                file_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by INTEGER,
                FOREIGN KEY(file_code) REFERENCES files(code) ON DELETE CASCADE
            )
        """)
        # Preserve any legacy duplicate names by leaving them visible; new uploads
        # and renames are checked transactionally before insertion/update.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                invite_link TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL,
                added_by INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS command_menu_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_uploads (
                user_id BIGINT PRIMARY KEY,
                chat_id BIGINT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('forcejoin', 'off') ON CONFLICT DO NOTHING"
        )

        now = datetime.now(timezone.utc).isoformat()
        for owner_id in OWNER_IDS:
            owner_id = int(owner_id)
            conn.execute(
                "INSERT INTO admins (user_id, added_at, added_by) VALUES (?, ?, NULL) ON CONFLICT DO NOTHING",
                (owner_id, now),
            )
            conn.execute(
                "INSERT INTO command_menu_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
                (owner_id,),
            )

        seeded = conn.execute(
            "SELECT value FROM settings WHERE key = 'initial_admins_seeded'"
        ).fetchone()
        if not seeded or seeded["value"] != "1":
            for admin_id in ADMIN_IDS:
                admin_id = int(admin_id)
                conn.execute(
                    "INSERT INTO admins (user_id, added_at, added_by) VALUES (?, ?, NULL) ON CONFLICT DO NOTHING",
                    (admin_id, now),
                )
                conn.execute(
                    "INSERT INTO command_menu_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
                    (admin_id,),
                )
            conn.execute("""
                INSERT INTO settings (key, value) VALUES ('initial_admins_seeded', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
            """)


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))


def is_owner_user(user_id: int) -> bool:
    return int(user_id) in {int(uid) for uid in OWNER_IDS}


def is_admin_user(user_id: int) -> bool:
    if is_owner_user(user_id):
        return True
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return row is not None


def get_admin_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
    return sorted({int(row["user_id"]) for row in rows} | {int(uid) for uid in OWNER_IDS})


def get_known_menu_user_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM command_menu_users").fetchall()
    return sorted({int(row["user_id"]) for row in rows})


def get_file_by_code(code: str):
    with db() as conn:
        return conn.execute("SELECT * FROM files WHERE code = ?", (code,)).fetchone()


def find_files(name_or_code: str):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM files
            WHERE LOWER(name) = LOWER(?) OR code = ?
            ORDER BY created_at DESC
        """, (name_or_code, name_or_code)).fetchall()


def get_channels():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM channels ORDER BY LOWER(title)"
        ).fetchall()

# -----------------------------------------------------------------------------
# COMMAND MENU SCOPES
# -----------------------------------------------------------------------------

async def set_admin_menu(application: Application, user_id: int) -> None:
    """Set a private-chat menu for one authorized admin/owner."""
    commands = OWNER_COMMANDS if is_owner_user(user_id) else ADMIN_COMMANDS
    try:
        await application.bot.set_my_commands(
            commands,
            scope=BotCommandScopeChat(chat_id=int(user_id)),
        )
        with db() as conn:
            conn.execute(
                "INSERT INTO command_menu_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
                (int(user_id),),
            )
        logger.info("Registered privileged command menu for user %s", user_id)
    except TelegramError:
        # A newly added admin may not have started the bot privately yet.
        # The admin record is still valid; retry on next startup or after /start.
        logger.warning(
            "Could not set private command menu for user %s. They may need to start the bot first.",
            user_id,
            exc_info=True,
        )


async def clear_admin_menu(application: Application, user_id: int) -> None:
    """Remove the per-chat override so the default user menu is inherited."""
    try:
        await application.bot.delete_my_commands(
            scope=BotCommandScopeChat(chat_id=int(user_id))
        )
    except TelegramError:
        logger.warning("Could not clear command menu for user %s", user_id, exc_info=True)

def set_pending_upload(user_id: int, chat_id: int) -> None:
    with db() as conn:
        conn.execute("INSERT INTO pending_uploads (user_id, chat_id) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id", (int(user_id), int(chat_id)))


def get_pending_upload_chat(user_id: int):
    with db() as conn:
        row = conn.execute("SELECT chat_id FROM pending_uploads WHERE user_id = ?", (int(user_id),)).fetchone()
    return int(row["chat_id"]) if row else None


def clear_pending_upload(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM pending_uploads WHERE user_id = ?", (int(user_id),))


# -----------------------------------------------------------------------------
# COMMON HELPERS / MESSAGE TEXT
# -----------------------------------------------------------------------------

async def require_admin(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user and is_admin_user(user.id):
        return True
    if message:
        await message.reply_text(
            "⛔ ACCESS DENIED\n\nYou are not authorized to use this command."
        )
    return False


async def require_owner(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user and is_owner_user(user.id):
        return True
    if message:
        await message.reply_text(
            "⛔ OWNER PERMISSION REQUIRED\n\nOnly a configured bot owner can perform this action."
        )
    return False


def get_file_info(message):
    media_types = [
        ("document", message.document),
        ("video", message.video),
        ("animation", message.animation),
        ("audio", message.audio),
        ("voice", message.voice),
        ("video_note", message.video_note),
        ("sticker", message.sticker),
    ]
    for file_type, media in media_types:
        if media:
            name = getattr(media, "file_name", None)
            if not name and file_type == "audio":
                name = media.title
            if not name:
                name = f"{file_type}_{message.message_id}"
            return name, file_type, int(media.file_size or 0)

    if message.photo:
        photo = message.photo[-1]
        return f"photo_{message.message_id}.jpg", "photo", int(photo.file_size or 0)
    return None


def format_size(size: int) -> str:
    if not size:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return "Unknown"


def compact_name(name: str, limit: int = 75) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


async def make_share_link(context: ContextTypes.DEFAULT_TYPE, code: str) -> str:
    bot = await context.bot.get_me()
    return f"https://t.me/{bot.username}?start=file_{code}"


def copy_actions_keyboard(
    file_code: str,
    share_link: str | None = None,
    share_code: str | None = None,
) -> InlineKeyboardMarkup:
    """Build Telegram native clipboard buttons for codes and share links."""
    rows = [[
        InlineKeyboardButton(
            "📋 Copy File Code",
            copy_text=CopyTextButton(text=file_code),
        )
    ]]
    if share_code:
        rows[0].append(
            InlineKeyboardButton(
                "🧩 Copy Share Code",
                copy_text=CopyTextButton(text=share_code),
            )
        )
    if share_link:
        rows.append([
            InlineKeyboardButton(
                "🔗 Copy Share Link",
                copy_text=CopyTextButton(text=share_link),
            )
        ])
    return InlineKeyboardMarkup(rows)


async def deliver_file(context: ContextTypes.DEFAULT_TYPE, user_id: int, file_row) -> None:
    await context.bot.copy_message(
        chat_id=user_id,
        from_chat_id=file_row["storage_chat_id"],
        message_id=file_row["storage_message_id"],
    )

# -----------------------------------------------------------------------------
# USER COMMANDS / SHARE LINKS
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    # Refresh a known admin's menu when they start or restart the bot.
    if is_admin_user(user.id):
        await set_admin_menu(context.application, user.id)

    if context.args and (context.args[0].startswith("file_") or context.args[0].startswith("share_")):
        payload = context.args[0]
        if payload.startswith("file_"):
            code = payload[5:]
        else:
            share_code = payload[6:]
            with db() as conn:
                share_row = conn.execute(
                    "SELECT file_code FROM shares WHERE share_code = ?", (share_code,)
                ).fetchone()
            code = share_row["file_code"] if share_row else ""
        file_row = get_file_by_code(code) if code else None
        if not file_row:
            await message.reply_text(
                "❌ FILE UNAVAILABLE\n\nThis share link is invalid or the file has been removed."
            )
            return

        if get_setting("forcejoin", "off") == "on":
            missing = await get_missing_channels(context, user.id)
            if missing:
                await message.reply_text(
                    "🔒 MEMBERSHIP REQUIRED\n\n"
                    "Join every required channel below, then tap “Verify Membership”.",
                    reply_markup=join_keyboard(missing, code),
                )
                return

        try:
            await deliver_file(context, user.id, file_row)
        except TelegramError:
            logger.exception("File delivery failed for user %s", user.id)
            await message.reply_text(
                "❌ FILE DELIVERY FAILED\n\nThe file could not be sent. Please try again later or contact the administrator."
            )
        return

    await message.reply_text(
        "📦 ARQX FILE STORE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Welcome!\n\n"
        "Open a file share link to receive a stored file here.\n"
        "If Force Join is enabled, you must join all required channels first.\n\n"
        "Use /help to view the available commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin = bool(user and is_admin_user(user.id))
    owner = bool(user and is_owner_user(user.id))
    lines = [
        "📘 ARQX FILE STORE — HELP",
        "━━━━━━━━━━━━━━━━━━━━",
        "USER COMMANDS",
        "/start — Start the bot or open a file share link",
        "/help — Show this command guide",
    ]
    if admin:
        lines += [
            "",
            "ADMIN COMMANDS",
            "/addfile — Start a file upload",
            "/cancel — Cancel the pending upload",
            "/listfiles — List files and share links",
            "/share FILE_CODE — Create a new share link",
            "/remove FILE_NAME_OR_CODE — Remove a stored file",
            "/rename OLD_NAME | NEW_NAME — Rename a catalog entry",
            "/addchannel CHAT_ID INVITE_LINK — Add a Force Join channel",
            "/removechannel CHAT_ID — Remove a channel",
            "/listchannel — List configured channels",
            "/forcejoin on|off — Enable or disable Force Join",
        ]
    if owner:
        lines += [
            "",
            "OWNER COMMANDS",
            "/addadmin USER_ID — Grant admin access",
            "/removeadmin USER_ID — Revoke admin access",
            "/listadmins — List configured admins and owners",
        ]
    lines += [
        "",
        "Admin commands are checked by the bot even if a command is entered manually.",
    ]
    await update.effective_message.reply_text("\n".join(lines))

# -----------------------------------------------------------------------------
# ADD / CANCEL FILE
# -----------------------------------------------------------------------------

async def addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "🔒 For privacy, start the bot in a private chat and use /addfile there."
        )
        return
    set_pending_upload(update.effective_user.id, update.effective_chat.id)
    await update.effective_message.reply_text(
        "📤 ADD FILE\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Send one supported file or media item in this chat.\n\n"
        "Supported: document, photo, video, audio, animation, voice, video note, and sticker.\n"
        "Use /cancel to cancel this upload."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if update.effective_user:
        clear_pending_upload(update.effective_user.id)
    await update.effective_message.reply_text(
        "✅ UPLOAD CANCELLED\n\nNo new file was stored."
    )


class DuplicateFileName(Exception):
    """Raised when a case-insensitive duplicate filename is uploaded."""


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    pending_chat_id = get_pending_upload_chat(user.id)
    if pending_chat_id is None:
        return
    if not is_admin_user(user.id):
        clear_pending_upload(user.id)
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if pending_chat_id != update.effective_chat.id:
        return

    info = get_file_info(message)
    if not info:
        await message.reply_text(
            "⚠️ Unsupported message. Send a file or supported media, or use /cancel."
        )
        return

    name, file_type, file_size = info
    storage_message_id = None
    try:
        copied = await context.bot.copy_message(
            chat_id=STORAGE_CHAT_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        storage_message_id = copied.message_id
        code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        created_at = datetime.now(timezone.utc).isoformat()

        # Compare names case-insensitively before inserting the catalog record.
        with db() as conn:
            conn.execute("BEGIN")
            existing = conn.execute(
                "SELECT code FROM files WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (name,),
            ).fetchone()
            if existing:
                raise DuplicateFileName(name)
            conn.execute("""
                INSERT INTO files (
                    code, name, file_type, file_size,
                    storage_chat_id, storage_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                code, name, file_type, file_size,
                STORAGE_CHAT_ID, storage_message_id, created_at,
            ))

        clear_pending_upload(user.id)
        link = await make_share_link(context, code)
        await message.reply_text(
            "✅ FILE STORED SUCCESSFULLY\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 File name: {compact_name(name)}\n"
            f"🗂 File type: {file_type}\n"
            f"💾 File size: {format_size(file_size)}\n"
            f"🔑 File code: {code}\n\n"
            f"🔗 SHARE LINK\n{link}\n\n"
            "ℹ️ Anyone with this link can request the file, subject to the current Force Join settings.\n"
            "👇 Use the buttons below to copy the file code or share link.",
            reply_markup=copy_actions_keyboard(code, share_link=link),
        )
    except DuplicateFileName:
        clear_pending_upload(user.id)
        if storage_message_id is not None:
            try:
                await context.bot.delete_message(
                    chat_id=STORAGE_CHAT_ID,
                    message_id=storage_message_id,
                )
            except TelegramError:
                logger.warning("Could not clean up duplicate-name storage message")
        await message.reply_text(
            "⚠️ DUPLICATE FILE NAME\n━━━━━━━━━━━━━━━━━━━━\n"
            f'A file named "{compact_name(name)}" already exists.\n'
            "Rename or remove the existing file before uploading another with the same name."
        )
    except Exception:
        logger.exception("Unable to store file")
        clear_pending_upload(user.id)
        if storage_message_id is not None:
            try:
                await context.bot.delete_message(
                    chat_id=STORAGE_CHAT_ID,
                    message_id=storage_message_id,
                )
            except TelegramError:
                logger.warning("Could not clean up an orphan storage message")
        await message.reply_text(
            "❌ FILE SAVE FAILED\n\nCheck the storage group ID, bot permissions, and database settings."
        )

# -----------------------------------------------------------------------------
# FILE LIST / REMOVE / RENAME
# -----------------------------------------------------------------------------

async def listfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    with db() as conn:
        total_row = conn.execute("SELECT COUNT(*) AS total FROM files").fetchone()
        rows = conn.execute(
            "SELECT * FROM files ORDER BY created_at DESC LIMIT 15"
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "📂 FILE CATALOG\n━━━━━━━━━━━━━━━━━━━━\nNo files have been stored yet.\nUse /addfile to add your first file."
        )
        return

    await update.effective_message.reply_text(
        f"📂 FILE CATALOG\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Total files: {total_row['total']}\nShowing the latest {len(rows)} file(s)."
    )
    for index, row in enumerate(rows, 1):
        link = await make_share_link(context, row["code"])
        await update.effective_message.reply_text(
            f"📄 {index:02d}. {compact_name(row['name'])}\n"
            f"🗂 Type: {row['file_type']} • 💾 Size: {format_size(row['file_size'])}\n"
            f"🔑 File code: {row['code']}\n"
            f"🔗 Share link: {link}\n\n"
            "👇 Tap a button to copy the file code or share link.",
            reply_markup=copy_actions_keyboard(row["code"], share_link=link),
        )


async def removefile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    key = " ".join(context.args).strip()
    if not key:
        await update.effective_message.reply_text(
            "📝 Usage: /remove FILE_NAME\n💡 If duplicate names exist, use /remove FILE_CODE."
        )
        return

    rows = find_files(key)
    if not rows:
        await update.effective_message.reply_text(
            "🔎 No file was found with that name or code."
        )
        return
    if len(rows) > 1:
        options = "\n".join(
            f"• {compact_name(row['name'])} — {row['code']}" for row in rows[:20]
        )
        await update.effective_message.reply_text(
            "⚠️ Multiple files have that name. Remove the exact file by code:\n\n" + options
        )
        return

    row = rows[0]
    try:
        await context.bot.delete_message(
            chat_id=row["storage_chat_id"],
            message_id=row["storage_message_id"],
        )
    except TelegramError:
        logger.exception("Could not delete storage message for code %s", row["code"])
        await update.effective_message.reply_text(
            "❌ STORAGE DELETE FAILED\n\nThe file remains in the catalog so its share link is not invalidated unexpectedly. Check the bot's storage-group permissions."
        )
        return

    with db() as conn:
        conn.execute("DELETE FROM files WHERE code = ?", (row["code"],))
    await update.effective_message.reply_text(
        "✅ FILE REMOVED\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Name: {compact_name(row['name'])}\n"
        "The catalog record and storage message were deleted."
    )


async def renamefile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    raw = " ".join(context.args)
    if "|" not in raw:
        await update.effective_message.reply_text(
            "📝 Usage: /rename OLD_NAME | NEW_NAME\nℹ️ This changes the catalog name, not the original Telegram media filename."
        )
        return

    old_name, new_name = (part.strip() for part in raw.split("|", 1))
    if not old_name or not new_name:
        await update.effective_message.reply_text("⚠️ Enter both the old and new names.")
        return
    rows = find_files(old_name)
    if not rows:
        await update.effective_message.reply_text("🔎 No file was found with that name or code.")
        return
    if len(rows) > 1:
        await update.effective_message.reply_text(
            "⚠️ Multiple files have that name. Use the exact file code as OLD_NAME."
        )
        return

    with db() as conn:
        conn.execute("BEGIN")
        duplicate = conn.execute(
            "SELECT code FROM files WHERE LOWER(name) = LOWER(?) AND code != ? LIMIT 1",
            (new_name, rows[0]["code"]),
        ).fetchone()
        if duplicate:
            await update.effective_message.reply_text(
                "⚠️ DUPLICATE FILE NAME\n\n"
                f'A file named "{compact_name(new_name)}" already exists. Choose a different name.'
            )
            return
        conn.execute(
            "UPDATE files SET name = ? WHERE code = ?",
            (new_name, rows[0]["code"]),
        )
    await update.effective_message.reply_text(
        "✅ FILE RENAMED\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Old name: {compact_name(old_name)}\nNew name: {compact_name(new_name)}\n\n"
        "Only the catalog name was changed."
    )

async def sharefile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "🔗 CREATE SHARE LINK\n━━━━━━━━━━━━━━━━━━━━\n"
            "Usage: /share FILE_CODE\n\n"
            "Use /listfiles to find the file code. Each command creates a new independent share link."
        )
        return

    file_code = context.args[0].strip()
    file_row = get_file_by_code(file_code)
    if not file_row:
        await update.effective_message.reply_text(
            "❌ FILE NOT FOUND\n\nNo file matches that code. Use /listfiles to see available file codes."
        )
        return

    share_code = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    created_at = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO shares (share_code, file_code, created_at, created_by) VALUES (?, ?, ?, ?)",
            (share_code, file_code, created_at, update.effective_user.id),
        )

    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=share_{share_code}"
    await update.effective_message.reply_text(
        "✅ NEW SHARE LINK CREATED\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File: {compact_name(file_row['name'])}\n"
        f"🔑 File code: {file_code}\n"
        f"🧩 Share code: {share_code}\n\n"
        f"🔗 Share link:\n{link}\n\n"
        "ℹ️ This link points to the selected file and follows the current Force Join settings.\n"
        "👇 Use the buttons to copy the codes or share link.",
        reply_markup=copy_actions_keyboard(
            file_code, share_link=link, share_code=share_code
        ),
    )


# -----------------------------------------------------------------------------
# FORCE JOIN / CHANNEL MANAGEMENT
# -----------------------------------------------------------------------------

async def get_missing_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    missing = []
    for channel in get_channels():
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
            )
            joined = (
                member.status in ("creator", "administrator", "member")
                or (member.status == "restricted" and bool(getattr(member, "is_member", False)))
            )
            if not joined:
                missing.append(channel)
        except TelegramError:
            # Fail closed if Telegram cannot confirm membership.
            logger.warning(
                "Membership check failed for chat %s and user %s",
                channel["chat_id"], user_id, exc_info=True,
            )
            missing.append(channel)
    return missing


def join_keyboard(channels, code: str) -> InlineKeyboardMarkup:
    buttons = []
    for channel in channels:
        buttons.append([
            InlineKeyboardButton(
                f"Join {compact_name(channel['title'], 50)}",
                url=channel["invite_link"],
            )
        ])
    buttons.append([
        InlineKeyboardButton("✅ Verify Membership", callback_data=f"check:{code}")
    ])
    return InlineKeyboardMarkup(buttons)


async def addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /addchannel CHAT_ID INVITE_LINK\n\n"
            "Example: /addchannel -1001234567890 https://t.me/+InviteCode\n"
            "Add the bot as a channel administrator before configuring the channel."
        )
        return

    chat_id, invite_link = context.args[0], context.args[1]
    parsed = urlsplit(invite_link)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("t.me", "www.t.me"):
        await update.effective_message.reply_text(
            "❌ Use a valid Telegram invite link beginning with https://t.me/."
        )
        return

    try:
        chat = await context.bot.get_chat(chat_id)
        bot_user = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=bot_user.id)
        if bot_member.status not in ("administrator", "creator"):
            await update.effective_message.reply_text(
                "❌ The bot must be an administrator in that channel before it can verify membership."
            )
            return
        title = chat.title or str(chat_id)
        with db() as conn:
            conn.execute("""
                INSERT INTO channels (chat_id, title, invite_link)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    invite_link = excluded.invite_link
            """, (str(chat.id), title, invite_link))
        await update.effective_message.reply_text(
            "✅ CHANNEL ADDED\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Title: {title}\nChat ID: {chat.id}\nInvite link: {invite_link}"
        )
    except Exception:
        logger.exception("Unable to add Force Join channel")
        await update.effective_message.reply_text(
            "❌ CHANNEL SETUP FAILED\n\nCheck the chat ID, invite link, and the bot's administrator permissions."
        )


async def removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not context.args:
        await update.effective_message.reply_text("📝 Usage: /removechannel CHAT_ID")
        return
    chat_id = context.args[0]
    with db() as conn:
        cursor = conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
    if cursor.rowcount:
        await update.effective_message.reply_text(
            f"✅ CHANNEL REMOVED\n━━━━━━━━━━━━━━━━━━━━\nChat ID: {chat_id}"
        )
    else:
        await update.effective_message.reply_text("⚠️ That chat ID is not in the channel list.")


async def listchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    channels = get_channels()
    if not channels:
        await update.effective_message.reply_text(
            "📢 FORCE JOIN CHANNELS\n━━━━━━━━━━━━━━━━━━━━\nNo channels have been configured."
        )
        return
    status = get_setting("forcejoin", "off").upper()
    await update.effective_message.reply_text(
        f"📢 FORCE JOIN CHANNELS\n━━━━━━━━━━━━━━━━━━━━\nStatus: {status}\nConfigured channels: {len(channels)}"
    )
    for channel in channels:
        await update.effective_message.reply_text(
            f"• {channel['title']}\nChat ID: {channel['chat_id']}\nInvite: {channel['invite_link']}"
        )


async def forcejoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not context.args:
        current = get_setting("forcejoin", "off").upper()
        await update.effective_message.reply_text(
            f"🔐 FORCE JOIN SETTINGS\n━━━━━━━━━━━━━━━━━━━━\nCurrent status: {current}\n\n"
            "Enable: /forcejoin on\nDisable: /forcejoin off"
        )
        return

    mode = context.args[0].lower()
    if mode not in ("on", "off"):
        await update.effective_message.reply_text("📝 Usage: /forcejoin on or /forcejoin off")
        return
    if mode == "on" and not get_channels():
        await update.effective_message.reply_text(
            "⚠️ Add at least one channel with /addchannel before enabling Force Join."
        )
        return
    set_setting("forcejoin", mode)
    await update.effective_message.reply_text(
        "✅ FORCE JOIN SETTINGS UPDATED\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {mode.upper()}"
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return

    code = query.data.split(":", 1)[1]
    row = get_file_by_code(code)
    if not row:
        await query.answer("File unavailable.", show_alert=True)
        try:
            await query.edit_message_text("❌ FILE UNAVAILABLE\n\nThis file has been removed or is no longer available.")
        except TelegramError:
            pass
        return

    if get_setting("forcejoin", "off") == "on":
        missing = await get_missing_channels(context, query.from_user.id)
        if missing:
            await query.answer("Join all required channels, then verify again.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=join_keyboard(missing, code))
            except TelegramError:
                logger.exception("Could not refresh verification keyboard")
            return

    await query.answer("Membership verified. Sending file…")
    try:
        await deliver_file(context, query.from_user.id, row)
        try:
            await query.edit_message_text(
                "✅ VERIFICATION COMPLETE\n━━━━━━━━━━━━━━━━━━━━\n"
                f"File delivered: {compact_name(row['name'])}"
            )
        except TelegramError:
            pass
    except TelegramError:
        logger.exception("Verified file delivery failed for user %s", query.from_user.id)
        try:
            await query.edit_message_text(
                "❌ VERIFICATION PASSED, BUT DELIVERY FAILED\n\n"
                "Please open the share link again or contact the administrator."
            )
        except TelegramError:
            pass

# -----------------------------------------------------------------------------
# ADMIN MANAGEMENT (OWNER ONLY)
# -----------------------------------------------------------------------------

async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "👤 ADD ADMIN\n━━━━━━━━━━━━━━━━━━━━\n"
            "Usage: /addadmin USER_ID\n\nOnly a numeric Telegram user ID is accepted."
        )
        return
    try:
        user_id = int(context.args[0])
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("⚠️ Enter a valid numeric Telegram user ID.")
        return

    if is_admin_user(user_id):
        await update.effective_message.reply_text(f"ℹ️ User ID {user_id} is already an admin or owner.")
        return

    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO admins (user_id, added_at, added_by) VALUES (?, ?, ?)",
            (user_id, now, update.effective_user.id),
        )
        conn.execute(
            "INSERT INTO command_menu_users (user_id) VALUES (?) ON CONFLICT DO NOTHING",
            (user_id,),
        )

    await set_admin_menu(context.application, user_id)
    await update.effective_message.reply_text(
        "✅ ADMIN ADDED\n━━━━━━━━━━━━━━━━━━━━\n"
        f"User ID: {user_id}\n"
        "The admin permission has been saved to SQLite. If the user has not started the bot yet, ask them to open the bot and send /start to refresh their command menu."
    )


async def removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "👤 REMOVE ADMIN\n━━━━━━━━━━━━━━━━━━━━\nUsage: /removeadmin USER_ID"
        )
        return
    try:
        user_id = int(context.args[0])
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("⚠️ Enter a valid numeric Telegram user ID.")
        return

    if is_owner_user(user_id):
        await update.effective_message.reply_text(
            "⛔ A configured owner cannot be removed with /removeadmin."
        )
        return

    with db() as conn:
        cursor = conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    if cursor.rowcount:
        # Clear the scoped admin menu; the default /start and /help menu applies again.
        await clear_admin_menu(context.application, user_id)
        await update.effective_message.reply_text(
            "✅ ADMIN REMOVED\n━━━━━━━━━━━━━━━━━━━━\n"
            f"User ID: {user_id}\nAdmin access has been revoked. The standard user command menu will apply."
        )
    else:
        await update.effective_message.reply_text("❌ That user ID is not in the admin list.")


async def listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return
    ids = get_admin_ids()
    lines = ["👥 ADMIN DIRECTORY", "━━━━━━━━━━━━━━━━━━━━"]
    for idx, user_id in enumerate(ids, 1):
        role = "OWNER" if is_owner_user(user_id) else "ADMIN"
        lines.append(f"{idx:02d}. {user_id} — {role}")
    lines.append(f"\nTotal: {len(ids)}")
    await update.effective_message.reply_text("\n".join(lines))

# -----------------------------------------------------------------------------
# STARTUP
# -----------------------------------------------------------------------------

async def post_init(application: Application):
    init_db()

    # All regular users inherit this default menu.
    await application.bot.set_my_commands(
        USER_COMMANDS,
        scope=BotCommandScopeDefault(),
    )

    # Reconcile every previously scoped menu: active admins keep elevated commands;
    # removed admins revert to the default user menu, even after a restart.
    active_ids = set(get_admin_ids())
    for user_id in get_known_menu_user_ids():
        if user_id in active_ids:
            await set_admin_menu(application, user_id)
        else:
            await clear_admin_menu(application, user_id)

    # Ensure currently configured admins and owners are tracked and scoped.
    for user_id in active_ids:
        await set_admin_menu(application, user_id)

    storage = await application.bot.get_chat(STORAGE_CHAT_ID)
    if storage.type not in ("group", "supergroup"):
        raise RuntimeError("STORAGE_CHAT_ID must point to a private Telegram group or supergroup.")

    logger.info("Database ready; command menus registered; storage group accessible.")


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addfile", addfile))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("listfiles", listfiles))
    application.add_handler(CommandHandler("share", sharefile))
    application.add_handler(CommandHandler("remove", removefile))
    application.add_handler(CommandHandler("rename", renamefile))
    application.add_handler(CommandHandler("addchannel", addchannel))
    application.add_handler(CommandHandler("removechannel", removechannel))
    application.add_handler(CommandHandler("listchannel", listchannel))
    application.add_handler(CommandHandler("forcejoin", forcejoin))
    application.add_handler(CommandHandler("addadmin", addadmin))
    application.add_handler(CommandHandler("removeadmin", removeadmin))
    application.add_handler(CommandHandler("listadmins", listadmins))
    application.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^check:"))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_file))
    return application


if __name__ == "__main__":
    raise SystemExit("This build is intended for Vercel webhook deployment. See README.md.")
