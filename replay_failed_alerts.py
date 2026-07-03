import json
import os
import shutil
import time
from datetime import datetime, timezone

import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

POWER_AUTOMATE_URL = os.environ.get("POWER_AUTOMATE_URL")
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
MAX_RETRIES = int(os.environ.get("WEBHOOK_MAX_RETRIES", 3))
RETRY_BACKOFF_SECONDS = int(os.environ.get("WEBHOOK_RETRY_BACKOFF", 2))
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
FAILED_ALERTS_PATH = os.path.join(LOG_DIR, "failed_alerts.jsonl")
BACKUP_PATH = os.path.join(LOG_DIR, "failed_alerts.backup.jsonl")


def normalize_record(record: dict) -> dict:
    alert_type = record.get("AlertType", "Unknown Alert")
    stock_symbol = record.get("StockSymbol", "UNKNOWN")

    if not record.get("Title"):
        record["Title"] = f"{stock_symbol} | {alert_type}"

    if record.get("RSI") is None:
        record["RSI"] = 0.0

    if not record.get("Action"):
        record["Action"] = "No Data"

    if "IsDailyRefresh" not in record:
        record["IsDailyRefresh"] = False

    if not record.get("TimestampUtc"):
        record["TimestampUtc"] = datetime.now(timezone.utc).isoformat()

    return record



def send_record(record: dict) -> bool:
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SHARED_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SHARED_SECRET

    payload = json.dumps(record)

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
                print(
                    f"Replay success: {record['StockSymbol']} | {record['AlertType']} (attempt {attempt})"
                )
                return True

            print(
                f"Replay failed with status {response.status_code} on attempt {attempt}: "
                f"{record['StockSymbol']} | {record['AlertType']} | {response.text[:300]}"
            )
        except requests.exceptions.RequestException as exc:
            print(
                f"Replay request error on attempt {attempt}: "
                f"{record['StockSymbol']} | {record['AlertType']} | {exc}"
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return False



def main() -> None:
    if not POWER_AUTOMATE_URL:
        raise SystemExit("Missing POWER_AUTOMATE_URL environment variable.")

    if not os.path.exists(FAILED_ALERTS_PATH):
        raise SystemExit(f"No failed alerts file found at {FAILED_ALERTS_PATH}")

    with open(FAILED_ALERTS_PATH, "r", encoding="utf-8") as handle:
        raw_lines = [line.strip() for line in handle if line.strip()]

    if not raw_lines:
        print("No failed alerts to replay.")
        return

    print(f"Loaded {len(raw_lines)} failed alerts from {FAILED_ALERTS_PATH}")

    remaining_records = []
    successful_count = 0

    for raw_line in raw_lines:
        try:
            record = normalize_record(json.loads(raw_line))
        except json.JSONDecodeError as exc:
            print(f"Skipping invalid JSON line: {exc}")
            continue

        if send_record(record):
            successful_count += 1
        else:
            remaining_records.append(record)

    shutil.copyfile(FAILED_ALERTS_PATH, BACKUP_PATH)

    with open(FAILED_ALERTS_PATH, "w", encoding="utf-8") as handle:
        for record in remaining_records:
            handle.write(json.dumps(record) + "\n")

    print(
        f"Replay complete. Success={successful_count}, Remaining={len(remaining_records)}, "
        f"Backup={BACKUP_PATH}"
    )


if __name__ == "__main__":
    main()
