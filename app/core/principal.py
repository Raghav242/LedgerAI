"""Who is calling. Defines the scope that Idempotency-Keys belong to.

There is no authentication yet, so every request acts as one explicitly defined demo
merchant. The scope comes from the server, never from a caller-supplied header or body
field: a client must not be able to read or collide with another merchant's keys by
claiming to be them. When authentication arrives, `get_current_principal` (see
app/api/dependencies.py) will derive the principal from the verified credentials and
nothing else needs to change.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    scope: str


DEMO_PRINCIPAL = Principal(scope="demo-merchant")
