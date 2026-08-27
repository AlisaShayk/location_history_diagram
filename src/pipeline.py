"""
Pipeline: raw Google Location History records → clean country spans.

Functions are ordered as they run in the pipeline:
  1. geocode_records  — extract coords, batch geocode, discard cross-country segments
  2. build_spans      — merge into country spans
  3. filter_airport_transits — remove airport-only layovers
  4. merge_consecutive — combine adjacent same-country spans
  5. apply_travel_overrides / apply_stay_overrides
  6. clamp_overlaps / insert_no_data_gaps
"""

import json
import os
from datetime import datetime, timedelta, timezone
from math import radians, sin, cos, sqrt, atan2
from zoneinfo import ZoneInfo

import reverse_geocoder as rg

from config import (
    OVERRIDES_FILE, AIRPORTS_FILE,
    AIRPORT_RADIUS_KM, AIRPORT_TRANSIT_MAX_HOURS, NO_DATA_GAP_THRESHOLD,
    get_country_timezone, get_country_name,
)


# ─── Parsing helpers ───────────────────────────────────────────────────────────

def parse_geo(geo_str):
    """Parse 'geo:lat,lon' → (lat, lon) or None."""
    if not geo_str or not geo_str.startswith('geo:'):
        return None
    parts = geo_str[4:].split(',')
    if len(parts) == 2:
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None
    return None


def parse_time(time_str):
    """Parse ISO timestamp → UTC datetime."""
    if not time_str:
        return None
    try:
        if time_str.endswith('Z'):
            dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        else:
            dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def local_to_utc(time_str, country_code):
    """Convert a local time string + country code → UTC datetime."""
    naive = datetime.fromisoformat(time_str)
    tz_name = get_country_timezone(country_code)
    if tz_name:
        localized = naive.replace(tzinfo=ZoneInfo(tz_name))
    else:
        localized = naive.replace(tzinfo=timezone.utc)
    return localized.astimezone(timezone.utc)


# ─── Coordinate extraction (single implementation, no duplication) ─────────────

def _extract_coords_from_record(record, all_points=False):
    """Extract GPS coordinates from a single location history record.

    all_points=False (default): returns start/end coords only (for geocoding).
    all_points=True: returns every available point (for airport proximity checks).
    """
    coords = []
    if 'visit' in record:
        c = parse_geo(record['visit'].get('topCandidate', {}).get('placeLocation', ''))
        if c:
            coords.append(c)
    elif 'activity' in record:
        for key in ('start', 'end'):
            c = parse_geo(record['activity'].get(key, ''))
            if c:
                coords.append(c)
    elif 'timelinePath' in record:
        points = record['timelinePath']
        if points:
            if all_points:
                for p in points:
                    c = parse_geo(p.get('point', ''))
                    if c:
                        coords.append(c)
            else:
                first = parse_geo(points[0].get('point', ''))
                last = parse_geo(points[-1].get('point', ''))
                if first:
                    coords.append(first)
                if last and last != first:
                    coords.append(last)
    return coords


def get_coords_in_timerange(data, start_dt, end_dt):
    """Extract all GPS coordinates from records overlapping [start_dt, end_dt)."""
    coords = []
    for record in data:
        rec_start = parse_time(record.get('startTime'))
        rec_end = parse_time(record.get('endTime'))
        if not rec_start or not rec_end:
            continue
        if rec_end <= start_dt or rec_start >= end_dt:
            continue
        coords.extend(_extract_coords_from_record(record, all_points=True))
    return coords


# ─── Geo math ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def is_near_airport(lat, lon, airports, radius_km=AIRPORT_RADIUS_KM):
    """Check if a point is within radius_km of any known airport."""
    return any(haversine_km(lat, lon, ap[0], ap[1]) <= radius_km for ap in airports)


# ─── Data loading ──────────────────────────────────────────────────────────────

def load_airports():
    """Load airport coordinates from JSON file."""
    if not os.path.exists(AIRPORTS_FILE):
        return []
    with open(AIRPORTS_FILE, 'r') as f:
        return json.load(f)


def load_overrides():
    """Load and parse overrides.json → (stay_fills, travels).

    Supports two formats:
      Structured (recommended):
        {"stays": [...], "travel": [...]}
      Legacy flat array:
        [{"type": "stay", ...}, {"type": "travel", ...}, ...]
    """
    if not os.path.exists(OVERRIDES_FILE):
        return [], []
    with open(OVERRIDES_FILE, 'r') as f:
        overrides = json.load(f)

    # Structured format: top-level dict with named sections
    if isinstance(overrides, dict):
        stays = [o for o in overrides.get('stays', []) if '_comment' not in o]
        travels = [o for o in overrides.get('travel', []) if '_comment' not in o]
        return stays, travels

    # Legacy flat array format
    stays, travels = [], []
    for ov in overrides:
        if '_comment' in ov:
            continue
        ov_type = ov.get('type', 'stay')
        if ov_type == 'stay':
            stays.append(ov)
        elif ov_type == 'travel':
            travels.append(ov)
    return stays, travels


