"""Телеграм-бот «Spending» — трекер личных финансов с кнопками.

Возможности:
- ➕ Трата: сумма (в любой валюте) → сфера → магазин → место хранения → комментарий.
- Сферы (категории) и магазины внутри них добавляются пользователями и общие в семье.
- 💰 Кошельки: места хранения денег (Кошелёк/Сейф/Куртка…), балансы, переводы между
  ними, пополнения. У каждого места своя валюта и своя настройка приватности.
- 📊 Статистика: сводка, разбивка по сферам и магазинам, по дням/дням недели,
  накопительно, балансы, прогресс по бюджетам.
- 🏆 Рейтинг: несколько досок (экономные за неделю/месяц, «транжиры», семейный) —
  пьедестал 🥇🥈🥉, всё сконвертировано в выбранную пользователем валюту.
- 👨‍👩‍👧 Семьи: общие сферы/магазины и семейный рейтинг, вход по коду-приглашению.

Интерфейс на русском. Данные — в SQLite. Суммы хранятся в валюте операции,
конвертация — «на лету» (см. bot/rates.py).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import charts, db, rates

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
log = logging.getLogger("finance-bot")

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ── Кнопки главного меню ─────────────────────────────────────────
BTN_ADD = "➕ Трата"
BTN_DELLAST = "🗑 Удалить последнюю"
BTN_STATS = "📊 Статистика"
BTN_WALLETS = "💰 Кошельки"
BTN_TOP = "🏆 Рейтинг"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "✖️ Отмена"
BTN_SKIP = "⏭ Пропустить"

MAIN_BTNS = [BTN_ADD, BTN_DELLAST, BTN_STATS, BTN_WALLETS, BTN_TOP, BTN_SETTINGS, BTN_HELP]

# ── Состояния диалогов ───────────────────────────────────────────
(
    NAME,
    A_AMOUNT, A_CAT, A_MERCH, A_ACC, A_NOTE, A_NEWCAT, A_NEWMERCH, A_NEWACC_NAME, A_NEWACC_CUR,
    NA_NAME, NA_CUR,
    TU_AMOUNT,
    TF_AMOUNT,
    CM_ADD,
    MM_MENU, MM_ADD,
    BU_AMOUNT,
    E_STATE,
) = range(19)


# ── Клавиатуры ───────────────────────────────────────────────────
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD],
            [BTN_DELLAST],
            [BTN_STATS, BTN_WALLETS],
            [BTN_TOP, BTN_SETTINGS],
            [BTN_HELP],
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def skip_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_SKIP], [BTN_CANCEL]], resize_keyboard=True)


def currency_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{rates.SYMBOL[c]} {c}", callback_data=f"{prefix}:{rates.SYMBOL[c]}")]
            for c in rates.ORDER]
    return InlineKeyboardMarkup(rows)


def _rows(buttons: list[InlineKeyboardButton], per: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per] for i in range(0, len(buttons), per)]


# ── Утилиты ──────────────────────────────────────────────────────
def fmt_money(value: float, currency: str) -> str:
    s = f"{value:,.0f}" if abs(value - round(value)) < 0.005 else f"{value:,.1f}"
    return s.replace(",", " ") + f" {currency}"


def parse_amount(text: str) -> tuple[float | None, str | None]:
    """Разбирает «500», «500 грн», «12.5 eur» → (сумма, символ валюты | None)."""
    t = text.strip().replace(",", ".")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(.*)$", t)
    if not m:
        return None, None
    amount = float(m.group(1))
    rest = m.group(2).strip()
    iso = rates.iso(rest) if rest else None
    return amount, (rates.SYMBOL.get(iso) if iso else None)


def split_emoji(text: str) -> tuple[str, str]:
    """«🍔 Еда» → ('Еда', '🍔'); «Еда» → ('Еда', '')."""
    parts = text.strip().split(None, 1)
    if len(parts) == 2 and not re.search(r"[\w\d]", parts[0], re.UNICODE):
        return parts[1].strip(), parts[0]
    return text.strip(), ""


def week_bounds(offset: int = 0) -> tuple[str, str]:
    today = date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
    return monday.isoformat(), (monday + timedelta(days=7)).isoformat()


def month_bounds() -> tuple[str, str]:
    today = date.today()
    first = today.replace(day=1)
    nxt = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    return first.isoformat(), nxt.isoformat()


def cat_label(cat: dict) -> str:
    return f"{cat['emoji']} {cat['name']}".strip()


def category_name(uid: int, cat_id: int | None) -> str:
    if cat_id is None:
        return "—"
    cat = db.get_category(cat_id)
    return cat_label(cat) if cat else "—"


def ensure_user(update: Update) -> None:
    u = update.effective_user
    db.upsert_user(u.id, u.username, u.first_name)


# ── /start и смена имени ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_new = not db.user_exists(u.id)
    db.upsert_user(u.id, u.username, u.first_name)
    if is_new:
        await update.message.reply_html(
            "👋 Привет! Я <b>Spending</b> — помогу считать траты.\n\n"
            "Как тебя показывать в рейтинге? Напиши имя или нажми «Пропустить».",
            reply_markup=ReplyKeyboardMarkup([[BTN_SKIP]], resize_keyboard=True),
        )
        return NAME
    name = db.get_display_name(u.id) or u.first_name
    await update.message.reply_html(
        f"С возвращением, <b>{name}</b>! 💸\n\n" + HELP_TEXT, reply_markup=main_kb()
    )
    return ConversationHandler.END


async def setname_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    current = db.get_display_name(update.effective_user.id)
    await update.message.reply_html(
        f"Текущее имя: <b>{current}</b>.\nНапиши новое или «Пропустить».",
        reply_markup=ReplyKeyboardMarkup([[BTN_SKIP]], resize_keyboard=True),
    )
    return NAME


async def setname_from_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(update)
    current = db.get_display_name(query.from_user.id)
    await query.message.reply_html(
        f"Текущее имя: <b>{current}</b>.\nНапиши новое или «Пропустить».",
        reply_markup=ReplyKeyboardMarkup([[BTN_SKIP]], resize_keyboard=True),
    )
    return NAME


async def name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (update.message.text or "").strip()
    if text == BTN_SKIP:
        name = db.get_display_name(u.id) or u.first_name
        await update.message.reply_html(f"Ок, оставил имя <b>{name}</b>.", reply_markup=main_kb())
        return ConversationHandler.END
    if not text or len(text) > 32:
        await update.message.reply_text(
            "Имя должно быть от 1 до 32 символов. Попробуй ещё раз.",
            reply_markup=ReplyKeyboardMarkup([[BTN_SKIP]], resize_keyboard=True),
        )
        return NAME
    db.set_display_name(u.id, text)
    await update.message.reply_html(f"Готово! Буду звать тебя <b>{text}</b>. 👍",
                                    reply_markup=main_kb())
    return ConversationHandler.END


# ── Добавление траты ─────────────────────────────────────────────
def categories_inline(uid: int, prefix: str, extra: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    cats = db.list_categories(uid)
    btns = [InlineKeyboardButton(cat_label(c), callback_data=f"{prefix}:{c['id']}") for c in cats]
    rows = _rows(btns, 2)
    rows.append([InlineKeyboardButton("➕ Новая сфера", callback_data=f"{prefix}:new")])
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def merchants_inline(uid: int, cat_id: int, prefix: str) -> InlineKeyboardMarkup:
    ms = db.list_merchants(uid, cat_id)
    btns = [InlineKeyboardButton(m["name"], callback_data=f"{prefix}:{m['id']}") for m in ms]
    rows = _rows(btns, 2)
    rows.append([InlineKeyboardButton("➕ Новый магазин", callback_data=f"{prefix}:new")])
    rows.append([InlineKeyboardButton("⏭ Без магазина", callback_data=f"{prefix}:skip")])
    return InlineKeyboardMarkup(rows)


def accounts_inline(uid: int, prefix: str) -> InlineKeyboardMarkup:
    accs = db.list_accounts(uid)
    btns = [InlineKeyboardButton(f"{a['emoji']} {a['name']} ({a['currency']})".strip(),
                                 callback_data=f"{prefix}:{a['id']}") for a in accs]
    rows = _rows(btns, 2)
    rows.append([InlineKeyboardButton("➕ Новое место", callback_data=f"{prefix}:new")])
    rows.append([InlineKeyboardButton("➖ Без места хранения", callback_data=f"{prefix}:none")])
    return InlineKeyboardMarkup(rows)


async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    context.user_data["add"] = {}
    cur = db.get_currency(update.effective_user.id)
    await update.message.reply_html(
        f"💸 Сколько потратил? Напиши сумму — например <code>500</code> или "
        f"<code>500 грн</code> (валюта по умолчанию — {cur}).",
        reply_markup=cancel_kb(),
    )
    return A_AMOUNT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    amount, cur = parse_amount(raw)
    if amount is None or amount <= 0 or amount > 100_000_000:
        await update.message.reply_text("Не понял сумму 🤔 Напиши число, например 250.",
                                        reply_markup=cancel_kb())
        return A_AMOUNT
    context.user_data["add"] = {"amount": amount, "currency": cur}
    await update.message.reply_html(
        f"Сумма: <b>{fmt_money(amount, cur or db.get_currency(u.id))}</b>\nВыбери сферу:",
        reply_markup=categories_inline(u.id, "acat"),
    )
    return A_CAT


async def add_cat_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "new":
        await query.edit_message_text("Название новой сферы (можно с эмодзи в начале, "
                                      "напр. «🍔 Еда»):")
        return A_NEWCAT
    context.user_data["add"]["category_id"] = int(data)
    cat = db.get_category(int(data))
    await query.edit_message_text(f"Сфера: {cat_label(cat)}\nГде потратил?")
    await query.message.reply_text("Выбери магазин:",
                                   reply_markup=merchants_inline(query.from_user.id, int(data), "amer"))
    return A_MERCH


async def add_newcat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    name, emoji = split_emoji(raw)
    if not name or len(name) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return A_NEWCAT
    cat_id = db.add_category(u.id, name, emoji)
    context.user_data["add"]["category_id"] = cat_id
    await update.message.reply_html(f"Добавил сферу {emoji} <b>{name}</b>. Выбери магазин:",
                                    reply_markup=merchants_inline(u.id, cat_id, "amer"))
    return A_MERCH


async def add_merch_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "new":
        await query.edit_message_text("Название магазина (напр. «Lidl»):")
        return A_NEWMERCH
    if data == "skip":
        context.user_data["add"]["merchant_id"] = None
    else:
        context.user_data["add"]["merchant_id"] = int(data)
    await query.edit_message_text("Откуда списать деньги?")
    await query.message.reply_text("Выбери место хранения:",
                                   reply_markup=accounts_inline(query.from_user.id, "aacc"))
    return A_ACC


async def add_newmerch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    if not raw or len(raw) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return A_NEWMERCH
    cat_id = context.user_data["add"].get("category_id")
    mid = db.add_merchant(u.id, cat_id, raw)
    context.user_data["add"]["merchant_id"] = mid
    await update.message.reply_html(f"Добавил магазин <b>{raw}</b>. Выбери место хранения:",
                                    reply_markup=accounts_inline(u.id, "aacc"))
    return A_ACC


async def add_acc_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "new":
        await query.edit_message_text("Название нового места хранения (напр. «💳 Карта», «Сейф»):")
        return A_NEWACC_NAME
    if data == "none":
        context.user_data["add"]["account_id"] = None
    else:
        context.user_data["add"]["account_id"] = int(data)
    await query.edit_message_text("Комментарий к трате? (необязательно)")
    await query.message.reply_text("Напиши комментарий или нажми «Пропустить».",
                                   reply_markup=skip_kb())
    return A_NOTE


async def add_newacc_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    name, emoji = split_emoji(raw)
    if not name or len(name) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return A_NEWACC_NAME
    context.user_data["newacc"] = {"name": name, "emoji": emoji}
    await update.message.reply_text("В какой валюте это место хранения?",
                                    reply_markup=currency_kb("acur"))
    return A_NEWACC_CUR


async def add_newacc_cur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sym = query.data.split(":", 1)[1]
    na = context.user_data.get("newacc", {})
    acc_id = db.add_account(query.from_user.id, na.get("name", "Кошелёк"), na.get("emoji", ""), sym)
    context.user_data["add"]["account_id"] = acc_id
    context.user_data.pop("newacc", None)
    await query.edit_message_text(f"Создал место хранения {na.get('emoji','')} "
                                  f"{na.get('name','')} ({sym}).".strip())
    await query.message.reply_text("Комментарий к трате? (необязательно)",
                                   reply_markup=skip_kb())
    return A_NOTE


async def add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    note = None if raw == BTN_SKIP else raw[:200]
    add = context.user_data.get("add", {})
    if "amount" not in add:
        await update.message.reply_text("Что-то пошло не так, начни заново.", reply_markup=main_kb())
        context.user_data.pop("add", None)
        return ConversationHandler.END

    acc_id = add.get("account_id")
    acc = db.get_account(acc_id) if acc_id else None
    # валюта: явно указанная → валюта места хранения → валюта отображения
    currency = add.get("currency") or (acc["currency"] if acc else db.get_currency(u.id))
    db.add_transaction(u.id, "expense", add["amount"], currency,
                       add.get("category_id"), add.get("merchant_id"), acc_id, note)
    context.user_data.pop("add", None)

    # Сводка
    disp = db.get_currency(u.id)
    m_start, m_end = month_bounds()
    today = date.today().isoformat()
    month_spent = db.expense_sum(u.id, disp, m_start, m_end)
    today_spent = db.expense_sum(u.id, disp, today, None)

    parts = [f"✅ Трата <b>{fmt_money(add['amount'], currency)}</b>"]
    if add.get("category_id"):
        parts.append(f"· {category_name(u.id, add['category_id'])}")
    if add.get("merchant_id"):
        m = db.get_merchant(add["merchant_id"])
        if m:
            parts.append(f"· 🏪 {m['name']}")
    if acc:
        parts.append(f"· из {acc['emoji']} {acc['name']}".rstrip())
    lines = [" ".join(parts).strip(),
             f"\n📅 Сегодня: <b>{fmt_money(today_spent, disp)}</b>",
             f"🗓 Этот месяц: <b>{fmt_money(month_spent, disp)}</b>"]

    warn = _budget_warning(u.id, add.get("category_id"))
    if warn:
        lines.append(warn)
    await update.message.reply_html("\n".join(lines), reply_markup=main_kb())
    return ConversationHandler.END


def _budget_warning(uid: int, cat_id: int | None) -> str | None:
    budgets = {b["category_id"]: b for b in db.get_budgets(uid)}
    m_start, m_end = month_bounds()
    checks = []
    if cat_id in budgets:
        checks.append((budgets[cat_id], cat_id))
    if None in budgets:
        checks.append((budgets[None], None))
    for b, cid in checks:
        rows = db.expense_rows(uid, m_start, m_end)
        spent = sum(rates.convert(float(r["amount"]), r["currency"], b["currency"])
                    for r in rows if (cid is None or r["category_id"] == cid))
        limit = b["amount"]
        if limit <= 0:
            continue
        pct = spent / limit * 100
        label = "общий бюджет" if cid is None else category_name(uid, cid)
        if spent > limit:
            return (f"⚠️ Превышен {label}: {fmt_money(spent, b['currency'])} из "
                    f"{fmt_money(limit, b['currency'])} ({pct:.0f}%)")
        if pct >= 80:
            return (f"🟡 {label.capitalize()}: {fmt_money(spent, b['currency'])} из "
                    f"{fmt_money(limit, b['currency'])} ({pct:.0f}%)")
    return None


# ── Удалить последнюю трату ──────────────────────────────────────
async def dellast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(update)
    tx = db.last_transaction(u.id, "expense")
    if not tx:
        await update.message.reply_text("Трат пока нет.", reply_markup=main_kb())
        return
    db.delete_transaction(tx["id"])
    when = tx["created_at"].replace("T", " ")[5:16]
    await update.message.reply_html(
        f"🗑 Удалил трату #{tx['id']} на <b>{fmt_money(tx['amount'], tx['currency'])}</b> ({when}).",
        reply_markup=main_kb(),
    )


# ── Статистика ───────────────────────────────────────────────────
def stats_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Сводка", callback_data="st:summary")],
        [InlineKeyboardButton("🥧 По сферам", callback_data="st:cat"),
         InlineKeyboardButton("🏪 По магазинам", callback_data="st:merch")],
        [InlineKeyboardButton("📂 Сфера → магазины", callback_data="st:catmerch"),
         InlineKeyboardButton("📈 По дням", callback_data="st:days")],
        [InlineKeyboardButton("🗓 По дням недели", callback_data="st:weekday"),
         InlineKeyboardButton("Σ Накопительно", callback_data="st:cumul")],
        [InlineKeyboardButton("💰 Балансы", callback_data="st:balances"),
         InlineKeyboardButton("💳 Бюджеты", callback_data="st:budgets")],
    ])


async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_html("📊 <b>Статистика</b> (за текущий месяц, где не указано иначе):",
                                    reply_markup=stats_menu_kb())


async def stats_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    what = query.data.split(":", 1)[1]
    msg = query.message
    disp = db.get_currency(u.id)
    m_start, m_end = month_bounds()

    if what == "summary":
        await _send_summary(msg, u.id)
        return
    if what == "catmerch":
        cats = db.list_categories(u.id)
        if not cats:
            await msg.reply_text("Сфер пока нет.", reply_markup=main_kb())
            return
        btns = [InlineKeyboardButton(cat_label(c), callback_data=f"scm:{c['id']}") for c in cats]
        await msg.reply_text("Выбери сферу для разбивки по магазинам:",
                             reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))
        return
    if what == "balances":
        accs = db.list_accounts(u.id)
        if not accs:
            await msg.reply_text("Мест хранения пока нет. Добавь их в «💰 Кошельки».",
                                 reply_markup=main_kb())
            return
        items = [(f"{a['emoji']} {a['name']}".strip(),
                  rates.convert(db.account_balance(a["id"]), a["currency"], disp)) for a in accs]
        items = [(n, v) for n, v in items if abs(v) > 0.005] or items
        img = charts.hbar_chart(sorted(items, key=lambda x: x[1]), disp, "Баланс по местам хранения")
        await msg.reply_photo(img, caption="💰 Где сейчас сколько денег")
        return
    if what == "budgets":
        await _send_budgets(msg, u.id)
        return

    rows = db.expense_rows(u.id, m_start, m_end)
    if not rows:
        await msg.reply_text("За этот месяц трат нет.", reply_markup=main_kb())
        return

    if what == "cat":
        agg: dict[int | None, float] = {}
        for r in rows:
            v = rates.convert(float(r["amount"]), r["currency"], disp)
            agg[r["category_id"]] = agg.get(r["category_id"], 0.0) + v
        items = sorted(((category_name(u.id, cid), s) for cid, s in agg.items()),
                       key=lambda x: x[1], reverse=True)
        img = charts.pie_chart(items, disp, "Траты по сферам")
        await msg.reply_photo(img, caption="🥧 На что ушли деньги (этот месяц)")
    elif what == "merch":
        agg = {}
        for r in rows:
            name = "🏪 —"
            if r["merchant_id"]:
                m = db.get_merchant(r["merchant_id"])
                name = m["name"] if m else "🏪 —"
            v = rates.convert(float(r["amount"]), r["currency"], disp)
            agg[name] = agg.get(name, 0.0) + v
        items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
        img = charts.pie_chart(items, disp, "Траты по магазинам")
        await msg.reply_photo(img, caption="🏪 Где ушли деньги (этот месяц)")
    elif what == "days":
        agg = {}
        for r in rows:
            day = r["created_at"][:10]
            agg[day] = agg.get(day, 0.0) + rates.convert(float(r["amount"]), r["currency"], disp)
        days = sorted(agg.items())
        img = charts.days_bar_chart(days, disp, "Траты по дням")
        await msg.reply_photo(img, caption="📈 По дням (этот месяц)")
    elif what == "weekday":
        vals = [0.0] * 7
        for r in rows:
            wd = datetime.fromisoformat(r["created_at"]).weekday()
            vals[wd] += rates.convert(float(r["amount"]), r["currency"], disp)
        img = charts.weekday_chart(vals, WEEKDAYS, disp, "Траты по дням недели")
        await msg.reply_photo(img, caption="🗓 По дням недели (этот месяц)")
    elif what == "cumul":
        allrows = db.expense_rows(u.id)
        points = [(r["created_at"], rates.convert(float(r["amount"]), r["currency"], disp))
                  for r in allrows]
        img = charts.cumulative_chart(points, disp, "Всего потрачено")
        await msg.reply_photo(img, caption="Σ Накопительно (за всё время)")


async def stats_catmerch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    cat_id = int(query.data.split(":", 1)[1])
    disp = db.get_currency(u.id)
    m_start, m_end = month_bounds()
    rows = [r for r in db.expense_rows(u.id, m_start, m_end) if r["category_id"] == cat_id]
    if not rows:
        await query.message.reply_text("В этой сфере за месяц трат нет.", reply_markup=main_kb())
        return
    agg: dict[str, float] = {}
    for r in rows:
        name = "— без магазина"
        if r["merchant_id"]:
            m = db.get_merchant(r["merchant_id"])
            name = m["name"] if m else name
        agg[name] = agg.get(name, 0.0) + rates.convert(float(r["amount"]), r["currency"], disp)
    items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
    title = f"{category_name(u.id, cat_id)} — по магазинам"
    img = charts.hbar_chart(items, disp, title)
    await query.message.reply_photo(img, caption="📂 Разбивка по магазинам (этот месяц)")


async def _send_summary(msg, uid: int):
    disp = db.get_currency(uid)
    m_start, m_end = month_bounds()
    w_start, w_end = week_bounds(0)
    today = date.today().isoformat()
    rows = db.expense_rows(uid, m_start, m_end)
    month_total = sum(rates.convert(float(r["amount"]), r["currency"], disp) for r in rows)
    today_total = db.expense_sum(uid, disp, today, None)
    week_total = db.expense_sum(uid, disp, w_start, w_end)
    all_total = db.expense_sum(uid, disp)

    days = {r["created_at"][:10] for r in rows}
    avg = month_total / len(days) if days else 0

    cat_agg: dict[int | None, float] = {}
    for r in rows:
        v = rates.convert(float(r["amount"]), r["currency"], disp)
        cat_agg[r["category_id"]] = cat_agg.get(r["category_id"], 0.0) + v
    top_cat = max(cat_agg.items(), key=lambda x: x[1]) if cat_agg else None
    biggest = max(rows, key=lambda r: rates.convert(float(r["amount"]), r["currency"], disp)) if rows else None

    total_balance = sum(rates.convert(db.account_balance(a["id"]), a["currency"], disp)
                        for a in db.list_accounts(uid))

    lines = [
        "📋 <b>Сводка</b>",
        f"📅 Сегодня: <b>{fmt_money(today_total, disp)}</b>",
        f"📆 Эта неделя: <b>{fmt_money(week_total, disp)}</b>",
        f"🗓 Этот месяц: <b>{fmt_money(month_total, disp)}</b>",
        f"💵 Всего за всё время: <b>{fmt_money(all_total, disp)}</b>",
        f"📊 В среднем в день (месяц): <b>{fmt_money(avg, disp)}</b>",
    ]
    if top_cat:
        lines.append(f"🏆 Топ сфера: <b>{category_name(uid, top_cat[0])}</b> — {fmt_money(top_cat[1], disp)}")
    if biggest:
        b = rates.convert(float(biggest["amount"]), biggest["currency"], disp)
        lines.append(f"💥 Самая крупная трата: <b>{fmt_money(b, disp)}</b>")
    lines.append(f"💰 Сейчас на всех местах хранения: <b>{fmt_money(total_balance, disp)}</b>")
    await msg.reply_html("\n".join(lines), reply_markup=main_kb())


async def _send_budgets(msg, uid: int):
    disp = db.get_currency(uid)
    budgets = db.get_budgets(uid)
    if not budgets:
        await msg.reply_text("Бюджеты не заданы. Задай их в ⚙️ Настройки → 💳 Бюджеты.",
                             reply_markup=main_kb())
        return
    m_start, m_end = month_bounds()
    rows = db.expense_rows(uid, m_start, m_end)
    lines = ["💳 <b>Бюджеты (этот месяц)</b>"]
    budgets.sort(key=lambda b: (b["category_id"] is not None, b["category_id"] or 0))
    for b in budgets:
        cid = b["category_id"]
        spent = sum(rates.convert(float(r["amount"]), r["currency"], b["currency"])
                    for r in rows if (cid is None or r["category_id"] == cid))
        limit = b["amount"]
        pct = (spent / limit * 100) if limit else 0
        filled = min(10, int(pct / 10))
        bar = "█" * filled + "░" * (10 - filled)
        label = "Общий" if cid is None else category_name(uid, cid)
        emoji = "🔴" if pct > 100 else ("🟡" if pct >= 80 else "🟢")
        lines.append(f"{emoji} <b>{label}</b>\n{bar} {pct:.0f}%\n"
                     f"{fmt_money(spent, b['currency'])} / {fmt_money(limit, b['currency'])}")
    await msg.reply_html("\n".join(lines), reply_markup=main_kb())


# ── Кошельки (места хранения) ────────────────────────────────────
def wallets_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Новое место", callback_data="wal:new")],
        [InlineKeyboardButton("💵 Пополнить", callback_data="wal:topup"),
         InlineKeyboardButton("🔁 Перевод", callback_data="wal:transfer")],
        [InlineKeyboardButton("⚙️ Управление местами", callback_data="wal:manage")],
    ])


def _wallets_text(uid: int) -> str:
    disp = db.get_currency(uid)
    accs = db.list_accounts(uid)
    lines = ["💰 <b>Кошельки — где хранятся деньги</b>"]
    if not accs:
        lines.append("\nПока нет ни одного места хранения. Добавь первое 👇")
        return "\n".join(lines)
    total = 0.0
    for a in accs:
        bal = db.account_balance(a["id"])
        total += rates.convert(bal, a["currency"], disp)
        flags = " 🔒" if a["is_private"] else ""
        flags += " ⭐" if a["is_default"] else ""
        lines.append(f"• {a['emoji']} <b>{a['name']}</b>: {fmt_money(bal, a['currency'])}{flags}")
    lines.append(f"\nИтого: <b>{fmt_money(total, disp)}</b> (⭐ — по умолчанию, 🔒 — скрыто от семьи)")
    return "\n".join(lines)


async def wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_html(_wallets_text(update.effective_user.id),
                                    reply_markup=wallets_menu_kb())


# создание места хранения (из кошельков)
async def acc_new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Название нового места хранения (напр. «💳 Карта», «Сейф»):",
                                   reply_markup=cancel_kb())
    return NA_NAME


async def acc_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    name, emoji = split_emoji(raw)
    if not name or len(name) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return NA_NAME
    context.user_data["newacc"] = {"name": name, "emoji": emoji}
    await update.message.reply_text("В какой валюте это место хранения?",
                                    reply_markup=currency_kb("nacur"))
    return NA_CUR


async def acc_new_cur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sym = query.data.split(":", 1)[1]
    na = context.user_data.get("newacc", {})
    db.add_account(query.from_user.id, na.get("name", "Кошелёк"), na.get("emoji", ""), sym)
    context.user_data.pop("newacc", None)
    await query.edit_message_text(f"✅ Создал {na.get('emoji','')} {na.get('name','')} ({sym}).".strip())
    await query.message.reply_html(_wallets_text(query.from_user.id), reply_markup=wallets_menu_kb())
    return ConversationHandler.END


# пополнение места хранения
async def topup_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accs = db.list_accounts(query.from_user.id)
    if not accs:
        await query.message.reply_text("Сначала добавь место хранения.", reply_markup=main_kb())
        return ConversationHandler.END
    btns = [InlineKeyboardButton(f"{a['emoji']} {a['name']} ({a['currency']})".strip(),
                                 callback_data=f"tup:{a['id']}") for a in accs]
    await query.message.reply_text("Какое место пополнить?", reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))
    return TU_AMOUNT


async def topup_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split(":", 1)[1])
    acc = db.get_account(acc_id)
    context.user_data["topup_acc"] = acc_id
    await query.edit_message_text(f"Пополнить {acc['emoji']} {acc['name']} ({acc['currency']}).".strip())
    await query.message.reply_html(f"На сколько? Напиши сумму (валюта по умолчанию — {acc['currency']}):",
                                   reply_markup=cancel_kb())
    return TU_AMOUNT


async def topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    acc_id = context.user_data.get("topup_acc")
    if not acc_id:
        await update.message.reply_text("Выбери место кнопкой выше.", reply_markup=cancel_kb())
        return TU_AMOUNT
    amount, cur = parse_amount(raw)
    if amount is None or amount <= 0:
        await update.message.reply_text("Не понял сумму. Напиши число:", reply_markup=cancel_kb())
        return TU_AMOUNT
    acc = db.get_account(acc_id)
    currency = cur or acc["currency"]
    db.add_transaction(u.id, "income", amount, currency, None, None, acc_id, "Пополнение")
    context.user_data.pop("topup_acc", None)
    bal = db.account_balance(acc_id)
    await update.message.reply_html(
        f"✅ Пополнил {acc['emoji']} <b>{acc['name']}</b> на {fmt_money(amount, currency)}.\n"
        f"Баланс: <b>{fmt_money(bal, acc['currency'])}</b>.",
        reply_markup=main_kb())
    return ConversationHandler.END


# перевод между местами
async def transfer_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accs = db.list_accounts(query.from_user.id)
    if len(accs) < 2:
        await query.message.reply_text("Нужно минимум два места хранения для перевода.",
                                       reply_markup=main_kb())
        return ConversationHandler.END
    context.user_data["transfer"] = {}
    btns = [InlineKeyboardButton(f"{a['emoji']} {a['name']} ({a['currency']})".strip(),
                                 callback_data=f"tff:{a['id']}") for a in accs]
    await query.message.reply_text("Откуда переводим?", reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))
    return TF_AMOUNT


async def transfer_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from_id = int(query.data.split(":", 1)[1])
    context.user_data.setdefault("transfer", {})["from"] = from_id
    accs = [a for a in db.list_accounts(query.from_user.id) if a["id"] != from_id]
    btns = [InlineKeyboardButton(f"{a['emoji']} {a['name']} ({a['currency']})".strip(),
                                 callback_data=f"tft:{a['id']}") for a in accs]
    frm = db.get_account(from_id)
    await query.edit_message_text(f"Из: {frm['emoji']} {frm['name']}. Куда?".strip())
    await query.message.reply_text("Выбери место назначения:",
                                   reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))
    return TF_AMOUNT


async def transfer_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    to_id = int(query.data.split(":", 1)[1])
    context.user_data.setdefault("transfer", {})["to"] = to_id
    frm = db.get_account(context.user_data["transfer"]["from"])
    await query.edit_message_text("Сколько перевести?")
    await query.message.reply_html(f"Напиши сумму (валюта по умолчанию — {frm['currency']}):",
                                   reply_markup=cancel_kb())
    return TF_AMOUNT


async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    tr = context.user_data.get("transfer", {})
    if "from" not in tr or "to" not in tr:
        await update.message.reply_text("Выбери места кнопками выше.", reply_markup=cancel_kb())
        return TF_AMOUNT
    amount, cur = parse_amount(raw)
    if amount is None or amount <= 0:
        await update.message.reply_text("Не понял сумму. Напиши число:", reply_markup=cancel_kb())
        return TF_AMOUNT
    frm = db.get_account(tr["from"])
    to = db.get_account(tr["to"])
    currency = cur or frm["currency"]
    db.add_transfer(u.id, tr["from"], tr["to"], amount, currency)
    context.user_data.pop("transfer", None)
    await update.message.reply_html(
        f"🔁 Перевёл {fmt_money(amount, currency)}: {frm['emoji']} {frm['name']} → "
        f"{to['emoji']} {to['name']}.\n"
        f"{frm['name']}: <b>{fmt_money(db.account_balance(frm['id']), frm['currency'])}</b> · "
        f"{to['name']}: <b>{fmt_money(db.account_balance(to['id']), to['currency'])}</b>",
        reply_markup=main_kb())
    return ConversationHandler.END


# управление местами
async def wallets_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accs = db.list_accounts(query.from_user.id)
    if not accs:
        await query.message.reply_text("Мест хранения нет.", reply_markup=main_kb())
        return
    btns = [InlineKeyboardButton(f"{a['emoji']} {a['name']}".strip(), callback_data=f"wm:{a['id']}")
            for a in accs]
    await query.message.reply_text("Выбери место для настройки:",
                                   reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))


async def wallet_manage_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split(":", 1)[1])
    acc = db.get_account(acc_id)
    if not acc or acc["user_id"] != query.from_user.id:
        await query.edit_message_text("Не найдено.")
        return
    priv = "открыть для семьи 👨‍👩‍👧" if acc["is_private"] else "скрыть от семьи 🔒"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Приватность: {priv}", callback_data=f"wmp:{acc_id}")],
        [InlineKeyboardButton("⭐ Сделать по умолчанию", callback_data=f"wmd:{acc_id}")],
        [InlineKeyboardButton("🗄 Архивировать", callback_data=f"wma:{acc_id}")],
    ])
    bal = db.account_balance(acc_id)
    await query.edit_message_text(
        f"{acc['emoji']} {acc['name']} ({acc['currency']}) — {fmt_money(bal, acc['currency'])}".strip(),
        reply_markup=kb)


async def wallet_toggle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split(":", 1)[1])
    acc = db.get_account(acc_id)
    db.set_account_private(acc_id, not acc["is_private"])
    await query.edit_message_text(
        f"Готово. {acc['name']} теперь {'🔒 скрыто от семьи' if not acc['is_private'] else '👨‍👩‍👧 видно семье'}.")
    await query.message.reply_html(_wallets_text(query.from_user.id), reply_markup=wallets_menu_kb())


async def wallet_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split(":", 1)[1])
    db.set_default_account(query.from_user.id, acc_id)
    await query.edit_message_text("⭐ Готово, теперь это место по умолчанию.")
    await query.message.reply_html(_wallets_text(query.from_user.id), reply_markup=wallets_menu_kb())


async def wallet_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split(":", 1)[1])
    db.archive_account(acc_id)
    await query.edit_message_text("🗄 Место архивировано (история сохранена).")
    await query.message.reply_html(_wallets_text(query.from_user.id), reply_markup=wallets_menu_kb())


# ── Рейтинг ──────────────────────────────────────────────────────
def top_menu_kb(in_family: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💚 Экономные (неделя)", callback_data="tb:week")],
        [InlineKeyboardButton("💚 Экономные (месяц)", callback_data="tb:month")],
        [InlineKeyboardButton("🔥 Транжиры (за всё время)", callback_data="tb:all")],
    ]
    if in_family:
        rows.append([InlineKeyboardButton("👨‍👩‍👧 Семейный (месяц)", callback_data="tb:family")])
    return InlineKeyboardMarkup(rows)


async def top_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    fid = db.get_family_id(update.effective_user.id)
    await update.message.reply_html("🏆 <b>Рейтинг</b> — выбери доску:",
                                    reply_markup=top_menu_kb(fid is not None))


async def top_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    board = query.data.split(":", 1)[1]
    disp = db.get_currency(u.id)

    if board == "week":
        start, end = week_bounds(0)
        users = db.rating_users(None)
        title, sub, reverse = "🏆 Экономные за неделю", "Меньше потратил — выше 💚", False
    elif board == "month":
        start, end = month_bounds()
        users = db.rating_users(None)
        title, sub, reverse = "🏆 Экономные за месяц", "Меньше потратил — выше 💚", False
    elif board == "all":
        start, end = None, None
        users = db.rating_users(None)
        title, sub, reverse = "🔥 Транжиры за всё время", "Больше потратил — выше", True
    else:  # family
        start, end = month_bounds()
        fid = db.get_family_id(u.id)
        users = db.rating_users(fid)
        title, sub, reverse = "👨‍👩‍👧 Семейный рейтинг (месяц)", "Меньше потратил — выше 💚", False

    scored = []
    for uid, name in users:
        s = db.expense_sum(uid, disp, start, end)
        if s > 0:
            scored.append((name, s))
    if not scored:
        await query.message.reply_text("Пока нет данных для этой доски.", reply_markup=main_kb())
        return
    scored.sort(key=lambda x: x[1], reverse=reverse)

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>{title}</b>", f"<i>{sub}</i>", ""]
    for i, (name, s) in enumerate(scored):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} <b>{name}</b> — {fmt_money(s, disp)}")
    await query.message.reply_html("\n".join(lines), reply_markup=main_kb())


# ── Настройки ────────────────────────────────────────────────────
def settings_menu_kb(uid: int) -> InlineKeyboardMarkup:
    optout = db.get_global_optout(uid)
    vis = "скрыт ❌" if optout else "виден ✅"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Имя", callback_data="se:name"),
         InlineKeyboardButton("💱 Валюта", callback_data="se:cur")],
        [InlineKeyboardButton("🗂 Сферы", callback_data="se:cats"),
         InlineKeyboardButton("🏪 Магазины", callback_data="se:merch")],
        [InlineKeyboardButton("💳 Бюджеты", callback_data="se:budget"),
         InlineKeyboardButton("👨‍👩‍👧 Семья", callback_data="se:family")],
        [InlineKeyboardButton(f"🌐 В глобальном рейтинге: {vis}", callback_data="se:global")],
        [InlineKeyboardButton("📝 Изменить траты", callback_data="se:edit")],
    ])


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(update)
    name = db.get_display_name(u.id)
    cur = db.get_currency(u.id)
    fid = db.get_family_id(u.id)
    fam = f"«{db.family_info(fid)['name']}»" if fid else "нет"
    await update.message.reply_html(
        f"⚙️ <b>Настройки</b>\nИмя: <b>{name}</b> · Валюта: <b>{cur}</b> · Семья: <b>{fam}</b>",
        reply_markup=settings_menu_kb(u.id))


async def settings_currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("В какой валюте показывать суммы и рейтинг?",
                                  reply_markup=currency_kb("secur"))


async def settings_currency_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sym = query.data.split(":", 1)[1]
    db.set_currency(query.from_user.id, sym)
    await query.edit_message_text(f"💱 Валюта отображения: {sym}. Всё сконвертирую автоматически.")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())


async def settings_global_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    new = not db.get_global_optout(u.id)
    db.set_global_optout(u.id, new)
    await query.edit_message_text(
        "🌐 Ты скрыт из глобального рейтинга." if new else "🌐 Ты снова в глобальном рейтинге.")
    await query.message.reply_html("⚙️ Настройки:", reply_markup=settings_menu_kb(u.id))


# семья
def family_menu_kb(in_family: bool) -> InlineKeyboardMarkup:
    if in_family:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Участники", callback_data="fam:members")],
            [InlineKeyboardButton("🔑 Показать код", callback_data="fam:code")],
            [InlineKeyboardButton("🚪 Выйти из семьи", callback_data="fam:leave")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать семью", callback_data="fam:create")],
        [InlineKeyboardButton("🔗 Войти по коду", callback_data="fam:join")],
    ])


async def family_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    fid = db.get_family_id(u.id)
    if fid:
        info = db.family_info(fid)
        txt = (f"👨‍👩‍👧 Семья <b>«{info['name']}»</b>\n"
               f"Общие сферы и магазины, семейный рейтинг.\nКод приглашения: <code>{info['code']}</code>")
    else:
        txt = ("👨‍👩‍👧 <b>Семья</b>\nВ семье общие сферы/магазины и общий рейтинг. "
               "Создай семью и поделись кодом — или вступи в существующую по коду.")
    await query.message.reply_html(txt, reply_markup=family_menu_kb(fid is not None))


async def family_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fid = db.get_family_id(query.from_user.id)
    if not fid:
        await query.message.reply_text("Ты не в семье.", reply_markup=main_kb())
        return
    info = db.family_info(fid)
    members = db.family_members(fid)
    lines = [f"👥 <b>Участники «{info['name']}»</b>"]
    for uid, name in members:
        crown = " 👑" if uid == info["owner_id"] else ""
        lines.append(f"• {name}{crown}")
    await query.message.reply_html("\n".join(lines), reply_markup=main_kb())


async def family_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fid = db.get_family_id(query.from_user.id)
    if not fid:
        return
    info = db.family_info(fid)
    await query.message.reply_html(
        f"🔑 Код приглашения в «{info['name']}»:\n<code>{info['code']}</code>\n"
        "Перешли его тем, кого хочешь добавить — они введут его в 👨‍👩‍👧 Семья → «Войти по коду».",
        reply_markup=main_kb())


async def family_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db.leave_family(query.from_user.id)
    await query.edit_message_text("🚪 Ты вышел из семьи. Личные сферы/магазины теперь только твои.")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())


async def family_create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["fam_mode"] = "create"
    await query.message.reply_text("Название семьи (напр. «Наша семья»):", reply_markup=cancel_kb())
    return CM_ADD  # переиспользуем текстовый ввод семьи


async def family_join_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["fam_mode"] = "join"
    await query.message.reply_text("Введи код приглашения (6 символов):", reply_markup=cancel_kb())
    return CM_ADD


async def family_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    mode = context.user_data.get("fam_mode")
    if mode == "create":
        if not raw or len(raw) > 40:
            await update.message.reply_text("Название 1–40 символов. Ещё раз:", reply_markup=cancel_kb())
            return CM_ADD
        fid, code = db.create_family(u.id, raw)
        context.user_data.pop("fam_mode", None)
        await update.message.reply_html(
            f"✅ Создал семью <b>«{raw}»</b>!\nКод приглашения: <code>{code}</code>\n"
            "Перешли его близким — они войдут по нему, и у вас будут общие сферы и рейтинг.",
            reply_markup=main_kb())
        return ConversationHandler.END
    if mode == "join":
        res = db.join_family(u.id, raw)
        context.user_data.pop("fam_mode", None)
        if not res:
            await update.message.reply_text("Код не найден 🤔 Проверь и попробуй ещё раз в меню Семья.",
                                            reply_markup=main_kb())
            return ConversationHandler.END
        await update.message.reply_html(f"✅ Ты в семье <b>«{res['name']}»</b>! "
                                        "Теперь сферы и рейтинг общие.", reply_markup=main_kb())
        return ConversationHandler.END
    await update.message.reply_text("Ок.", reply_markup=main_kb())
    return ConversationHandler.END


# управление сферами
async def cats_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cats = db.list_categories(query.from_user.id)
    lines = ["🗂 <b>Сферы</b>"]
    rows = []
    for c in cats:
        lines.append(f"• {cat_label(c)}")
        rows.append([InlineKeyboardButton(f"🗑 {cat_label(c)}", callback_data=f"caar:{c['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить сферу", callback_data="camgr:add")])
    if not cats:
        lines.append("Пока пусто — добавь первую.")
    await query.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def cat_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = int(query.data.split(":", 1)[1])
    if db.category_visible_to(query.from_user.id, cid):
        db.archive_category(cid)
        await query.edit_message_text("🗑 Сфера убрана (старые траты сохранены).")
    else:
        await query.edit_message_text("Нельзя.")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())


async def cat_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["cat_mgr"] = True
    await query.message.reply_text("Название новой сферы (можно с эмодзи, напр. «🏠 Жильё»):",
                                   reply_markup=cancel_kb())
    return CM_ADD


async def cat_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    name, emoji = split_emoji(raw)
    if not name or len(name) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return CM_ADD
    db.add_category(u.id, name, emoji)
    context.user_data.pop("cat_mgr", None)
    await update.message.reply_html(f"✅ Добавил сферу {emoji} <b>{name}</b>.", reply_markup=main_kb())
    return ConversationHandler.END


# управление магазинами
async def merch_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cats = db.list_categories(query.from_user.id)
    if not cats:
        await query.message.reply_text("Сначала добавь хотя бы одну сферу.", reply_markup=main_kb())
        return
    btns = [InlineKeyboardButton(cat_label(c), callback_data=f"mmc:{c['id']}") for c in cats]
    await query.message.reply_text("Магазины какой сферы показать?",
                                   reply_markup=InlineKeyboardMarkup(_rows(btns, 2)))


async def merch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = int(query.data.split(":", 1)[1])
    context.user_data["mm_cat"] = cid
    ms = db.list_merchants(query.from_user.id, cid)
    lines = [f"🏪 <b>Магазины: {category_name(query.from_user.id, cid)}</b>"]
    rows = []
    for m in ms:
        lines.append(f"• {m['name']}")
        rows.append([InlineKeyboardButton(f"🗑 {m['name']}", callback_data=f"maar:{m['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить магазин", callback_data="mmadd")])
    if not ms:
        lines.append("Пока пусто.")
    await query.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    return MM_MENU


async def merch_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mid = int(query.data.split(":", 1)[1])
    db.archive_merchant(mid)
    await query.edit_message_text("🗑 Магазин убран (старые траты сохранены).")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())
    return ConversationHandler.END


async def merch_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Название магазина (напр. «Tesco»):", reply_markup=cancel_kb())
    return MM_ADD


async def merch_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    if not raw or len(raw) > 32:
        await update.message.reply_text("Название 1–32 символа. Ещё раз:", reply_markup=cancel_kb())
        return MM_ADD
    cid = context.user_data.get("mm_cat")
    db.add_merchant(u.id, cid, raw)
    context.user_data.pop("mm_cat", None)
    await update.message.reply_html(
        f"✅ Добавил магазин <b>{raw}</b> в сферу {category_name(u.id, cid)}.", reply_markup=main_kb())
    return ConversationHandler.END


# бюджеты
async def budget_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = query.from_user
    budgets = {b["category_id"]: b for b in db.get_budgets(u.id)}
    rows = [[InlineKeyboardButton(
        f"💳 Общий{' — ' + fmt_money(budgets[None]['amount'], budgets[None]['currency']) if None in budgets else ''}",
        callback_data="bup:overall")]]
    for c in db.list_categories(u.id):
        tag = ""
        if c["id"] in budgets:
            tag = f" — {fmt_money(budgets[c['id']]['amount'], budgets[c['id']]['currency'])}"
        rows.append([InlineKeyboardButton(f"{cat_label(c)}{tag}", callback_data=f"bup:{c['id']}")])
    await query.message.reply_html(
        "💳 <b>Месячные бюджеты</b>\nВыбери, для чего задать лимит (0 — убрать):",
        reply_markup=InlineKeyboardMarkup(rows))


async def budget_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    context.user_data["budget_cat"] = None if key == "overall" else int(key)
    label = "общего бюджета" if key == "overall" else category_name(query.from_user.id, int(key))
    cur = db.get_currency(query.from_user.id)
    await query.message.reply_html(
        f"Введи месячный лимит для <b>{label}</b> в {cur} (или 0, чтобы убрать):",
        reply_markup=cancel_kb())
    return BU_AMOUNT


async def budget_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    amount, cur = parse_amount(raw)
    if amount is None or amount < 0:
        await update.message.reply_text("Не понял сумму. Напиши число (или 0):", reply_markup=cancel_kb())
        return BU_AMOUNT
    cat_id = context.user_data.get("budget_cat")
    currency = cur or db.get_currency(u.id)
    if amount == 0:
        db.delete_budget(u.id, cat_id)
        msg = "убрал"
    else:
        db.set_budget(u.id, cat_id, amount, currency)
        msg = f"задал {fmt_money(amount, currency)}"
    context.user_data.pop("budget_cat", None)
    label = "общий бюджет" if cat_id is None else category_name(u.id, cat_id)
    await update.message.reply_html(f"✅ Бюджет ({label}): {msg}.", reply_markup=main_kb())
    return ConversationHandler.END


# ── Редактирование трат ──────────────────────────────────────────
async def _edit_list(message, uid: int):
    txs = db.recent_transactions(uid, 10)
    txs = [t for t in txs if t["type"] == "expense"]
    if not txs:
        await message.reply_text("Трат пока нет.", reply_markup=main_kb())
        return ConversationHandler.END
    rows = []
    for t in txs:
        when = t["created_at"].replace("T", " ")[5:16]
        label = f"#{t['id']} · {when} · {fmt_money(t['amount'], t['currency'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"epick:{t['id']}")])
    await message.reply_text("Выбери трату:", reply_markup=InlineKeyboardMarkup(rows))
    await message.reply_text("Или «Отмена».", reply_markup=cancel_kb())
    return E_STATE


async def edit_from_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _edit_list(query.message, query.from_user.id)


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _edit_list(update.message, update.effective_user.id)


async def edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    if db.transaction_owner(tx_id) != query.from_user.id:
        await query.edit_message_text("Это не твоя запись.")
        return ConversationHandler.END
    context.user_data["edit_id"] = tx_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Сумма", callback_data=f"echg:{tx_id}")],
        [InlineKeyboardButton("🕐 Время", callback_data=f"etime:{tx_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"edel:{tx_id}")],
    ])
    tx = db.get_transaction(tx_id)
    await query.edit_message_text(
        f"Запись #{tx_id}: {fmt_money(tx['amount'], tx['currency'])}. Что сделать?", reply_markup=kb)
    return E_STATE


async def edit_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    context.user_data["edit_id"] = tx_id
    context.user_data["edit_mode"] = "amount"
    await query.edit_message_text(f"Новая сумма для #{tx_id} (можно с валютой):")
    return E_STATE


async def edit_time_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    context.user_data["edit_id"] = tx_id
    context.user_data["edit_mode"] = "time"
    tx = db.get_transaction(tx_id)
    now = tx["created_at"].replace("T", " ")[:16]
    await query.edit_message_text(
        f"Новое время для #{tx_id} (сейчас {now}).\nФорматы: <code>ДД.ММ ЧЧ:ММ</code>, "
        f"<code>ЧЧ:ММ</code>, <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>.", parse_mode="HTML")
    return E_STATE


async def edit_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, удалить", callback_data=f"edok:{tx_id}")],
        [InlineKeyboardButton("Нет", callback_data="edno")],
    ])
    await query.edit_message_text(f"Удалить запись #{tx_id}?", reply_markup=kb)
    return E_STATE


async def edit_delete_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    if db.transaction_owner(tx_id) == query.from_user.id:
        db.delete_transaction(tx_id)
    context.user_data.pop("edit_id", None)
    await query.edit_message_text(f"🗑 Запись #{tx_id} удалена.")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())
    return ConversationHandler.END


async def edit_delete_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("edit_id", None)
    await query.edit_message_text("Ок, оставил как есть.")
    await query.message.reply_text("Главное меню 👇", reply_markup=main_kb())
    return ConversationHandler.END


def _parse_new_time(text: str, current_iso: str) -> str | None:
    text = text.strip().replace("T", " ")
    try:
        cur = datetime.fromisoformat(current_iso)
    except (ValueError, TypeError):
        cur = datetime.now()
    fmts = [
        ("%Y-%m-%d %H:%M:%S", None), ("%Y-%m-%d %H:%M", None),
        ("%d.%m.%Y %H:%M", None), ("%d.%m %H:%M", "year"),
        ("%d.%m.%Y", None), ("%d.%m", "year"),
        ("%H:%M:%S", "date"), ("%H:%M", "date"),
    ]
    for fmt, fill in fmts:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fill == "year":
            dt = dt.replace(year=cur.year)
        elif fill == "date":
            dt = dt.replace(year=cur.year, month=cur.month, day=cur.day)
        return dt.replace(second=0, microsecond=0).isoformat(timespec="seconds")
    return None


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    raw = (update.message.text or "").strip()
    if raw == BTN_CANCEL:
        return await cancel(update, context)
    tx_id = context.user_data.get("edit_id")
    if tx_id is None or db.transaction_owner(tx_id) != u.id:
        await update.message.reply_text("Выбери запись заново.", reply_markup=main_kb())
        context.user_data.pop("edit_id", None)
        return ConversationHandler.END
    mode = context.user_data.get("edit_mode")
    if mode == "time":
        tx = db.get_transaction(tx_id)
        new_iso = _parse_new_time(raw, tx["created_at"])
        if not new_iso:
            await update.message.reply_text("Не понял время. Ещё раз:", reply_markup=cancel_kb())
            return E_STATE
        db.edit_transaction_time(tx_id, new_iso)
        context.user_data.pop("edit_id", None)
        context.user_data.pop("edit_mode", None)
        await update.message.reply_html(f"🕐 Время #{tx_id} → {new_iso.replace('T', ' ')[:16]}.",
                                        reply_markup=main_kb())
        return ConversationHandler.END
    amount, _ = parse_amount(raw)
    if amount is None or amount <= 0:
        await update.message.reply_text("Не понял сумму. Ещё раз:", reply_markup=cancel_kb())
        return E_STATE
    db.edit_transaction_amount(tx_id, amount)
    context.user_data.pop("edit_id", None)
    context.user_data.pop("edit_mode", None)
    await update.message.reply_html(f"✏️ Сумма #{tx_id} изменена.", reply_markup=main_kb())
    return ConversationHandler.END


# ── Прочее ───────────────────────────────────────────────────────
HELP_TEXT = (
    "Я <b>Spending</b> — считаю траты. 💸\n\n"
    "• <b>➕ Трата</b> — сумма → сфера → магазин → место хранения → комментарий.\n"
    "• <b>🗑 Удалить последнюю</b> — стереть последнюю трату.\n"
    "• <b>📊 Статистика</b> — сферы, магазины, дни, балансы, бюджеты, графики.\n"
    "• <b>💰 Кошельки</b> — места хранения денег, пополнения и переводы.\n"
    "• <b>🏆 Рейтинг</b> — доски: экономные за неделю/месяц, транжиры, семейный.\n"
    "• <b>⚙️ Настройки</b> — имя, валюта, сферы, магазины, бюджеты, семья.\n\n"
    "Валюту можно писать прямо в сумме: <code>500 грн</code>, <code>12 eur</code>. "
    "Всё сводится в выбранную тобой валюту."
)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP_TEXT, reply_markup=main_kb())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("add", "newacc", "topup_acc", "transfer", "fam_mode", "cat_mgr",
              "mm_cat", "budget_cat", "edit_id", "edit_mode"):
        context.user_data.pop(k, None)
    await update.message.reply_text("Отменил. 👍", reply_markup=main_kb())
    return ConversationHandler.END


async def conv_escape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка главного меню посреди диалога — выходим и выполняем действие."""
    for k in ("add", "newacc", "topup_acc", "transfer", "fam_mode", "cat_mgr",
              "mm_cat", "budget_cat", "edit_id", "edit_mode"):
        context.user_data.pop(k, None)
    text = (update.message.text or "").strip()
    direct = {
        BTN_DELLAST: dellast_cmd, BTN_STATS: stats_menu, BTN_WALLETS: wallets_menu,
        BTN_TOP: top_menu, BTN_SETTINGS: settings_menu, BTN_HELP: help_cmd,
    }
    if text in direct:
        await direct[text](update, context)
    else:  # BTN_ADD
        await update.message.reply_text("Отменил. Нажми «➕ Трата», чтобы начать заново.",
                                        reply_markup=main_kb())
    return ConversationHandler.END


