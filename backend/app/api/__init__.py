"""The HTTP layer: PRD section 10's routes and the shapes they return.

Thin by rule. ``routes`` validates and delegates, ``schemas`` projects domain
objects onto the wire, ``deps`` supplies a per-request connection and the
broker-bound repository every load route goes through. No SQL and no arithmetic
live here — the first belongs to ``app.repository``, the second to
``app.domain``.
"""

from .routes import router

__all__ = ["router"]
