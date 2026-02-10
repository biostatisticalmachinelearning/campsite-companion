"""Test the camping agent tools directly, without an LLM.

Usage:
    .venv/bin/python test_tools.py

Requires RIDB_API_KEY in .env file.
"""

import asyncio
import json
from datetime import date, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from camping_agent.config import settings
from camping_agent.geocoding import geocode, distance_miles
from camping_agent.tools.recreation_gov import search_recreation_gov
from camping_agent.tools.browser import open_reservation_page

console = Console()


async def main():
    console.print(Panel("[bold]Camping Agent — Tool Test (no LLM)[/]", style="green"))

    # Step 1: Test geocoding
    console.print("\n[bold cyan]Step 1: Geocoding[/]")
    location = "San Francisco"
    lat, lon = geocode(location)
    console.print(f"  {location} → ({lat}, {lon})")

    # Step 2: Set search parameters
    start = date.today() + timedelta(days=5)
    end = start + timedelta(days=2)
    radius = 150.0
    num_people = 2

    console.print(f"\n[bold cyan]Step 2: Search parameters[/]")
    console.print(f"  Location: {location} ({lat}, {lon})")
    console.print(f"  Dates: {start} to {end}")
    console.print(f"  Radius: {radius} miles")
    console.print(f"  Group size: {num_people}")

    if not settings.ridb_api_key or settings.ridb_api_key == "your_recreation_gov_api_key":
        console.print("\n[bold red]Error:[/] RIDB_API_KEY not set in .env")
        console.print("Get one at https://ridb.recreation.gov/")
        return

    # Step 3: Search Recreation.gov
    console.print(f"\n[bold cyan]Step 3: Searching Recreation.gov...[/]")
    results = await search_recreation_gov.ainvoke({
        "latitude": lat,
        "longitude": lon,
        "radius_miles": radius,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "num_people": num_people,
    })

    if not results:
        console.print("  [yellow]No available campsites found for those dates.[/]")
        console.print("  Try adjusting dates or increasing the radius.")
        return

    # Step 4: Display results
    console.print(f"\n[bold cyan]Step 4: Results ({len(results)} campgrounds found)[/]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Name")
    table.add_column("Distance", justify="right")
    table.add_column("Available Dates")
    table.add_column("Type")

    for i, site in enumerate(results, 1):
        dates = site.get("available_dates", [])
        date_str = ", ".join(dates[:5])
        if len(dates) > 5:
            date_str += f" (+{len(dates) - 5} more)"
        table.add_row(
            str(i),
            site["name"],
            f"{site.get('distance_miles', '?')} mi",
            date_str,
            site.get("campsite_type", ""),
        )

    console.print(table)

    # Step 5: Let user pick
    console.print()
    choice = console.input(
        f"[bold cyan]Pick a campsite (1-{len(results)}) or 'q' to quit:[/] "
    )
    if choice.strip().lower() in ("q", "quit", ""):
        console.print("Done!")
        return

    try:
        idx = int(choice) - 1
        selected = results[idx]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection[/]")
        return

    console.print(f"\n  Selected: [bold]{selected['name']}[/]")
    console.print(f"  URL: {selected['reservation_url']}")

    # Step 6: Open browser
    open_it = console.input("\n[bold cyan]Open in browser? (y/n):[/] ")
    if open_it.strip().lower() in ("y", "yes"):
        result = open_reservation_page.invoke({
            "url": selected["reservation_url"],
            "campsite_name": selected["name"],
        })
        console.print(f"  {result}")
    else:
        console.print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
