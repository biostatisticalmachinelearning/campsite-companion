from datetime import date
from enum import Enum

from pydantic import BaseModel


class SearchSource(str, Enum):
    RECREATION_GOV = "recreation_gov"
    RESERVE_CALIFORNIA = "reserve_california"


class Campsite(BaseModel):
    name: str
    facility_id: str
    source: SearchSource
    latitude: float | None = None
    longitude: float | None = None
    distance_miles: float | None = None
    available_dates: list[date] = []
    description: str = ""
    reservation_url: str = ""
    campsite_type: str = ""


class SearchResults(BaseModel):
    campsites: list[Campsite] = []
    errors: list[str] = []
