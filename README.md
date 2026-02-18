# Campsite Companion

A web app that searches for available campsites across **Recreation.gov** (national parks, forests, BLM land) and **ReserveCalifornia** (California state parks). It streams results in real-time as each campground is checked, so you see availability as soon as it's found.

## Features

- **Campsite Search** -- Enter a location and date range to find available campsites nearby. Results stream in live as each campground is checked.
- **Next Available Date Finder** -- Pick a specific park from a catalog of 4,700+ parks and find the next date with availability. Optionally drill down to a specific campground or individual campsite.
- **Multi-source search** -- Searches Recreation.gov and ReserveCalifornia simultaneously.
- **Filters** -- Exclude boat-in, equestrian, or day-use sites. Filter to only tent, RV, backpacking, or lodging sites.
- **Weekend filtering** -- In the Next Available Date Finder, filter results to only show Fri--Sun, Sat--Sun, or Fri--Mon availability.
- **Site-level drill-down** -- After selecting a park, load its individual campgrounds and campsites. Select one or more specific sites to search.
- **Fast catalog-based discovery** -- Campground discovery uses a pre-built local catalog instead of live API calls, making search startup instant. Only availability checks hit the APIs.
- **Auto-refresh** -- The catalog automatically rebuilds in the background when the server starts if it's older than 14 days.

## Prerequisites

- **Python 3.10+**
- **A Recreation.gov RIDB API key** (free) -- get one at https://ridb.recreation.gov/. This is required for building the park catalog. The campsite availability search itself uses a public API that doesn't require a key.

## Quick Start

There's a setup script that handles everything:

```bash
git clone <repo-url>
cd campsite-companion
bash setup.sh
```

The script creates a virtual environment, installs dependencies, sets up your `.env` file, and optionally builds the park catalog. Or follow the manual steps below:

### 1. Clone and install

```bash
git clone <repo-url>
cd campsite-companion
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Set up your environment

Copy the example environment file and add your API key:

```bash
cp .env.example .env
```

Open `.env` and fill in your Recreation.gov RIDB API key:

```
RIDB_API_KEY=your-actual-api-key-here
```

The other API keys (Anthropic, OpenAI, etc.) are only needed if you want to use the CLI chat agent, not the web app.

### 3. Build the park catalog

This step downloads the list of all parks/campgrounds from both Recreation.gov and ReserveCalifornia, including facility metadata for California state parks. It takes about 5 minutes and only needs to be run once (the server will automatically rebuild it in the background if it becomes older than 14 days):

```bash
build-catalog
```

This creates two JSON files in the `data/` directory:
- `data/catalog_recgov.json` (~4,600 campgrounds)
- `data/catalog_rca.json` (~110 California state parks, with facility IDs)

### 4. Start the web server

```bash
camping-web
```

Open http://localhost:8000 in your browser.

## Using the App

### Campsite Search (main page)

1. Enter a location (city name, address, or coordinates)
2. Set your check-in and check-out dates
3. Adjust the search radius (default: 100 miles)
4. Optionally enable ReserveCalifornia to also search California state parks
5. Use the filter checkboxes to narrow results (exclude boat-in, show only tent sites, etc.)
6. Click **Search** and watch results stream in

Each result card shows the campground name, distance, available dates, and individual site details. Click **Book** to go directly to the reservation page.

### Next Available Date Finder

1. Click **Next Available Date** in the top-right corner of the main page (or go to `/next-available`)
2. Search for a park by name using the search box or letter bar
3. Select a park from the list
4. **(Optional)** After selecting a park, the app loads its campgrounds and individual sites. You can:
   - For California state parks: select a specific campground/facility from the dropdown
   - For any park: filter and select specific campsites using the checkbox list. Type a name to filter (e.g., "wildcat"), then click **Select visible** to check all matches.
5. Choose a weekend preference and lookahead period
6. Click **Find Next Available Date**

The search checks one month at a time and stops as soon as it finds availability.

## Project Structure

```
campsite-companion/
  .env.example          # Template for environment variables
  pyproject.toml        # Python package config and dependencies
  setup.sh              # Automated setup script
  data/                 # Park catalog JSON files (generated, gitignored)
  src/camping_agent/
    catalog.py          # Park catalog builder and location-based search
    config.py           # Settings loaded from .env
    geocoding.py        # Location geocoding and distance calculations
    models.py           # Pydantic models (Campsite, CatalogPark, etc.)
    tools/
      recreation_gov.py # Recreation.gov API client
      reserve_california.py # ReserveCalifornia API client
    web/
      app.py            # FastAPI server with SSE streaming and caching
      static/
        index.html      # Campsite search page
        next-available.html # Next available date finder page
```

## Troubleshooting

**"Failed to load catalog"** on the Next Available Date page:
- Run `build-catalog` first to generate the park catalog.

**Recreation.gov rate limit (429)**:
- The app automatically limits request concurrency, but Recreation.gov may still throttle you under heavy use. Wait a minute and try again.

**"No campgrounds found within radius"**:
- Try increasing the search radius.
- Make sure the location geocoded correctly (check the map pin in the status messages).

**ReserveCalifornia search returns no results**:
- ReserveCalifornia only covers California state parks. Make sure your search location is in or near California.
