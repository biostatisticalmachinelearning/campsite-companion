from langchain_core.tools import tool

from camping_agent.geocoding import geocode


@tool
def geocode_location(location: str) -> dict:
    """Convert a city or place name to latitude and longitude coordinates.

    Use this when the user mentions a location by name and you need coordinates
    for searching campgrounds.
    """
    lat, lon = geocode(location)
    return {"latitude": lat, "longitude": lon, "location": location}
