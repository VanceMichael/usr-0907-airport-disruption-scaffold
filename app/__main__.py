"""Service entry point: ``python -m app``."""
from __future__ import annotations

import logging
import signal
import threading

from .config import Config
from .fixtures import load_airports, load_flights
from .http_api import create_server
from .service import DisruptionService
from .storage import Storage

logger = logging.getLogger("disruption")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    airports = load_airports(config.fixtures_dir)
    flights = load_flights(config.fixtures_dir, airports)
    storage = Storage(config.db_path)
    service = DisruptionService(storage, airports, flights)
    server = create_server((config.host, config.port), service)

    def _stop(signum, _frame) -> None:
        logger.info("received signal %s, shutting down", signum)
        # shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info(
        "serving on %s:%s (db=%s, airports=%d, flights=%d)",
        config.host,
        config.port,
        config.db_path,
        len(airports),
        len(flights),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        storage.close()
        logger.info("shutdown complete")


if __name__ == "__main__":
    main()