def parse_travels(travel_overrides):
    """Parse travel overrides into UTC dicts with depart/arrive datetimes."""
    parsed = []
    for ov in travel_overrides:
        parsed.append({
            'depart': local_to_utc(ov['depart'], ov['from']),
            'arrive': local_to_utc(ov['arrive'], ov['to']),
            'from_cc': ov['from'],
            'to_cc': ov['to'],
        })
    parsed.sort(key=lambda x: x['depart'])
    return parsed


# ─── Pipeline steps ───────────────────────────────────────────────────────────

def geocode_records(data):
    """Extract GPS coords, batch geocode, discard cross-country flight segments."""
    entries = []
    no_coord_count = 0

    for record in data:
        start_dt = parse_time(record.get('startTime'))
        end_dt = parse_time(record.get('endTime'))
        if not start_dt or not end_dt:
            continue
        coords = _extract_coords_from_record(record)
        if coords:
            if len(coords) >= 2:
                entries.append((start_dt, end_dt, coords[0], coords[1]))
            else:
                entries.append((start_dt, end_dt, coords[0], None))
        else:
            no_coord_count += 1

    # Batch geocode all start points
    start_coords = [e[2] for e in entries]
    start_results = rg.search(start_coords)

    # Batch geocode end points (where they exist)
    end_indices = [i for i, e in enumerate(entries) if e[3] is not None]
    end_coords = [entries[i][3] for i in end_indices]
    end_results = rg.search(end_coords) if end_coords else []
    end_cc_map = {end_indices[j]: end_results[j]['cc'] for j in range(len(end_results))}

    # Build tagged records, discard cross-country travel segments
    tagged = []
    for i, entry in enumerate(entries):
        start_cc = start_results[i]['cc']
        end_cc = end_cc_map.get(i)
        if end_cc and end_cc != start_cc:
            continue
        tagged.append({
            'start': entry[0], 'end': entry[1],
            'cc': start_cc, 'country': get_country_name(start_cc),
        })

    tagged.sort(key=lambda x: x['start'])
    print(f"  {len(tagged)} geocoded, {no_coord_count} skipped (no coords)")
    return tagged


def build_spans(tagged):
    """Merge geocoded records into country spans.
    Same country → merge. Different country → new span."""
    if not tagged:
        return []
    spans = []
    current = dict(tagged[0])
    for entry in tagged[1:]:
        if entry['cc'] == current['cc']:
            current['end'] = max(current['end'], entry['end'])
        else:
            spans.append(current)
            current = dict(entry)
    spans.append(current)
    return spans


def filter_airport_transits(spans, data, airports):
    """Remove spans where ALL GPS points are near airports and duration < threshold.
    Returns (filtered_spans, removed_transits)."""
    if not airports:
        return spans, []

    removed, filtered = [], []
    for span in spans:
        if span['cc'] == '--':
            filtered.append(span)
            continue
        duration_hours = (span['end'] - span['start']).total_seconds() / 3600
        if duration_hours >= AIRPORT_TRANSIT_MAX_HOURS:
            filtered.append(span)
            continue

        coords = get_coords_in_timerange(data, span['start'], span['end'])
        if not coords:
            filtered.append(span)
            continue

        geo_results = rg.search(coords)
        country_coords = [coords[i] for i in range(len(coords)) if geo_results[i]['cc'] == span['cc']]
        if not country_coords:
            filtered.append(span)
            continue

        if all(is_near_airport(lat, lon, airports) for lat, lon in country_coords):
            removed.append(span)
        else:
            filtered.append(span)

    if removed:
        print(f"  Removed {len(removed)} airport-only transit(s)")
    return filtered, removed


def merge_consecutive(spans):
    """Merge consecutive same-country spans (absorb transit into real stays)."""
    if not spans:
        return spans
    merged = [spans[0].copy()]
    for s in spans[1:]:
        prev = merged[-1]
        if s['cc'] == prev['cc'] and s['cc'] != '--':
            prev['end'] = max(prev['end'], s['end'])
            if not s.get('_transit') or not prev.get('_transit'):
                prev.pop('_transit', None)
        else:
            merged.append(s.copy())
    return merged


