"""Run the local experiment controller.

    python -m labctl                     # status only: failure experiments disabled
    python -m labctl --enable-failures   # allow the four allow-listed failure experiments
    python -m labctl --enable-benchmarks # allow bounded Locust runs (isolated bench project)

Binds 127.0.0.1 only. Stop with Ctrl+C; a run in progress is cancelled and every
disturbed service is restored before the process exits.
"""

import argparse
import logging

import uvicorn

from app.core.config import Settings
from app.core.logging import configure_logging
from labctl.server import (
    DEFAULT_PORT,
    HOST,
    TOKEN_FILE,
    Controller,
    create_controller_app,
    refuse_unsafe,
    write_token,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--enable-failures",
        action="store_true",
        help="allow experiments that stop and restart allow-listed local services",
    )
    parser.add_argument(
        "--enable-benchmarks",
        action="store_true",
        help="allow bounded benchmark runs (separate Compose project ledgerflow-bench)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    refuse_unsafe(settings)
    token = write_token()
    controller = Controller(
        settings,
        failures_enabled=args.enable_failures,
        benchmarks_enabled=args.enable_benchmarks,
        token=token,
    )
    controller.recover_interrupted()
    log = logging.getLogger("labctl")
    log.warning(
        "Experiment controller on http://%s:%d (failures %s, benchmarks %s). Token in %s",
        HOST,
        args.port,
        "ENABLED" if args.enable_failures else "disabled",
        "ENABLED" if args.enable_benchmarks else "disabled",
        TOKEN_FILE,
    )
    try:
        uvicorn.run(
            create_controller_app(controller), host=HOST, port=args.port, log_level="warning"
        )
    finally:
        controller.close()


if __name__ == "__main__":
    main()
