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
