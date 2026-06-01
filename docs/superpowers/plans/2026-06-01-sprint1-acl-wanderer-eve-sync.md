# Sprint 1: ACL Wanderer EVE Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `wacl-sync` — a lean Python service that one-way syncs EVE ESI Access Lists into Wanderer ACL, with no DB/broker, state in a single JSON file.

**Architecture:** Asyncio loop runs `reconcile → sleep` per rule. ESI is read-only source of truth. Only members added by this service (`managed`) are ever modified or removed — manual Wanderer members are untouched.

**Tech Stack:** Python 3.12, uv, httpx (async), pydantic v2, pydantic-settings, cryptography (Fernet), tenacity, click, PyYAML. Dev: pytest, pytest-asyncio, respx.

---

## File Map

| File | Responsibility |
|---|---|
| `pyproject.toml` | Project metadata, deps, CLI entrypoint, pytest config |
| `app/__init__.py` | Empty package marker |
| `app/schemas.py` | Pydantic DTOs for ESI + Wanderer responses |
| `app/config.py` | `Settings` (env) + `RuleConfig` + `load_rules()` with `${ENV}` interpolation |
| `app/state.py` | `StateManager`: load/save `state.json` atomically, Fernet enc/dec |
| `app/clients/sso.py` | OAuth2 refresh flow + `enroll()` one-shot CLI |
| `app/clients/esi.py` | `EsiClient`: `get_access_list()` with ETag/304, error-limit backoff |
| `app/clients/wanderer.py` | `WandererClient`: CRUD members, idempotent add |
| `app/services/mapping.py` | `build_desired()` — pure function, ESI → desired members dict |
| `app/services/reconciler.py` | `reconcile()` — diff + apply, returns `RunResult` |
| `app/runner.py` | `run_forever()` asyncio loop + `run_once()`, SIGTERM handling |
| `app/cli.py` | click CLI: `run`, `once`, `preview`, `sso` |
| `Dockerfile` | Single-stage, uv-based image |
| `docker-compose.yml` | Single service, volumes for state + config |
| `tests/test_mapping.py` | `build_desired` unit tests |
| `tests/test_reconciler.py` | Reconciler diff logic, safety invariants, dry_run, idempotency |
| `tests/test_clients.py` | ESI ETag/304, token refresh, Wanderer idempotent add — all via respx |

---

## Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`, `app/clients/__init__.py`, `app/services/__init__.py`
- Create: `tests/__init__.py`
- Create: `.env.example`, `config.yaml.example`

- [ ] **Step 1: Initialize project with uv**

```bash
cd /home/hynrey/dev/eve-online/acl_wanderer_eve_sync
uv init --no-readme --name wanderer-acl-sync
rm -f hello.py  # uv init creates a stub
```

- [ ] **Step 2: Write pyproject.toml**

Replace the generated `pyproject.toml` with:

```toml
[project]
name = "wanderer-acl-sync"
version = "0.1.0"
description = "Sync EVE ESI Access Lists to Wanderer ACL"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "cryptography>=42",
    "tenacity>=8.3",
    "pyyaml>=6.0",
    "click>=8.1",
]

[project.scripts]
wacl-sync = "app.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

- [ ] **Step 3: Install dependencies**

```bash
uv sync
```

Expected: lock file created, `.venv` populated.

- [ ] **Step 4: Create package directories**

```bash
mkdir -p app/clients app/services tests
touch app/__init__.py app/clients/__init__.py app/services/__init__.py tests/__init__.py
```

- [ ] **Step 5: Write .env.example**

```bash
cat > .env.example << 'EOF'
ESI_CLIENT_ID=your_eve_app_client_id
ESI_CLIENT_SECRET=your_eve_app_client_secret
ESI_CALLBACK_URL=http://localhost:8765/callback
ESI_USER_AGENT=acl-wanderer-sync/1.0 (your@email.com)
FERNET_KEY=  # optional: generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
STATE_PATH=state.json
CONFIG_PATH=config.yaml
EOF
```

- [ ] **Step 6: Write config.yaml.example**

```bash
cat > config.yaml.example << 'EOF'
rules:
  - name: dark-side-main
    esi_character_id: 92576340
    esi_access_list_id: 1
    wanderer_base_url: https://wanderer.example.com
    wanderer_acl_id: 19712899-ec3a-47b1-b73b-2bae221c5513
    wanderer_acl_token: ${WANDERER_ACL_TOKEN}
    default_role: viewer
    blocked_role: blocked     # null to skip blocked entries
    protected_eve_ids: []
    interval_seconds: 300
    dry_run: false
