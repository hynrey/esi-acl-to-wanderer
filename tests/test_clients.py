import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from app.clients.esi import EsiClient, ESI_BASE
from app.clients.sso import EsiAuthError, ESI_TOKEN_URL, get_valid_access_token
from app.clients.wanderer import WandererClient
from app.schemas import AclEntryType
from app.state import StateManager

WANDERER_BASE = "http://wanderer.test"


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    data = {
        "tokens": {
            "123": {
                "refresh_token": "valid_refresh",
                "access_token": "expired_token",
                "expires_at": 1.0,
            }
        },
        "rules": {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    return StateManager(p, fernet_key=None)


# --- ESI ---

@pytest.mark.asyncio
async def test_esi_parses_access_list():
    payload = {
        "id": 1,
        "name": "Main List",
        "membership": {
            "allow_everyone": False,
            "characters": [{"character_id": 100, "access": "Unspecified"}],
            "corporations": [],
            "alliances": [],
        },
    }
    async with respx.mock:
        respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(200, json=payload, headers={"ETag": '"etag1"'})
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            result, etag = await client.get_access_list(123, 1, "token")

    assert result is not None
    assert len(result.entries) == 1
    assert result.entries[0].eve_id == 100
    assert etag == '"etag1"'


@pytest.mark.asyncio
async def test_esi_304_returns_none_with_original_etag():
    async with respx.mock:
        respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(304)
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            result, etag = await client.get_access_list(123, 1, "token", etag='"etag1"')

    assert result is None
    assert etag == '"etag1"'


@pytest.mark.asyncio
async def test_esi_sends_if_none_match():
    async with respx.mock:
        route = respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(304)
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            await client.get_access_list(123, 1, "token", etag='"cached"')

    assert route.calls[0].request.headers["If-None-Match"] == '"cached"'


# --- SSO token refresh ---

@pytest.mark.asyncio
async def test_sso_refresh_on_expired_token(state: StateManager):
    async with respx.mock:
        respx.post(ESI_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "new_access",
                "refresh_token": "new_refresh",
                "expires_in": 1200,
            })
        )
        token = await get_valid_access_token(state, 123, "client_id", "client_secret")

    assert token == "new_access"
    saved = state.get_token(123)
    assert saved["access_token"] == "new_access"
    assert saved["refresh_token"] == "new_refresh"


@pytest.mark.asyncio
async def test_sso_raises_esi_auth_error_on_invalid_grant(state: StateManager):
    async with respx.mock:
        respx.post(ESI_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(EsiAuthError):
            await get_valid_access_token(state, 123, "client_id", "client_secret")


@pytest.mark.asyncio
async def test_sso_uses_cached_token_when_fresh(tmp_path: Path):
    future_expires = time.time() + 3600
    data = {
        "tokens": {
            "123": {
                "refresh_token": "rtoken",
                "access_token": "fresh_token",
                "expires_at": future_expires,
            }
        },
        "rules": {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    state = StateManager(p, fernet_key=None)

    async with respx.mock:
        token = await get_valid_access_token(state, 123, "client_id", "client_secret")
        assert respx.calls.call_count == 0

    assert token == "fresh_token"


# --- Wanderer ---

@pytest.mark.asyncio
async def test_wanderer_parses_members():
    payload = {
        "id": "acl-uuid",
        "members": [
            {"eve_character_id": "100", "role": "viewer"},
            {"eve_corporation_id": "200", "role": "admin"},
        ],
    }
    async with respx.mock:
        respx.get(f"{WANDERER_BASE}/api/acls/acl-uuid").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            acl = await client.get_acl("acl-uuid")

    assert len(acl.members) == 2
    assert acl.members[0].eve_id == 100
    assert acl.members[0].entry_type == AclEntryType.character
    assert acl.members[1].eve_id == 200
    assert acl.members[1].entry_type == AclEntryType.corporation


@pytest.mark.asyncio
async def test_wanderer_add_idempotent_on_409():
    async with respx.mock:
        respx.post(f"{WANDERER_BASE}/api/acls/acl-uuid/members").mock(
            return_value=httpx.Response(409)
        )
        respx.put(f"{WANDERER_BASE}/api/acls/acl-uuid/members/100").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            await client.add_member("acl-uuid", 100, AclEntryType.character, "viewer")


@pytest.mark.asyncio
async def test_wanderer_remove_member():
    async with respx.mock:
        respx.delete(f"{WANDERER_BASE}/api/acls/acl-uuid/members/100").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            await client.remove_member("acl-uuid", 100)
