"""Service entry point: ``python -m app``."""
from __future__ import annotations

import signal
import threading

from .config import Config
from .events import EventService
from .fixtures import load_fixtures
from .server import make_server
from .storage import Storage
from .validation import load_contract


def main() -> None:
    config = Config.from_env()
    airports, flights = load_fixtures(config.fixtures_dir)
    schema = load_contract(config.contract_path)

    storage = Storage(config.db_path)
    storage.init_schema()

    service = EventService(storage, schema, airports, flights)
    server = make_server(config.host, config.port, service)

    def _shutdown(signum, _frame):
        # serve_forever must be stopped from another thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(
        f"airport-disruption-api listening on {config.host}:{config.port} "
        f"(db={config.db_path}, airports={len(airports)}, flights={len(flights)})",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
