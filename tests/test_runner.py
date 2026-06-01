import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import RuleConfig
from app.runner import run_once
from app.state import StateManager


def _settings():
    s = MagicMock()
    s.esi_client_id = "cid"
    s.esi_client_secret = "csec"
    s.esi_user_agent = "test-agent"
    s.esi_compatibility_date = "2026-05-19"
    return s


def _rule(name: str) -> RuleConfig:
    return RuleConfig(
        name=name,
        esi_character_id=123,
        esi_access_list_id=1,
        wanderer_base_url="http://w.test",
        wanderer_acl_id="acl-uuid",
        wanderer_acl_token="tok",
    )


def _state(tmp_path: Path) -> StateManager:
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"tokens": {}, "rules": {}}))
    return StateManager(p, fernet_key=None)


@pytest.mark.asyncio
async def test_run_once_one_rule_crash_does_not_abort_others(tmp_path: Path):
    """An unhandled exception in one rule must not stop the other rule, and must
    flip the exit-code bool to False."""
    state = _state(tmp_path)
    rules = [_rule("good"), _rule("bad")]
    calls = []

    async def fake_reconcile(state, rule, settings, esi, wanderer, dry_run=False):
        calls.append(rule.name)
        if rule.name == "bad":
            raise RuntimeError("wanderer exploded")
        result = MagicMock()
        result.status = "ok"
        return result

    with (
        patch("app.runner.reconcile", side_effect=fake_reconcile),
        patch("app.runner.EsiClient", return_value=AsyncMock()),
        patch("app.runner.WandererClient", return_value=AsyncMock()),
    ):
        ok = await run_once(rules, _settings(), state)

    assert ok is False  # bad rule flips exit code
    assert "good" in calls  # good rule still ran
    assert "bad" in calls  # bad rule was attempted


@pytest.mark.asyncio
async def test_run_once_all_ok_returns_true(tmp_path: Path):
    state = _state(tmp_path)
    rules = [_rule("a"), _rule("b")]

    async def fake_reconcile(state, rule, settings, esi, wanderer, dry_run=False):
        result = MagicMock()
        result.status = "ok"
        return result

    with (
        patch("app.runner.reconcile", side_effect=fake_reconcile),
        patch("app.runner.EsiClient", return_value=AsyncMock()),
        patch("app.runner.WandererClient", return_value=AsyncMock()),
    ):
        ok = await run_once(rules, _settings(), state)

    assert ok is True
