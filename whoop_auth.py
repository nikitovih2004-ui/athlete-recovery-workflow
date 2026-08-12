"""Safe WHOOP OAuth token storage and refresh."""
from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import hashlib
import json
import os
import socket
import sys
import tempfile
import time

import requests

TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
DEFAULT_TOKENS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tokens.json",
)
TOKENS_FILE = DEFAULT_TOKENS_FILE
LOCK_FILE = TOKENS_FILE + ".lock"
AMBIGUOUS_FILE = TOKENS_FILE + ".refresh-ambiguous"
RUNTIME_STATE_FILE = os.path.join(
    os.path.dirname(TOKENS_FILE), "oauth_runtime_state.json",
)
DEFAULT_REFRESH_MARGIN_SECONDS = 60


class WhoopAuthError(RuntimeError):
    """Safe OAuth error whose text never contains credentials or tokens."""

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        suffix = f" http_status={status_code}" if status_code is not None else ""
        super().__init__(f"WHOOP OAuth failed category={category}{suffix}")


@contextlib.contextmanager
def _refresh_lock():
    """Serialize rotating refresh-token use across cron and bot processes."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    handle = open(LOCK_FILE, "a+b")
    try:
        try:
            os.chmod(LOCK_FILE, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return None
    with open(TOKENS_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def token_fingerprint(value):
    """Return a non-secret identity for one rotating token."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_token_fingerprint(value, length=12):
    fingerprint = token_fingerprint(value)
    return fingerprint[:int(length)] if fingerprint else None


def transferred_marker_path():
    override = os.environ.get("WHOOP_TRANSFER_MARKER", "").strip()
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.dirname(TOKENS_FILE)
        return os.path.join(base, "whoop-workouts", "token.transferred")
    return TOKENS_FILE + ".transferred"


def audit_file_path():
    return os.path.join(os.path.dirname(TOKENS_FILE), "token_rotation_audit.jsonl")


def _identity():
    return {"host": socket.gethostname(), "owner": getpass.getuser()}


def _audit_actor():
    if os.environ.get("MORNING_PIPELINE_RUN_ID"):
        return "morning_pipeline"
    if os.environ.get("INVOCATION_ID"):
        return "systemd"
    return "manual_or_handoff"


def _access_token_expires_at(token):
    if not isinstance(token, dict):
        return None
    try:
        expires_at = float(token["obtained_at"]) + float(token["expires_in"])
    except (KeyError, TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(
        expires_at, tz=dt.timezone.utc,
    ).isoformat(timespec="seconds")


def record_rotation_audit(*, result_category, token=None, old_token=None,
                          new_token=None, http_status=None):
    """Append one credential-free, fsynced lifecycle event."""
    event = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **_identity(),
        "result_category": str(result_category),
        "pid": os.getpid(),
        "process": os.path.basename(sys.argv[0]) if sys.argv else "",
        "actor": _audit_actor(),
        "pipeline_run_id": os.environ.get("MORNING_PIPELINE_RUN_ID") or None,
        "token_store": os.path.abspath(TOKENS_FILE),
        "refresh_lock": os.path.abspath(LOCK_FILE),
        "refresh_lock_held": True,
        "token_fingerprint_short": short_token_fingerprint(token),
        "old_token_fingerprint_short": short_token_fingerprint(old_token),
        "new_token_fingerprint_short": short_token_fingerprint(new_token),
        "http_status": int(http_status) if http_status is not None else None,
        "access_token_expires_at": _access_token_expires_at(token),
        "token_store_write_confirmed": result_category in {
            "rotated", "installed_single_owner",
        },
    }
    path = audit_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
    finally:
        os.close(fd)


def validate_authorization_tokens(tokens, *, authorization_client_id,
                                  authorization_redirect_uri, expected_client_id,
                                  expected_redirect_uri):
    """Fail closed unless the OAuth response and bound request are complete."""
    if not isinstance(tokens, dict):
        raise WhoopAuthError("malformed_token_response")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    scopes = set(str(tokens.get("scope") or "").split())
    if not isinstance(access, str) or not access:
        raise WhoopAuthError("access_token_missing")
    if not isinstance(refresh, str) or not refresh:
        raise WhoopAuthError("refresh_token_missing")
    if "offline" not in scopes:
        raise WhoopAuthError("offline_scope_missing")
    if not expected_client_id or authorization_client_id != expected_client_id:
        raise WhoopAuthError("client_id_mismatch")
    if not expected_redirect_uri or authorization_redirect_uri != expected_redirect_uri:
        raise WhoopAuthError("redirect_uri_mismatch")
    return dict(tokens)


