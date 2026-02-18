import asyncio
import logging
import re
from datetime import date, datetime

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

from camping_agent.config import settings
from camping_agent.geocoding import is_within_radius
from camping_agent.models import Campsite, SearchSource, SiteAvailability

# Limit concurrent availability requests to respect rate limits
_SEMAPHORE = asyncio.Semaphore(5)

RECGOV_SEARCH_URL = "https://www.recreation.gov/api/search"


# --- Name-based patterns ---
_BOAT_PATTERNS = re.compile(
    r"boat[- ]in|sail|anchor|vessel|mooring", re.IGNORECASE
)
_EQUESTRIAN_PATTERNS = re.compile(
    r"equestrian|horse|stock\s+camp", re.IGNORECASE
)
_BACKPACKING_PATTERNS = re.compile(
    r"backpack|hike[- ]?in|walk[- ]?in|primitive|backcountry|wilderness", re.IGNORECASE
)
_LODGING_PATTERNS = re.compile(
    r"cabin|lodge|yurt|lookout|chalet|bunkhouse", re.IGNORECASE
)


def _classify_campground(campground: dict) -> set[str]:
    """Return the set of category tags that apply to a campground."""
    name = campground.get("name", "")
    equipment = set(campground.get("campsite_equipment_name", []))
    use_types = campground.get("campsite_type_of_use", [])
    tags: set[str] = set()

    # Exclusion-type categories
    if _BOAT_PATTERNS.search(name) or equipment == {"Boat"}:
        tags.add("boat_in")
    if _EQUESTRIAN_PATTERNS.search(name) or equipment == {"Horse"}:
        tags.add("equestrian")
    if use_types == ["Day"]:
        tags.add("day_use")

    # Inclusion-type categories
    if _BACKPACKING_PATTERNS.search(name):
        tags.add("backpacking")
    if _LODGING_PATTERNS.search(name):
        tags.add("lodging")
    if equipment & {"RV", "Trailer", "Fifth Wheel", "Pop up", "Caravan/Camper Van", "Pickup Camper", "RV/Motorhome"}:
        tags.add("rv")
    if "Tent" in equipment:
        tags.add("tent")

    # If no specific type was detected, default to tent camping
    if not tags & {"backpacking", "lodging", "rv", "tent", "boat_in", "equestrian", "day_use"}:
        tags.add("tent")

    return tags


_SITE_TYPE_MAP = {
    "BOAT IN": "boat_in",
    "HIKE TO": "backpacking",
    "WALK TO": "backpacking",
    "GROUP HIKE TO": "backpacking",
    "GROUP WALK TO": "backpacking",
    "STANDARD NONELECTRIC": "tent",
    "STANDARD ELECTRIC": "tent",
    "TENT ONLY NONELECTRIC": "tent",
    "TENT ONLY ELECTRIC": "tent",
    "GROUP TENT ONLY NONELECTRIC": "tent",
    "GROUP TENT ONLY ELECTRIC": "tent",
    "GROUP TENT ONLY AREA NONELECTRIC": "tent",
    "GROUP TENT ONLY AREA ELECTRIC": "tent",
    "GROUP STANDARD NONELECTRIC": "tent",
    "GROUP STANDARD ELECTRIC": "tent",
    "GROUP STANDARD AREA NONELECTRIC": "tent",
    "RV NONELECTRIC": "rv",
    "RV ELECTRIC": "rv",
    "EQUESTRIAN NONELECTRIC": "equestrian",
    "EQUESTRIAN ELECTRIC": "equestrian",
    "GROUP EQUESTRIAN": "equestrian",
    "CABIN NONELECTRIC": "lodging",
    "CABIN ELECTRIC": "lodging",
    "YURT": "lodging",
    "LOOKOUT": "lodging",
}


