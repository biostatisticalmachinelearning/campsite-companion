import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from camping_agent.geocoding import geocode
from camping_agent.tools.recreation_gov import (
    RateLimitError,
    _check_availability,
    _get_months_to_check,
    _parse_available_sites,
)
from camping_agent.catalog import catalog_is_stale, load_catalog, search_catalog_by_location
from camping_agent.config import settings
from camping_agent.models import Campsite, SearchSource, SiteAvailability

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Camping Reservation Search")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SearchRequest(BaseModel):
    location: str
    radius_miles: float = 100.0
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    num_people: int = 1
    search_recreation_gov: bool = True
    search_reserve_california: bool = False
    exclude_boat_in: bool = False
    exclude_equestrian: bool = False
    exclude_day_use: bool = False
    include_tent: bool = False
    include_rv: bool = False
    include_backpacking: bool = False
    include_lodging: bool = False


class GeocodeRequest(BaseModel):
    location: str


class NextAvailableRequest(BaseModel):
    park_id: str
    source: str
    filter_days: list[int] | None = None  # weekday numbers: 0=Mon..6=Sun; None=any
    lookahead_months: int = 6
    search_all_months: bool = False
    facility_id: str | None = None
    site_names: list[str] | None = None


RCA_API_BASE = (
    "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr"
)

# In-memory TTL cache for park children (site metadata)
_children_cache: dict[str, tuple[float, dict]] = {}
_CHILDREN_TTL = 86400  # 24 hours


@app.on_event("startup")
async def _refresh_catalog_if_stale():
    """Rebuild the park catalog in the background if it's older than 14 days."""
    if not catalog_is_stale():
        return

    async def _rebuild():
        try:
            from camping_agent.catalog import build_all
            print("Catalog is stale (>14 days old), rebuilding in background...")
            await build_all()
        except Exception as e:
            print(f"Background catalog rebuild failed: {e}")

    asyncio.create_task(_rebuild())


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/next-available")
async def next_available_page():
    return FileResponse(STATIC_DIR / "next-available.html")


@app.get("/api/catalog")
async def api_catalog(q: str | None = None):
    parks = load_catalog(q)
    return {
        "parks": [p.model_dump(mode="json") for p in parks],
    }


