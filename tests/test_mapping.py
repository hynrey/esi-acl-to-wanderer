import logging

from app.schemas import AccessListDTO, AclEntryDTO, AclEntryType, EsiAccessType
from app.services.mapping import build_desired


def _acl(entries, allow_everyone=False) -> AccessListDTO:
    return AccessListDTO(id=1, name="test", allow_everyone=allow_everyone, entries=entries)


def _entry(eve_id: int, entry_type: AclEntryType, access: EsiAccessType) -> AclEntryDTO:
    return AclEntryDTO(eve_id=eve_id, entry_type=entry_type, access=access)


def test_allow_gets_default_role():
    acl = _acl([_entry(100, AclEntryType.character, EsiAccessType.allowed)])
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[100].role == "viewer"


def test_unspecified_gets_default_role():
    acl = _acl([_entry(200, AclEntryType.corporation, EsiAccessType.unspecified)])
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[200].role == "viewer"


def test_blocked_gets_blocked_role():
    acl = _acl([_entry(300, AclEntryType.alliance, EsiAccessType.blocked)])
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[300].role == "blocked"


def test_blocked_skipped_when_blocked_role_none():
    acl = _acl([_entry(300, AclEntryType.alliance, EsiAccessType.blocked)])
    result = build_desired(acl, default_role="viewer", blocked_role=None, protected_eve_ids=set())
    assert 300 not in result


def test_protected_ids_excluded():
    acl = _acl(
        [
            _entry(100, AclEntryType.character, EsiAccessType.allowed),
            _entry(999, AclEntryType.character, EsiAccessType.allowed),
        ]
    )
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids={999})
    assert 100 in result
    assert 999 not in result


def test_allow_everyone_logs_warning_but_syncs_entries(caplog):
    acl = _acl([_entry(100, AclEntryType.character, EsiAccessType.allowed)], allow_everyone=True)
    with caplog.at_level(logging.WARNING):
        result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert "allow_everyone" in caplog.text
    assert 100 in result


def test_entry_types_preserved():
    acl = _acl(
        [
            _entry(1, AclEntryType.character, EsiAccessType.allowed),
            _entry(2, AclEntryType.corporation, EsiAccessType.allowed),
            _entry(3, AclEntryType.alliance, EsiAccessType.allowed),
        ]
    )
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[1].entry_type == AclEntryType.character
    assert result[2].entry_type == AclEntryType.corporation
    assert result[3].entry_type == AclEntryType.alliance


def test_empty_acl_returns_empty():
    result = build_desired(_acl([]), default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result == {}


def test_manager_and_admin_fall_back_to_default_without_role_map():
    acl = _acl(
        [
            _entry(10, AclEntryType.character, EsiAccessType.manager),
            _entry(11, AclEntryType.character, EsiAccessType.admin),
        ]
    )
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[10].role == "viewer"
    assert result[11].role == "viewer"


def test_role_map_overrides_per_access_level():
    acl = _acl(
        [
            _entry(10, AclEntryType.character, EsiAccessType.admin),
            _entry(11, AclEntryType.character, EsiAccessType.manager),
            _entry(12, AclEntryType.character, EsiAccessType.allowed),
        ]
    )
    result = build_desired(
        acl,
        default_role="viewer",
        blocked_role="blocked",
        protected_eve_ids=set(),
        role_map={"Admin": "admin", "Manager": "admin"},
    )
    assert result[10].role == "admin"  # Admin -> admin
    assert result[11].role == "admin"  # Manager -> admin
    assert result[12].role == "viewer"  # Allowed unmapped -> default


def test_role_map_does_not_affect_blocked():
    acl = _acl([_entry(20, AclEntryType.character, EsiAccessType.blocked)])
    result = build_desired(
        acl,
        default_role="viewer",
        blocked_role="blocked",
        protected_eve_ids=set(),
        role_map={"Blocked": "admin"},  # must be ignored; blocked_role wins
    )
    assert result[20].role == "blocked"
