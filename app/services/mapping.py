import logging
from dataclasses import dataclass

from app.schemas import AccessListDTO, AclEntryType, EsiAccessType

logger = logging.getLogger(__name__)


@dataclass
class DesiredMember:
    eve_id: int
    entry_type: AclEntryType
    role: str


def build_desired(
    acl: AccessListDTO,
    default_role: str,
    blocked_role: str | None,
    protected_eve_ids: set[int],
    role_map: dict[str, str] | None = None,
) -> dict[int, DesiredMember]:
    """Translate an ESI access list into the desired Wanderer ACL membership.

    ESI `access` is one of: Unspecified, Allowed, Blocked, Manager, Admin.
    Mapping to a Wanderer role:
      - Blocked            -> blocked_role (or skipped if blocked_role is None)
      - any other value    -> role_map[access] if present, else default_role

    role_map lets the operator pin specific ESI access levels to specific Wanderer
    roles (e.g. {"Admin": "admin", "Manager": "admin"}); unmapped levels fall back
    to default_role.
    """
    role_map = role_map or {}

    if acl.allow_everyone:
        logger.warning("ESI access list '%s' has allow_everyone=true; syncing explicit entries only", acl.name)

    result: dict[int, DesiredMember] = {}
    for entry in acl.entries:
        if entry.eve_id in protected_eve_ids:
            continue
        if entry.access == EsiAccessType.blocked:
            if blocked_role is None:
                continue
            role = blocked_role
        else:
            role = role_map.get(entry.access.value, default_role)
        result[entry.eve_id] = DesiredMember(eve_id=entry.eve_id, entry_type=entry.entry_type, role=role)
    return result
