"""Слой работы с базой данных (SQLite) для финансового трекера.

Сущности:
- users        — пользователи (валюта отображения, семья, участие в глобальном рейтинге)
- families     — семьи (общие категории/магазины, семейный рейтинг), вход по коду
- categories   — сферы трат (Еда, Транспорт…). Общие внутри семьи
- merchants    — магазины/места трат внутри категории (Lidl, Penny…). Общие внутри семьи
- accounts     — места хранения денег (Кошелёк, Сейф, Куртка…), у каждого своя валюта
- transactions — траты и доходы (сумма, валюта, категория, магазин, место хранения)
- transfers    — переводы денег между местами хранения
- budgets      — месячные лимиты (общий и по категориям)

Все суммы хранятся в валюте самой операции; конвертация — «на лету» через bot.rates.
Миграции только аддитивные (CREATE TABLE IF NOT EXISTS / ALTER ADD COLUMN) — данные
никогда не теряются.
"""

from __future__ import annotations

import os
import random
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from . import rates

DB_PATH = os.environ.get("DB_PATH", "/data/finance.db")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                display_name   TEXT,
                currency       TEXT DEFAULT '€',
                family_id      INTEGER,
                global_optout  INTEGER DEFAULT 0,
                created_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS families (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                invite_code  TEXT UNIQUE NOT NULL,
                owner_id     INTEGER NOT NULL,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scope       TEXT NOT NULL,   -- 'user' | 'family'
                scope_id    INTEGER NOT NULL,
                name        TEXT NOT NULL,
                emoji       TEXT,
                created_by  INTEGER,
                archived    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS merchants (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope        TEXT NOT NULL,   -- 'user' | 'family'
                scope_id     INTEGER NOT NULL,
                category_id  INTEGER,         -- к какой сфере привязан (NULL = любой)
                name         TEXT NOT NULL,
                created_by   INTEGER,
                archived     INTEGER DEFAULT 0,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                emoji       TEXT,
                currency    TEXT NOT NULL,
                is_private  INTEGER DEFAULT 0,
                is_default  INTEGER DEFAULT 0,
                archived    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT NOT NULL,   -- 'expense' | 'income'
                amount      REAL NOT NULL,
                currency    TEXT NOT NULL,
                category_id INTEGER,
                merchant_id INTEGER,
                account_id  INTEGER,
                note        TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at);

            CREATE TABLE IF NOT EXISTS transfers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                from_account  INTEGER,
                to_account    INTEGER,
                amount        REAL NOT NULL,
                currency      TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                category_id INTEGER,          -- NULL = общий месячный бюджет
                amount      REAL NOT NULL,
                currency    TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )


# ── Пользователи ─────────────────────────────────────────────────
def user_exists(user_id: int) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, display_name, currency, created_at)
            VALUES (?, ?, ?, ?, '€', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, first_name, _now()),
        )


def get_display_name(user_id: int) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(display_name, first_name, username) AS name "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["name"] if row else None


def set_display_name(user_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE user_id = ?", (name, user_id))


def get_currency(user_id: int) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT currency FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row["currency"] if row and row["currency"] else "€")


