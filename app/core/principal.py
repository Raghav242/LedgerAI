"""Who is calling. Defines the scope that Idempotency-Keys and rate limits belong to.

There is no authentication yet, so every request acts as one explicitly defined demo
merchant. The scope comes from the server, never from a caller-supplied header or body
field: a client must not be able to read or collide with another merchant's keys by
claiming to be them. When authentication arrives, `get_current_principal` (see
app/api/dependencies.py) will derive the principal from the verified credentials and
nothing else needs to change.

The one other principal is Experiment Lab traffic (Phase 5B): requests that carry a
server-signed `X-Lab-Traffic` token (app/lab/signing.py). Only the server can mint that
token, so a browser cannot use it to escape its quota. A missing or invalid token simply
means "demo merchant".
"""

from dataclasses import dataclass
from typing import Literal

LabRateMode = Literal["standard", "burst"]


@dataclass(frozen=True, slots=True)
class Principal:
    scope: str
    # Set only for signed lab traffic: "standard" = the normal read/write policies (on
    # the run's own bucket), "burst" = the lab burst policy, for experiments that are not
    # about rate limiting. None = an ordinary caller.
    lab_rate_mode: LabRateMode | None = None


DEMO_PRINCIPAL = Principal(scope="demo-merchant")
