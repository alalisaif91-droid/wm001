# -*- coding: utf-8 -*-
"""WM004 rain feed, written by GitHub Actions every 30 minutes (25 Oct 2026). Why: the laptop that used to write it goes into Modern Standby and no scheduled task runs while it sleeps (gaps of 7 h at night,
3 h by day), and the cloud routine sits behind a proxy that refuses Open-Meteo. GitHub's runners are always on and reach the internet freely.
Reads the Open-Meteo model forecast (best match, ECMWF based) at the eight rain points of the plan, side by side with retries, and writes wm004/weather.json in the same
shape the console and the website card read. A model forecast, not a warning. Python 3 standard library only.
Rules: fewer than 6 of the 8 points read = write nothing and exit 1 (the workflow then fails visibly and GitHub e-mails the owner); the last good file stays.
A feed written in the last 20 minutes is fresh enough and the run does nothing, unless FEED_FORCE=1.
The NCM reading is not read here: it comes from wm004/ncm_state.json (the NCM live reader, every 15 minutes) when that is under 30 minutes old, else from wm004/ncm.json, written from the department laptop when a reading
is logged. The block says which (via). Facts only: no sentence of NCM reaches this file.
Usage: python3 wm004/weather_feed_action.py wm004/weather.json

28 Oct 2026 (second audit of the weather channel, CLOUD-FEED): this is the file the hosted console loads (wm001-site wm004/weather.json), and it still had the four faults that
weather_feed_legacy.py lost on the laptop (F05, F07, LEGACY-1). Fixed here, the same way:
  1  forecast_days is 4, not 3. Three days from midnight left 51 hours at 21:00, so a point carried 51 hourly values and next72 was the sum of 51 hours under the label Next 72 h.
     Four days hold at least 73 hours from any hour of the day. With fewer than 72 hours read, next72 is null (no reading), never a short sum; hoursInWindow says how many were read.
  2  an hour with no value is no reading, never 0 mm (p = [x or 0 ...] was the LEGACY-1 fault): a precipitation hour that is null inside the window makes that point unread. The points
     read and the unread ones are written (pointsRead, pointsTotal, unreadPoints), so the console says 6 of 8 points read beside the level.
  3  the ncm block is no longer carried forward as it was typed. ncm.json was written on the laptop with ageHours 0.0 and a fog colour that ended at 09:00; 14 hours later it read as fresh and in
     force. The age is worked out again at every run from the stamp of the reading; the colours of the reading stay as READ (levelAtReading, levelsAtReading) and nothing is claimed to be in
     force now (levelInForce and levelsInForce unknown, inForce empty) because the NCM log, which knows the windows of a warning, is on the laptop only. When the laptop says that the NCM
     warnings page shows a colour nobody has logged (pageShows.pending, carried in ncm.json by publish_feed.py), the level and the hazards it could be about are unknown, not none.
  4  ncm.stale: a reading older than 24 hours, with no stamp, or with a stamp ahead of the clock is stale and its level is unknown, never none. The feed itself is stamped (asof, the run),
     so the console ages it.

29 Oct 2026 (review before it replaces the file in wm001-site):
  *  the chance of rain (probMax24) and the gust (gustMax24) of the next 24 hours are null when an hour of them has no value or the list is missing; they were 0, a calm figure for no reading.
  *  a reply that does not hold the hour of the run is not used (the point is unread): the window used to start at the first hour of the reply when every hour was in the past.
  *  the ncm block is worked out the same way however often it passes through (a block of the previous run is read from levelAtReading, not from the unknown it was given), and stale is
     worked out at every run, never carried.
  *  the NCM text and the note of the page colour are written for a public file: no internal command or file name (the laptop wrote "logged with ncm_log.py add"), and the sentence about the
     page ends before "Last logged reading" begins (CARD-NCM-TEXT).
30 Oct 2026 (the live reader):
  *  reader_block: the ncm block is built from wm004/ncm_state.json when it is under 30 minutes old. In force is known there, so levelInForce and inForce are stated. NO READING and TEXT WARNING give unknown, never none.
  *  FEED-REFUSED-NOTE-INTERNAL-NAMES: refused.note, the words of a refused reading and by pass through public_text or by_role, so no internal name reaches the public feed.
  *  HOSTED-NCM-DROPS-REFUSAL: a typed reading the log refused travels in ncm.json, and refresh_ncm makes the block stale and the hazards it names unknown.
Status (30 Oct 2026): staged in rain emergency/_build/site_feed; deployment is in site_ncm/DEPLOY.md.
The workflow does not change. The tests are wm001-site/wm004/test_weather_feed_action.py (python3 -m unittest, no network)."""
import json, sys, os, re, datetime, urllib.request, urllib.parse, time
from concurrent.futures import ThreadPoolExecutor, wait

