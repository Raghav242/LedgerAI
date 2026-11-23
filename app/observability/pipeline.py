"""Event pipeline read model (Phase 5C): outbox -> publisher -> RabbitMQ -> consumers.

Read-only and bounded: aggregates (one row each), keyset-paginated event pages (at most
100 rows), and single-event lookups. Event payloads are not returned; only the amount
and the transfer id are surfaced.

Everything here is PERSISTED state: outbox rows, consumer receipts (processed_events),
the audit consumer's effect rows and the workers' heartbeats. Timestamps are PostgreSQL
`now()` values, i.e. the start of the transaction that wrote each row.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import DomainError, TransferNotFoundError
from app.events.ids import event_id_for
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED
from app.models import Transfer, TransferKind
from app.observability.schemas import (
    ConsumerStats,
    EventConsumption,
    EventLifecycle,
    EventPage,
    EventSummary,
    LifecycleStage,
    PipelineSummary,
    QueueDepth,
)
from app.observability.tracing import trace_id_of

StatusFilter = Literal["all", "unpublished", "published", "retrying"]
MAX_PAGE = 100
LAG_SAMPLE = 500


class EventNotFoundError(DomainError):
    code = "EVENT_NOT_FOUND"


class InvalidCursorError(DomainError):
    code = "INVALID_CURSOR"


def expected_consumers(settings: Settings) -> list[str]:
    """Consumer names whose receipts the UI should expect for an event."""
    names = []
    workers = settings.observability_expected_workers
    if "event-consumer" in workers:
        names.append(settings.consumer_name)
    if "anomaly-detector" in workers:
        names.append("anomaly-detector")
    return names


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000, 1)


_EVENT_COLUMNS = """
    o.id, o.event_type, o.event_version, o.aggregate_id, o.payload->>'transfer_id' AS transfer_id,
    (o.payload->>'amount_minor')::bigint AS amount_minor, o.correlation_id, o.trace_parent,
    o.status, o.attempts, o.last_error, o.created_at, o.available_at, o.published_at,
    coalesce((
        SELECT json_agg(json_build_object(
                   'consumer', p.consumer_name,
                   'processed_at', p.processed_at,
                   'delivery_attempt', (SELECT a.delivery_attempt FROM event_audit_log a
                                        WHERE a.event_id = p.event_id
                                          AND a.consumer_name = p.consumer_name
                                        ORDER BY a.id LIMIT 1))
                   ORDER BY p.processed_at)
        FROM processed_events p WHERE p.event_id = o.id), '[]'::json) AS consumers