EOF
```

- [ ] **Step 7: Verify structure**

```bash
find app tests -type f | sort
```

Expected output:
```
app/__init__.py
app/clients/__init__.py
app/services/__init__.py
tests/__init__.py
```

- [ ] **Step 8: Commit**

```bash
git init
git add pyproject.toml uv.lock app/ tests/ .env.example config.yaml.example
git commit -m "chore: scaffold project with uv, pyproject.toml, package structure"
```

---

## Task 2: Schemas

**Files:**
- Create: `app/schemas.py`

- [ ] **Step 1: Write app/schemas.py**

```python
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel


class EsiAccessType(str, Enum):
    unspecified = "Unspecified"
    allow = "allow"
    blocked = "blocked"


class EsiCharacterEntry(BaseModel):
    character_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiCorporationEntry(BaseModel):
    corporation_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiAllianceEntry(BaseModel):
    alliance_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiMembership(BaseModel):
    allow_everyone: bool = False
    characters: list[EsiCharacterEntry] = []
    corporations: list[EsiCorporationEntry] = []
    alliances: list[EsiAllianceEntry] = []


class AclEntryType(str, Enum):
    character = "character"
    corporation = "corporation"
    alliance = "alliance"


class AclEntryDTO(BaseModel):
    eve_id: int
    entry_type: AclEntryType
    access: EsiAccessType


class AccessListDTO(BaseModel):
    id: int
    name: str
    allow_everyone: bool
    entries: list[AclEntryDTO]

    @classmethod
    def from_esi_response(cls, data: dict) -> AccessListDTO:
        membership = EsiMembership(**data.get("membership", {}))
        entries: list[AclEntryDTO] = []
        for c in membership.characters:
            entries.append(AclEntryDTO(eve_id=c.character_id, entry_type=AclEntryType.character, access=c.access))
        for corp in membership.corporations:
            entries.append(AclEntryDTO(eve_id=corp.corporation_id, entry_type=AclEntryType.corporation, access=corp.access))
        for a in membership.alliances:
            entries.append(AclEntryDTO(eve_id=a.alliance_id, entry_type=AclEntryType.alliance, access=a.access))
        return cls(id=data["id"], name=data["name"], allow_everyone=membership.allow_everyone, entries=entries)


class WandererMemberDTO(BaseModel):
    eve_id: int
    entry_type: AclEntryType
    role: str

    @classmethod
    def from_wanderer_response(cls, data: dict) -> WandererMemberDTO:
        if "eve_character_id" in data:
            return cls(eve_id=int(data["eve_character_id"]), entry_type=AclEntryType.character, role=data["role"])
        if "eve_corporation_id" in data:
            return cls(eve_id=int(data["eve_corporation_id"]), entry_type=AclEntryType.corporation, role=data["role"])
        if "eve_alliance_id" in data:
            return cls(eve_id=int(data["eve_alliance_id"]), entry_type=AclEntryType.alliance, role=data["role"])
        raise ValueError(f"Cannot determine entity type from member data: {data}")


class WandererAclDTO(BaseModel):
    id: str
    members: list[WandererMemberDTO]
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from app.schemas import AccessListDTO, WandererAclDTO; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/schemas.py
git commit -m "feat: add ESI + Wanderer Pydantic DTOs"
```

---

## Task 3: Mapping Service (TDD)

**Files:**
- Create: `tests/test_mapping.py`
- Create: `app/services/mapping.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mapping.py
import logging
import pytest
from app.schemas import AccessListDTO, AclEntryDTO, AclEntryType, EsiAccessType
from app.services.mapping import build_desired