POINTS = [('RAK city (MRF, Sectors 3 and 4)', 25.7549, 55.9705), ('Al Rams (1B)', 25.8713, 56.0218), ('Al Burairat, Wadi Al Beeh front (2B)', 25.7708, 56.0397),
          ('Jebel Jais road, Wadi Shehah (1C)', 25.9357, 56.1176), ('Khatt, Wadi Naqab front (5A)', 25.6433, 56.0105), ('Al Jazeera Al Hamra (6A)', 25.6952, 55.7945),
          ('Showkah and Kadrah (8A)', 25.0925, 56.1193), ('Al Munai (9A)', 24.9231, 56.1496)]
THRESH6 = {'watch': 10.0, 'high': 25.0}        # mm in 6 h at any point (Proposed, S17; AL10 and AL11)
MIN_POINTS = 6
WINDOW_H = 72
FORECAST_DAYS = 4                              # 4 days from midnight hold at least 73 hours from any hour of the day
NCM_STALE_H = 24                               # the same limit as ncm_log.ncm_block on the laptop
READER_FRESH_MIN = 30                          # ncm_state.json older than this gives way to ncm.json
NCM_PAGE = 'https://www.ncm.gov.ae/maps-warnings?lang=en'
FRESH_MIN = 20                                 # a feed written this many minutes ago is fresh enough (the triggers do not pile up commits)
WRITER = 'GitHub Actions, every 30 minutes'
NCM_LEVELS = ('none', 'yellow', 'orange', 'red')
HAZARDS = ('rain', 'fog', 'dust')
HERE = os.path.dirname(os.path.abspath(__file__))


def uae_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=4)


def fetch(lat, lon):
    q = urllib.parse.urlencode({'latitude': lat, 'longitude': lon, 'hourly': 'precipitation,precipitation_probability,wind_gusts_10m',
                                'forecast_days': FORECAST_DAYS, 'timezone': 'Asia/Dubai', 'models': 'best_match'})
    last = None
    for k in range(3):
        try:
            with urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?' + q, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:                     # retry with a pause: a 429 or a reset is usually gone in seconds
            last = e
            time.sleep(2 + 3 * k)
    raise last


def _max_all(vals, n):
    """the largest of the first n values, or None (no reading) when one of them has no value: a missing hour could hold the largest one, so a partial maximum is never given"""
    v = list(vals[:n])
    if len(v) < n or any(x is None for x in v):
        return None
    return max(float(x) for x in v)


