"""
Stock Alert Automation - Telegram Listener
============================================
Reads stock alert messages from a Telegram group using a personal account
(via Telethon), parses them, and forwards structured data to a Power
Automate HTTP trigger for storage in SharePoint and downstream Power BI
reporting.

Why Telethon instead of a bot:
A Telegram Bot API account can only read messages in a group if it has been
added as a member with appropriate permissions, which usually requires admin
action. Telethon authenticates as a real user account (using your phone
number), so it can read any group you are already a member of without
needing admin rights or bot privileges.

Author: Generated for production use
"""

import os
import re
import json
import time
import logging
import asyncio
import contextlib
import urllib3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

try:
    from tradingview_ta import TA_Handler, Interval as TVInterval
except ImportError:
    TA_Handler = None
    TVInterval = None

load_dotenv()  # loads variables from a local .env file if present; safe no-op otherwise
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Apply SSL bypass globally for corporate network interception environments.
# Must be done before any TradingView or Alpha Vantage API call.
_original_requests_post = requests.post
requests.post = lambda *a, **kw: _original_requests_post(*a, **{**kw, "verify": False})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Load all secrets from environment variables. Never hardcode credentials.
# See config.env.example for the full list of required variables.

API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")
PHONE_NUMBER = os.environ.get("TG_PHONE_NUMBER")
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "stock_alert_session")

# Single target group/channel: numeric ID, @username, or invite link
TARGET_GROUP = os.environ.get("TG_GROUP_ID")

# Convert numeric IDs properly so Telethon treats them as peer IDs, not usernames.
if TARGET_GROUP and TARGET_GROUP.lstrip("-").isdigit():
    TARGET_GROUP = int(TARGET_GROUP)

POWER_AUTOMATE_URL = os.environ.get("POWER_AUTOMATE_URL")
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "")
RSI_PROVIDER = os.environ.get("RSI_PROVIDER", "auto").strip().lower()
RSI_INTERVAL = os.environ.get("RSI_INTERVAL", "1day")
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_SERIES_TYPE = os.environ.get("RSI_SERIES_TYPE", "close")
RSI_YELLOW_PERIOD = int(os.environ.get("RSI_YELLOW_PERIOD", "14"))
RSI_YELLOW_MA_TYPE = os.environ.get("RSI_YELLOW_MA_TYPE", "sma").strip().lower()
BUY_ALERT_REQUIRE_FULL_SLOPE = os.environ.get(
    "BUY_ALERT_REQUIRE_FULL_SLOPE", "true"
).lower() in ("1", "true", "yes", "on")
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
RSI_CACHE_TTL_SECONDS = int(os.environ.get("RSI_CACHE_TTL_SECONDS", "300"))
IST_TIMEZONE = os.environ.get("IST_TIMEZONE", "Asia/Kolkata")
DAILY_REFRESH_ENABLED = os.environ.get("DAILY_REFRESH_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DAILY_REFRESH_WEBHOOK_URL = os.environ.get("DAILY_REFRESH_WEBHOOK_URL", POWER_AUTOMATE_URL)

LOG_DIR = os.environ.get("LOG_DIR", "./logs")
LOG_FILE = os.path.join(LOG_DIR, "stock_alerts.log")
MAX_RETRIES = int(os.environ.get("WEBHOOK_MAX_RETRIES", 3))
RETRY_BACKOFF_SECONDS = int(os.environ.get("WEBHOOK_RETRY_BACKOFF", 2))

if not all([API_ID, API_HASH, PHONE_NUMBER, TARGET_GROUP, POWER_AUTOMATE_URL]):
    raise SystemExit(
        "Missing required environment variables. Check TG_API_ID, TG_API_HASH, "
        "TG_PHONE_NUMBER, TG_GROUP_ID, and POWER_AUTOMATE_URL."
    )

API_ID = int(API_ID)
IST_TZ = ZoneInfo(IST_TIMEZONE)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("stock_alerts")
logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
console_handler = logging.StreamHandler()

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

if not ALPHA_VANTAGE_KEY:
    logger.warning(
        "ALPHA_VANTAGE_KEY is not set. RSI will be sent as 0.0 with Action='No Data'."
    )

if TA_Handler is None:
    logger.warning(
        "tradingview_ta package not installed. TradingView RSI provider will be unavailable."
    )

if RSI_PROVIDER not in ("auto", "tradingview", "alpha_vantage"):
    logger.warning("Invalid RSI_PROVIDER '%s'. Falling back to 'auto'.", RSI_PROVIDER)
    RSI_PROVIDER = "auto"

RSI_CACHE = {}
RSI_HISTORY = defaultdict(list)

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
    "1week": TVInterval.INTERVAL_1_WEEK if TVInterval else None,
    "weekly": TVInterval.INTERVAL_1_WEEK if TVInterval else None,
    "1month": TVInterval.INTERVAL_1_MONTH if TVInterval else None,
    "monthly": TVInterval.INTERVAL_1_MONTH if TVInterval else None,
}

