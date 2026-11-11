import uuid

from app.services.idempotency import request_fingerprint
from app.services.transfer_service import CREATE_TRANSFER_OPERATION, TransferCommand

SOURCE = uuid.UUID("6f1b8b7e-3c61-4a5e-9d0c-8f0e7b3a2c11")
DESTINATION = uuid.UUID("0a9e2d4c-5b7f-4e1a-8c3d-2f6b9e1d7a55")


def _fingerprint(command: TransferCommand) -> str:
    return request_fingerprint(CREATE_TRANSFER_OPERATION, command.canonical_payload())


def test_fingerprint_is_stable_and_hex_sha256() -> None:
    command = TransferCommand(SOURCE, DESTINATION, 1_000, "USD")
    assert _fingerprint(command) == _fingerprint(TransferCommand(SOURCE, DESTINATION, 1_000, "USD"))
    assert len(_fingerprint(command)) == 64
    int(_fingerprint(command), 16)


def test_fingerprint_ignores_key_order() -> None:
    payload = {"b": 2, "a": 1}
    assert request_fingerprint("op", payload) == request_fingerprint("op", {"a": 1, "b": 2})


def test_fingerprint_changes_with_any_field() -> None:
    base = _fingerprint(TransferCommand(SOURCE, DESTINATION, 1_000, "USD"))
    variants = [
        TransferCommand(SOURCE, DESTINATION, 1_001, "USD"),
        TransferCommand(DESTINATION, SOURCE, 1_000, "USD"),
        TransferCommand(SOURCE, uuid.uuid4(), 1_000, "USD"),
        TransferCommand(SOURCE, DESTINATION, 1_000, "EUR"),
    ]
    assert all(_fingerprint(v) != base for v in variants)


def test_fingerprint_is_scoped_to_the_operation() -> None:
    payload = TransferCommand(SOURCE, DESTINATION, 1_000, "USD").canonical_payload()
    assert request_fingerprint("transfers.create", payload) != request_fingerprint(
        "refunds.create", payload
    )