def set_currency(user_id: int, currency: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET currency = ? WHERE user_id = ?", (currency, user_id))


def get_global_optout(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT global_optout FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row["global_optout"]) if row else False


def set_global_optout(user_id: int, value: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET global_optout = ? WHERE user_id = ?", (1 if value else 0, user_id)
        )


# ── Семьи ────────────────────────────────────────────────────────
def _gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def get_family_id(user_id: int) -> Optional[int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT family_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["family_id"]) if row and row["family_id"] else None


def create_family(user_id: int, name: str) -> tuple[int, str]:
    with _connect() as conn:
        for _ in range(20):
            code = _gen_code()
            if not conn.execute(
                "SELECT 1 FROM families WHERE invite_code = ?", (code,)
            ).fetchone():
                break
        cur = conn.execute(
            "INSERT INTO families (name, invite_code, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (name, code, user_id, _now()),
        )
        fid = int(cur.lastrowid)
        conn.execute("UPDATE users SET family_id = ? WHERE user_id = ?", (fid, user_id))
        return fid, code


def join_family(user_id: int, code: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name FROM families WHERE invite_code = ?", (code.strip().upper(),)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE users SET family_id = ? WHERE user_id = ?", (row["id"], user_id))
        return {"id": int(row["id"]), "name": row["name"]}


def leave_family(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET family_id = NULL WHERE user_id = ?", (user_id,))


def family_info(family_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, invite_code, owner_id FROM families WHERE id = ?", (family_id,)
        ).fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "name": row["name"],
                "code": row["invite_code"], "owner_id": int(row["owner_id"])}


def family_members(family_id: int) -> list[tuple[int, str]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, COALESCE(display_name, first_name, username, 'Аноним') AS name "
            "FROM users WHERE family_id = ? ORDER BY name",
            (family_id,),
        ).fetchall()
        return [(int(r["user_id"]), r["name"]) for r in rows]


# ── Категории (сферы) ────────────────────────────────────────────
def _scope_for(user_id: int) -> tuple[str, int]:
    """Куда писать новую категорию/магазин: в семью (если есть) или лично."""
    fid = get_family_id(user_id)
    return ("family", fid) if fid else ("user", user_id)


def list_categories(user_id: int) -> list[dict]:
    fid = get_family_id(user_id)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, emoji FROM categories
            WHERE archived = 0 AND (
                (scope = 'user' AND scope_id = ?)
                OR (scope = 'family' AND scope_id = ?)
            )
            ORDER BY name
            """,
            (user_id, fid if fid else -1),
        ).fetchall()
        return [{"id": int(r["id"]), "name": r["name"], "emoji": r["emoji"] or ""} for r in rows]


def add_category(user_id: int, name: str, emoji: str = "") -> int:
    scope, scope_id = _scope_for(user_id)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO categories (scope, scope_id, name, emoji, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scope, scope_id, name, emoji, user_id, _now()),
        )
        return int(cur.lastrowid)


def get_category(category_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, emoji, scope, scope_id FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()
        if not r:
            return None
        return {"id": int(r["id"]), "name": r["name"], "emoji": r["emoji"] or "",
                "scope": r["scope"], "scope_id": int(r["scope_id"])}


def rename_category(category_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))


def archive_category(category_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE categories SET archived = 1 WHERE id = ?", (category_id,))


def category_visible_to(user_id: int, category_id: int) -> bool:
    cat = get_category(category_id)
    if not cat:
        return False
    if cat["scope"] == "user":
        return cat["scope_id"] == user_id
    return cat["scope_id"] == (get_family_id(user_id) or -1)


# ── Магазины (места трат внутри сферы) ────────────────────────────
def list_merchants(user_id: int, category_id: Optional[int]) -> list[dict]:
    fid = get_family_id(user_id)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, category_id FROM merchants
            WHERE archived = 0
              AND ( (scope = 'user' AND scope_id = ?) OR (scope = 'family' AND scope_id = ?) )
              AND (category_id = ? OR category_id IS NULL)
            ORDER BY name
            """,
            (user_id, fid if fid else -1, category_id),
        ).fetchall()
        return [{"id": int(r["id"]), "name": r["name"]} for r in rows]


def add_merchant(user_id: int, category_id: Optional[int], name: str) -> int:
    scope, scope_id = _scope_for(user_id)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO merchants (scope, scope_id, category_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scope, scope_id, category_id, name, user_id, _now()),
        )
        return int(cur.lastrowid)


def get_merchant(merchant_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, category_id FROM merchants WHERE id = ?", (merchant_id,)
        ).fetchone()
        if not r:
            return None
        return {"id": int(r["id"]), "name": r["name"],
                "category_id": int(r["category_id"]) if r["category_id"] else None}


