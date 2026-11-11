import hashlib
import json
from typing import Any


def request_fingerprint(operation: str, payload: dict[str, Any]) -> str:
    """SHA-256 of a canonical JSON encoding of the request.

    Canonical = sorted keys, no insignificant whitespace, values already normalised by
    the caller (UUIDs as lowercase strings, amounts as ints). Two requests that mean the
    same thing always hash the same, whatever key order or UUID casing the client sent;
    any change to the operation or a field yields a different fingerprint.
    """
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
