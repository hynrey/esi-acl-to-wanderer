## Sprint 1: Синхронизация EVE ESI Access List → Wanderer ACL (lean, без БД/брокера)

**Цель:** один лёгкий сервис периодически читает ESI Access List (источник истины) и приводит к нему Wanderer ACL, не затирая участников, добавленных в Wanderer вручную. Никаких Celery/Redis/PostgreSQL — состояние в одном JSON-файле.

---

### Контекст и архитектурные решения

> Однонаправленный sync ESI → Wanderer. Планировщик — обычный `asyncio`-цикл (`reconcile → sleep(interval)`), он же умеет `--once` для systemd-timer/cron. Один процесс ⇒ прогоны последовательны ⇒ распределённый лок не нужен. Состояние (refresh-токен, набор «наших» членов, ETag) — атомарно пишется в один JSON-файл. БД нет.

**Стек / окружение:**

- Python 3.12, httpx (async), pydantic v2 + pydantic-settings, cryptography (Fernet — только для refresh-токена на диске), tenacity (retry/backoff, опционально). Всё.
- Запуск: один Docker-сервис (long-running цикл) ИЛИ `--once` под systemd-timer/cron. Деплой рядом с уже хостящимся Wanderer.
- SSO-enroll: одноразовый CLI, ловит OAuth-callback стдлибным `http.server` на localhost — без FastAPI и веб-слоя.

**Соглашения:**

- HTTP-клиенты ESI и Wanderer — отдельные async-классы на общем паттерне (retry, маскирование секретов в логах).
- Pydantic v2 для парсинга ответов ESI/Wanderer и конфига; `model_config = ConfigDict(from_attributes=True)` где нужно.
- Состояние читается/пишется через единый модуль `state.py`: загрузка в память на старте, атомарная запись (tmp + `os.replace`), файл с правами `0600`.
- refresh-токен на диске шифруется Fernet, если задан `FERNET_KEY`; иначе — plaintext с предупреждением в логе. Токены/секреты никогда не логировать.

**Запрещено:**

- Тащить Celery, Redis, PostgreSQL, SQLAlchemy, Alembic, FastAPI, APScheduler — ничего из этого не нужно.
- Синхронные HTTP-вызовы в async-цикле.
- Удалять/менять в Wanderer ACL членов, которых нет в нашем `managed`-наборе, и из `protected_eve_ids`.
- Любая запись обратно в ESI (только чтение) и двунаправленный sync.

---

#### Внешние API (справочно)

**Источник — EVE ESI Access List** (новый ESI, версия через `X-Compatibility-Date`, без `/latest/`):

```
GET https://esi.evetech.net/characters/{character_id}/access-lists/{access_list_id}
Headers: Accept: application/json | Authorization: Bearer <access_token>
         X-Compatibility-Date: 2026-05-19 | If-None-Match: <etag>
Scopes:  esi-access.read_lists.v1, esi-activities.read_character.v1
```

Ответ:

```
{ "id":1, "name":"...", "description":"...",
  "membership": {
    "allow_everyone": true,
    "characters":   [{ "access":"Unspecified", "character_id":90000001 }],
    "corporations": [{ "access":"Unspecified", "corporation_id":98777771 }],
    "alliances":    [{ "access":"Unspecified", "alliance_id":99000001 }] } }
```

> ⚠️ Значения `access` (видели `"Unspecified"`; ожидаются allow/block-варианты) и list-эндпоинт `GET /characters/{id}/access-lists/` подтвердить по живому Swagger с `X-Compatibility-Date: 2026-05-19`.

**Цель — Wanderer ACL** (`Authorization: Bearer <ACL_API_TOKEN>`, `member_id` в путях = внешний EVE id):

```
GET    /api/acls/:id                      # ACL + members[]
POST   /api/acls/:acl_id/members          # { "member": { "eve_character_id"|"eve_corporation_id"|"eve_alliance_id":"<id>", "role":"<role>" } }
PUT    /api/acls/:acl_id/members/:eve_id   # { "member": { "role":"<role>" } }
DELETE /api/acls/:acl_id/members/:eve_id   # -> { "ok": true }
```

> ⚠️ Точный набор ролей Wanderer (видели `admin`, `viewer`) и роль для allow / для blocked — подтвердить в UI ACL, вынести в конфиг.

---

#### Конфиг и состояние (без БД)

**`config.yaml`** — список правил (секреты через `${ENV}`-интерполяцию, в репозиторий не коммитятся):