def summarise(d, now=None):
    h = d['hourly']; p = h['precipitation']; t = h['time']
    pr = h.get('precipitation_probability') or [None] * len(p); g = h.get('wind_gusts_10m') or [None] * len(p)
    now_h = (now or uae_now()).strftime('%Y-%m-%dT%H:00')
    i0 = next((i for i, x in enumerate(t) if x >= now_h), None)
    if i0 is None or t[i0] != now_h:
        # the hour of the run is not in the reply (a reply that ends before now, or starts after it): no window can be read from it
        raise ValueError('the reply does not hold the hour %s' % now_h)
    win = p[i0:i0 + WINDOW_H]
    if len(win) < 24:
        raise ValueError('fewer than 24 forecast hours')
    nulls = sum(1 for x in win if x is None)
    if nulls:
        # a precipitation hour that was not read is no reading, not 0 mm: the point is unread (LEGACY-1)
        raise ValueError('%d precipitation hours in the window have no value' % nulls)
    fut = [float(x) for x in win]
    n = len(fut)

    def s(k): return round(sum(fut[:k]), 1)
    max6 = max((sum(fut[i:i + 6]) for i in range(0, max(1, n - 5))), default=0)
    peak_i = max(range(n), key=lambda i: fut[i])
    pmax, gmax = _max_all(pr[i0:], 24), _max_all(g[i0:], 24)
    return {'next6': s(6), 'next24': s(24), 'next72': (s(WINDOW_H) if n >= WINDOW_H else None), 'hoursInWindow': n, 'max6': round(max6, 1), 'peakAt': t[i0 + peak_i], 'peakMm': round(fut[peak_i], 1),
            'probMax24': (None if pmax is None else round(pmax)), 'gustMax24': (None if gmax is None else round(gmax)),
            'hourly': [[t[i0 + k], round(fut[k], 1)] for k in range(min(WINDOW_H, n))]}


def one(pt, fetcher=None, now=None):
    name, lat, lon = pt
    try:
        return dict(name=name, lat=lat, lon=lon, **summarise((fetcher or fetch)(lat, lon), now))
    except Exception as e:
        return {'name': name, 'lat': lat, 'lon': lon, 'error': str(e)[:120]}


def _age_h(asof, now):
    """hours from the stamp of a reading to now, or None when the stamp does not read or is more than 10 minutes ahead of the clock (a stamp from the future is not a fresh reading)"""
    try:
        a = (now - datetime.datetime.strptime(str(asof).replace('T', ' ')[:16], '%Y-%m-%d %H:%M')).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return None
    if a < -10 / 60.0:
        return None
    return round(max(0.0, a), 1)


# internal commands and file names that the laptop puts in a sentence for the weather desk; the feed is a public file (CARD-NCM-TEXT)
_SUBCMD = r'(?:\s+(?:add|clear-refused|list|show|status))?'
_PUBLIC = ((re.compile(r'\s*\((?:python3?\s+)?ncm_log\.py' + _SUBCMD + r'\)'), ''),
           (re.compile(r'\s+(?:with|through|using)\s+(?:python3?\s+)?ncm_log\.py' + _SUBCMD), ' by the weather desk'),
           (re.compile(r'\bncm_auto_state\.json\b'), 'the record of the automatic NCM page read'),
           (re.compile(r'\bncm_refused\.jsonl\b'), 'the file of refused NCM readings'),
           (re.compile(r'(?:python3?\s+)?\b[\w-]+\.(?:py|cmd|bat|jsonl)\b' + _SUBCMD), 'the weather desk log'))


def public_text(x):
    """a sentence for a public file: no internal command or file name, and a stop before "Last logged reading" so that two sentences never run together"""
    t = ' '.join(str(x or '').split())
    for rx, rep in _PUBLIC:
        t = rx.sub(rep, t)
    t = re.sub(r'(?<=[^\s.!?:;])\s+(Last logged reading:)', r'. \1', t)
    t = ' '.join(t.split())
    return (t[0].upper() + t[1:]) if t else t


def by_role(by):
    """who made an entry, as a role. What a person typed is never published, and neither is the name of a script."""
    b = str(by or '').lower()
    if not b:
        return None
    if b.startswith('engine'):
        return 'engine'
    if 'live reader' in b:
        return 'live reader'
    if 'backfill' in b:
        return 'backfill'
    return 'manual'


def _utc_as_uae(s):
    """a time of the reader as a naive Ras Al Khaimah time, or None"""
    try:
        t = datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        return t
    return (t.astimezone(datetime.timezone.utc) + datetime.timedelta(hours=4)).replace(tzinfo=None)


