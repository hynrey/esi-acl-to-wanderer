# wanderer-acl-sync

One-way sync of an **EVE Online ESI Access List** → a **Wanderer ACL**. A lean Python
service: no database, no broker. ESI is the source of truth; the service brings the
Wanderer ACL into line with it without ever touching members it didn't add.

## What it does

- Reads an ESI Access List (`GET /characters/{id}/access-lists/{acl_id}`).
- Reconciles it into a Wanderer ACL: adds members present in ESI, updates their role
  when it drifts, removes members it previously added that have left ESI.
- **Never touches manual Wanderer members** (anyone not in its own `managed` set) or
  anyone listed in `protected_eve_ids`.
- Runs as a long-lived loop (`run`) or a one-shot pass (`once`) for cron/systemd.
- State (refresh token, ETag, managed set) lives in a single `state.json`. The refresh
  token is Fernet-encrypted when `FERNET_KEY` is set.

## Quick start

```bash
# 0. uv is at ~/.pyenv/versions/3.13.0/bin/uv on this machine
alias wacl='~/.pyenv/versions/3.13.0/bin/uv run wacl-sync'

# 1. configure (see docs/SETUP.md for the full walkthrough)
cp .env.example .env            # fill EVE app creds + FERNET_KEY
cp config.yaml.example config.yaml   # fill character/acl ids + roles

# 2. enroll the ESI character via EVE SSO (one time)
wacl sso <character_id>

# 3. dry-run — prints the plan, changes nothing
WANDERER_ACL_TOKEN=<token> wacl preview

# 4. apply once
WANDERER_ACL_TOKEN=<token> wacl once

# 5. run forever (or use docker compose up -d)
WANDERER_ACL_TOKEN=<token> wacl run
```

Full first-time setup — EVE application, scopes, SSO on WSL/headless, role mapping —
is in **[docs/SETUP.md](docs/SETUP.md)**.

## CLI

| Command | What |
|---|---|
| `wacl-sync sso <character_id>` | EVE SSO enrollment; writes the refresh token to `state.json` |
| `wacl-sync preview [rule]` | Dry-run; prints `add/update/remove/skip`, touches nothing |
| `wacl-sync once` | One reconcile pass over all rules; exit code ≠ 0 if any rule errored |
| `wacl-sync run` | Long-running loop; sleeps `interval_seconds`; clean SIGTERM shutdown |

## Configuration

- **`.env`** — global secrets/settings: `ESI_CLIENT_ID`, `ESI_CLIENT_SECRET`,
  `ESI_CALLBACK_URL`, `ESI_USER_AGENT`, `FERNET_KEY` (optional), `STATE_PATH`,
  `CONFIG_PATH`.
- **`config.yaml`** — one or more `rules`. Secrets via `${ENV_VAR}` interpolation
  (e.g. `wanderer_acl_token: ${WANDERER_ACL_TOKEN}`) — never commit real tokens.

### Role mapping (ESI access → Wanderer role)

ESI `access` is one of `Unspecified`, `Allowed`, `Blocked`, `Manager`, `Admin`.
Wanderer roles (API form, lowercase): `admin`, `manager`, `member`, `viewer`, `blocked`.

- `Blocked` → `blocked_role` (or skipped if `blocked_role: null`)
- everything else → `role_map[access]` if set, else `default_role`

```yaml
default_role: viewer
blocked_role: blocked
role_map:
  Admin: admin
  Manager: manager
```

## Deployment

```bash
docker compose up -d        # single long-running container, command: run
docker compose logs -f
```

`state.json` and `config.yaml` are mounted as volumes so the container survives
restarts (ETag cache + managed set persist). For a non-resident setup, run
`command: once` from an external systemd-timer/cron instead (see comment in
`docker-compose.yml`).

## Safety guarantees

- Removal/role-update only ever target the `managed` set — members the service itself
  added. A member added by hand in Wanderer is never modified or removed.
- `protected_eve_ids` are excluded from the desired set entirely.
- On ESI `304 Not Modified` the run short-circuits before any Wanderer mutation.
- Per-member and per-rule errors are isolated: one failure degrades to `partial` /
  logs and continues; it never crashes the loop.

## Development

```bash
~/.pyenv/versions/3.13.0/bin/uv sync        # install deps
~/.pyenv/versions/3.13.0/bin/uv run pytest  # run tests (no network; respx-mocked)
```
