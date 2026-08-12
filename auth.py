"""One-time WHOOP authorization with direct production handoff.

Run only from the current production-lineage checkout. The OAuth response is
validated and installed atomically in the single production token store; no
local token copy is retained. Set WHOOP_ENV_FILE to an existing environment
file when this checkout intentionally has no local .env.
"""
import os
from pathlib import Path
import secrets
import hmac
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

from whoop_auth import TOKEN_URL, validate_authorization_tokens
import token_handoff

ENV_FILE = os.environ.get("WHOOP_ENV_FILE", "").strip()
if ENV_FILE:
    env_path = Path(ENV_FILE).expanduser()
    if not env_path.is_file():
        raise RuntimeError("WHOOP_ENV_FILE does not point to a readable file")
    load_dotenv(env_path)
else:
    load_dotenv()

CLIENT_ID = os.environ["WHOOP_CLIENT_ID"]
CLIENT_SECRET = os.environ["WHOOP_CLIENT_SECRET"]
REDIRECT_URI = os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8080/callback")

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"

# offline -> нужен, чтобы получить refresh_token и не логиниться каждый час
SCOPES = "offline read:profile read:workout read:recovery read:sleep read:cycles read:body_measurement"

STATE = secrets.token_hex(4)  # ровно 8 символов, как требует WHOOP
_result = {}
_callback_consumed = False


def consume_authorization_callback(params):
    """Validate and consume exactly one state-bound callback."""
    global _callback_consumed
    received_state = params.get("state", [None])[0]
    if (
        not isinstance(received_state, str)
        or not hmac.compare_digest(received_state, STATE)
    ):
        raise ValueError("authorization_state_invalid")
    if _callback_consumed:
        raise ValueError("authorization_callback_already_consumed")
    if params.get("error"):
        raise ValueError("authorization_denied")
    code = params.get("code", [None])[0]
    if not isinstance(code, str) or not code:
        raise ValueError("authorization_code_missing")
    _callback_consumed = True
    return code


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)

        try:
            code = consume_authorization_callback(params)
        except ValueError as exc:
            if str(exc) == "authorization_denied":
                _result["error"] = "authorization_denied"
            self.send_response(400)
            self.end_headers()
            self.wfile.write(str(exc).encode("utf-8"))
            return

        _result["code"] = code
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<h2>Готово! Можно закрыть вкладку и вернуться в терминал.</h2>".encode("utf-8")
        )

    def log_message(self, format, *args):
        pass  # не засоряем консоль логами HTTP-сервера


def main():
    port = urlparse(REDIRECT_URI).port or 8080

    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": STATE,
        }
    )
    auth_url = f"{AUTH_URL}?{query}"

    # Bind the callback before opening the browser. Some Windows browser
    # launchers do not return immediately, which previously left no listener
    # available when WHOOP redirected back to localhost.
    server = HTTPServer(("localhost", port), _CallbackHandler)

    print("Открываю браузер для входа в WHOOP...")
    print("Если окно не открылось само, вставь эту ссылку в браузер вручную:\n")
    print(auth_url, "\n")
    webbrowser.open(auth_url)

    print(f"Жду ответ на {REDIRECT_URI} ...")
    while "code" not in _result and "error" not in _result:
        server.handle_request()

    if "error" in _result:
        raise SystemExit(f"WHOOP вернул ошибку: {_result['error']}")

    print("Код авторизации получен, обмениваю на токены...")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _result["code"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    resp.raise_for_status()
    tokens = validate_authorization_tokens(
        resp.json(),
        authorization_client_id=CLIENT_ID,
        authorization_redirect_uri=REDIRECT_URI,
        expected_client_id=CLIENT_ID,
        expected_redirect_uri=REDIRECT_URI,
    )
    token_handoff.install_direct_to_production(
        tokens, client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
    )
    print("Готово! Токен установлен напрямую на production; локальной копии нет.")


if __name__ == "__main__":
    main()
