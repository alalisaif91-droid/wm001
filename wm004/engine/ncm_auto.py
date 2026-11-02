# -*- coding: utf-8 -*-
"""Automatic NCM readings for WM004 (status Reported, official; appended to ncm_log.jsonl with by=engine).

The sources scout of 24 Oct 2026 found two things that can be read without a person:
  1. the NCM 5 day weather bulletin, a PDF behind a fixed address (plain HTTP, redirect, pdftotext). It carries no colour and no sector.
     It is read, and one entry is written in the engine's own words: which days name fog or mist, convective rain, blowing dust and strong wind.
     Level none, kind bulletin: it is never a warning and never counts as one.
  2. the warnings page, which is a JavaScript map. It is read through a rendered page (headless Edge or Chrome, one page load, a time limit).
     The engine reads only whether the page shows "No warning for now" or shows warnings (their colour classes). It does not forge or replay the page's tokens.
     "No warning for now" is written as an engine entry of level none, labelled automatic; it never ends or hides a manual colour entry.
     When the page shows warnings the engine cannot read their hazard, areas or text reliably, so it writes NO level: it returns the colours it saw, keeps them in
     the state file as pageWarnings (until a page read shows no warning, or a person logs a reading made after they first appeared) and the engine then calls the
     rain, fog and dust levels unknown, never none, and raises an alert to read the page and log the warning with ncm_log.py add (audit D1, D6).
Terms: NCM's terms bar commercial use and reproduction of its content needs written authorization (owner: the department head with the NCM liaison). Nothing here copies NCM text:
the entries are the engine's own summary. An empty list is not proof that the service answered, so an automatic none does not refresh the manual reading age.
Every failure is returned as a status, never raised; a failure writes nothing.

30 Oct 2026 (the live reader: warnings were being missed, because the only reader was hourly and stopped when the laptop slept). NCM is now read by a GitHub Action, every 30 minutes since 31 Oct (site_ncm/ncm_reader_action.py).
  *  The page message "No warning for now" is no longer a reading. The page prints it after a failed fetch, a missing list, a malformed item or a hidden tab (site_ncm/SCHEMA.md section 4), so this file writes no
     entry on it and never clears a sticky colour on it. parse_warnings_dom calls it 'message', and it means only that the page rendered.
  *  read_live fetches the reader's public state from the site, and with the laptop key opens ncm_live.enc. Each new warning is written to the log with source the NCM warnings page and by NCM live reader, with no
     duplicate on a rerun and nothing when the reading is not good. The text of an entry is in our own words, because the log text reaches public files (weather.json, ncm.json, status.json); NCM's words stay in a field
     no public file reads. With no key the public facts are used.
  *  A colour not read, or a warning in text only, is kept as a sticky page colour: the levels are unknown, not none.
  *  A reading of NO WARNING IN FORCE (SCHEMA.md section 8, and nothing else) writes an automatic none every 3 hours and clears the sticky colours. It is by engine, so it never ends a colour a person or the reader logged.
  *  The Edge read stays as the backup: it runs only when the live reader gave no good reading.
  *  ncm_live_state.json is the snapshot the weather channel reads. Its age is taken from the Date header of the site's reply."""
import os, re, sys, time, shutil, subprocess, tempfile, datetime as dt, json, email.utils
from wx_util import read_json, read_json_status, quarantine_copy, atomic_write_json, f_local, now_local, DEFAULT_DATA_DIR, parse_local, hours_between
import ncm_log as N

BULLETIN_URL = 'https://www.ncm.gov.ae/services/download-weather-bulletin?lang=en'
WARNINGS_URL = 'https://www.ncm.gov.ae/maps-warnings?lang=en'
BULLETIN_EVERY_H = 6.0
WARNINGS_EVERY_H = 3.0
SITE_WM004 = 'https://alalisaif91-droid.github.io/wm001/wm004/'
LIVE_BY = 'NCM live reader'
LIVE_STATES = ('WARNING', 'TEXT WARNING', 'NO WARNING IN FORCE', 'NO READING')
LIVE_FRESH_MIN = 90                                 # older than this a reading is not acted on here; the channel's silent alert is at 90 minutes
LIVE_SNAPSHOT = 'ncm_live_state.json'
NCM_KEY_PATH = os.environ.get('WM004_NCM_KEY') or os.path.join(os.path.expanduser('~'), '.wm004_ncm_key.pem')
SEALING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'site_ncm')
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
PDFTOTEXT_PATHS = [r'C:\Program Files\Git\mingw64\bin\pdftotext.exe', r'C:\Program Files\poppler\Library\bin\pdftotext.exe', r'C:\poppler\Library\bin\pdftotext.exe', '/mingw64/bin/pdftotext', '/usr/bin/pdftotext']
BROWSER_PATHS = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                 r'C:\Program Files\Google\Chrome\Application\chrome.exe', r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe']


