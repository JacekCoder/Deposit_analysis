"""
Deposit Analysis - Daily asset report for stocks and gold.

Usage:
  Set the SERVERCHAN_SCKEY environment variable to your Server酱 (ServerChan) key.
  Optionally override portfolio and cost via environment variables (see below).
  Run:  python deposit_analysis.py

Environment variables (all optional – defaults match the example portfolio):
  SERVERCHAN_SCKEY   Server酱 send-key (required to push WeChat notification)
  NVDA_SHARES        Number of NVDA shares held         (default: 2.5)
  QQQ_SHARES         Number of QQQ shares held          (default: 1.3)
  GOLD_GRAMS         Grams of gold held                 (default: 2.5)
  COST_RMB           Initial total investment in RMB    (default: 7500)
"""

import os
import sys

import requests
import yfinance as yf


# ---------------------------------------------------------------------------
# Portfolio configuration (can be overridden via environment variables)
# ---------------------------------------------------------------------------
NVDA_SHARES = float(os.environ.get("NVDA_SHARES", 2.5))
QQQ_SHARES = float(os.environ.get("QQQ_SHARES", 1.3))
GOLD_GRAMS = float(os.environ.get("GOLD_GRAMS", 2.5))
COST_RMB = float(os.environ.get("COST_RMB", 7500))

# Server酱 send-key for WeChat push notifications
SERVERCHAN_SCKEY = os.environ.get("SERVERCHAN_SCKEY", "")

# Report message template (use .format_map() with a dict of computed values)
REPORT_TEMPLATE = """
📅 今日资产日报
💰 总资产: {total_value:.2f} RMB
📈 总盈亏: {profit:.2f} RMB

---资产明细---
🇺🇸 股票市值: {stock_value_rmb:.2f} RMB
🏅 黄金市值: {gold_value_rmb:.2f} RMB
"""


# ---------------------------------------------------------------------------
# Data-fetching helpers
# ---------------------------------------------------------------------------

def get_us_stock_price(ticker: str) -> float:
    """Return the latest closing price (USD) for a US stock ticker via yfinance."""
    stock = yf.Ticker(ticker)
    hist = stock.history(period="1d")
    if hist.empty:
        raise ValueError(f"No price data returned for ticker '{ticker}'.")
    return float(hist["Close"].iloc[-1])


def get_cn_gold_price() -> float:
    """
    Return the current gold spot price in CNY per gram.

    Uses the free metals-api-compatible endpoint provided by exchangerate.host
    to get XAU/CNY, then converts from troy-ounce to gram.

    1 troy ounce = 31.1035 grams
    """
    url = "https://api.exchangerate.host/live"
    params = {
        "source": "XAU",
        "currencies": "CNY",
        "places": 4,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success", True):
        raise ValueError(f"Gold price API error: {data.get('error', data)}")

    # The rate is CNY per 1 troy ounce of gold
    xau_cny = data["quotes"]["XAUCNY"]
    troy_ounce_to_gram = 31.1035
    return float(xau_cny) / troy_ounce_to_gram


def get_exchange_rate(from_currency: str, to_currency: str) -> float:
    """
    Return the exchange rate from *from_currency* to *to_currency*.

    Uses the free exchangerate.host API (no API key required for basic usage).
    """
    url = "https://api.exchangerate.host/convert"
    params = {
        "from": from_currency,
        "to": to_currency,
        "amount": 1,
        "places": 6,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success", True):
        raise ValueError(f"Exchange rate API error: {data.get('error', data)}")

    return float(data["result"])


# ---------------------------------------------------------------------------
# WeChat push (Server酱)
# ---------------------------------------------------------------------------

def send_to_wechat(message: str, title: str = "📊 资产日报") -> None:
    """
    Push *message* to WeChat via Server酱 (https://sct.ftqq.com/).

    Requires the SERVERCHAN_SCKEY environment variable to be set.
    If it is not set, the message is printed to stdout instead.
    """
    if not SERVERCHAN_SCKEY:
        print("SERVERCHAN_SCKEY not set – printing report to stdout instead:\n")
        print(message)
        return

    url = f"https://sctapi.ftqq.com/{SERVERCHAN_SCKEY}.send"
    payload = {"title": title, "desp": message}
    resp = requests.post(url, data=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"Server酱 push failed: {result}")
    print("WeChat notification sent successfully.")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main() -> None:
    print("Fetching market data …")

    nvda_price = get_us_stock_price("NVDA")
    qqq_price = get_us_stock_price("QQQ")
    gold_price = get_cn_gold_price()
    usd_to_rmb = get_exchange_rate("USD", "CNY")

    stock_value_usd = (NVDA_SHARES * nvda_price) + (QQQ_SHARES * qqq_price)
    stock_value_rmb = stock_value_usd * usd_to_rmb
    gold_value_rmb = GOLD_GRAMS * gold_price

    total_value = stock_value_rmb + gold_value_rmb
    profit = total_value - COST_RMB

    message = REPORT_TEMPLATE.format(
        total_value=total_value,
        profit=profit,
        stock_value_rmb=stock_value_rmb,
        gold_value_rmb=gold_value_rmb,
    )

    send_to_wechat(message)


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError) as exc:
        print(f"Data fetch error: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"Notification error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
