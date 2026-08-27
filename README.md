# Country Timeline

Turns your Google Location History into a visual country-by-country timeline.
Useful for visa applications, tax residency tracking, or just seeing where you've been.

## Getting Your Location History

This tool relies on Timeline data collected by Google Maps. If Timeline is turned off on your device, there is no data to analyze and the tool has nothing to work with.

1. Open **Google Maps** on your phone
2. Tap your profile picture → **Settings** → **Location and privacy**
3. Make sure **Timeline** is **On**
4. Under **Timeline**, tap **Export Timeline data**
5. Download the file — it will be a JSON file with your visits and routes
6. Rename it to `location-history.json` and place it in `data/`

## Quick Start

1. Install dependencies: `pip3 install -r requirements.txt`
2. Run: `python3 src/build_country_timeline.py`
3. Open `output/travel_history.html`

## Output

| File | Description |
|------|-------------|
| `output/travel_history.html` | Interactive circular timeline + trip table per year |
| `output/country_timeline.json` | Machine-readable timeline (JSON) |
| `output/country_timeline.csv` | Machine-readable timeline (CSV) |

## Overrides

GPS data has gaps and errors. Use `data/overrides.json` to correct them.
The file uses a structured format with two sections:

```json
{
  "stays": [ ... ],
  "travel": [ ... ]
}
```

All sections are optional — include only what you need.

### Stay overrides

Use when you were in a country but GPS doesn't show it:

```json
{
  "stays": [
    {"from": "2024-03-10", "to": "2024-03-15", "country_code": "JP"}
  ]
}
```

### Travel overrides

Flights or buses between countries. Times are **local** (as printed on your ticket):

```json
{
  "travel": [
    {"depart": "2024-06-16T19:20", "arrive": "2024-06-16T23:05", "from": "CZ", "to": "TR"},
    {"depart": "2024-06-17T01:50", "arrive": "2024-06-17T05:40", "from": "TR", "to": "KR"}
  ]
}
```


## Project Structure

```
├── src/
│   ├── build_country_timeline.py   # Entry point
│   ├── config.py                   # Paths, colors, timezone lookup
│   ├── pipeline.py                 # Geocoding → spans → overrides
│   ├── html_report.py              # HTML report data preparation
│   ├── template.html               # HTML/CSS/JS visualization
│   └── airports.json               # Airport coordinates (bundled)
├── data/
│   ├── location-history.json       # ← YOUR DATA (from Google Maps export)
│   └── overrides.json              # Manual corrections (optional)
├── output/                         # Generated files (gitignored)
├── requirements.txt
└── README.md
```

## Note

This is a vibecoded MVP — it works well for what it does, but don't expect production polish.

## How It Works

1. Reads your Google Maps location history and figures out which country each GPS point belongs to
2. Groups nearby points into continuous stays per country
3. Filters out airport layovers — if you had a connecting flight through a country and all your GPS points are near an airport, it won't count as a visit
4. Applies your manual corrections (flights, stays) from `overrides.json`
5. Generates an interactive HTML report with a circular timeline and trip table for each year
