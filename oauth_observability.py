"""Redacted WHOOP OAuth reliability state for production reports."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import whoop_auth

TZ = ZoneInfo("Europe/Kyiv")
POLL_MINUTES = {7, 22, 37, 52}

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _last_successful_refresh():
    path = Path(whoop_auth.audit_file_path())
    if not path.exists():
        return None
    latest = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if event.get("result_category") in {
            "rotated", "installed_single_owner",
        }:
            latest = event.get("timestamp")
    return latest


def _next_scheduled_poll(now):
    local = now.astimezone(TZ)
    candidate = local.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(24 * 60 + 1):
        if 6 <= candidate.hour <= 23 and candidate.minute in POLL_MINUTES:
            return candidate.astimezone(dt.timezone.utc).isoformat(
                timespec="seconds"
            )
        candidate += dt.timedelta(minutes=1)
    raise RuntimeError("next_whoop_poll_not_found")


def snapshot(now=None):
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    now_utc = now_utc.astimezone(dt.timezone.utc)
    tokens = whoop_auth.load_tokens() or {}
    refresh = tokens.get("refresh_token")
    refresh_fp = whoop_auth.token_fingerprint(refresh)
    quarantine = _read_json(whoop_auth.AMBIGUOUS_FILE)
    runtime = _read_json(whoop_auth.RUNTIME_STATE_FILE)
    try:
        expires_at_epoch = (
            float(tokens["obtained_at"]) + float(tokens["expires_in"])
        )
        expires_at = dt.datetime.fromtimestamp(
            expires_at_epoch, dt.timezone.utc,
        )
        remaining = round((expires_at - now_utc).total_seconds())
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        expires_at = None
        remaining = None
    quarantine_active = Path(whoop_auth.AMBIGUOUS_FILE).exists()
    fingerprint_matches = bool(
        refresh_fp
        and quarantine.get("token_fingerprint") == refresh_fp
    )
    authorization_required = (
        not tokens.get("access_token")
        or not refresh
        or (quarantine_active and fingerprint_matches)
    )
    # WHOOP documents that using a refresh token invalidates the prior access
    # token. An ambiguous refresh therefore cannot prove that access remains
    # valid, even if its local expiry has not passed.
    ingestion_can_continue = bool(
        not quarantine_active
        and tokens.get("access_token")
        and remaining is not None
        and remaining > 0
    )
    return {
        "access_token_present": bool(tokens.get("access_token")),
        "access_token_expiry": (
            expires_at.isoformat(timespec="seconds") if expires_at else None
        ),
        "access_token_seconds_remaining": remaining,
        "refresh_token_present": bool(refresh),
        "refresh_token_fingerprint_short": (
            refresh_fp[:8] if refresh_fp else None
        ),
        "quarantine_active": quarantine_active,
        "quarantine_timestamp": quarantine.get("recorded_at"),
        "quarantine_reason": quarantine.get("category"),
        "quarantine_fingerprint_matches": fingerprint_matches,
        "last_successful_refresh": _last_successful_refresh(),
        "last_successful_whoop_api_call": runtime.get(
            "last_successful_whoop_api_call"
        ),
        "last_successful_import": runtime.get("last_successful_import"),
        "authorization_required": authorization_required,
        "next_scheduled_cron": _next_scheduled_poll(now_utc),
        "ingestion_can_continue_using_current_access_token":
            ingestion_can_continue,
    }


def render(now=None):
    state = snapshot(now)
    return "\n".join(
        f"{key}: {value if value is not None else 'unknown'}"
        for key, value in state.items()
    )