def find_tool(names, paths):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for p in paths:
        if os.path.exists(p):
            return p
    return None


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _kill_tree(p):
    """end a process and the helper processes it started (Edge starts several)"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True, timeout=15)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_bounded(cmd, capture_output=True, timeout=None):
    """subprocess.run with a time limit that holds. On Windows subprocess.run(capture_output=True, timeout=...) waits for the pipes to close, and a helper process that inherited them
    keeps them open after the browser itself has gone: a 5 second limit returned after 41 seconds. Here the output goes to files, not pipes, the wait is on the process alone, and on
    a timeout the whole process tree is ended before TimeoutExpired is raised (audit F17). Same arguments and result as subprocess.run for the uses in this module."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        of, ef = os.path.join(d, 'out'), os.path.join(d, 'err')
        with open(of, 'wb') as fo, open(ef, 'wb') as fe:
            p = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(p)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise subprocess.TimeoutExpired(cmd, timeout)
        with open(of, 'rb') as fo, open(ef, 'rb') as fe:
            return _Result(p.returncode, fo.read(), fe.read())


# ------------------------------------------------------------------------------------------------ the bulletin
def parse_bulletin_text(text):
    """Facts from the extracted bulletin text: its date and, for each day it covers, whether fog or mist, convective rain, blowing dust and strong wind are named.
    Returns None when the text does not look like the bulletin."""
    if not text or 'Weather Today' not in text and 'Forecast for the coming' not in text:
        return None
    head = ' '.join(text[:400].split())
    m = re.search(r'(\d{1,2}) (%s) (\d{4})' % '|'.join(MONTHS), head)
    if not m:
        return None
    date0 = dt.datetime(int(m.group(3)), MONTHS.index(m.group(2)) + 1, int(m.group(1)))
    flat = text
    # the sections: today, then one per later day starting with a weekday line
    day_pat = re.compile(r'^\s*(%s) (\d{1,2}) (%s) (\d{4})\s*$' % ('|'.join(WEEKDAYS), '|'.join(MONTHS)), re.M)
    marks = [(mm.start(), dt.datetime(int(mm.group(4)), MONTHS.index(mm.group(3)) + 1, int(mm.group(2)))) for mm in day_pat.finditer(flat)]
    t0 = flat.find('Weather Today')
    t1 = flat.find('Area', t0) if t0 >= 0 else -1
    sections = []
    if t0 >= 0:
        sections.append((date0, flat[t0:t1 if t1 > t0 else t0 + 900]))
    for i, (pos, d) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(flat)
        sections.append((d, flat[pos:end]))
    days = []
    for d, sec in sections:
        s = ' '.join(sec.lower().split())
        fog = 'fog' in s
        mist = 'mist' in s
        wording = 'chance' if 'chance of fog' in s or 'chance of some' in s else ('probability' if 'probability of fog' in s else None)
        rain = bool(re.search(r'\brain', s))
        conv = 'convective' in s
        dust = 'blowing dust' in s or ' dust' in s
        reach = [int(x) for x in re.findall(r'reaching (\d+)', s)]
        days.append({'date': d.strftime('%Y-%m-%d'), 'fog': fog or mist, 'fogWording': wording if (fog or mist) else None, 'rain': rain, 'convective': conv, 'dust': dust,
                     'windMaxKmh': max(reach) if reach else None, 'northEast': ('northward' in s or 'eastward' in s)})
    return {'date': date0.strftime('%Y-%m-%d'), 'days': days}


def _span(ds):
    """the days named, as runs: '24 Oct, 26 to 28 Oct' (a day with no mention is not inside a range)"""
    if not ds:
        return ''
    days = sorted(dt.datetime.strptime(x, '%Y-%m-%d') for x in ds)
    runs, cur = [], [days[0]]
    for d in days[1:]:
        if (d - cur[-1]).days == 1:
            cur.append(d)
        else:
            runs.append(cur)
            cur = [d]
    runs.append(cur)
    f = lambda d: d.strftime('%d %b')
    return ', '.join(f(r[0]) if len(r) == 1 else '%s to %s' % (f(r[0]), f(r[-1])) for r in runs)


def bulletin_text(facts):
    """the engine's own summary of the bulletin: no NCM sentence is copied"""
    d = dt.datetime.strptime(facts['date'], '%Y-%m-%d').strftime('%d %b %Y')
    fog = [x['date'] for x in facts['days'] if x['fog']]
    rain = [x['date'] for x in facts['days'] if x['rain']]
    dust = [x['date'] for x in facts['days'] if x['dust']]
    w = max([x['windMaxKmh'] for x in facts['days'] if x['windMaxKmh']] or [0])
    parts = ['NCM 5 day bulletin of %s, read automatically; the bulletin carries no colour, so this is not a warning.' % d]
    parts.append('Fog or mist is named for %s (a chance or a probability).' % _span(fog) if fog else 'No fog or mist is named.')
    parts.append('Convective cloud with rainfall is named for %s.' % _span(rain) if rain else 'No rainfall is named.')
    if dust:
        parts.append('Blowing dust is named for %s%s.' % (_span(dust), (' with wind reaching %d km/h' % w) if w else ''))
    elif w:
        parts.append('Wind reaching %d km/h is named.' % w)
    return ' '.join(parts)