def _acl(entries, allow_everyone=False) -> AccessListDTO:
    return AccessListDTO(id=1, name="test", allow_everyone=allow_everyone, entries=entries)


def _entry(eve_id: int, entry_type: AclEntryType, access: EsiAccessType) -> AclEntryDTO:
    return AclEntryDTO(eve_id=eve_id, entry_type=entry_type, access=access)


def test_allow_gets_default_role():
    acl = _acl([_entry(100, AclEntryType.character, EsiAccessType.allow)])
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
    acl = _acl([
        _entry(100, AclEntryType.character, EsiAccessType.allow),
        _entry(999, AclEntryType.character, EsiAccessType.allow),
    ])
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids={999})
    assert 100 in result
    assert 999 not in result


def test_allow_everyone_logs_warning_but_syncs_entries(caplog):
    acl = _acl([_entry(100, AclEntryType.character, EsiAccessType.allow)], allow_everyone=True)
    with caplog.at_level(logging.WARNING):
        result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert "allow_everyone" in caplog.text
    assert 100 in result


def test_entry_types_preserved():
    acl = _acl([
        _entry(1, AclEntryType.character, EsiAccessType.allow),
        _entry(2, AclEntryType.corporation, EsiAccessType.allow),
        _entry(3, AclEntryType.alliance, EsiAccessType.allow),
    ])
    result = build_desired(acl, default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result[1].entry_type == AclEntryType.character
    assert result[2].entry_type == AclEntryType.corporation
    assert result[3].entry_type == AclEntryType.alliance


def test_empty_acl_returns_empty():
    result = build_desired(_acl([]), default_role="viewer", blocked_role="blocked", protected_eve_ids=set())
    assert result == {}
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_mapping.py -v
```

Expected: `ImportError: cannot import name 'build_desired' from 'app.services.mapping'` (module doesn't exist yet)

- [ ] **Step 3: Write app/services/mapping.py**

```python
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
) -> dict[int, DesiredMember]:
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
            role = default_role
        result[entry.eve_id] = DesiredMember(eve_id=entry.eve_id, entry_type=entry.entry_type, role=role)
    return result
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
uv run pytest tests/test_mapping.py -v
```

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/mapping.py tests/test_mapping.py
git commit -m "feat: add build_desired() mapping service with tests"
```

---

## Task 4: Config + State

**Files:**
- Create: `app/config.py`
- Create: `app/state.py`

- [ ] **Step 1: Write app/config.py**

```python
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuleConfig(BaseModel):
    name: str
    esi_character_id: int
    esi_access_list_id: int
    wanderer_base_url: str
    wanderer_acl_id: str
    wanderer_acl_token: str
    default_role: str = "viewer"
    blocked_role: str | None = "blocked"
    protected_eve_ids: list[int] = []
    interval_seconds: int = 300
    dry_run: bool = False


def _interpolate(obj: object) -> object:
    if isinstance(obj, str):
        def replace(m: re.Match) -> str:
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                raise ValueError(f"Environment variable {var!r} required by config but not set")
            return val
        return re.sub(r"\$\{([^}]+)\}", replace, obj)
    if isinstance(obj, dict):
        return {k: _interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate(i) for i in obj]
    return obj


def load_rules(config_path: Path) -> list[RuleConfig]:
    raw = yaml.safe_load(config_path.read_text())
    interpolated = _interpolate(raw)
    return [RuleConfig(**r) for r in interpolated["rules"]]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    esi_client_id: str
    esi_client_secret: str
    esi_callback_url: str = "http://localhost:8765/callback"
    esi_compatibility_date: str = "2026-05-19"
    esi_user_agent: str = "acl-wanderer-sync/1.0"
    fernet_key: str | None = None
    state_path: Path = Path("state.json")
    config_path: Path = Path("config.yaml")
```

