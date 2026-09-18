"""Airport disruption API.

A dependency-free (stdlib-only) HTTP service that ingests airport
closure / extension / reopening events, computes flight impacts against
the fixture schedule and persists everything in SQLite.
"""

__version__ = "1.0.0"