def _classify_site_type(campsite_type: str, site_name: str = "") -> str:
    """Map an availability API campsite_type to a filter category.

    Also checks the site name for keywords like 'BOAT' that override
    the formal type (some boat-in sites are typed as GROUP TENT).
    """
    # Check site name first — catches misclassified boat-in sites
    name_upper = site_name.upper()
    if "BOAT" in name_upper:
        return "boat_in"

    upper = campsite_type.upper().strip()
    if upper in _SITE_TYPE_MAP:
        return _SITE_TYPE_MAP[upper]
    # Fallback heuristics on type string
    if "BOAT" in upper:
        return "boat_in"
    if "EQUESTRIAN" in upper or "HORSE" in upper:
        return "equestrian"
    if "CABIN" in upper or "YURT" in upper or "LODGE" in upper or "LOOKOUT" in upper:
        return "lodging"
    if "RV" in upper or "TRAILER" in upper:
        return "rv"
    if "HIKE" in upper or "WALK" in upper or "BACKPACK" in upper:
        return "backpacking"
    return "tent"


def _should_filter_out(
    campground: dict, exclude: set[str], include: set[str]
) -> bool:
    """Check if a campground should be filtered out.

    exclude: categories to always remove (e.g. boat_in, equestrian, day_use)
    include: categories to show — campground must match at least one
    """
    tags = _classify_campground(campground)

    # Check exclusions first
    if tags & exclude:
        return True

    # If include filters are active, campground must match at least one
    if include:
        return not bool(tags & include)

    return False