- [ ] **Step 2: Write app/state.py**

```python
import json
import logging
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_path: Path, fernet_key: str | None = None) -> None:
        self._path = state_path
        self._fernet = Fernet(fernet_key.encode()) if fernet_key else None
        if not fernet_key:
            logger.warning("FERNET_KEY not set — storing tokens as plaintext. Set FERNET_KEY for production use.")
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {"tokens": {}, "rules": {}}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def _enc(self, value: str) -> str:
        if self._fernet:
            return self._fernet.encrypt(value.encode()).decode()
        return value

    def _dec(self, value: str) -> str:
        if self._fernet:
            return self._fernet.decrypt(value.encode()).decode()
        return value

    def get_token(self, character_id: int) -> dict | None:
        raw = self._data["tokens"].get(str(character_id))
        if raw is None:
            return None
        return {
            "refresh_token": self._dec(raw["refresh_token"]),
            "access_token": self._dec(raw["access_token"]) if raw.get("access_token") else None,
            "expires_at": raw["expires_at"],
        }

    def set_token(self, character_id: int, refresh_token: str, access_token: str | None, expires_at: float) -> None:
        self._data["tokens"][str(character_id)] = {
            "refresh_token": self._enc(refresh_token),
            "access_token": self._enc(access_token) if access_token else None,
            "expires_at": expires_at,
        }
        self._save()

    def get_etag(self, rule_name: str) -> str | None:
        return self._data["rules"].get(rule_name, {}).get("etag")

    def get_managed(self, rule_name: str) -> dict[str, dict]:
        return dict(self._data["rules"].get(rule_name, {}).get("managed", {}))

    def update_rule_state(self, rule_name: str, etag: str | None, managed: dict[str, dict]) -> None:
        if rule_name not in self._data["rules"]:
            self._data["rules"][rule_name] = {}
        if etag is not None:
            self._data["rules"][rule_name]["etag"] = etag
        self._data["rules"][rule_name]["managed"] = managed
        self._save()
```

- [ ] **Step 3: Verify imports**

```bash
uv run python -c "from app.config import Settings, load_rules; from app.state import StateManager; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/config.py app/state.py
git commit -m "feat: add Settings, load_rules, and StateManager"
```

---

## Task 5: SSO Client

**Files:**
- Create: `app/clients/sso.py`

- [ ] **Step 1: Write app/clients/sso.py**

```python
import base64
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ESI_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SCOPES = ["esi-access.read_lists.v1", "esi-activities.read_character.v1"]


class EsiAuthError(Exception):
    pass


def _basic_auth(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


async def refresh_token(client_id: str, client_secret: str, refresh_tok: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            ESI_TOKEN_URL,
            headers={"Authorization": _basic_auth(client_id, client_secret)},
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
        )
    if resp.status_code == 400 and resp.json().get("error") == "invalid_grant":
        raise EsiAuthError("Refresh token is invalid or revoked. Re-run: wacl-sync sso <character_id>")
    resp.raise_for_status()
    return resp.json()


async def get_valid_access_token(state, character_id: int, client_id: str, client_secret: str) -> str:
    token = state.get_token(character_id)
    if token is None:
        raise EsiAuthError(f"No token for character {character_id}. Run: wacl-sync sso {character_id}")
    if token["access_token"] and token["expires_at"] - time.time() > 60:
        return token["access_token"]
    data = await refresh_token(client_id, client_secret, token["refresh_token"])
    expires_at = time.time() + data["expires_in"] - 30
    state.set_token(character_id, data["refresh_token"], data["access_token"], expires_at)
    return data["access_token"]


def enroll(character_id: int, client_id: str, client_secret: str, callback_url: str, state) -> None:
    """One-shot SSO enrollment. Opens browser, waits for callback, saves token."""
    import secrets

    state_param = secrets.token_urlsafe(16)
    parsed = urllib.parse.urlparse(callback_url)
    port = parsed.port or 8765

    auth_url = ESI_AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": callback_url,
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "state": state_param,
    })

    code_holder: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code_holder["code"] = qs.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *args):
            pass

    print(f"Opening browser for EVE SSO...\n{auth_url}")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", port), _Handler)
    server.handle_request()

    code = code_holder.get("code")
    if not code:
        raise EsiAuthError("No authorization code received from SSO callback")

    resp = httpx.post(
        ESI_TOKEN_URL,
        headers={"Authorization": _basic_auth(client_id, client_secret)},
        data={"grant_type": "authorization_code", "code": code},
    )
    resp.raise_for_status()
    data = resp.json()
    expires_at = time.time() + data["expires_in"] - 30
    state.set_token(character_id, data["refresh_token"], data["access_token"], expires_at)
    print(f"Token for character {character_id} saved.")
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from app.clients.sso import EsiAuthError, get_valid_access_token, enroll; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/clients/sso.py
git commit -m "feat: add ESI SSO client (refresh flow + enroll)"
```

