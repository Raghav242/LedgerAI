"""What an experiment gets to work with, and shared helpers for building results.

Split of responsibilities:
  - SETUP and VERIFICATION (create/fund lab accounts, read balances, reconcile) call the
    existing services in-process. They are not what is being measured, and doing them
    over HTTP would spend the run's rate-limit tokens before the experiment starts.
  - The MEASURED traffic goes over real HTTP to LAB_TARGET_BASE_URL (NGINX in the stack),
    carrying the run's signed lab token: load balancing, rate limiting, idempotency and
    locking all happen exactly as for any client.
"""

import asyncio
import statistics
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.rate_limit import RateLimits
from app.core.config import Settings
from app.core.principal import LabRateMode
from app.lab.recorder import RunRecorder
from app.lab.schemas import LabCheck, LabMetric
from app.lab.signing import LabTrafficSigner
from app.lab.traffic import LabHttp, Observation
from app.models import Account, Transfer, TransferKind
from app.services.account_service import AccountService
from app.services.reconciliation_service import ReconciliationReport, ReconciliationService

TransportFactory = Callable[[], httpx.AsyncBaseTransport]
REQUEST_TIMEOUT_SECONDS = 20


@dataclass
class ExperimentOutcome:
    summary: str
    explanation: list[str]
    metrics: list[LabMetric]
    checks: list[LabCheck]
    outcome_counts: dict[str, int] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


class ExperimentContext:
    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        recorder: RunRecorder,
        session_factory: sessionmaker[Session],
        settings: Settings,
        rate_limits: RateLimits,
        signer: LabTrafficSigner,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.run_id = run_id
        self.recorder = recorder
        self.settings = settings
        self.rate_limits = rate_limits
        self._session_factory = session_factory
        self._signer = signer
        self._transport_factory = transport_factory

    @property
    def short_id(self) -> str:
        return self.run_id.hex[:8]

    @property
    def limiter_backend(self) -> str:
        return type(self.rate_limits.limiter).__name__

    # --- in-process setup and verification ------------------------------------------------

    def _accounts(self, session: Session) -> AccountService:
        return AccountService(session, lock_timeout_ms=self.settings.transfer_lock_timeout_ms)

    async def create_account(self, label: str) -> str:
        """A uniquely named account that only this run ever touches."""

        def create() -> str:
            with self._session_factory() as session:
                account = self._accounts(session).create_account(
                    f"lab-{self.short_id}-{label}", "USD"
                )
                return str(account.id)

        return await asyncio.to_thread(create)

    async def fund(self, account_id: str, amount_minor: int) -> None:
        def fund() -> None:
            with self._session_factory() as session:
                self._accounts(session).fund_account(
                    uuid.UUID(account_id), amount_minor, correlation_id=f"lab-{self.short_id}"
                )

        await asyncio.to_thread(fund)

    async def balance(self, account_id: str) -> int:
        def read() -> int:
            with self._session_factory() as session:
                return session.get_one(Account, uuid.UUID(account_id)).balance_minor

        return await asyncio.to_thread(read)

    async def transfer_count(self, account_id: str) -> int:
        """Merchant transfers (not fundings) touching this account, read from PostgreSQL."""

        def count() -> int:
            account = uuid.UUID(account_id)
            with self._session_factory() as session:
                return (
                    session.scalar(
                        select(func.count())
                        .select_from(Transfer)
                        .where(
                            Transfer.kind == TransferKind.TRANSFER,
                            or_(
                                Transfer.source_account_id == account,
                                Transfer.destination_account_id == account,
                            ),
                        )
                    )
                    or 0
                )

        return await asyncio.to_thread(count)

    async def reconcile(self) -> ReconciliationReport:
        def run() -> ReconciliationReport:
            with self._session_factory() as session:
                return ReconciliationService(session).run()

        return await asyncio.to_thread(run)

    # --- measured HTTP traffic --------------------------------------------------------------

    @asynccontextmanager
    async def http(self, mode: LabRateMode, *, max_connections: int) -> AsyncIterator[LabHttp]:
        limits = httpx.Limits(
            max_connections=max_connections, max_keepalive_connections=max_connections
        )
        transport = self._transport_factory() if self._transport_factory else None
        async with httpx.AsyncClient(
            base_url=self.settings.lab_target_base_url,
            transport=transport,
            limits=limits,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={self._signer.header: self._signer.token(self.run_id, mode)},
        ) as client:
            yield LabHttp(client)


# --- result helpers ---------------------------------------------------------------------


def check(
    check_id: str,
    label: str,
    ok: bool | None,
    observed: str,
    description: str | None = None,
) -> LabCheck:
    """ok=None means the check does not apply to this run (e.g. a single replica)."""
    status = "skipped" if ok is None else ("pass" if ok else "fail")
    return LabCheck(
        id=check_id, label=label, status=status, observed=observed, description=description
    )


def ledger_checks(report: ReconciliationReport, account_ids: Iterable[str]) -> list[LabCheck]:
    mismatched = {str(m.account_id) for m in report.balance_mismatches}
    ours = [a for a in account_ids if a in mismatched]
    return [
        check(
            "ledger_matches_balances",
            "Lab accounts: stored balance == sum of ledger entries",
            not ours,
            "all match" if not ours else f"mismatch on {len(ours)} account(s)",
            "Every balance change was posted to the double-entry ledger.",
        ),
        check(
            "reconciliation",
            "Global reconciliation passes",
            report.ok,
            f"ledger total {report.ledger_total_minor}, balance total "
            f"{report.balance_total_minor}, {len(report.balance_mismatches)} mismatches, "
            f"{len(report.unbalanced_transfer_ids)} unbalanced transfers "
            f"({report.accounts_checked} accounts, {report.ledger_entries_checked} entries)",
            "All entries sum to zero and every account matches its ledger.",
        ),
    ]


def latency_summary(observations: Iterable[Observation]) -> dict[str, float]:
    values = sorted(o.latency_ms for o in observations if o.status is not None)
    if not values:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0}
    p95_index = max(0, min(len(values) - 1, round(0.95 * len(values)) - 1))
    return {
        "p50": round(statistics.median(values), 1),
        "p95": round(values[p95_index], 1),
        "max": round(values[-1], 1),
    }


def usd(minor: int) -> str:
    return f"${minor / 100:,.2f}"


def replica_label(counts: dict[str, int]) -> str:
    if not counts:
        return "no replica header"
    return ", ".join(f"{name} ({n})" for name, n in sorted(counts.items()))
