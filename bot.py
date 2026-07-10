"""
Admin Post Generator Bot
-------------------------
Standalone admin-only helper bot. It does NOT deliver episodes and does NOT
replace your existing deep-link delivery bot. Its job:

  1. Ask you (the admin) for: Anime Name, Short Code, Episode Count,
     Starting Message ID.
  2. Calculate every message_id for every quality of every episode.
  3. Detect short-code conflicts and let you choose how to handle them.
  4. Back up, then append the new callback_key -> message_id entries to
     config.json (the SAME config.json your delivery bot reads from).
  5. Send you one nicely formatted message per episode, in your private
     chat, each with quality buttons (and an optional second row) pointing
     at your delivery bot's deep links. You then forward these wherever
     you want.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC-..."      # this admin bot's own token
    export ADMIN_ID="123456789"            # your numeric Telegram user id
    # delivery bot username: set ONE of these
    export BOT_USERNAME="MyAnimeBot"       # env var, OR put "bot_username"
                                            # in config.json (env wins)
    python bot.py

Command:
    /generate_posts   - start the guided flow
    /cancel            - abort at any point
"""

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config / secrets
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "")
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))
QUALITIES = ["480", "720"]  # order matters: matches your key format

# How often to edit the progress message (every N episodes processed).
PROGRESS_UPDATE_EVERY = 10
# Minimum seconds between progress edits, to stay well under Telegram's
# per-chat rate limits even on very large batches.
PROGRESS_MIN_INTERVAL_SECONDS = 1.0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID environment variable is not set.")
ADMIN_ID = int(ADMIN_ID_RAW)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    ANIME_NAME,
    SHORT_CODE,
    NUM_EPISODES,
    START_MSG_ID,
    CONFIRM,          # waiting on inline Generate/Preview/Cancel button
    CONFLICT,         # waiting on Continue/Replace/Cancel button
    PREVIEW,          # showing dry-run output, waiting on Generate/Cancel
    SEND_APPROVAL,    # per-episode: Send to Channel / Skip / Stop All
) = range(8)

# Callback data values
CB_GENERATE = "gen:generate"
CB_PREVIEW = "gen:preview"
CB_CANCEL = "gen:cancel"
CB_CONFLICT_CONTINUE = "gen:conflict_continue"
CB_CONFLICT_REPLACE = "gen:conflict_replace"
CB_CONFLICT_CANCEL = "gen:conflict_cancel"
CB_EP_SEND = "ep:send"
CB_EP_SKIP = "ep:skip"
CB_EP_STOP = "ep:stop"

# ---------------------------------------------------------------------------
# config.json helpers
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.warning("config.json is empty or invalid, starting fresh.")
                return {}
    return {}


