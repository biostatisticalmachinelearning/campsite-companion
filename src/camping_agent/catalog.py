"""Build and load park/campground catalogs from Recreation.gov and ReserveCalifornia."""

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

from camping_agent.config import settings
from camping_agent.geocoding import distance_miles
from camping_agent.models import CatalogFacility, CatalogPark, SearchSource

DATA_DIR = Path(__file__).parent.parent.parent / "data"

RECGOV_SEARCH_URL = "https://www.recreation.gov/api/search"
RCA_API_BASE = (
    "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr"
)

# Grid points for Recreation.gov (continental US coverage)
_RECGOV_GRID = [
    (47.6, -122.3),   # Seattle
    (45.5, -122.7),   # Portland
    (37.8, -122.4),   # San Francisco
    (34.1, -118.2),   # Los Angeles
    (32.7, -117.2),   # San Diego
    (33.4, -112.0),   # Phoenix
    (39.7, -105.0),   # Denver
    (40.8, -111.9),   # Salt Lake City
    (43.6, -116.2),   # Boise
    (45.8, -108.5),   # Billings
    (44.9, -93.3),    # Minneapolis
    (41.9, -87.6),    # Chicago
    (42.3, -83.0),    # Detroit
    (36.2, -86.8),    # Nashville
    (33.7, -84.4),    # Atlanta
    (25.8, -80.2),    # Miami
    (35.2, -80.8),    # Charlotte
    (38.9, -77.0),    # DC
    (40.7, -74.0),    # NYC
    (42.4, -71.1),    # Boston
    (32.8, -96.8),    # Dallas
    (29.8, -95.4),    # Houston
    (39.1, -94.6),    # Kansas City
    (35.1, -106.6),   # Albuquerque
    (61.2, -149.9),   # Anchorage
    (46.9, -110.4),   # Montana center
    (43.1, -75.2),    # Upstate NY
    (37.5, -79.4),    # Virginia
    (30.3, -89.3),    # Gulf Coast
]

# Grid points for ReserveCalifornia (California coverage)
_RCA_GRID = [
    (37.8, -122.4),   # San Francisco
    (34.1, -118.2),   # Los Angeles
    (38.6, -121.5),   # Sacramento
    (32.7, -117.2),   # San Diego
    (36.7, -119.8),   # Fresno
    (40.8, -124.2),   # Eureka
    (39.1, -120.0),   # Lake Tahoe
    (36.6, -117.4),   # Death Valley area
]

# Module-level cache for loaded catalog
_catalog_cache: list[CatalogPark] | None = None

_CATALOG_MAX_AGE_DAYS = 14


def catalog_is_stale() -> bool:
    """Return True if either catalog file is missing or older than _CATALOG_MAX_AGE_DAYS."""
    import time

    max_age_secs = _CATALOG_MAX_AGE_DAYS * 86400
    now = time.time()

    for path in (DATA_DIR / "catalog_recgov.json", DATA_DIR / "catalog_rca.json"):
        if not path.exists():
            return True
        if now - path.stat().st_mtime > max_age_secs:
            return True
    return False


