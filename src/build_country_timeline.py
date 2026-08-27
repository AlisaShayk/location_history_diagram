#!/usr/bin/env python3
"""
Country Timeline — Travel history from Google Location History.

Takes location-history.json (Google Takeout) and generates:
  - country_timeline.json / .csv (machine-readable)
  - travel_history.html (interactive visualization + trip table)

Usage:
  pip install -r requirements.txt
  python src/build_country_timeline.py
"""

import json
import os
import sys

from config import INPUT_FILE, OUTPUT_DIR
from pipeline import build_timeline
from html_report import write_html


def write_json_csv(spans):
    """Serialize spans to JSON and CSV."""
    output = []
    for s in spans:
        duration = (s['end'] - s['start']).total_seconds() / 86400
        entry = {
            'start': s['start'].isoformat(),
            'end': s['end'].isoformat(),
            'country_code': s['cc'],
            'country': s['country'],
            'duration_days': round(duration, 2),
        }
        if s.get('_transit'):
            entry['_transit'] = True
        output.append(entry)

    json_path = os.path.join(OUTPUT_DIR, 'country_timeline.json')
    with open(json_path, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    csv_path = os.path.join(OUTPUT_DIR, 'country_timeline.csv')
    with open(csv_path, 'w') as f:
        f.write("start,end,country_code,country,duration_days\n")
        for row in output:
            country_escaped = row['country'].replace('"', '""')
            f.write(f"{row['start']},{row['end']},{row['country_code']},\"{country_escaped}\",{row['duration_days']}\n")

    print(f"  ✓ {json_path}")
    print(f"  ✓ {csv_path}")
    return output


def main():
    print(f"\nInput: {INPUT_FILE}")
    print(f"Output dir: {OUTPUT_DIR}\n")

    if not os.path.exists(INPUT_FILE):
        print(f"\n✗ Input file not found: {INPUT_FILE}")
        print("  Place your Google Takeout location-history.json at:")
        print(f"  {INPUT_FILE}")
        print("\n  See README.md for instructions.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(INPUT_FILE, 'r') as f:
        data = json.load(f)

    spans, airport_transits, travels = build_timeline(data)

    print("\n[6/6] Writing outputs...")
    output = write_json_csv(spans)
    totals = write_html(output, airport_transits=airport_transits, travels=travels)

    total_all = sum(totals.values())
    no_data_days = sum(s['duration_days'] for s in output if s['country'] == 'NO DATA')
    tracked_days = total_all
    total_period = total_all + no_data_days
    print(f'\n{"─" * 50}')
    print(f'  Total period:      {total_period:.0f} days ({total_period / 365.25:.1f} years)')
    print(f'  Tracked:           {tracked_days:.0f} days ({tracked_days / total_period * 100:.1f}%)')
    print(f'  NO DATA:           {no_data_days:.0f} days ({no_data_days / total_period * 100:.1f}%)')
    print(f'  Countries visited: {len(totals)}')
    print(f'  Timeline spans:    {len(output)}')


if __name__ == '__main__':
    main()
