import asyncio
from datetime import date, datetime

import httpx
from langchain_core.tools import tool

from camping_agent.config import settings
from camping_agent.geocoding import is_within_radius
from camping_agent.models import Campsite, SearchSource

# Limit concurrent availability requests to avoid hitting rate limits
_SEMAPHORE = asyncio.Semaphore(10)


async def _search_ridb_facilities(
    lat: float, lon: float, radius: float
) -> list[dict]:
    """Call RIDB API to find camping facilities near coordinates."""
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        resp = await client.get(
            f"{settings.ridb_base_url}/facilities",
            params={
                "latitude": lat,
                "longitude": lon,
                "radius": radius,
                "activity": "CAMPING",
                "limit": 50,
            },
            headers={"apikey": settings.ridb_api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("RECDATA", [])


async def _check_availability(facility_id: str, month_start: date) -> dict:
    """Check availability for a campground for a given month."""
    date_str = month_start.strftime("%Y-%m-%dT00:00:00.000Z")
    url = f"{settings.recgov_availability_url}/{facility_id}/month"
    async with _SEMAPHORE:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            resp = await client.get(url, params={"start_date": date_str})
            resp.raise_for_status()
            return resp.json()


def _get_months_to_check(start_date: date, end_date: date) -> list[date]:
    """Return first-of-month dates for all months in the date range."""
    months = []
    current = start_date.replace(day=1)
    end_month = end_date.replace(day=1)
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def _parse_available_sites(
    availability_data: dict, start_date: date, end_date: date, num_people: int
) -> list[date]:
    """Extract dates that have at least one available site in the requested range."""
    available_dates: set[date] = set()
    campsites = availability_data.get("campsites", {})
    for _site_id, site_info in campsites.items():
        availabilities = site_info.get("availabilities", {})
        for date_str, status in availabilities.items():
            if status == "Available":
                try:
                    d = datetime.fromisoformat(
                        date_str.replace("T00:00:00Z", "")
                    ).date()
                    if start_date <= d <= end_date:
                        available_dates.add(d)
                except ValueError:
                    continue
    return sorted(available_dates)


@tool
async def search_recreation_gov(
    latitude: float,
    longitude: float,
    radius_miles: float,
    start_date: str,
    end_date: str,
    num_people: int = 1,
) -> list[dict]:
    """Search Recreation.gov for available campsites near given coordinates.

    Args:
        latitude: Latitude of search center
        longitude: Longitude of search center
        radius_miles: Search radius in miles
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        num_people: Number of people in the group

    Returns a list of available campgrounds sorted by distance.
    """
    sd = date.fromisoformat(start_date)
    ed = date.fromisoformat(end_date)
    origin = (latitude, longitude)
    months_to_check = _get_months_to_check(sd, ed)

    # Step 1: Find facilities near the location
    facilities = await _search_ridb_facilities(latitude, longitude, radius_miles)

    # Step 2: Check availability for each facility concurrently
    results: list[Campsite] = []

    async def check_facility(fac: dict) -> Campsite | None:
        fac_lat = fac.get("FacilityLatitude")
        fac_lon = fac.get("FacilityLongitude")
        fac_id = str(fac.get("FacilityID", ""))
        if not fac_lat or not fac_lon or not fac_id:
            return None

        dist = is_within_radius(origin, (fac_lat, fac_lon), radius_miles)
        if dist is None:
            return None

        all_available: list[date] = []
        for month_start in months_to_check:
            try:
                avail_data = await _check_availability(fac_id, month_start)
                all_available.extend(
                    _parse_available_sites(avail_data, sd, ed, num_people)
                )
            except Exception:
                continue

        if not all_available:
            return None

        return Campsite(
            name=fac.get("FacilityName", "Unknown"),
            facility_id=fac_id,
            source=SearchSource.RECREATION_GOV,
            latitude=fac_lat,
            longitude=fac_lon,
            distance_miles=round(dist, 1),
            available_dates=sorted(set(all_available)),
            description=(fac.get("FacilityDescription", "") or "")[:200],
            reservation_url=f"https://www.recreation.gov/camping/campgrounds/{fac_id}",
            campsite_type=fac.get("FacilityTypeDescription", ""),
        )

    tasks = [check_facility(fac) for fac in facilities]
    checked = await asyncio.gather(*tasks, return_exceptions=True)

    for result in checked:
        if isinstance(result, Campsite):
            results.append(result)

    results.sort(key=lambda c: c.distance_miles or 999)
    return [c.model_dump(mode="json") for c in results[:20]]
