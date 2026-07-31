"""Repository layer: the only code in the project that speaks SQL.

Routes and domain code call these objects; neither builds a query. Everything
tenant-scoped goes through :class:`BrokerRepository`, which is bound to one
``broker_id`` at construction and enforced by row-level security underneath —
see ``broker_repository`` for the three barriers and the one documented way
around them.

:class:`PoolRepository` is the single deliberate exception and lives in its own
module for that reason: it is the opt-in shared carrier pool (DECISIONS.md
D17), it reads one projection that has no money column, and every statement in
it is on the page.
"""

from .broker_repository import BrokerRepository, UnknownBroker, broker_session
from .pool_repository import PoolRepository
from .db import (
    APP_ROLE,
    BROKER_SETTING,
    DEFAULT_ADMIN_DATABASE_URL,
    DEFAULT_DATABASE_URL,
    SCHEMA_PATH,
    bootstrap,
    connect,
    connect_admin,
    get_broker,
    list_brokers,
)

__all__ = [
    "APP_ROLE",
    "BROKER_SETTING",
    "DEFAULT_ADMIN_DATABASE_URL",
    "DEFAULT_DATABASE_URL",
    "SCHEMA_PATH",
    "BrokerRepository",
    "PoolRepository",
    "UnknownBroker",
    "bootstrap",
    "broker_session",
    "connect",
    "connect_admin",
    "get_broker",
    "list_brokers",
]
