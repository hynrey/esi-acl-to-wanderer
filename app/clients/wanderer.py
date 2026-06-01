import logging

import httpx

from app.schemas import AclEntryType, WandererAclDTO, WandererMemberDTO

logger = logging.getLogger(__name__)

_EVE_ID_FIELD = {
    AclEntryType.character: "eve_character_id",
    AclEntryType.corporation: "eve_corporation_id",
    AclEntryType.alliance: "eve_alliance_id",
}


class WandererClient:
    def __init__(self, base_url: str, acl_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {acl_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WandererClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def get_acl(self, acl_id: str) -> WandererAclDTO:
        resp = await self._client.get(f"/api/acls/{acl_id}")
        resp.raise_for_status()
        data = resp.json()
        members = [WandererMemberDTO.from_wanderer_response(m) for m in data.get("members", [])]
        return WandererAclDTO(id=str(data["id"]), members=members)

    async def add_member(self, acl_id: str, eve_id: int, entry_type: AclEntryType, role: str) -> None:
        payload = {"member": {_EVE_ID_FIELD[entry_type]: str(eve_id), "role": role}}
        resp = await self._client.post(f"/api/acls/{acl_id}/members", json=payload)
        if resp.status_code == 409:
            await self.update_member_role(acl_id, eve_id, role)
            return
        resp.raise_for_status()

    async def update_member_role(self, acl_id: str, eve_id: int, role: str) -> None:
        resp = await self._client.put(f"/api/acls/{acl_id}/members/{eve_id}", json={"member": {"role": role}})
        resp.raise_for_status()

    async def remove_member(self, acl_id: str, eve_id: int) -> None:
        resp = await self._client.delete(f"/api/acls/{acl_id}/members/{eve_id}")
        resp.raise_for_status()
