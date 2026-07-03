"""
Export RSI values for all stock symbols found in the listener logs.
Outputs rsi_lookup.csv which can be used as a VLOOKUP source in Excel.

Usage:
    python export_rsi_csv.py

Output: rsi_lookup.csv  (StockSymbol, RSI, Action)
"""

import csv
import os
import re
import time
import urllib3

import requests
from dotenv import load_dotenv

try:
    from tradingview_ta import TA_Handler, Interval as TVInterval
except ImportError:
    TA_Handler = None
    TVInterval = None

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Apply SSL bypass globally for corporate network interception environments.
# Must be done before any TA_Handler call.
_original_requests_post = requests.post
requests.post = lambda *a, **kw: _original_requests_post(*a, **{**kw, "verify": False})

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "")
RSI_PROVIDER = os.environ.get("RSI_PROVIDER", "auto").strip().lower()
RSI_INTERVAL = os.environ.get("RSI_INTERVAL", "1day")
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_SERIES_TYPE = os.environ.get("RSI_SERIES_TYPE", "close")
RSI_SYMBOL_SUFFIXES = [
    item.strip()
    for item in os.environ.get("RSI_SYMBOL_SUFFIXES", "NSE,BSE,").split(",")
    if item is not None
]
RSI_TV_EXCHANGES = [
    item.strip()
    for item in os.environ.get("RSI_TV_EXCHANGES", "NSE,BSE").split(",")
    if item and item.strip()
]
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
OUTPUT_FILE = os.environ.get("RSI_EXPORT_CSV", "./rsi_lookup.csv")

TV_INTERVAL_MAP = {
    "1min": TVInterval.INTERVAL_1_MINUTE if TVInterval else None,
    "5min": TVInterval.INTERVAL_5_MINUTES if TVInterval else None,
    "15min": TVInterval.INTERVAL_15_MINUTES if TVInterval else None,
    "30min": TVInterval.INTERVAL_30_MINUTES if TVInterval else None,
    "60min": TVInterval.INTERVAL_1_HOUR if TVInterval else None,
    "1h": TVInterval.INTERVAL_1_HOUR if TVInterval else None,
    "4h": TVInterval.INTERVAL_4_HOURS if TVInterval else None,
    "1day": TVInterval.INTERVAL_1_DAY if TVInterval else None,
    "daily": TVInterval.INTERVAL_1_DAY if TVInterval else None,
}

RSI_CACHE = {}


def get_rsi_action(rsi_value: float) -> str:
    if rsi_value < 30:
        return "Hold"
    if rsi_value <= 70:
        return "Buy"
    return "Sell"


def _resolve_tv_interval():
    return TV_INTERVAL_MAP.get(RSI_INTERVAL.lower()) or (
        TVInterval.INTERVAL_15_MINUTES if TVInterval else None
    )


def _fetch_rsi_from_tradingview(symbol: str):
    if TA_Handler is None:
        return None

    interval = _resolve_tv_interval()
    if interval is None:
        return None

    for exchange in RSI_TV_EXCHANGES:
        try:
            handler = TA_Handler(
                symbol=symbol,
                screener="india",
                exchange=exchange,
                interval=interval,
            )
            analysis = handler.get_analysis()
            rsi_value = analysis.indicators.get("RSI")
            if rsi_value is None:
                continue
            action = get_rsi_action(float(rsi_value))
            return round(float(rsi_value), 2), action
        except Exception as exc:
            print(f"  [TV] {symbol} on {exchange} failed: {exc}")
            continue

    return None


def _fetch_rsi_from_alpha_vantage(symbol: str):
    if not ALPHA_VANTAGE_KEY:
        return None

    for suffix in RSI_SYMBOL_SUFFIXES:
        av_symbol = f"{symbol}.{suffix}" if suffix else symbol
        url = (
            "https://www.alphavantage.co/query"
            f"?function=RSI&symbol={av_symbol}"
            f"&interval={RSI_INTERVAL}&time_period={RSI_PERIOD}&series_type={RSI_SERIES_TYPE}"
            f"&apikey={ALPHA_VANTAGE_KEY}"
        )
        try:
            response = requests.get(url, timeout=20, verify=False)
            data = response.json()
            if "Note" in data or "Information" in data:
                return None
            if "Error Message" in data:
                continue
            rsi_data = data.get("Technical Analysis: RSI", {})
            if not rsi_data:
                continue
            latest_time = next(iter(rsi_data))
            latest_rsi = float(rsi_data[latest_time]["RSI"])
            action = get_rsi_action(latest_rsi)
            return round(latest_rsi, 2), action
        except Exception:
            continue

    return None


def fetch_rsi(symbol: str):
    if symbol in RSI_CACHE:
        return RSI_CACHE[symbol]

    result = None
    if RSI_PROVIDER in ("auto", "tradingview"):
        result = _fetch_rsi_from_tradingview(symbol)

    if not result and RSI_PROVIDER in ("auto", "alpha_vantage"):
        result = _fetch_rsi_from_alpha_vantage(symbol)

    if result:
        RSI_CACHE[symbol] = result
        return result

    RSI_CACHE[symbol] = (0.0, "No Data")
    return 0.0, "No Data"


def collect_symbols_from_logs() -> list:
    """Extract all unique stock symbols from the log file."""
    log_file = os.path.join(LOG_DIR, "stock_alerts.log")
    symbols = set()

    # Pattern: "Sent alert to Power Automate: SYMBOL | AlertType"
    pattern = re.compile(r"Sent alert to Power Automate:\s+([A-Z0-9&\-\.]+)\s+\|")

    if not os.path.exists(log_file):
        print(f"Log file not found: {log_file}")
        return []

    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                sym = match.group(1).strip().upper()
                if sym and sym != "SYSTEM":
                    symbols.add(sym)

    return sorted(symbols)


def main():
    print("Collecting symbols from logs...")
    symbols = collect_symbols_from_logs()

    if not symbols:
        print("No symbols found in logs. Nothing to export.")
        return

    print(f"Found {len(symbols)} unique symbols. Fetching RSI...")

    rows = []
    for idx, symbol in enumerate(symbols, start=1):
        rsi, action = fetch_rsi(symbol)
        rows.append({"StockSymbol": symbol, "RSI": rsi, "Action": action})
        print(f"[{idx}/{len(symbols)}] {symbol} => RSI={rsi}, Action={action}")
        time.sleep(0.2)  # Avoid hitting API too fast

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["StockSymbol", "RSI", "Action"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nExported {len(rows)} rows to: {os.path.abspath(OUTPUT_FILE)}")
    print(
        "\nIn Excel, use this VLOOKUP formula to fill RSI column:"
    )
    print(
        "  =IFERROR(VLOOKUP(C2, rsi_lookup.csv!$A:$B, 2, 0), 0)"
    )
    print("Or to fill Action column:")
    print(
        "  =IFERROR(VLOOKUP(C2, rsi_lookup.csv!$A:$C, 3, 0), \"No Data\")"
    )


if __name__ == "__main__":
    main()
