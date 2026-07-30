"""Offline distance: great-circle miles with a flat road factor.

CLAUDE.md invariant 7 fixes the method — Haversine x 1.2, no routing API, no
network. Every mile figure the system reports for a lane traces to this module.
"""

from __future__ import annotations

import math

from .geo import Place

__all__ = [
    "EARTH_RADIUS_MILES",
    "ROAD_FACTOR",
    "haversine_miles",
    "road_miles",
    "haversine_between",
    "road_miles_between",
]

# Mean Earth radius in statute miles.
EARTH_RADIUS_MILES = 3958.7613

# PRD section 3: straight line x 1.2 stands in for road routing.
ROAD_FACTOR = 1.2


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles between two lat/lon pairs."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def road_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Estimated road miles: great-circle x ``ROAD_FACTOR``."""
    return haversine_miles(lat1, lon1, lat2, lon2) * ROAD_FACTOR


def haversine_between(a: Place, b: Place) -> float:
    """Great-circle miles between two resolved places."""
    return haversine_miles(a.lat, a.lon, b.lat, b.lon)


def road_miles_between(a: Place, b: Place) -> float:
    """Estimated road miles between two resolved places."""
    return road_miles(a.lat, a.lon, b.lat, b.lon)