async def build_recgov_catalog() -> list[CatalogPark]:
    """Build Recreation.gov catalog by grid-searching across the US."""
    seen_ids: set[str] = set()
    parks: list[CatalogPark] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for i, (lat, lon) in enumerate(_RECGOV_GRID):
            logger.debug("Recreation.gov grid point %d/%d: (%s, %s)", i + 1, len(_RECGOV_GRID), lat, lon)
            start = 0
            page_size = 50
            max_results = 500

            while start < max_results:
                try:
                    resp = await client.get(
                        RECGOV_SEARCH_URL,
                        params={
                            "fq": "entity_type:campground",
                            "lat": lat,
                            "lng": lon,
                            "size": page_size,
                            "start": start,
                        },
                        headers={"User-Agent": "camping-reservation-agent/0.1"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    batch = data.get("results", [])
                    if not batch:
                        break

                    for cg in batch:
                        eid = str(cg.get("entity_id", ""))
                        if not eid or eid in seen_ids:
                            continue
                        seen_ids.add(eid)
                        parks.append(CatalogPark(
                            id=eid,
                            name=cg.get("name", "Unknown"),
                            source=SearchSource.RECREATION_GOV,
                            latitude=float(cg["latitude"]) if cg.get("latitude") else None,
                            longitude=float(cg["longitude"]) if cg.get("longitude") else None,
                            description=(cg.get("description", "") or "")[:300],
                            reservation_url=f"https://www.recreation.gov/camping/campgrounds/{eid}",
                        ))

                    if len(batch) < page_size:
                        break
                    start += page_size
                except Exception as e:
                    logger.error("Error at grid point (%s, %s) offset %d: %s", lat, lon, start, e)
                    break

            await asyncio.sleep(0.3)

    parks.sort(key=lambda p: p.name.lower())
    logger.info("Recreation.gov: %d unique campgrounds", len(parks))
    return parks


async def build_rca_catalog() -> list[CatalogPark]:
    """Build ReserveCalifornia catalog by grid-searching across California."""
    seen_ids: set[str] = set()
    parks: list[CatalogPark] = []

    # RCA requires dates for search — use next 7 days as dummy
    start_date = date.today()
    end_date = start_date + timedelta(days=7)
    api_end = (end_date - timedelta(days=1)).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
        for i, (lat, lon) in enumerate(_RCA_GRID):
            logger.debug("ReserveCalifornia grid point %d/%d: (%s, %s)", i + 1, len(_RCA_GRID), lat, lon)
            try:
                resp = await client.post(
                    f"{RCA_API_BASE}/search/place",
                    json={
                        "PlaceId": 0,
                        "Latitude": lat,
                        "Longitude": lon,
                        "StartDate": start_date.isoformat(),
                        "EndDate": api_end,
                        "Nights": 1,
                        "CountNearby": True,
                        "NearbyLimit": 200,
                        "NearbyOnlyAvailable": False,
                        "Sort": "distance",
                        "CustomerAccountId": 0,
                        "IsADA": False,
                        "UnitCategoryId": 0,
                        "SleepingUnitId": 0,
                        "MinVehicleLength": 0,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                nearby = data.get("NearbyPlaces", [])
                for park in nearby:
                    pid = str(park.get("PlaceId", ""))
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    parks.append(CatalogPark(
                        id=pid,
                        name=park.get("Name", "Unknown"),
                        source=SearchSource.RESERVE_CALIFORNIA,
                        latitude=park.get("Latitude"),
                        longitude=park.get("Longitude"),
                        description=(park.get("Description", "") or "")[:300],
                        reservation_url=f"https://www.reservecalifornia.com/park/{pid}",
                    ))
            except Exception as e:
                logger.error("Error at RCA grid point (%s, %s): %s", lat, lon, e)

            await asyncio.sleep(0.3)

    # Fetch facility metadata for each RCA park
    logger.info("Fetching facility metadata for %d RCA parks...", len(parks))
    async with httpx.AsyncClient(timeout=30) as client:
        for i, park in enumerate(parks):
            if (i + 1) % 20 == 0:
                logger.debug("Facility fetch %d/%d...", i + 1, len(parks))
            try:
                resp = await client.post(
                    f"{RCA_API_BASE}/search/place",
                    json={
                        "PlaceId": int(park.id),
                        "Latitude": 0,
                        "Longitude": 0,
                        "StartDate": start_date.isoformat(),
                        "EndDate": api_end,
                        "Nights": 1,
                        "CountNearby": False,
                        "NearbyLimit": 0,
                        "Sort": "distance",
                        "CustomerAccountId": 0,
                        "IsADA": False,
                        "UnitCategoryId": 0,
                        "SleepingUnitId": 0,
                        "MinVehicleLength": 0,
                    },
                )
                resp.raise_for_status()
                place_data = resp.json()
                selected = place_data.get("SelectedPlace", {})
                facilities_raw = selected.get("Facilities", {})
                if isinstance(facilities_raw, dict):
                    park.facilities = [
                        CatalogFacility(id=str(fac_id), name=fac.get("Name", ""))
                        for fac_id, fac in facilities_raw.items()
                    ]
            except Exception as e:
                logger.error("Error fetching facilities for %s: %s", park.name, e)
            await asyncio.sleep(0.3)

    parks.sort(key=lambda p: p.name.lower())
    logger.info("ReserveCalifornia: %d unique parks", len(parks))
    return parks


def save_catalog(recgov: list[CatalogPark], rca: list[CatalogPark]) -> None:
    """Save catalogs to JSON files."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    recgov_path = DATA_DIR / "catalog_recgov.json"
    rca_path = DATA_DIR / "catalog_rca.json"

    with open(recgov_path, "w") as f:
        json.dump([p.model_dump(mode="json") for p in recgov], f, indent=2)
    logger.info("Saved %d Recreation.gov parks to %s", len(recgov), recgov_path)

    with open(rca_path, "w") as f:
        json.dump([p.model_dump(mode="json") for p in rca], f, indent=2)
    logger.info("Saved %d ReserveCalifornia parks to %s", len(rca), rca_path)


def load_catalog(q: str | None = None) -> list[CatalogPark]:
    """Load merged catalog from JSON files, optionally filtered by search term."""
    global _catalog_cache

    if _catalog_cache is None:
        parks: list[CatalogPark] = []

        recgov_path = DATA_DIR / "catalog_recgov.json"
        if recgov_path.exists():
            with open(recgov_path) as f:
                parks.extend(CatalogPark(**p) for p in json.load(f))

        rca_path = DATA_DIR / "catalog_rca.json"
        if rca_path.exists():
            with open(rca_path) as f:
                parks.extend(CatalogPark(**p) for p in json.load(f))

        parks.sort(key=lambda p: p.name.lower())
        _catalog_cache = parks

    result = _catalog_cache
    if q:
        term = q.lower()
        result = [p for p in result if term in p.name.lower()]

    return result


def search_catalog_by_location(
    lat: float,
    lon: float,
    radius_miles: float,
    source: SearchSource | None = None,
) -> list[dict]:
    """Search the catalog for parks near a location, returning dicts with distance.

    Returns dicts compatible with the campground format used by _stream_recgov's
    check_one(): entity_id, name, latitude, longitude, description, type, _distance_miles.
    For RCA parks, also includes facilities list from catalog.
    """
    catalog = load_catalog()
    results = []
    origin = (lat, lon)

    for park in catalog:
        if source and park.source != source:
            continue
        if park.latitude is None or park.longitude is None:
            continue

        dist = distance_miles(origin, (park.latitude, park.longitude))
        if dist > radius_miles:
            continue

        entry = {
            "entity_id": park.id,
            "name": park.name,
            "latitude": park.latitude,
            "longitude": park.longitude,
            "description": park.description,
            "type": "",
            "_distance_miles": round(dist, 1),
        }
        if park.facilities is not None:
            entry["_catalog_facilities"] = [
                {"id": f.id, "name": f.name} for f in park.facilities
            ]
        results.append(entry)

    results.sort(key=lambda r: r["_distance_miles"])
    return results


async def build_all() -> None:
    """Build catalogs from both sources and save."""
    logger.info("Building park catalog...")
    recgov = await build_recgov_catalog()
    rca = await build_rca_catalog()
    save_catalog(recgov, rca)
    logger.info("Done! %d Recreation.gov + %d ReserveCalifornia parks", len(recgov), len(rca))


def main():
    """CLI entry point for building the catalog."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(build_all())


if __name__ == "__main__":
    main()