# ── Сборка приложения ────────────────────────────────────────────
def _btn(text: str) -> filters.BaseFilter:
    return filters.Regex(f"^{re.escape(text)}$")


def _main_filter() -> filters.BaseFilter:
    return filters.Regex("^(" + "|".join(re.escape(b) for b in MAIN_BTNS) + ")$")


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

    db.init_db()
    app = Application.builder().token(token).build()

    escape_fb = MessageHandler(_main_filter(), conv_escape)
    common_fb = [
        CommandHandler("cancel", cancel),
        MessageHandler(_btn(BTN_CANCEL), cancel),
        escape_fb,
    ]
    # свободный текст в диалоге: всё, кроме кнопок главного меню и «Отмена»
    free_text = filters.TEXT & ~filters.COMMAND & ~_main_filter() & ~_btn(BTN_CANCEL)

    name_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("setname", setname_entry),
            CallbackQueryHandler(setname_from_cb, pattern=r"^se:name$"),
        ],
        states={NAME: [MessageHandler(free_text, name_received)]},
        fallbacks=common_fb, allow_reentry=True,
    )

    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(_btn(BTN_ADD), add_entry),
            CommandHandler("add", add_entry),
        ],
        states={
            A_AMOUNT: [MessageHandler(free_text, add_amount)],
            A_CAT: [CallbackQueryHandler(add_cat_pick, pattern=r"^acat:")],
            A_NEWCAT: [MessageHandler(free_text, add_newcat)],
            A_MERCH: [CallbackQueryHandler(add_merch_pick, pattern=r"^amer:")],
            A_NEWMERCH: [MessageHandler(free_text, add_newmerch)],
            A_ACC: [CallbackQueryHandler(add_acc_pick, pattern=r"^aacc:")],
            A_NEWACC_NAME: [MessageHandler(free_text, add_newacc_name)],
            A_NEWACC_CUR: [CallbackQueryHandler(add_newacc_cur, pattern=r"^acur:")],
            A_NOTE: [MessageHandler(free_text, add_note)],
        },
        fallbacks=common_fb, allow_reentry=True,
    )

    acc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(acc_new_entry, pattern=r"^wal:new$")],
        states={
            NA_NAME: [MessageHandler(free_text, acc_new_name)],
            NA_CUR: [CallbackQueryHandler(acc_new_cur, pattern=r"^nacur:")],
        },
        fallbacks=common_fb, allow_reentry=True,
    )

    topup_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(topup_entry, pattern=r"^wal:topup$")],
        states={TU_AMOUNT: [
            CallbackQueryHandler(topup_pick, pattern=r"^tup:"),
            MessageHandler(free_text, topup_amount),
        ]},
        fallbacks=common_fb, allow_reentry=True,
    )

    transfer_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(transfer_entry, pattern=r"^wal:transfer$")],
        states={TF_AMOUNT: [
            CallbackQueryHandler(transfer_from, pattern=r"^tff:"),
            CallbackQueryHandler(transfer_to, pattern=r"^tft:"),
            MessageHandler(free_text, transfer_amount),
        ]},
        fallbacks=common_fb, allow_reentry=True,
    )

    family_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(family_create_entry, pattern=r"^fam:create$"),
            CallbackQueryHandler(family_join_entry, pattern=r"^fam:join$"),
        ],
        states={CM_ADD: [MessageHandler(free_text, family_text)]},
        fallbacks=common_fb, allow_reentry=True,
    )

    cat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cat_add_entry, pattern=r"^camgr:add$")],
        states={CM_ADD: [MessageHandler(free_text, cat_add_text)]},
        fallbacks=common_fb, allow_reentry=True,
    )

    merch_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(merch_menu, pattern=r"^mmc:")],
        states={
            MM_MENU: [
                CallbackQueryHandler(merch_add_entry, pattern=r"^mmadd$"),
                CallbackQueryHandler(merch_archive, pattern=r"^maar:"),
            ],
            MM_ADD: [MessageHandler(free_text, merch_add_text)],
        },
        fallbacks=common_fb, allow_reentry=True,
    )

    budget_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(budget_pick, pattern=r"^bup:")],
        states={BU_AMOUNT: [MessageHandler(free_text, budget_amount)]},
        fallbacks=common_fb, allow_reentry=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_cmd),
            CallbackQueryHandler(edit_from_settings, pattern=r"^se:edit$"),
        ],
        states={E_STATE: [
            CallbackQueryHandler(edit_pick, pattern=r"^epick:"),
            CallbackQueryHandler(edit_change, pattern=r"^echg:"),
            CallbackQueryHandler(edit_time_ask, pattern=r"^etime:"),
            CallbackQueryHandler(edit_delete_ask, pattern=r"^edel:"),
            CallbackQueryHandler(edit_delete_ok, pattern=r"^edok:"),
            CallbackQueryHandler(edit_delete_no, pattern=r"^edno$"),
            MessageHandler(free_text, edit_value),
        ]},
        fallbacks=common_fb, allow_reentry=True,
    )

    for c in (name_conv, add_conv, acc_conv, topup_conv, transfer_conv,
              family_conv, cat_conv, merch_conv, budget_conv, edit_conv):
        app.add_handler(c)

    # Простые кнопки/действия
    app.add_handler(MessageHandler(_btn(BTN_DELLAST), dellast_cmd))
    app.add_handler(CommandHandler("dellast", dellast_cmd))

    app.add_handler(MessageHandler(_btn(BTN_STATS), stats_menu))
    app.add_handler(CommandHandler("stats", stats_menu))
    app.add_handler(CallbackQueryHandler(stats_catmerch, pattern=r"^scm:"))
    app.add_handler(CallbackQueryHandler(stats_action, pattern=r"^st:"))

    app.add_handler(MessageHandler(_btn(BTN_WALLETS), wallets_menu))
    app.add_handler(CommandHandler("wallets", wallets_menu))
    app.add_handler(CallbackQueryHandler(wallets_manage, pattern=r"^wal:manage$"))
    app.add_handler(CallbackQueryHandler(wallet_manage_one, pattern=r"^wm:"))
    app.add_handler(CallbackQueryHandler(wallet_toggle_private, pattern=r"^wmp:"))
    app.add_handler(CallbackQueryHandler(wallet_set_default, pattern=r"^wmd:"))
    app.add_handler(CallbackQueryHandler(wallet_archive, pattern=r"^wma:"))

    app.add_handler(MessageHandler(_btn(BTN_TOP), top_menu))
    app.add_handler(CommandHandler("top", top_menu))
    app.add_handler(CallbackQueryHandler(top_board, pattern=r"^tb:"))

    app.add_handler(MessageHandler(_btn(BTN_SETTINGS), settings_menu))
    app.add_handler(CommandHandler("settings", settings_menu))
    app.add_handler(CallbackQueryHandler(settings_currency_menu, pattern=r"^se:cur$"))
    app.add_handler(CallbackQueryHandler(settings_currency_set, pattern=r"^secur:"))
    app.add_handler(CallbackQueryHandler(settings_global_toggle, pattern=r"^se:global$"))
    app.add_handler(CallbackQueryHandler(cats_manage, pattern=r"^se:cats$"))
    app.add_handler(CallbackQueryHandler(cat_archive, pattern=r"^caar:"))
    app.add_handler(CallbackQueryHandler(merch_manage, pattern=r"^se:merch$"))
    app.add_handler(CallbackQueryHandler(merch_archive, pattern=r"^maar:"))
    app.add_handler(CallbackQueryHandler(budget_menu, pattern=r"^se:budget$"))
    app.add_handler(CallbackQueryHandler(family_menu, pattern=r"^se:family$"))
    app.add_handler(CallbackQueryHandler(family_members, pattern=r"^fam:members$"))
    app.add_handler(CallbackQueryHandler(family_code, pattern=r"^fam:code$"))
    app.add_handler(CallbackQueryHandler(family_leave, pattern=r"^fam:leave$"))

    app.add_handler(MessageHandler(_btn(BTN_HELP), help_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
