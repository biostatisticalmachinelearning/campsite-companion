import asyncio
from datetime import date, timedelta
from typing import AsyncIterator

import httpx
from langchain_core.tools import tool

from camping_agent.geocoding import distance_miles
from camping_agent.models import Campsite, SearchSource, SiteAvailability

RCA_API_BASE = (
    "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr"
)

# Map RCA UnitCategoryId → our filter category
_CATEGORY_MAP = {
    1: "tent",       # Camping
    2: "tent",       # Group Camping
    7: "day_use",    # DailyUse
    1008: "lodging", # Lodging
    1014: "backpacking",  # Remote Camping
    1015: "rv",      # Hook Up Camping
    1016: "equestrian",   # Equestrian
}

# Map RCA SleepingUnitId → our filter category
_SLEEPING_UNIT_MAP = {
    74: "rv",    # Trailer
    75: "rv",    # RV/Motorhome
    79: "tent",  # Truck/SUV/Van
    83: "tent",  # Tent
}


def _classify_unit(unit: dict) -> str:
    """Classify an RCA unit into a filter category."""
    cat_id = unit.get("UnitCategoryId", 0)
    name = unit.get("Name", "").upper()

    # Check name for specific keywords first
    if "BOAT" in name:
        return "boat_in"
    if any(k in name for k in ("CABIN", "YURT", "LODGE", "LOOKOUT")):
        return "lodging"
    if any(k in name for k in ("HIKE", "WALK", "TRAIL", "BACKPACK", "REMOTE")):
        return "backpacking"
    if any(k in name for k in ("EQUESTRIAN", "HORSE")):
        return "equestrian"

    # Use category mapping
    if cat_id in _CATEGORY_MAP:
        return _CATEGORY_MAP[cat_id]

    return "tent"


def _should_filter_unit(unit: dict, exclude: set[str], include: set[str]) -> bool:
    """Check if a unit should be filtered out."""
    category = _classify_unit(unit)
    if category in exclude:
        return True
    if include and category not in include:
        return True
    return False


@tool
async def search_reserve_california(
    latitude: float,
    longitude: float,
    radius_miles: float,
    start_date: str,
    end_date: str,
    num_people: int = 1,
) -> list[dict]:
    """Search ReserveCalifornia.com for available state park campsites.

    Uses the ReserveCalifornia API directly. Dates in YYYY-MM-DD format.
    """
    try:
        results = []
        async for result in search_rca_api(
            latitude,
            longitude,
            radius_miles,
            date.fromisoformat(start_date),
            date.fromisoformat(end_date),
            num_people,
        ):
            results.append(result)
        return results
    except Exception as e:
        return [
            {
                "error": (
                    f"ReserveCalifornia search failed: {e}. "
                    "Results from Recreation.gov may still be available."
                )
            }
        ]


