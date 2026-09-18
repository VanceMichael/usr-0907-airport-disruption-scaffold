"""Container health check: exits 0 when the service answers /health."""
from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "8080")
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