def archive_merchant(merchant_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE merchants SET archived = 1 WHERE id = ?", (merchant_id,))


# ── Места хранения (кошельки/сейф/…) ──────────────────────────────
def list_accounts(user_id: int, include_archived: bool = False) -> list[dict]:
    q = ("SELECT id, name, emoji, currency, is_private, is_default FROM accounts "
         "WHERE user_id = ?")
    if not include_archived:
        q += " AND archived = 0"
    q += " ORDER BY is_default DESC, id"
    with _connect() as conn:
        rows = conn.execute(q, (user_id,)).fetchall()
        return [{"id": int(r["id"]), "name": r["name"], "emoji": r["emoji"] or "",
                 "currency": r["currency"], "is_private": bool(r["is_private"]),
                 "is_default": bool(r["is_default"])} for r in rows]


def add_account(user_id: int, name: str, emoji: str, currency: str) -> int:
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM accounts WHERE user_id = ? AND archived = 0", (user_id,)
        ).fetchone()["c"]
        is_default = 1 if n == 0 else 0  # первое место хранения — по умолчанию
        cur = conn.execute(
            "INSERT INTO accounts (user_id, name, emoji, currency, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, emoji, currency, is_default, _now()),
        )
        return int(cur.lastrowid)


def get_account(account_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, user_id, name, emoji, currency, is_private, is_default "
            "FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not r:
            return None
        return {"id": int(r["id"]), "user_id": int(r["user_id"]), "name": r["name"],
                "emoji": r["emoji"] or "", "currency": r["currency"],
                "is_private": bool(r["is_private"]), "is_default": bool(r["is_default"])}


def default_account(user_id: int) -> Optional[dict]:
    accs = list_accounts(user_id)
    return accs[0] if accs else None


def rename_account(account_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))


def set_account_private(account_id: int, value: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE accounts SET is_private = ? WHERE id = ?", (1 if value else 0, account_id)
        )


def set_default_account(user_id: int, account_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE accounts SET is_default = 0 WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE accounts SET is_default = 1 WHERE id = ?", (account_id,))


def archive_account(account_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE accounts SET archived = 1 WHERE id = ?", (account_id,))


def account_balance(account_id: int) -> float:
    """Баланс места хранения в его собственной валюте (доходы − траты + переводы)."""
    acc = get_account(account_id)
    if not acc:
        return 0.0
    to = acc["currency"]
    bal = 0.0
    with _connect() as conn:
        for r in conn.execute(
            "SELECT type, amount, currency FROM transactions WHERE account_id = ?",
            (account_id,),
        ):
            v = rates.convert(float(r["amount"]), r["currency"], to)
            bal += v if r["type"] == "income" else -v
        for r in conn.execute(
            "SELECT amount, currency FROM transfers WHERE to_account = ?", (account_id,)
        ):
            bal += rates.convert(float(r["amount"]), r["currency"], to)
        for r in conn.execute(
            "SELECT amount, currency FROM transfers WHERE from_account = ?", (account_id,)
        ):
            bal -= rates.convert(float(r["amount"]), r["currency"], to)
    return bal


# ── Транзакции ───────────────────────────────────────────────────
def add_transaction(user_id: int, type_: str, amount: float, currency: str,
                    category_id: Optional[int], merchant_id: Optional[int],
                    account_id: Optional[int], note: Optional[str]) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO transactions "
            "(user_id, type, amount, currency, category_id, merchant_id, account_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, type_, amount, currency, category_id, merchant_id, account_id, note, _now()),
        )
        return int(cur.lastrowid)


def add_transfer(user_id: int, from_account: Optional[int], to_account: Optional[int],
                 amount: float, currency: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO transfers (user_id, from_account, to_account, amount, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, from_account, to_account, amount, currency, _now()),
        )
        return int(cur.lastrowid)


def transaction_owner(tx_id: int) -> Optional[int]:
    with _connect() as conn:
        r = conn.execute("SELECT user_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        return int(r["user_id"]) if r else None


def last_transaction(user_id: int, type_: str = "expense") -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, amount, currency, category_id, merchant_id, account_id, note, created_at "
            "FROM transactions WHERE user_id = ? AND type = ? ORDER BY id DESC LIMIT 1",
            (user_id, type_),
        ).fetchone()
        return dict(r) if r else None


def delete_transaction(tx_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))