# ---------------------------------------------------------------------------
# Alert parsing
# ---------------------------------------------------------------------------
# Each line in the source messages follows the pattern:
#   "<Alert Type>: <STOCK_SYMBOL>"
# Examples seen in the group:
#   "Pocket Pivot Volume: ZENSARTECH"
#   "Fresh IV: ZENSARTECH"
#   "50Day RVOL greater than 4x: INFOBEAN"
#   "Breakout Alert: TATAMOTORS"
#
# Because alert-type wording varies, we capture everything before the colon
# as the alert type rather than hardcoding strict categories, and we also
# tag each parsed alert with a normalized "Category" used for Power BI
# grouping (Volume, IV, RVOL, Breakout, Other).

ALERT_LINE_PATTERN = re.compile(r"^\s*(?P<alert_type>[^:]+):\s*(?P<symbol>[A-Z0-9&\-\.]+)\s*$")

CATEGORY_RULES = [
    (re.compile(r"pocket pivot|volume", re.IGNORECASE), "Volume Spike"),
    (re.compile(r"fresh iv|iv\b", re.IGNORECASE), "Fresh IV"),
    (re.compile(r"rvol", re.IGNORECASE), "RVOL"),
    (re.compile(r"breakout", re.IGNORECASE), "Breakout"),
]


def categorize(alert_type: str) -> str:
    """Map a free-text alert type to a normalized category for reporting."""
    for pattern, category in CATEGORY_RULES:
        if pattern.search(alert_type):
            return category
    return "Other"


def get_rsi_action(rsi_value: float) -> str:
    """Map RSI to trading action using fixed bands."""
    if rsi_value < 30:
        return "Hold"
    if rsi_value <= 70:
        return "Buy"
    return "Sell"


def normalize_rsi_value(rsi_value) -> float:
    """Ensure webhook payload always sends a numeric RSI value."""
    if rsi_value is None:
        return 0.0
    return float(rsi_value)


def _get_cached_rsi(symbol: str):
    cached = RSI_CACHE.get(symbol)
    if not cached:
        return None

    age_seconds = time.time() - cached["timestamp"]
    if age_seconds > RSI_CACHE_TTL_SECONDS:
        return None

    return cached["rsi"], cached["action"]


def _get_last_cached_rsi(symbol: str):
    """Return last known RSI/action even if cache TTL has expired."""
    cached = RSI_CACHE.get(symbol)
    if not cached:
        return None
    return cached["rsi"], cached["action"]


def _set_cached_rsi(symbol: str, rsi_value: float, action: str) -> None:
    RSI_CACHE[symbol] = {
        "rsi": round(float(rsi_value), 2),
        "action": action,
        "timestamp": time.time(),
    }


def _moving_average(values: list[float], period: int) -> float | None:
    if not values:
        return None
    slice_values = values[-period:]
    return sum(slice_values) / len(slice_values)


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (period + 1)
    ema_value = values[0]
    for value in values[1:]:
        ema_value = (value * alpha) + (ema_value * (1 - alpha))
    return ema_value


def _rsi_based_ma(values: list[float], period: int, ma_type: str) -> float | None:
    if not values:
        return None
    slice_values = values[-period:]
    if ma_type == "ema":
        return _ema(slice_values, period)
    return _moving_average(slice_values, period)


