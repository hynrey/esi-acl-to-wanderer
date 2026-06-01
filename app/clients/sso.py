import base64
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ESI_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SCOPES = ["esi-access.read_lists.v1"]


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


def _extract_code(pasted: str) -> str | None:
    """Accept either a bare auth code or a full redirect URL containing ?code=..."""
    pasted = pasted.strip()
    if not pasted:
        return None
    if "code=" in pasted:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        return qs.get("code", [None])[0]
    return pasted


def enroll(character_id: int, client_id: str, client_secret: str, callback_url: str, state) -> None:
    """One-shot SSO enrollment.

    Starts a localhost callback server AND accepts a manually pasted redirect URL /
    code. Whichever arrives first wins — the manual path is the fallback for
    environments (WSL, remote shells, headless) where the browser cannot reach the
    localhost callback server.
    """
    import secrets
    import threading

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
    got_code = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            if code and not got_code.is_set():
                code_holder["code"] = code
                got_code.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")

        def log_message(self, *args):
            pass

    def _serve(server: HTTPServer) -> None:
        while not got_code.is_set():
            server.handle_request()

    print("Open this URL in a browser and authorize:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)  # no-op / harmless if no browser available

    server = HTTPServer(("localhost", port), _Handler)
    server.timeout = 1  # so the serve loop can notice got_code being set elsewhere
    threading.Thread(target=_serve, args=(server,), daemon=True).start()

    print(
        f"Waiting for the callback on http://localhost:{port}/callback ...\n"
        "If the browser can't reach localhost (common on WSL/remote), copy the URL it\n"
        "redirected to (or just the code= value) and paste it here, then press Enter:"
    )

    # Manual paste path. Runs in the main thread; if the auto-server already captured
    # the code, this input is ignored.
    try:
        pasted = input("> ")
    except EOFError:
        pasted = ""

    if not got_code.is_set():
        code = _extract_code(pasted)
        if code:
            code_holder["code"] = code
            got_code.set()

    server.server_close()

    code = code_holder.get("code")
    if not code:
        raise EsiAuthError("No authorization code received (neither callback nor manual paste)")

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
