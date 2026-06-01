import logging
import time
from dataclasses import dataclass, field

from app.clients.sso import EsiAuthError, get_valid_access_token
from app.services.mapping import build_desired

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    rule_name: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | partial | error | dry_run | skipped


async def reconcile(state, rule, settings, esi, wanderer, dry_run: bool = False) -> RunResult:
    result = RunResult(rule_name=rule.name)
    start = time.monotonic()

    try:
        token = await get_valid_access_token(
            state, rule.esi_character_id, settings.esi_client_id, settings.esi_client_secret
        )
    except EsiAuthError as e:
        result.status = "error"
        result.errors.append(str(e))
        logger.error("Rule %s auth error: %s", rule.name, e)
        return result

    etag = state.get_etag(rule.name)
    acl_dto, new_etag = await esi.get_access_list(rule.esi_character_id, rule.esi_access_list_id, token, etag)

    if acl_dto is None:
        logger.debug("Rule %s: ESI 304, skipping reconcile", rule.name)
        result.status = "skipped"
        return result

    protected = set(rule.protected_eve_ids)
    desired = build_desired(acl_dto, rule.default_role, rule.blocked_role, protected)

    wanderer_acl = await wanderer.get_acl(rule.wanderer_acl_id)
    managed_state = state.get_managed(rule.name)
    managed_ids = {int(k) for k in managed_state}
    current_by_id = {m.eve_id: m for m in wanderer_acl.members}

    to_add = {eid: m for eid, m in desired.items() if eid not in current_by_id}
    to_update = {
        eid: m for eid, m in desired.items()
        if eid in current_by_id and eid in managed_ids and current_by_id[eid].role != m.role
    }
    to_remove = {eid for eid in managed_ids if eid not in desired and eid not in protected}

    if dry_run or rule.dry_run:
        result.added = len(to_add)
        result.updated = len(to_update)
        result.removed = len(to_remove)
        result.status = "dry_run"
        logger.info("DRY RUN rule=%s add=%d update=%d remove=%d", rule.name, result.added, result.updated, result.removed)
        return result

    new_managed = dict(managed_state)

    for eid, member in to_add.items():
        try:
            await wanderer.add_member(rule.wanderer_acl_id, eid, member.entry_type, member.role)
            new_managed[str(eid)] = {"type": member.entry_type.value, "role": member.role, "last_seen": time.time()}
            result.added += 1
            logger.info("Rule %s: added %s %d as %s", rule.name, member.entry_type.value, eid, member.role)
        except Exception as e:
            result.errors.append(f"add {eid}: {e}")
            result.status = "partial"
            logger.error("Rule %s: failed to add %d: %s", rule.name, eid, e)

    for eid, member in to_update.items():
        try:
            await wanderer.update_member_role(rule.wanderer_acl_id, eid, member.role)
            new_managed[str(eid)]["role"] = member.role
            result.updated += 1
            logger.info("Rule %s: updated %d → %s", rule.name, eid, member.role)
        except Exception as e:
            result.errors.append(f"update {eid}: {e}")
            result.status = "partial"
            logger.error("Rule %s: failed to update %d: %s", rule.name, eid, e)

    for eid in to_remove:
        try:
            await wanderer.remove_member(rule.wanderer_acl_id, eid)
            new_managed.pop(str(eid), None)
            result.removed += 1
            logger.info("Rule %s: removed %d", rule.name, eid)
        except Exception as e:
            result.errors.append(f"remove {eid}: {e}")
            result.status = "partial"
            logger.error("Rule %s: failed to remove %d: %s", rule.name, eid, e)

    # On a partial run, do NOT advance the etag: a future ESI 304 would short-circuit
    # the reconcile and the failed members would never be retried. Keeping the old etag
    # forces a full re-fetch next cycle so failed add/update/remove get another attempt.
    etag_to_save = None if result.status == "partial" else new_etag
    state.update_rule_state(rule.name, etag_to_save, new_managed)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Rule %s: added=%d updated=%d removed=%d skipped=%d errors=%d time=%dms",
        rule.name, result.added, result.updated, result.removed, result.skipped, len(result.errors), elapsed_ms,
    )

    return result
