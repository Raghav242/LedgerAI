"""Write the API's OpenAPI document without a database, broker or network (CI, Phase 6C).

    python scripts/export_openapi.py openapi.json
    cd frontend && npx openapi-typescript ../openapi.json -o src/types/openapi.gen.ts

The frontend's generated types must match this document exactly; CI regenerates them and
fails on any difference (an API change without regenerated types, or a hand-edited file).
The development surface is included because the frontend uses it.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.rate_limiting import InMemoryFixedWindowLimiter  # noqa: E402


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        postgres_user="openapi",
        postgres_password="openapi-export-only",
        enable_dev_endpoints=True,
        lab_traffic_secret="openapi-export-only-not-a-real-signing-key-0123",
        rate_limit_backend="memory",
    )
    app = create_app(settings, rate_limiter=InMemoryFixedWindowLimiter())
    target.write_text(json.dumps(app.openapi()), encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
