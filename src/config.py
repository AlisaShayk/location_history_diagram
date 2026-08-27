"""
Configuration constants for Country Timeline.

All lookup tables, color maps, and tunable parameters live here.
"""

import colorsys
import os
from datetime import timedelta

import pycountry
import pytz


# ─── Paths (relative to project root) ─────────────────────────────────────────
# User-provided files live at the project root level (data/).
# Bundled reference data lives alongside the source (src/).

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
INPUT_FILE = os.path.join(PROJECT_DIR, 'data', 'location-history.json')
OVERRIDES_FILE = os.path.join(PROJECT_DIR, 'data', 'overrides.json')
AIRPORTS_FILE = os.path.join(SCRIPT_DIR, 'airports.json')
OUTPUT_DIR = os.path.join(PROJECT_DIR, 'output')
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, 'template.html')


# ─── Tunable parameters ───────────────────────────────────────────────────────

AIRPORT_RADIUS_KM = 3.0
AIRPORT_TRANSIT_MAX_HOURS = 12
NO_DATA_GAP_THRESHOLD = timedelta(hours=24)


# ─── Timezone lookup (local time → UTC for travel overrides) ──────────────────
# Uses pytz.country_timezones (covers 247 countries).
# For multi-timezone countries the first entry is used (usually the capital).
# Travel override times are approximate anyway — a few hours off is acceptable.


def get_country_timezone(cc):
    """Get the primary IANA timezone name for a country code.
    Returns None if the country code is unknown."""
    tz_list = pytz.country_timezones.get(cc, [])
    return tz_list[0] if tz_list else None


# ─── Country names ────────────────────────────────────────────────────────────


def get_country_name(cc):
    """Get country name for a two-letter code via pycountry.
    Prefers common_name (e.g. 'South Korea') over official name."""
    try:
        country = pycountry.countries.get(alpha_2=cc)
        if country:
            return getattr(country, 'common_name', country.name)
    except Exception:
        pass
    return cc


# ─── Visualization colors ─────────────────────────────────────────────────────
# Evenly spaces hues around the color wheel based on how many countries
# the user actually visited. This guarantees maximum visual separation.

NO_DATA_COLOR = '#374151'


def build_color_map(country_codes):
    """Given an ordered list of country codes, return {cc: '#hex'} with well-separated colors.
    Uses the golden angle to step through the hue wheel — each successive country
    lands as far as possible from all previous ones.
    Pass codes sorted by importance (e.g. most time first) so the dominant
    countries get the most distinct hues."""
    codes = [cc for cc in country_codes if cc != '--']
    color_map = {'--': NO_DATA_COLOR}
    golden_angle = 0.618033988749895  # 1 / phi
    for i, cc in enumerate(codes):
        h = (i * golden_angle) % 1.0
        r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.65)
        color_map[cc] = f'#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}'
    return color_map
