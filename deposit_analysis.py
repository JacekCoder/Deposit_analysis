"""
Deposit / Portfolio Analysis Script
Fetches latest US stock prices (via yfinance), domestic gold price (via Sina),
and USD/CNY exchange rate, then calculates total assets in CNY and sends
a daily summary via PushPlus to WeChat.

Weekly mode (--weekly): generates P&L trend charts and sends them on Saturday.

Required env vars (GitHub Secrets):
  WECHAT_WEBHOOK  - WeChat Work bot webhook URL
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.axes import Axes
import requests
import yfinance as yf

BASE_DIR = os.path.dirname(__file__)
PROFILES_FILE = os.path.join(BASE_DIR, "profiles.json")
PORTFOLIO_FILE = os.path.join(BASE_DIR, "portfolio.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

# Beijing Time UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))

# ── API URLs ─────────────────────────────────────────────────────────────────

# Sina Finance for gold price (AU9999 in CNY/gram)
SINA_GOLD_URL = "https://hq.sinajs.cn/list=hf_GC"

# Exchange rate via exchangerate-api (free, no key needed)
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"


def load_portfolio(path: str) -> dict:
    """Load portfolio configuration from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Stock Prices (yfinance) ──────────────────────────────────────────────────

def fetch_stock_prices(symbols: list[str], max_retries: int = 3) -> dict[str, float]:
    """Fetch latest closing prices for US stock symbols with retries."""
    prices: dict[str, float] = {}
    if not symbols:
        return prices
    for attempt in range(1, max_retries + 1):
        failed = [s for s in symbols if s not in prices or prices[s] == 0.0]
        if not failed:
            break
        if attempt > 1:
            wait = attempt * 5
            print(f"Retry {attempt}/{max_retries}: waiting {wait}s before retrying {', '.join(failed)}...")
            import time
            time.sleep(wait)
        tickers = yf.Tickers(" ".join(failed))
        for symbol in failed:
            try:
                hist = tickers.tickers[symbol].history(period="2d")
                if not hist.empty:
                    prices[symbol] = float(hist["Close"].iloc[-1])
                else:
                    print(f"Warning: no price data for {symbol} (attempt {attempt})", file=sys.stderr)
                    prices[symbol] = 0.0
            except (TypeError, ValueError, KeyError, IndexError, OSError) as exc:
                print(f"Warning: could not fetch price for {symbol} (attempt {attempt}): {exc}", file=sys.stderr)
                prices[symbol] = 0.0
    return prices


# ── Gold Price ───────────────────────────────────────────────────────────────

def fetch_gold_price_cny() -> float:
    """Fetch current gold price in CNY per gram.

    Tries multiple sources:
    1. Sina Finance COMEX gold → convert to CNY/gram via exchange rate
    2. Fallback to a fixed recent price if APIs fail
    """
    # Try fetching COMEX gold from Sina (USD per troy ounce)
    try:
        headers = {"Referer": "https://finance.sina.com.cn"}
        resp = requests.get(SINA_GOLD_URL, headers=headers, timeout=10)
        resp.encoding = "gbk"
        text = resp.text
        # Format: var hq_str_hf_GC="...,price,..."
        if "hf_GC" in text:
            parts = text.split('"')[1].split(",")
            # The current price is typically the first numeric field
            usd_per_oz = float(parts[0])
            # 1 troy ounce = 31.1035 grams
            usd_per_gram = usd_per_oz / 31.1035
            # Convert to CNY
            rate = fetch_usd_cny_rate()
            cny_per_gram = usd_per_gram * rate
            print(f"Gold price (COMEX→CNY): {cny_per_gram:.2f} CNY/gram")
            return cny_per_gram
    except Exception as exc:
        print(f"Warning: Sina gold API failed: {exc}", file=sys.stderr)

    # Fallback: use yfinance for gold futures
    try:
        gold = yf.Ticker("GC=F")
        hist = gold.history(period="2d")
        if not hist.empty:
            usd_per_oz = float(hist["Close"].iloc[-1])
            usd_per_gram = usd_per_oz / 31.1035
            rate = fetch_usd_cny_rate()
            cny_per_gram = usd_per_gram * rate
            print(f"Gold price (yfinance→CNY): {cny_per_gram:.2f} CNY/gram")
            return cny_per_gram
    except Exception as exc:
        print(f"Warning: yfinance gold fallback failed: {exc}", file=sys.stderr)

    print("Warning: using fallback gold price 1100 CNY/gram", file=sys.stderr)
    return 1100.0