"""


def _summary(row: Any) -> EventSummary:
    return EventSummary(
        id=str(row.id),
        event_type=row.event_type,
        event_version=row.event_version,
        aggregate_id=str(row.aggregate_id),
        transfer_id=row.transfer_id,
        amount_minor=row.amount_minor,
        correlation_id=row.correlation_id,
        trace_id=trace_id_of(row.trace_parent),
        status=row.status,
        attempts=row.attempts,
        last_error=(row.last_error or None) and row.last_error[:300],
        created_at=row.created_at,
        available_at=row.available_at,
        published_at=row.published_at,
        consumers=[
            EventConsumption(
                consumer=c["consumer"],
                processed_at=datetime.fromisoformat(c["processed_at"]),
                delivery_attempt=c.get("delivery_attempt"),
            )
            for c in row.consumers
        ],
    )


class PipelineReader:
    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # --- aggregate view ---------------------------------------------------------------------

    def summary(self) -> PipelineSummary:
        s = self._session
        totals = s.execute(
            text(
                """
                SELECT
                  count(*) FILTER (WHERE status = 'pending') AS pending,
                  count(*) FILTER (WHERE status = 'processing') AS processing,
                  count(*) FILTER (WHERE status = 'published') AS published,
                  count(*) FILTER (WHERE status <> 'published'
                                     AND last_error IS NOT NULL) AS awaiting_retry,
                  count(*) FILTER (WHERE status = 'published' AND attempts > 1) AS republished,
                  min(created_at) FILTER (WHERE status <> 'published') AS oldest_unpublished,
                  count(*) FILTER (WHERE published_at > now() - interval '5 minutes')
                      AS published_5m,
                  now() AS db_now
                FROM outbox_events
                """
            )
        ).one()
        lag = s.execute(
            text(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_ms) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_ms) AS p95
                FROM (SELECT extract(epoch FROM published_at - created_at) * 1000 AS lag_ms
                      FROM outbox_events WHERE status = 'published'
                      ORDER BY published_at DESC LIMIT :n) recent
                """
            ),
            {"n": LAG_SAMPLE},
        ).one()
        heartbeats = list(
            s.execute(
                text(
                    "SELECT component, details, last_error, last_error_at, beat_at, "
                    "next_beat_before < now() AS stale FROM worker_heartbeats "
                    "WHERE beat_at > now() - interval '1 day' ORDER BY beat_at DESC"
                )
            ).mappings()
        )
        receipts = {
            row.consumer_name: row
            for row in s.execute(
                text(
                    "SELECT consumer_name, count(*) AS n, max(processed_at) AS last_at "
                    "FROM processed_events GROUP BY consumer_name"
                )
            )
        }
        redelivered = dict(
            s.execute(
                text(
                    "SELECT consumer_name, count(*) FROM event_audit_log "
                    "WHERE delivery_attempt > 1 GROUP BY consumer_name "
                    "UNION ALL SELECT 'anomaly-detector', count(*) FROM anomaly_results "
                    "WHERE delivery_attempt > 1"
                )
            ).all()
        )
        publisher = next((h for h in heartbeats if h["component"] == "outbox-publisher"), None)
        consumer_beats: dict[str, Any] = {}
        for beat in heartbeats:
            name = (beat["details"] or {}).get("consumer")
            if name and name not in consumer_beats:
                consumer_beats[name] = beat
        names = sorted(
            set(expected_consumers(self._settings)) | set(receipts) | set(consumer_beats)
        )
        consumers = []
        for name in names:
            beat = consumer_beats.get(name)
            details = (beat or {}).get("details") or {}
            receipt = receipts.get(name)
            consumers.append(
                ConsumerStats(
                    consumer=name,
                    component=beat["component"] if beat else "unknown",
                    events_processed=receipt.n if receipt else 0,
                    last_processed_at=receipt.last_at if receipt else None,
                    effects_after_redelivery=redelivered.get(name),
                    since_start=details.get("counts"),
                    queues=[QueueDepth(**q) for q in details.get("queues", [])],
                    reported_at=beat["beat_at"] if beat else None,
                    report_stale=beat["stale"] if beat else None,
                )
            )
        oldest = totals.oldest_unpublished
        return PipelineSummary(
            observed_at=totals.db_now,
            outbox_pending=totals.pending,
            outbox_processing=totals.processing,
            outbox_published=totals.published,
            awaiting_retry=totals.awaiting_retry,
            republished_events=totals.republished,
            oldest_unpublished_at=oldest,
            oldest_unpublished_age_seconds=(
                round((totals.db_now - oldest).total_seconds(), 1) if oldest else None
            ),
            published_last_5m=totals.published_5m,
            publish_lag_ms_p50=round(lag.p50, 1) if lag.p50 is not None else None,
            publish_lag_ms_p95=round(lag.p95, 1) if lag.p95 is not None else None,
            publisher_last_error=publisher["last_error"] if publisher else None,
            publisher_last_error_at=publisher["last_error_at"] if publisher else None,
            consumers=consumers,
        )

    # --- event pages ------------------------------------------------------------------------

    def events(
        self,
        *,
        status: StatusFilter,
        event_type: str | None,
        correlation_id: str | None,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
    ) -> EventPage:
        where = ["true"]
        params: dict[str, Any] = {"limit": min(limit, MAX_PAGE) + 1}
        if status == "unpublished":
            where.append("o.status <> 'published'")
        elif status == "published":
            where.append("o.status = 'published'")
        elif status == "retrying":
            where.append("o.status <> 'published' AND o.last_error IS NOT NULL")
        if event_type:
            where.append("o.event_type = :event_type")
            params["event_type"] = event_type
        if correlation_id:
            where.append("o.correlation_id = :correlation_id")
            params["correlation_id"] = correlation_id
        if cursor:
            where.append("(o.created_at, o.id) < (:cursor_at, :cursor_id)")
            params["cursor_at"], params["cursor_id"] = cursor
        rows = list(
            self._session.execute(
                text(
                    f"SELECT {_EVENT_COLUMNS} FROM outbox_events o WHERE {' AND '.join(where)} "
                    "ORDER BY o.created_at DESC, o.id DESC LIMIT :limit"
                ),
                params,
            )
        )
        page = rows[: min(limit, MAX_PAGE)]
        next_cursor = None
        if len(rows) > len(page) and page:
            last = page[-1]
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        return EventPage(items=[_summary(r) for r in page], next_cursor=next_cursor)

    def _event(self, event_id: uuid.UUID) -> EventSummary | None:
        row = self._session.execute(
            text(f"SELECT {_EVENT_COLUMNS} FROM outbox_events o WHERE o.id = :id"),
            {"id": event_id},
        ).first()
        return _summary(row) if row else None

    # --- one money movement, end to end ----------------------------------------------------

    def lifecycle_for_event(self, event_id: uuid.UUID) -> EventLifecycle:
        event = self._event(event_id)
        if event is None:
            raise EventNotFoundError("Event not found", {"event_id": str(event_id)})
        transfer = (
            self._session.get(Transfer, uuid.UUID(event.transfer_id)) if event.transfer_id else None
        )
        return self._lifecycle(transfer, event)

    def lifecycle_for_transfer(self, transfer_id: uuid.UUID) -> EventLifecycle:
        transfer = self._session.get(Transfer, transfer_id)
        if transfer is None:
            raise TransferNotFoundError("Transfer not found", {"transfer_id": str(transfer_id)})
        event_type = {
            TransferKind.TRANSFER: TRANSFER_COMPLETED,
            TransferKind.DEV_FUNDING: ACCOUNT_FUNDED,
        }.get(transfer.kind)
        event = self._event(event_id_for(event_type, transfer.id)) if event_type else None
        return self._lifecycle(transfer, event)

    def _lifecycle(self, transfer: Transfer | None, event: EventSummary | None) -> EventLifecycle:
        consumers = expected_consumers(self._settings)
        stages: list[LifecycleStage] = []
        if transfer is not None:
            stages.append(
                LifecycleStage(
                    id="commit",
                    label="Transaction committed",
                    state="observed",
                    at=transfer.created_at,
                    detail="Balances, both ledger entries, the idempotency outcome and the "
                    "outbox row became durable in one COMMIT (time = transaction start).",
                )
            )
        if event is None:
            reason = (
                "This movement has no event (it predates the outbox, migration 0003)."
                if transfer is not None and transfer.kind != TransferKind.LEGACY_OPENING_BALANCE
                else "Legacy opening balances publish no event."
            )
            for stage_id, label in (
                ("outbox", "Outbox row"),
                ("publish", "Published to RabbitMQ"),
            ):
                stages.append(
                    LifecycleStage(id=stage_id, label=label, state="not_applicable", detail=reason)
                )
        else:
            stages.append(
                LifecycleStage(
                    id="outbox",
                    label="Outbox row written",
                    state="observed",
                    at=event.created_at,
                    detail=f"{event.event_type} v{event.event_version}, in the same transaction.",
                )
            )
            stages.append(self._publish_stage(event))
            receipts = {c.consumer: c for c in event.consumers}
            for name in sorted(set(consumers) | set(receipts)):
                stages.append(self._consumer_stage(name, event, receipts.get(name)))
        return EventLifecycle(
            transfer_id=str(transfer.id) if transfer else event.transfer_id if event else None,
            transfer_kind=str(transfer.kind) if transfer else None,
            transfer_created_at=transfer.created_at if transfer else None,
            event=event,
            expected_consumers=consumers,
            stages=stages,
        )

    @staticmethod
    def _publish_stage(event: EventSummary) -> LifecycleStage:
        if event.status == "published":
            return LifecycleStage(
                id="publish",
                label="Published to RabbitMQ",
                state="observed",
                at=event.published_at,
                duration_ms=_ms(event.created_at, event.published_at),
                detail=f"Broker confirmed after {event.attempts} publish attempt(s).",
            )
        if event.status == "processing":
            return LifecycleStage(
                id="publish",
                label="Published to RabbitMQ",
                state="pending",
                detail=f"Claimed by a publisher (attempt {event.attempts}); awaiting the confirm.",
            )
        if event.last_error:
            return LifecycleStage(
                id="publish",
                label="Published to RabbitMQ",
                state="failed",
                at=event.available_at,
                detail=f"Attempt {event.attempts} failed ({event.last_error[:120]}). "
                "Safe in the outbox; the next attempt is scheduled for the time shown.",
            )
        return LifecycleStage(
            id="publish",
            label="Published to RabbitMQ",
            state="pending",
            detail="Committed and waiting for the publisher.",
        )

    @staticmethod
    def _consumer_stage(
        name: str, event: EventSummary, receipt: EventConsumption | None
    ) -> LifecycleStage:
        label = f"Consumed by {name}"
        if receipt is not None:
            attempt = (
                f" on delivery attempt {receipt.delivery_attempt}"
                if receipt.delivery_attempt
                else ""
            )
            since_publish = _ms(event.published_at, receipt.processed_at)
            note = ""
            if since_publish is not None and since_publish < 0:
                # published_at is written AFTER the broker confirm, in its own transaction;
                # a fast consumer can commit first. No meaningful duration then.
                since_publish = None
                note = " Processed before the publisher recorded its broker confirm."
            return LifecycleStage(
                id=f"consume:{name}",
                label=label,
                state="observed",
                at=receipt.processed_at,
                duration_ms=since_publish,
                detail=f"Receipt and effect committed together{attempt}; "
                f"redeliveries are recognised by event id.{note}",
            )
        return LifecycleStage(
            id=f"consume:{name}",
            label=label,
            state="pending",
            detail="Waiting for the consumer."
            if event.status == "published"
            else "Waits until the event is published.",
        )
