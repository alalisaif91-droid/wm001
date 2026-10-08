# -*- coding: utf-8 -*-
import json, sys, datetime, urllib.request, urllib.parse, os, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
POINTS = [('RAK city (MRF, Sectors 3 and 4)', 25.7549, 55.9705), ('Al Rams (1B)', 25.8713, 56.0218), ('Al Burairat, Wadi Al Beeh front (2B)', 25.7708, 56.0397), ('Jebel Jais road, Wadi Shehah (1C)', 25.9357, 56.1176), ('Khatt, Wadi Naqab front (5A)', 25.6433, 56.0105), ('Al Jazeera Al Hamra (6A)', 25.6952, 55.7945), ('Showkah and Kadrah (8A)', 25.0925, 56.1193), ('Al Munai (9A)', 24.9231, 56.1496)]
THRESH6 = {'watch': 10.0, 'high': 25.0}

def fetch(lat, lon):
    q = urllib.parse.urlencode({'latitude': lat, 'longitude': lon, 'hourly': 'precipitation,precipitation_probability,wind_gusts_10m', 'forecast_days': 3, 'timezone': 'Asia/Dubai', 'models': 'best_match'})
    with urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?' + q, timeout=30) as r:
        return json.loads(r.read().decode())

def summarise(d):
    h = d['hourly']; p = h['precipitation']; pr = h.get('precipitation_probability') or [None] * len(p); g = h.get('wind_gusts_10m') or [None] * len(p); t = h['time']
    now = (datetime.datetime.utcnow() + datetime.timedelta(hours=4)).strftime('%Y-%m-%dT%H:00')
    i0 = next((i for i, x in enumerate(t) if x >= now), 0); fut = p[i0:]
    def s(n): return round(sum(fut[:n]), 1)
    max6 = max((sum(fut[i:i + 6]) for i in range(0, max(1, len(fut) - 5))), default=0)
    peak_i = max(range(len(fut)), key=lambda i: fut[i]) if fut else 0
    return {'next6': s(6), 'next24': s(24), 'next72': s(72), 'max6': round(max6, 1), 'peakAt': t[i0 + peak_i] if fut else None, 'peakMm': round(fut[peak_i], 1) if fut else 0, 'probMax24': max([x for x in pr[i0:i0 + 24] if x is not None] or [0]), 'gustMax24': round(max([x for x in g[i0:i0 + 24] if x is not None] or [0])), 'hourly': [[t[i0 + k], round(fut[k], 1)] for k in range(min(72, len(fut)))]}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('out', nargs='?', default=os.path.join(HERE, 'weather.json'))
    ap.add_argument('--ncm-level', default=None); ap.add_argument('--ncm-text', default=None); ap.add_argument('--ncm-source', default='https://www.ncm.gov.ae')
    a = ap.parse_args()
    prev = {}
    try: prev = json.load(open(a.out, encoding='utf-8'))
    except Exception: pass
    pts = []
    for name, lat, lon in POINTS:
        try: pts.append(dict(name=name, lat=lat, lon=lon, **summarise(fetch(lat, lon))))
        except Exception as e: pts.append({'name': name, 'lat': lat, 'lon': lon, 'error': str(e)[:120]})
    worst = max((x.get('max6', 0) for x in pts), default=0)
    model_level = 'high' if worst >= THRESH6['high'] else ('watch' if worst >= THRESH6['watch'] else 'none')
    uae = (datetime.datetime.utcnow() + datetime.timedelta(hours=4)).strftime('%Y-%m-%d %H:%M')
    ncm = prev.get('ncm', {'level': 'unknown', 'text': 'No NCM reading recorded yet', 'source': a.ncm_source, 'asof': None})
    if a.ncm_level: ncm = {'level': a.ncm_level, 'text': a.ncm_text or '', 'source': a.ncm_source, 'asof': uae}
    out = {'asof': uae, 'tz': 'Asia/Dubai', 'source': 'Open-Meteo best match model (ECMWF and others), model forecast, not an official warning', 'thresholds': THRESH6, 'modelLevel': model_level, 'worstMax6': round(worst, 1), 'points': pts, 'ncm': ncm}
    json.dump(out, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('weather.json', uae, 'model level', model_level, 'worst 6 h', round(worst, 1), 'mm')

if __name__ == '__main__': main()
