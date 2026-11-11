"""Load-balancing demo: round robin across replicas, then lose a replica and recover.

LOCAL ONLY: stops and starts the api-2 container of the local Compose stack.

    python scripts/demo_load_balancing.py [--requests 20]
"""

import subprocess
import time
from collections import Counter

from demo_common import base_arg_parser, check
from stack_control import (
    compose,
    ensure_local,
    served_by,
    wait_ready,
    wait_until_served_only_by,
)


def show(label: str, replicas: list[str]) -> Counter[str]:
    counts = Counter(replicas)
    print(f"\n{label}")
    print("  sequence: " + " ".join(r.replace("api-", "") for r in replicas))
    for replica, n in sorted(counts.items()):
        print(f"  {replica:<6} {'#' * n} {n}")
    return counts


def nginx_retries_since(since: str) -> int:
    """Requests NGINX had to pass to a second upstream (first one refused the connection)."""
    logs = subprocess.run(
        ["docker", "logs", "--since", since, "ledgerflow-nginx"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return sum(1 for line in logs.splitlines() if "upstream_status=502, 200" in line)


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--requests", type=int, default=20)
    args = parser.parse_args()
    base_url = args.base_url
    ensure_local(base_url)
    wait_ready(base_url)

    counts = show("1) Both replicas up: round robin", served_by(base_url, args.requests))
    check(set(counts) == {"api-1", "api-2"}, "traffic reaches both replicas")
    check(abs(counts["api-1"] - counts["api-2"]) <= 1, "traffic is evenly split")

    print("\n2) Stopping api-2 (docker compose stop api-2) ...")
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    compose("stop", "api-2")
    try:
        counts = show("   Immediately after the stop", served_by(base_url, args.requests))
        check("ERROR" not in counts, "no request failed while api-2 was down")
        check(set(counts) == {"api-1"}, "api-1 served everything")
        print(
            f"  NGINX re-sent {nginx_retries_since(since)} request(s) whose connection to api-2 "
            "was refused (safe: they never reached it). 0 means NGINX had already dropped "
            "api-2 after re-resolving Docker DNS during the graceful stop."
        )
    finally:
        print("\n3) Starting api-2 again ...")
        compose("start", "api-2")
        wait_until_served_only_by(base_url, {"api-1", "api-2"}, timeout=60)

    counts = show(
        "   After recovery (NGINX re-resolved Docker DNS)", served_by(base_url, args.requests)
    )
    check(set(counts) == {"api-1", "api-2"}, "api-2 is back in rotation")
    print("\nPASS: stateless replicas behind NGINX; losing one replica loses no requests.")


if __name__ == "__main__":
    main()