def evaluate_buy_crossover(symbol: str, current_rsi: float):
    """Detect Blue RSI uptrend crossover above Yellow RSI-based MA."""
    previous_values = RSI_HISTORY[symbol]
    previous_rsi = previous_values[-1] if previous_values else None
    previous_yellow = _rsi_based_ma(previous_values, RSI_YELLOW_PERIOD, RSI_YELLOW_MA_TYPE)

    updated_values = previous_values + [current_rsi]
    RSI_HISTORY[symbol] = updated_values[-(RSI_YELLOW_PERIOD * 3):]
    current_yellow = _rsi_based_ma(
        RSI_HISTORY[symbol], RSI_YELLOW_PERIOD, RSI_YELLOW_MA_TYPE
    )

    if previous_rsi is None or previous_yellow is None or current_yellow is None:
        return False, current_yellow

    crossed_up = previous_rsi <= previous_yellow and current_rsi > current_yellow
    blue_uptrend = current_rsi > previous_rsi
    yellow_uptrend = current_yellow >= previous_yellow
    slope_ok = blue_uptrend and yellow_uptrend if BUY_ALERT_REQUIRE_FULL_SLOPE else blue_uptrend

    return crossed_up and slope_ok, current_yellow


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
            logger.info(
                "RSI resolved for %s using TradingView %s interval=%s => %.2f (%s)",
                symbol,
                exchange,
                RSI_INTERVAL,
                float(rsi_value),
                action,
            )
            return round(float(rsi_value), 2), action
        except Exception as exc:
            logger.debug("TradingView RSI lookup failed for %s on %s: %s", symbol, exchange, exc)

    return None


def _fetch_rsi_from_alpha_vantage(symbol: str):
    if not ALPHA_VANTAGE_KEY:
        return None

    # Try multiple exchanges/symbol formats to align with chart source used in TradingView.
    # Example order: NSE first, then BSE, then raw symbol.
    for suffix in RSI_SYMBOL_SUFFIXES:
        av_symbol = f"{symbol}.{suffix}" if suffix else symbol
        url = (
            "https://www.alphavantage.co/query"
            f"?function=RSI&symbol={av_symbol}"
            f"&interval={RSI_INTERVAL}&time_period={RSI_PERIOD}&series_type={RSI_SERIES_TYPE}"
            f"&apikey={ALPHA_VANTAGE_KEY}"
        )

        response = requests.get(url, timeout=20, verify=False)
        data = response.json()

        if "Note" in data or "Information" in data:
            logger.warning(
                "Alpha Vantage rate/info for %s: %s",
                av_symbol,
                data.get("Note") or data.get("Information"),
            )
            return None

        if "Error Message" in data:
            logger.debug("Alpha Vantage symbol error for %s: %s", av_symbol, data["Error Message"])
            continue

        rsi_data = data.get("Technical Analysis: RSI", {})
        if not rsi_data:
            logger.debug("No RSI data for %s at interval %s", av_symbol, RSI_INTERVAL)
            continue

        latest_time = next(iter(rsi_data))
        latest_rsi = float(rsi_data[latest_time]["RSI"])
        action = get_rsi_action(latest_rsi)
        logger.info(
            "RSI resolved for %s using %s interval=%s period=%s => %.2f (%s)",
            symbol,
            av_symbol,
            RSI_INTERVAL,
            RSI_PERIOD,
            latest_rsi,
            action,
        )
        return round(latest_rsi, 2), action

    return None


def calculate_rsi(symbol):
    try:
        cached = _get_cached_rsi(symbol)
        if cached:
            return cached

        result = None

        if RSI_PROVIDER in ("auto", "tradingview"):
            result = _fetch_rsi_from_tradingview(symbol)
            if result:
                _set_cached_rsi(symbol, result[0], result[1])
                return result

        if RSI_PROVIDER in ("auto", "alpha_vantage"):
            result = _fetch_rsi_from_alpha_vantage(symbol)
            if result:
                _set_cached_rsi(symbol, result[0], result[1])
                return result

        stale_cached = _get_last_cached_rsi(symbol)
        if stale_cached:
            logger.warning(
                "Using stale cached RSI for %s => %.2f (%s)",
                symbol,
                stale_cached[0],
                stale_cached[1],
            )
            return stale_cached

        return None, "No Data"

    except Exception as e:
        logger.exception("RSI Error for %s: %s", symbol, e)
        return None, "Error"


