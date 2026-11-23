"""The ONLY infrastructure actions the experiment controller can perform.

Strict allow-list: four Compose services, a handful of fixed actions, fixed argument
lists, never a shell, never a container or command name taken from a request. Every
disruptive action is written to a journal BEFORE it happens, so an interrupted controller
can restore exactly what it disturbed the next time it starts.
"""

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".ledgerflow"
COMMAND_TIMEOUT_SECONDS = 180

# Compose service -> container name. Nothing else can ever be stopped or restarted.
SERVICES: dict[str, str] = {
    "redis": "ledgerflow-redis",
    "rabbitmq": "ledgerflow-rabbitmq",
    "event-consumer": "ledgerflow-event-consumer-1",
    "api-2": "ledgerflow-api-2-1",
}
# Development-only consumer fault hooks (refused by Settings when APP_ENV=production).
FAULT_VARIABLES = ("CONSUMER_CRASH_BEFORE_COMMIT_EVENT", "CONSUMER_CRASH_AFTER_COMMIT_EVENT")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_QUEUE = re.compile(r"^ledgerflow\.[a-z0-9.\-]+$")

CommandRunner = Callable[[list[str], dict[str, str] | None], subprocess.CompletedProcess[str]]


class InfraNotAllowedError(ValueError):
    """An action or target outside the allow-list (a bug, never user input)."""


class InfraCommandError(RuntimeError):
    pass


class RecoveryFailedError(RuntimeError):
    """Restoration did not bring a disturbed service back. Reported, never hidden."""


def docker(args: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


class Journal:
    """What the controller disturbed and has not yet restored (a small JSON file)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_DIR / "controller-journal.json"

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def add(self, run_id: str, service: str, action: str) -> None:
        state = self.load() or {"run_id": run_id, "started_at": datetime.now(UTC).isoformat()}
        disturbed = state.setdefault("disturbed", {})
        disturbed[service] = action
        self._write(state)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never a half-written journal


class Infra:
    def __init__(self, run_id: str, journal: Journal, runner: CommandRunner = docker) -> None:
        self.run_id = run_id
        self.journal = journal
        self._runner = runner

    @staticmethod
    def _check(service: str) -> str:
        if service not in SERVICES:
            raise InfraNotAllowedError(f"service {service!r} is not on the allow-list")
        return service

    async def _run(self, args: list[str], env: dict[str, str] | None = None) -> str:
        result = await asyncio.to_thread(self._runner, args, env)
        if result.returncode != 0:
            raise InfraCommandError(
                f"docker {' '.join(args[:3])} failed: {(result.stderr or result.stdout)[-300:]}"
            )
        return result.stdout

    # --- disruptive actions (journaled first) -----------------------------------------------

    async def stop(self, service: str) -> None:
        self.journal.add(self.run_id, self._check(service), "stopped")
        await self._run(["compose", "stop", service])

    async def recreate_consumer(self, fault: dict[str, str]) -> None:
        """Recreate the consumer with (or, with {}, without) a fault hook for one event id."""
        for key, value in fault.items():
            if key not in FAULT_VARIABLES or not _UUID.match(value):
                raise InfraNotAllowedError(f"fault {key}={value!r} is not allowed")
        self.journal.add(self.run_id, "event-consumer", "fault_injected" if fault else "recreated")
        env = {name: fault.get(name, "") for name in FAULT_VARIABLES}
        await self._run(["compose", "up", "-d", "--no-deps", "event-consumer"], env)

    # --- restoration ---------------------------------------------------------------------

    async def start(self, service: str) -> None:
        await self._run(["compose", "start", self._check(service)])

    async def restore(self) -> list[str]:
        """Bring back everything the journal says is disturbed; verify; clear the journal."""
        state = self.journal.load()
        if not state:
            return []
        disturbed: dict[str, str] = state.get("disturbed", {})
        problems = []
        for service in disturbed:
            try:
                if service == "event-consumer":
                    env = dict.fromkeys(FAULT_VARIABLES, "")
                    await self._run(["compose", "up", "-d", "--no-deps", "event-consumer"], env)
                else:
                    await self.start(service)
            except (InfraCommandError, InfraNotAllowedError, subprocess.TimeoutExpired) as exc:
                problems.append(f"{service}: {exc}")
        for service in disturbed:
            if service in SERVICES and not await self.running(service):
                problems.append(f"{service} is not running after restoration")
        if problems:
            raise RecoveryFailedError(
                "RECOVERY FAILED: "
                + "; ".join(problems)
                + ". Restore manually: docker compose up -d --wait"
            )
        self.journal.clear()
        return sorted(disturbed)

    # --- read-only observations ---------------------------------------------------------------

    async def running(self, service: str) -> bool:
        out = await self._run(
            ["inspect", "-f", "{{.State.Running}}", SERVICES[self._check(service)]]
        )
        return out.strip() == "true"

    async def restart_count(self, service: str) -> int:
        out = await self._run(
            ["inspect", "-f", "{{.RestartCount}}", SERVICES[self._check(service)]]
        )
        return int(out.strip() or 0)

    async def logs(self, service: str, *, since: str) -> str:
        return await self._run(
            ["compose", "logs", "--no-color", "--since", since, self._check(service)]
        )

    async def queue_depth(self, queue: str) -> tuple[int, int] | None:
        """(ready, unacked) of one ledgerflow queue, via rabbitmqctl in the broker container."""
        if not _QUEUE.match(queue):
            raise InfraNotAllowedError(f"queue {queue!r} is not allowed")
        out = await self._run(
            [
                "exec",
                SERVICES["rabbitmq"],
                "rabbitmqctl",
                "-q",
                "list_queues",
                "name",
                "messages_ready",
                "messages_unacknowledged",
            ]
        )
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == queue:
                return int(parts[1]), int(parts[2])
        return None
