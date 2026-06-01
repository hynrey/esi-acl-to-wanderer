# Deployment — cron-pull CD

Continuous deployment with **zero secrets in GitHub and zero inbound ports**. The
server polls the public repo on a cron interval; when `main` moves, it rebuilds and
restarts the container. Because the repo is public, the pull is anonymous HTTPS — no
SSH key, no deploy token.

```
GitHub (public repo)  ──HTTPS pull──>  server cron  ──>  scripts/deploy.sh  ──>  docker compose up -d --build
```

## Why this is the safe option

- **No GitHub Secrets.** Nothing of yours lives in GitHub Actions.
- **No inbound port.** The server only makes outbound HTTPS to github.com — nothing
  listens for a webhook or SSH deploy.
- A compromised server cannot push to the repo (it only ever pulls, anonymously).
- The only secrets on the box are the runtime ones in `.env` (`WANDERER_ACL_TOKEN`,
  `FERNET_KEY`, ESI creds) — unrelated to the deploy path.

Trade-off: deploys land within the cron interval (e.g. up to 2 min after a push),
not instantly. Fine for a sync daemon.

## One-time server setup

```bash
# 1. Clone (HTTPS — anonymous, no key needed for a public repo)
sudo git clone https://github.com/hynrey/esi-acl-to-wanderer.git /opt/esi-acl-to-wanderer
cd /opt/esi-acl-to-wanderer

# 2. Configure secrets + rules (NOT committed)
cp .env.example .env
$EDITOR .env          # ESI_CLIENT_ID/SECRET, FERNET_KEY, WANDERER_ACL_TOKEN
cp config.yaml.example config.yaml
$EDITOR config.yaml   # character/acl ids, roles, interval

# 3. Enroll the ESI character once (writes state.json). See docs/SETUP.md step 7.
#    Run it wherever you can complete the EVE SSO, then copy state.json onto the server,
#    or run the container's sso command interactively.

# 4. First boot
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` mounts `state.json` and `config.yaml` as volumes, so they survive
every rebuild/restart.

## Enable cron-pull

```bash
crontab -e
```

Add (checks every 2 minutes, logs to a file):

```cron
*/2 * * * * /opt/esi-acl-to-wanderer/scripts/deploy.sh >> /var/log/esi-acl-deploy.log 2>&1
```

That's it. On each tick `scripts/deploy.sh`:

1. `git fetch` origin/main
2. compares local vs remote SHA — **exits silently if unchanged** (no rebuild churn)
3. on change: `git pull --ff-only` → `docker compose up -d --build` → prunes old images
4. uses `flock` so a slow build never overlaps the next tick

## Watch it

```bash
tail -f /var/log/esi-acl-deploy.log     # deploy events
docker compose logs -f                  # the sync service itself
docker compose ps                       # container health
```

## Manual deploy / rollback

```bash
# Force a deploy now (don't wait for cron):
/opt/esi-acl-to-wanderer/scripts/deploy.sh

# Roll back to a previous commit:
cd /opt/esi-acl-to-wanderer
git checkout <good-sha>
docker compose up -d --build
# (cron will fast-forward you back to main on the next push; pin a tag/branch if you
#  need to stay on the rollback.)
```

## Notes

- `deploy.sh` is **fast-forward only** — it never rewrites history on the server. If
  `git pull --ff-only` fails, someone edited files in place; reconcile manually
  (`git status`, `git stash` or `git reset --hard origin/main` if the change was junk).
- Keep `FERNET_KEY` stable across deploys — rotating it makes the stored refresh token
  undecryptable and forces a re-enroll (`wacl-sync sso`).
- For a non-resident alternative (run `once` from a systemd-timer instead of a resident
  container), see the commented block in `docker-compose.yml`.
