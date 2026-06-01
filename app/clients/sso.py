import base64
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ESI_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SCOPES = ["esi-access.read_lists.v1", "esi-activities.read_character.v1"]


class EsiAuthError(Exception):
    pass


def _basic_auth(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


async def refresh_token(client_id: str, client_secret: str, refresh_tok: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            ESI_TOKEN_URL,
            headers={"Authorization": _basic_auth(client_id, client_secret)},
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
        )
    if resp.status_code == 400 and resp.json().get("error") == "invalid_grant":
        raise EsiAuthError("Refresh token is invalid or revoked. Re-run: wacl-sync sso <character_id>")
    resp.raise_for_status()
    return resp.json()


async def get_valid_access_token(state, character_id: int, client_id: str, client_secret: str) -> str:
    token = state.get_token(character_id)
    if token is None:
        raise EsiAuthError(f"No token for character {character_id}. Run: wacl-sync sso {character_id}")
    if token["access_token"] and token["expires_at"] - time.time() > 60:
        return token["access_token"]
    data = await refresh_token(client_id, client_secret, token["refresh_token"])
    expires_at = time.time() + data["expires_in"] - 30
    state.set_token(character_id, data["refresh_token"], data["access_token"], expires_at)
    return data["access_token"]


def enroll(character_id: int, client_id: str, client_secret: str, callback_url: str, state) -> None:
    """One-shot SSO enrollment. Opens browser, waits for callback, saves token."""
    import secrets

    state_param = secrets.token_urlsafe(16)
    parsed = urllib.parse.urlparse(callback_url)
    port = parsed.port or 8765

    auth_url = ESI_AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": callback_url,
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "state": state_param,
    })

    code_holder: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code_holder["code"] = qs.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *args):
            pass

    print(f"Opening browser for EVE SSO...\n{auth_url}")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", port), _Handler)
    server.handle_request()

    code = code_holder.get("code")
    if not code:
        raise EsiAuthError("No authorization code received from SSO callback")

    resp = httpx.post(
        ESI_TOKEN_URL,
        headers={"Authorization": _basic_auth(client_id, client_secret)},
        data={"grant_type": "authorization_code", "code": code},
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = time.time() + data["expires_in"] - 30
    state.set_token(character_id, data["refresh_token"], data["access_token"], expires_at)
    print(f"Token for character {character_id} saved.")