def parse_message(raw_text: str, message_timestamp: datetime) -> list:
    """
    Parse a raw Telegram message into a list of structured alert dicts.
    A single message can contain multiple alert lines, so this returns a list.
    """
    parsed_alerts = []

    if not raw_text:
        return parsed_alerts

    # Derive source bucket from full message text once, then stamp on each parsed line.
    if "Pocket Pivot" in raw_text:
        source_group = "Volume Alerts"
    elif "Breakout" in raw_text:
        source_group = "Breakout Alerts"
    elif "Undercuts 20EMA" in raw_text:
        source_group = "20EMA & 50EMA Undercuts"
    elif "Shakeouts" in raw_text:
        source_group = "Other Alerts"
    elif "Blue dots" in raw_text:
        source_group = "GMMA Cloud Alerts"
    else:
        source_group = "Other"

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        match = ALERT_LINE_PATTERN.match(line)
        if not match:
            logger.debug("Skipped unrecognized line format: %s", line)
            continue

        alert_type = match.group("alert_type").strip()
        stock_symbol = match.group("symbol").strip().upper()
        rsi_value, action = calculate_rsi(stock_symbol)
        print(f"{stock_symbol} RSI: {rsi_value}, Action: {action}")

        buy_signal = False
        yellow_line = None
        if rsi_value is not None:
            buy_signal, yellow_line = evaluate_buy_crossover(stock_symbol, float(rsi_value))
            if buy_signal:
                action = "Buy"
                logger.info(
                    "BUY signal for %s: Blue RSI crossed above Yellow RSI-based MA with uptrend (full slope binding=%s)",
                    stock_symbol,
                    BUY_ALERT_REQUIRE_FULL_SLOPE,
                )

        payload = {
            "Title": f"{stock_symbol} | {alert_type}",
            "AlertType": alert_type,
            "StockSymbol": stock_symbol,
            "Category": categorize(alert_type),
            "SourceGroup": source_group,
            "Date": message_timestamp.strftime("%Y-%m-%d"),
            "Time": message_timestamp.strftime("%H:%M:%S"),
            "TimestampUtc": message_timestamp.astimezone(timezone.utc).isoformat(),
            "Status": "New",
            "Count": 1,
            "Notes": (
                "BUY ALERT: Blue RSI crossed above Yellow RSI-based MA in uptrend (full slope binding)"
                if buy_signal
                else ""
            ),
        }

        payload["RSI"] = normalize_rsi_value(rsi_value)
        payload["Action"] = action
        payload["BuySignal"] = buy_signal
        if yellow_line is not None:
            payload["RSIYellowLine"] = round(float(yellow_line), 2)
        parsed_alerts.append(payload)

    return parsed_alerts


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

def send_to_power_automate(alert_record: dict) -> bool:
    """
    POST a single parsed alert to the Power Automate HTTP trigger.
    Retries with exponential backoff on transient failures.
    Returns True on success, False if all retries are exhausted.
    """
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SHARED_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SHARED_SECRET

    payload = json.dumps(alert_record)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                POWER_AUTOMATE_URL,
                data=payload,
                headers=headers,
                timeout=15,
                verify=False,
            )
            if response.status_code in (200, 201, 202):
                logger.info(
                    "Sent alert to Power Automate: %s | %s (attempt %d)",
                    alert_record["StockSymbol"],
                    alert_record["AlertType"],
                    attempt,
                )
                return True

            logger.warning(
                "Power Automate responded with status %d on attempt %d: %s",
                response.status_code,
                attempt,
                response.text[:300],
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Webhook request failed on attempt %d: %s", attempt, exc)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    logger.error(
        "Failed to deliver alert after %d attempts: %s | %s",
        MAX_RETRIES,
        alert_record["StockSymbol"],
        alert_record["AlertType"],
    )
    return False