---

## Task 6: ESI Client

**Files:**
- Create: `app/clients/esi.py`

- [ ] **Step 1: Write app/clients/esi.py**

```python
import logging
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.schemas import AccessListDTO

logger = logging.getLogger(__name__)

ESI_BASE = "https://esi.evetech.net"


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (420, 429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


class EsiClient:
    def __init__(self, user_agent: str, compatibility_date: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=ESI_BASE,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "X-Compatibility-Date": compatibility_date,
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "EsiClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    def _check_error_limit(self, response: httpx.Response) -> None:
        remain = response.headers.get("X-ESI-Error-Limit-Remain")
        if remain and int(remain) == 0:
            reset = int(response.headers.get("X-ESI-Error-Limit-Reset", 60))
            logger.warning("ESI error limit reached, backing off %ds", reset)
            raise httpx.HTTPStatusError(
                "ESI error limit reached", request=response.request, response=response
            )

    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def get_access_list(
        self,
        character_id: int,
        access_list_id: int,
        token: str,
        etag: Optional[str] = None,
    ) -> tuple[Optional[AccessListDTO], Optional[str]]:
        """
        Returns (AccessListDTO, new_etag) or (None, old_etag) on 304.
        """
        headers = {"Authorization": f"Bearer {token}"}
        if etag:
            headers["If-None-Match"] = etag

        resp = await self._client.get(
            f"/characters/{character_id}/access-lists/{access_list_id}",
            headers=headers,
        )

        if resp.status_code == 304:
            return None, etag

        self._check_error_limit(resp)
        resp.raise_for_status()

        new_etag = resp.headers.get("ETag")
        return AccessListDTO.from_esi_response(resp.json()), new_etag
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from app.clients.esi import EsiClient; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/clients/esi.py
git commit -m "feat: add EsiClient with ETag/304 support and error-limit backoff"
```

---

## Task 7: Wanderer Client

**Files:**
- Create: `app/clients/wanderer.py`

- [ ] **Step 1: Write app/clients/wanderer.py**

```python
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
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from app.clients.wanderer import WandererClient; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/clients/wanderer.py
git commit -m "feat: add WandererClient with idempotent add (409 → update)"
```

---

## Task 8: Client Tests

**Files:**
- Create: `tests/test_clients.py`

- [ ] **Step 1: Write tests/test_clients.py**

