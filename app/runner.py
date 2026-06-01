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
                # Guard every rule: an unhandled error (e.g. ESI 401/403, Wanderer 5xx)
                # must not crash the loop — log it and keep the service alive so the
                # other rules and the next cycle still run.
                try:
                    async with WandererClient(rule.wanderer_base_url, rule.wanderer_acl_token) as wanderer:
                        await reconcile(state, rule, settings, esi, wanderer)
                except Exception:
                    logger.exception("Rule %s crashed this cycle; continuing", rule.name)

            interval = min(r.interval_seconds for r in rules)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
            except TimeoutError:
                pass

    logger.info("Sync loop stopped cleanly")


async def run_once(rules, settings, state) -> bool:
    all_ok = True
    async with EsiClient(settings.esi_user_agent, settings.esi_compatibility_date) as esi:
        for rule in rules:
            # Same guard for the one-shot path so one bad rule doesn't abort the rest;
            # any failure still flips the exit code for cron/systemd.
            try:
                async with WandererClient(rule.wanderer_base_url, rule.wanderer_acl_token) as wanderer:
                    result = await reconcile(state, rule, settings, esi, wanderer)
                    if result.status == "error":
                        all_ok = False
            except Exception:
                logger.exception("Rule %s crashed", rule.name)
                all_ok = False
    return all_ok
