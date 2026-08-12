"""Send the daily Telegram reminder from cron without exposing credentials in crontab."""
from __future__ import annotations

import argparse
import os

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate configuration without sending a message")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured.")
    if args.dry_run:
        print("Telegram reminder configuration: OK")
        return

    text = (
        "🤖 *Напоминание от Джарвиса:*\n"
        "Не забудь принять креатин. Если сегодня была тренировка, запиши её сообщением в бот."
    )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=20,
        )
        response.raise_for_status()
        confirmed = response.json().get("ok")
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f", HTTP {status}" if isinstance(status, int) else ""
        raise RuntimeError(
            f"Telegram reminder delivery failed ({type(exc).__name__}{suffix})."
        ) from None
    except (TypeError, ValueError):
        raise RuntimeError("Telegram reminder returned an invalid response.") from None
    if not confirmed:
        raise RuntimeError("Telegram API did not confirm reminder delivery.")
    print("Telegram reminder sent.")


if __name__ == "__main__":
    main()
