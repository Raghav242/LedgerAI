"""Server-signed marker for Experiment Lab traffic.

The lab runner sends its measured requests through NGINX like any other client. To keep
each run isolated (its own rate-limit bucket and its own Idempotency-Key namespace) the
receiving replica must recognise them, and a browser must NOT be able to claim that
status to escape its quota. So the runner attaches an HMAC token:

    X-Lab-Traffic: v1.<run id hex>.<mode>.<hex HMAC-SHA256(secret, "v1.<run>.<mode>")>

Every replica shares the secret LAB_TRAFFIC_SECRET: an independently generated random
value that is REQUIRED whenever the lab is enabled. It is never derived from another
credential (an earlier version derived it from POSTGRES_PASSWORD, whose development
default is public, so anyone could forge tokens). The token never leaves the server side:
it is created by the runner and consumed by a replica. A missing, malformed or forged
token is ignored and the request is treated as the ordinary demo merchant, so ordinary
clients see no behaviour change at all.

This is NOT user authentication. A valid token proves only that the request was minted
by a lab runner holding the secret; it selects a separate rate-limit and idempotency
scope for simulated traffic and grants no other privilege.
"""

import hashlib
import hmac
import uuid
from typing import cast, get_args

from app.core.config import Settings, lab_secret_problem
from app.core.principal import LabRateMode, Principal

LAB_TRAFFIC_HEADER = "X-Lab-Traffic"
_VERSION = "v1"
_MODES = frozenset(get_args(LabRateMode))


class LabSigningNotConfiguredError(RuntimeError):
    """The lab is enabled without a usable, independent LAB_TRAFFIC_SECRET."""


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
        # Checked again here (not only in the Settings validator): Settings built with
        # model_copy skip validation, and there is deliberately no fallback key.
        problem = lab_secret_problem(settings)
        if problem is not None:
            raise LabSigningNotConfiguredError(problem)
        assert settings.lab_traffic_secret is not None
        return cls(settings.lab_traffic_secret.get_secret_value().encode())

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