def reader_block(st, now):
    """The ncm block from wm004/ncm_state.json, or None when that file is missing, in another shape or 30 minutes old or more. Facts only. A warning counts for a hazard unless it is known not to touch RAK, the same rule as the
    NCM log. TEXT WARNING and NO READING say unknown, never none: a failed read must not look calm."""
    if not isinstance(st, dict) or st.get('state') not in ('WARNING', 'TEXT WARNING', 'NO WARNING IN FORCE', 'NO READING'):
        return None
    read = _utc_as_uae(st.get('readAt'))
    if read is None:
        return None
    age_min = (now - read).total_seconds() / 60.0
    if age_min >= READER_FRESH_MIN or age_min < -10:
        return None
    state = st['state']
    items = [x for x in (st.get('items') or []) if isinstance(x, dict)]
    counts = [x for x in items if x.get('rak') != 'not touched' or x.get('named')]
    keys = list(HAZARDS)

    def top(rows, h):
        cols = [x.get('colour') for x in rows if h in (x.get('hazards') or [])]
        if 'not read' in cols:
            return 'unknown'
        return max(cols, key=NCM_LEVELS.index) if cols else 'none'
    if state == 'NO WARNING IN FORCE':
        levels = {h: 'none' for h in keys}
        now_levels = dict(levels)
    elif state == 'WARNING' and not int(st.get('itemsMore') or 0):
        started = [x for x in counts if (_utc_as_uae(x.get('from')) or now) <= now]
        levels = {h: top(counts, h) for h in keys}
        now_levels = {h: top(started, h) for h in keys}
    else:
        levels = {h: 'unknown' for h in keys}
        now_levels = dict(levels)
    rak = st.get('rak') if isinstance(st.get('rak'), dict) else {}
    what = '%s, highest %s' % (state, st.get('highest')) if state == 'WARNING' else state
    hz = [h for h in (st.get('hazards') or []) if isinstance(h, str)]
    text = 'NCM live reader: %s%s, RAK %s, read %s, list %s h old.' % (what, (', ' + ', '.join(hz)) if hz else '', rak.get('status') or 'not known', read.strftime('%d %b %H:%M'), st.get('listAgeH') if st.get('listAgeH') is not None else 'not read')
    if state == 'NO READING':
        text = 'NCM live reader: NO READING (%s), read %s. The state of the NCM warnings is not known.' % (st.get('reason'), read.strftime('%d %b %H:%M'))
    return {'level': levels['rain'], 'levels': levels, 'levelAtReading': levels['rain'], 'levelsAtReading': dict(levels), 'levelInForce': now_levels['rain'], 'levelsInForce': now_levels,
            'inForce': [x.get('id') for x in started] if state == 'WARNING' and levels['rain'] != 'unknown' else [], 'upcoming': [x.get('id') for x in counts if (_utc_as_uae(x.get('from')) or now) > now] if state == 'WARNING' else [],
            'text': text, 'source': NCM_PAGE, 'via': 'NCM live reader on GitHub Actions, wm004/ncm_state.json', 'asof': read.strftime('%Y-%m-%d %H:%M'), 'ageHours': round(max(0.0, age_min) / 60.0, 2), 'hazard': hz, 'id': None,
            'stale': state == 'NO READING', 'by': 'live reader', 'kind': 'warning', 'refreshed': now.strftime('%Y-%m-%d %H:%M'),
            'reader': {'state': state, 'reason': st.get('reason'), 'highest': st.get('highest'), 'count': st.get('count'), 'rak': rak.get('status'), 'named': rak.get('named'), 'city': rak.get('city'), 'listAgeH': st.get('listAgeH'),
                       'limitH': st.get('limitH'), 'tickerRead': st.get('tickerRead'), 'health': st.get('health'), 'status': 'Reported'},
            'note': 'the colours are those of the NCM live reader at %s; it reads the list and the ticker every 15 minutes' % read.strftime('%Y-%m-%d %H:%M')}


