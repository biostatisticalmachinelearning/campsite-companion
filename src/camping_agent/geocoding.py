import logging

from geopy.distance import geodesic
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)

# Fallback lookup table for major California locations
CA_CITIES: dict[str, tuple[float, float]] = {
    "san francisco": (37.7749, -122.4194),
    "los angeles": (34.0522, -118.2437),
    "san diego": (32.7157, -117.1611),
    "sacramento": (38.5816, -121.4944),
    "san jose": (37.3382, -121.8863),
    "oakland": (37.8044, -122.2712),
    "fresno": (36.7378, -119.7871),
    "santa barbara": (34.4208, -119.6982),
    "lake tahoe": (39.0968, -120.0324),
    "yosemite": (37.8651, -119.5383),
    "big sur": (36.2704, -121.8081),
    "joshua tree": (33.8734, -115.9010),
    "mammoth lakes": (37.6485, -118.9721),
    "santa cruz": (36.9741, -122.0308),
    "monterey": (36.6002, -121.8947),
    "redding": (40.5865, -122.3917),
    "eureka": (40.8021, -124.1637),
    "death valley": (36.5054, -116.8661),
    "sequoia": (36.4864, -118.5658),
    "point reyes": (38.0682, -122.8808),
}


def geocode(location: str) -> tuple[float, float]:
    """Return (lat, lon) for a location string.

    Tries the built-in lookup table first, then Nominatim.
    """
    normalized = location.lower().strip()
    if normalized in CA_CITIES:
        logger.debug("Geocode cache hit: %r", location)
        return CA_CITIES[normalized]

    logger.debug("Geocode Nominatim lookup: %r", location)
    try:
        geolocator = Nominatim(user_agent="camping-reservation-agent")
        result = geolocator.geocode(f"{location}, California, USA")
        if result:
            return (result.latitude, result.longitude)
        # Try without state qualifier
        result = geolocator.geocode(location)
        if result:
            return (result.latitude, result.longitude)
    except Exception:
        pass

    raise ValueError(f"Could not geocode location: {location}")


def distance_miles(
    origin: tuple[float, float],
    target: tuple[float, float],
) -> float:
    """Return distance in miles between two (lat, lon) points."""
    return geodesic(origin, target).miles


def is_within_radius(
    origin: tuple[float, float],
    target: tuple[float, float],
    radius_miles: float,
) -> float | None:
    """Return distance in miles if target is within radius, else None."""
    dist = distance_miles(origin, target)
    return dist if dist <= radius_miles else None
