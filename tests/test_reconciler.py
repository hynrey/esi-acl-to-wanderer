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
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allowed)])
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
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allowed)])
    wanderer = _wanderer_mock([])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer, dry_run=True)

    assert result.status == "dry_run"
    assert result.added == 1
    wanderer.add_member.assert_not_called()
    wanderer.update_member_role.assert_not_called()
    wanderer.remove_member.assert_not_called()


@pytest.mark.asyncio
async def test_esi_304_skips_reconcile_without_touching_wanderer(tmp_path: Path):
    """ESI 304 (acl_dto None) must return early — no get_acl, no mutations."""
    state = _state(tmp_path, managed={"100": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = AsyncMock()
    esi.get_access_list.return_value = (None, "etag1")  # 304: unchanged
    wanderer = AsyncMock()

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.status == "skipped"
    wanderer.get_acl.assert_not_called()
    wanderer.add_member.assert_not_called()
    wanderer.update_member_role.assert_not_called()
    wanderer.remove_member.assert_not_called()


@pytest.mark.asyncio
async def test_partial_failure_keeps_failed_member_managed(tmp_path: Path):
    """A failed remove must leave the member in managed (retry next run) and set status=partial."""
    state = _state(tmp_path, managed={
        "100": {"type": "character", "role": "viewer", "last_seen": 0},
        "200": {"type": "character", "role": "viewer", "last_seen": 0},
    })
    esi = _esi_mock([])  # both gone from ESI → both desired-for-removal
    wanderer = _wanderer_mock([
        WandererMemberDTO(eve_id=100, entry_type=AclEntryType.character, role="viewer"),
        WandererMemberDTO(eve_id=200, entry_type=AclEntryType.character, role="viewer"),
    ])
    # 100 removal fails, 200 succeeds
    async def remove_side_effect(acl_id, eve_id):
        if eve_id == 100:
            raise RuntimeError("wanderer 500")
    wanderer.remove_member.side_effect = remove_side_effect

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.status == "partial"
    assert result.removed == 1
    assert len(result.errors) == 1
    managed_after = state.get_managed("test-rule")
    assert "100" in managed_after  # failed member retained
    assert "200" not in managed_after  # successfully removed


@pytest.mark.asyncio
async def test_auth_error_sets_error_status_no_wanderer_calls(tmp_path: Path):
    """Expired/invalid token → status=error, no Wanderer or ESI calls."""
    state = _state(tmp_path)
    state.get_token = MagicMock(return_value=None)  # forces EsiAuthError in get_valid_access_token
    esi = AsyncMock()
    wanderer = AsyncMock()

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.status == "error"
    assert len(result.errors) == 1
    esi.get_access_list.assert_not_called()
    wanderer.get_acl.assert_not_called()


@pytest.mark.asyncio
async def test_managed_set_persisted_after_add(tmp_path: Path):
    """Round-trip: a successful add writes the member into persisted state."""
    state = _state(tmp_path)
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allowed)])
    wanderer = _wanderer_mock([])

    await reconcile(state, _rule(), _settings(), esi, wanderer)

    managed_after = state.get_managed("test-rule")
    assert "100" in managed_after
    assert managed_after["100"]["role"] == "viewer"
    assert managed_after["100"]["type"] == "character"


@pytest.mark.asyncio
async def test_idempotent_second_run(tmp_path: Path):
    state = _state(tmp_path, managed={"100": {"type": "character", "role": "viewer", "last_seen": 0}})
    esi = _esi_mock([AclEntryDTO(eve_id=100, entry_type=AclEntryType.character, access=EsiAccessType.allowed)])
    wanderer = _wanderer_mock([WandererMemberDTO(eve_id=100, entry_type=AclEntryType.character, role="viewer")])

    result = await reconcile(state, _rule(), _settings(), esi, wanderer)

    assert result.added == 0
    assert result.updated == 0
    assert result.removed == 0
