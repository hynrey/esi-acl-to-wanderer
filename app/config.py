import os
import re
from pathlib import Path
from typing import Any

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
    role_map: dict[str, str] = {}  # ESI access level (e.g. "Admin") -> Wanderer role
    protected_eve_ids: list[int] = []
    interval_seconds: int = 300
    dry_run: bool = False


def _interpolate(obj: Any) -> Any:
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
