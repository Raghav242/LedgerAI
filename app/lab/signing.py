"""Server-signed marker for Experiment Lab traffic.

The lab runner sends its measured requests through NGINX like any other client. To keep
each run isolated (its own rate-limit bucket and its own Idempotency-Key namespace) the
receiving replica must recognise them, and a browser must NOT be able to claim that
status to escape its quota. So the runner attaches an HMAC token:

    X-Lab-Traffic: v1.<run id hex>.<mode>.<hex HMAC-SHA256(secret, "v1.<run>.<mode>")>

Every replica shares the secret (LAB_TRAFFIC_SECRET, or a key derived from
POSTGRES_PASSWORD, which all replicas already share). The token never leaves the
server side: it is created by the runner and consumed by a replica. A missing, malformed
or forged token is ignored and the request is treated as the ordinary demo merchant, so
ordinary clients see no behaviour change at all.
"""

import hashlib
import hmac
import uuid
from typing import cast, get_args

from app.core.config import Settings
from app.core.principal import LabRateMode, Principal

LAB_TRAFFIC_HEADER = "X-Lab-Traffic"
_VERSION = "v1"
_DERIVATION_LABEL = b"ledgerflow-lab-traffic-v1"
_MODES = frozenset(get_args(LabRateMode))


def lab_scope(run_id: uuid.UUID) -> str:
    """Rate-limit and idempotency scope of one lab run (fits the 100-char scope column)."""
    return f"lab:{run_id.hex}"


class LabTrafficSigner:
    header = LAB_TRAFFIC_HEADER

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("lab traffic secret must be at least 16 bytes")
        self._secret = secret

    @classmethod
    def from_settings(cls, settings: Settings) -> "LabTrafficSigner":
        if settings.lab_traffic_secret is not None:
            return cls(settings.lab_traffic_secret.get_secret_value().encode())
        password = settings.postgres_password.get_secret_value().encode()
        return cls(hmac.new(password, _DERIVATION_LABEL, hashlib.sha256).digest())

    def _mac(self, run_hex: str, mode: str) -> str:
        message = f"{_VERSION}.{run_hex}.{mode}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def token(self, run_id: uuid.UUID, mode: LabRateMode) -> str:
        if mode not in _MODES:
            raise ValueError(f"unknown lab rate mode {mode!r}")
        return f"{_VERSION}.{run_id.hex}.{mode}.{self._mac(run_id.hex, mode)}"

    def verify(self, token: str | None) -> tuple[uuid.UUID, LabRateMode] | None:
        if not token or len(token) > 200:
            return None
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _VERSION or parts[2] not in _MODES:
            return None
        _, run_hex, mode, mac = parts
        try:
            run_id = uuid.UUID(hex=run_hex)
        except ValueError:
            return None
        if run_id.hex != run_hex or not hmac.compare_digest(mac, self._mac(run_hex, mode)):
            return None
        return run_id, cast(LabRateMode, mode)

    def principal_for(self, token: str | None) -> Principal | None:
        verified = self.verify(token)
        if verified is None:
            return None
        run_id, mode = verified
        return Principal(scope=lab_scope(run_id), lab_rate_mode=mode)
