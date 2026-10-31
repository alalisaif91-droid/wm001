# -*- coding: utf-8 -*-
"""WM004 NCM reader, run by GitHub Actions every 30 minutes (Saif, 31 Oct 2026). Why: the only reader was on the laptop, hourly, and it stops when the laptop sleeps, so warnings were missed.

What it does: one ordinary page visit to the NCM warnings page, capturing only what that page loads. It never builds a token, never logs in, never hides what it is. A refusal, a block or a change of
shape is reported and the run stops; it is never worked around. The rules are SCHEMA.md section 8 and nothing else produces NO WARNING IN FORCE. NCM's own message is never read.

Output, one writer per file, in the folder given (wm004 on the runner):
  ncm_state.json     public, facts only, under 6 KB
  ncm_index.json     public, facts only: the reader's memory, because the runner cannot open its own sealed files
  ncm_live.enc       sealed: the full reading (frame 1), and when this run read nothing, the last good reading copied unopened (frame 2, not in force)
  ncm_history.enc    sealed: every warning and ticker item ever seen, append only, frames never rewritten
  ncm_radar.enc      sealed: one clipped radar still, only while a warning is in force or rain was measured in RAK, at most every 30 minutes, under 200 KB
The sealed formats are in CRYPTO.md. NCM's words and radar pictures go only into sealed files: decision of 30 Oct 2026, because NCM's copyright page prohibits unauthorized reproduction.
Times: now is the Date header of NCM's reply, because the laptop clock was 3 min 22 s fast on 30 Oct (SCHEMA.md section 1).

Usage: python3 ncm_reader_action.py <folder> [--no-issues] [--plain] [--radar-test] [--force-commit] [--pub <file>]
--pub reads the public key from another place, for a laptop check into a scratch folder.
Exit 0 whether or not NCM could be read: a failed read is a state, written as NO READING. Exit 1 only when the reader cannot write at all (no public key)."""
import base64, datetime as dt, gzip, hashlib, json, os, re, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ncm_crypto as C  # noqa: E402

WARNINGS_URL = 'https://mobile.ncm.gov.ae/maps-warnings?lang=en'
RADAR_URL = 'https://mobile.ncm.gov.ae/maps-radars/uae-radars-network?lang=en'
BOARD_URL = 'https://alalisaif91-droid.github.io/wm001/wm004/ncm/'
REPO = 'alalisaif91-droid/wm001'
OWNER = 'alalisaif91-droid'
LABEL = 'wm004-ncm'
LIMIT_H = 72                    # freshness limit of generated_at, decided 30 Oct 2026; the age is always shown and the gaps are logged so it can be set from data later
HEARTBEAT_MIN = 25              # commit when the last commit is this old: every 30 minute run, so a commit is never older than about 30 minutes
RADAR_EVERY_MIN = 28            # the radar is due at the second run after the last still, which keeps it to at most one every 30 minutes
RADAR_MAX = 200000              # bytes of the sealed file
STATE_MAX = 6000                # bytes of the public state
HISTORY_MAX = 1500000           # bytes of ncm_history.enc before it is moved aside and a compact index starts the next file
ALERTS_PER_DAY = 8
FAIL_ALERT_H = 2.0
GAPS_KEPT = 300
UAE = dt.timezone(dt.timedelta(hours=4))
EMPTY_HASH = hashlib.sha256(b'[]').hexdigest()
COLOURS = {1: ('yellow', 'Be Aware'), 2: ('orange', 'Be Prepared'), 3: ('red', 'Take Action')}
RANK = {'not read': 0, 'none': 0, 'yellow': 1, 'orange': 2, 'red': 3}
HAZARD_OF = {'FG': 'fog', 'DS': 'dust', 'WD': 'wind', 'RS': 'sea', 'TR': 'rain', 'CC': 'rain'}
RAK_WORDS = re.compile(r'ras\s*al[\s\-]*kh[a]?i?mah|رأس\s*الخيمة|راس\s*الخيمة', re.I)
TEXT_HAZARDS = (('rain', re.compile(r'\brain|thunder|lightning|convective|أمطار|مطر|رعد|برق|ركامية', re.I)), ('fog', re.compile(r'\bfog\b|\bmist\b|ضباب', re.I)),
                ('dust', re.compile(r'\bdust|\bsand\b|غبار|رمال', re.I)), ('wind', re.compile(r'\bwind|رياح', re.I)), ('sea', re.compile(r'rough sea|\bsea\b|بحر', re.I)))
REQUIRED = ('areas', 'list', 'tickers', 'announcements', 'aws', 'airports')
TILE_PAIRS = (((5, 20, 13), (5, 21, 13)), ((6, 41, 27), (6, 42, 27)))     # 56.25 E splits the emirate: both tiles of a pair are needed
CITY_UNITS = ('RK001',)         # Al Mamourah holds the city; station 420 is the other test
CITY_STATION = 420
STATE_WORDS = {'warning': 'WARNING', 'text_warning': 'TEXT WARNING', 'none': 'NO WARNING IN FORCE', 'no_reading': 'NO READING'}
RAIN_NOTE = 'A 0 is Reported, and a stuck gauge reads the same. A missing station is no reading, never dry.'


# ------------------------------------------------------------------------------------------------ small helpers
def iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if t else None


def parse_iso(s):
    """returns an aware datetime or None"""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UAE)


def parse_time(v):
    """a time of NCM: (aware datetime or None, no offset, unreadable). Read offsets as given; no offset is read as UAE time and flagged; unreadable is kept and flagged (SCHEMA.md section 8)."""
    if not isinstance(v, str) or not v.strip():
        return None, False, True
    s = v.strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', s):
        return None, False, True
    try:
        t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None, False, True
    if t.tzinfo:
        return t, False, False
    return t.replace(tzinfo=UAE), True, False