# ── Exchange Rates ───────────────────────────────────────────────────────────

_usd_cny_cache: float | None = None


def fetch_usd_cny_rate() -> float:
    """Fetch USD to CNY exchange rate."""
    global _usd_cny_cache
    if _usd_cny_cache is not None:
        return _usd_cny_cache

    try:
        resp = requests.get(EXCHANGE_RATE_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"]["CNY"])
        _usd_cny_cache = rate
        print(f"USD/CNY rate: {rate:.4f}")
        return rate
    except Exception as exc:
        print(f"Warning: exchange rate API failed: {exc}, using 7.25", file=sys.stderr)
        _usd_cny_cache = 7.25
        return 7.25


def fetch_sgd_cny_rate() -> float:
    """Fetch SGD to CNY exchange rate."""
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/SGD", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"]["CNY"])
        print(f"SGD/CNY rate: {rate:.4f}")
        return rate
    except Exception as exc:
        print(f"Warning: SGD rate API failed: {exc}, using 5.35", file=sys.stderr)
        return 5.35


# ── Calculation ──────────────────────────────────────────────────────────────

def calculate_stock_performance(portfolio: dict, prices: dict[str, float]) -> dict:
    """Calculate stock portfolio performance in USD and CNY."""
    positions = portfolio["stocks"]["positions"]
    results = []
    total_cost_usd = 0.0
    total_value_usd = 0.0

    for pos in positions:
        symbol = pos["symbol"]
        shares = pos["shares"]
        # weighted average cost
        total_batch_cost = sum(b["shares"] * b["cost_price"] for b in pos["batches"])
        total_batch_shares = sum(b["shares"] for b in pos["batches"])
        avg_cost = total_batch_cost / total_batch_shares if total_batch_shares else 0

        current_price = prices.get(symbol, 0.0)
        position_cost = shares * avg_cost
        position_value = shares * current_price
        pnl = position_value - position_cost
        pnl_pct = (pnl / position_cost * 100) if position_cost else 0.0

        total_cost_usd += position_cost
        total_value_usd += position_value

        results.append({
            "symbol": symbol,
            "name": pos.get("name", symbol),
            "shares": shares,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "cost_usd": position_cost,
            "value_usd": position_value,
            "pnl_usd": pnl,
            "pnl_pct": pnl_pct,
        })

    return {
        "positions": results,
        "total_cost_usd": total_cost_usd,
        "total_value_usd": total_value_usd,
        "total_pnl_usd": total_value_usd - total_cost_usd,
        "total_pnl_pct": ((total_value_usd - total_cost_usd) / total_cost_usd * 100)
        if total_cost_usd else 0.0,
    }


def calculate_gold_performance(portfolio: dict, gold_price: float) -> dict:
    """Calculate gold portfolio performance in CNY."""
    gold = portfolio["gold"]
    positions = gold["positions"]
    results = []
    total_cost_cny = 0.0
    total_grams = 0.0

    for pos in positions:
        grams = pos["grams"]
        batch_cost = sum(
            b.get("grams", b.get("grams_approx", 0)) * b["cost_per_gram"]
            for b in pos["batches"]
        )
        batch_grams = sum(
            b.get("grams", b.get("grams_approx", 0))
            for b in pos["batches"]
        )
        avg_cost = batch_cost / batch_grams if batch_grams else 0
        value = grams * gold_price
        cost = grams * avg_cost
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0

        total_cost_cny += cost
        total_grams += grams

        results.append({
            "name": pos["name"],
            "grams": grams,
            "avg_cost": avg_cost,
            "current_price": gold_price,
            "cost_cny": cost,
            "value_cny": value,
            "pnl_cny": pnl,
            "pnl_pct": pnl_pct,
        })

    total_value_cny = total_grams * gold_price
    return {
        "positions": results,
        "total_grams": total_grams,
        "total_cost_cny": total_cost_cny,
        "total_value_cny": total_value_cny,
        "total_pnl_cny": total_value_cny - total_cost_cny,
        "total_pnl_pct": ((total_value_cny - total_cost_cny) / total_cost_cny * 100)
        if total_cost_cny else 0.0,
    }


