"""Runtime configuration, sourced from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    fixtures_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8080")),
            db_path=os.environ.get("DISRUPTION_DB_PATH", "./data/disruptions.db"),
            fixtures_dir=os.environ.get("FIXTURES_DIR", "./fixtures"),
        )