def http_date(s):
    """the Date header of a reply, as a UTC datetime, or None"""
    if not s:
        return None
    try:
        import email.utils
        t = email.utils.parsedate_to_datetime(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def scrub(x, n=160):
    """an error for a public log or file: no address at all, because a page token lives in the path and in the query of every NCM address"""
    t = re.sub(r'https?://\S+', '<address>', str(x))
    t = re.sub(r'[A-Za-z0-9_\-]{16,}\.\d{8,}', '<token>', t)
    t = re.sub(r'(AUTH_TOKEN|hmac|acl|exp|st)=\S+', r'\1=<token>', t)
    return ' '.join(t.split())[:n]


def parse_body(c):
    """(object, reason) of one captured reply"""
    if not c:
        return None, 'not captured'
    b = c.get('body')
    if b is None:
        return None, 'not captured'
    if c.get('status') not in (None, 200):
        return None, 'HTTP %s' % c.get('status')
    if isinstance(b, bytes):
        try:
            b = b.decode('utf-8')
        except UnicodeDecodeError:
            return None, 'not JSON'
    try:
        return json.loads(b), None
    except (TypeError, ValueError):
        return None, 'not JSON'


def short_key(*parts):
    return hashlib.sha256('\x1f'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:16]


def shape_of(x):
    """the shape of an item, not its values: the field names and the kind of each value"""
    if not isinstance(x, dict):
        return short_key(type(x).__name__)
    return short_key(*['%s:%s' % (k, type(x[k]).__name__) for k in sorted(x)])


# ------------------------------------------------------------------------------------------------ places
def load_places(path=None):
    with open(path or os.path.join(HERE, 'ncm_places.json'), encoding='utf-8') as f:
        return json.load(f)


# ------------------------------------------------------------------------------------------------ vector tiles, standard library only
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, i
        s += 7


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        elif w == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        else:
            raise ValueError('wire type %d' % w)
        yield f, v


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def _packed(b):
    i, out = 0, []
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _value(b):
    for f, v in _fields(b):
        if f == 1:
            return v.decode('utf-8', 'replace')
        if f in (4, 5):
            return v
        if f == 6:
            return _zigzag(v)
        if f == 7:
            return bool(v)
    return None


def decode_tile(data):
    """{layer name: {'extent': int, 'features': [{'props': {}, 'rings': [[(x, y), ...], ...]}]}}. Polygons only."""
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    layers = {}
    for f, v in _fields(data):
        if f != 3:
            continue
        name, extent, keys, values, feats = '', 4096, [], [], []
        for lf, lv in _fields(v):
            if lf == 1:
                name = lv.decode('utf-8', 'replace')
            elif lf == 2:
                feats.append(lv)
            elif lf == 3:
                keys.append(lv.decode('utf-8', 'replace'))
            elif lf == 4:
                values.append(_value(lv))
            elif lf == 5:
                extent = lv
        out = []
        for fb in feats:
            tags, geom, typ = [], [], 0
            for ff, fv in _fields(fb):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    typ = fv
                elif ff == 4:
                    geom = _packed(fv)
            props = {keys[tags[i]]: values[tags[i + 1]] for i in range(0, len(tags) - 1, 2) if tags[i] < len(keys) and tags[i + 1] < len(values)}
            rings, cur, x, y, i = [], [], 0, 0, 0
            while i < len(geom):
                cmd = geom[i]
                i += 1
                op, count = cmd & 7, cmd >> 3
                if op == 7:
                    if cur:
                        rings.append(cur)
                    cur = []
                    continue
                for _ in range(count):
                    x += _zigzag(geom[i])
                    y += _zigzag(geom[i + 1])
                    i += 2
                    if op == 1 and cur:
                        rings.append(cur)
                        cur = []
                    cur.append((x, y))
            if cur:
                rings.append(cur)
            if typ == 3 or typ == 0:
                out.append({'props': props, 'rings': rings})
        layers[name] = {'extent': extent, 'features': out}
    return layers


def _lonlat(px, py, z, x, y, extent):
    import math
    n = 2.0 ** z
    lon = (x + px / float(extent)) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / float(extent)) / n))))
    return lon, lat


def _in_rings(pt, rings):
    """even odd over every ring of a feature, which treats holes and several parts correctly"""
    px, py, inside = pt[0], pt[1], False
    for r in rings:
        j = len(r) - 1
        for i in range(len(r)):
            xi, yi, xj, yj = r[i][0], r[i][1], r[j][0], r[j][1]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
            j = i
    return inside


def _segments_cross(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(a, b, c) > 0) != (o(a, b, d) > 0) and (o(c, d, a) > 0) != (o(c, d, b) > 0)


def _rings_touch(a, b):
    """two polygons share ground: a vertex of one inside the other, or two edges crossing"""
    if any(_in_rings(p, b) for r in a for p in r) or any(_in_rings(p, a) for r in b for p in r):
        return True
    for ra in a:
        for i in range(len(ra)):
            p, q = ra[i], ra[(i + 1) % len(ra)]
            for rb in b:
                for j in range(len(rb)):
                    if _segments_cross(p, q, rb[j], rb[(j + 1) % len(rb)]):
                        return True
    return False


def polygons_of(tiles):
    """{warning id: [{'rings': lon lat rings, 'tile': (z, x, y), 'seam': bool}]}, read from every captured tile. seam: the polygon reaches the edge that joins the other tile of its pair."""
    out = {}
    for key, data in (tiles or {}).items():
        z, x, y = key
        try:
            layers = decode_tile(data)
        except (ValueError, IndexError, OSError):
            continue
        for name, lay in layers.items():
            m = re.match(r'warning_(.+)$', name)
            for f in lay['features']:
                wid = f['props'].get('warning_id')
                wid = str(wid) if wid is not None else (m.group(1) if m else None)
                if wid is None:
                    continue
                ext = lay['extent']
                seam = any(p[0] <= 8 or p[0] >= ext - 8 for r in f['rings'] for p in r)
                rings = [[_lonlat(p[0], p[1], z, x, y, ext) for p in r] for r in f['rings']]
                out.setdefault(wid, []).append({'rings': rings, 'tile': key, 'seam': seam})
    return out


def rak_of_polygons(polys, tiles, places):
    """(status, city, units) of one warning from its polygons. A polygon that runs to the edge of a tile whose neighbour was not read cannot say not touched."""
    if not polys:
        return 'not known', None, []
    hit_units, city, st_hit = set(), False, False
    for p in polys:
        for s in places['stations']:
            if s.get('rak') and _in_rings((s['lon'], s['lat']), p['rings']):
                st_hit = True
                if s['id'] == CITY_STATION:
                    city = True
                if s.get('unit'):
                    hit_units.add(s['unit'])
        for u in places['units']:
            if _rings_touch(p['rings'], [u['ring']]):
                hit_units.add(u['name'])
                if u['id'] in CITY_UNITS:
                    city = True
    if hit_units or st_hit:
        return 'touched', city, sorted(hit_units)
    for p in polys:
        for pair in TILE_PAIRS:
            if p['tile'] in pair and p['seam'] and any(t not in (tiles or {}) for t in pair):
                return 'not known', None, []
    return 'not touched', False, []


# ------------------------------------------------------------------------------------------------ the reading
def norm_id(v, raw):
    if isinstance(v, bool) or v is None:
        return 'x' + short_key(json.dumps(raw, sort_keys=True, ensure_ascii=False))
    s = str(v).strip()
    if re.match(r'^-?\d+(\.0+)?$', s):
        return str(int(float(s)))
    return s or ('x' + short_key(json.dumps(raw, sort_keys=True, ensure_ascii=False)))


def norm_codes(v):
    if isinstance(v, str):
        v = re.split(r'[,;\s]+', v)
    if not isinstance(v, (list, tuple)):
        return []
    return [str(c).strip().upper() for c in v if str(c).strip()]


def norm_severity(v):
    if isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n in COLOURS else None


def normalise_item(raw, now, polys, tiles, places):
    """one list item. Never dropped: a field that cannot be read stays raw and is flagged. Returns (item, expired)."""
    r = raw if isinstance(raw, dict) else {}
    wid = norm_id(r.get('WARNING_ID'), raw)
    sev = norm_severity(r.get('SEVERITY'))
    codes = norm_codes(r.get('WARNING_CODES'))
    unknown = [c for c in codes if c not in HAZARD_OF]
    hazards = sorted({HAZARD_OF.get(c, 'other') for c in codes} or {'other'})
    pub, pub_no, pub_bad = parse_time(r.get('PUBLISHED_ON'))
    frm, frm_no, frm_bad = parse_time(r.get('VALIDITY_FROM'))
    to, to_no, to_bad = parse_time(r.get('VALIDITY_TO'))
    en, ar = r.get('WARNING_EN'), r.get('WARNING_AR')
    words = ' '.join(x for x in (en, ar) if isinstance(x, str))
    named = bool(RAK_WORDS.search(words))
    status, city, units = rak_of_polygons((polys or {}).get(wid), tiles, places)
    item = {'id': wid, 'severityRaw': r.get('SEVERITY'), 'colour': COLOURS[sev][0] if sev else 'not read', 'colourName': COLOURS[sev][1] if sev else None,
            'codes': codes, 'unknownCodes': unknown, 'hazards': hazards,
            'published': iso(pub), 'from': iso(frm), 'to': iso(to),
            'flags': {'upcoming': bool(frm and frm > now), 'notYetPublished': bool(pub and pub > now), 'timesNoOffset': bool(pub_no or frm_no or to_no), 'timesUnreadable': bool(pub_bad or frm_bad or to_bad),
                      'colourNotRead': sev is None, 'placeNotKnown': status == 'not known'},
            'rak': {'status': status, 'named': named, 'city': city, 'units': units},
            'en': en if isinstance(en, str) else None, 'ar': ar if isinstance(ar, str) else None, 'raw': raw}
    return item, bool(to and to < now)