```
rules:
  - name: dark-side-main
    esi_character_id: 92576340
    esi_access_list_id: 1
    wanderer_base_url: https://wanderer.example.com
    wanderer_acl_id: 19712899-ec3a-47b1-b73b-2bae221c5513
    wanderer_acl_token: ${WANDERER_ACL_TOKEN}
    default_role: viewer
    blocked_role: blocked        # или null -> blocked-записи пропускать
    protected_eve_ids: [11111111]
    interval_seconds: 300
    dry_run: false
```

**`state.json`** — единственный persist-файл:

```
{ "tokens": { "92576340": { "refresh_token": "<enc>", "access_token":"<enc>", "expires_at": 0 } },
  "rules":  { "dark-side-main": { "etag": "...", "managed": { "<eve_id>": {"type":"character","role":"viewer","last_seen":"..."} } } } }
```

`managed` = ровно те, кого добавил sync. Только их sync вправе менять/удалять.

---

#### Маппинг ESI → Wanderer

- Тип 1:1: `characters`→`eve_character_id`, `corporations`→`eve_corporation_id`, `alliances`→`eve_alliance_id`.
- `access` allow/Unspecified → `default_role`; `blocked` → `blocked_role` или пропуск (`skipped`).
- `allow_everyone=true` через ACL-members API не выразить (нет сущности «everyone») → синкаем только явные записи, факт логируем как warning.
- `protected_eve_ids` исключаются из desired (роль не навязываем, не удаляем).

---

### Задачи

#### Фаза A — Каркас

- [ ] **S1.1** `pyproject.toml` + структура пакета `wanderer-acl-sync` (`app/`, `app/clients/`, `app/services/`, `tests/`). Зависимости: httpx, pydantic, pydantic-settings, cryptography, tenacity. Без db/celery/redis/fastapi.
- [ ] **S1.2** `app/config.py`: `Settings` (env: `esi_client_id/secret`, `esi_callback_url`, `esi_compatibility_date`=`2026-05-19`, `esi_user_agent`, `fernet_key?`, `state_path`, `config_path`) + загрузка/валидация `config.yaml` в список `RuleConfig` (pydantic) с `${ENV}`-интерполяцией.
- [ ] **S1.3** `app/state.py`: загрузка `state.json` в память, атомарная запись (tmp+`os.replace`, chmod `0600`); геттеры/сеттеры для token-блока, `etag` и `managed` по имени правила; helpers `enc()/dec()` (Fernet при наличии ключа, иначе passthrough + warn).
- [ ] **S1.4** `app/schemas.py`: DTO `AccessListDTO`/`AclEntryDTO` (парсер `membership.*` + `allow_everyone` в плоский `list[AclEntryDTO]`), `WandererMemberDTO`/`WandererAclDTO`.

#### Фаза B — ESI-клиент

- [ ] **S1.5** `app/clients/sso.py`: refresh-flow `POST https://login.eveonline.com/v2/oauth/token` (Basic client_id:secret).
  - `get_valid_access_token(state, character_id) -> str`: вернуть кэш из state, если не протух (запас ~60с), иначе обновить и сохранить.
  - При `400 invalid_grant` — кинуть `EsiAuthError` (правило уйдёт в error, остальные живут).
- [ ] **S1.6** `app/clients/esi.py`: `EsiClient` на `httpx.AsyncClient`.
  - Всегда `X-Compatibility-Date`, `User-Agent`, `Accept`.
  - `get_access_list(character_id, access_list_id, token, etag) -> tuple[AccessListDTO|None, etag]`: поддержать `If-None-Match` и `304` (вернуть `None` = не изменилось).
  - error-limit: читать `X-ESI-Error-Limit-Remain/-Reset`, тормозить у нуля; tenacity-retry на 5xx/420/429.
- [ ] **S1.7** `app/clients/sso.py::enroll()` (одноразовый): сгенерить SSO-URL c нужными scope, поднять стдлибный `http.server` на `localhost:<port>` для одного `?code=`, обменять на refresh-токен, записать в `state.json`, выйти.

#### Фаза C — Wanderer-клиент

- [ ] **S1.8** `app/clients/wanderer.py`: `WandererClient(base_url, acl_token)`.
  - `get_acl(acl_id) -> WandererAclDTO` (парсить `members[]`, у каждого один из `eve_*_id` + `role`).
  - `add_member/update_member_role/remove_member` по эндпоинтам выше.
  - `add_member` идемпотентен: на существующем члене не падать (свести к update роли / no-op).