def refresh_ncm(n, now):
    """The ncm block as it stands at `now` (CLOUD-FEED 3 and 4). What the laptop wrote (ncm.json, or the block of the previous run) says what the reading SAID at its stamp; how old it is, whether it
    is stale and what is in force are worked out again here, at this run, from the stamp. This script cannot read the NCM log (it is on the laptop), so it never claims that a colour is in force now:
    levelInForce and levelsInForce are unknown and inForce is empty. A block that already passed through here is read from what it kept of the reading (levelAtReading, levelsAtReading)."""
    n = dict(n or {})
    if not n.get('level') and not n.get('asof') and 'levelAtReading' not in n:
        n = {'level': 'unknown', 'text': 'No NCM reading recorded yet', 'source': 'https://www.ncm.gov.ae', 'asof': None}
    n.pop('ageHoursAtWrite', None)
    read_level = n['levelAtReading'] if 'levelAtReading' in n else n.get('level')
    lv = n['levelsAtReading'] if isinstance(n.get('levelsAtReading'), dict) else (n.get('levels') if isinstance(n.get('levels'), dict) else {})
    lv = dict(lv)
    age = _age_h(n.get('asof'), now)
    n['ageHours'] = age
    if 'warningsAgeHours' in n:
        n['warningsAgeHours'] = age
    ps = n.get('pageShows') if isinstance(n.get('pageShows'), dict) else {}
    page_pending = bool(ps.get('pending'))
    pend = [h for h in (ps.get('pendingHazards') or HAZARDS)] if page_pending else []
    # a reading typed at the desk that the log refused: the laptop marks the block stale and the hazards it names unknown, so the hosted block does too (HOSTED-NCM-DROPS-REFUSAL)
    ref = n.get('refused') if isinstance(n.get('refused'), dict) else {}
    ref_open = [x for x in (ref.get('open') or []) if isinstance(x, dict)]
    refused = bool(ref_open or ref.get('unreadableLines'))
    if refused:
        names = [h for x in ref_open for h in (x.get('pendingHazards') or HAZARDS)] if ref_open and not ref.get('unreadableLines') else list(HAZARDS)
        pend = list(dict.fromkeys(pend + names))
    old = (age is None) or age > NCM_STALE_H
    n['stale'] = bool(old or page_pending or refused)     # the laptop marks a block stale while a page colour is not logged (weather_feed_legacy.apply_page_pending); the same here
    n['levelAtReading'] = read_level
    n['levelsAtReading'] = dict(lv)
    rain_unknown = old or ('rain' in pend) or read_level not in NCM_LEVELS
    n['level'] = 'unknown' if rain_unknown else read_level
    keys = list(lv) + [h for h in HAZARDS if h not in lv]          # in a fixed order, so that two runs on the same reading write the same block
    if old:
        n['levels'] = {h: 'unknown' for h in keys}
    else:
        n['levels'] = {h: ('unknown' if (h in pend or v not in NCM_LEVELS) else v) for h, v in lv.items()}
        for h in pend:
            n['levels'][h] = 'unknown'
    n['levelInForce'] = 'unknown'
    n['levelsInForce'] = {h: 'unknown' for h in keys}
    n['inForce'] = []
    n['upcoming'] = []
    if n.get('text') is not None:
        n['text'] = public_text(n['text'])
    if ps:
        ps = dict(ps)
        if ps.get('note') is not None:
            ps['note'] = public_text(ps['note'])
        n['pageShows'] = ps
    if 'refused' in n and isinstance(n['refused'], dict):         # FEED-REFUSED-NOTE-INTERNAL-NAMES: the note and the words of a refused reading go through the same filter as the text
        r2 = dict(n['refused'])
        if r2.get('note') is not None:
            r2['note'] = public_text(r2['note'])
        r2['open'] = [{k: (public_text(v) if k in ('text', 'reason') else v) for k, v in x.items()} for x in (r2.get('open') or []) if isinstance(x, dict)]
        n['refused'] = r2
    if n.get('by') is not None:
        n['by'] = by_role(n['by'])
    n.setdefault('via', 'laptop reading, wm004/ncm.json')
    n['refreshed'] = now.strftime('%Y-%m-%d %H:%M')
    n['note'] = ('the colours are those of the reading at %s, %s; this feed cannot read the NCM log, so nothing is claimed to be in force now%s%s'
                 % (n.get('asof') or 'an unknown time', ('%.1f h old' % age) if age is not None else 'age unknown', '; the NCM warnings page shows a colour nobody has logged' if page_pending else '',
                    '; a reading typed at the weather desk was refused by the log' if refused else ''))
    return n


