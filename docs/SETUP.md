# Setup — from zero to a running sync

Full first-time preparation. Each step is required once; after that you only run the
service.

Throughout this guide:

```bash
WSYNC=~/.pyenv/versions/3.13.0/bin/uv     # uv is not on PATH on this machine
```

---

## 1. Install dependencies

```bash
cd /home/hynrey/dev/eve-online/acl_wanderer_eve_sync
$WSYNC sync
```

This creates `.venv/` and installs the project (editable) plus the `wacl-sync` CLI.
Verify:

```bash
$WSYNC run wacl-sync --help
```

---

## 2. Create an EVE application

Go to <https://developers.eveonline.com> → **Create New Application**.

- **Connection Type:** `Authentication & API Access`
- **Permissions / Scopes:** `esi-access.read_lists.v1`
  (this is the **only** scope needed — do not add others; SSO rejects
  `esi-activities.read_character.v1`)
- **Callback URL:** `http://localhost:8765/callback`

Save. Copy the **Client ID** and **Secret Key**.

---

## 3. Generate a Fernet key (encrypts the refresh token on disk)

```bash
$WSYNC run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Optional but recommended. Without it the refresh token is stored as plaintext (the
service logs a warning).

---

## 4. Fill `.env`

```bash
cp .env.example .env
```

```ini
ESI_CLIENT_ID=<your client id>
ESI_CLIENT_SECRET=<your secret key>
ESI_CALLBACK_URL=http://localhost:8765/callback
# Quote the value — the parens break `source .env` in bash otherwise:
ESI_USER_AGENT="acl-wanderer-sync/1.0 (hynrey@gmail.com)"
FERNET_KEY=<key from step 3>
STATE_PATH=state.json
CONFIG_PATH=config.yaml
# Optional: keep the Wanderer token here too (see step 6 for why it must reach os.environ)
WANDERER_ACL_TOKEN=<your wanderer acl token>
```

---

## 5. Find your Wanderer ACL id, token, and roles

In Wanderer:

1. Open the ACL page. The URL is `http://<host>/access-lists/<ACL-UUID>` — the
   **`<ACL-UUID>`** is your `wanderer_acl_id` (not the map id).
2. The **ACL API token** (`api_key`) is shown on the ACL create/edit screen; it is
   passed as `Authorization: Bearer <token>`.
3. Roles in the UI: `Admin`, `Manager`, `Member`, `Viewer`, `-blocked-`. The **API
   uses lowercase**: `admin`, `manager`, `member`, `viewer`, `blocked`.

Sanity-check the token + id against the live API:

```bash
curl -s -H "Authorization: Bearer <token>" \
  http://<host>/api/acls/<ACL-UUID> | python3 -m json.tool
```

A `200` with a `{"data": {... "members": [...] ...}}` body means you have the right id
and token. A `404` usually means the id is a map id, not an ACL id.

---

## 6. Fill `config.yaml`

```bash
cp config.yaml.example config.yaml
```

```yaml
rules:
  - name: dark-side-main
    esi_character_id: 92576340            # the character that owns the ESI access list
    esi_access_list_id: 538293            # the ESI access list id
    wanderer_base_url: http://localhost:1210
    wanderer_acl_id: b795eee9-0fdd-4d18-8ab6-05a4790eeeca
    wanderer_acl_token: ${WANDERER_ACL_TOKEN}   # never inline a real token; config.yaml is not gitignored
    default_role: viewer                  # Allowed/Unspecified → this
    blocked_role: blocked                 # ESI "Blocked" → this; null to skip blocked entries
    role_map:                             # override per ESI access level
      Admin: admin
      Manager: manager
    protected_eve_ids: []                 # ids the sync must never touch
    interval_seconds: 300
    dry_run: false
```

> **`WANDERER_ACL_TOKEN` must be in the process environment.** `config.yaml`'s
> `${WANDERER_ACL_TOKEN}` is resolved against `os.environ`, *not* `.env` (pydantic's
> `.env` only feeds `Settings`). So either prefix the command
> (`WANDERER_ACL_TOKEN=... $WSYNC run wacl-sync ...`) or load `.env` into the shell:
> `set -a; source .env; set +a`.

---

## 7. Enroll the character (EVE SSO, one time)

```bash
$WSYNC run wacl-sync sso 92576340
```

It prints an SSO URL and starts a localhost callback server on `:8765`.

- **Normal desktop:** the browser opens, you authorize, the callback is captured
  automatically, the token is saved. Done.
- **WSL / headless / remote:** there is no browser and the localhost callback often
  fails forwarding. The command handles this:
  1. Copy the printed URL into a browser on your host machine, authorize.
  2. EVE redirects to `http://localhost:8765/callback?code=...`. The browser may hang
     on that redirect — that's expected.
  3. Copy the **full redirect URL** (or just the `code=` value) and paste it into the
     terminal prompt (`> `), press Enter.

On success: `Token for character 92576340 saved.` and `state.json` now holds the
encrypted refresh token (file mode `0600`).

---

## 8. Dry-run, then apply

```bash
# Plan only — touches nothing in Wanderer:
WANDERER_ACL_TOKEN=<token> $WSYNC run wacl-sync preview
# -> dark-side-main: add=7 update=0 remove=0 skip=0

# Apply for real:
WANDERER_ACL_TOKEN=<token> $WSYNC run wacl-sync once

# Verify idempotency — a second run with no ESI change returns 0/0/0
# (often a 304 -> "skipped", which skips Wanderer entirely):
WANDERER_ACL_TOKEN=<token> $WSYNC run wacl-sync once
```

Check the Wanderer ACL UI to confirm the expected members/roles. Any manual member
(one the sync didn't add) and any `protected_eve_ids` must be untouched.

---

## 9. Run it continuously

The service has its own loop — **you do not need cron inside Docker**.

**Long-running container (recommended):**

```bash
set -a; source .env; set +a   # so WANDERER_ACL_TOKEN reaches docker compose
docker compose up -d
docker compose logs -f
```

One container, `restart: unless-stopped`, sleeps `interval_seconds` between cycles,
clean SIGTERM shutdown on `docker compose down`. `state.json` + `config.yaml` are
mounted, so it survives restarts.

**Or, host process:**

```bash
set -a; source .env; set +a
$WSYNC run wacl-sync run
```

**Or, external scheduler** (if you'd rather not keep a process resident): run
`wacl-sync once` from a systemd-timer or cron — it returns a non-zero exit code on
error. See the commented alternative in `docker-compose.yml`.
