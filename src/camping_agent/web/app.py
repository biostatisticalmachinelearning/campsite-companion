import asyncio
import json
from datetime import date
from pathlib import Path

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
    _search_campgrounds,
)
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


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
        campgrounds = await _search_campgrounds(lat, lon, radius, exclude, include)
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
        count = 0
        async for result in search_rca_api(lat, lon, radius, sd, ed, num_people, exclude, include):
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
