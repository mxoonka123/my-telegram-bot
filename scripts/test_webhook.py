import os
import json
import argparse
import logging
import time

from sqlalchemy.orm import Session

# We will lazy-import db only if DB access is required
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # sync is fine for a one-shot test

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_webhook")


def find_active_bot(db: Session):
    # Prefer the most recently configured webhook
    from db import BotInstance  # local import to avoid module when not needed
    q = (
        db.query(BotInstance)
        .filter(BotInstance.status == 'active')
        .order_by(BotInstance.last_webhook_set_at.desc().nullslast())
    )
    return q.first()


def main():
    parser = argparse.ArgumentParser(description="Send a test Telegram update to the multi-bot webhook")
    parser.add_argument("base_url", help="Base URL of your app, e.g. https://<app>.railway.app or http://localhost:8080")
    parser.add_argument("--text", default="/start test", help="Message text to send in the fake update")
    parser.add_argument("--chat-id", default="123456789", help="Chat ID to use in the fake update")
    parser.add_argument("--user-id", default="123456789", help="User ID to use in the fake update")
    parser.add_argument("--token", default=None, help="Bot token to target (bypass DB)")
    parser.add_argument("--secret", default=None, help="Webhook secret to use (bypass DB)")
    args = parser.parse_args()

    # If token/secret provided, skip DB entirely
    if args.token:
        bot_token = args.token
        webhook_secret = args.secret
        url = f"{args.base_url.rstrip('/')}/telegram/{bot_token}"
        headers = {"Content-Type": "application/json"}
        if webhook_secret:
            headers["X-Telegram-Bot-Api-Secret-Token"] = webhook_secret
        logger.info("Bypassing DB: using --token/--secret from CLI")
    else:
        # Use DB to fetch an active bot
        from db import initialize_database, get_db  # local import
        initialize_database()
        with get_db() as db:
            bot = find_active_bot(db)
            if not bot or not bot.bot_token:
                raise SystemExit("No active BotInstance with bot_token found. Bind a bot first or pass --token and --secret.")
            if not bot.webhook_secret:
                logger.warning("Bot has no webhook_secret saved; Telegram will still call without secret, but test will be rejected by app. Re-run bind to set webhook and save secret.")
            token_tail = bot.bot_token[-8:] if bot.bot_token else ""
            logger.info(f"Using bot @{bot.telegram_username} (id={bot.telegram_bot_id}, token=...{token_tail})")

            url = f"{args.base_url.rstrip('/')}/telegram/{bot.bot_token}"
            headers = {"Content-Type": "application/json"}
            if bot.webhook_secret:
                headers["X-Telegram-Bot-Api-Secret-Token"] = bot.webhook_secret

        update_payload = {
            "update_id": int(time.time()),
            "message": {
                "message_id": 1,
                "from": {
                    "id": int(args.user_id),
                    "is_bot": False,
                    "first_name": "test",
                    "username": "testuser"
                },
                "chat": {
                    "id": int(args.chat_id),
                    "type": "private",
                    "first_name": "test",
                    "username": "testuser"
                },
                "date": int(time.time()),
                "text": args.text
            }
        }

        logger.info(f"POST {url}")
        resp = requests.post(url, headers=headers, data=json.dumps(update_payload), timeout=20)
        logger.info(f"Response: {resp.status_code}\n{resp.text}")
        if resp.status_code == 200:
            print("OK: webhook accepted")
        else:
            print("FAILED: non-200 from webhook")


if __name__ == "__main__":
    main()