```python
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from app.clients.esi import EsiClient, ESI_BASE
from app.clients.sso import EsiAuthError, ESI_TOKEN_URL, get_valid_access_token
from app.clients.wanderer import WandererClient
from app.schemas import AclEntryType
from app.state import StateManager

WANDERER_BASE = "http://wanderer.test"


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    data = {
        "tokens": {
            "123": {
                "refresh_token": "valid_refresh",
                "access_token": "expired_token",
                "expires_at": 1.0,
            }
        },
        "rules": {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    return StateManager(p, fernet_key=None)


# --- ESI ---

@pytest.mark.asyncio
async def test_esi_parses_access_list():
    payload = {
        "id": 1,
        "name": "Main List",
        "membership": {
            "allow_everyone": False,
            "characters": [{"character_id": 100, "access": "Unspecified"}],
            "corporations": [],
            "alliances": [],
        },
    }
    async with respx.mock:
        respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(200, json=payload, headers={"ETag": '"etag1"'})
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            result, etag = await client.get_access_list(123, 1, "token")

    assert result is not None
    assert len(result.entries) == 1
    assert result.entries[0].eve_id == 100
    assert etag == '"etag1"'


@pytest.mark.asyncio
async def test_esi_304_returns_none_with_original_etag():
    async with respx.mock:
        respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(304)
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            result, etag = await client.get_access_list(123, 1, "token", etag='"etag1"')

    assert result is None
    assert etag == '"etag1"'


@pytest.mark.asyncio
async def test_esi_sends_if_none_match():
    async with respx.mock:
        route = respx.get(f"{ESI_BASE}/characters/123/access-lists/1").mock(
            return_value=httpx.Response(304)
        )
        async with EsiClient("test-agent", "2026-05-19") as client:
            await client.get_access_list(123, 1, "token", etag='"cached"')

    assert route.calls[0].request.headers["If-None-Match"] == '"cached"'


# --- SSO token refresh ---

@pytest.mark.asyncio
async def test_sso_refresh_on_expired_token(state: StateManager):
    async with respx.mock:
        respx.post(ESI_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "new_access",
                "refresh_token": "new_refresh",
                "expires_in": 1200,
            })
        )
        token = await get_valid_access_token(state, 123, "client_id", "client_secret")

    assert token == "new_access"
    saved = state.get_token(123)
    assert saved["access_token"] == "new_access"
    assert saved["refresh_token"] == "new_refresh"


@pytest.mark.asyncio
async def test_sso_raises_esi_auth_error_on_invalid_grant(state: StateManager):
    async with respx.mock:
        respx.post(ESI_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(EsiAuthError):
            await get_valid_access_token(state, 123, "client_id", "client_secret")


@pytest.mark.asyncio
async def test_sso_uses_cached_token_when_fresh(tmp_path: Path):
    future_expires = time.time() + 3600
    data = {
        "tokens": {
            "123": {
                "refresh_token": "rtoken",
                "access_token": "fresh_token",
                "expires_at": future_expires,
            }
        },
        "rules": {},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    state = StateManager(p, fernet_key=None)

    async with respx.mock:
        token = await get_valid_access_token(state, 123, "client_id", "client_secret")
        assert respx.calls.call_count == 0

    assert token == "fresh_token"


# --- Wanderer ---

@pytest.mark.asyncio
async def test_wanderer_parses_members():
    payload = {
        "id": "acl-uuid",
        "members": [
            {"eve_character_id": "100", "role": "viewer"},
            {"eve_corporation_id": "200", "role": "admin"},
        ],
    }
    async with respx.mock:
        respx.get(f"{WANDERER_BASE}/api/acls/acl-uuid").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            acl = await client.get_acl("acl-uuid")

    assert len(acl.members) == 2
    assert acl.members[0].eve_id == 100
    assert acl.members[0].entry_type == AclEntryType.character
    assert acl.members[1].eve_id == 200
    assert acl.members[1].entry_type == AclEntryType.corporation


@pytest.mark.asyncio
async def test_wanderer_add_idempotent_on_409():
    async with respx.mock:
        respx.post(f"{WANDERER_BASE}/api/acls/acl-uuid/members").mock(
            return_value=httpx.Response(409)
        )
        respx.put(f"{WANDERER_BASE}/api/acls/acl-uuid/members/100").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            await client.add_member("acl-uuid", 100, AclEntryType.character, "viewer")


@pytest.mark.asyncio
async def test_wanderer_remove_member():
    async with respx.mock:
        respx.delete(f"{WANDERER_BASE}/api/acls/acl-uuid/members/100").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with WandererClient(WANDERER_BASE, "token") as client:
            await client.remove_member("acl-uuid", 100)
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_clients.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_clients.py
git commit -m "test: add client tests (ESI ETag/304, token refresh, Wanderer idempotent add)"
```