def read_bulletin(fetcher, now, runner=run_bounded):
    """returns {'ok','error','facts','ms','how'}; never raises"""
    t0 = time.monotonic()
    out = {'ok': False, 'error': None, 'facts': None, 'how': None}
    try:
        text = None
        if getattr(fetcher, 'fixture_dir', None):
            p = os.path.join(fetcher.fixture_dir, 'ncm_bulletin.txt')
            if not os.path.exists(p):
                out['error'] = 'no fixture'
                return out
            with open(p, encoding='utf-8') as f:
                text = f.read()
            out['how'] = 'fixture text'
        else:
            data, info = fetcher.get_bytes('ncm_bulletin', BULLETIN_URL)
            if data is None:
                out['error'] = 'bulletin download failed: %s' % (info.get('error') or info.get('status'))
                return out
            if not data.startswith(b'%PDF'):
                out['error'] = 'the bulletin address did not return a PDF'
                return out
            tool = find_tool(['pdftotext'], PDFTOTEXT_PATHS)
            if not tool:
                out['error'] = 'pdftotext not found on this laptop (it is in Git for Windows under mingw64\\bin, or in poppler)'
                return out
            with tempfile.TemporaryDirectory() as d:
                pdf = os.path.join(d, 'b.pdf')
                with open(pdf, 'wb') as f:
                    f.write(data)
                r = runner([tool, '-layout', pdf, '-'], capture_output=True, timeout=40)
                if r.returncode != 0:
                    out['error'] = 'pdftotext failed (%s)' % r.returncode
                    return out
                text = r.stdout.decode('utf-8', errors='replace')
            out['how'] = 'download and pdftotext'
        facts = parse_bulletin_text(text)
        if not facts:
            out['error'] = 'the text did not look like the NCM bulletin (the format may have changed)'
            return out
        out['facts'] = facts
        out['ok'] = True
    except Exception as e:                                      # graceful: a failure is a status
        out['error'] = 'bulletin read failed: %s' % str(e)[:120]
    finally:
        out['ms'] = int((time.monotonic() - t0) * 1000)
    return out


# ------------------------------------------------------------------------------------------------ the warnings page
def parse_warnings_dom(dom):
    """state of the rendered NCM warnings page: 'warnings' (colour classes present), 'message' (the empty message: the page rendered and nothing more is known, see SCHEMA.md section 4) or 'unreadable'"""
    if not dom or len(dom) < 20000 or 'pageContainer' not in dom:
        return {'state': 'unreadable', 'reason': 'the page did not render (too short or not the NCM page)'}
    cols = re.findall(r'class="[^"]*\bwarning-([123])\b[^"]*"', dom)
    if cols:
        names = {'1': 'yellow', '2': 'orange', '3': 'red'}
        seen = sorted({names[c] for c in cols}, key=lambda x: N.RANK[x])
        return {'state': 'warnings', 'colours': seen, 'count': len(cols)}
    if 'msg-no-warnings' in dom or 'No warning for now' in dom:
        tick = len(re.findall(r'class="ticker-time', dom))
        return {'state': 'message', 'tickerItems': tick}
    return {'state': 'unreadable', 'reason': 'the page rendered but shows neither the empty message nor warning colours'}


def read_warnings_page(fixture_dir=None, runner=run_bounded, timeout=80):
    t0 = time.monotonic()
    out = {'ok': False, 'error': None, 'state': None}
    try:
        if fixture_dir:
            p = os.path.join(fixture_dir, 'ncm_warnings_dom.html')
            if not os.path.exists(p):
                out['error'] = 'no fixture'
                return out
            with open(p, encoding='utf-8') as f:
                dom = f.read()
        else:
            exe = find_tool(['msedge', 'chrome'], BROWSER_PATHS)
            if not exe:
                out['error'] = 'no Edge or Chrome found for the rendered read of the warnings page'
                return out
            with tempfile.TemporaryDirectory() as prof:
                r = runner([exe, '--headless=new', '--disable-gpu', '--no-first-run', '--user-data-dir=' + prof, '--virtual-time-budget=20000', '--dump-dom', WARNINGS_URL],
                           capture_output=True, timeout=timeout)
                dom = r.stdout.decode('utf-8', errors='replace')
        res = parse_warnings_dom(dom)
        out.update(res)
        out['ok'] = res['state'] in ('message', 'warnings')
        if not out['ok']:
            out['error'] = res.get('reason')
    except subprocess.TimeoutExpired:
        out['error'] = 'the rendered read of the warnings page timed out'
    except Exception as e:
        out['error'] = 'warnings page read failed: %s' % str(e)[:120]
    finally:
        out['ms'] = int((time.monotonic() - t0) * 1000)
    return out


