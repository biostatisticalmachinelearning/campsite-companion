import webbrowser

from langchain_core.tools import tool


@tool
def open_reservation_page(url: str, campsite_name: str) -> str:
    """Open the reservation/booking page in the user's default web browser.

    Use this after the user has selected a campsite from the search results.
    """
    webbrowser.open(url)
    return f"Opened reservation page for {campsite_name} in your browser: {url}"