async def _search_campgrounds(
    lat: float, lon: float, radius: float,
    exclude: set[str] | None = None,
    include: set[str] | None = None,
) -> list[dict]:
    """Search Recreation.gov for campgrounds near coordinates.

    Uses the Recreation.gov website search API which has far more
    complete data than the RIDB facilities endpoint.
    Paginates to get all results within range.

    exclude: categories to remove, e.g. {"boat_in", "equestrian", "day_use"}
    include: NOT applied here — only applied at site level in
             _parse_available_sites, since campground metadata doesn't
             reliably indicate individual site types (e.g. a campground
             may have HIKE TO sites without "backpacking" in its name).
    """
    all_results: list[dict] = []
    page_size = 50
    start = 0
    max_results = 500
    exclude = exclude or set()

    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        while start < max_results:
            resp = await client.get(
                RECGOV_SEARCH_URL,
                params={
                    "fq": "entity_type:campground",
                    "lat": lat,
                    "lng": lon,
                    "size": page_size,
                    "start": start,
                },
                headers={"User-Agent": "campsite-companion/0.1"},
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("results", [])
            if not batch:
                break

            # Filter by distance and type
            for campground in batch:
                cg_lat = campground.get("latitude")
                cg_lon = campground.get("longitude")
                if not cg_lat or not cg_lon:
                    continue
                try:
                    cg_lat = float(cg_lat)
                    cg_lon = float(cg_lon)
                except (ValueError, TypeError):
                    continue
                dist = is_within_radius((lat, lon), (cg_lat, cg_lon), radius)
                if dist is not None:
                    if _should_filter_out(campground, exclude, set()):
                        continue
                    campground["_distance_miles"] = round(dist, 1)
                    all_results.append(campground)
                else:
                    # Results are sorted by distance; once we exceed radius, stop
                    return all_results

            if len(batch) < page_size:
                break
            start += page_size

    return all_results


class RateLimitError(Exception):
    """Raised when Recreation.gov returns 429 Too Many Requests."""
    pass


async def _check_availability(facility_id: str, month_start: date) -> dict:
    """Check availability for a campground for a given month."""
    logger.debug("Checking availability: facility=%s month=%s", facility_id, month_start.strftime("%Y-%m"))
    date_str = month_start.strftime("%Y-%m-%dT00:00:00.000Z")
    url = f"{settings.recgov_availability_url}/{facility_id}/month"
    async with _SEMAPHORE:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            resp = await client.get(
                url,
                params={"start_date": date_str},
                headers={"User-Agent": "Mozilla/5.0 (compatible; campsite-companion/0.1)"},
            )
            if resp.status_code == 429:
                logger.warning("Recreation.gov rate limit hit (429) for facility %s", facility_id)
                raise RateLimitError("Recreation.gov rate limit hit (429)")
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
    availability_data: dict, start_date: date, end_date: date, num_people: int,
    exclude: set[str] | None = None, include: set[str] | None = None,
) -> tuple[list[date], list[dict]]:
    """Extract dates and per-site details for available sites in the range.

    Returns (sorted_dates, site_details) where site_details is a list of
    {"site_name": str, "site_type": str, "available_dates": [date, ...]}.
    """
    exclude = exclude or set()
    include = include or set()
    all_dates: set[date] = set()
    # Collect per-site info keyed by (site_name, site_type)
    site_map: dict[tuple[str, str], set[date]] = {}
    campsites = availability_data.get("campsites", {})
    for _site_id, site_info in campsites.items():
        site_type = site_info.get("campsite_type", "")
        site_name = site_info.get("site", "")
        loop_name = site_info.get("loop", "")
        category = _classify_site_type(site_type, site_name)
        if category in exclude:
            continue
        if include and category not in include:
            continue

        site_dates: set[date] = set()
        availabilities = site_info.get("availabilities", {})
        for date_str, status in availabilities.items():
            if status == "Available":
                try:
                    d = datetime.fromisoformat(
                        date_str.replace("T00:00:00Z", "")
                    ).date()
                    if start_date <= d < end_date:
                        site_dates.add(d)
                except ValueError:
                    continue
        if site_dates:
            display_name = site_name
            if loop_name and loop_name not in site_name:
                display_name = f"{loop_name} — {site_name}"
            key = (display_name, site_type)
            site_map.setdefault(key, set()).update(site_dates)
            all_dates.update(site_dates)

    site_details = [
        {"site_name": name, "site_type": stype, "available_dates": sorted(dates)}
        for (name, stype), dates in sorted(site_map.items())
    ]
    return sorted(all_dates), site_details


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
    months_to_check = _get_months_to_check(sd, ed)

    campgrounds = await _search_campgrounds(latitude, longitude, radius_miles)
    results: list[Campsite] = []

    async def check_campground(cg: dict) -> Campsite | None:
        cg_id = str(cg.get("entity_id", ""))
        if not cg_id:
            return None

        all_available: list[date] = []
        all_site_details: list[dict] = []
        for month_start in months_to_check:
            try:
                avail_data = await _check_availability(cg_id, month_start)
                dates, details = _parse_available_sites(avail_data, sd, ed, num_people)
                all_available.extend(dates)
                all_site_details.extend(details)
            except Exception:
                continue

        if not all_available:
            return None

        return Campsite(
            name=cg.get("name", "Unknown"),
            facility_id=cg_id,
            source=SearchSource.RECREATION_GOV,
            latitude=float(cg.get("latitude", 0)),
            longitude=float(cg.get("longitude", 0)),
            distance_miles=cg.get("_distance_miles"),
            available_dates=sorted(set(all_available)),
            site_availability=[
                SiteAvailability(
                    site_name=d["site_name"],
                    site_type=d["site_type"],
                    available_dates=d["available_dates"],
                )
                for d in all_site_details
            ],
            description=(cg.get("description", "") or "")[:200],
            reservation_url=f"https://www.recreation.gov/camping/campgrounds/{cg_id}",
            campsite_type=cg.get("type", ""),
        )

    tasks = [check_campground(cg) for cg in campgrounds]
    checked = await asyncio.gather(*tasks, return_exceptions=True)

    for result in checked:
        if isinstance(result, Campsite):
            results.append(result)

    results.sort(key=lambda c: c.distance_miles or 999)
    return [c.model_dump(mode="json") for c in results[:20]]