# ── Report Formatting ────────────────────────────────────────────────────────

def format_report(stock_perf: dict, gold_perf: dict, portfolio: dict,
                  usd_cny: float, sgd_cny: float, gold_price: float) -> str:
    """Format a comprehensive report in Chinese."""
    now_beijing = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    lines = [
        "📊 每日资产报告",
        f"🕗 报告时间（北京时间）: {now_beijing}",
        f"💱 汇率: 1 USD = {usd_cny:.4f} CNY | 1 SGD = {sgd_cny:.4f} CNY",
        f"🥇 国际金价: {gold_price:.2f} CNY/克",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "一、美股持仓（Moomoo）",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for pos in stock_perf["positions"]:
        sign = "🟢" if pos["pnl_usd"] >= 0 else "🔴"
        lines.append(
            f"{sign} {pos['symbol']} ({pos['name']})\n"
            f"   持仓: {pos['shares']}股 | 均价: ${pos['avg_cost']:.2f} "
            f"| 现价: ${pos['current_price']:.2f}\n"
            f"   市值: ${pos['value_usd']:.2f} "
            f"| 盈亏: ${pos['pnl_usd']:+.2f} ({pos['pnl_pct']:+.2f}%)"
        )

    stock_total_sign = "🟢" if stock_perf["total_pnl_usd"] >= 0 else "🔴"
    stock_value_cny = stock_perf["total_value_usd"] * usd_cny
    lines += [
        f"\n  股票合计: ${stock_perf['total_value_usd']:.2f} "
        f"(≈ ¥{stock_value_cny:.2f})",
        f"  股票盈亏: {stock_total_sign} ${stock_perf['total_pnl_usd']:+.2f} "
        f"({stock_perf['total_pnl_pct']:+.2f}%)",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "二、黄金持仓（支付宝）",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for pos in gold_perf["positions"]:
        sign = "🟢" if pos["pnl_cny"] >= 0 else "🔴"
        lines.append(
            f"{sign} {pos['name']}\n"
            f"   持仓: {pos['grams']:.4f}克 | 均价: ¥{pos['avg_cost']:.2f}/克 "
            f"| 现价: ¥{pos['current_price']:.2f}/克\n"
            f"   市值: ¥{pos['value_cny']:.2f} "
            f"| 盈亏: ¥{pos['pnl_cny']:+.2f} ({pos['pnl_pct']:+.2f}%)"
        )

    gold_total_sign = "🟢" if gold_perf["total_pnl_cny"] >= 0 else "🔴"
    lines += [
        f"\n  黄金合计: ¥{gold_perf['total_value_cny']:.2f}",
        f"  黄金盈亏: {gold_total_sign} ¥{gold_perf['total_pnl_cny']:+.2f} "
        f"({gold_perf['total_pnl_pct']:+.2f}%)",
    ]

    # Cash
    cash = portfolio.get("cash", {})
    cash_sgd = cash.get("SGD", 0)
    cash_cny = cash.get("CNY", 0)
    cash_total_cny = cash_cny + cash_sgd * sgd_cny

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "三、现金",
        "━━━━━━━━━━━━━━━━━━━━",
        f"  人民币现金: ¥{cash_cny:.2f}",
    ]
    if cash_sgd > 0:
        lines.append(
            f"  新加坡元现金: S${cash_sgd:.2f} (≈ ¥{cash_sgd * sgd_cny:.2f})"
        )
    lines.append(f"  现金合计: ¥{cash_total_cny:.2f}")

    # Total summary
    total_cny = stock_value_cny + gold_perf["total_value_cny"] + cash_total_cny
    total_cost_cny = (stock_perf["total_cost_usd"] * usd_cny
                      + gold_perf["total_cost_cny"]
                      + cash_total_cny)
    total_pnl_cny = total_cny - total_cost_cny
    total_pnl_pct = (total_pnl_cny / total_cost_cny * 100) if total_cost_cny else 0.0
    total_sign = "🟢" if total_pnl_cny >= 0 else "🔴"

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "四、资产总览",
        "━━━━━━━━━━━━━━━━━━━━",
        f"  股票: ¥{stock_value_cny:.2f} "
        f"({stock_value_cny / total_cny * 100:.1f}%)" if total_cny else "  股票: ¥0.00",
        f"  黄金: ¥{gold_perf['total_value_cny']:.2f} "
        f"({gold_perf['total_value_cny'] / total_cny * 100:.1f}%)" if total_cny else "  黄金: ¥0.00",
        f"  现金: ¥{cash_total_cny:.2f} "
        f"({cash_total_cny / total_cny * 100:.1f}%)" if total_cny else "  现金: ¥0.00",
        "",
        f"  💰 总资产: ¥{total_cny:.2f}",
        f"  {total_sign} 总盈亏: ¥{total_pnl_cny:+.2f} ({total_pnl_pct:+.2f}%)",
        "",
        f"  📅 每月投资预算: ¥{portfolio.get('monthly_budget_cny', 0)}",
    ]

    return "\n".join(lines)