def save_config(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def backup_config() -> Path | None:
    """Create a timestamped backup of config.json. Returns the backup path,
    or None if there was nothing to back up yet."""
    if not CONFIG_PATH.exists():
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = CONFIG_PATH.with_name(f"config_backup_{timestamp}.json")
    shutil.copy2(CONFIG_PATH, backup_path)
    logger.info("Backed up config.json -> %s", backup_path)
    return backup_path


def get_bot_username() -> str:
    """Env var takes priority; falls back to config.json's "bot_username" key."""
    env_value = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
    if env_value:
        return env_value
    config = load_config()
    config_value = str(config.get("bot_username", "")).strip().lstrip("@")
    return config_value


# Optional, configurable second row of buttons. Configure via config.json:
#   "extra_buttons": [
#       {"text": "📢 Updates", "url": "https://t.me/MyUpdatesChannel"},
#       {"text": "❤️ Support", "url": "https://t.me/MySupportChannel"}
#   ]
def get_extra_button_row() -> list[InlineKeyboardButton] | None:
    config = load_config()
    extra = config.get("extra_buttons")
    if not extra:
        return None
    row = []
    for item in extra:
        text = item.get("text")
        url = item.get("url")
        if text and url:
            row.append(InlineKeyboardButton(text, url=url))
    return row or None


DEFAULT_EPISODE_TEMPLATE = "🎬 {anime}\n\n📺 Episode {episode}\n\nSelect your preferred quality:"


def get_episode_template() -> str:
    """Configurable via config.json's "episode_template" key. Falls back to
    the default format if missing. Supports {anime} and {episode}."""
    config = load_config()
    template = config.get("episode_template")
    if template:
        return template
    return DEFAULT_EPISODE_TEMPLATE


def get_pin_setting() -> bool:
    """Configurable via config.json's "pin_generated_messages" key (default False)."""
    config = load_config()
    return bool(config.get("pin_generated_messages", False))


def get_post_channel() -> int:
    """Where episode posts are sent. Falls back to ADMIN_ID (your DM) if not set.
    Set "post_channel_id" in config.json to send directly to a channel."""
    config = load_config()
    channel = config.get("post_channel_id")
    if channel:
        return int(channel)
    return ADMIN_ID


def build_episode_plan(short_code: str, num_episodes: int, start_id: int):
    """
    Returns a list of dicts, one per episode:
    {
        "episode": 1,
        "keys": {"480": "jjk_ep1480", "720": "jjk_ep1720"},
        "message_ids": {"480": 311, "720": 312},
    }
    """
    plan = []
    for n in range(1, num_episodes + 1):
        base = start_id + ((n - 1) * len(QUALITIES))
        message_ids = {q: base + i for i, q in enumerate(QUALITIES)}
        keys = {q: f"{short_code}_ep{n}{q}" for q in QUALITIES}
        plan.append({"episode": n, "keys": keys, "message_ids": message_ids})
    return plan


def find_conflicting_keys(config: dict, plan: list) -> list[str]:
    conflicts = []
    for ep in plan:
        for q in QUALITIES:
            key = ep["keys"][q]
            if key in config:
                conflicts.append(key)
    return conflicts


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
async def admin_only(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        if update.effective_message:
            await update.effective_message.reply_text("This command is admin-only.")
        return False
    return True


# ---------------------------------------------------------------------------
# Conversation handlers — collection steps
# ---------------------------------------------------------------------------
async def generate_posts_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_only(update):
        return ConversationHandler.END

    if not get_bot_username():
        await update.message.reply_text(
            "No delivery bot username configured.\n\n"
            "Set the BOT_USERNAME environment variable, or add "
            '"bot_username": "MyAnimeBot" to config.json, then try again.'
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "Let's generate episode posts.\n\n"
        "Step 1/4 — Send the Anime Name (e.g. Jujutsu Kaisen).\n"
        "Send /cancel anytime to stop."
    )
    return ANIME_NAME


async def anime_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["anime_name"] = update.message.text.strip()
    await update.message.reply_text("Step 2/4 — Send the Short Code (e.g. jjk).")
    return SHORT_CODE


async def short_code_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().lower()
    if not code.isalnum():
        await update.message.reply_text(
            "Short code should be alphanumeric (letters/numbers only). Try again."
        )
        return SHORT_CODE
    context.user_data["short_code"] = code
    await update.message.reply_text("Step 3/4 — Send the Number of Episodes (e.g. 24).")
    return NUM_EPISODES


async def num_episodes_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Please send a positive whole number.")
        return NUM_EPISODES
    context.user_data["num_episodes"] = int(text)
    await update.message.reply_text(
        "Step 4/4 — Send the Starting Message ID for Episode 1 / 480p "
        "(the message_id of the first uploaded file in your storage channel)."
    )
    return START_MSG_ID


async def start_msg_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Please send a positive whole number.")
        return START_MSG_ID
    context.user_data["start_msg_id"] = int(text)

    d = context.user_data
    plan = build_episode_plan(d["short_code"], d["num_episodes"], d["start_msg_id"])
    context.user_data["plan"] = plan
    bot_username = get_bot_username()
    context.user_data["bot_username"] = bot_username

    last = plan[-1]
    summary = (
        "Please confirm:\n\n"
        f"Anime: {d['anime_name']}\n"
        f"Short Code: {d['short_code']}\n"
        f"Episodes: {d['num_episodes']}\n"
        f"Starting Message ID: {d['start_msg_id']}\n"
        f"Bot Username: @{bot_username}\n\n"
        f"Episode 1 message IDs: {plan[0]['message_ids']}\n"
        f"Episode {last['episode']} message IDs: {last['message_ids']}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Generate", callback_data=CB_GENERATE),
                InlineKeyboardButton("👁 Preview", callback_data=CB_PREVIEW),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
            ]
        ]
    )
    await update.message.reply_text(summary, reply_markup=keyboard)
    return CONFIRM