def clamp_overlaps(spans):
    """Clamp spans so they don't overlap, drop tiny ones (< 5 min)."""
    for i in range(1, len(spans)):
        if spans[i]['start'] < spans[i - 1]['end']:
            spans[i]['start'] = spans[i - 1]['end']
    return [s for s in spans if (s['end'] - s['start']) > timedelta(minutes=5)]


def insert_no_data_gaps(spans, min_gap=NO_DATA_GAP_THRESHOLD):
    """Insert NO DATA spans wherever there's a gap > min_gap."""
    if not spans:
        return spans
    result = [spans[0]]
    for s in spans[1:]:
        prev = result[-1]
        gap = s['start'] - prev['end']
        if gap > min_gap:
            result.append({'start': prev['end'], 'end': s['start'], 'cc': '--', 'country': 'NO DATA'})
        result.append(s)
    return result


def apply_stay_overrides(spans, stay_overrides):
    """Apply stay overrides — cut into any span to insert known stays."""
    if not stay_overrides:
        return spans

    parsed = []
    for ov in stay_overrides:
        ov_start = datetime.fromisoformat(ov['from'] + 'T00:00:00+00:00')
        ov_end = datetime.fromisoformat(ov['to'] + 'T23:59:59+00:00')
        cc = ov['country_code']
        parsed.append({'start': ov_start, 'end': ov_end, 'cc': cc, 'country': get_country_name(cc)})
    parsed.sort(key=lambda x: x['start'])

    applied = 0
    for ov in parsed:
        new_spans = []
        for span in spans:
            if span['end'] <= ov['start'] or span['start'] >= ov['end']:
                new_spans.append(span)
            elif span['cc'] == ov['cc']:
                new_spans.append(span)
            else:
                if span['start'] < ov['start']:
                    new_spans.append({**span, 'end': ov['start']})
                new_spans.append({
                    'start': max(span['start'], ov['start']),
                    'end': min(span['end'], ov['end']),
                    'cc': ov['cc'], 'country': ov['country'],
                })
                if span['end'] > ov['end']:
                    new_spans.append({**span, 'start': ov['end']})
                applied += 1
        spans = new_spans

    if applied:
        print(f"  {applied} stay override(s) applied")
    return merge_consecutive(spans)


