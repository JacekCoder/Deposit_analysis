"""
Deposit / Portfolio Analysis Script
Fetches the latest US stock prices, calculates account performance,
and sends a summary notification via WeChat (WxPusher).

Required GitHub Secrets:
  WXPUSHER_APP_TOKEN  - WxPusher application token
                        (register at https://wxpusher.zjiecode.com/)
  WXPUSHER_UID        - WxPusher user UID (follow the WxPusher official
                        WeChat account and scan the QR code to get your UID)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "portfolio.json")
WXPUSHER_API_URL = "https://wxpusher.zjiecode.com/api/send/message"

# Beijing Time is UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))


def load_portfolio(path: str) -> dict:
    """Load portfolio configuration from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch the latest closing prices for the given stock symbols."""
    prices: dict[str, float] = {}
    if not symbols:
        return prices
    tickers = yf.Tickers(" ".join(symbols))
    for symbol in symbols:
        try:
            # Request 2 days of history so that if today's data is not yet
            # available (e.g. market still open or weekend), we fall back to
            # the most recent available closing price.
            hist = tickers.tickers[symbol].history(period="2d")
            if not hist.empty:
                prices[symbol] = float(hist["Close"].iloc[-1])
            else:
                print(f"Warning: no price data returned for {symbol}", file=sys.stderr)
                prices[symbol] = 0.0
        except (TypeError, ValueError, KeyError, IndexError, OSError) as exc:
            print(f"Warning: could not fetch price for {symbol}: {exc}", file=sys.stderr)
            prices[symbol] = 0.0
    return prices


def calculate_performance(portfolio: dict, prices: dict[str, float]) -> dict:
    """Calculate per-position and total portfolio performance."""
    results = []
    total_cost = 0.0
    total_value = 0.0

    for position in portfolio.get("positions", []):
        symbol = position["symbol"]
        shares = position["shares"]
        cost_basis = position["cost_basis"]  # per-share cost

        current_price = prices.get(symbol, 0.0)
        position_cost = shares * cost_basis
        position_value = shares * current_price
        pnl = position_value - position_cost
        pnl_pct = (pnl / position_cost * 100) if position_cost else 0.0

        total_cost += position_cost
        total_value += position_value

        results.append(
            {
                "symbol": symbol,
                "name": position.get("name", symbol),
                "shares": shares,
                "cost_basis": cost_basis,
                "current_price": current_price,
                "position_cost": position_cost,
                "position_value": position_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0

    return {
        "account_name": portfolio.get("account_name", "My Portfolio"),
        "currency": portfolio.get("currency", "USD"),
        "positions": results,
        "total_cost": total_cost,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
    }


def format_report(performance: dict) -> str:
    """Format the performance data into a human-readable text report."""
    currency = performance["currency"]
    now_beijing = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📊 {performance['account_name']}",
        f"🕗 Report Time (Beijing): {now_beijing}",
        "",
        "── Position Details ──",
    ]

    for pos in performance["positions"]:
        sign = "🟢" if pos["pnl"] >= 0 else "🔴"
        lines.append(
            f"{sign} {pos['symbol']} ({pos['name']})\n"
            f"   Shares: {pos['shares']} | Cost: {currency}{pos['cost_basis']:.2f} "
            f"| Price: {currency}{pos['current_price']:.2f}\n"
            f"   Value: {currency}{pos['position_value']:.2f} "
            f"| P&L: {currency}{pos['pnl']:+.2f} ({pos['pnl_pct']:+.2f}%)"
        )

    total_sign = "🟢" if performance["total_pnl"] >= 0 else "🔴"
    lines += [
        "",
        "── Portfolio Summary ──",
        f"Total Cost:  {currency}{performance['total_cost']:.2f}",
        f"Total Value: {currency}{performance['total_value']:.2f}",
        f"Total P&L:   {total_sign} {currency}{performance['total_pnl']:+.2f} "
        f"({performance['total_pnl_pct']:+.2f}%)",
    ]

    return "\n".join(lines)


def send_wechat(message: str, app_token: str, uid: str) -> bool:
    """Send a WeChat message via WxPusher."""
    payload = {
        "appToken": app_token,
        "content": message,
        "contentType": 1,  # 1 = plain text
        "uids": [uid],
    }
    try:
        response = requests.post(WXPUSHER_API_URL, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            print("WeChat notification sent successfully.")
            return True
        print(f"WxPusher API returned non-success: {data}", file=sys.stderr)
        return False
    except requests.RequestException as exc:
        print(f"Failed to send WeChat notification: {exc}", file=sys.stderr)
        return False


def main() -> int:
    # ── Load portfolio ────────────────────────────────────────────────────────
    if not os.path.exists(PORTFOLIO_FILE):
        print(f"Error: portfolio file not found at {PORTFOLIO_FILE}", file=sys.stderr)
        return 1

    portfolio = load_portfolio(PORTFOLIO_FILE)
    symbols = [p["symbol"] for p in portfolio.get("positions", [])]

    if not symbols:
        print("No positions found in portfolio. Exiting.", file=sys.stderr)
        return 1

    # ── Fetch latest prices ───────────────────────────────────────────────────
    print(f"Fetching prices for: {', '.join(symbols)}")
    prices = fetch_prices(symbols)

    # ── Calculate performance ─────────────────────────────────────────────────
    performance = calculate_performance(portfolio, prices)

    # ── Build report ──────────────────────────────────────────────────────────
    report = format_report(performance)
    print("\n" + report + "\n")

    # ── Send WeChat notification ──────────────────────────────────────────────
    app_token = os.environ.get("WXPUSHER_APP_TOKEN", "")
    uid = os.environ.get("WXPUSHER_UID", "")

    if not app_token or not uid:
        print(
            "Warning: WXPUSHER_APP_TOKEN or WXPUSHER_UID not set. "
            "Skipping WeChat notification.",
            file=sys.stderr,
        )
        return 0

    success = send_wechat(report, app_token, uid)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