def read_tickers(c):
    """(items, reason). Items are [{type, time, en, ar, key}], with status 1 as NCM's ticker uses it."""
    d, why = parse_body(c)
    if d is None:
        return None, 'the ticker reply was %s' % why
    if not isinstance(d, dict) or d.get('status') not in (1, 200, '1', '200') or not isinstance(d.get('result'), list):
        return None, 'the ticker reply did not hold a list'
    out = []
    for x in d['result']:
        if not isinstance(x, dict):             # never dropped: an item that cannot be read is not news, so it cannot leave the ticker reading as calm (SCHEMA.md section 8)
            raw = json.dumps(x, ensure_ascii=False)
            out.append({'type': 'not read', 'time': None, 'en': x if isinstance(x, str) else None, 'ar': None, 'key': short_key('not read', raw), 'raw': x})
            continue
        t, en, ar, tm = x.get('type'), x.get('text_en'), x.get('text_ar'), x.get('time')
        out.append({'type': str(t) if t is not None else 'not read', 'time': tm if isinstance(tm, str) else None, 'en': en if isinstance(en, str) else None, 'ar': ar if isinstance(ar, str) else None,
                    'key': short_key(t, tm, en, ar), 'raw': x})
    return out, None


def read_announcements(c):
    d, why = parse_body(c)
    if d is None:
        return None, 'the announcements reply was %s' % why
    if not isinstance(d, dict) or not isinstance(d.get('result'), list):
        return None, 'the announcements reply did not hold a list'
    return d['result'], None


def station_row(obs, now):
    """(last report, latest row) from a station's rows, newest row with a usable time"""
    best, row = None, None
    for r in obs or []:
        if not isinstance(r, dict):
            continue
        t = parse_iso(r.get('time'))
        if t and t <= now + dt.timedelta(minutes=10) and (best is None or t > best):
            best, row = t, r
    return best, row


def rain_sum(obs, now, hours):
    """the amount in the rows inside the window. Rows are summed, never multiplied, because a row's span differs by station. (sum or None, rows with a value, rows with no value). None: no row had a value."""
    lo, total, got, bad = now - dt.timedelta(hours=hours), 0.0, 0, 0
    for r in obs or []:
        t = parse_iso(r.get('time')) if isinstance(r, dict) else None
        if not t or not lo < t <= now + dt.timedelta(minutes=10):
            continue
        v = r.get('precipitation')
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            bad += 1
            continue
        total += float(v)
        got += 1
    return (round(total, 1) if got else None), got, bad


def _obs_map(c):
    d, why = parse_body(c)
    if d is None:
        return None, why
    res = d.get('result') if isinstance(d, dict) else None
    if not isinstance(res, list):
        return None, 'no result list'
    return {s.get('id'): s for s in res if isinstance(s, dict)}, None