# ---------------------------------------------------------------------------
# Confirmation + conflict handling (inline buttons)
# ---------------------------------------------------------------------------
def build_preview_text(plan: list, config: dict) -> str:
    """Pure simulation text: callbacks + message IDs + conflict flags. Never
    touches config.json, never creates a backup, never sends episode posts."""
    lines = ["👁 Preview (dry run — nothing has been changed)\n"]
    conflict_count = 0
    for ep in plan:
        n = ep["episode"]
        lines.append(f"Episode {n}")
        for q in QUALITIES:
            key = ep["keys"][q]
            mid = ep["message_ids"][q]
            flag = ""
            if key in config:
                flag = "  ⚠️ already exists in config.json"
                conflict_count += 1
            lines.append(f"{q}p → message_id {mid}{flag}")
        lines.append("")
        lines.append("Callbacks")
        for q in QUALITIES:
            lines.append(ep["keys"][q])
        lines.append("")

    if conflict_count:
        lines.append(f"⚠️ {conflict_count} key(s) already exist. Generate will ask how to handle them.")
    else:
        lines.append("No conflicts detected.")
    return "\n".join(lines)


async def confirm_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CB_CANCEL:
        await query.edit_message_text("Cancelled. No changes were made.")
        context.user_data.clear()
        return ConversationHandler.END

    d = context.user_data
    plan = d["plan"]

    if query.data == CB_PREVIEW:
        config = load_config()
        # Telegram messages cap at 4096 chars; split into chunks if a huge
        # batch would overflow a single message. The original confirmation
        # message keeps its buttons so Generate/Cancel still work afterward.
        text = build_preview_text(plan, config)
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [text]
        for chunk in chunks:
            await context.bot.send_message(chat_id=ADMIN_ID, text=chunk)
        return CONFIRM

    # CB_GENERATE -> validate for conflicts before touching anything
    config = load_config()
    conflicts = find_conflicting_keys(config, plan)

    if conflicts:
        context.user_data["conflicts"] = conflicts
        preview = ", ".join(conflicts[:5])
        more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Continue (only missing)", callback_data=CB_CONFLICT_CONTINUE)],
                [InlineKeyboardButton("♻️ Replace existing entries", callback_data=CB_CONFLICT_REPLACE)],
                [InlineKeyboardButton("❌ Cancel", callback_data=CB_CONFLICT_CANCEL)],
            ]
        )
        await query.edit_message_text(
            f"The short code \"{d['short_code']}\" already has {len(conflicts)} "
            f"existing entr{'y' if len(conflicts) == 1 else 'ies'} in config.json:\n"
            f"{preview}{more}\n\n"
            "Choose an option:",
            reply_markup=keyboard,
        )
        return CONFLICT

    # No conflicts -> proceed straight to generation
    await query.edit_message_text("Generating posts...\n\nProgress:\n0 / " + str(len(plan)))
    return await run_generation(update, context, status_message=query.message, mode="append")