# ── History Tracking ─────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    """Load P&L history from JSON file."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list[dict]) -> None:
    """Save P&L history to JSON file."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def record_daily_snapshot(stock_perf: dict, gold_perf: dict,
                          usd_cny: float) -> None:
    """Append today's P&L data to history."""
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    history = load_history()

    # Avoid duplicate entries for the same date
    if history and history[-1].get("date") == today:
        history.pop()

    entry = {
        "date": today,
        "stocks": {},
        "stock_total_pnl_cny": stock_perf["total_pnl_usd"] * usd_cny,
        "gold_total_pnl_cny": gold_perf["total_pnl_cny"],
    }
    for pos in stock_perf["positions"]:
        entry["stocks"][pos["symbol"]] = {
            "pnl_usd": pos["pnl_usd"],
            "pnl_cny": pos["pnl_usd"] * usd_cny,
            "pnl_pct": pos["pnl_pct"],
        }

    history.append(entry)
    save_history(history)
    print(f"Saved daily snapshot for {today}")


# ── Chart Generation ─────────────────────────────────────────────────────────

def _setup_chart_style():
    """Configure matplotlib for Chinese text support."""
    for font in ["SimHei", "WenQuanYi Micro Hei", "Noto Sans CJK SC",
                 "Microsoft YaHei", "PingFang SC", "Arial Unicode MS",
                 "DejaVu Sans"]:
        try:
            plt.rcParams["font.sans-serif"] = [font] + plt.rcParams["font.sans-serif"]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


def _adaptive_params(n: int) -> dict:
    """Return adaptive visual parameters based on the number of data points."""
    if n <= 7:
        return {"fig_w": 10, "marker": 8, "lw": 2.5, "annot": "all",
                "font": 9, "tick_interval": 1}
    if n <= 14:
        return {"fig_w": 12, "marker": 6, "lw": 2.2, "annot": "all",
                "font": 8, "tick_interval": 1}
    if n <= 21:
        return {"fig_w": 14, "marker": 4, "lw": 2.0, "annot": "weekly",
                "font": 8, "tick_interval": 2}
    if n <= 35:
        return {"fig_w": 16, "marker": 3, "lw": 1.8, "annot": "weekly",
                "font": 7, "tick_interval": 3}
    return {"fig_w": 18, "marker": 2, "lw": 1.5, "annot": "endpoints",
            "font": 7, "tick_interval": 7}


def _should_annotate(idx: int, n: int, mode: str,
                     dates_dt: list[datetime]) -> bool:
    """Decide whether to annotate a data point."""
    if mode == "all":
        return True
    if mode == "weekly":
        # Annotate every Saturday (weekday 5) + first + last point
        if idx == 0 or idx == n - 1:
            return True
        return dates_dt[idx].weekday() == 5
    if mode == "endpoints":
        return idx == 0 or idx == n - 1
    return False