def read_stations(c, now, places):
    m, why = _obs_map(c)
    out = []
    for s in places['stations']:
        rec = {'id': s['id'], 'name': s['name'], 'nameAr': s.get('nameAr'), 'group': 'rak' if s.get('rak') else 'neighbour', 'unit': s.get('unit'), 'lat': s['lat'], 'lon': s['lon'], 'elev': s.get('elev'),
               'reading': False, 'lastReport': None, 'ageMin': None, 'rain1h': None, 'rain3h': None, 'rain24h': None, 'rainRows': 0, 'rainGaps': 0, 'windKmh': None, 'windDir': None, 'humidity': None, 'tempC': None}
        st = (m or {}).get(s['id'])
        if st:
            obs = st.get('observations')
            t, row = station_row(obs, now)
            if t:
                r1, _, _ = rain_sum(obs, now, 1)
                r3, _, _ = rain_sum(obs, now, 3)
                r24, g, b = rain_sum(obs, now, 24)
                rec.update({'reading': True, 'lastReport': iso(t), 'ageMin': max(0, int((now - t).total_seconds() // 60)), 'rain1h': r1, 'rain3h': r3, 'rain24h': r24, 'rainRows': g, 'rainGaps': b,
                            'windKmh': row.get('wind_speed'), 'windDir': row.get('wind_dir'), 'humidity': row.get('humidity'), 'tempC': row.get('dry_temperature')})
        out.append(rec)
    return out, why


def read_airport(c, now, places):
    a = places.get('airport') or {}
    rec = {'id': a.get('id'), 'name': a.get('name'), 'reading': False, 'lastReport': None, 'ageMin': None, 'windKmh': None, 'windDir': None, 'humidity': None, 'tempC': None}
    m, _ = _obs_map(c)
    st = (m or {}).get(a.get('id'))
    if st:
        t, row = station_row(st.get('observations'), now)
        if t:
            rec.update({'reading': True, 'lastReport': iso(t), 'ageMin': max(0, int((now - t).total_seconds() // 60)), 'windKmh': row.get('wind_speed'), 'windDir': row.get('wind_dir'),
                        'humidity': row.get('humidity'), 'tempC': row.get('dry_temperature')})
    return rec


def rain_totals(stations):
    """highest 1, 3 and 24 h in RAK with the station id. None when no RAK station gave a value."""
    rak = [s for s in stations if s['group'] == 'rak']
    def top(k):
        vals = [(s[k], s['id']) for s in rak if s[k] is not None]
        if not vals:
            return None
        v = max(vals, key=lambda x: x[0])
        return {'mm': v[0], 'station': v[1]}
    return {'h1': top('rain1h'), 'h3': top('rain3h'), 'h24': top('rain24h'), 'stationsRead': len([s for s in rak if s['reading']]), 'stationsExpected': len(rak),
            'missing': [s['id'] for s in rak if not s['reading']], 'note': RAIN_NOTE}


def reason_nr(code, text):
    return {'state': 'no_reading', 'reason': code, 'reasonText': text}


def judge(capture, now, limit_h):
    """SCHEMA.md section 8, the whole of it. Returns the state with its reason and what the list held. Nothing else in this file may produce NO WARNING IN FORCE."""
    out = {'items_raw': [], 'latest': None, 'version': None, 'contentHash': None, 'generatedAt': None, 'listAgeH': None}
    if capture.get('failed'):
        out.update(reason_nr('visit_failed', 'the visit could not be made: %s' % capture['failed']))
        return out
    if capture.get('refused'):
        out.update(reason_nr('refused', 'NCM refused the page visit: %s' % capture['refused']))
        return out
    areas, why = parse_body(capture.get('areas'))
    if areas is None:
        out.update(reason_nr('areas_' + ('json' if why == 'not JSON' else 'missing'), 'warnings_areas.json: %s' % ('the reply was not JSON' if why == 'not JSON' else why)))
        return out
    latest = areas.get('latest_warnings') if isinstance(areas, dict) else None
    if not latest:
        out.update(reason_nr('areas_no_latest', 'warnings_areas.json has no latest_warnings'))
        return out
    out['latest'] = str(latest)
    lst, why = parse_body(capture.get('list'))
    if lst is None:
        out.update(reason_nr('list_json' if why == 'not JSON' else 'list_missing', 'the warnings list reply was not JSON' if why == 'not JSON' else 'the warnings list was %s in this run' % why))
        return out
    if not isinstance(lst, dict):
        out.update(reason_nr('list_shape', 'the warnings list is not a JSON object, so the warnings array cannot be read'))
        return out
    out['version'], out['contentHash'] = lst.get('version'), lst.get('content_hash')
    w = lst.get('warnings')
    if not isinstance(w, list):
        out.update(reason_nr('list_no_warnings', 'the warnings list has no warnings array'))
        return out
    gen = parse_iso(lst.get('generated_at'))
    if gen is None:
        out.update(reason_nr('list_no_generated_at', 'the warnings list has no readable generated_at'))
        return out
    out['generatedAt'] = lst.get('generated_at')
    age = (now - gen).total_seconds() / 3600.0
    if age < -10 / 60.0:
        out.update(reason_nr('list_time_ahead', 'generated_at is ahead of the clock of NCM\'s reply, so it is not a readable stamp'))
        return out
    out['listAgeH'] = round(max(0.0, age), 2)
    out['items_raw'] = w
    if age > limit_h:
        out.update(reason_nr('list_old', 'the warnings list is %.1f h old, over the limit of %d h' % (age, limit_h)))
        return out
    if not w and lst.get('content_hash') != EMPTY_HASH:
        out.update(reason_nr('list_hash', 'the list is empty and its hash is not the hash of an empty list'))
    return out


def evaluate(capture, now, places, limit_h=LIMIT_H):
    """The full reading of one run, public and private fields together. public_state() takes only facts from it."""
    j = judge(capture, now, limit_h)
    tick, tick_why = read_tickers(capture.get('tickers'))
    ann, ann_why = read_announcements(capture.get('announcements'))
    tiles = capture.get('tiles') or {}
    polys = polygons_of(tiles)
    items, dropped = [], []
    for raw in j['items_raw']:
        it, exp = normalise_item(raw, now, polys, tiles, places)
        (dropped.append(it['id']) if exp else items.append(it))
    items.sort(key=lambda x: (-RANK[x['colour']], x['id']))
    non_news = [t for t in (tick or []) if t['type'] != 'news']
    if j.get('state') == 'no_reading':
        state, reason, text = 'no_reading', j['reason'], j['reasonText']
    elif items:
        state, reason, text = 'warning', 'list_items', '%d warning%s on the NCM list' % (len(items), '' if len(items) == 1 else 's')
        if tick is None:
            text += '; %s, so the ticker was not read in this run' % tick_why
    elif tick is None:
        state, reason, text = 'no_reading', 'ticker_failed', 'the list is empty but the ticker was not read: %s' % tick_why
    elif non_news:
        state, reason, text = 'text_warning', 'ticker_only', 'the NCM list is empty and the ticker holds %d item%s that are not news; the colour is not read' % (len(non_news), '' if len(non_news) == 1 else 's')
    else:
        state, reason, text = 'none', 'list_and_ticker_clear', 'the list is empty, recent, its hash holds, and the ticker holds only news; both were read in this run'
    colours = [i['colour'] for i in items if i['colour'] != 'not read']
    if state == 'warning':
        highest = max(colours, key=lambda c: RANK[c]) if colours else 'not read'
    elif state == 'none':
        highest = 'none'
    else:
        highest = 'not read'
    in_force = items if state == 'warning' else []
    hazards = sorted({h for i in in_force for h in i['hazards']})
    if state == 'text_warning':
        words = ' '.join((t.get('en') or '') + ' ' + (t.get('ar') or '') for t in non_news)
        hazards = sorted({h for h, rx in TEXT_HAZARDS if rx.search(words)}) or ['other']
    # RAK of the whole reading
    named = any(i['rak']['named'] for i in in_force) or bool(RAK_WORDS.search(' '.join((t.get('en') or '') + ' ' + (t.get('ar') or '') for t in non_news)))
    units = sorted({u for i in in_force for u in i['rak']['units']})
    if state == 'none':
        status, city = 'not touched', False
    elif in_force:
        sts = [i['rak']['status'] for i in in_force]
        status = 'touched' if 'touched' in sts else ('not known' if 'not known' in sts else 'not touched')
        city = True if any(i['rak']['city'] for i in in_force) else (False if status != 'not known' else None)
    else:
        status, city = 'not known', None
    stations, st_why = read_stations(capture.get('aws'), now, places)
    latest_tick = sorted(tick or [], key=lambda t: t.get('time') or '', reverse=True)[:10]
    r = {'state': state, 'stateWord': STATE_WORDS[state], 'reason': reason, 'reasonText': text, 'now': now, 'latest': j['latest'], 'version': j['version'], 'contentHash': j['contentHash'],
         'listTime': j['generatedAt'], 'listAgeH': j['listAgeH'], 'limitH': limit_h, 'highest': highest, 'hazards': hazards, 'count': len(in_force),
         'colourNotRead': len([i for i in in_force if i['colour'] == 'not read']), 'oldListItems': len(items) if state == 'no_reading' else 0,
         'rak': {'status': status, 'named': named, 'city': city, 'units': units}, 'warnings': items, 'droppedExpired': dropped,
         'ticker': {'read': tick is not None, 'reason': tick_why, 'items': [{k: t[k] for k in ('type', 'time', 'en', 'ar')} for t in latest_tick]}, 'tickerAll': tick,
         'announcements': {'read': ann is not None, 'reason': ann_why, 'items': ann or [], 'flag': bool(ann)}, 'stations': stations, 'stationsReason': st_why, 'airport': read_airport(capture.get('airports'), now, places),
         'tilesRead': len(tiles), 'visit': {k: capture.get(k) for k in ('bytes', 'decoded', 'requests', 'aborted', 'aborts', 'ms')}}
    r['rain'] = rain_totals(stations)
    return r


# ------------------------------------------------------------------------------------------------ output
def public_items(reading):
    return [{'id': i['id'], 'colour': i['colour'], 'hazards': i['hazards'], 'from': i['from'], 'to': i['to'], 'rak': i['rak']['status'], 'named': i['rak']['named'], 'city': i['rak']['city']}
            for i in reading['warnings'] if reading['state'] == 'warning']


def public_state(reading, health, radar_at, clock):
    """facts only: state word, colour, our own hazard labels, times, the RAK flag, rain numbers. Never a word of NCM."""
    items = public_items(reading)
    st = {'schema': 1, 'status': 'Reported', 'source': 'NCM reader on GitHub Actions', 'state': reading['stateWord'], 'reason': reading['reason'], 'reasonText': reading['reasonText'],
          'readAt': iso(reading['now']), 'readClock': clock, 'listTime': reading['listTime'], 'listAgeH': reading['listAgeH'], 'limitH': reading['limitH'], 'highest': reading['highest'], 'hazards': reading['hazards'],
          'count': reading['count'], 'colourNotRead': reading['colourNotRead'], 'oldListItems': reading['oldListItems'], 'tickerRead': reading['ticker']['read'],
          'tickerFailed': reading['ticker']['reason'], 'tickerNotNews': len([t for t in (reading.get('tickerAll') or []) if t['type'] != 'news']), 'announcements': len(reading['announcements']['items']),
          'rak': reading['rak'], 'items': items, 'itemsMore': 0, 'rain': reading['rain'], 'health': health, 'live': True, 'radarAt': radar_at,
          'bytes': reading['visit'].get('bytes'), 'requests': reading['visit'].get('requests')}
    while len(json.dumps(st, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > STATE_MAX - 100 and st['items']:
        st['items'].pop()
        st['itemsMore'] += 1
    return st


def live_doc(reading, health, clock):
    r = reading
    return {'kind': 'live', 'schema': 1, 'status': 'Reported', 'state': r['stateWord'], 'reason': r['reason'], 'reasonText': r['reasonText'], 'readAt': iso(r['now']), 'readClock': clock, 'listTime': r['listTime'],
            'listAgeH': r['listAgeH'], 'limitH': r['limitH'], 'latest': r['latest'], 'version': r['version'], 'contentHash': r['contentHash'], 'highest': r['highest'], 'hazards': r['hazards'], 'count': r['count'],
            'colourNotRead': r['colourNotRead'], 'oldListItems': r['oldListItems'], 'rak': r['rak'], 'warnings': r['warnings'], 'droppedExpired': r['droppedExpired'], 'ticker': r['ticker'], 'announcements': r['announcements'],
            'stations': r['stations'], 'stationsReason': r['stationsReason'], 'airport': r['airport'], 'rain': r['rain'],
            'health': dict(health, bytes=r['visit'].get('bytes'), requests=r['visit'].get('requests'), aborts=r['visit'].get('aborts'), tilesRead=r['tilesRead'])}


def update_health(prev, reading):
    prev = prev if isinstance(prev, dict) else {}
    t = iso(reading['now'])
    if reading['state'] == 'no_reading':
        return {'lastOk': prev.get('lastOk'), 'consecutiveFailures': int(prev.get('consecutiveFailures') or 0) + 1, 'failingSince': prev.get('failingSince') or t}
    return {'lastOk': t, 'consecutiveFailures': 0, 'failingSince': None}


def fingerprint(reading, health, radar_at):
    """what counts as a change: a different state, reason, colour, hazard, count, RAK flag, warning, ticker item or rain number. The age of the list and the read time do not."""
    return short_key(json.dumps({'s': reading['stateWord'], 'r': reading['reason'], 'h': reading['highest'], 'z': reading['hazards'], 'c': reading['count'], 'k': reading['rak'],
                                 'w': [[i['id'], i['colour'], i['from'], i['to'], i['rak']['status']] for i in reading['warnings']],
                                 't': sorted(t['key'] for t in (reading.get('tickerAll') or [])), 'a': reading['announcements']['flag'], 'f': health['consecutiveFailures'],
                                 'n': [reading['rain'][k] and reading['rain'][k]['mm'] for k in ('h1', 'h3', 'h24')], 'o': radar_at}, sort_keys=True, ensure_ascii=False))


def new_index():
    return {'schema': 1, 'warnings': {}, 'ticker': {}, 'announcements': {}, 'shapes': {}, 'gaps': [], 'maxAgeH': None, 'alerts': {'day': None, 'count': 0, 'suppressed': 0, 'sent': {}, 'open': {}}, 'lastCommitAt': None,
            'fingerprint': None, 'lastRadarAt': None, 'archives': []}


def _mk_history(events, now, public_key):
    plain = json.dumps({'kind': 'history', 'schema': 1, 't': iso(now), 'events': events}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return C.frame(C.seal(plain, public_key))


def diff_records(index, reading):
    """Update the public index from this reading and return the history events for the sealed record. An old item is never rewritten: a change is a new event, and an end is a new event.
    A reading that failed proves nothing about what ended, so it ends nothing."""
    ev, t = [], iso(reading['now'])
    good_list = reading['state'] != 'no_reading' or reading['reason'] == 'ticker_failed'
    wx = index['warnings']
    for i in reading['warnings']:
        shape = shape_of(i['raw'])
        known = index['shapes'].setdefault('warning', [])
        if not known:
            ev.append({'event': 'raw_first', 'type': 'warning', 't': t, 'raw': i['raw'], 'note': 'first raw item ever seen'})
            known.append(shape)
        elif shape not in known:
            ev.append({'event': 'shape', 'type': 'warning', 't': t, 'raw': i['raw'], 'note': 'new item shape'})
            known.append(shape)
        rec = wx.get(i['id'])
        facts = {'colour': i['colour'], 'hazards': i['hazards'], 'from': i['from'], 'to': i['to'], 'rak': i['rak']['status']}
        if rec is None or rec.get('ended'):
            wx[i['id']] = dict(facts, first=t, last=t, ended=None, reopened=bool(rec))
            ev.append({'event': 'first', 'type': 'warning', 'id': i['id'], 't': t, 'item': i})
        else:
            if any(rec.get(k) != v for k, v in facts.items()):
                ev.append({'event': 'change', 'type': 'warning', 'id': i['id'], 't': t, 'item': i})
            rec.update(facts)
            rec['last'] = t
    if good_list:
        now_ids = {i['id'] for i in reading['warnings']}
        for wid, rec in wx.items():
            if not rec.get('ended') and wid not in now_ids:
                rec['ended'] = t
                ev.append({'event': 'ended', 'type': 'warning', 'id': wid, 't': t, 'lastSeen': rec.get('last')})
    tick = reading.get('tickerAll')
    if tick is not None:
        tx = index['ticker']
        for it in tick:
            shape = shape_of(it['raw'])
            known = index['shapes'].setdefault('ticker', [])
            if not known:
                ev.append({'event': 'raw_first', 'type': 'ticker', 't': t, 'raw': it['raw'], 'note': 'first raw item ever seen'})
                known.append(shape)
            elif shape not in known:
                ev.append({'event': 'shape', 'type': 'ticker', 't': t, 'raw': it['raw'], 'note': 'new item shape'})
                known.append(shape)
            if it['type'] not in index['shapes'].setdefault('tickerTypes', []):
                index['shapes']['tickerTypes'].append(it['type'])
                if it['type'] != 'news':
                    ev.append({'event': 'raw_first', 'type': 'ticker:' + it['type'], 't': t, 'raw': it['raw'], 'note': 'first raw item of this ticker type'})
            rec = tx.get(it['key'])
            if rec is None or rec.get('ended'):
                tx[it['key']] = {'type': it['type'], 'time': it['time'], 'first': t, 'last': t, 'ended': None}
                ev.append({'event': 'first', 'type': 'ticker', 'key': it['key'], 't': t, 'item': {k: it[k] for k in ('type', 'time', 'en', 'ar')}})
            else:
                rec['last'] = t
        keys = {it['key'] for it in tick}
        for k, rec in tx.items():
            if not rec.get('ended') and k not in keys:
                rec['ended'] = t
                ev.append({'event': 'ended', 'type': 'ticker', 'key': k, 't': t, 'lastSeen': rec.get('last')})
    if reading['announcements']['read']:
        ax = index['announcements']
        for a in reading['announcements']['items']:
            k = short_key(json.dumps(a, sort_keys=True, ensure_ascii=False))
            if k not in ax:
                ax[k] = {'first': t}
                ev.append({'event': 'first', 'type': 'announcement', 'key': k, 't': t, 'raw': a})
    return ev


def log_gap(index, reading):
    """the record the freshness limit will be set from: every change of the NCM folder, and the longest age seen"""
    if reading['listAgeH'] is not None:
        index['maxAgeH'] = max(index.get('maxAgeH') or 0, reading['listAgeH'])
    if reading['latest'] and (not index['gaps'] or index['gaps'][-1].get('latest') != reading['latest']):
        prev = parse_iso(index['gaps'][-1].get('generatedAt')) if index['gaps'] else None
        cur = parse_iso(reading['listTime'])
        index['gaps'].append({'latest': reading['latest'], 'generatedAt': reading['listTime'], 'firstSeen': iso(reading['now']), 'gapH': round((cur - prev).total_seconds() / 3600.0, 2) if (cur and prev) else None})
        del index['gaps'][:-GAPS_KEPT]


def compact_index_event(index, archive, now):
    items = [dict(type='warning', id=k, **{x: v.get(x) for x in ('colour', 'hazards', 'first', 'last', 'ended', 'rak')}) for k, v in index['warnings'].items()]
    items += [dict(type='ticker', key=k, tickerType=v.get('type'), **{x: v.get(x) for x in ('time', 'first', 'last', 'ended')}) for k, v in index['ticker'].items()]
    return {'event': 'index', 't': iso(now), 'items': items, 'archives': list(index['archives']) + [archive], 'note': 'compact index of everything before this file'}


# ------------------------------------------------------------------------------------------------ alerts
def fmt_uae(s):
    t = parse_iso(s) if s else None
    return t.astimezone(UAE).strftime('%d %b %Y %H:%M') + ' UAE' if t else 'not read'


def _rak_words(status, named):
    return 'RAK %s%s' % (status, ', named in the text' if named else '')


def alert_events(index, reading, health):
    """What deserves a phone alert now. Worked out from what has been sent, not from what the reader saw before, so an alert whose call failed stays due. Facts only: colour, our own hazard labels, from, to, RAK status, read time."""
    ev, read, sent = [], fmt_uae(iso(reading['now'])), index['alerts']['sent']
    for i in reading['warnings'] if reading['state'] == 'warning' else []:
        first = sent.get('w:%s' % i['id'])
        told = (first or {}).get('colour')
        for k, r in sent.items():
            if k.startswith('w:%s:' % i['id']) and RANK.get(r.get('colour'), 0) > RANK.get(told, 0):
                told = r.get('colour')
        if first is None:
            title = 'NCM warning: %s, %s. %s' % (i['colour'], ', '.join(i['hazards']), _rak_words(i['rak']['status'], i['rak']['named']))
            key = 'w:%s' % i['id']
        elif told is not None and RANK[i['colour']] > RANK.get(told, 0):
            title = 'NCM warning rose from %s to %s: %s. %s' % (told, i['colour'], ', '.join(i['hazards']), _rak_words(i['rak']['status'], i['rak']['named']))
            key = 'w:%s:%s' % (i['id'], i['colour'])
        else:
            continue
        ev.append({'key': key, 'close': 'w:%s' % i['id'], 'colour': i['colour'], 'title': title,
                   'lines': [('Colour', i['colour']), ('Hazard', ', '.join(i['hazards'])), ('From', fmt_uae(i['from'])), ('To', fmt_uae(i['to'])), ('RAK', _rak_words(i['rak']['status'], i['rak']['named'])), ('Read', read)]})
    for it in reading.get('tickerAll') or []:
        key = 't:%s' % it['key']
        if it['type'] != 'news' and key not in sent:
            ev.append({'key': key, 'close': key, 'title': 'NCM text warning, no colour read. %s' % _rak_words(reading['rak']['status'], reading['rak']['named']),
                       'lines': [('Colour', 'not read'), ('Hazard', ', '.join(reading['hazards']) or 'not read'), ('Issued', fmt_uae(it['time'])), ('RAK', _rak_words(reading['rak']['status'], reading['rak']['named'])), ('Read', read)]})
    since = parse_iso(health.get('failingSince'))
    if since and (reading['now'] - since).total_seconds() / 3600.0 >= FAIL_ALERT_H:
        ev.append({'key': 'fail:%s' % health['failingSince'], 'close': 'fail', 'title': 'NCM reader: no reading for over %d hours' % FAIL_ALERT_H,
                   'lines': [('State', reading['stateWord']), ('Reason', reading['reason']), ('Since', fmt_uae(health['failingSince'])), ('Last good read', fmt_uae(health.get('lastOk'))), ('Read', read)]})
    return [e for e in ev if e['key'] not in sent]


def ended_closers(index, reading, health):
    """the close keys of alerts whose cause has ended"""
    out = []
    for wid, rec in index['warnings'].items():
        if rec.get('ended'):
            out.append(('w:%s' % wid, 'Ended %s. Last seen %s.' % (fmt_uae(rec['ended']), fmt_uae(rec.get('last')))))
    for k, rec in index['ticker'].items():
        if rec.get('ended') and rec.get('type') != 'news':
            out.append(('t:%s' % k, 'Left the NCM ticker %s.' % fmt_uae(rec['ended'])))
    if not health.get('failingSince'):
        out.append(('fail', 'The reader read NCM again at %s.' % fmt_uae(iso(reading['now']))))
    return out


class Issues:
    """GitHub issues through the REST API with the standard library. Never raises: a failed call never fails the reading."""

    def __init__(self, token, repo=REPO, opener=None):
        self.token, self.repo, self.opener, self.errors = token, repo, opener, []

    def call(self, method, path, body=None):
        req = urllib.request.Request('https://api.github.com/repos/%s%s' % (self.repo, path), method=method, data=json.dumps(body).encode('utf-8') if body is not None else None,
                                     headers={'Authorization': 'Bearer %s' % self.token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'wm004-ncm-reader', 'Content-Type': 'application/json'})
        try:
            if self.opener:
                return self.opener(req)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode('utf-8') or 'null')
        except urllib.error.HTTPError as e:
            self.errors.append('%s %s: HTTP %s' % (method, path.split('?')[0], e.code))
        except Exception as e:                  # a failed issue call never fails the reading
            self.errors.append('%s %s: %s' % (method, path.split('?')[0], scrub(e, 80)))
        return None

    def ensure_label(self):
        if self.call('GET', '/labels/%s' % LABEL) is None:
            self.errors[:] = self.errors[:-1]
            self.call('POST', '/labels', {'name': LABEL, 'color': 'b60205', 'description': 'WM004 NCM reader alert'})

    def seen_keys(self):
        """the keys of the latest alerts, from GitHub itself, so a commit that failed after an issue was opened cannot repeat it"""
        got = self.call('GET', '/issues?labels=%s&state=all&per_page=50' % LABEL) or []
        out = {}
        for i in got if isinstance(got, list) else []:
            m = re.search(r'wm004-ncm key=(\S+)', i.get('body') or '')
            if m:
                out[m.group(1)] = i.get('number')
        return out

    def open(self, title, body):
        r = self.call('POST', '/issues', {'title': title, 'body': body, 'assignees': [OWNER], 'labels': [LABEL]})
        return r.get('number') if isinstance(r, dict) else None

    def close(self, number, note):
        self.call('POST', '/issues/%d/comments' % number, {'body': note})
        return self.call('PATCH', '/issues/%d' % number, {'state': 'closed', 'state_reason': 'completed'}) is not None


def issue_body(ev, last_of_day=False):
    lines = ['@%s' % OWNER, ''] + ['**%s:** %s' % (k, v) for k, v in ev['lines']] + ['', 'Board: %s' % BOARD_URL, '', 'Status: Reported, read by the WM004 NCM reader.']
    if last_of_day:
        lines += ['', 'This is alert %d of the %d allowed today. Further changes today appear on the board only.' % (ALERTS_PER_DAY, ALERTS_PER_DAY)]
    lines += ['', '<!-- wm004-ncm key=%s -->' % ev['key']]
    return '\n'.join(lines)


def _close_of(key):
    """the close key of an alert found on GitHub with no record here"""
    if key.startswith('fail:'):
        return 'fail'
    return ':'.join(key.split(':')[:2])


def run_alerts(index, reading, health, issues, log):
    """Opens and closes issues and records them in the index. The caps and the no repeat rule are here."""
    al = index['alerts']
    day = reading['now'].astimezone(UAE).strftime('%Y-%m-%d')
    if al.get('day') != day:
        al.update(day=day, count=0, suppressed=0, suppressedKeys=[])
    al.setdefault('suppressedKeys', [])
    fresh = alert_events(index, reading, health)
    if fresh and issues:
        issues.ensure_label()
        for k, num in issues.seen_keys().items():
            al['sent'].setdefault(k, {'issue': num, 'close': _close_of(k), 'closed': False})
        fresh = [e for e in fresh if e['key'] not in al['sent']]
    done = []
    for e in fresh:
        if al['count'] >= ALERTS_PER_DAY:
            if e['key'] not in al['suppressedKeys']:
                al['suppressedKeys'].append(e['key'])
                al['suppressed'] = len(al['suppressedKeys'])
                log('alert held back by the daily cap: %s' % e['key'])
            continue
        al['count'] += 1
        if issues:
            num = issues.open(e['title'], issue_body(e, last_of_day=al['count'] == ALERTS_PER_DAY))
            if num is None:
                al['count'] -= 1            # not sent: it stays due at the next run
                log('alert not sent: %s' % e['key'])
                continue
            log('alert opened: issue %s for %s' % (num, e['key']))
        else:
            num = None
            log('alert due, no token in this run: %s' % e['key'])
        al['sent'][e['key']] = {'issue': num, 'close': e['close'], 'closed': False, 'colour': e.get('colour')}
        done.append(e)
    closers = dict(ended_closers(index, reading, health))
    for k, rec in al['sent'].items():
        if rec.get('closed') or rec.get('close') not in closers:
            continue
        if issues and rec.get('issue'):
            if issues.close(rec['issue'], closers[rec['close']]):
                rec['closed'] = True
                log('alert closed: issue %s' % rec['issue'])
        elif not rec.get('issue'):
            rec['closed'] = True
    shut = [k for k, r in al['sent'].items() if r.get('closed')]
    for k in shut[:max(0, len(shut) - 300)]:
        del al['sent'][k]
    return done


# ------------------------------------------------------------------------------------------------ one run
def read_json_file(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_bytes(p):
    try:
        with open(p, 'rb') as f:
            return f.read()
    except OSError:
        return None


def write_atomic(p, data):
    tmp = p + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, p)


def radar_due(reading, index, now):
    wet = reading['state'] in ('warning', 'text_warning') or any((reading['rain'][k] or {}).get('mm') for k in ('h1', 'h3'))
    if not wet:
        return False
    last = parse_iso(index.get('lastRadarAt'))
    return last is None or (now - last).total_seconds() / 60.0 >= RADAR_EVERY_MIN


def step(capture, folder, public_key, places, now, clock, issues=None, radar_fn=None, force_commit=False, limit_h=LIMIT_H, history_max=HISTORY_MAX, log=print):
    """Everything after the visit. Pure apart from the files in folder and the calls on issues, so the whole of it runs in the tests with no network."""
    p = lambda n: os.path.join(folder, n)
    prior_state = read_json_file(p('ncm_state.json')) or {}
    index = read_json_file(p('ncm_index.json'))
    if not isinstance(index, dict) or index.get('schema') != 1:
        index = new_index()
    for k, v in new_index().items():
        index.setdefault(k, v)
    reading = evaluate(capture, now, places, limit_h)
    health = update_health(prior_state.get('health'), reading)
    log_gap(index, reading)
    events = diff_records(index, reading)
    # the radar
    radar_at = index.get('lastRadarAt')
    radar_blob = None
    if radar_fn and radar_due(reading, index, now):
        try:
            still = radar_fn()
        except Exception as e:
            log('radar not captured: %s' % scrub(e))
            still = None
        if still:
            doc = {'kind': 'radar', 'schema': 1, 'status': 'Reported', 'readAt': iso(now), 'frameTime': still.get('frameTime'), 'mime': still['mime'], 'b64': base64.b64encode(still['data']).decode('ascii')}
            radar_blob = C.frame(C.seal(json.dumps(doc, separators=(',', ':')).encode('utf-8'), public_key))
            if len(radar_blob) > RADAR_MAX:
                log('radar still over the limit, not kept: %d bytes' % len(radar_blob))
                radar_blob = None
            else:
                radar_at = iso(now)
                index['lastRadarAt'] = radar_at
    fp = fingerprint(reading, health, radar_at)
    last = parse_iso(index.get('lastCommitAt'))
    age_min = (now - last).total_seconds() / 60.0 if last else None
    changed = fp != index.get('fingerprint') or bool(events)
    fired = run_alerts(index, reading, health, issues, log)
    alerts_changed = bool(fired) or any(True for r in index['alerts']['sent'].values() if r.get('closed') and not r.get('closeLogged'))
    for r in index['alerts']['sent'].values():
        if r.get('closed'):
            r['closeLogged'] = True
    commit = force_commit or changed or alerts_changed or age_min is None or age_min >= HEARTBEAT_MIN
    summary = {'read': iso(now), 'clock': clock, 'state': reading['stateWord'], 'reason': reading['reason'], 'latest': reading['latest'], 'generated_at': reading['listTime'], 'ageH': reading['listAgeH'], 'count': reading['count'],
               'highest': reading['highest'], 'rak': reading['rak']['status'], 'failures': health['consecutiveFailures'], 'bytes': reading['visit'].get('bytes'), 'decoded': reading['visit'].get('decoded'),
               'requests': reading['visit'].get('requests'), 'aborted': reading['visit'].get('aborted'), 'commit': bool(commit), 'events': len(events), 'alerts': len(fired)}
    log('NCM ' + json.dumps(summary, ensure_ascii=False))
    if not commit:
        return {'commit': False, 'reading': reading, 'events': events, 'summary': summary}
    index['fingerprint'], index['lastCommitAt'] = fp, iso(now)
    # the sealed reading: on a failed read the last good reading is copied unopened as frame 2
    frame1 = C.seal(json.dumps(live_doc(reading, health, clock), ensure_ascii=False, separators=(',', ':')).encode('utf-8'), public_key)
    live = C.frame(frame1)
    if reading['state'] == 'no_reading':
        prior = read_bytes(p('ncm_live.enc'))
        try:
            fr = C.frames(prior) if prior else []
        except ValueError:
            fr = []
        keep = (fr[1] if len(fr) > 1 else None) if prior_state.get('state') == 'NO READING' else (fr[0] if fr else None)
        if keep:
            live += C.frame(keep)
    write_atomic(p('ncm_live.enc'), live)
    if events:
        hist = read_bytes(p('ncm_history.enc')) or b''
        new = _mk_history(events, now, public_key)
        if len(hist) + len(new) > history_max and hist:
            archive = 'ncm_history_%s.enc' % now.astimezone(UAE).strftime('%Y%m')
            n = 1
            while os.path.exists(p(archive)):
                n += 1
                archive = 'ncm_history_%s_%d.enc' % (now.astimezone(UAE).strftime('%Y%m'), n)
            os.replace(p('ncm_history.enc'), p(archive))
            head = _mk_history([compact_index_event(index, archive, now)], now, public_key)
            index['archives'].append(archive)
            index['ticker'] = {k: v for k, v in index['ticker'].items() if not v.get('ended')}
            hist = head
        with open(p('ncm_history.enc'), 'wb') as f:
            f.write(hist + new)
    if radar_blob:
        write_atomic(p('ncm_radar.enc'), radar_blob)
    state = public_state(reading, health, radar_at, clock)
    state['alertsSuppressedToday'] = index['alerts']['suppressed']
    write_atomic(p('ncm_state.json'), json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    write_atomic(p('ncm_index.json'), json.dumps(index, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    return {'commit': True, 'reading': reading, 'events': events, 'summary': summary, 'state': state}


# ------------------------------------------------------------------------------------------------ the visit
def _kind(u):
    """what a reply is, from its address. The address itself is never kept: a page token lives in it."""
    path = u.split('?')[0]
    if path.endswith('/warnings/warnings_areas.json'):
        return 'areas', None
    if path.endswith('/warnings_list.json') and '/warnings/areas/' in path:
        return 'list', None
    if path.endswith('.pbf') and '/warnings/areas/' in path:
        m = re.search(r'/(\d+)/(\d+)/(\d+)\.pbf$', path)
        return ('tile', tuple(int(x) for x in m.groups())) if m else (None, None)
    for k, tail in (('tickers', '/ncm-api/warnings/tickers'), ('announcements', '/ncm-api/announcements'), ('aws', '/ncm-api/aws/observations/24h'), ('airports', '/ncm-api/airports/observations/24h')):
        if path.endswith(tail):
            return k, None
    return None, None


def _abort_it(rtype, url):
    """Skip pictures, fonts and map tiles to keep the load on NCM down. Warning tiles always pass: they are requested only when the list has items."""
    path = url.split('?')[0]
    if '/warnings/areas/' in path:
        return False
    if rtype in ('image', 'font', 'media'):
        return True
    return path.endswith(('.pbf', '.mvt', '.png', '.jpg', '.jpeg', '.webp'))


def visit(url=WARNINGS_URL, aborts=True, limit_s=45, tile_wait_s=12, viewport=(390, 800)):
    """One ordinary page visit: default browser identity, no stealth, no login. Captures what the page loads and stops when the list has arrived, or at the time limit."""
    from playwright.sync_api import sync_playwright
    cap = {'tiles': {}, 'aborts': aborts, 'bytes': 0, 'decoded': 0, 'requests': 0, 'aborted': 0}
    t0 = time.monotonic()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={'width': viewport[0], 'height': viewport[1]})
            page = ctx.new_page()
            queue = []
            page.on('response', lambda r: queue.append(r))
            page.on('request', lambda r: cap.__setitem__('requests', cap['requests'] + 1))
            try:
                cdp = ctx.new_cdp_session(page)
                cdp.send('Network.enable')
                cdp.on('Network.loadingFinished', lambda e: cap.__setitem__('bytes', cap['bytes'] + int(e.get('encodedDataLength') or 0)))
                cdp.on('Network.dataReceived', lambda e: cap.__setitem__('decoded', cap['decoded'] + int(e.get('dataLength') or 0)))
            except Exception:
                cap['bytes'] = cap['decoded'] = None
            if aborts:
                def router(route):
                    if _abort_it(route.request.resource_type, route.request.url):
                        cap['aborted'] += 1
                        route.abort()
                    else:
                        route.continue_()
                page.route('**/*', router)

            def drain():
                while queue:
                    r = queue.pop(0)
                    try:
                        k, tile = _kind(r.url)
                        if not k:
                            continue
                        if k == 'tile':
                            if r.status == 200:
                                cap['tiles'][tile] = r.body()
                        elif k not in cap:
                            cap[k] = {'status': r.status, 'date': r.headers.get('date'), 'body': r.body()}
                    except Exception:
                        pass
            try:
                doc = page.goto(url, wait_until='commit', timeout=30000)
            except Exception as e:
                cap['refused'] = 'the page did not load: %s' % scrub(e, 90)
                return cap
            if doc is None or doc.status >= 400:
                cap['refused'] = 'HTTP %s on the page' % (doc.status if doc else 'no reply')
                return cap
            cap['docDate'] = doc.headers.get('date')
            tile_deadline = None
            while time.monotonic() - t0 < limit_s:
                page.wait_for_timeout(200)
                drain()
                if all(k in cap for k in REQUIRED):
                    has = False
                    try:
                        has = bool(json.loads(cap['list']['body']).get('warnings'))
                    except (ValueError, AttributeError, TypeError):
                        pass
                    if not has or any(all(t in cap['tiles'] for t in pair) for pair in TILE_PAIRS):
                        break
                    tile_deadline = tile_deadline or time.monotonic() + tile_wait_s
                    if time.monotonic() > tile_deadline:
                        break
            drain()
            if 'list' not in cap or 'areas' not in cap:
                try:
                    low = page.content()[:200000].lower()
                except Exception:
                    low = ''
                if any(w in low for w in ('captcha', 'access denied', 'just a moment', 'request blocked', 'attention required')):
                    cap['refused'] = 'the page shows a block or a challenge'
        finally:
            cap['ms'] = int((time.monotonic() - t0) * 1000)
            browser.close()
    return cap


def visit_radar(limit_s=40):
    """A clipped still of the radar page. Pictures must load here, so only fonts are skipped. The clip leaves out the cookie banner at the foot of the page."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={'width': 900, 'height': 760})
            page = ctx.new_page()
            page.route('**/*', lambda r: r.abort() if r.request.resource_type == 'font' else r.continue_())
            tiles = []
            page.on('response', lambda r: tiles.append(1) if 'radar-Merge' in r.url and r.status == 200 else None)
            page.goto(RADAR_URL, wait_until='domcontentloaded', timeout=30000)
            t0 = time.monotonic()
            while time.monotonic() - t0 < limit_s and len(tiles) < 4:
                page.wait_for_timeout(300)
            page.wait_for_timeout(1500)
            if not tiles:
                return None
            box = None
            canvas = page.query_selector('canvas')
            if canvas:
                box = canvas.bounding_box()
            # the cookie banner, left unanswered, covers the foot of the map: the bottom 12% is left out. RAK sits in the upper right of this view, so nothing of use is lost.
            clip = {'x': (box or {}).get('x', 0), 'y': (box or {}).get('y', 0), 'width': min((box or {}).get('width', 900), 900), 'height': round(min((box or {}).get('height', 640), 640) * 0.88)}
            frame = None
            try:
                m = re.search(r'\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}\s*UAE', page.inner_text('body'))
                frame = m.group(0) if m else None
            except Exception:
                pass
            for q in (55, 40, 28, 20):
                data = page.screenshot(type='jpeg', quality=q, clip=clip)
                if len(data) * 4 / 3 < RADAR_MAX - 2000:
                    return {'data': data, 'mime': 'image/jpeg', 'frameTime': frame}
            return None
        finally:
            browser.close()


def clock_of(cap):
    """now, from the Date header of NCM's reply, never from this machine. The machine's clock is used only when no reply came at all, and the state says so."""
    for k in ('list', 'areas', 'tickers', 'aws'):
        t = http_date((cap.get(k) or {}).get('date'))
        if t:
            return t, 'NCM Date header'
    t = http_date(cap.get('docDate'))
    if t:
        return t, 'NCM Date header'
    return dt.datetime.now(dt.timezone.utc), 'runner clock, no NCM reply'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pub_path = None
    if '--pub' in argv:                 # a laptop check into a scratch folder reads the public key where it is staged
        i = argv.index('--pub')
        pub_path = argv[i + 1]
        del argv[i:i + 2]
    flags = {a for a in argv if a.startswith('--')}
    folder = next((a for a in argv if not a.startswith('--')), 'wm004')
    pem = read_bytes(pub_path or os.path.join(folder, 'ncm_pub.pem'))
    if not pem:
        print('READER CANNOT WRITE: ncm_pub.pem is missing in %s' % folder)
        return 1
    public_key = C.public_from_pem(pem)
    places = load_places()
    try:
        cap = visit(aborts='--plain' not in flags)
    except Exception as e:              # a visit that dies is NO READING, never a run that leaves the last reading standing as if it were current
        cap = {'failed': scrub(e, 120), 'tiles': {}}
    now, clock = clock_of(cap)
    token = os.environ.get('GITHUB_TOKEN')
    issues = Issues(token, os.environ.get('GITHUB_REPOSITORY') or REPO) if token and '--no-issues' not in flags else None
    radar_fn = visit_radar
    if '--radar-test' in flags:
        # a laptop check only: takes the still whatever the weather
        still = visit_radar()
        print('radar test:', {'bytes': len(still['data']), 'frameTime': still['frameTime']} if still else 'no still')
        if still:
            write_atomic(os.path.join(folder, 'ncm_radar.enc'), C.frame(C.seal(json.dumps({'kind': 'radar', 'schema': 1, 'status': 'Reported', 'readAt': iso(now), 'frameTime': still['frameTime'], 'mime': still['mime'],
                                                                                          'b64': base64.b64encode(still['data']).decode('ascii')}, separators=(',', ':')).encode('utf-8'), public_key)))
    res = step(cap, folder, public_key, places, now, clock, issues=issues, radar_fn=radar_fn, force_commit='--force-commit' in flags)
    if issues and issues.errors:
        print('issue calls that failed, the reading stands:', issues.errors[:5])
    gh = os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as f:
            f.write('commit=%s\nstate=%s\n' % ('yes' if res['commit'] else 'no', res['reading']['stateWord'].replace(' ', '_')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
