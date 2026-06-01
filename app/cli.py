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