# ------------------------------------------------------------------------------------------------ the live reader
def _uae(s):
    """an ISO time of the reader as a local time string, or None"""
    try:
        return f_local(parse_local(s)) if s else None
    except (ValueError, TypeError):
        return None


def _site_now(fetcher, now):
    """the clock of the site's reply, because the laptop clock is minutes fast; the laptop clock only when no reply came"""
    ds = getattr(fetcher, 'server_dates', None) or []
    try:
        return parse_local(email.utils.parsedate_to_datetime(ds[-1]))
    except (IndexError, TypeError, ValueError):
        return now


def _open_live(blob):
    """(frame 1 of ncm_live.enc opened with the laptop key, why not). Frame 2 is a past reading and is never read here."""
    if not os.path.exists(NCM_KEY_PATH):
        return None, 'no key on this laptop'
    try:
        if SEALING_DIR not in sys.path:
            sys.path.insert(0, SEALING_DIR)
        import ncm_crypto as C
        with open(NCM_KEY_PATH, 'rb') as f:
            key = C.private_from_pem(f.read())
        return json.loads(C.open_sealed(C.frames(blob)[0], key).decode('utf-8')), None
    except Exception as e:                                  # a sealed file that does not open leaves the public facts
        return None, 'the sealed reading did not open (%s)' % type(e).__name__


