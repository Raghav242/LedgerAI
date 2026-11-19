"""Liveness signal for worker processes (they serve no HTTP endpoint).

The main loop calls beat() every iteration; the container health check considers the
worker unhealthy if the file is older than 30 s (docker-compose.yml). A failure to write
it must never disturb the worker itself.
"""

import contextlib
import tempfile
from pathlib import Path

# /tmp inside the Linux container (where the health check looks); the OS temp dir locally.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "ledgerflow-worker-heartbeat"


def beat() -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT_FILE.touch(exist_ok=True)