async def conflict_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CB_CONFLICT_CANCEL:
        await query.edit_message_text("Cancelled. No changes were made.")
        context.user_data.clear()
        return ConversationHandler.END

    mode = "continue" if query.data == CB_CONFLICT_CONTINUE else "replace"
    plan = context.user_data["plan"]
    await query.edit_message_text("Generating posts...\n\nProgress:\n0 / " + str(len(plan)))
    return await run_generation(update, context, status_message=query.message, mode=mode)


# ---------------------------------------------------------------------------
# Generation (validate -> backup -> write -> send posts) with progress edits
# ---------------------------------------------------------------------------
async def run_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, status_message, mode: str) -> int:
    d = context.user_data
    plan = d["plan"]
    anime_name = d["anime_name"]
    bot_username = d["bot_username"]
    total = len(plan)

    # ---- Step: load + validate (already validated for conflicts upstream) ----
    config = load_config()

    # ---- Step: backup before any write ----
    try:
        backup_path = backup_config()
    except Exception as e:
        logger.error("Backup failed: %s", e)
        await status_message.edit_text(
            f"Backup of config.json failed ({e}). Aborting before any changes were made."
        )
        context.user_data.clear()
        return

    # ---- Step: build the new config in memory, then write once ----
    added, cfg_skipped, replaced = 0, 0, 0
    for ep in plan:
        for q in QUALITIES:
            key = ep["keys"][q]
            exists = key in config
            if exists and mode == "continue":
                cfg_skipped += 1
                continue
            if exists and mode == "replace":
                replaced += 1
            elif not exists:
                added += 1
            config[key] = {"message_id": ep["message_ids"][q]}

    try:
        save_config(config)
    except Exception as e:
        logger.error("Writing config.json failed: %s", e)
        backup_note = f" A backup is available at {backup_path}." if backup_path else ""
        await status_message.edit_text(
            f"Writing config.json failed ({e}).{backup_note} No posts were sent."
        )
        context.user_data.clear()
        return

    logger.info(
        "config.json updated (mode=%s): %d added, %d replaced, %d skipped",
        mode, added, replaced, cfg_skipped,
    )

    # ---- config.json summary (shown once, stays visible in chat) ----
    summary_lines = [f"✅ config.json updated: {added} added"]
    if replaced:
        summary_lines.append(f"   {replaced} replaced")
    if cfg_skipped:
        summary_lines.append(f"   {cfg_skipped} skipped (already existed)")
    if backup_path:
        summary_lines.append(f"   Backup: {backup_path.name}")
    summary_lines.append(f"\nReady to send {total} episode(s) one by one for your approval.")
    await status_message.edit_text("\n".join(summary_lines))

    # ---- Kick off per-episode approval ----
    context.user_data["ep_index"] = 0
    context.user_data["ep_sent"] = 0
    context.user_data["ep_skipped"] = 0
    context.user_data["ep_failed"] = []
    return await show_episode_for_approval(context)