def apply_travel_overrides(spans, travels, raw_data=None, airports=None):
    """Apply travel overrides (pre-parsed UTC datetimes).
    These take priority over GPS data — they cut/replace spans in the affected time range."""
    if not travels:
        return spans

    for tv in travels:
        dep, arr = tv['depart'], tv['arrive']
        dest_cc, origin_cc = tv['to_cc'], tv['from_cc']

        new_spans = []
        for span in spans:
            if span['end'] <= dep:
                new_spans.append(span)
            elif span['start'] >= arr:
                new_spans.append(span)
            elif span['start'] < dep and span['end'] > arr:
                new_spans.append({**span, 'end': dep})
                new_spans.append({'start': arr, 'end': span['end'],
                                  'cc': dest_cc, 'country': get_country_name(dest_cc)})
            elif span['start'] < dep:
                new_spans.append({**span, 'end': dep})
            elif span['end'] > arr:
                new_spans.append({'start': arr, 'end': span['end'],
                                  'cc': dest_cc, 'country': get_country_name(dest_cc)})

        # Connect arrival to the next event:
        #  - If the next span is the SAME country as destination → bridge to it
        #    (covers the case: fly RU→GR, GPS starts 6 days later in GR)
        #  - If next travel leg departs from this country → use that departure time
        #  - If the next span is a DIFFERENT country → don't fill; let NO DATA
        #    handle the gap (covers: fly ES→FR, next GPS is DE 13 days later)
        #  - If nothing follows at all → don't fill
        next_start = None
        for s in new_spans:
            if s['start'] >= arr:
                if s['cc'] == dest_cc:
                    # Next span IS the destination country — bridge to it
                    next_start = s['start']
                # else: next span is a different country — don't bridge
                break

        tv_idx = travels.index(tv)
        if tv_idx < len(travels) - 1:
            next_tv = travels[tv_idx + 1]
            if next_tv['from_cc'] == dest_cc:
                next_dep = next_tv['depart']
                if next_start is None or next_dep < next_start:
                    next_start = next_dep

        if next_start and next_start > arr:
            insert_idx = len(new_spans)
            for i, s in enumerate(new_spans):
                if s['start'] >= arr:
                    insert_idx = i
                    break
            new_spans.insert(insert_idx, {
                'start': arr, 'end': next_start,
                'cc': dest_cc, 'country': get_country_name(dest_cc),
            })
        elif next_start is None:
            # No same-country span or outbound travel found — insert a minimal
            # arrival-only span so the timeline records the arrival event
            # without fabricating a long stay.
            insert_idx = len(new_spans)
            for i, s in enumerate(new_spans):
                if s['start'] >= arr:
                    insert_idx = i
                    break
            new_spans.insert(insert_idx, {
                'start': arr, 'end': arr,
                'cc': dest_cc, 'country': get_country_name(dest_cc),
            })

        # Extend origin country to departure time
        last_origin_idx = None
        for i in range(len(new_spans) - 1, -1, -1):
            if new_spans[i]['end'] <= dep and new_spans[i]['cc'] == origin_cc:
                last_origin_idx = i
                break

        if last_origin_idx is not None and new_spans[last_origin_idx]['end'] < dep:
            remove_indices = []
            for i in range(last_origin_idx + 1, len(new_spans)):
                if new_spans[i]['start'] >= dep:
                    break
                if new_spans[i]['end'] <= dep:
                    remove_indices.append(i)
                else:
                    new_spans[i]['start'] = dep
            for i in sorted(remove_indices, reverse=True):
                new_spans.pop(i)
            new_spans[last_origin_idx]['end'] = dep
        elif last_origin_idx is None:
            for i in range(len(new_spans) - 1, -1, -1):
                if new_spans[i]['end'] <= dep:
                    if new_spans[i]['cc'] == '--' and i > 0 and new_spans[i - 1]['cc'] == origin_cc:
                        new_spans[i - 1]['end'] = dep
                        new_spans.pop(i)
                    break

        spans = new_spans

    spans = [s for s in spans if s['end'] > s['start']]
    spans.sort(key=lambda x: x['start'])

    # Post-process: mark transit spans (flight in + flight out)
    # Decision logic:
    #   1. If GPS data exists for this country → transit only if ALL coords are near airports
    #   2. If no GPS data for this country → transit only if duration < threshold
    for span in spans:
        if span.get('_transit'):
            continue
        arrives_here = any(
            tv['to_cc'] == span['cc'] and abs((tv['arrive'] - span['start']).total_seconds()) < 60
            for tv in travels
        )
        departs_here = any(
            tv['from_cc'] == span['cc'] and span['start'] <= tv['depart'] <= span['end']
            for tv in travels
        )
        if arrives_here and departs_here:
            is_transit = None  # undecided
            if raw_data and airports:
                coords = get_coords_in_timerange(raw_data, span['start'], span['end'])
                if coords:
                    geo_results = rg.search(coords)
                    country_coords = [coords[i] for i in range(len(coords))
                                      if geo_results[i]['cc'] == span['cc']]
                    if country_coords:
                        # GPS evidence in this country — let it decide
                        is_transit = all(is_near_airport(lat, lon, airports)
                                         for lat, lon in country_coords)
            if is_transit is None:
                # No local GPS data — fall back to duration threshold
                duration_hours = (span['end'] - span['start']).total_seconds() / 3600
                is_transit = duration_hours < AIRPORT_TRANSIT_MAX_HOURS
            if is_transit:
                span['_transit'] = True

    print(f"  {len(travels)} travel override(s) applied")
    return merge_consecutive(spans)


# ─── Full pipeline ─────────────────────────────────────────────────────────────

def build_timeline(data):
    """Full pipeline: raw records → (spans, airport_transits, parsed_travels).

    Returns the final spans list, removed airport transits (for trip table),
    and parsed travel overrides in UTC.
    """
    print("=" * 60)
    print("COUNTRY TIMELINE")
    print("=" * 60)
    print(f"\n[1/6] Loaded {len(data)} records")

    airports = load_airports()
    stay_overrides, travel_overrides = load_overrides()
    travels = parse_travels(travel_overrides)

    print("[2/6] Geocoding...")
    tagged = geocode_records(data)

    print("[3/6] Building spans...")
    spans = build_spans(tagged)
    print(f"  {len(spans)} initial spans")

    print("[4/6] Filtering airport transits...")
    spans, airport_transits = filter_airport_transits(spans, data, airports)
    spans = merge_consecutive(spans)

    print("[5/6] Applying overrides...")
    spans = apply_travel_overrides(spans, travels, raw_data=data, airports=airports)
    spans = clamp_overlaps(spans)

    spans = insert_no_data_gaps(spans)
    spans = apply_stay_overrides(spans, stay_overrides)
    spans = merge_consecutive(spans)

    print(f"  {len(spans)} final spans")
    return spans, airport_transits, travels
