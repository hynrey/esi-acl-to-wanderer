"""Throwaway ESI diagnostic: print token scopes + the raw access-list response.

Usage (from repo root, with .env present):
    STATE_PATH=data/state.json uv run python scripts/diag.py <character_id> <access_list_id>
"""

import asyncio
import base64
import json
import sys

import httpx

from app.clients.sso import get_valid_access_token
from app.config import Settings
from app.state import StateManager


async def main() -> None:
    character_id = int(sys.argv[1])
    access_list_id = int(sys.argv[2])

    settings = Settings()
    state = StateManager(settings.state_path, settings.fernet_key)
    token = await get_valid_access_token(
        state, character_id, settings.esi_client_id, settings.esi_client_secret
    )

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    print("SCOPES:", claims.get("scp"))
    print("TOKEN sub:", claims.get("sub"))

    url = f"https://esi.evetech.net/characters/{character_id}/access-lists/{access_list_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Compatibility-Date": settings.esi_compatibility_date,
            },
        )
    print("HTTP", resp.status_code)
    print(resp.text[:800])


if __name__ == "__main__":
    asyncio.run(main())