def read_live(fetcher, now):
    """What the reader on the site last wrote. Never raises. ok only for a good reading (not NO READING, not old). {'ran','ok','error','facts','warnings','how','ageMin','state'}"""
    t0 = time.monotonic()
    out = {'ran': True, 'ok': False, 'error': None, 'facts': None, 'warnings': None, 'how': None, 'ageMin': None, 'state': None}
    try:
        data, info = fetcher.get_bytes('ncm_site_state', '%sncm_state.json?t=%d' % (SITE_WM004, int(time.time())))
        if data is None:
            out['error'] = 'the reader state was not fetched: %s' % (info.get('error') or info.get('status'))
            return out
        st = json.loads(data.decode('utf-8'))
        if not isinstance(st, dict) or st.get('state') not in LIVE_STATES or not _uae(st.get('readAt')):
            out['error'] = 'the reader state is not in the shape this file reads'
            return out
        out['facts'], out['state'] = st, st['state']
        out['ageMin'] = int((_site_now(fetcher, now) - parse_local(st['readAt'])).total_seconds() // 60)
        if out['ageMin'] > LIVE_FRESH_MIN:
            out['error'] = 'the reader state is %d minutes old' % out['ageMin']
            return out
        if out['ageMin'] < -10:
            out['error'] = 'the read time of the reader state is ahead of the clock'
            return out
        if st['state'] == 'NO READING':
            out['error'] = 'the reader says NO READING (%s)' % st.get('reason')
            return out
        out['ok'] = True
        out['how'] = 'public facts'
        if st['state'] == 'WARNING' and os.path.exists(NCM_KEY_PATH):
            blob, _ = fetcher.get_bytes('ncm_site_live', '%sncm_live.enc?t=%d' % (SITE_WM004, int(time.time())))
            doc, why = _open_live(blob) if blob else (None, 'the sealed reading was not fetched')
            if doc and doc.get('kind') == 'live' and doc.get('readAt') == st.get('readAt'):
                out['warnings'], out['how'] = doc.get('warnings') or [], 'sealed reading'
            elif why:
                out['note'] = why
    except Exception as e:                                  # graceful: a failure is a status
        out['error'] = 'the reader state could not be read: %s' % str(e)[:120]
    finally:
        out['ms'] = int((time.monotonic() - t0) * 1000)
    return out


def write_snapshot(data_dir, live, now):
    """what the weather channel reads: facts of the reader, with the age it had when the site was asked"""
    st = live.get('facts') or {}
    atomic_write_json(os.path.join(data_dir, LIVE_SNAPSHOT), {'schema': 1, 'fetchedAt': f_local(now), 'fetched': bool(st), 'error': live.get('error'), 'how': live.get('how'), 'state': st.get('state'), 'reason': st.get('reason'),
                                                              'readAt': _uae(st.get('readAt')), 'ageMinAtFetch': live.get('ageMin'), 'listAgeH': st.get('listAgeH'), 'highest': st.get('highest'), 'hazards': st.get('hazards') or [],
                                                              'count': st.get('count'), 'colourNotRead': st.get('colourNotRead') or 0, 'tickerNotNews': st.get('tickerNotNews') or 0, 'tickerRead': st.get('tickerRead'), 'tickerHazards': st.get('tickerHazards') or [],
                                                              'itemsMore': st.get('itemsMore') or 0, 'rak': st.get('rak'), 'items': st.get('items') or [],
                                                              'health': st.get('health')}, indent=1)


def _live_items(live):
    """the warnings of a good reading in one shape, from the sealed reading or from the public facts"""
    if live.get('warnings') is not None:
        return [{'id': w.get('id'), 'colour': w.get('colour'), 'hazards': w.get('hazards') or [], 'from': w.get('from'), 'to': w.get('to'), 'published': w.get('published'), 'rak': (w.get('rak') or {}).get('status'),
                 'named': bool((w.get('rak') or {}).get('named')), 'units': (w.get('rak') or {}).get('units') or [], 'en': w.get('en'), 'ar': w.get('ar')} for w in live['warnings']]
    return [{'id': w.get('id'), 'colour': w.get('colour'), 'hazards': w.get('hazards') or [], 'from': w.get('from'), 'to': w.get('to'), 'published': None, 'rak': w.get('rak'), 'named': bool(w.get('named')),
             'units': [], 'en': None, 'ar': None} for w in (live.get('facts') or {}).get('items') or []]


def log_live(data_dir, live, now, write):
    """Writes each warning of a good reading that the log does not hold yet. Returns (ids appended, warnings whose colour could not be read, errors). The key is the warning, its colour and its end, so a rerun adds nothing
    and an extended or raised warning is a new entry; an old entry is never touched."""
    entries, _ = N.load(data_dir)
    held = {(e.get('ncmId'), e.get('level'), e.get('to')) for e in entries if e.get('ncmId')}
    read = _uae((live.get('facts') or {}).get('readAt'))
    appended, unread, errors = [], [], []
    for w in _live_items(live):
        if w['colour'] not in N.RANK or w['colour'] == 'none':
            unread.append(w['id'])
            continue
        t_from, t_to = _uae(w['from']), _uae(w['to'])
        if (w['id'], w['colour'], t_to) in held:
            continue
        rak = 'yes' if (w['rak'] == 'touched' or w['named']) else ('no' if w['rak'] == 'not touched' else 'unknown')
        hz = [h for h in w['hazards'] if h in N.HAZARDS] or ['other']
        areas = 'RAK %s%s' % (w['rak'] or 'not known', (': ' + ', '.join(w['units'])) if w['units'] else '')
        text = 'NCM warning %s, %s, %s, from %s to %s, RAK %s. Read by the NCM live reader.' % (w['id'], w['colour'], ', '.join(hz), t_from or 'not read', t_to or 'not read', w['rak'] or 'not known')
        extra = {'ncmId': w['id'], 'auto': True, 'note': 'NCM live reader, every 30 minutes; the text is in our own words; ' + live['how']}
        if w['en'] or w['ar']:
            extra['ncmWords'] = {'en': w['en'], 'ar': w['ar']}        # kept on the laptop only: no public file reads this field
        try:
            e = N.add_entry(data_dir, now=now, write=write, level=w['colour'], hazard=','.join(hz), text=text, source=WARNINGS_URL, areas=areas, rak_named=rak, t_from=t_from, t_to=t_to, by=LIVE_BY, read=read, kind='warning',
                            issued=_uae(w['published']), extra=extra)
            appended.append(e['id'])
            held.add((w['id'], w['colour'], t_to))
        except (ValueError, OSError) as ex:
            errors.append('warning %s not written: %s' % (w['id'], ex))
            unread.append(w['id'])
    return appended, unread, errors


def hazard_open(st):
    """a warning in force that the colours of the list cannot answer for: a ticker warning beside the list, which has no colour and no polygon, a ticker that was not read, or an item of hazard other that may touch RAK. No log entry can
    carry any of these as a rain, fog or dust colour, so without this the rain level stayed none beside it (independent review, F3, F4 and F5)."""
    if st.get('state') != 'WARNING':
        return False
    items = [x for x in st.get('items') or [] if isinstance(x, dict)]
    if st.get('tickerRead') is False or any('other' in (x.get('hazards') or []) and (x.get('rak') != 'not touched' or x.get('named')) for x in items):
        return True
    if not st.get('tickerNotNews'):
        return False
    # 1 Nov 2026, the first real warning (2435, yellow): NCM repeats a list warning on the ticker in words. Hazards the ticker names that a coloured item of the list already carries add nothing, or every warning leaves fog unknown.
    listed = {h for x in items if x.get('colour') in N.RANK and x.get('colour') != 'none' for h in x.get('hazards') or []}
    tick = st.get('tickerHazards') or []
    return not tick or 'other' in tick or not set(tick) <= listed


def apply_live(data_dir, live, state, now, write, status):
    """What a good reading changes here. Returns True when the state file changed."""
    st = live['facts']
    state['lastLiveOk'], state['lastLiveState'] = f_local(now), st['state']
    state.pop('stateLostAt', None) if st['state'] != 'TEXT WARNING' else None
    if st['state'] == 'WARNING':
        ids, unread, errs = log_live(data_dir, live, now, write)
        status['appended'] += ids
        status['live'].update(logged=ids, colourNotRead=unread, errors=errs)
    else:
        unread = []
    if unread or st['state'] == 'TEXT WARNING' or hazard_open(st):
        # a warning whose colour is not known, or whose hazard is not read: the levels it could be about are unknown, not none, until a person logs a reading or the reader reads no warning in force
        prev = state.get('pageWarnings') if isinstance(state.get('pageWarnings'), dict) else {}
        state['pageWarnings'] = {'since': prev.get('since') or f_local(now), 'seen': f_local(now), 'colours': prev.get('colours') or [], 'count': len(unread) or st.get('tickerNotNews') or 1, 'by': LIVE_BY}
    elif st['state'] == 'WARNING' and isinstance(state.get('pageWarnings'), dict) and state['pageWarnings'].get('by') == LIVE_BY:
        state.pop('pageWarnings', None)                     # what the reader could not read earlier, it has now read: the colours are on the log. A colour seen on the page by the Edge read is not touched.
    if st['state'] == 'NO WARNING IN FORCE':
        state.pop('pageWarnings', None)                     # SCHEMA.md section 8: the list and the ticker were both read and hold nothing. The page message never does this.
        last = state.get('lastLiveNone')
        try:
            due_none = not last or hours_between(parse_local(last), now) >= WARNINGS_EVERY_H
        except ValueError:
            due_none = True
        if due_none:
            try:
                e = N.add_entry(data_dir, now=now, write=write, level='none', hazard='rain,fog,dust,wind', text='The NCM live reader read the warnings list and the ticker and found no warning in force.', source=WARNINGS_URL, areas='all areas',
                                rak_named='unknown', by='engine', read=_uae(st['readAt']), kind='warning', extra={'auto': True, 'note': 'NCM live reader: list and ticker both read; never ends or hides a logged colour'})
                status['appended'].append(e['id'])
                state['lastLiveNone'] = f_local(now)
            except (ValueError, OSError) as ex:
                status['live']['errors'] = ['automatic none not written: %s' % ex]
    return True


# ------------------------------------------------------------------------------------------------ one pass
def state_shape_ok(st):
    """a state file the engine can read: a table whose pageWarnings, when present, is a table with a readable since or seen time and a list of colour words. Valid JSON of another shape (a list, a
    string, a number where a table was, an unparsable time) is damage like any other: the colour it may have held is not known (audit N8, FILE-1)."""
    if not isinstance(st, dict):
        return False
    pw = st.get('pageWarnings')
    if pw is None:
        return True
    if not isinstance(pw, dict):
        return False
    have = False
    for k in ('since', 'seen'):
        v = pw.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            return False
        try:
            if parse_local(v) is None:
                return False
        except (ValueError, TypeError, OverflowError):
            return False
        have = True
    if not have:
        return False
    cols = pw.get('colours')
    return cols is None or (isinstance(cols, list) and all(isinstance(c, str) for c in cols))


def run(data_dir, fetcher, now, runner=run_bounded, force=False, write=True):
    """Read what can be read now and append the engine's entries. Returns a status dict; never raises and never edits an earlier entry.
    {'bulletin': {...}, 'warnings': {...}, 'appended': [ids]}"""
    st_path = os.path.join(data_dir, 'ncm_auto_state.json')
    state, st_status = read_json_status(st_path)
    status = {'bulletin': {'ran': False}, 'warnings': {'ran': False}, 'live': {'ran': False}, 'appended': []}
    changed = False
    write_blocked = False
    if st_status == 'missing' or (st_status == 'ok' and state_shape_ok(state)):
        state = state if isinstance(state, dict) else {}
    else:
        # a state file that is there and does not read is not an empty one: it holds the only memory of warning colours that nobody has logged (audit F7). It is kept aside before it is
        # rewritten, and the new file says that what was in it is lost, so the colours stay unknown until a page read shows no warning or a person logs a reading
        state = {'stateLostAt': f_local(now)}
        changed = True
        status['state_damaged'] = 'ncm_auto_state.json %s' % ('cannot be opened' if st_status == 'unreadable' else 'does not parse or is not in the shape the engine reads')
        if write:
            try:
                if st_status == 'unreadable':
                    raise OSError('cannot be opened')
                quarantine_copy(st_path)
            except OSError:
                write_blocked = True                              # the damaged file stays as it is: nothing is written over it

    def due(key, every):
        last = state.get(key)
        if force or not last:
            return True
        try:
            return hours_between(parse_local(last), now) >= every
        except ValueError:
            return True
    try:
        entries, _ = N.load(data_dir)
        if due('lastBulletinTry', BULLETIN_EVERY_H):
            b = read_bulletin(fetcher, now, runner)
            status['bulletin'] = {'ran': True, 'ok': b['ok'], 'error': b['error'], 'ms': b.get('ms'), 'how': b.get('how')}
            state['lastBulletinTry'] = f_local(now)
            changed = True
            if b['ok']:
                facts = b['facts']
                state['lastBulletinOk'] = f_local(now)
                if any(e.get('bulletinDate') == facts['date'] and e.get('text') == bulletin_text(facts) for e in entries):
                    status['bulletin']['note'] = 'this bulletin is already in the log'
                else:
                    hz = [h for h, k in (('fog', 'fog'), ('rain', 'rain'), ('dust', 'dust')) if any(x[k] for x in facts['days'])]
                    if any((x['windMaxKmh'] or 0) >= 40 for x in facts['days']):
                        hz.append('wind')
                    hz = hz or ['other']
                    try:
                        # numbered and appended under the lock of the log, so a person logging a reading at the same moment cannot lose it or share its id (audit N3)
                        e = N.add_entry(data_dir, now=now, write=write, level='none', hazard=','.join(hz), text=bulletin_text(facts), source=BULLETIN_URL, areas=N.BULLETIN_AREAS,
                                        rak_named='unknown', t_from=facts['date'] + ' 08:00', t_to=None, by='engine', read=f_local(now), kind='bulletin',
                                        extra={'auto': True, 'bulletinDate': facts['date'], 'note': 'automatic read of the 5 day bulletin; no colour; own words, no NCM text copied'})
                        status['appended'].append(e['id'])
                        entries = entries + [e]
                    except (ValueError, OSError) as ex:
                        status['bulletin']['error'] = 'not written: %s' % ex
            else:
                state['lastBulletinError'] = b['error']
        live = read_live(fetcher, now) if hasattr(fetcher, 'get_bytes') else {'ran': False, 'ok': False}
        status['live'] = {k: live.get(k) for k in ('ran', 'ok', 'error', 'state', 'ageMin', 'how', 'ms', 'note') if k in live}
        if live.get('ran') and write:
            try:
                write_snapshot(data_dir, live, now)
            except OSError as ex:
                status['live']['error'] = 'the snapshot was not written: %s' % ex
        if live.get('ok'):
            changed = apply_live(data_dir, live, state, now, write, status) or changed
        left = fetcher.time_left() if hasattr(fetcher, 'time_left') else 999
        host_down = (status['bulletin'].get('ran') and not status['bulletin'].get('ok') and 'download failed' in (status['bulletin'].get('error') or ''))
        if live.get('ok'):
            status['warnings'] = {'ran': False, 'skipped': 'the live reader gave a good reading; the Edge read is the backup'}
        elif due('lastWarningsTry', WARNINGS_EVERY_H) and (host_down or left < 25):
            status['warnings'] = {'ran': False, 'skipped': 'ncm.gov.ae did not answer the bulletin request' if host_down else 'the time budget of the run is nearly used'}
        elif due('lastWarningsTry', WARNINGS_EVERY_H):
            w = read_warnings_page(getattr(fetcher, 'fixture_dir', None), runner, timeout=int(min(80, max(20, left - 10))))
            status['warnings'] = {'ran': True, 'ok': w['ok'], 'state': w.get('state'), 'colours': w.get('colours'), 'count': w.get('count'), 'error': w['error'], 'ms': w.get('ms')}
            state['lastWarningsTry'] = f_local(now)
            changed = True
            state['lastWarningsState'] = w.get('state')
            state['lastWarningsColours'] = w.get('colours')
            if w['ok']:
                state['lastWarningsOk'] = f_local(now)
            if w['ok'] and w['state'] == 'warnings':
                state.pop('stateLostAt', None)                      # the page has been read since the state file was lost: what it shows is known again. The empty message shows nothing.
                # sticky: it stays until the live reader reads no warning in force or a person logs a reading made after the colours first appeared; a failed or unreadable read
                # later does not clear it, and neither does the page's empty message (SCHEMA.md section 4). A colour not seen in the earlier sighting starts the clock again.
                prev = state.get('pageWarnings') if isinstance(state.get('pageWarnings'), dict) else {}
                new_cols = [c for c in (w.get('colours') or []) if c not in (prev.get('colours') or [])]
                state['pageWarnings'] = {'since': (prev.get('since') if prev.get('since') and not new_cols else f_local(now)), 'seen': f_local(now),
                                         'colours': w.get('colours'), 'count': w.get('count')}
            if w['ok'] and w['state'] == 'warnings':
                status['warnings']['alert'] = 'The NCM warnings page shows %d warning(s), colours %s. The engine cannot read the hazard and areas reliably, so no level was written: read the page and log it with ncm_log.py add.' % (w['count'], ', '.join(w['colours']))
        if changed and write and not write_blocked:
            state['updated'] = f_local(now)
            atomic_write_json(st_path, state, indent=1)
    except Exception as e:                                       # graceful failure, always
        status['error'] = 'automatic NCM read failed: %s' % str(e)[:150]
    status['state'] = {k: state.get(k) for k in ('lastBulletinOk', 'lastBulletinError', 'lastWarningsOk', 'lastWarningsState', 'lastWarningsColours', 'pageWarnings', 'stateLostAt', 'lastLiveOk', 'lastLiveState')}
    return status


def page_pending_text(page):
    """the sentence for a page colour, or a lost state file, that nobody has logged (alert, stage flag, NCM text)"""
    hz = ', '.join(page.get('pendingHazards') or ['rain', 'fog', 'dust'])
    if page.get('stateUnreadable'):
        return ('ncm_auto_state.json cannot be read (or is in a shape the engine does not read), so it is not known whether the NCM warnings page shows warning colours that nobody has logged: the %s level(s) are unknown, not none, '
                'until a person has logged a reading of the page that names %s (ncm_log.py add) made after %s, or an automatic page read shows no warning' % (hz, hz, page.get('since')))
    if page.get('by') == LIVE_BY:
        return ('The NCM reader reads a warning whose colour or hazard it could not read, first seen %s: the %s level(s) are unknown, not none, until the reader reads a colour for it or reads no warning in force' % (page.get('since'), hz))
    cols = ', '.join(page.get('colours') or ['colour not read'])
    return ('The NCM warnings page shows warning colours (%s) that nobody has logged yet, first seen %s, and no person has logged a reading naming %s since: the %s level(s) are unknown, not none, '
            'until the page is read and logged with ncm_log.py add' % (cols, page.get('since'), hz, hz))


def pending_page_warnings(data_dir, entries, now):
    """The warning colours the NCM page showed that no person has logged yet (read only; works whether or not this run read the page, so --no-ncm-auto cannot hide it).
    Returns None when there is none, else {'since','seen','colours','count','covered','coveredBy','pending'}. A colour on the page with no hazard or text attached is
    covered only by a manual warning reading made at or after the time it first appeared: the engine cannot tell which hazard it is for, so until then the rain,
    fog and dust levels it would call none are unknown."""
    sp = os.path.join(data_dir, 'ncm_auto_state.json')
    st, status = read_json_status(sp)
    unreadable = False
    if status == 'missing':
        st = {}
    elif status != 'ok' or not state_shape_ok(st):
        # a state file that is there and cannot be read is not an empty one: it is the only memory of warning colours nobody has logged (audit F7). Whether the page shows colours is
        # then not known, and the colours stay unknown until a person logs a reading made after the file was last written, or an automatic page read shows no warning.
        unreadable = True
        st = {}
    pw = st.get('pageWarnings')
    if st.get('stateLostAt') and not isinstance(pw, dict):
        unreadable = True
    if unreadable:
        try:
            if st.get('stateLostAt'):
                since = parse_local(st['stateLostAt'])
            else:
                m = dt.datetime.fromtimestamp(os.path.getmtime(sp), tz=dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)
                since = min(m, now)
        except (OSError, ValueError, TypeError):
            since = now
        pw = {'since': f_local(since), 'seen': f_local(since), 'colours': [], 'count': None}
    elif not isinstance(pw, dict) or not (pw.get('seen') or pw.get('since')):
        if st.get('lastWarningsState') == 'warnings' and st.get('lastWarningsOk'):
            pw = {'since': st['lastWarningsOk'], 'seen': st['lastWarningsOk'], 'colours': st.get('lastWarningsColours'), 'count': None}
        else:
            return None
    try:
        since = parse_local(pw.get('since') or pw.get('seen'))
    except (ValueError, TypeError):
        return None
    cols = [c for c in (pw.get('colours') or []) if c in N.RANK]
    # a colour with no hazard attached is covered only by manual warning readings made at or after it first appeared that NAME the hazard: rain, fog and dust each need one (a reading of
    # heat or wind says nothing about a colour that may be for fog), so a hazard no reading names stays unknown (audit F8)
    named, after = set(), []
    for e in entries:
        if e.get('kind') == 'warning' and not N.no_person(e) and parse_local(e['read']) >= since:
            named |= set(e['hazard'])
            after.append(e['id'])
    pend = [h for h in N.COVER_HAZARDS if h not in named]
    return {'since': f_local(since), 'seen': pw.get('seen') or pw.get('since'), 'colours': cols, 'count': pw.get('count'), 'covered': not pend, 'coveredBy': after[:5], 'pending': bool(pend),
            'pendingHazards': pend, 'namedHazards': sorted(named & set(N.COVER_HAZARDS)), 'stateUnreadable': unreadable, 'by': pw.get('by')}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='one automatic read of the NCM bulletin and the warnings page (appends engine entries to ncm_log.jsonl)')
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    from weather_engine import Fetcher
    f = Fetcher()
    print(json.dumps(run(a.data_dir, f, now_local(), force=a.force, write=not a.dry_run), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