---

## Task 9: Reconciler (TDD)

**Files:**
- Create: `tests/test_reconciler.py`
- Create: `app/services/reconciler.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_reconciler.py
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
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_reconciler.py -v
```

Expected: `ImportError` (reconciler doesn't exist yet)

- [ ] **Step 3: Write app/services/reconciler.py**

```python
import logging
import time
from dataclasses import dataclass, field

from app.clients.sso import EsiAuthError, get_valid_access_token
from app.schemas import AclEntryType
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

    state.update_rule_state(rule.name, new_etag, new_managed)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Rule %s: added=%d updated=%d removed=%d skipped=%d errors=%d time=%dms",
        rule.name, result.added, result.updated, result.removed, result.skipped, len(result.errors), elapsed_ms,
    )

    return result
```

- [ ] **Step 4: Run tests — all pass**

```bash
uv run pytest tests/test_reconciler.py -v
```

Expected: 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/reconciler.py tests/test_reconciler.py
git commit -m "feat: add reconciler with diff logic; test safety invariants"
```

---

## Task 10: Runner

**Files:**
- Create: `app/runner.py`

- [ ] **Step 1: Write app/runner.py**

```python
import asyncio
import logging
import signal

from app.clients.esi import EsiClient
from app.clients.wanderer import WandererClient
from app.services.reconciler import reconcile

logger = logging.getLogger(__name__)


async def run_forever(rules, settings, state) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: (logger.info("Shutdown signal received"), stop_event.set()))

    logger.info("Starting sync loop for %d rule(s)", len(rules))

    async with EsiClient(settings.esi_user_agent, settings.esi_compatibility_date) as esi:
        while not stop_event.is_set():
            for rule in rules:
                if stop_event.is_set():
                    break
                async with WandererClient(rule.wanderer_base_url, rule.wanderer_acl_token) as wanderer:
                    await reconcile(state, rule, settings, esi, wanderer)

            interval = min(r.interval_seconds for r in rules)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
            except asyncio.TimeoutError:
                pass

    logger.info("Sync loop stopped cleanly")


async def run_once(rules, settings, state) -> bool:
    all_ok = True
    async with EsiClient(settings.esi_user_agent, settings.esi_compatibility_date) as esi:
        for rule in rules:
            async with WandererClient(rule.wanderer_base_url, rule.wanderer_acl_token) as wanderer:
                result = await reconcile(state, rule, settings, esi, wanderer)
                if result.status == "error":
                    all_ok = False
    return all_ok
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from app.runner import run_forever, run_once; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/runner.py
git commit -m "feat: add run_forever (asyncio loop + SIGTERM) and run_once"
```

---

## Task 11: CLI

**Files:**
- Create: `app/cli.py`

- [ ] **Step 1: Write app/cli.py**

```python
import asyncio
import logging
import sys

import click

from app.config import Settings, load_rules
from app.state import StateManager


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


@click.group()
def cli() -> None:
    pass


@cli.command()
def run() -> None:
    """Long-running sync loop."""
    _setup_logging()
    settings = Settings()
    rules = load_rules(settings.config_path)
    state = StateManager(settings.state_path, settings.fernet_key)
    from app.runner import run_forever
    asyncio.run(run_forever(rules, settings, state))


@cli.command()
def once() -> None:
    """Run all rules once. Exit code 1 if any rule errored."""
    _setup_logging()
    settings = Settings()
    rules = load_rules(settings.config_path)
    state = StateManager(settings.state_path, settings.fernet_key)
    from app.runner import run_once
    ok = asyncio.run(run_once(rules, settings, state))
    sys.exit(0 if ok else 1)