async def search_rca_api(
    lat: float,
    lon: float,
    radius: float,
    start: date,
    end: date,
    num_people: int,
    exclude: set[str] | None = None,
    include: set[str] | None = None,
    catalog_parks: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Async generator that yields campsite results using the RCA API.

    If catalog_parks is provided, uses those parks (with pre-loaded facility IDs)
    instead of calling the search/place API for discovery and per-park facility lookup.
    Each dict should have: entity_id, name, latitude, longitude, description,
    _distance_miles, and optionally _catalog_facilities (list of {id, name}).
    """
    exclude = exclude or set()
    include = include or set()
    nights = (end - start).days
    if nights < 1:
        nights = 1
    # RCA API EndDate is the last night, not checkout
    api_end = (end - timedelta(days=1)).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
        # Build park list: either from catalog or from live API search
        if catalog_parks is not None:
            parks_to_check = []
            for cp in catalog_parks:
                parks_to_check.append({
                    "place_id": int(cp["entity_id"]),
                    "name": cp["name"],
                    "lat": cp.get("latitude"),
                    "lon": cp.get("longitude"),
                    "dist": cp.get("_distance_miles"),
                    "description": cp.get("description", ""),
                    "catalog_facilities": cp.get("_catalog_facilities"),
                })
        else:
            # Step 1: Search for nearby parks with availability
            search_resp = await client.post(
                f"{RCA_API_BASE}/search/place",
                json={
                    "PlaceId": 0,
                    "Latitude": lat,
                    "Longitude": lon,
                    "StartDate": start.isoformat(),
                    "EndDate": api_end,
                    "Nights": nights,
                    "CountNearby": True,
                    "NearbyLimit": 100,
                    "NearbyOnlyAvailable": True,
                    "Sort": "distance",
                    "CustomerAccountId": 0,
                    "IsADA": False,
                    "UnitCategoryId": 0,
                    "SleepingUnitId": 0,
                    "MinVehicleLength": 0,
                },
            )
            search_resp.raise_for_status()
            data = search_resp.json()

            nearby = data.get("NearbyPlaces", [])
            if not nearby:
                return

            parks_to_check = []
            for park in nearby:
                place_id = park.get("PlaceId")
                if not place_id:
                    continue
                park_lat = park.get("Latitude")
                park_lon = park.get("Longitude")
                dist = None
                if park_lat and park_lon:
                    dist = round(
                        distance_miles((lat, lon), (park_lat, park_lon)), 1
                    )
                    if dist > radius:
                        continue
                parks_to_check.append({
                    "place_id": place_id,
                    "name": park.get("Name", "Unknown"),
                    "lat": park_lat,
                    "lon": park_lon,
                    "dist": dist,
                    "description": park.get("Description", ""),
                    "catalog_facilities": None,
                })

        # Step 2: For each park, get facilities and check availability
        for park_info in parks_to_check:
            place_id = park_info["place_id"]
            name = park_info["name"]
            park_lat = park_info["lat"]
            park_lon = park_info["lon"]
            dist = park_info["dist"]
            description = park_info["description"]
            catalog_facs = park_info.get("catalog_facilities")

            if catalog_facs is not None:
                # Use catalog facility IDs — skip place API call entirely.
                # We only need the facility IDs to call grid API.
                facility_ids = {str(f["id"]): f["name"] for f in catalog_facs}
            else:
                # No catalog data — query park directly to get Facilities
                try:
                    place_resp = await client.post(
                        f"{RCA_API_BASE}/search/place",
                        json={
                            "PlaceId": place_id,
                            "Latitude": park_lat or lat,
                            "Longitude": park_lon or lon,
                            "StartDate": start.isoformat(),
                            "EndDate": api_end,
                            "Nights": nights,
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
                    place_resp.raise_for_status()
                    place_data = place_resp.json()
                except Exception:
                    continue

                selected = place_data.get("SelectedPlace", {})
                facilities_raw = selected.get("Facilities", {})
                if not isinstance(facilities_raw, dict):
                    continue
                facility_ids = {
                    str(fac_id): fac.get("Name", "")
                    for fac_id, fac in facilities_raw.items()
                    if fac.get("Available")
                }
                description = selected.get("Description", "") or description

            all_site_avail: list[SiteAvailability] = []
            all_dates: set[date] = set()

            for fac_id, fac_name in facility_ids.items():

                try:
                    grid_resp = await client.post(
                        f"{RCA_API_BASE}/search/grid",
                        json={
                            "PlaceId": place_id,
                            "FacilityId": int(fac_id),
                            "StartDate": start.isoformat(),
                            "EndDate": api_end,
                            "Nights": nights,
                            "IsADA": False,
                            "UnitCategoryId": 0,
                            "SleepingUnitId": 0,
                            "MinVehicleLength": 0,
                        },
                    )
                    grid_resp.raise_for_status()
                    grid_data = grid_resp.json()
                except Exception:
                    continue

                units = grid_data.get("Facility", {}).get("Units", {})
                if not isinstance(units, dict):
                    continue

                for _uid, unit in units.items():
                    if unit.get("AvailableCount", 0) <= 0:
                        continue

                    # Apply filters
                    if _should_filter_unit(unit, exclude, include):
                        continue

                    unit_name = unit.get("Name", "Unknown")
                    cat_id = unit.get("UnitCategoryId", 0)
                    cat_name = _CATEGORY_MAP.get(cat_id, "tent")

                    # Get available dates from slices
                    avail_dates = []
                    slices = unit.get("Slices", {})
                    for _ts, sl in slices.items():
                        if sl.get("IsFree"):
                            try:
                                d = date.fromisoformat(sl["Date"])
                                avail_dates.append(d)
                            except (ValueError, KeyError):
                                continue

                    if avail_dates:
                        display_name = unit_name
                        if fac_name and fac_name not in unit_name:
                            display_name = f"{fac_name} — {unit_name}"
                        all_site_avail.append(
                            SiteAvailability(
                                site_name=display_name,
                                site_type=cat_name,
                                available_dates=sorted(avail_dates),
                            )
                        )
                        all_dates.update(avail_dates)

            if not all_dates:
                continue

            yield Campsite(
                name=name,
                facility_id=str(place_id),
                source=SearchSource.RESERVE_CALIFORNIA,
                latitude=park_lat,
                longitude=park_lon,
                distance_miles=dist,
                available_dates=sorted(all_dates),
                site_availability=all_site_avail,
                description=(description or "")[:300],
                reservation_url=f"https://www.reservecalifornia.com/park/{place_id}",
            ).model_dump(mode="json")
