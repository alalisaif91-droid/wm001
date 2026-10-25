# -*- coding: utf-8 -*-
"""WM004 rain feed, written by GitHub Actions every 30 minutes (25 Oct 2026). Why: the laptop that used to write it goes into Modern Standby and no scheduled task runs while it
sleeps (gaps of 7 h at night, 3 h by day), and the cloud routine sits behind a proxy that refuses Open-Meteo. GitHub's runners are always on and reach the internet freely.
Reads the Open-Meteo model forecast (best match, ECMWF based) at the eight rain points of the plan, side by side with retries, and writes wm004/weather.json in the same
shape the console and the website card read. A model forecast, not a warning.
Rules: fewer than 6 of the 8 points read = write nothing and exit 1 (the workflow then fails visibly and GitHub e-mails the owner); the last good file stays.
The NCM reading is not read here (the NCM page is a script map): it comes from wm004/ncm.json, written from the department laptop when a reading is logged, else from the previous weather.json.
Usage: python3 wm004/weather_feed_action.py wm004/weather.json"""
import json, sys, os, datetime, urllib.request, urllib.parse, time
from concurrent.futures import ThreadPoolExecutor, wait

POINTS = [('RAK city (MRF, Sectors 3 and 4)', 25.7549, 55.9705), ('Al Rams (1B)', 25.8713, 56.0218), ('Al Burairat, Wadi Al Beeh front (2B)', 25.7708, 56.0397),
          ('Jebel Jais road, Wadi Shehah (1C)', 25.9357, 56.1176), ('Khatt, Wadi Naqab front (5A)', 25.6433, 56.0105), ('Al Jazeera Al Hamra (6A)', 25.6952, 55.7945),
          ('Showkah and Kadrah (8A)', 25.0925, 56.1193), ('Al Munai (9A)', 24.9231, 56.1496)]
THRESH6 = {'watch': 10.0, 'high': 25.0}        # mm in 6 h at any point (Proposed, S17; AL10 and AL11)
MIN_POINTS = 6
HERE = os.path.dirname(os.path.abspath(__file__))


def uae_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=4)


def fetch(lat, lon):
    q = urllib.parse.urlencode({'latitude': lat, 'longitude': lon, 'hourly': 'precipitation,precipitation_probability,wind_gusts_10m',
                                'forecast_days': 3, 'timezone': 'Asia/Dubai', 'models': 'best_match'})
    last = None
    for k in range(3):
        try:
            with urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?' + q, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:                     # retry with a pause: a 429 or a reset is usually gone in seconds
            last = e
            time.sleep(2 + 3 * k)
    raise last


def summarise(d):
    h = d['hourly']; p = [x or 0 for x in h['precipitation']]; pr = h.get('precipitation_probability') or [None] * len(p); g = h.get('wind_gusts_10m') or [None] * len(p); t = h['time']
    now = uae_now().strftime('%Y-%m-%dT%H:00')
    i0 = next((i for i, x in enumerate(t) if x >= now), 0); fut = p[i0:]
    if len(fut) < 24:
        raise ValueError('fewer than 24 forecast hours')
    def s(n): return round(sum(fut[:n]), 1)
    max6 = max((sum(fut[i:i + 6]) for i in range(0, max(1, len(fut) - 5))), default=0)
    peak_i = max(range(len(fut)), key=lambda i: fut[i])
    return {'next6': s(6), 'next24': s(24), 'next72': s(72), 'max6': round(max6, 1), 'peakAt': t[i0 + peak_i], 'peakMm': round(fut[peak_i], 1),
            'probMax24': max([x for x in pr[i0:i0 + 24] if x is not None] or [0]), 'gustMax24': round(max([x for x in g[i0:i0 + 24] if x is not None] or [0])),
            'hourly': [[t[i0 + k], round(fut[k], 1)] for k in range(min(72, len(fut)))]}


def one(pt):
    name, lat, lon = pt
    try:
        return dict(name=name, lat=lat, lon=lon, **summarise(fetch(lat, lon)))
    except Exception as e:
        return {'name': name, 'lat': lat, 'lon': lon, 'error': str(e)[:120]}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'weather.json')
    prev = {}
    try:
        prev = json.load(open(out_path, encoding='utf-8'))
    except Exception:
        pass
    ncm = prev.get('ncm') or {'level': 'unknown', 'text': 'No NCM reading recorded yet', 'source': 'https://www.ncm.gov.ae', 'asof': None}
    try:
        n2 = json.load(open(os.path.join(HERE, 'ncm.json'), encoding='utf-8'))
        if n2.get('level') and (n2.get('asof') or '') >= (ncm.get('asof') or ''):
            ncm = n2
    except Exception:
        pass
    ex = ThreadPoolExecutor(max_workers=len(POINTS))
    futs = [ex.submit(one, pt) for pt in POINTS]
    wait(futs, timeout=150)
    pts = [f.result() if f.done() else {'name': pt[0], 'lat': pt[1], 'lon': pt[2], 'error': 'no answer within 150 s'} for f, pt in zip(futs, POINTS)]
    ex.shutdown(wait=False, cancel_futures=True)
    got = [x for x in pts if 'error' not in x]
    if len(got) < MIN_POINTS:
        print('FEED NOT WRITTEN: only %d of %d points answered (%s)' % (len(got), len(POINTS), next((x['error'] for x in pts if 'error' in x), '')))
        return 1
    worst = max(x['max6'] for x in got)
    level = 'high' if worst >= THRESH6['high'] else ('watch' if worst >= THRESH6['watch'] else 'none')
    out = {'asof': uae_now().strftime('%Y-%m-%d %H:%M'), 'tz': 'Asia/Dubai', 'source': 'Open-Meteo best match model (ECMWF and others), model forecast, not an official warning',
           'writer': 'GitHub Actions, every 30 minutes', 'thresholds': THRESH6, 'modelLevel': level, 'worstMax6': round(worst, 1), 'points': pts, 'ncm': ncm}
    tmp = out_path + '.tmp'
    json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    print('weather.json', out['asof'], 'model level', level, 'worst 6 h', round(worst, 1), 'mm', '(%d of %d points)' % (len(got), len(POINTS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
