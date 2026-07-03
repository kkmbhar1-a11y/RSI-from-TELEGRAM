# Stock Alert Automation System

Reads stock alerts from a Telegram group you're a member of (no admin rights
needed), parses them, and pushes structured data into SharePoint via Power
Automate for Power BI reporting.

## Why this works without bot/admin access

A Telegram Bot account can only see messages in a group if it's added as a
member, which on most groups requires admin approval. Telethon instead logs
in as **your own user account** using your phone number and Telegram's API
credentials. Since you're already a member of the group, your account can
read every message in it — no admin action required.

## Getting your Telegram API credentials

1. Go to https://my.telegram.org/apps while logged into your Telegram
   account in a browser.
2. Create a new application (any name/description works — this is just for
   API access, not a public bot).
3. Copy the `api_id` and `api_hash` values into your `.env` file.

## Setup

```bash
cd stock_alert_system
cp config.env.example .env
# edit .env with your real values
pip install -r requirements.txt
python telegram_listener.py
```

On first run, you'll be prompted for the login code Telegram sends to your
account, and your 2FA password if enabled. After that, a `.session` file is
saved and you won't be prompted again unless you log out or move servers.

## Daily IST auto-refresh at 12:00 AM

The listener now includes a built-in daily scheduler that sends a refresh
signal exactly at IST midnight (12:00 AM), even if no Telegram alert arrives.

Environment variables:
- `DAILY_REFRESH_ENABLED` (default: `true`) — turn midnight refresh signal on/off
- `IST_TIMEZONE` (default: `Asia/Kolkata`) — timezone used for the daily trigger
- `DAILY_REFRESH_WEBHOOK_URL` (default: `POWER_AUTOMATE_URL`) — webhook that receives the daily refresh signal

The payload includes:
- `AlertType`: `Daily Refresh`
- `StockSymbol`: `SYSTEM`
- `IsDailyRefresh`: `true`

In Power Automate, add a condition on `IsDailyRefresh` or `AlertType` to run
your daily refresh branch (for example, trigger only Power BI refresh and
skip normal stock-alert upsert logic).

## Finding your group's identifier

`TG_GROUP_ID` can be:
- `@groupusername` if the group has a public username
- The numeric chat ID, which you can get by running this snippet once:

```python
from telethon.sync import TelegramClient
with TelegramClient("temp_session", API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        print(dialog.id, dialog.name)
```

Find your group's name in the printed list and use the corresponding ID.

## Files in this package

| File | Purpose |
|---|---|
| `telegram_listener.py` | Main script: listens, parses, sends to Power Automate |
| `config.env.example` | Template for required environment variables |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container build for Azure/VPS deployment |
| `AZURE_DEPLOYMENT.md` | Azure-specific deployment guidance and constraints |
| `POWER_AUTOMATE_SETUP.md` | Full flow design: SharePoint schema, dedup logic, Power BI refresh |
| `POWER_BI_SETUP.md` | Dashboard visuals and layout guidance |
| `run_task_scheduler.bat` | Windows Task Scheduler wrapper script |

## Deployment options

### 1. Run locally
Simplest for testing. Just run `python telegram_listener.py` in a terminal
and leave it running. Stops when your machine sleeps or the terminal closes.

### 2. Run on a VPS (recommended for reliability)
Any small Linux VPS (e.g., a $5–6/month instance) works well, since this
script is lightweight and mostly idle between messages.

```bash
# On the VPS, after cloning/copying this folder:
pip install -r requirements.txt
python telegram_listener.py   # complete the login prompt once

# Then run it persistently with a process manager:
pip install supervisor   # or use systemd, shown below
```

**Recommended: systemd service** (`/etc/systemd/system/stockalerts.service`):

```ini
[Unit]
Description=Stock Alert Telegram Listener
After=network.target

[Service]
WorkingDirectory=/home/youruser/stock_alert_system
ExecStart=/usr/bin/python3 /home/youruser/stock_alert_system/telegram_listener.py
Restart=always
RestartSec=10
EnvironmentFile=/home/youruser/stock_alert_system/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable stockalerts
sudo systemctl start stockalerts
sudo journalctl -u stockalerts -f   # watch logs
```

### 3. Run on Azure
See `AZURE_DEPLOYMENT.md` — important note: Azure Functions on the
Consumption plan is not suitable for an always-listening process. Use
Container Apps or an Always-On App Service instead, both covered in that
file with exact deployment commands.

### 4. Run on Windows Task Scheduler
Task Scheduler is built for scheduled, finite tasks rather than a process
that runs forever, so the practical pattern is: have Task Scheduler start
the listener at system startup and rely on its "Restart if the task fails"
setting to bring it back up if it ever crashes.

Use the included `run_task_scheduler.bat`, then in Task Scheduler:
1. Create Task (not Basic Task, so you get full options)
2. Trigger: "At startup" (or "At log on" if it needs your user session)
3. Action: Start a program → point to `run_task_scheduler.bat`
4. Settings tab: check "Restart the task every 1 minute" with a high retry
   count, and check "Run task as soon as possible after a scheduled start is
   missed"

## Logging and error handling

- All activity is logged to `./logs/stock_alerts.log` (rotating, 5MB x 5
  backups) and to the console.
- Webhook delivery retries up to `WEBHOOK_MAX_RETRIES` times with increasing
  backoff before giving up.
- Any alert that fails delivery after all retries is appended to
  `./logs/failed_alerts.jsonl` so nothing is silently lost — you can write a
  small replay script to re-POST these later if needed.

To replay saved failures after fixing schema or RSI issues:

```bash
python replay_failed_alerts.py
```

This script:
- reloads `./logs/failed_alerts.jsonl`
- fills missing `Title`, `RSI`, and `Action` fields for older records
- retries each alert against Power Automate
- keeps only still-failing alerts in `./logs/failed_alerts.jsonl`
- writes a backup copy to `./logs/failed_alerts.backup.jsonl`

To backfill RSI/Action for all existing rows in an Excel file:

```bash
python backfill_excel_rsi.py --file "C:\\path\\to\\StockAlerts.xlsx" --sheet "StockAlerts"
```

This script:
- creates a backup `*.bak` file before editing
- recalculates RSI and Action for every row using the same provider logic
- writes updated values back to the worksheet in place
- Unhandled exceptions in message processing are caught per-message, so one
  malformed message can't crash the whole listener.

## Security notes

- Treat your `.env` file like a password — it grants access to your personal
  Telegram account's session and your Power Automate flow.
- The `WEBHOOK_SHARED_SECRET` is a basic safeguard against someone else
  finding your Power Automate URL and injecting fake records; for stronger
  protection, consider Azure API Management or an Azure Function acting as a
  validated proxy in front of the flow.
- Never commit `.env` or the `.session` file to a public repository.
