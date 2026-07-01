"""Курсы валют для авто-конвертации сумм.

Поддерживаем несколько валют (евро, доллар, гривна, чешская крона, злотый).
Курс берём бесплатно, без ключа, с https://open.er-api.com (база — EUR),
кэшируем в /data на ~12 часов. Если интернета/API нет — используем приблизительный
хардкод-фолбэк, чтобы бот никогда не падал. Конвертация «примерная» — для оценки
трат и общего рейтинга этого достаточно.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("finance-bot.rates")

_API = "https://open.er-api.com/v6/latest/EUR"
_TTL = 12 * 3600  # как часто обновлять курс, секунд
_CODES = ("EUR", "USD", "UAH", "CZK", "PLN")

# Приблизительный запасной курс (единиц валюты за 1 EUR) — если API недоступен.
_FALLBACK = {"EUR": 1.0, "USD": 1.08, "UAH": 51.0, "CZK": 24.5, "PLN": 4.3}

# ISO-код ↔ отображаемый символ
SYMBOL = {"EUR": "€", "USD": "$", "UAH": "грн", "CZK": "Kč", "PLN": "zł"}
_SYM2ISO = {
    "€": "EUR", "eur": "EUR", "EUR": "EUR",
    "$": "USD", "usd": "USD", "USD": "USD",
    "грн": "UAH", "₴": "UAH", "uah": "UAH", "грн.": "UAH",
    "Kč": "CZK", "kč": "CZK", "kc": "CZK", "czk": "CZK", "крон": "CZK",
    "zł": "PLN", "zl": "PLN", "pln": "PLN", "злот": "PLN",
}

# Порядок для меню выбора валюты
ORDER = ["EUR", "USD", "UAH", "CZK", "PLN"]

_CACHE_PATH = os.path.join(
    os.path.dirname(os.environ.get("DB_PATH", "/data/finance.db")) or "/data",
    "rates.json",
)

# Кэш в памяти процесса: (timestamp, rates)
_mem: tuple[float, dict] | None = None


def iso(symbol: str | None) -> str | None:
    """ISO-код по отображаемому символу валюты (или None, если не знаем)."""
    if not symbol:
        return None
    s = symbol.strip()
    if s in _SYM2ISO:
        return _SYM2ISO[s]
    return _SYM2ISO.get(s.lower())


def _fetch() -> dict | None:
    try:
        with urllib.request.urlopen(_API, timeout=10) as r:
            d = json.load(r)
        if d.get("result") == "success":
            rates = d["rates"]
            return {c: float(rates[c]) for c in _CODES if c in rates}
    except Exception as e:  # noqa: BLE001 — любой сбой сети/парсинга не должен ронять бота
        log.warning("Не удалось получить курс валют: %s", e)
    return None


def get_rates() -> dict:
    """Курсы (единиц валюты за 1 EUR). С кэшем в памяти, на диске и фолбэком."""
    global _mem
    now = time.time()
    if _mem and now - _mem[0] < _TTL:
        return _mem[1]

    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            disk = json.load(f)
        if now - disk.get("ts", 0) < _TTL and disk.get("rates"):
            _mem = (disk["ts"], disk["rates"])
            return _mem[1]
    except (OSError, ValueError):
        disk = None

    fresh = _fetch()
    if fresh:
        _mem = (now, fresh)
        try:
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"ts": now, "rates": fresh}, f)
        except OSError as e:
            log.warning("Не удалось записать кэш курсов: %s", e)
        return fresh

    if disk and disk.get("rates"):
        _mem = (disk["ts"], disk["rates"])
        return _mem[1]
    return _FALLBACK


def convert(amount: float, from_symbol: str | None, to_symbol: str | None) -> float:
    """Переводит сумму из одной валюты в другую по примерному курсу.
    Если валюту не распознали — возвращает сумму как есть (без конвертации)."""
    a, b = iso(from_symbol), iso(to_symbol)
    if a is None or b is None or a == b:
        return amount
    rates = get_rates()
    if a not in rates or b not in rates:
        return amount
    return amount / rates[a] * rates[b]