async def show_episode_for_approval(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the episode preview (with real URL buttons) + a control prompt to
    the admin DM. Returns SEND_APPROVAL so the ConversationHandler waits."""
    d = context.user_data
    plan = d["plan"]
    idx = d["ep_index"]
    total = len(plan)

    if idx >= total:
        await send_final_summary(context)
        context.user_data.clear()
        return ConversationHandler.END

    ep = plan[idx]
    n = ep["episode"]
    anime_name = d["anime_name"]
    bot_username = d["bot_username"]
    extra_row = get_extra_button_row()
    template = get_episode_template()
    post_channel = get_post_channel()

    text = template.format(anime=anime_name, episode=n)
    quality_row = [
        InlineKeyboardButton(f"{q}p", url=f"https://t.me/{bot_username}?start={ep['keys'][q]}")
        for q in QUALITIES
    ]
    rows = [quality_row]
    if extra_row:
        rows.append(extra_row)

    # 1. Show the exact message that will be posted — with real URL buttons so
    #    you can tap them to verify the deep links before approving.
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        reply_markup=InlineKeyboardMarkup(rows),
    )

    # 2. Control prompt directly below.
    channel_label = f"channel {post_channel}" if post_channel != ADMIN_ID else "your DM"
    control_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send to Channel", callback_data=CB_EP_SEND),
        InlineKeyboardButton("⏭ Skip",            callback_data=CB_EP_SKIP),
        InlineKeyboardButton("🛑 Stop",            callback_data=CB_EP_STOP),
    ]])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"Episode {n} of {total} — Send to {channel_label}?",
        reply_markup=control_keyboard,
    )
    return SEND_APPROVAL


async def episode_approval_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle ✅ Send / ⏭ Skip / 🛑 Stop for each episode."""
    query = update.callback_query
    await query.answer()

    d = context.user_data
    plan = d["plan"]
    idx = d["ep_index"]
    ep = plan[idx]
    n = ep["episode"]

    if query.data == CB_EP_STOP:
        await query.edit_message_text("🛑 Stopped. No more episodes will be sent.")
        await send_final_summary(context)
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == CB_EP_SKIP:
        await query.edit_message_text(f"⏭ Episode {n} skipped.")
        d["ep_skipped"] += 1

    elif query.data == CB_EP_SEND:
        anime_name = d["anime_name"]
        bot_username = d["bot_username"]
        extra_row = get_extra_button_row()
        template = get_episode_template()
        should_pin = get_pin_setting()
        post_channel = get_post_channel()

        text = template.format(anime=anime_name, episode=n)
        quality_row = [
            InlineKeyboardButton(f"{q}p", url=f"https://t.me/{bot_username}?start={ep['keys'][q]}")
            for q in QUALITIES
        ]
        rows = [quality_row]
        if extra_row:
            rows.append(extra_row)

        try:
            sent_msg = await context.bot.send_message(
                chat_id=post_channel,
                text=text,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            d["ep_sent"] += 1
            await query.edit_message_text(f"✅ Episode {n} sent to channel.")
            if should_pin:
                try:
                    await context.bot.pin_chat_message(
                        chat_id=post_channel,
                        message_id=sent_msg.message_id,
                        disable_notification=True,
                    )
                except Exception as pin_err:
                    logger.warning("Pinning episode %d failed (skipping): %s", n, pin_err)
        except Exception as e:
            logger.error("Failed to send episode %d: %s", n, e)
            d["ep_failed"].append(n)
            await query.edit_message_text(f"❌ Episode {n} failed: {e}")

    # Advance to the next episode.
    d["ep_index"] += 1
    return await show_episode_for_approval(context)


async def send_final_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a session summary to the admin DM after all episodes are processed."""
    d = context.user_data
    total = len(d.get("plan", []))
    sent = d.get("ep_sent", 0)
    skipped = d.get("ep_skipped", 0)
    failed = d.get("ep_failed", [])
    lines = [
        "📋 All done!",
        f"Total: {total}  |  Sent: {sent}  |  Skipped: {skipped}",
    ]
    if failed:
        lines.append(f"Failed episodes: {failed}")
    await context.bot.send_message(chat_id=ADMIN_ID, text="\n".join(lines))


# ---------------------------------------------------------------------------
# Misc handlers
# ---------------------------------------------------------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("generate_posts", generate_posts_start)],
        states={
            ANIME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, anime_name_received)],
            SHORT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, short_code_received)],
            NUM_EPISODES: [MessageHandler(filters.TEXT & ~filters.COMMAND, num_episodes_received)],
            START_MSG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_msg_id_received)],
            CONFIRM: [CallbackQueryHandler(confirm_button_pressed, pattern=r"^gen:(generate|preview|cancel)$")],
            CONFLICT: [CallbackQueryHandler(conflict_button_pressed, pattern=r"^gen:conflict_")],
            SEND_APPROVAL: [CallbackQueryHandler(episode_approval_button, pattern=r"^ep:(send|skip|stop)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Admin post generator bot starting (polling)...")
    application.run_polling()
