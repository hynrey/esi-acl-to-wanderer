# acl_wanderer_eve_sync — Design Spec

**Date:** 2026-06-01  
**Status:** Approved

## Purpose

One-way sync: EVE ESI Access List → Wanderer ACL. Runs as a lightweight Python service — no DB, no broker. State lives in a single JSON file.

## Stack

- Python 3.12, uv
- httpx (async HTTP)
- pydantic v2 + pydantic-settings
- cryptography (Fernet — refresh token encryption at rest)
- tenacity (retry/backoff)
- **Excluded:** Celery, Redis, PostgreSQL, SQLAlchemy, FastAPI, APScheduler

## Architecture

### Sync Direction

ESI Access List (source of truth) → Wanderer ACL (target). Read-only from ESI. No bidirectional sync.

### Scheduling

Plain `asyncio` loop: `reconcile → sleep(interval_seconds)`. Supports `--once` flag for systemd-timer/cron use. Single process = sequential runs = no distributed lock needed.

### State

Single `state.json` file:
- Tokens per character (refresh + access, encrypted with Fernet if `FERNET_KEY` set, else plaintext + warning)
- ETag per rule (for ESI 304 caching)
- `managed` set per rule: EVE IDs this service added — only these are eligible for update/remove

Atomic write: write to tmp file → `os.replace` → chmod 0600.

### Package Structure

```
app/
  cli.py            # CLI entrypoint: run | once | preview [rule] | sso <char_id>
  config.py         # Settings (env) + RuleConfig (config.yaml with ${ENV} interpolation)
  state.py          # Load/save state.json, Fernet enc/dec helpers
  schemas.py        # AccessListDTO, AclEntryDTO, WandererMemberDTO, WandererAclDTO
  runner.py         # async run_forever(), SIGINT/SIGTERM handling
  clients/
    sso.py          # OAuth2 refresh flow + enroll() (stdlib http.server, no FastAPI)
    esi.py          # EsiClient: get_access_list() with ETag, error-limit backoff
    wanderer.py     # WandererClient: get_acl(), add/update/remove member (idempotent add)
  services/
    mapping.py      # build_desired(entries, rule) -> dict[eve_id, DesiredMember] — pure fn
    reconciler.py   # reconcile(state, rule, esi, wanderer, dry_run) -> RunResult
tests/
  test_mapping.py   # build_desired: allow/blocked/unspecified, allow_everyone, protected_eve_ids
  test_reconciler.py # diff logic, manual members untouched, dry_run, idempotency
  test_clients.py   # httpx.MockTransport: ETag 304, token refresh, Wanderer idempotent add
```

### Configuration

`config.yaml` (secrets via `${ENV}` interpolation, not committed):

```yaml
rules:
  - name: dark-side-main
    esi_character_id: 92576340
    esi_access_list_id: 1
    wanderer_base_url: https://wanderer.example.com
    wanderer_acl_id: 19712899-ec3a-47b1-b73b-2bae221c5513
    wanderer_acl_token: ${WANDERER_ACL_TOKEN}
    default_role: viewer
    blocked_role: blocked        # null = skip blocked entries
    protected_eve_ids: [11111111]
    interval_seconds: 300
    dry_run: false
```

Global settings (env vars): `ESI_CLIENT_ID`, `ESI_CLIENT_SECRET`, `ESI_CALLBACK_URL`, `ESI_COMPATIBILITY_DATE` (default: `2026-05-19`), `ESI_USER_AGENT`, `FERNET_KEY` (optional), `STATE_PATH`, `CONFIG_PATH`.

### ESI → Wanderer Mapping

| ESI type | Wanderer field |
|---|---|
| `characters[].character_id` | `eve_character_id` |
| `corporations[].corporation_id` | `eve_corporation_id` |
| `alliances[].alliance_id` | `eve_alliance_id` |

- `access` = allow / Unspecified → `default_role`
- `access` = blocked → `blocked_role` (or skip if null)
- `allow_everyone = true` → log warning, sync explicit entries only
- `protected_eve_ids` → excluded from desired (never touched)

> ⚠️ TODO: confirm `access` enum values via ESI Swagger `X-Compatibility-Date: 2026-05-19`  
> ⚠️ TODO: confirm Wanderer roles (seen: `admin`, `viewer`) and blocked role name via live API

### Reconciler Logic

1. Fetch ESI Access List (respect ETag / 304)
2. Build `desired` via `build_desired()`
3. Fetch Wanderer ACL — split members into `managed` (in state) and `unmanaged` (ignore)
4. Diff:
   - `to_add`: in desired, not in Wanderer
   - `to_update`: in desired + managed, but role differs
   - `to_remove`: in managed, not in desired
   - `protected_eve_ids` and unmanaged members: never in to_remove
5. Apply: add/update first, then remove; update state.managed + etag
6. Per-member errors → `partial` result, run continues

### CLI Commands

| Command | Behavior |
|---|---|
| `wacl-sync run` | Long-running loop, SIGTERM exits cleanly |
| `wacl-sync once` | Single pass all rules, exit code ≠ 0 on any error |
| `wacl-sync preview [rule]` | Dry-run, print plan, no Wanderer writes |
| `wacl-sync sso <char_id>` | EVE SSO enroll, write refresh token to state.json |

### Deployment

Single Docker service (`command: run`), volume mounts for `state.json` and `config.yaml`. Alternative: image + `command: once` under external systemd-timer/cron.

## Error Handling

- Expired refresh token → log `EsiAuthError` for that rule, other rules continue
- Per-member Wanderer API error → `partial` RunResult, log, continue
- Tokens/secrets never logged (masked)
- `wacl-sync once` returns non-zero exit code if any rule errored

## Testing

- `pytest` with `httpx.MockTransport` for client tests (no real network)
- All reconciler logic tested with mock clients
- Key invariants tested: manual members not deleted, dry_run makes no calls, idempotency (second run = 0/0/0)
