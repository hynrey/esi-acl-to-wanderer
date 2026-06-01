# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`wanderer-acl-sync` — a lean Python service that one-way syncs an EVE Online **ESI
Access List** into a **Wanderer ACL**. No DB, no broker. ESI is the source of truth;
the service reconciles the Wanderer ACL to match it without touching members it didn't
add. State is a single `state.json`.

## Commands

`uv` is **not on PATH** on this machine — it lives at `~/.pyenv/versions/3.13.0/bin/uv`.

```bash
~/.pyenv/versions/3.13.0/bin/uv sync                       # install deps + editable project
~/.pyenv/versions/3.13.0/bin/uv run pytest                 # all tests (no network; respx-mocked)
~/.pyenv/versions/3.13.0/bin/uv run pytest tests/test_reconciler.py::test_manual_member_never_removed -v   # single test
~/.pyenv/versions/3.13.0/bin/uv run wacl-sync <run|once|preview|sso>   # CLI
```

If the `wacl-sync` console script fails with `ModuleNotFoundError: No module named 'app'`,
the editable install didn't register — run `uv sync --reinstall-package esi-acl-to-wanderer`.

## Architecture

Data flows in one direction: **ESI → reconcile → Wanderer**.

```
cli.py ──> runner.py ──> services/reconciler.py ──> services/mapping.py (pure)
                              │                          │
                       clients/esi.py            schemas.py (DTOs)
                       clients/wanderer.py
                       clients/sso.py
                       state.py (state.json)     config.py (Settings + rules)
```

- **`services/reconciler.py`** is the core. `reconcile()` fetches the ESI list,
  builds the desired set via `build_desired()`, diffs against the live Wanderer ACL,
  and applies add/update/remove. It returns a `RunResult` (`added/updated/removed/
  skipped/errors/status`).
- **`services/mapping.py`** — `build_desired()` is a **pure function** (no I/O); all
  role-mapping logic lives here and is the most heavily unit-tested piece.
- **`clients/`** — three async `httpx` clients. `esi.py` handles ETag/304 +
  error-limit backoff (tenacity). `wanderer.py` does ACL member CRUD. `sso.py` does the
  OAuth refresh flow and one-time `enroll()`.
- **`state.py`** — `StateManager` reads/writes `state.json` atomically (tmp +
  `os.replace`, chmod `0600`) and Fernet-encrypts tokens when `FERNET_KEY` is set.
- **`runner.py`** — `run_forever()` (asyncio loop, SIGTERM-clean) and `run_once()`.

## The safety invariant (do not break this)

The service must **never modify or remove a Wanderer member it did not add**, nor
anything in `protected_eve_ids`. This is enforced structurally in `reconcile()`:

- `to_remove` and `to_update` iterate **only** `managed_ids` (the set persisted in
  `state.json` under the rule's `managed`). A member absent from `managed` cannot enter
  either set.
- `protected_eve_ids` are dropped in `build_desired()` **and** excluded from
  `to_remove`.
- On ESI `304` (`acl_dto is None`) `reconcile()` returns **before** calling
  `wanderer.get_acl()` — a stale ESI cache hit can't trigger mutations.

`tests/test_reconciler.py` locks these down (`test_manual_member_never_removed`,
`test_protected_member_never_removed`, `test_esi_304_skips_reconcile_*`,
`test_partial_failure_keeps_failed_member_managed`). Keep them green.

Errors are isolated at two levels: per-member failures degrade a run to `partial`;
per-rule unhandled errors are caught in `runner.py` so one bad rule never crashes the
long-running loop. The reconciler also does **not** advance the stored ETag on a
`partial` run, so failed members are retried next cycle instead of being hidden behind
a 304.

## External API specifics (these bit us — the original spec guessed them wrong)

- **ESI scope:** only `esi-access.read_lists.v1`. `esi-activities.read_character.v1` is
  invalid and SSO rejects it.
- **ESI `access` enum:** `Unspecified`, `Allowed`, `Blocked`, `Manager`, `Admin`
  (capitalized; `Manager`/`Admin` characters-only). See `EsiAccessType` in `schemas.py`.
- **Wanderer responses** use a JSON:API `{"data": {...}}` envelope — `get_acl()` unwraps
  it. Member objects carry an internal `id` plus `eve_*_id` (as **strings**) and `role`.
- **Wanderer roles** are **lowercase** in the API (`admin`, `manager`, `member`,
  `viewer`, `blocked`) even though the UI shows them capitalized / `-blocked-`.
- **ACL id** comes from the Wanderer UI URL `…/access-lists/<uuid>` (not the map id).

## Config & env gotchas

- `config.yaml` `${VAR}` interpolation resolves against `os.environ`, **not** `.env`
  (pydantic's `.env` only feeds `Settings`). `WANDERER_ACL_TOKEN` must be exported, or
  prefixed on the command, or `source`d in.
- Quote `.env` values containing parens (`ESI_USER_AGENT`) — they break bash `source`.
- WSL/headless SSO: `enroll()` prints the URL and accepts a manually pasted redirect
  URL / `code=` value as a fallback when the localhost callback can't be reached.

## Testing conventions

- `pytest-asyncio` in `auto` mode; tests are `async def` with `@pytest.mark.asyncio`.
- HTTP is mocked with `respx` (`tests/test_clients.py`) — tests never hit the network.
- Reconciler tests use `AsyncMock` clients and a real `StateManager` on `tmp_path`.
- Follow TDD for `mapping.py`/`reconciler.py` changes: add the failing test first.

## Docs

`docs/SETUP.md` is the full first-time walkthrough (EVE app → SSO → run). `README.md`
is the overview + quick start. `docs/tasks/task-001.md` is the original Sprint 1 spec
(in Russian); `docs/superpowers/` holds the design + plan.
