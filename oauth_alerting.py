"""Credential-free, deduplicated owner alerts for WHOOP authorization loss."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile

import requests


HERE = Path(__file__).resolve().parent
ALERT_STATE_FILE = HERE / "oauth_alert_state.json"
AMBIGUOUS_REASONS = {
    "network_error",
    "oauth_server_error",
    "refresh_inflight",
    "refresh_outcome_ambiguous",
}


def _write_state(payload):
    ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".oauth-alert-", suffix=".json", dir=ALERT_STATE_FILE.parent,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, ALERT_STATE_FILE)
        os.chmod(ALERT_STATE_FILE, 0o600)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def clear_alert_state():
    try:
        ALERT_STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def notify_authorization_required(*, reason, refresh_fingerprint):
    """Send one alert per quarantined credential and return whether it sent."""
    fingerprint_key = (
        str(refresh_fingerprint)[:16] if refresh_fingerprint else None
    )
    if ALERT_STATE_FILE.exists():
        try:
            current = json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = {}
        if current.get("refresh_fingerprint_short") == fingerprint_key:
            return False

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return False
    if str(reason) in AMBIGUOUS_REASONS:
        message = (
            "WHOOP token refresh returned an ambiguous provider/network outcome "
            f"({reason}). The rotating refresh token is quarantined and will not "
            "be replayed. This does not prove that WHOOP consent was revoked. "
            "Operator recovery must use the hardened production token handoff."
        )
    else:
        message = (
            "WHOOP authorization is no longer usable "
            f"({reason}). Automatic refresh is stopped. Operator recovery must "
            "use the hardened production token handoff; do not run a legacy local auth.py."
        )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        # HTTPError may include response.url, which embeds the bot token.
        # Never propagate or log that exception.
        return False
    _write_state({
        "refresh_fingerprint_short": fingerprint_key,
        "reason": str(reason),
        "notified_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
    })
    return True
