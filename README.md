<div align="center">

# 💸 Spending — Telegram finance tracker

**Log an expense in a few taps, split it across your own categories, shops and wallets, keep several currencies side by side, and see where the month actually went.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white&style=for-the-badge)](requirements.txt)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.x%20async-2CA5E0?logo=telegram&logoColor=white&style=for-the-badge)](requirements.txt)
[![matplotlib](https://img.shields.io/badge/matplotlib-charts-11557C?style=for-the-badge)](requirements.txt)
[![SQLite](https://img.shields.io/badge/SQLite-storage-003B57?logo=sqlite&logoColor=white&style=for-the-badge)](#tech)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white&style=for-the-badge)](docker-compose.yml)

</div>

A button-driven Telegram bot for tracking personal expenses, built as a bigger sibling of
[cigarette-counter-bot](https://github.com/BreraDMR/cigarette-counter-bot). You log a
spend in a few taps, split it across your own **categories** and **shops**, keep money in
named **storage places** (wallet, safe, jacket…), move funds between them, and compete on
several **leaderboards** — solo, with friends, or inside a **family** with shared
categories and a family board.

Everything is in Russian (the target users are Russian-speaking). Amounts are stored in the
currency of each operation and converted on the fly, so you can mix currencies freely.

## Features

- **➕ Expense** — amount → category → shop → storage place → optional note. Type the
  currency right in the amount (`500 грн`, `12 eur`); otherwise it defaults to the storage
  place's currency.
- **Categories & shops** — you create your own. A category (e.g. *Groceries*) holds shops
  (*Lidl / Penny / Tesco*), so you can see not only *how much* per category but *where*.
  In a family they are shared automatically: add a category and every member sees it.
- **💰 Wallets (storage places)** — Wallet / Safe / Jacket / Card… each with its own
  currency. Live balances, **top‑ups**, and **transfers** between places. Per‑place privacy
  toggle (hide a place's balance from the family).
- **📊 Statistics** — summary, spending by category, by shop, category → shops drill‑down,
  by day, by weekday, cumulative, balances, and budget progress bars. Excel‑style charts.
- **💳 Budgets** — monthly limits, overall and per category, with 🟢/🟡/🔴 warnings.
- **🏆 Leaderboards** — most frugal this week / this month, biggest spenders all‑time, and a
  family board. Podium 🥇🥈🥉 + full list, all converted to the currency you choose.
- **👨‍👩‍👧 Families** — invite‑code based. Shared categories/shops and a family leaderboard.
- **⚙️ Settings** — display name, display currency, categories, shops, budgets, family, and
  a global‑leaderboard opt‑out.
- **📝 Edit** — fix the amount or time of any past expense, or delete it.
- Multi‑currency: EUR / USD / UAH / CZK / PLN, live rates from
  [open.er-api.com](https://open.er-api.com) (free, no key) with disk cache and a safe
  hard‑coded fallback.

## Tech

- Python 3.12, [python-telegram-bot](https://python-telegram-bot.org/) 21.x
- SQLite (no external DB), matplotlib for charts
- Docker / docker‑compose, data persisted in a `./data` volume

## Project layout

```
bot/
  main.py     # Telegram handlers, menus, conversations
  db.py       # SQLite layer (users, families, categories, merchants, accounts,
              # transactions, transfers, budgets)
  charts.py   # matplotlib → PNG
  rates.py    # currency rates + conversion (cached, with fallback)
Dockerfile
docker-compose.yml
requirements.txt
```

## Run it yourself

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy the env template and fill in the token:
   ```bash
   cp .env.example .env
   # edit .env → BOT_TOKEN=...
   ```
3. Start it:
   ```bash
   docker compose up -d --build
   ```

The SQLite database and the rates cache live in `./data` (git‑ignored). Change the
timezone with the `TZ` variable in `.env` / `docker-compose.yml`.

## Commands

`/start`, `/add`, `/stats`, `/wallets`, `/top`, `/settings`, `/edit`, `/dellast`,
`/setname`, `/help` — or just use the reply‑keyboard buttons.

## License

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE) — free for personal,
educational, and other noncommercial use. Commercial use requires a separate
license; contact damir.brera.eb@gmail.com.