def _seconds_until_next_ist_midnight(now_utc: datetime | None = None) -> float:
    """Return seconds from now until the next IST 00:00."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST_TZ)
    next_midnight_ist = (now_ist + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (next_midnight_ist - now_ist).total_seconds()


def _build_daily_refresh_payload(run_time_utc: datetime) -> dict:
    run_time_ist = run_time_utc.astimezone(IST_TZ)
    return {
        "Title": "SYSTEM | Daily Refresh",
        "AlertType": "Daily Refresh",
        "StockSymbol": "SYSTEM",
        "Category": "Other",
        "SourceGroup": "System",
        "Date": run_time_ist.strftime("%Y-%m-%d"),
        "Time": run_time_ist.strftime("%H:%M:%S"),
        "TimestampUtc": run_time_utc.isoformat(),
        "Status": "New",
        "Count": 1,
        "Notes": "Auto refresh signal at IST midnight",
        "IsDailyRefresh": True,
        "Timezone": IST_TIMEZONE,
    }


def send_daily_refresh_signal() -> bool:
    """Send an IST-midnight refresh signal to the automation webhook."""
    if not DAILY_REFRESH_WEBHOOK_URL:
        logger.warning("Skipping daily refresh signal: DAILY_REFRESH_WEBHOOK_URL is not set.")
        return False

    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SHARED_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SHARED_SECRET

    run_time_utc = datetime.now(timezone.utc)
    payload = _build_daily_refresh_payload(run_time_utc)
    payload_json = json.dumps(payload)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                DAILY_REFRESH_WEBHOOK_URL,
                data=payload_json,
                headers=headers,
                timeout=15,
                verify=False,
            )
            if response.status_code in (200, 201, 202):
                logger.info(
                    "Sent IST daily refresh signal successfully (attempt %d)",
                    attempt,
                )
                return True

            logger.warning(
                "Daily refresh webhook status %d on attempt %d: %s",
                response.status_code,
                attempt,
                response.text[:300],
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Daily refresh request failed on attempt %d: %s", attempt, exc)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    logger.error("Failed to deliver IST daily refresh signal after %d attempts", MAX_RETRIES)
    return False


async def run_daily_ist_refresh_scheduler() -> None:
    """Background scheduler that fires once per day at IST midnight."""
    while True:
        seconds_to_wait = max(1.0, _seconds_until_next_ist_midnight())
        next_run = datetime.now(timezone.utc) + timedelta(seconds=seconds_to_wait)
        logger.info(
            "Next IST daily refresh scheduled at %s",
            next_run.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        await asyncio.sleep(seconds_to_wait)
        send_daily_refresh_signal()


# ---------------------------------------------------------------------------
# Telethon client and event handler
# ---------------------------------------------------------------------------

# Client will be instantiated inside main() to avoid event loop issues
client = None


async def handle_new_message(event):
    try:
        raw_text = event.message.message
        message_time = event.message.date  # timezone-aware UTC datetime from Telegram

        alerts = parse_message(raw_text, message_time)

        if not alerts:
            logger.debug("No parseable alerts in message id %s", event.message.id)
            return

        for alert in alerts:
            success = send_to_power_automate(alert)
            if not success:
                # Persist failed alerts locally so nothing is silently lost.
                _save_failed_alert(alert)

    except Exception:
        logger.exception("Unhandled error while processing message id %s", event.message.id)


def _save_failed_alert(alert_record: dict) -> None:
    """Append undelivered alerts to a local fallback file for later replay."""
    fallback_path = os.path.join(LOG_DIR, "failed_alerts.jsonl")
    try:
        with open(fallback_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert_record) + "\n")
        logger.info("Saved failed alert to fallback file: %s", fallback_path)
    except OSError:
        logger.exception("Could not write to fallback file either.")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def main():
    global client
    
    # Create client inside async context where event loop exists
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    # Register event handler for one target group/channel
    client.on(events.NewMessage(chats=TARGET_GROUP))(handle_new_message)
    
    logger.info("Starting Telegram stock alert listener...")
    logger.info("Listening on target group: %s", TARGET_GROUP)
    await client.start(phone=PHONE_NUMBER)

    if not await client.is_user_authorized():
        # First-run login flow. On subsequent runs the session file handles auth.
        try:
            await client.send_code_request(PHONE_NUMBER)
            code = input("Enter the Telegram login code sent to your device: ")
            await client.sign_in(PHONE_NUMBER, code)
        except SessionPasswordNeededError:
            password = input("Two-factor authentication enabled. Enter your password: ")
            await client.sign_in(password=password)

    logger.info("Authenticated successfully. Listening on target group: %s", TARGET_GROUP)
    daily_refresh_task = None
    if DAILY_REFRESH_ENABLED:
        daily_refresh_task = asyncio.create_task(run_daily_ist_refresh_scheduler())
        logger.info("IST daily refresh scheduler enabled (12:00 AM %s).", IST_TIMEZONE)
    else:
        logger.info("IST daily refresh scheduler disabled.")

    try:
        await client.run_until_disconnected()
    finally:
        if daily_refresh_task:
            daily_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await daily_refresh_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Listener stopped manually.")
    except Exception:
        logger.exception("Fatal error in listener. Exiting.")
