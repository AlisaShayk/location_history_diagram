"""
HTML report generation: timeline spans → travel_history.html.

The heavy lifting (circular SVG viz + trip table + interactivity) lives in
template.html as a Jinja-style template with simple $placeholder$ markers.
This module prepares the data and renders it.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from calendar import isleap

from config import (
    OUTPUT_DIR, TEMPLATE_FILE,
    build_color_map, get_country_name,
)


# ─── Data preparation ─────────────────────────────────────────────────────────

def _get_color_map(spans):
    """Build color map for all country codes, sorted by total duration.
    Countries with the most time get the most visually distinct colors."""
    totals = defaultdict(float)
    for s in spans:
        totals[s['country_code']] += s.get('duration_days', 0)
    # Sort by descending duration so dominant countries get maximally separated hues
    codes = sorted((cc for cc in totals if cc != '--'), key=lambda cc: -totals[cc])
    return build_color_map(codes)


def _build_year_data(output):
    """Convert timeline spans into per-year day-of-year segments for the circular viz.
    Transit spans are excluded — they only appear in the trip table."""
    year_data = defaultdict(list)
    for s in output:
        if s.get('_transit'):
            continue
        start_dt = datetime.fromisoformat(s['start'])
        end_dt = datetime.fromisoformat(s['end'])
        cc, name = s['country_code'], s['country']
        current = start_dt
        while current < end_dt:
            year = current.year
            year_start = datetime(year, 1, 1, tzinfo=current.tzinfo)
            year_end_dt = datetime(year + 1, 1, 1, tzinfo=current.tzinfo)
            days_in_year = 366 if isleap(year) else 365
            chunk_end = min(end_dt, year_end_dt)
            start_day = max(0.0, (current - year_start).total_seconds() / 86400)
            end_day = min(float(days_in_year), (chunk_end - year_start).total_seconds() / 86400)
            from_str = current.strftime('%Y-%m-%d')
            to_date = (chunk_end - timedelta(seconds=1)).date()
            span_days = (to_date - current.date()).days + 1
            year_data[str(year)].append({
                'cc': cc, 'name': name,
                'start': round(start_day, 1), 'end': round(end_day, 1),
                'from': from_str, 'to': to_date.strftime('%Y-%m-%d'), 'days': span_days,
            })
            current = chunk_end
    return dict(year_data)


def _build_trip_list(output, airport_transits, travels):
    """Build trip list for the table from the final spans.
    Inserts airport transits and flight-gap markers between trips."""
    trips = []
    for s in output:
        start = datetime.fromisoformat(s['start']) if isinstance(s['start'], str) else s['start']
        end = datetime.fromisoformat(s['end']) if isinstance(s['end'], str) else s['end']
        trips.append({
            'start': start, 'end': end,
            'cc': s['country_code'],
            'country': s['country'],
            'duration_days': s.get('duration_days', (end - start).total_seconds() / 86400),
            '_transit': s.get('_transit', False),
        })

    # Insert airport transits that fit chronologically
    for t in airport_transits:
        dominated = any(
            existing['cc'] == t['cc']
            and t['start'].date() <= existing['end'].date() + timedelta(days=1)
            and t['end'].date() >= existing['start'].date() - timedelta(days=1)
            for existing in trips if existing['cc'] != '--'
        )
        if dominated:
            continue
        for i in range(len(trips) - 1):
            if (trips[i]['end'].date() <= t['start'].date()
                    and t['end'].date() <= trips[i + 1]['start'].date()):
                trips.insert(i + 1, {
                    'start': t['start'], 'end': t['end'],
                    'cc': t['cc'], 'country': t['country'],
                    'duration_days': (t['end'] - t['start']).total_seconds() / 86400,
                    '_transit': True,
                })
                break

    # Insert gap rows for unaccounted days between consecutive trips
    def gap_is_flight(gap_start, gap_end):
        return any(
            tv['depart'].date() <= gap_end and tv['arrive'].date() >= gap_start
            for tv in travels
        )

    # Tag existing NO DATA spans that overlap a travel override as flight gaps.
    # If the flight only covers part of the NO DATA span, split it so the
    # flight portion is marked and the remainder stays as plain NO DATA.
    split_trips = []
    for t in trips:
        if t['country'] == 'NO DATA' and not t.get('_travel_gap'):
            t_start_date = t['start'].date() if hasattr(t['start'], 'date') else t['start']
            t_end_date = t['end'].date() if hasattr(t['end'], 'date') else t['end']
            # Find the latest-arriving travel override that overlaps this gap
            overlapping = [
                tv for tv in travels
                if tv['depart'].date() <= t_end_date and tv['arrive'].date() >= t_start_date
            ]
            if overlapping:
                # Use the last overlapping flight's arrival as the split point
                last_flight = max(overlapping, key=lambda tv: tv['arrive'])
                flight_end_dt = last_flight['arrive']
                # If the flight ends before the NO DATA span ends, split
                if flight_end_dt < t['end']:
                    flight_part = dict(t)
                    flight_part['end'] = flight_end_dt
                    flight_part['duration_days'] = (flight_end_dt - t['start']).total_seconds() / 86400
                    flight_part['_travel_gap'] = True
                    flight_part['_is_flight'] = True
                    split_trips.append(flight_part)

                    remainder = dict(t)
                    remainder['start'] = flight_end_dt
                    remainder['duration_days'] = (t['end'] - flight_end_dt).total_seconds() / 86400
                    split_trips.append(remainder)
                else:
                    t['_travel_gap'] = True
                    t['_is_flight'] = True
                    split_trips.append(t)
            else:
                split_trips.append(t)
        else:
            split_trips.append(t)
    trips = split_trips

    result = []
    for i, t in enumerate(trips):
        result.append(t)
        if i < len(trips) - 1:
            next_t = trips[i + 1]
            if t['country'] == 'NO DATA' or next_t['country'] == 'NO DATA':
                continue
            current_exit = t['end'].date()
            next_entry = next_t['start'].date()
            if current_exit != next_entry:
                gap_days = (next_entry - current_exit).days
                result.append({
                    'start': datetime.combine(current_exit, datetime.min.time(), tzinfo=timezone.utc),
                    'end': datetime.combine(next_entry, datetime.min.time(), tzinfo=timezone.utc),
                    'cc': '--', 'country': 'NO DATA',
                    'duration_days': gap_days,
                    '_travel_gap': True,
                    '_is_flight': gap_is_flight(current_exit, next_entry),
                })

    # Merge consecutive transit/flight-gap spans into single "in flight" rows
    merged = []
    i = 0
    while i < len(result):
        t = result[i]
        is_inflight = t.get('_transit') or (t.get('_travel_gap') and t.get('_is_flight'))
        if is_inflight:
            flight_start, flight_end = t['start'], t['end']
            route_codes = []
            if t.get('_transit') and t.get('cc', '--') != '--':
                route_codes.append(t['cc'])
            j = i + 1
            while j < len(result):
                nxt = result[j]
                if nxt.get('_transit') or (nxt.get('_travel_gap') and nxt.get('_is_flight')):
                    flight_end = max(flight_end, nxt['end'])
                    if nxt.get('_transit') and nxt.get('cc', '--') != '--':
                        if not route_codes or route_codes[-1] != nxt['cc']:
                            route_codes.append(nxt['cc'])
                    j += 1
                else:
                    break
            duration = (flight_end - flight_start).total_seconds() / 86400
            route_label = ' → '.join(get_country_name(cc) for cc in route_codes) if route_codes else ''
            if flight_start.date() != flight_end.date():
                merged.append({
                    'start': flight_start, 'end': flight_end,
                    'cc': '--', 'country': 'NO DATA',
                    'duration_days': duration,
                    '_travel_gap': True, '_is_flight': True, '_route': route_label,
                })
            # Check gap between flight end and next real entry
            if j < len(result):
                next_t = result[j]
                if next_t['country'] != 'NO DATA':
                    check_end = flight_end if flight_start.date() != flight_end.date() else flight_start
                    flight_exit = check_end.date()
                    next_entry = next_t['start'].date()
                    gap_days = (next_entry - flight_exit).days
                    if flight_exit != next_entry and gap_days > 0:
                        merged.append({
                            'start': datetime.combine(flight_exit, datetime.min.time(), tzinfo=timezone.utc),
                            'end': datetime.combine(next_entry, datetime.min.time(), tzinfo=timezone.utc),
                            'cc': '--', 'country': 'NO DATA',
                            'duration_days': gap_days,
                            '_travel_gap': True, '_is_flight': False,
                        })
            i = j
        else:
            merged.append(t)
            i += 1

    return merged


def _build_trips_json(trips, trips_by_year):
    """Convert trips grouped by year into the JSON structure the template expects."""
    data = {}
    for year, year_trips in sorted(trips_by_year.items()):
        data[year] = []
        for t in year_trips:
            t_start = t['start'].date() if hasattr(t['start'], 'date') else datetime.fromisoformat(str(t['start'])).date()
            t_end = t['end'].date() if hasattr(t['end'], 'date') else datetime.fromisoformat(str(t['end'])).date()
            cal_days = (t_end - t_start).days + 1
            dur = f'{cal_days}d' if cal_days >= 1 else f'{t["duration_days"] * 24:.0f}h'
            entry = {'cc': t['cc'], 'country': t['country'], 'entry': t_start.isoformat(), 'exit': t_end.isoformat(), 'dur': dur}
            if t.get('_transit'):
                entry['transit'] = True
            elif t.get('_is_flight'):
                entry['country'] = 'In flight'
                entry['flight'] = True
            elif t['country'] == 'NO DATA' or t.get('_travel_gap'):
                entry['country'] = '⚠️ NO DATA'
                entry['gap'] = True
            data[year].append(entry)
    return data


def _build_summary_rows(output, color_map):
    """Build HTML rows for the country summary table."""
    totals = defaultdict(float)
    for s in output:
        if s['country'] != 'NO DATA':
            totals[s['country']] += s['duration_days']

    rows = ''
    for country, days in sorted(totals.items(), key=lambda x: -x[1]):
        cc = next((s['country_code'] for s in output if s['country'] == country), '--')
        color = color_map.get(cc, '#555')
        badge = f'<span class="badge" style="background:{color}">{country}</span>'
        if days >= 365:
            dur = f'{days / 365.25:.1f} years'
        elif days >= 30:
            dur = f'{days:.0f} days ({days / 30.44:.1f} months)'
        else:
            dur = f'{days:.0f} days'
        rows += f'<tr><td>{badge}</td><td class="num">{dur}</td></tr>\n'
    return rows, totals


# ─── Main entry point ─────────────────────────────────────────────────────────

def write_html(output, airport_transits=None, travels=None):
    """Render the HTML report using template.html and write it to disk."""
    airport_transits = airport_transits or []
    travels = travels or []

    first_start = output[0]['start'][:10]
    last_end = output[-1]['end'][:10]
    color_map = _get_color_map(output)

    trips = _build_trip_list(output, airport_transits, travels)

    # Group trips by year
    trips_by_year = defaultdict(list)
    for t in trips:
        start_year = str(t['start'].year)
        end_year = str(t['end'].year)
        trips_by_year[start_year].append(t)
        if end_year != start_year:
            trips_by_year[end_year].append(t)

    year_data = _build_year_data(output)
    trips_json_data = _build_trips_json(trips, trips_by_year)
    summary_rows, totals = _build_summary_rows(output, color_map)

    countries_count = len(set(
        t['cc'] for t in trips
        if t['country'] != 'NO DATA' and not t.get('_transit') and not t.get('_travel_gap')
    ))
    trip_count = len([
        t for t in trips
        if t['country'] != 'NO DATA' and not t.get('_transit') and not t.get('_travel_gap')
    ])

    # Read template and substitute placeholders
    with open(TEMPLATE_FILE, 'r') as f:
        template = f.read()

    html = template.replace('$YEAR_DATA$', json.dumps(year_data, ensure_ascii=False))
    html = html.replace('$COLOR_MAP$', json.dumps(color_map, ensure_ascii=False))
    html = html.replace('$TRIPS_BY_YEAR$', json.dumps(trips_json_data, ensure_ascii=False))
    html = html.replace('$FIRST_START$', first_start)
    html = html.replace('$LAST_END$', last_end)
    html = html.replace('$TRIP_COUNT$', str(trip_count))
    html = html.replace('$COUNTRIES_COUNT$', str(countries_count))
    html = html.replace('$SUMMARY_ROWS$', summary_rows)

    html_path = os.path.join(OUTPUT_DIR, 'travel_history.html')
    with open(html_path, 'w') as f:
        f.write(html)
    print(f"  ✓ {html_path}")
    return totals
