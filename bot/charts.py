"""Построение графиков (стиль «как в Excel») через matplotlib.

Каждая функция возвращает PNG в виде BytesIO, готовый к отправке в Telegram.
Подписи/заголовки передаются готовыми строками — модуль не зависит от локализации.
"""

from __future__ import annotations

import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # без GUI, рендер в файл
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

# Палитра в духе Excel
ACCENT = "#2E75B6"   # синий
ACCENT2 = "#ED7D31"  # оранжевый
RED = "#C0392B"
GRID = "#D9D9D9"
TEXT = "#404040"
PALETTE = [ACCENT, ACCENT2, "#7B61FF", "#27AE60", "#C0392B", "#F1C40F",
           "#16A085", "#E84393", "#2C3E50", "#D35400"]

plt.rcParams.update(
    {
        "font.size": 11,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT,
        "text.color": TEXT,
        "xtick.color": TEXT,
        "ytick.color": TEXT,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
)


def _finish(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") if abs(v - round(v)) < 0.05 else f"{v:,.1f}".replace(",", " ")


def pie_chart(items: list[tuple[str, float]], currency: str, title: str) -> io.BytesIO:
    """Кольцевая диаграмма: на что/где ушли деньги."""
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(values))]
    total = sum(values) or 1

    fig, ax = plt.subplots(figsize=(8, 6))

    def autopct(pct):
        return f"{pct:.0f}%\n{_fmt(pct / 100 * total)}"

    wedges, _t, autotexts = ax.pie(
        values, colors=colors, autopct=autopct, startangle=90,
        wedgeprops=dict(width=0.42, edgecolor="white"), pctdistance=0.78,
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_fontweight("bold")
        t.set_color(TEXT)

    ax.legend(wedges, labels, loc="center", frameon=False, fontsize=10)
    ax.set_title(f"{title}\n{_fmt(total)} {currency}", fontsize=15, fontweight="bold", pad=14)
    ax.set(aspect="equal")
    return _finish(fig)


def hbar_chart(items: list[tuple[str, float]], currency: str, title: str) -> io.BytesIO:
    """Горизонтальные столбики (топ трат / магазинов / балансы)."""
    items = list(items)[::-1]  # чтобы самый крупный оказался сверху
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(values))]

    fig, ax = plt.subplots(figsize=(9, max(3.2, 0.55 * len(items) + 1.4)))
    bars = ax.barh(labels, values, color=colors, edgecolor="white")
    for rect, v in zip(bars, values):
        ax.annotate(f"{_fmt(v)} {currency}",
                    (rect.get_width(), rect.get_y() + rect.get_height() / 2),
                    textcoords="offset points", xytext=(6, 0), va="center",
                    fontsize=9, fontweight="bold")

    ax.set_title(title, fontsize=15, fontweight="bold", pad=14)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.16)
    return _finish(fig)


def days_bar_chart(days: list[tuple[str, float]], currency: str, title: str) -> io.BytesIO:
    """Столбики потраченного по дням (самый дорогой день — красным)."""
    labels = [datetime.fromisoformat(d).strftime("%d.%m") for d, _ in days]
    values = [v for _, v in days]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(labels, values, color=ACCENT2, width=0.62, edgecolor="white")
    if values:
        worst = max(range(len(values)), key=lambda i: values[i])
        bars[worst].set_color(RED)
    for rect, v in zip(bars, values):
        ax.annotate(_fmt(v), (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                    textcoords="offset points", xytext=(0, 5), ha="center",
                    fontsize=9, fontweight="bold")

    ax.set_title(title, fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel(currency)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", visible=False)
    ax.margins(y=0.18)
    fig.autofmt_xdate(rotation=30)
    return _finish(fig)


def weekday_chart(values: list[float], labels: list[str], currency: str, title: str) -> io.BytesIO:
    """Столбики трат по дням недели (выходные — фиолетовым, пик — оранжевым)."""
    fig, ax = plt.subplots(figsize=(9, 5))
    colors = [ACCENT] * 7
    colors[5] = colors[6] = "#7B61FF"
    bars = ax.bar(labels, values, color=colors, width=0.66, edgecolor="white")
    if any(values):
        peak = max(range(7), key=lambda i: values[i])
        bars[peak].set_color(ACCENT2)
    for rect, v in zip(bars, values):
        if v:
            ax.annotate(_fmt(v), (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                        textcoords="offset points", xytext=(0, 5), ha="center",
                        fontsize=9, fontweight="bold")

    ax.set_title(title, fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel(currency)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", visible=False)
    ax.margins(y=0.18)
    return _finish(fig)


def cumulative_chart(points: list[tuple[str, float]], currency: str, title: str) -> io.BytesIO:
    """Накопительный график: сколько всего потрачено к каждому моменту."""
    x = [datetime.fromisoformat(ts) for ts, _ in points]
    cum, running = [], 0.0
    for _, v in points:
        running += v
        cum.append(running)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(x, cum, color=ACCENT2, linewidth=2.6)
    ax.fill_between(x, cum, color=ACCENT2, alpha=0.18)

    ax.set_title(f"{title}\n{_fmt(cum[-1] if cum else 0)} {currency}",
                 fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel(currency)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    fig.autofmt_xdate(rotation=30)
    ax.spines[["top", "right"]].set_visible(False)
    ax.margins(y=0.12)
    return _finish(fig)
