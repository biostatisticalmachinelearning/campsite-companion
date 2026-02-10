from datetime import date

from langchain_core.tools import tool

from camping_agent.models import Campsite, SearchSource


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

    This uses browser automation and may be slow or occasionally fail.
    Dates should be in YYYY-MM-DD format.
    """
    try:
        return await _scrape_reserve_california(
            latitude,
            longitude,
            radius_miles,
            date.fromisoformat(start_date),
            date.fromisoformat(end_date),
            num_people,
        )
    except Exception as e:
        return [
            {
                "error": (
                    f"ReserveCalifornia search failed: {e}. "
                    "Results from Recreation.gov may still be available."
                )
            }
        ]


async def _scrape_reserve_california(
    lat: float,
    lon: float,
    radius: float,
    start: date,
    end: date,
    num_people: int,
) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(20_000)

        await page.goto("https://www.reservecalifornia.com/Web2/")

        # Enter search criteria
        # NOTE: These selectors are best-effort and will likely need adjustment
        # after inspecting the live ReserveCalifornia DOM structure.
        search_input = page.locator("#txtSearchparkali498").first
        await search_input.fill("camping")

        # Set dates
        checkin = page.locator("#mainContent_txtArrivalDate").first
        await checkin.fill(start.strftime("%m/%d/%Y"))
        checkout = page.locator("#mainContent_txtDepartureDate").first
        await checkout.fill(end.strftime("%m/%d/%Y"))

        # Submit search
        search_btn = page.locator("#btnSearch").first
        await search_btn.click()

        # Wait for results
        await page.wait_for_selector(".search-result-item", timeout=15_000)

        items = await page.locator(".search-result-item").all()
        results: list[dict] = []
        for item in items[:15]:
            try:
                name_el = item.locator(".facility-name").first
                name = await name_el.text_content() or "Unknown"
                link_el = item.locator("a").first
                link = await link_el.get_attribute("href") or ""
                url = (
                    f"https://www.reservecalifornia.com{link}"
                    if link and not link.startswith("http")
                    else link
                )
                results.append(
                    Campsite(
                        name=name.strip(),
                        facility_id=link,
                        source=SearchSource.RESERVE_CALIFORNIA,
                        reservation_url=url,
                    ).model_dump(mode="json")
                )
            except Exception:
                continue

        await browser.close()
        return results
