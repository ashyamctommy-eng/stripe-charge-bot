# Stripe Charge Checker Bot

Telegram bot that attempts real Stripe charges through `gospelpianosimple.com/checkout`.

## Commands

| Command | Description |
|---|---|
| `/sh cc\|mm\|yy\|cvv` | Single charge attempt |
| `/check [N]` | Mass charge check with live progress |
| `/bin <BIN>` | BIN lookup |
| `/results` | Show approved charges |
| `/status` | Session status |
| `/addcards` | Load cards from replied message |
| `/addproxy` | Load proxies from replied message |
| `/saved` | Show saved approved cards |
| `/clear` | Reset session |

## Features

- **Live progress** — message updates after every card during `/check`
- **Proxy retry** — up to 3 retries with different proxies on system errors
- **File upload** — drop `.txt` files, auto-detected as cards or proxies
- **Reply mode** — paste cards, reply `/check` = instant run

## Deploy

1. Set `BOT_TOKEN` env var (Telegram bot token)
2. Deploy on Railway — auto-deploys on GitHub push
3. Health check at `GET /` on port 8080
