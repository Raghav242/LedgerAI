import uuid

# Fixed namespace for LedgerFlow event ids (never change it: ids must stay stable).
EVENT_NAMESPACE = uuid.UUID("6c0b6f3e-6a3f-4d4c-9b1f-2b8f8f4a7e01")


def event_id_for(event_type: str, operation_id: uuid.UUID) -> uuid.UUID:
    """Deterministic event id: the same operation always yields the same event id.

    One committed money movement therefore maps to exactly one event id, the outbox
    primary key makes a second row for it impossible, and anyone holding a transfer id
    can compute its event id (the worker-crash demo relies on that).
    """
    return uuid.uuid5(EVENT_NAMESPACE, f"{event_type}:{operation_id}")