def _add_week_separators(ax: Axes, dates_dt: list[datetime],
                         date_nums: list[float]) -> None:
    """Add vertical dashed lines at each Saturday to mark week boundaries."""
    for i, dt in enumerate(dates_dt):
        if dt.weekday() == 5:  # Saturday
            ax.axvline(x=date_nums[i], color="#ffffff", linestyle=":",
                       alpha=0.15, linewidth=1)


def _style_ax(ax: Axes, title: str, n: int,
              accent: str, p: dict) -> None:
    """Apply common axis styling."""
    ax.axhline(y=0, color="#ffffff", linestyle="-", alpha=0.3, linewidth=0.8)
    ax.set_title(title, fontsize=16, color="white", fontweight="bold", pad=15)
    ax.set_ylabel("P&L (CNY)", fontsize=12, color="white")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=p["tick_interval"]))
    ax.tick_params(colors="white", labelsize=max(7, 10 - n // 10))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45 if n > 14 else 0,
             ha="right" if n > 14 else "center")
    ax.legend(loc="upper left", fontsize=10, facecolor="#16213e",
              edgecolor=accent, labelcolor="white")
    ax.grid(True, alpha=0.2, color="white")
    for spine in ax.spines.values():
        spine.set_color(accent)
        spine.set_alpha(0.3)



def _build_stock_chart(data: list[dict], date_nums: list[float],
                       dates_dt: list[datetime], p: dict,
                       ax: Axes) -> None:
    """Plot stock P&L lines on the given axes."""
    n = len(data)
    all_symbols: set[str] = set()
    for d in data:
        all_symbols.update(d.get("stocks", {}).keys())

    colors = [
        "#e94560", "#53d8fb", "#f8b500", "#6c5ce7", "#00b894", "#fd79a8",
        "#55efc4", "#a29bfe", "#ff7675", "#74b9ff", "#ffeaa7", "#dfe6e9",
        "#fab1a0", "#81ecec", "#636e72", "#e17055", "#00cec9", "#d63031",
    ]
    for i, symbol in enumerate(sorted(all_symbols)):
        pnl_values = [d.get("stocks", {}).get(symbol, {}).get("pnl_cny", 0)
                      for d in data]
        color = colors[i % len(colors)]
        ax.plot(date_nums, pnl_values, marker="o", linewidth=p["lw"],
                markersize=p["marker"], label=symbol, color=color)
        for j, (x, y) in enumerate(zip(date_nums, pnl_values)):
            if _should_annotate(j, n, p["annot"], dates_dt):
                ax.annotate(f"¥{y:.0f}", (x, y), textcoords="offset points",
                            xytext=(0, 10), ha="center", fontsize=p["font"],
                            color=color, fontweight="bold")

    total_pnl = [d.get("stock_total_pnl_cny", 0) for d in data]
    ax.plot(date_nums, total_pnl, marker="s", linewidth=p["lw"] + 0.5,
            markersize=p["marker"] + 1, label="Stock Total",
            color="#f39c12", linestyle="--")
    for j, (x, y) in enumerate(zip(date_nums, total_pnl)):
        if _should_annotate(j, n, p["annot"], dates_dt):
            ax.annotate(f"¥{y:.0f}", (x, y), textcoords="offset points",
                        xytext=(0, -15), ha="center", fontsize=p["font"],
                        color="#f39c12", fontweight="bold")

    _add_week_separators(ax, dates_dt, date_nums)
    _style_ax(ax, "Stock Cumulative P&L Trend (CNY)", n, "#53d8fb", p)


def _build_gold_chart(data: list[dict], date_nums: list[float],
                      dates_dt: list[datetime], p: dict,
                      ax: Axes) -> None:
    """Plot gold P&L line on the given axes."""
    n = len(data)
    gold_pnl = [d.get("gold_total_pnl_cny", 0) for d in data]
    ax.plot(date_nums, gold_pnl, marker="o", linewidth=p["lw"],
            markersize=p["marker"], color="#f1c40f", label="Gold Total")
    ax.fill_between(date_nums, gold_pnl, 0, alpha=0.12, color="#f1c40f")
    for j, (x, y) in enumerate(zip(date_nums, gold_pnl)):
        if _should_annotate(j, n, p["annot"], dates_dt):
            ax.annotate(f"¥{y:.0f}", (x, y), textcoords="offset points",
                        xytext=(0, 10), ha="center", fontsize=p["font"],
                        color="#f1c40f", fontweight="bold")

    _add_week_separators(ax, dates_dt, date_nums)
    _style_ax(ax, "Gold Cumulative P&L Trend (CNY)", n, "#f1c40f", p)


def _render_charts(history: list[dict],
                   save_to_files: bool = False,
                   chart_prefix: str = "chart") -> list[str]:
    """Core chart renderer. Uses ALL history data (cumulative).

    Returns base64-encoded PNGs (save_to_files=False) or file paths
    (save_to_files=True).
    """
    _setup_chart_style()

    if not history:
        print("No history data for charts.", file=sys.stderr)
        return []

    dates_dt = [datetime.strptime(d["date"], "%Y-%m-%d") for d in history]
    date_nums = list(mdates.date2num(dates_dt))
    n = len(history)
    p = _adaptive_params(n)

    results = []

    for chart_type in ("stock", "gold"):
        fig, ax = plt.subplots(figsize=(p["fig_w"], 6))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#16213e")

        if chart_type == "stock":
            _build_stock_chart(history, date_nums, dates_dt, p, ax)
        else:
            _build_gold_chart(history, date_nums, dates_dt, p, ax)

        fig.tight_layout()

        if save_to_files:
            suffix = "stocks" if chart_type == "stock" else "gold"
            path = os.path.join(BASE_DIR, f"{chart_prefix}_{suffix}.png")
            fig.savefig(path, dpi=150, bbox_inches="tight",
                        facecolor=fig.get_facecolor())
            results.append(path)
        else:
            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                        facecolor=fig.get_facecolor())
            buf.seek(0)
            results.append(base64.b64encode(buf.read()).decode())

        plt.close(fig)

    return results


