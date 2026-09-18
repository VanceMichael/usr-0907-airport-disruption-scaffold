"""Runtime configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    fixtures_dir: str
    contract_path: str

    @classmethod
    def from_env(cls) -> "Config":
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError:
            raise SystemExit("PORT must be an integer")
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=port,
            db_path=os.environ.get("DB_PATH", "./data/disruptions.db"),
            fixtures_dir=os.environ.get("FIXTURES_DIR", "./fixtures"),
            contract_path=os.environ.get(
                "CONTRACT_PATH", "./contracts/disruption-event.schema.json"),
        )