#### Фаза D — Реконсилер

- [ ] **S1.9** `app/services/mapping.py`: чистая `build_desired(entries, rule) -> dict[eve_id, DesiredMember]` (allow/blocked/unspecified, `allow_everyone`-warning, исключить `protected_eve_ids`).
- [ ] **S1.10** `app/services/reconciler.py`: `async reconcile(state, rule, esi, wanderer, dry_run) -> RunResult`.
  - `desired` из ESI (учесть `304` по ETag → если не изменилось и прошлый прогон ок, можно пропустить тело, но всё равно сверить с Wanderer не чаще, чем нужно).
  - `current` из `get_acl`, разбить на «наши» (есть в `managed`) и чужие (игнор).
  - diff: `to_add` / `to_update` (роль отличается) / `to_remove` (в `managed`, нет в desired). **Чужих и `protected_eve_ids` в `to_remove` не класть никогда.**
  - `dry_run=True` → вернуть план, ничего не вызывая.
  - Применять: сначала add/update, потом remove; обновлять `managed` и `etag` в state; ошибка на отдельном члене → `partial`, не валит прогон.
    > ⚠️ Логировать каждую операцию; считать added/updated/removed/skipped.

#### Фаза E — Запуск

- [ ] **S1.11** `app/runner.py`: `async run_forever(interval)` — цикл `for rule: reconcile(); sleep до следующего тика по rule.interval_seconds`. Аккуратная остановка по SIGINT/SIGTERM.
- [ ] **S1.12** `app/cli.py`: команды `run` (forever), `once` (один прогон всех правил, exit-code≠0 при error — для systemd/cron), `preview [rule]` (dry-run, печать плана), `sso <character_id>` (enroll, S1.7).
- [ ] **S1.13** `Dockerfile` + `docker-compose.yml`: **один** сервис (`command: run`), volume на `state.json` и `config.yaml`, env с секретами. В comment — альтернатива: образ + `command: once` под внешним systemd-timer/cron.

#### Фаза F — Тесты/логи

- [ ] **S1.14** Логи: на каждый прогон строка `rule / added/updated/removed/skipped / ms`, маскирование секретов.
- [ ] **S1.15** `tests/test_mapping.py`: `build_desired` (allow/blocked/unspecified, `allow_everyone`, `protected_eve_ids`).
- [ ] **S1.16** `tests/test_reconciler.py` (моки клиентов): add/update/remove; **ручной член (нет в `managed`) не удаляется**; `dry_run` ничего не вызывает; идемпотентность (второй прогон = 0/0/0).
- [ ] **S1.17** `tests/test_clients.py` (`httpx.MockTransport`): ESI 304/ETag + refresh при протухшем токене + error-limit backoff; Wanderer идемпотентный add + разбор `members[]`.

---

### Definition of Done

- [ ] `wacl-sync sso 92576340` проводит EVE SSO и пишет в `state.json` зашифрованный refresh-токен; файл `0600`; токенов в логах нет.
- [ ] `wacl-sync preview dark-side-main` печатает план `{added, updated, removed, skipped}` на реальном ESI Access List, **ничего не меняя** в Wanderer.
- [ ] `wacl-sync once` приводит Wanderer ACL к составу ESI ACL: новый char/corp/alliance из ESI появляется с `default_role`; ушедший из ESI — удаляется; изменённая роль — обновляется.
- [ ] Ручной член Wanderer ACL (нет в `managed`) и член из `protected_eve_ids` **не удаляются и не меняются** ни при каком прогоне (покрыто `test_reconciler.py`).
- [ ] Повторный `once` без изменений в ESI → `added=0, updated=0, removed=0`.
- [ ] `wacl-sync run` крутит цикл с интервалом из правила; SIGTERM завершает чисто; `docker compose up` поднимает **один** контейнер, переживает рестарт (state на volume).
- [ ] `allow_everyone=true` не ломает прогон: явные записи синкаются, факт — warning в логе.
- [ ] Протухший refresh-токен не валит сервис: правило → error с понятным сообщением, остальные правила работают; `once` возвращает ненулевой код.
- [ ] `pytest` зелёный.

---

> 🔲 Подтвердить перед стартом: (1) enum `access` и наличие `GET /characters/{id}/access-lists/` по Swagger `X-Compatibility-Date: 2026-05-19`; (2) роли Wanderer и маппинг allow/blocked; (3) сколько правил на старте (одно — можно вообще без `config.yaml`, через env).
