"""Single-owner WHOOP token installation over the pinned production SSH channel."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import uuid

from dotenv import load_dotenv

import whoop_auth


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def secure_remove(path):
    """Best-effort overwrite plus unlink; new handoffs never create a local token file."""
    target = Path(path)
    if not target.exists():
        return
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("refusing_to_remove_non_regular_token_artifact")
    size = target.stat().st_size
    try:
        with target.open("r+b", buffering=0) as handle:
            remaining = size
            block = b"\0" * min(max(size, 1), 65536)
            while remaining:
                chunk = block[:min(len(block), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        target.unlink(missing_ok=True)


def _write_transfer_marker(fingerprint, production_host):
    marker = Path(whoop_auth.transferred_marker_path())
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "transferred_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "refresh_token_fingerprint": fingerprint,
        "production_host": production_host,
        "local_use_forbidden": True,
    }
    temp = marker.with_name(marker.name + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, marker)


def finalize_workstation_handoff(fingerprint, production_host):
    """Permanently disable the workstation copy after remote verification."""
    _write_transfer_marker(fingerprint, production_host)
    secure_remove(whoop_auth.TOKENS_FILE)
    if Path(whoop_auth.TOKENS_FILE).exists():
        raise RuntimeError("workstation_token_copy_remains")


def _validated_envelope(tokens, *, client_id, redirect_uri):
    validated = whoop_auth.validate_authorization_tokens(
        tokens,
        authorization_client_id=client_id,
        authorization_redirect_uri=redirect_uri,
        expected_client_id=os.environ.get("WHOOP_CLIENT_ID", ""),
        expected_redirect_uri=os.environ.get("WHOOP_REDIRECT_URI", ""),
    )
    return {
        "tokens": validated,
        "authorization_context": {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        },
    }


def accept_uploaded_token(upload_path, service_user="whoop"):
    """Production-side atomic install. Must run through the privileged SSH account."""
    upload = Path(upload_path).resolve()
    remote_root = HERE.resolve()
    if upload.parent != remote_root or not upload.name.startswith(".tokens-upload-"):
        raise RuntimeError("invalid_token_upload_path")
    try:
        envelope = json.loads(upload.read_text(encoding="utf-8"))
        context = envelope.get("authorization_context") or {}
        tokens = whoop_auth.validate_authorization_tokens(
            envelope.get("tokens"),
            authorization_client_id=context.get("client_id"),
            authorization_redirect_uri=context.get("redirect_uri"),
            expected_client_id=os.environ.get("WHOOP_CLIENT_ID", ""),
            expected_redirect_uri=os.environ.get("WHOOP_REDIRECT_URI", ""),
        )
        new_refresh = tokens["refresh_token"]
        with whoop_auth._refresh_lock():
            previous = whoop_auth.load_tokens() or {}
            old_refresh = previous.get("refresh_token")
            persisted_tokens = whoop_auth.save_tokens(tokens)
            try:
                import pwd
                account = pwd.getpwnam(service_user)
                for path in (
                    whoop_auth.TOKENS_FILE,
                    whoop_auth.LOCK_FILE,
                    whoop_auth.audit_file_path(),
                ):
                    if os.path.exists(path):
                        os.chown(path, account.pw_uid, account.pw_gid)
                        os.chmod(path, 0o600)
            except (ImportError, KeyError):
                if os.name != "nt":
                    raise
            marker = whoop_auth.transferred_marker_path()
            if os.path.exists(marker):
                os.unlink(marker)
            # A fresh authorization supersedes the old quarantine only after
            # the replacement token pair has been durably committed.
            whoop_auth._clear_ambiguous_refresh()
            try:
                import oauth_alerting
                oauth_alerting.clear_alert_state()
            except OSError:
                pass
            whoop_auth.record_rotation_audit(
                result_category="installed_single_owner",
                token=persisted_tokens,
                old_token=old_refresh,
                new_token=new_refresh,
            )
            if os.name != "nt":
                import pwd
                account = pwd.getpwnam(service_user)
                os.chown(whoop_auth.audit_file_path(), account.pw_uid, account.pw_gid)
                os.chmod(whoop_auth.audit_file_path(), 0o600)
        persisted = whoop_auth.load_tokens()
        fingerprint = whoop_auth.short_token_fingerprint(
            persisted.get("refresh_token"),
        )
        if fingerprint != whoop_auth.short_token_fingerprint(new_refresh):
            raise RuntimeError("production_token_verification_failed")
        return {"ok": True, "refresh_token_fingerprint_short": fingerprint}
    finally:
        secure_remove(upload)


def install_direct_to_production(tokens, *, client_id, redirect_uri):
    """Upload only in memory over SFTP, install under lock, then disable local use."""
    envelope = _validated_envelope(tokens, client_id=client_id, redirect_uri=redirect_uri)
    expected_fp = whoop_auth.short_token_fingerprint(
        envelope["tokens"]["refresh_token"],
    )
    import deploy

    client = deploy.connect()
    remote_temp = f"{deploy.REMOTE_DIR}/.tokens-upload-{uuid.uuid4().hex}.json"
    try:
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_temp, "wb") as target:
                target.write(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
                target.flush()
            sftp.chmod(remote_temp, 0o600)
        finally:
            sftp.close()
        command = (
            f"cd {shlex.quote(deploy.REMOTE_DIR)} && "
            f"{shlex.quote(deploy.REMOTE_DIR + '/venv/bin/python')} token_handoff.py "
            f"--accept-upload {shlex.quote(remote_temp)} "
            f"--service-user {shlex.quote(deploy.SERVICE_USER)}"
        )
        result = json.loads(deploy.run(client, command).splitlines()[-1])
        if (
            not result.get("ok")
            or result.get("refresh_token_fingerprint_short") != expected_fp
        ):
            raise RuntimeError("production_token_installation_failed")
        verify_command = (
            f"cd {shlex.quote(deploy.REMOTE_DIR)} && "
            f"runuser -u {shlex.quote(deploy.SERVICE_USER)} -- "
            f"{shlex.quote(deploy.REMOTE_DIR + '/venv/bin/python')} -c "
            + shlex.quote(
                "import json,whoop_auth; d=whoop_auth.load_tokens(); "
                "print(json.dumps({'readable':bool(d and d.get('access_token') and d.get('refresh_token')),"
                "'fingerprint_short':whoop_auth.short_token_fingerprint(d.get('refresh_token'))}))"
            )
        )
        verified = json.loads(deploy.run(client, verify_command).splitlines()[-1])
        if (
            not verified.get("readable")
            or verified.get("fingerprint_short") != expected_fp
        ):
            raise RuntimeError("production_service_user_cannot_read_token")
    finally:
        try:
            deploy.run(client, f"rm -f -- {shlex.quote(remote_temp)}")
        finally:
            client.close()

    finalize_workstation_handoff(expected_fp, deploy.VPS_HOST)
    return {"ok": True, "refresh_token_fingerprint_short": expected_fp}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept-upload")
    parser.add_argument("--service-user", default="whoop")
    args = parser.parse_args()
    if not args.accept_upload:
        raise SystemExit("--accept-upload is required")
    print(json.dumps(accept_uploaded_token(args.accept_upload, args.service_user)))


if __name__ == "__main__":
    main()
