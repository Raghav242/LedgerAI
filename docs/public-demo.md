# Public Demo: a Safe, Reduced Design

**Status: designed and runnable locally; not deployed.** The repository publishes nothing.

The development stack is deliberately powerful: it mints money (`/fund`), runs load
experiments, exposes internal topology, and (on the host) can stop containers. A public
demo must keep the impressive parts, such as real transfers, idempotency and the ledger,
without any of that.

## 1. What a visitor may do

| Capability | Development | Public demo |
|---|---|---|
| Create accounts | unlimited | rate-limited (global write bucket: 5 burst, 1 per 2 s) |
| Mint money (`POST /accounts/{id}/fund`) | yes | **no** (route not mounted; startup refuses it) |
| Transfers between accounts | yes | yes, within the strict global quota |
| History, ledger, reconciliation | yes | yes (read quota 60 burst) |
| Experiment Lab, failure lab, benchmarks | yes | **no** (dev surface not mounted; controller absent) |
| Live topology, AI results, Performance Lab | yes | **no** (they reveal internals) |
| Replica header `X-Served-By` | yes | **no** |
| Database / Redis / RabbitMQ ports | loopback | **none published** |

## 2. How it is enforced

`docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait`

- `APP_ENV=production`: `Settings` **refuses to start** with:
  - development endpoints;
  - the replica header;
  - `DB_ECHO`;
  - the in-memory limiter;
  - consumer fault injection.

  So a misconfigured demo fails loudly instead of running unsafely.
- `GATEWAY_BIND=${DEMO_BIND}` is passed to the replicas. If someone re-enabled dev
  endpoints on a public bind, startup is refused a second time, independently.
- Data stores publish no ports (`ports: !reset []`). Only NGINX (API) and the frontend are
  reachable, on `DEMO_BIND`, which defaults to 127.0.0.1.
- The frontend image has no `/controller` route, and `labctl` never runs.
- The API runs as the least-privilege runtime role, and the ledger is append-only at three
  levels: application, triggers and grants.
- Tests: `test_the_public_demo_profile_exposes_no_data_store_and_no_dev_surface`, plus the
  production-mode tests in `tests/unit/test_hardening.py`.

## 3. The missing piece: where does demo money come from?

Without `/fund`, new accounts start at zero, so a visitor cannot move money. Options, from
safest:

1. **Seeded read-mostly tenant (recommended).**
   - A one-off job, run by the operator and not exposed over HTTP, creates a handful of
     funded demo accounts through the normal ledger path. Their opening balances are
     ordinary `dev_funding` transfers, so the ledger stays balanced.
   - Visitors transfer only between those accounts, under the strict quota.
   - A nightly job restores the seed state from a verified backup (`scripts/db_backup.py`).
2. **Per-visitor sandbox.** An anonymous session token becomes the principal (the
   idempotency and rate-limit scope already come from `get_current_principal`), and each
   visitor gets their own small seeded accounts. This needs authentication-lite and
   per-principal quotas (Phase 7 item).
3. **Capped self-funding.** A `POST /demo/claim` gives at most $50 per principal per day.
   It is still minting, so it needs per-visitor identity and abuse controls. Not
   recommended.

## 4. Required before any internet exposure

| Item | Why |
|---|---|
| TLS termination (Caddy or a cloud load balancer) in front of NGINX | credentials and traffic in clear text otherwise |
| Secrets from the platform (not `.env`), rotated | dev defaults in `.env.example` are public |
| Authentication or anonymous session principals + per-principal quotas | one global quota lets one visitor starve everyone |
| Request logging without bodies; log retention | privacy |
| Backups + restore drill on a schedule | recovery |
| Host firewall: only 443 open | defense in depth |
| Image scanning in CI (`security.yml`) passing on the deployed digests | supply chain |

## 5. Verified locally

The profile was started as a separate Compose project with its own ports and volumes, then
checked. Results are in
[phase-6b-6d.md](phase-6b-6d.md#deployment-and-public-demo).
