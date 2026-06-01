import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import RuleConfig
from app.schemas import AclEntryDTO, AclEntryType, AccessListDTO, EsiAccessType, WandererAclDTO, WandererMemberDTO
from app.services.reconciler import RunResult, reconcile
from app.state import StateManager


def _settings():
    s = MagicMock()
    s.esi_client_id = "cid"
    s.esi_client_secret = "csec"
    return s


def _rule(**kw) -> RuleConfig:
    base = dict(
        name="test-rule",
        esi_character_id=123,
        esi_access_list_id=1,
        wanderer_base_url="http://w.test",
        wanderer_acl_id="acl-uuid",
        wanderer_acl_token="tok",
        default_role="viewer",
        blocked_role="blocked",
        protected_eve_ids=[],
        interval_seconds=300,
        dry_run=False,
    )
    base.update(kw)
    return RuleConfig(**base)


def _state(tmp_path: Path, managed: dict | None = None) -> StateManager:
    data: dict = {
        "tokens": {
            "123": {
                "refresh_token": "rtoken",
                "access_token": "atoken",
                "expires_at": time.time() + 3600,
            }
        },
        "rules": {},
    }
    if managed is not None:
        data["rules"]["test-rule"] = {"managed": managed}
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    return StateManager(p, fernet_key=None)


def _acl_dto(entries: list[AclEntryDTO]) -> AccessListDTO:
    return AccessListDTO(id=1, name="test", allow_everyone=False, entries=entries)


def _wanderer_acl(members: list[WandererMemberDTO]) -> WandererAclDTO:
    return WandererAclDTO(id="acl-uuid", members=members)


def _esi_mock(entries: list[AclEntryDTO], etag: str = "etag1"):
    m = AsyncMock()
    m.get_access_list.return_value = (_acl_dto(entries), etag)
    return m


def _wanderer_mock(members: list[WandererMemberDTO]):
    m = AsyncMock()
    m.get_acl.return_value = _wanderer_acl(members)
    return m


@pytest.mark.asyncio
async def test_adds_new_member(tmp_path: Path):
    state = _state(tmp_path)
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allow)])
    wanderer = _wanderer_mock([])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.added == 1
    assert result.updated == 0
    assert result.removed == 0
    wanderer.add_member.assert_called_once_with("acl-uuid", 100, AclEntryType.character, "viewer")


@pytest.mark.asyncio
async def test_updates_role_for_managed_member(tmp_path: Path):
    state = _state(tmp_path, managed={"100": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.blocked)])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=100, entry_type=AclEntryType.character, role="viewer")])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.updated == 1
    wanderer.update_member_role.assert_called_once_with("acl-uuid", 100, "blocked")


@pytest.mark.asyncio
async def test_removes_managed_member_gone_from_esi(tmp_path: Path):
    state = _state(tmp_path, managed={"100": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = _esi_mock([])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=100, entry_type=AclEntryType.character, role="viewer")])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.removed == 1
    wanderer.remove_member.assert_called_once_with("acl-uuid", 100)


@pytest.mark.asyncio
async def test_manual_member_never_removed(tmp_path: Path):
    """999 is in Wanderer ACL but not in state.managed → must not be removed."""
    state = _state(tmp_path, managed={})
    esi = _esi_mock([])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=999, entry_type=AclEntryType.character, role="admin")])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.removed == 0
    wanderer.remove_member.assert_not_called()


@pytest.mark.asyncio
async def test_protected_member_never_removed(tmp_path: Path):
    state = _state(tmp_path, managed={"555": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = _esi_mock([])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=555, entry_type=AclEntryType.character, role="viewer")])

    result = await reconcile(state, _rule(protected_eve_ids=[555]), _settings(), esi, wanderer)

    assert result.removed == 0
    wanderer.remove_member.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_makes_no_wanderer_calls(tmp_path: Path):
    state = _state(tmp_path, managed={})
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allow)])
    wanderer = _wanderer_mock([])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer, dry_run=True)

    assert result.status == "dry_run"
    assert result.added == 1
    wanderer.add_member.assert_not_called()
    wanderer.update_member_role.assert_not_called()
    wanderer.remove_member.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_second_run(tmp_path: Path):
    state = _state(tmp_path, managed={"100": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allow)])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=100, entry_type=AclEntryType.character, role="viewer")])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.added == 0
    assert result.updated == 0
    assert result.removed == 0
