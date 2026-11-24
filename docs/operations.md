# Operations Runbook (local stack)

Commands assume the repository root and a configured `.env`. Nothing here deletes
financial data. The few commands that remove data are marked **DESTRUCTIVE**.

## 1. Start, stop, restart

| Task | Command | Data |
|---|---|---|
| Start everything (build, migrate once, apply DB roles) | `docker compose up -d --build --wait` | kept |
| Stop | `docker compose stop` | kept |
| Restart one service | `docker compose restart api-1` | kept |
| Remove containers | `docker compose down` | kept (volumes stay) |
| **DESTRUCTIVE**: remove containers and all data | `docker compose down -v` | **deleted** |

`migrate` and `rabbitmq-monitor-setup` are one-shot jobs and show `exited (0)`.
`docker compose up --wait` exits non-zero if either fails; the API and workers keep running.

## 2. Backups and restore drills

```powershell
python scripts/db_backup.py backup                      # dev database -> backups/<db>-<ts>.dump
python scripts/db_backup.py verify backups\<file>.dump   # restore into a throwaway server, verify
python scripts/db_backup.py restore-drill               # both
```

- The dump is taken inside a REPEATABLE READ snapshot, and the row counts in the manifest
  (`.json` next to the dump) come from that same snapshot, so they match even while traffic
  continues.
- `verify` starts a throwaway `postgres:16-alpine` container with no published port and no
  volume, restores with `--exit-on-error`, then checks:
  - row counts per table;
  - sum of all postings = 0;
  - every account balance = its postings;
  - the append-only triggers came back;
  - the Alembic version.

  The container is always removed afterwards.
- `backups/` is gitignored. Dumps contain all data, so treat them like the database.
- **Rolling a real environment back** (not automated on purpose):
  1. Restore into a NEW database: `createdb ledgerflow_restored`, then `pg_restore -d ledgerflow_restored <dump>`.
  2. Run `verify` checks against it.
  3. Run `python -m app.core.db_roles` with `POSTGRES_DB=ledgerflow_restored`.
  4. Stop the writers, switch `POSTGRES_DB`, and start them again.

  The script never restores over a live database.

## 3. Retention

```powershell
docker compose --profile maintenance run --rm retention --dry-run   # counts only
docker compose --profile maintenance run --rm retention             # delete
```

It deletes only finished lab runs older than `RETENTION_LAB_RUN_DAYS` (default 30) and
worker heartbeat rows older than `RETENTION_HEARTBEAT_DAYS` (default 7). It runs as the
runtime role, which cannot delete financial or audit rows.

## 4. Dead-letter queues

| Queue | Holds |
|---|---|
| `ledgerflow.ledger-audit.dlq` | events the audit consumer could not apply |
| `ledgerflow.anomaly-detector.dlq` | events the AI worker could not score |
| `ledgerflow.events.unrouted.dlq` | broker dead letters without a routing policy (should stay empty) |

- **Where to look:** the live topology (System Topology → RabbitMQ) shows per-queue depth
  and "Dead-lettered (all DLQs)" from the independent management-API probe. The management
  UI is at http://127.0.0.1:15672.
- **Why a message failed:** the `x-ledgerflow-error` header holds the reason, and
  `x-ledgerflow-consumer` names the consumer. Broker dead letters carry
  `x-last-death-reason=delivery_limit`.
- **Replay after fixing the cause:**
  - Move the messages back with the management UI's "Move messages" (shovel plugin) or a
    one-off script.
  - Consumers deduplicate by event id, so a replay can never apply an event twice.
- **Policies:** check them with
  `docker exec ledgerflow-rabbitmq rabbitmqctl list_policies`. Each consumer re-applies its
  own policy at start-up.

## 5. Database roles

- The `migrate` job re-applies runtime grants after every upgrade.
- To check or repair by hand, run `docker compose run --rm migrate` (idempotent).
- If the job reports `tables without a runtime privilege policy`, a new migration added a
  table. Add it to `TABLE_PRIVILEGES` in `app/core/db_roles.py`, deciding on the minimal
  rights.

## 6. Model updates

```powershell
python -m app.ai.train             # retrain the active version (deterministic)
python -m app.ai.evaluate          # refresh app/ai/model/evaluation.json
docker compose up -d --build ai-worker
```

- A new version means a new pair of files plus an entry in `VERSIONS` and a new
  `ACTIVE_VERSION`.
- The worker refuses an artifact whose SHA-256 or scikit-learn version does not match its
  metadata.
- Watch `/api/v1/ai/drift` after deploying.

## 7. Recovery cheatsheet

| Symptom | Likely cause | Action |
|---|---|---|
| Transfers return 503 `RATE_LIMITER_UNAVAILABLE` | Redis down | `docker compose start redis` (writes fail closed by design) |
| Transfers fine, events not arriving | broker or publisher down | check the topology page; `docker compose start rabbitmq outbox-publisher`; the outbox drains on its own |
| 502/504 from NGINX right after a full redeploy | all replicas in `fail_timeout` | wait 2 s or redeploy rolling |
| Worker container `unhealthy` | heartbeat file stale (hung loop or DB unreachable) | `docker compose logs <worker>`; `docker compose restart <worker>` |
| Lab says "controller unavailable" | `python -m labctl` not running, or the containerized frontend is in use | run the controller and use `npm run dev` |
| Failure-lab journal left behind | controller killed mid-experiment | restart `python -m labctl`: it restores what the journal lists |