def get_transaction(tx_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        return dict(r) if r else None


def recent_transactions(user_id: int, limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, type, amount, currency, category_id, merchant_id, note, created_at "
            "FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def edit_transaction_amount(tx_id: int, amount: float) -> None:
    with _connect() as conn:
        conn.execute("UPDATE transactions SET amount = ? WHERE id = ?", (amount, tx_id))


def edit_transaction_time(tx_id: int, created_at: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE transactions SET created_at = ? WHERE id = ?", (created_at, tx_id))


# ── Выборки трат для статистики (конвертация — в Python) ──────────
def expense_rows(user_id: int, start: Optional[str] = None,
                 end: Optional[str] = None) -> list[dict]:
    """Траты пользователя (type='expense') за период [start, end) или все."""
    q = ("SELECT amount, currency, category_id, merchant_id, created_at "
         "FROM transactions WHERE user_id = ? AND type = 'expense'")
    args: list = [user_id]
    if start is not None:
        q += " AND created_at >= ?"
        args.append(start)
    if end is not None:
        q += " AND created_at < ?"
        args.append(end)
    q += " ORDER BY created_at"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def expense_sum(user_id: int, to_currency: str, start: Optional[str] = None,
                end: Optional[str] = None) -> float:
    return sum(
        rates.convert(float(r["amount"]), r["currency"], to_currency)
        for r in expense_rows(user_id, start, end)
    )


# ── Бюджеты ──────────────────────────────────────────────────────
def set_budget(user_id: int, category_id: Optional[int], amount: float, currency: str) -> None:
    with _connect() as conn:
        if category_id is None:
            conn.execute("DELETE FROM budgets WHERE user_id = ? AND category_id IS NULL",
                         (user_id,))
        else:
            conn.execute("DELETE FROM budgets WHERE user_id = ? AND category_id = ?",
                         (user_id, category_id))
        conn.execute(
            "INSERT INTO budgets (user_id, category_id, amount, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, category_id, amount, currency, _now()),
        )


def delete_budget(user_id: int, category_id: Optional[int]) -> None:
    with _connect() as conn:
        if category_id is None:
            conn.execute("DELETE FROM budgets WHERE user_id = ? AND category_id IS NULL",
                         (user_id,))
        else:
            conn.execute("DELETE FROM budgets WHERE user_id = ? AND category_id = ?",
                         (user_id, category_id))


def get_budgets(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category_id, amount, currency FROM budgets WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [{"category_id": int(r["category_id"]) if r["category_id"] else None,
                 "amount": float(r["amount"]), "currency": r["currency"]} for r in rows]


# ── Рейтинг ──────────────────────────────────────────────────────
def rating_users(family_id: Optional[int] = None,
                 exclude_optout: bool = True) -> list[tuple[int, str]]:
    """Кандидаты для рейтинга: вся семья (если задано) или все пользователи."""
    q = ("SELECT user_id, COALESCE(display_name, first_name, username, 'Аноним') AS name "
         "FROM users WHERE 1=1")
    args: list = []
    if family_id is not None:
        q += " AND family_id = ?"
        args.append(family_id)
    elif exclude_optout:
        q += " AND global_optout = 0"
    with _connect() as conn:
        rows = conn.execute(q, args).fetchall()
        return [(int(r["user_id"]), r["name"]) for r in rows]
