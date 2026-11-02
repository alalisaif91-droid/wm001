# -*- coding: utf-8 -*-
"""WM004 complaints and pumps reader, on GitHub's servers (2 Nov 2026, the owner: "i need it to auto and live all time").
Reads the public incident site (m85.ae/re) and the Wet Utilities pump map (m85.ae/psd) on every tick of the clock, as an ordinary visit. The laptop tasks stop when it sleeps; this does not.
  complaints.enc   sealed with the public key of the NCM reader: every report, opened only behind the access code. The runner can seal it and cannot open it.
  pumps_live.enc   sealed the same way: every pump with its plate and status
  live_index.json  public, counts and times only, no place and no words
A failed read is never calm: the sealed file stays as it was and the index names the failure and the time of the last good read. A new sealed file is written only when the content changes.
  python3 wm004/live_reader_action.py"""
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ncm_crypto as C

CSV_URL = 'https://m85.ae/re/ajax.php?action=export_csv'
PUMP_URL = 'https://m85.ae/psd/map.php'
UA = {'User-Agent': 'Mozilla/5.0 (PSD WM004 refresh)'}
INDEX = os.path.join(HERE, 'live_index.json')
LONG_DIGITS = re.compile(r'\d{7,}')


def now_uae():
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)).strftime('%Y-%m-%d %H:%M')


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40).read()


def num(x):
    try:
        return round(float(x), 6)
    except (TypeError, ValueError):
        return None


def complaints():
    rows = list(csv.DictReader(io.StringIO(get(CSV_URL).decode('utf-8-sig'))))
    if not rows or 'ID' not in rows[0] or 'Latitude' not in rows[0]:
        raise ValueError('the export does not read as the incident list')
    out = []
    for r in rows:
        text = (r.get('Description EN') or r.get('Description AR') or '').strip()
        if text.lower().startswith('test'):
            continue
        out.append(dict(id=int(r['ID']), type=r.get('Type') or '', custom=(r.get('Custom Type') or '').strip(), text='' if LONG_DIGITS.search(text) else text[:160],
                        priority=r.get('Priority') or '', status=r.get('Status') or '', lat=num(r.get('Latitude')), lon=num(r.get('Longitude')),
                        reported=(r.get('Reported At') or '')[:16], resolved=(r.get('Resolved At') or '')[:16]))
    return sorted(out, key=lambda c: c['id'])


def pumps():
    html = get(PUMP_URL).decode('utf-8', 'replace')
    dec = json.JSONDecoder()
    m = re.search(r'\bPUMPS\s*=\s*\[', html)
    if not m:
        raise ValueError('the pump list is not on the page')
    P = dec.raw_decode(html[m.end() - 1:])[0]
    keep = ('lat', 'lng', 'lon', 'plate', 'size', 'status', 'is_running', 'sector_name', 'location', 'location_name', 'name')
    return [{k: p[k] for k in keep if k in p} for p in P]


def seal(obj, name, pub, memo):
    """writes the sealed file when the content changed; returns the content hash"""
    plain = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    h = hashlib.sha256(plain).hexdigest()[:16]
    if memo != h or not os.path.exists(os.path.join(HERE, name)):
        tmp = os.path.join(HERE, '.' + name + '.part')
        with open(tmp, 'wb') as f:
            f.write(C.frame(C.seal(gzip.compress(plain, 9, mtime=0), pub)))
        os.replace(tmp, os.path.join(HERE, name))
    return h


def main():
    try:
        with open(INDEX, encoding='utf-8') as f:
            ix = json.load(f)
    except (OSError, ValueError):
        ix = {}
    with open(os.path.join(HERE, 'ncm_pub.txt'), 'rb') as f:
        pub = C.public_from_pem(f.read())
    now = now_uae()
    ix.update(schema=1, tz='Asia/Dubai', readAt=now, asof=now)
    try:
        c = complaints()
        ix['complaintsHash'] = seal(c, 'complaints.enc', pub, ix.get('complaintsHash'))
        ix['complaints'] = dict(read=now, count=len(c), open=sum(1 for x in c if x['status'] != 'resolved'), newest=max((x['reported'] for x in c), default=None), lastId=c[-1]['id'] if c else None)
        ix.pop('complaintsError', None)
        print('complaints', ix['complaints'])
    except Exception as e:      # the site down or changed: say so, keep the last good file
        ix['complaintsError'] = '%s: %s' % (type(e).__name__, str(e)[:120])
        print('COMPLAINTS NO READING', ix['complaintsError'])
    try:
        p = pumps()
        ix['pumpsHash'] = seal(p, 'pumps_live.enc', pub, ix.get('pumpsHash'))
        st = {}
        for x in p:
            st[x.get('status')] = st.get(x.get('status'), 0) + 1
        ix['pumps'] = dict(captured=now, total=len(p), standby=st.get('Standby', 0), working=st.get('Working', 0), noPump=st.get('Not Received', 0))
        ix.pop('pumpsError', None)
        print('pumps', ix['pumps'])
    except Exception as e:
        ix['pumpsError'] = '%s: %s' % (type(e).__name__, str(e)[:120])
        print('PUMPS NO READING', ix['pumpsError'])
    tmp = INDEX + '.part'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(ix, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)
    return 0


if __name__ == '__main__':
    sys.exit(main())