def save_chart_files(history: list[dict], chart_prefix: str = "chart") -> list[str]:
    """Generate cumulative P&L charts saved to local files."""
    return _render_charts(history, save_to_files=True, chart_prefix=chart_prefix)


# ── WeChat Work Bot Notification ─────────────────────────────────────────────

def send_wechat_text(content: str, webhook_url: str) -> bool:
    """Send a text message via WeChat Work bot webhook."""
    payload = {"msgtype": "text", "text": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            print("WeChat Work notification sent successfully.")
            return True
        print(f"WeChat Work API returned: {data}", file=sys.stderr)
        return False
    except requests.RequestException as exc:
        print(f"Failed to send WeChat Work notification: {exc}", file=sys.stderr)
        return False


def send_wechat_image(image_path: str, webhook_url: str) -> bool:
    """Send an image via WeChat Work bot webhook (base64 + md5)."""
    import hashlib
    with open(image_path, "rb") as f:
        image_data = f.read()
    b64 = base64.b64encode(image_data).decode()
    md5 = hashlib.md5(image_data).hexdigest()
    payload = {"msgtype": "image", "image": {"base64": b64, "md5": md5}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            print(f"Image sent: {image_path}")
            return True
        print(f"WeChat Work image API returned: {data}", file=sys.stderr)
        return False
    except requests.RequestException as exc:
        print(f"Failed to send image: {exc}", file=sys.stderr)
        return False


def send_weekly_report(stock_perf: dict, gold_perf: dict, portfolio: dict,
                       usd_cny: float, sgd_cny: float, gold_price: float,
                       webhook_url: str, chart_prefix: str = "chart") -> bool:
    """Generate weekly charts and send report + images via WeChat Work bot."""
    history = load_history()
    if not history:
        print("No history data, skipping weekly report.", file=sys.stderr)
        return False

    # Save chart images to files
    paths = save_chart_files(history, chart_prefix=chart_prefix)
    for p in paths:
        print(f"Chart saved: {p}")

    # Send text report
    text_report = format_report(stock_perf, gold_perf, portfolio,
                                usd_cny, sgd_cny, gold_price)
    now_date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    success = send_wechat_text(f"📈 Weekly Report {now_date}\n\n{text_report}",
                               webhook_url)

    # Send chart images
    for p in paths:
        send_wechat_image(p, webhook_url)

    return success


# ── Main ─────────────────────────────────────────────────────────────────────

def _load_profile_config(profile_name: str) -> dict:
    """Load profile config from profiles.json."""
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        if profile_name in profiles:
            return profiles[profile_name]
    return {}


def main() -> int:
    global PORTFOLIO_FILE, HISTORY_FILE

    weekly_mode = "--weekly" in sys.argv
    charts_only = "--charts" in sys.argv
    no_notify = "--no-notify" in sys.argv

    # Parse --profile argument
    profile_name = "default"
    for arg in sys.argv:
        if arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]

    # Load profile config and override file paths
    profile_cfg = _load_profile_config(profile_name)
    if profile_cfg:
        PORTFOLIO_FILE = os.path.join(BASE_DIR, profile_cfg.get("portfolio_file", "portfolio.json"))
        HISTORY_FILE = os.path.join(BASE_DIR, profile_cfg.get("history_file", "history.json"))
        print(f"Using profile: {profile_name} ({profile_cfg.get('label', '')})")

    chart_prefix = profile_cfg.get("chart_prefix", "chart")

    if not os.path.exists(PORTFOLIO_FILE):
        print(f"Error: portfolio file not found at {PORTFOLIO_FILE}", file=sys.stderr)
        return 1

    portfolio = load_portfolio(PORTFOLIO_FILE)

    # Fetch exchange rates
    usd_cny = fetch_usd_cny_rate()
    sgd_cny = fetch_sgd_cny_rate()

    # Fetch stock prices
    symbols = [p["symbol"] for p in portfolio["stocks"]["positions"]]
    if symbols:
        print(f"Fetching stock prices for: {', '.join(symbols)}")
    stock_prices = fetch_stock_prices(symbols)

    # Abort if any stock price failed after retries
    failed_symbols = [s for s in symbols if stock_prices.get(s, 0) == 0.0]
    if failed_symbols:
        print(f"Error: failed to fetch prices for {', '.join(failed_symbols)} after retries. Aborting.", file=sys.stderr)
        return 1

    # Fetch gold price
    print("Fetching gold price...")
    gold_price = fetch_gold_price_cny()

    # Calculate performance
    stock_perf = calculate_stock_performance(portfolio, stock_prices)
    gold_perf = calculate_gold_performance(portfolio, gold_price)

    # Always save daily snapshot to history
    record_daily_snapshot(stock_perf, gold_perf, usd_cny)

    # Build report
    report = format_report(stock_perf, gold_perf, portfolio,
                           usd_cny, sgd_cny, gold_price)
    print("\n" + report + "\n")

    # --charts: generate chart PNGs locally and exit (for preview)
    if charts_only:
        history = load_history()
        paths = save_chart_files(history, chart_prefix=chart_prefix)
        for p in paths:
            print(f"Chart saved: {p}")
        return 0

    # --no-notify: only record data, skip sending notification
    if no_notify:
        print("Data recorded. Skipping notification (--no-notify).")
        return 0

    # Send WeChat Work bot notification
    webhook_url = profile_cfg.get("webhook_url", "") or os.environ.get("WECHAT_WEBHOOK", "")
    if not webhook_url:
        print(
            "Warning: No webhook URL configured. Skipping notification.",
            file=sys.stderr,
        )
        return 0

    if weekly_mode:
        success = send_weekly_report(stock_perf, gold_perf, portfolio,
                                     usd_cny, sgd_cny, gold_price, webhook_url,
                                     chart_prefix=chart_prefix)
    else:
        now_date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        success = send_wechat_text(f"📊 每日资产报告 {now_date}\n\n{report}",
                                   webhook_url)

    if not success:
        print("Warning: notification failed, but data was saved.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