@cli.command()
@click.argument("rule_name", required=False)
def preview(rule_name: str | None) -> None:
    """Dry-run: print sync plan without touching Wanderer."""
    _setup_logging()
    settings = Settings()
    rules = load_rules(settings.config_path)
    if rule_name:
        rules = [r for r in rules if r.name == rule_name]
        if not rules:
            click.echo(f"Rule {rule_name!r} not found", err=True)
            sys.exit(1)
    state = StateManager(settings.state_path, settings.fernet_key)

    from app.clients.esi import EsiClient
    from app.clients.wanderer import WandererClient
    from app.services.reconciler import reconcile

    async def _run() -> None:
        async with EsiClient(settings.esi_user_agent, settings.esi_compatibility_date) as esi:
            for rule in rules:
                async with WandererClient(rule.wanderer_base_url, rule.wanderer_acl_token) as wanderer:
                    result = await reconcile(state, rule, settings, esi, wanderer, dry_run=True)
                    click.echo(
                        f"{rule.name}: add={result.added} update={result.updated} "
                        f"remove={result.removed} skip={result.skipped}"
                    )

    asyncio.run(_run())


@cli.command()
@click.argument("character_id", type=int)
def sso(character_id: int) -> None:
    """Enroll a character via EVE SSO (one-time setup)."""
    _setup_logging()
    settings = Settings()
    state = StateManager(settings.state_path, settings.fernet_key)
    from app.clients.sso import enroll
    enroll(character_id, settings.esi_client_id, settings.esi_client_secret, settings.esi_callback_url, state)


def main() -> None:
    cli()
```

- [ ] **Step 2: Verify CLI entrypoint**

```bash
uv run wacl-sync --help
```

Expected: shows `run`, `once`, `preview`, `sso` commands.

- [ ] **Step 3: Commit**

```bash
git add app/cli.py
git commit -m "feat: add CLI (run, once, preview, sso)"
```

---

## Task 12: Docker

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY app/ app/

ENTRYPOINT ["uv", "run", "wacl-sync"]
CMD ["run"]
```

- [ ] **Step 2: Write docker-compose.yml**

```yaml
services:
  wacl-sync:
    build: .
    restart: unless-stopped
    command: run
    volumes:
      - ./state.json:/app/state.json
      - ./config.yaml:/app/config.yaml:ro
    environment:
      ESI_CLIENT_ID: ${ESI_CLIENT_ID}
      ESI_CLIENT_SECRET: ${ESI_CLIENT_SECRET}
      FERNET_KEY: ${FERNET_KEY:-}
    # To use with systemd-timer/cron instead of long-running loop:
    #   command: once
    #   restart: "no"
```

- [ ] **Step 3: Write .dockerignore**

```
.venv/
__pycache__/
*.pyc
.env
state.json
.git/
tests/
docs/
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore
git commit -m "feat: add Dockerfile (uv-based) and docker-compose"
```

---

## Task 13: Full Test Run + Verification

- [ ] **Step 1: Run all tests**

```bash
uv run pytest -v
```

Expected: all tests green, no warnings.

- [ ] **Step 2: Verify CLI help**

```bash
uv run wacl-sync --help
uv run wacl-sync run --help
uv run wacl-sync once --help
uv run wacl-sync preview --help
uv run wacl-sync sso --help
```

Expected: all commands shown with descriptions.

- [ ] **Step 3: Verify package imports cleanly**

```bash
uv run python -c "
from app.cli import main
from app.config import Settings, load_rules
from app.state import StateManager
from app.schemas import AccessListDTO, WandererAclDTO
from app.clients.esi import EsiClient
from app.clients.wanderer import WandererClient
from app.clients.sso import get_valid_access_token, enroll
from app.services.mapping import build_desired
from app.services.reconciler import reconcile, RunResult
from app.runner import run_forever, run_once
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: verify all tests pass and imports clean"
```
