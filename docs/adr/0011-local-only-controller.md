# ADR 0011: Infrastructure control only in a local, allow-listed controller

Status: accepted · Phase: Phase 5F

## Context

Failure experiments must stop and restart containers, which requires the Docker socket.

## Decision

No container mounts the Docker socket and no API endpoint runs commands. A separate host
process (`python -m labctl`) binds 127.0.0.1, requires a per-start token injected by the
Vite dev proxy, checks Host and Origin, allows four services and fixed argument lists,
journals every disturbance first and restores it afterwards (also after a crash).

## Consequences

+ An internet-facing API compromise cannot reach Docker.
- Failure experiments need the local dev server; the containerized frontend has no
controller.

## Alternatives considered

Docker socket in an API container (host takeover risk); remote agent (unnecessary).