def save_tokens(tokens: dict):
    """Atomically persist rotated tokens with owner-only permissions."""
    payload = dict(tokens)
    payload["obtained_at"] = time.time()
    directory = os.path.dirname(TOKENS_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tokens-", suffix=".json", dir=directory)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, TOKENS_FILE)
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except OSError:
            pass
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return payload


def _fsync_directory(path):
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def record_runtime_fact(name, when=None):
    if name not in {
        "last_successful_whoop_api_call",
        "last_successful_import",
    }:
        raise ValueError("unsupported OAuth runtime fact")
    payload = {}
    if os.path.exists(RUNTIME_STATE_FILE):
        try:
            with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            payload = {}
    timestamp = when or dt.datetime.now(dt.timezone.utc)
    payload[name] = timestamp.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds"
    )
    directory = os.path.dirname(RUNTIME_STATE_FILE)
    fd, temp_path = tempfile.mkstemp(
        prefix=".oauth-runtime-", suffix=".json", dir=directory,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, RUNTIME_STATE_FILE)
        _fsync_directory(directory)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _write_ambiguous_refresh(refresh_token, category):
    """Durably prevent a rotating token with an unknown outcome being replayed."""
    payload = {
        "token_fingerprint": token_fingerprint(refresh_token),
        "category": str(category),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    directory = os.path.dirname(AMBIGUOUS_FILE)
    fd, temp_path = tempfile.mkstemp(
        prefix=".refresh-ambiguous-", suffix=".json", dir=directory,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, AMBIGUOUS_FILE)
        _fsync_directory(directory)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _clear_ambiguous_refresh():
    if not os.path.exists(AMBIGUOUS_FILE):
        return
    os.unlink(AMBIGUOUS_FILE)
    _fsync_directory(os.path.dirname(AMBIGUOUS_FILE))


def _notify_authorization_required(refresh_token, category):
    """Best-effort owner alert; OAuth safety never depends on Telegram."""
    if (
        os.path.abspath(TOKENS_FILE) != os.path.abspath(DEFAULT_TOKENS_FILE)
        and os.environ.get("WHOOP_OAUTH_ALERTS_TEST") != "1"
    ):
        return
    try:
        import oauth_alerting
        oauth_alerting.notify_authorization_required(
            reason=str(category),
            refresh_fingerprint=token_fingerprint(refresh_token),
        )
    except Exception:
        # The durable quarantine remains authoritative. A later cron run will
        # retry the deduplicated alert without retrying the refresh token.
        pass


def _record_audit_best_effort(**kwargs):
    try:
        record_rotation_audit(**kwargs)
    except OSError:
        pass


def _reject_ambiguous_replay(refresh_token):
    if not os.path.exists(AMBIGUOUS_FILE):
        return
    try:
        with open(AMBIGUOUS_FILE, "r", encoding="utf-8") as handle:
            fingerprint = json.load(handle).get("token_fingerprint")
    except (OSError, ValueError, AttributeError):
        _notify_authorization_required(refresh_token, "refresh_outcome_ambiguous")
        raise WhoopAuthError("refresh_outcome_ambiguous")
    if fingerprint == token_fingerprint(refresh_token):
        _notify_authorization_required(
            refresh_token, "refresh_outcome_ambiguous",
        )
        raise WhoopAuthError("refresh_outcome_ambiguous")
    # A newly installed/rotated credential supersedes an old quarantine marker.
    _clear_ambiguous_refresh()


def _is_current(tokens):
    required = tokens and tokens.get("access_token") and tokens.get("obtained_at")
    if not required:
        return False
    try:
        margin = int(os.environ.get(
            "WHOOP_REFRESH_MARGIN_SECONDS",
            str(DEFAULT_REFRESH_MARGIN_SECONDS),
        ))
        if margin < 0 or margin > 900:
            return False
        expires_in = float(tokens["expires_in"])
        obtained_at = float(tokens["obtained_at"])
        if expires_in <= 0:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return time.time() < obtained_at + expires_in - margin


def _safe_error_category(response):
    try:
        code = response.json().get("error")
    except (ValueError, AttributeError):
        code = None
    if code in {"invalid_grant", "invalid_request", "invalid_client", "unauthorized_client"}:
        return code
    return "oauth_client_error" if 400 <= response.status_code < 500 else "oauth_server_error"


def get_valid_access_token(
    client_id: str,
    client_secret: str,
    *,
    force_refresh=False,
    return_metadata=False,
):
    """Return an access token, refreshing a rotating token exactly once.

    ``return_metadata`` is used by production observability.  It exposes only
    non-secret lifecycle facts and never token values or fingerprints.
    """
    def result(access_token, refresh_performed, reason):
        if return_metadata:
            return {
                "access_token": access_token,
                "refresh_performed": bool(refresh_performed),
                "reason": str(reason),
            }
        return access_token

    with _refresh_lock():
        if os.path.exists(transferred_marker_path()):
            raise WhoopAuthError("token_transferred_to_production")
        # Reload after acquiring the inter-process lock. Another process may
        # have already rotated and atomically persisted the refresh token.
        tokens = load_tokens()
        if tokens is None:
            raise WhoopAuthError("tokens_missing")
        if _is_current(tokens) and not force_refresh:
            return result(
                tokens["access_token"], False, "access_token_current",
            )
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise WhoopAuthError("refresh_token_missing")
        _reject_ambiguous_replay(refresh_token)

        # Persist an in-flight marker before the request. If this process dies
        # after WHOOP receives or answers the request but before tokens.json is
        # replaced, the next process must not replay the rotating credential.
        _write_ambiguous_refresh(refresh_token, "refresh_inflight")
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "offline",
                },
                timeout=30,
            )
        except requests.exceptions.ConnectTimeout as exc:
            # Requests explicitly documents ConnectTimeout as safe to retry:
            # the request did not reach the remote server.
            _clear_ambiguous_refresh()
            _record_audit_best_effort(
                result_category="connect_timeout_request_not_sent",
                token=refresh_token,
            )
            raise WhoopAuthError("connect_timeout_request_not_sent") from exc
        except requests.exceptions.RequestException as exc:
            _write_ambiguous_refresh(refresh_token, "network_error")
            _record_audit_best_effort(
                result_category="network_error", token=refresh_token,
            )
            _notify_authorization_required(refresh_token, "network_error")
            raise WhoopAuthError("network_error") from exc

        if response.ok:
            try:
                new_tokens = response.json()
                access_token = new_tokens["access_token"]
                new_refresh_token = new_tokens["refresh_token"]
                if "offline" not in set(str(new_tokens.get("scope") or "").split()):
                    raise KeyError("offline_scope")
            except (ValueError, KeyError, TypeError) as exc:
                _write_ambiguous_refresh(
                    refresh_token, "malformed_token_response",
                )
                _record_audit_best_effort(
                    result_category="malformed_token_response",
                    token=refresh_token, http_status=response.status_code,
                )
                _notify_authorization_required(
                    refresh_token, "malformed_token_response",
                )
                raise WhoopAuthError("malformed_token_response", response.status_code) from exc
            try:
                persisted_tokens = save_tokens(new_tokens)
            except OSError as exc:
                _write_ambiguous_refresh(
                    refresh_token, "token_persistence_failed",
                )
                _record_audit_best_effort(
                    result_category="token_persistence_failed",
                    token=refresh_token, http_status=response.status_code,
                )
                _notify_authorization_required(
                    refresh_token, "token_persistence_failed",
                )
                raise WhoopAuthError(
                    "token_persistence_failed", response.status_code,
                ) from exc
            _record_audit_best_effort(
                result_category="rotated", old_token=refresh_token,
                token=persisted_tokens,
                new_token=new_refresh_token,
                http_status=response.status_code,
            )
            # tokens.json is already durable. If clearing fails, the old
            # fingerprint cannot block the newly persisted credential.
            _clear_ambiguous_refresh()
            return result(access_token, True, "token_rotated")

        category = _safe_error_category(response)
        # A rotating credential rejected with 4xx is terminal for this token;
        # a 5xx is ambiguous. Neither may be replayed automatically.
        _write_ambiguous_refresh(refresh_token, category)
        _record_audit_best_effort(
            result_category=category, token=refresh_token,
            http_status=response.status_code,
        )
        _notify_authorization_required(refresh_token, category)
        raise WhoopAuthError(category, response.status_code)