@app.get("/api/park-children/{source}/{park_id}")
async def api_park_children(source: str, park_id: str):
    cache_key = f"{source}:{park_id}"
    cached = _children_cache.get(cache_key)
    if cached:
        ts, data = cached
        if time.time() - ts < _CHILDREN_TTL:
            return data

    if source == "recreation_gov":
        result = await _get_recgov_children(park_id)
    elif source == "reserve_california":
        result = await _get_rca_children(park_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    _children_cache[cache_key] = (time.time(), result)
    return result


async def _get_recgov_children(park_id: str) -> dict:
    """Get individual sites for a Recreation.gov campground."""
    month_start = date.today().replace(day=1)
    try:
        avail_data = await _check_availability(park_id, month_start)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch availability: {e}")

    campsites = avail_data.get("campsites", {})
    seen = set()
    sites = []
    for _site_id, site_info in campsites.items():
        name = site_info.get("site", "")
        stype = site_info.get("campsite_type", "")
        loop = site_info.get("loop", "")
        display_name = name
        if loop and loop not in name:
            display_name = f"{loop} — {name}"
        if display_name not in seen:
            seen.add(display_name)
            sites.append({"name": display_name, "type": stype})
    sites.sort(key=lambda s: s["name"])
    return {"sites": sites}


async def _get_rca_children(park_id: str) -> dict:
    """Get facilities and units for a ReserveCalifornia park."""
    start_date = date.today()
    end_date = start_date + timedelta(days=7)
    api_end = (end_date - timedelta(days=1)).isoformat()

    # Try to get facility list from catalog to skip the place API call
    catalog_facility_ids: dict[str, str] | None = None
    catalog = load_catalog()
    for park in catalog:
        if park.id == park_id and park.facilities is not None:
            catalog_facility_ids = {f.id: f.name for f in park.facilities}
            break

    if catalog_facility_ids is None:
        # Fallback: fetch facility list from place API
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RCA_API_BASE}/search/place",
                json={
                    "PlaceId": int(park_id),
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
            data = resp.json()

        selected = data.get("SelectedPlace", {})
        facilities_raw = selected.get("Facilities", {})
        if not isinstance(facilities_raw, dict):
            return {"facilities": []}
        catalog_facility_ids = {
            str(fac_id): fac.get("Name", "") for fac_id, fac in facilities_raw.items()
        }

    # Fetch unit details via grid API for each facility
    from camping_agent.tools.reserve_california import _classify_unit

    facilities = []
    async with httpx.AsyncClient(timeout=30) as client:
        for fac_id, fac_name in catalog_facility_ids.items():
            units = []
            try:
                grid_resp = await client.post(
                    f"{RCA_API_BASE}/search/grid",
                    json={
                        "PlaceId": int(park_id),
                        "FacilityId": int(fac_id),
                        "StartDate": start_date.isoformat(),
                        "EndDate": api_end,
                        "Nights": 1,
                        "IsADA": False,
                        "UnitCategoryId": 0,
                        "SleepingUnitId": 0,
                        "MinVehicleLength": 0,
                    },
                )
                grid_resp.raise_for_status()
                grid_data = grid_resp.json()
            except Exception:
                grid_data = {}

            grid_units = grid_data.get("Facility", {}).get("Units", {})
            if isinstance(grid_units, dict):
                for _uid, unit in grid_units.items():
                    units.append({
                        "id": str(_uid),
                        "name": unit.get("Name", ""),
                        "type": _classify_unit(unit),
                    })
                units.sort(key=lambda u: u["name"])

            facilities.append({
                "id": str(fac_id),
                "name": fac_name,
                "units": units,
            })
    facilities.sort(key=lambda f: f["name"])
    return {"facilities": facilities}


@app.post("/api/next-available")
async def api_next_available(req: NextAvailableRequest):
    """Stream next-available search via SSE."""
    filter_days = set(req.filter_days) if req.filter_days else None
    months = _get_lookahead_months(req.lookahead_months)

    async def event_stream():
        if req.source == "recreation_gov":
            async for event in _stream_next_recgov(
                req.park_id, months, filter_days, req.site_names, req.search_all_months
            ):
                yield event
        elif req.source == "reserve_california":
            async for event in _stream_next_rca(
                req.park_id, months, filter_days, req.facility_id, req.site_names,
                req.search_all_months,
            ):
                yield event
        else:
            yield _sse("error", {"message": f"Unknown source: {req.source}"})
        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _get_lookahead_months(n: int) -> list[date]:
    """Return first-of-month dates for the next N months."""
    today = date.today()
    current = today.replace(day=1)
    months = []
    for _ in range(n):
        months.append(current)
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def _filter_days(dates: list[date], filter_days: set[int] | None) -> list[date]:
    """Filter dates to only those matching selected weekdays (0=Mon..6=Sun)."""
    if filter_days is None:
        return dates
    return [d for d in dates if d.weekday() in filter_days]


async def _stream_next_recgov(
    park_id: str, months: list[date], filter_days: set[int] | None,
    site_names: list[str] | None = None, search_all_months: bool = False,
):
    """Yield SSE events for next-available search on a Recreation.gov campground."""
    site_name_set = set(site_names) if site_names else None
    total = len(months)
    found_any = False

    for i, month_start in enumerate(months):
        month_label = month_start.strftime("%B %Y")
        yield _sse("progress", {
            "message": f"Checking {month_label}...",
            "months_checked": i,
            "total_months": total,
        })

        try:
            avail_data = await _check_availability(park_id, month_start)
        except RateLimitError:
            yield _sse("error", {
                "message": "Recreation.gov rate limit hit (429). Wait a minute and try again."
            })
            return
        except Exception as e:
            yield _sse("status", {"message": f"Error checking {month_label}: {e}"})
            continue

        # Parse with wide date range covering the whole month
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        dates, details = _parse_available_sites(avail_data, month_start, month_end, 1)

        # Filter by site name if specified
        if site_name_set:
            filtered_details = [d for d in details if d["site_name"] in site_name_set]
            filtered_dates = set()
            for d in filtered_details:
                filtered_dates.update(d["available_dates"])
            dates = sorted(filtered_dates)
            details = filtered_details

        # Filter by selected days
        dates = _filter_days(dates, filter_days)
        if filter_days:
            details = [
                {
                    "site_name": d["site_name"],
                    "site_type": d["site_type"],
                    "available_dates": _filter_days(d["available_dates"], filter_days),
                }
                for d in details
            ]
            details = [d for d in details if d["available_dates"]]

        if dates:
            found_any = True
            # Serialize dates to ISO strings
            serialized_details = []
            for d in details:
                serialized_details.append({
                    "site_name": d["site_name"],
                    "site_type": d["site_type"],
                    "available_dates": [
                        dt.isoformat() if hasattr(dt, "isoformat") else dt
                        for dt in d["available_dates"]
                    ],
                })
            yield _sse("found", {
                "month": month_label,
                "available_dates": [d.isoformat() for d in dates],
                "site_availability": serialized_details,
            })
            if not search_all_months:
                return

        await asyncio.sleep(0.3)

    if not found_any:
        yield _sse("not_found", {
            "message": f"No availability found in the next {total} months.",
        })


async def _stream_next_rca(
    park_id: str, months: list[date], filter_days: set[int] | None,
    facility_id: str | None = None, site_names: list[str] | None = None,
    search_all_months: bool = False,
):
    """Yield SSE events for next-available search on a ReserveCalifornia park."""
    site_name_set = set(site_names) if site_names else None
    total = len(months)
    found_any = False

    # Try to get facility list from catalog to skip per-month place API calls
    catalog_facility_ids: dict[str, str] | None = None
    catalog = load_catalog()
    for park in catalog:
        if park.id == park_id and park.facilities is not None:
            catalog_facility_ids = {f.id: f.name for f in park.facilities}
            break

    for i, month_start in enumerate(months):
        month_label = month_start.strftime("%B %Y")
        yield _sse("progress", {
            "message": f"Checking {month_label}...",
            "months_checked": i,
            "total_months": total,
        })

        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        nights = (month_end - month_start).days
        api_end = (month_end - timedelta(days=1)).isoformat()

        try:
            if catalog_facility_ids is not None:
                # Use catalog — skip place API call
                fac_ids_to_check = catalog_facility_ids
            else:
                # Fallback: fetch facility list from place API
                async with httpx.AsyncClient(timeout=30) as client:
                    place_resp = await client.post(
                        f"{RCA_API_BASE}/search/place",
                        json={
                            "PlaceId": int(park_id),
                            "Latitude": 0,
                            "Longitude": 0,
                            "StartDate": month_start.isoformat(),
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

                selected = place_data.get("SelectedPlace", {})
                facilities_raw = selected.get("Facilities", {})
                if not isinstance(facilities_raw, dict):
                    continue
                fac_ids_to_check = {
                    str(fac_id): fac.get("Name", "")
                    for fac_id, fac in facilities_raw.items()
                }

            all_dates: set[date] = set()
            all_site_details: list[dict] = []

            for fac_id, fac_name in fac_ids_to_check.items():
                if facility_id and str(fac_id) != facility_id:
                    continue

                async with httpx.AsyncClient(timeout=30) as client:
                    grid_resp = await client.post(
                        f"{RCA_API_BASE}/search/grid",
                        json={
                            "PlaceId": int(park_id),
                            "FacilityId": int(fac_id),
                            "StartDate": month_start.isoformat(),
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

                units = grid_data.get("Facility", {}).get("Units", {})
                if not isinstance(units, dict):
                    continue

                from camping_agent.tools.reserve_california import _classify_unit

                for _uid, unit in units.items():
                    if unit.get("AvailableCount", 0) <= 0:
                        continue

                    unit_name = unit.get("Name", "Unknown")
                    display_name = unit_name
                    if fac_name and fac_name not in unit_name:
                        display_name = f"{fac_name} — {unit_name}"

                    if site_name_set and display_name not in site_name_set:
                        continue

                    cat = _classify_unit(unit)
                    avail_dates = []
                    slices = unit.get("Slices", {})
                    for _ts, sl in slices.items():
                        if sl.get("IsFree"):
                            try:
                                d = date.fromisoformat(sl["Date"])
                                avail_dates.append(d)
                            except (ValueError, KeyError):
                                continue

                    avail_dates = _filter_days(avail_dates, filter_days)
                    if avail_dates:
                        all_dates.update(avail_dates)
                        all_site_details.append({
                            "site_name": display_name,
                            "site_type": cat,
                            "available_dates": [d.isoformat() for d in sorted(avail_dates)],
                        })

            if all_dates:
                found_any = True
                yield _sse("found", {
                    "month": month_label,
                    "available_dates": [d.isoformat() for d in sorted(all_dates)],
                    "site_availability": all_site_details,
                })
                if not search_all_months:
                    return

        except Exception as e:
            yield _sse("status", {"message": f"Error checking {month_label}: {e}"})
            continue

        await asyncio.sleep(0.3)

    if not found_any:
        yield _sse("not_found", {
            "message": f"No availability found in the next {total} months.",
        })


@app.post("/api/geocode")
async def api_geocode(req: GeocodeRequest):
    try:
        lat, lon = geocode(req.location)
        return {"latitude": lat, "longitude": lon, "location": req.location}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Stream search results via Server-Sent Events as each facility is checked."""
    # Geocode up front (fast) so we can fail early
    try:
        lat, lon = geocode(req.location)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    sd = date.fromisoformat(req.start_date)
    ed = date.fromisoformat(req.end_date)

    exclude = set()
    if req.exclude_boat_in:
        exclude.add("boat_in")
    if req.exclude_equestrian:
        exclude.add("equestrian")
    if req.exclude_day_use:
        exclude.add("day_use")

    include = set()
    if req.include_tent:
        include.add("tent")
    if req.include_rv:
        include.add("rv")
    if req.include_backpacking:
        include.add("backpacking")
    if req.include_lodging:
        include.add("lodging")

    async def event_stream():
        # Send search metadata
        yield _sse("meta", {
            "search_center": {"latitude": lat, "longitude": lon, "location": req.location},
        })

        # Recreation.gov (uses public search API, no key required)
        if req.search_recreation_gov:
            async for event in _stream_recgov(lat, lon, req.radius_miles, sd, ed, req.num_people, exclude, include):
                yield event

        # ReserveCalifornia
        if req.search_reserve_california:
            async for event in _stream_rca(lat, lon, req.radius_miles, sd, ed, req.num_people, exclude, include):
                yield event

        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_recgov(
    lat: float, lon: float, radius: float, sd: date, ed: date, num_people: int,
    exclude: set[str] | None = None, include: set[str] | None = None,
):
    """Yield SSE events as each Recreation.gov campground is checked."""
    months = _get_months_to_check(sd, ed)

    try:
        yield _sse("status", {"message": "Searching Recreation.gov campgrounds..."})
        campgrounds = search_catalog_by_location(lat, lon, radius, source=SearchSource.RECREATION_GOV)
        yield _sse("status", {
            "message": f"Found {len(campgrounds)} campgrounds within {radius} mi, checking availability..."
        })
    except Exception as e:
        yield _sse("error", {"message": f"Recreation.gov search failed: {e}"})
        return

    if not campgrounds:
        yield _sse("status", {"message": "No campgrounds found within radius."})
        return

    checked_count = 0
    result_count = 0

    rate_limited = False

    async def check_one(cg: dict) -> Campsite | None:
        nonlocal rate_limited
        cg_id = str(cg.get("entity_id", ""))
        if not cg_id:
            return None

        all_available: list[date] = []
        all_site_details: list[dict] = []
        for month_start in months:
            try:
                avail_data = await _check_availability(cg_id, month_start)
                dates, details = _parse_available_sites(avail_data, sd, ed, num_people, exclude, include)
                all_available.extend(dates)
                all_site_details.extend(details)
            except RateLimitError:
                rate_limited = True
                return None
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
            description=(cg.get("description", "") or "")[:300],
            reservation_url=f"https://www.recreation.gov/camping/campgrounds/{cg_id}",
            campsite_type=cg.get("type", ""),
        )

    # Process in batches of 5 for streaming (smaller to reduce rate limiting)
    batch_size = 5
    for i in range(0, len(campgrounds), batch_size):
        batch = campgrounds[i : i + batch_size]
        tasks = [check_one(cg) for cg in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            checked_count += 1
            if isinstance(r, Campsite):
                result_count += 1
                yield _sse("result", r.model_dump(mode="json"))

        yield _sse("progress", {
            "checked": checked_count,
            "total": len(campgrounds),
            "found": result_count,
        })

        if rate_limited:
            yield _sse("error", {
                "message": "Recreation.gov rate limit hit (429). Wait a minute and try again."
            })
            break

        if result_count >= 30:
            break

        # Small delay between batches to avoid rate limiting
        await asyncio.sleep(0.5)


async def _stream_rca(
    lat: float, lon: float, radius: float, sd: date, ed: date, num_people: int,
    exclude: set[str] | None = None, include: set[str] | None = None,
):
    """Yield SSE events for ReserveCalifornia search, streaming as found."""
    yield _sse("status", {"message": "Searching ReserveCalifornia..."})
    try:
        from camping_agent.tools.reserve_california import search_rca_api
        # Use catalog for park discovery to skip live search API calls
        catalog_parks = search_catalog_by_location(lat, lon, radius, source=SearchSource.RESERVE_CALIFORNIA)
        count = 0
        async for result in search_rca_api(
            lat, lon, radius, sd, ed, num_people, exclude, include,
            catalog_parks=catalog_parks if catalog_parks else None,
        ):
            count += 1
            yield _sse("result", result)
            yield _sse("progress", {
                "checked": count,
                "total": 0,
                "found": count,
                "source": "ReserveCalifornia",
            })
        if count == 0:
            yield _sse("status", {"message": "No ReserveCalifornia results found."})
    except Exception as e:
        yield _sse("error", {"message": f"ReserveCalifornia search failed: {e}"})


def main():
    print("Starting Camping Reservation Search at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