def main(argv=None, fetcher=None, now=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out_path = argv[0] if argv else os.path.join(HERE, 'weather.json')
    prev = {}
    try:
        with open(out_path, encoding='utf-8') as f:
            prev = json.load(f)
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        pass
    now = now or uae_now()
    # several triggers can start a run (the schedule and the laptop every hour): a feed written in the last 20 minutes is fresh enough, so they do not pile up commits
    try:
        age = (now - datetime.datetime.strptime(prev.get('asof', ''), '%Y-%m-%d %H:%M')).total_seconds() / 60.0
        if 0 <= age < FRESH_MIN and os.environ.get('FEED_FORCE') != '1':
            print('weather.json is %.0f minutes old: fresh enough, nothing to do' % age)
            return 0
    except (ValueError, TypeError):
        pass
    folder = os.path.dirname(os.path.abspath(out_path))
    ncm = prev.get('ncm') if isinstance(prev.get('ncm'), dict) else {'level': 'unknown', 'text': 'No NCM reading recorded yet', 'source': 'https://www.ncm.gov.ae', 'asof': None}
    if ncm.get('reader'):                                    # a block of the live reader is good only while it is under 30 minutes old; it is never carried on as if it were a logged reading
        ncm = {'level': 'unknown', 'text': 'No NCM reading recorded yet', 'source': 'https://www.ncm.gov.ae', 'asof': None}
    try:
        with open(os.path.join(folder, 'ncm.json'), encoding='utf-8') as f:
            n2 = json.load(f)
        if isinstance(n2, dict) and n2.get('level') and (n2.get('asof') or '') >= (ncm.get('asof') or ''):
            ncm = n2
    except Exception:
        pass
    live_block = None
    try:
        with open(os.path.join(folder, 'ncm_state.json'), encoding='utf-8') as f:
            live_block = reader_block(json.load(f), now)
    except Exception:
        pass
    ex = ThreadPoolExecutor(max_workers=len(POINTS))
    futs = [ex.submit(one, pt, fetcher, now) for pt in POINTS]
    wait(futs, timeout=150)
    pts = [f.result() if f.done() else {'name': pt[0], 'lat': pt[1], 'lon': pt[2], 'error': 'no answer within 150 s'} for f, pt in zip(futs, POINTS)]
    ex.shutdown(wait=False, cancel_futures=True)
    got = [x for x in pts if 'error' not in x]
    if len(got) < MIN_POINTS:
        print('FEED NOT WRITTEN: only %d of %d points answered (%s)' % (len(got), len(POINTS), next((x['error'] for x in pts if 'error' in x), '')))
        return 1
    worst = max(x['max6'] for x in got)
    level = 'high' if worst >= THRESH6['high'] else ('watch' if worst >= THRESH6['watch'] else 'none')
    out = {'asof': now.strftime('%Y-%m-%d %H:%M'), 'tz': 'Asia/Dubai', 'source': 'Open-Meteo best match model (ECMWF and others), model forecast, not an official warning',
           'writer': WRITER, 'thresholds': THRESH6, 'modelLevel': level, 'worstMax6': round(worst, 1), 'pointsRead': len(got), 'pointsTotal': len(POINTS),
           'unreadPoints': [x['name'] for x in pts if 'error' in x], 'windowHours': WINDOW_H, 'points': pts, 'ncm': live_block or refresh_ncm(ncm, now)}
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)
    print('weather.json', out['asof'], 'model level', level, 'worst 6 h', round(worst, 1), 'mm', '(%d of %d points)' % (len(got), len(POINTS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
