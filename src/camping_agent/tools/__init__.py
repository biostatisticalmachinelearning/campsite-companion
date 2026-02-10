from camping_agent.tools.geocode import geocode_location
from camping_agent.tools.recreation_gov import search_recreation_gov
from camping_agent.tools.reserve_california import search_reserve_california
from camping_agent.tools.browser import open_reservation_page

ALL_TOOLS = [
    geocode_location,
    search_recreation_gov,
    search_reserve_california,
    open_reservation_page,
]
