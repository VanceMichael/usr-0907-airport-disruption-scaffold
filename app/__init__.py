"""Airport disruption service.

A dependency-free Python 3.12 HTTP service that ingests airport disruption
events (closed / extended / reopened), computes flight impacts against the
fixture schedule, and persists everything in SQLite.
"""

__version__ = "1.0.0"
