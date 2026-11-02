# -*- coding: utf-8 -*-
"""Observations for the WM004 weather engine: aviation reports from OMRK, OMFJ and OMSJ (status Reported).

Per station summaries (rain, shower and thunder hours, fog, mist, haze, dust, visibility, wind and gust, temperature, dew point,
pressure, cloud), the observed fog ladder of the calibration, the TAF summary, and the hourly observation store that keeps what the
stations reported hour by hour (data/weather/obs/YYYY-MM.json). The store only adds: merging the same report twice changes nothing,
so the 30 hour look back of every run fills the gaps between runs and the event register, the season record and the verification can
all read the same record. A station that did not answer is an alert, never a clear sky."""
import os, re, json, math, datetime as dt
from wx_util import (read_json, read_json_status, read_text_raw, quarantine_copy, atomic_write_json, file_lock, floor_hour, f_local, f_iso, hours_between, haversine_km, DEFAULT_DATA_DIR, POINTS,
                     utc_to_local, point_in_polygon)
import wx_parse as P

STATIONS_DEFAULT = [
    {'icao': 'OMRK', 'name': 'Ras Al Khaimah Intl', 'lat': 25.613, 'lon': 55.939, 'elev': 18, 'side': 'west coast', 'source': 'aviationweather.gov station record'},
    {'icao': 'OMFJ', 'name': 'Fujairah Intl', 'lat': 25.112, 'lon': 56.324, 'elev': 27, 'side': 'east coast', 'source': 'aviationweather.gov station record'},
    {'icao': 'OMSJ', 'name': 'Sharjah Intl', 'lat': 25.329, 'lon': 55.517, 'elev': 33, 'side': 'west coast (Sharjah, not Ras Al Khaimah)', 'source': 'aviationweather.gov station record'},
]
SILENT_HOURS = 3.0
RAIN_NOW_MIN = 90
FOG_VIS_LE = 1000          # visibility of 1000 m or less is fog for the observed ladder, fogNow, the fog event trigger and the station fog hours of an event (the calibrated ladder says 1 km or less);
                           # the season fog hours keep the calibration's own fog hour (under 1 km) so that they compare with last season (audit D15)
HOLD_MIN_MIN = 60          # a qualifying fog report holds its level at least this long, whatever the clock hour (audit WX-08)
FUTURE_TOL_MIN = 10        # a report stamped later than the engine clock plus this many minutes is not a report yet: it is not stored and not used (audit N2)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_stations(data_dir, problems=None):
    """the stations of stations.json (or the three defaults). A record without a four letter id or without numeric coordinates is skipped and named in problems, never a
    KeyError that stops the run (audit WX-11)."""
    s, s_status = read_json_status(os.path.join(data_dir, 'stations.json'))
    if s_status in ('damaged', 'unreadable') and problems is not None:
        # a file that is there and does not read is not a missing one: a station that was added to it would be dropped without a word (audit V8)
        problems.append('stations.json %s: the three default stations are used, so a station added to it is not read (owner: the weather desk)' % ('cannot be opened' if s_status == 'unreadable' else 'does not parse'))
    items = s.get('stations') if isinstance(s, dict) else None
    items = items if isinstance(items, list) and items else STATIONS_DEFAULT
    st = []
    for x in items:
        if isinstance(x, dict) and isinstance(x.get('icao'), str) and x['icao'] and _num(x.get('lat')) and _num(x.get('lon')) and -90 <= x['lat'] <= 90 and -180 <= x['lon'] <= 180:
            st.append(dict(x))            # coordinates outside the globe are a typing slip: the station is skipped and named, never a math domain error that stops every run (audit FILE-1)
        elif problems is not None:
            problems.append('stations.json: a station record without an id or coordinates was skipped (%s)' % (str(x.get('icao') if isinstance(x, dict) else x)[:20]))
    if not st:
        st = [dict(x) for x in STATIONS_DEFAULT]
        if problems is not None:
            problems.append('stations.json holds no usable station: the three default stations are used')
    for x in st:
        x['distKm'] = {p[0]: round(haversine_km(x['lat'], x['lon'], p[1], p[2]), 1) for p in POINTS}
    return st


# ------------------------------------------------------------------------------------------------ reading a response
def parse_metar_response(data):
    """AWC METAR JSON (a list of records) to {icao: [observation rows, oldest first]} plus a list of problems. Rows that cannot be read are counted."""
    rows, problems = {}, []
    if not isinstance(data, list):
        return rows, ['the METAR answer is not a list']
    bad, nil = 0, 0
    for rec in data:
        try:
            o = P.norm_metar_record(rec)
        except Exception as e:                      # one bad record never stops the rest
            bad += 1
            continue
        if not o['station'] or o['time_utc'] is None:
            bad += 1
            continue
        if o.get('nil'):
            nil += 1                                # a NIL METAR says the report is missing: it is not a clear sky and not a fresh report (audit D8)
            continue
        rows.setdefault(o['station'], []).append(o)
    for k in rows:
        rows[k].sort(key=lambda r: r['time_utc'])
    if bad:
        problems.append('%d METAR records could not be read' % bad)
    if nil:
        problems.append('%d NIL METAR reports (no data) were not counted as reports' % nil)
    return rows, problems


def parse_taf_response(data):
    """AWC TAF JSON to {icao: parsed latest TAF}. The raw text is parsed; the structured groups are the fallback."""
    out, problems = {}, []
    if not isinstance(data, list):
        return out, ['the TAF answer is not a list']
    best = {}
    for rec in data:
        if not isinstance(rec, dict):
            problems.append('a TAF record of the wrong shape was skipped')
            continue
        icao = rec.get('icaoId')
        if not icao or not isinstance(icao, str):
            continue
        mr = rec.get('mostRecent')
        key = (str(rec.get('issueTime') or rec.get('bulletinTime') or ''), mr if isinstance(mr, (int, float)) and not isinstance(mr, bool) else 0)
        if icao not in best or key >= best[icao][0]:
            best[icao] = (key, rec)
    for icao, (_, rec) in sorted(best.items()):
        issue = None
        try:
            if isinstance(rec.get('issueTime'), (int, float)):
                issue = dt.datetime.fromtimestamp(rec['issueTime'], tz=dt.timezone.utc).replace(tzinfo=None)
            elif rec.get('issueTime'):
                issue = dt.datetime.fromisoformat(str(rec['issueTime']).replace('Z', '+00:00')).astimezone(dt.timezone.utc).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            issue = None                                       # an issue time beyond the calendar is no issue time (audit TAF-1)
        t = None
        try:
            if isinstance(rec.get('rawTAF'), str) and rec.get('rawTAF'):
                t = P.parse_taf(rec['rawTAF'], issue)
                if not t['groups'] or t['valid_from'] is None:
                    t = None
        except Exception:
            t = None
        if t is None:
            try:
                t = P.taf_from_json(rec)
            except Exception as e:                                # a record of a shape nobody expected: that station has no TAF, the others are read (audit V4)
                problems.append('TAF %s could not be read (%s)' % (icao, type(e).__name__))
                continue
            if issue and not t.get('issue_utc'):
                t['issue_utc'] = issue
            problems.append('TAF %s read from the structured fields (the raw text did not parse)' % icao)
        t.setdefault('source', 'raw text')
        out[icao] = t
    return out, problems


# ------------------------------------------------------------------------------------------------ reports stamped in the future (audit N2)
def split_future(rows_by_station, now_local, tol_min=FUTURE_TOL_MIN):
    """(rows stamped no later than now plus tol_min, [{'station','at','kind':'METAR'} for the rows that were later]). A METAR stamped in the future is a fault of the feed, of the laptop clock or of a
    station clock: merged into the append only store it would open a fog or rain event when the clock reached its hour, with no report behind it. A station whose every row is later is
    left out altogether (it did not answer). The clock is the engine's clock, which is the servers' time when the laptop's was off."""
    lim = now_local + dt.timedelta(minutes=tol_min)
    keep, rej = {}, []
    for icao, rows in rows_by_station.items():
        ok = []
        for r in rows:
            t = r.get('time_local')
            if t is not None and t > lim:
                rej.append({'station': icao, 'at': f_local(t), 'kind': 'METAR'})
            else:
                ok.append(r)
        if ok or not rows:
            keep[icao] = ok
    return keep, rej


def split_future_tafs(tafs, now_local, tol_min=FUTURE_TOL_MIN):
    """(tafs issued no later than now plus tol_min, [{'station','at','kind':'TAF'}]): a TAF's validity runs ahead by design, its issue time never does"""
    lim = now_local + dt.timedelta(minutes=tol_min)
    keep, rej = {}, []
    for icao, t in tafs.items():
        iu = t.get('issue_utc') if isinstance(t, dict) else None
        if iu is not None and utc_to_local(iu) > lim:
            rej.append({'station': icao, 'at': f_local(utc_to_local(iu)), 'kind': 'TAF'})
        else:
            keep[icao] = t
    return keep, rej


def future_alert(rejected, now_local):
    """the sentence for the health alerts; station and stamp, at most six named"""
    if not rejected:
        return None
    shown = '; '.join('%s %s %s' % (x['station'], x['kind'], x['at']) for x in rejected[:6])
    return ('%d aviation report(s) were stamped more than %d minutes after the engine clock (%s) and were not stored and not used: %s%s. A report from the future is a fault of the feed or of a clock; '
            'check the laptop clock and the station feed' % (len(rejected), FUTURE_TOL_MIN, f_local(now_local), shown, ' and more' if len(rejected) > 6 else ''))


# ------------------------------------------------------------------------------------------------ station summary
def _hour_key(t):
    return floor_hour(t).strftime('%Y-%m-%dT%H')


def station_summary(meta, rows, now_local, hours=24):
    """Summary of one station over the last `hours` (rows are observation rows with time_local, naive local). Reported."""
    icao = meta['icao']
    out = {'icao': icao, 'name': meta.get('name'), 'lat': meta.get('lat'), 'lon': meta.get('lon'), 'side': meta.get('side'), 'distKm': meta.get('distKm'), 'ok': False}
    rs = [r for r in rows if r['time_local'] and r['time_local'] <= now_local + dt.timedelta(minutes=10)]
    if not rs:
        out['error'] = 'no report in the answer'
        out['n24'] = 0
        return out
    out['ok'] = True
    start = now_local - dt.timedelta(hours=hours)
    last24 = [r for r in rs if r['time_local'] >= start]
    last = rs[-1]
    out['n24'] = len(last24)
    out['lastReport'] = f_local(last['time_local'])
    out['lastReportAgeMin'] = int(round((now_local - last['time_local']).total_seconds() / 60.0))
    out['type'] = last['type']
    out['wind'] = {'dir': last['wind_dir'], 'vrb': last['wind_vrb'], 'kt': last['wind_kt'], 'gustKt': last['gust_kt']}
    out['visM'] = last['vis_m']
    out['visMinDirM'] = last.get('vis_min_m')
    out['visMinDir'] = last.get('vis_min_dir')
    out['cavok'] = last['cavok']
    out['tempC'] = last['temp_c']
    out['dewC'] = last['dew_c']
    out['spreadC'] = last['spread_c']
    out['qnhHpa'] = last['qnh_hpa']
    out['cloud'] = 'CAVOK' if last['cavok'] else ('NSC' if (last['nsc'] and not last['clouds']) else ' '.join('%s%s%s' % (c['cover'], '%03d' % (c['base_ft'] // 100) if c['base_ft'] is not None else '///', c['type'] or '') for c in last['clouds']) or None)
    out['wx'] = last['wx_text'] or None
    out['raw'] = last['raw']
    fl = last['flags']
    fresh = out['lastReportAgeMin'] <= RAIN_NOW_MIN
    out['rainNow'] = bool(fl['rain_any']) if fresh else None
    out['fogNow'] = bool(fl['fog'] or (last['vis_m'] is not None and last['vis_m'] <= FOG_VIS_LE) or last['vv_ft'] is not None) if fresh else None
    out['mistNow'] = bool(fl['mist']) if fresh else None
    out['hazeNow'] = bool(fl['haze']) if fresh else None
    out['dustNow'] = bool(fl['dust']) if fresh else None
    out['thunderNow'] = bool(fl['thunder']) if fresh else None
    out['vcThunderNow'] = bool(fl['vc_ts'] or fl['vc_sh']) if fresh else None

    def hrs(pred):
        return len({_hour_key(r['time_local']) for r in last24 if pred(r)})
    out['rainHours24'] = hrs(lambda r: r['flags']['rain'])
    out['showerHours24'] = hrs(lambda r: r['flags']['shower'])
    out['thunderHours24'] = hrs(lambda r: r['flags']['thunder'])
    out['rainAnyHours24'] = hrs(lambda r: r['flags']['rain_any'])
    out['fogHours24'] = hrs(lambda r: r['flags']['fog'] or (r['vis_m'] is not None and r['vis_m'] <= FOG_VIS_LE) or r['vv_ft'] is not None)
    out['mistHours24'] = hrs(lambda r: r['flags']['mist'])
    out['hazeHours24'] = hrs(lambda r: r['flags']['haze'])
    out['dustHours24'] = hrs(lambda r: r['flags']['dust'])
    out['vcTsReports24'] = sum(1 for r in last24 if r['flags']['vc_ts'] or r['flags']['vc_sh'])
    vis = [(r['vis_m'], r['time_local']) for r in last24 if r['vis_m'] is not None]
    if vis:
        v, t = min(vis, key=lambda x: (x[0], x[1]))
        out['minVisM24'] = v
        out['minVisAt24'] = f_local(t)
    else:
        out['minVisM24'], out['minVisAt24'] = None, None
    g = [r['gust_kt'] for r in last24 if r['gust_kt'] is not None]
    out['maxGustKt24'] = max(g) if g else None
    out['maxGustKmh24'] = round(max(g) * 1.852) if g else None
    out['silentHours'] = round(out['lastReportAgeMin'] / 60.0, 1)
    out['silent'] = out['silentHours'] > SILENT_HOURS
    out['fresh'] = not out['silent']
    if out['fogNow'] and (out['minVisM24'] is None or out['minVisM24'] > FOG_VIS_LE):
        out['minVisNote'] = 'fog is reported but the report carries no visibility group of 1000 m or less: the lowest visibility of the 24 hours is not the fog visibility'
    return out


def obs_block(stations, rows_by_station, now_local):
    """the weather.json obs block"""
    block = {'status': 'Reported (aviation weather reports, METAR and SPECI, aviationweather.gov)', 'asof': f_local(now_local), 'stations': {}, 'notes': [
        'The reports carry no rainfall amount: precipitation is a present weather code only. No rain gauge register is on file (gap, owner Wet Utilities with NCM).',
        'Station visibility is the prevailing visibility in metres; 10000 means 10 km or more. OMSJ is Sharjah, not Ras Al Khaimah.']}
    for m in stations:
        try:
            block['stations'][m['icao']] = station_summary(m, rows_by_station.get(m['icao'], []), now_local)
        except Exception as e:
            # one station whose reports cannot be summarised is a station that did not answer, never a run that stops (audit F3)
            block['stations'][m['icao']] = {'icao': m['icao'], 'name': m.get('name'), 'ok': False, 'error': 'the reports could not be summarised (%s: %s)' % (type(e).__name__, str(e)[:60]), 'n24': 0}
    ok = [s for s in block['stations'].values() if s['ok']]
    west = [m['icao'] for m in stations if 'west' in str(m.get('side') or '').lower()]
    block['any'] = {'westStations': west, 'westFresh': [i for i in west if block['stations'][i]['ok'] and not block['stations'][i].get('silent')],
                    'freshStations': [s['icao'] for s in ok if not s.get('silent')],
                    'stationsAnswered': len(ok), 'stationsFresh': sum(1 for s in ok if not s.get('silent')), 'stationsTotal': len(stations),
                    'rainNow': (any(s.get('rainNow') for s in ok) if any(s.get('rainNow') is not None for s in ok) else None),
                    'fogNow': (any(s.get('fogNow') for s in ok) if any(s.get('fogNow') is not None for s in ok) else None),
                    'dustNow': (any(s.get('dustNow') for s in ok) if any(s.get('dustNow') is not None for s in ok) else None),
                    'rainStations': [s['icao'] for s in ok if s.get('rainNow')], 'fogStations': [s['icao'] for s in ok if s.get('fogNow')],
                    'dustStations': [s['icao'] for s in ok if s.get('dustNow')],
                    'rainHours24': {s['icao']: s['rainAnyHours24'] for s in ok}, 'fogHours24': {s['icao']: s['fogHours24'] for s in ok},
                    'minVisM24': min([s['minVisM24'] for s in ok if s['minVisM24'] is not None] or [None]) if any(s['minVisM24'] is not None for s in ok) else None}
    return block


def taf_block(stations, tafs, now_local):
    now_utc = now_local - dt.timedelta(hours=4)
    out = {'status': 'Reported (aerodrome forecast, TAF, aviationweather.gov); a forecast by the aviation forecaster, not an NCM warning', 'asof': f_local(now_local), 'stations': {}}
    for m in stations:
        t = tafs.get(m['icao'])
        if not t:
            out['stations'][m['icao']] = {'ok': False, 'error': 'no TAF in the answer'}
            continue
        fl = P.taf_flags(t, now_utc, 24)
        groups = []
        for g in t['groups']:
            if g.get('from') is None:
                continue
            groups.append({'type': g['type'], 'from': f_local(utc_to_local(g['from'])), 'to': f_local(utc_to_local(g.get('persist_to') or g['to'])) if (g.get('persist_to') or g.get('to')) else None,
                           'prob': g.get('prob'), 'visM': g.get('vis_m'), 'wx': g.get('wx_text') or None, 'cb': g.get('convective_cloud')})
        out['stations'][m['icao']] = {'ok': True, 'issued': f_local(utc_to_local(t['issue_utc'])) if t.get('issue_utc') else None,
                                      'validFrom': f_local(utc_to_local(t['valid_from'])) if t.get('valid_from') else None,
                                      'validTo': f_local(utc_to_local(t['valid_to'])) if t.get('valid_to') else None, 'raw': t.get('raw'), 'groups': groups, 'next24': fl,
                                      'rainForecast': fl['rainForecast'], 'fogForecast': fl['fogForecast'], 'source': t.get('source', 'raw text')}
    oks = [s for s in out['stations'].values() if s.get('ok')]
    out['any'] = {'rainForecast': any(s['rainForecast'] for s in oks) if oks else None, 'fogForecast': any(s['fogForecast'] for s in oks) if oks else None,
                  'convective': any(s['next24']['convectiveCloud'] or s['next24']['thunderForecast'] for s in oks) if oks else None,
                  'dust': any(s['next24']['dustForecast'] for s in oks) if oks else None}
    return out


# ------------------------------------------------------------------------------------------------ the observed fog ladder
def _night(h):
    return h >= 20 or h <= 7


def _hold_until(t):
    """a qualifying fog report holds the level until 10:00 of the same morning (night and early morning reports), or 3 hours (daytime), and never for less than
    HOLD_MIN_MIN minutes: a report of 09:50 still says fog at 10:05 and the level must not read none while the station says fog now (audit WX-08)"""
    if t.hour < 10:
        base = t.replace(hour=10, minute=0, second=0, microsecond=0)
    elif t.hour >= 20:
        base = (t + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    else:
        base = t + dt.timedelta(hours=3)
    return max(base, t + dt.timedelta(minutes=HOLD_MIN_MIN))


def rows_from_store(store, icao, start, end, skip_hours=()):
    """observation rows rebuilt from the hourly store for the hours that the live answer does not hold (a failed or empty METAR feed): the first minute of fog, vertical
    visibility and the lowest visibility of each hour. Enough for the fog rules that need no dew point; the night mist rule needs the spread and is not applied to them."""
    out = []
    for t, rec in store.hours(icao, start, end):
        hk = t.strftime('%Y-%m-%dT%H')
        if hk in skip_hours:
            continue
        mnt = _rec_flag(rec, 'fog')
        if mnt is None and rec.get('minVis') is None:
            continue
        when = t + dt.timedelta(minutes=mnt if mnt is not None else rec.get('minVisMin', 0))
        out.append({'station': icao, 'time_local': when, 'vis_m': rec.get('minVis'), 'vv_ft': 100 if 'vv' in rec else None, 'spread_c': None, 'wind_kt': None, 'fromStore': True,
                    'flags': {'fog': 'fog' in rec, 'mist': 'mist' in rec}})
    return out


def observed_fog(rows_by_station, now_local, store=None, stations=None):
    """The observed side of the calibrated fog ladder (Proposed, calibration.json fog.levels), read from the reports of the last 30 hours.
    severe: a report under 500 m, vertical visibility reported, or 1000 m or less in 3 consecutive hours.
    alert: fog reported or 1000 m or less; or, between 20:00 and 07:00, visibility under 5 km or mist with a dew point spread of 1.5 C or less and wind 5 kt or less.
    A qualifying report holds its level until 10:00 (the calibration: 89 to 98 percent of episodes are over by 10:00), and for at least an hour.
    With a store, the hours the live answer does not hold are read from the hourly record, so a METAR feed that fails while fog is occurring does not throw away what the
    stations reported before it failed (audit WX-03).
    Returns (level 0..3, list of the reports that set it)"""
    best, why = 0, []
    rows_by_station = dict(rows_by_station)
    if store is not None:
        for icao in [m['icao'] for m in (stations or [])] or list(rows_by_station):
            live = rows_by_station.get(icao, [])
            have = {_hour_key(r['time_local']) for r in live if r.get('time_local')}
            extra = rows_from_store(store, icao, now_local - dt.timedelta(hours=30), now_local, have)
            if extra:
                rows_by_station[icao] = sorted(list(live) + extra, key=lambda r: r['time_local'])
    for icao, rows in rows_by_station.items():
        qual = []
        hour_min = {}
        for r in rows:
            t = r['time_local']
            if t is None or t > now_local + dt.timedelta(minutes=10):
                continue
            vis, fl = r['vis_m'], r['flags']
            lv, rule = 0, None
            if r['vv_ft'] is not None:
                lv, rule = 3, 'vertical visibility reported'
            elif vis is not None and vis < 500:
                lv, rule = 3, 'visibility under 500 m'
            elif fl['fog'] or (vis is not None and vis <= FOG_VIS_LE):
                lv, rule = 2, ('fog reported' if fl['fog'] else ('visibility 1000 m or less' + (' with dust reported (blowing dust, not fog; it errs towards alarm)' if fl.get('dust') and not fl.get('mist') else '')))
            elif _night(t.hour) and ((vis is not None and vis < 5000) or fl['mist']) and r['spread_c'] is not None and r['spread_c'] <= 1.5 and (r['wind_kt'] is not None and r['wind_kt'] <= 5):
                lv, rule = 2, 'night, visibility under 5 km or mist, spread %.1f C, wind %s kt' % (r['spread_c'], r['wind_kt'])
            if lv:
                qual.append((t, lv, rule, vis))
            if vis is not None:
                k = floor_hour(t)
                hour_min[k] = min(hour_min.get(k, 99999), vis)
        # 1000 m or less for three consecutive hours
        hrs = sorted(k for k, v in hour_min.items() if v <= 1000)
        for k in hrs:
            if (k + dt.timedelta(hours=1)) in hour_min and hour_min[k + dt.timedelta(hours=1)] <= 1000 and (k + dt.timedelta(hours=2)) in hour_min and hour_min[k + dt.timedelta(hours=2)] <= 1000:
                qual.append((k + dt.timedelta(hours=2), 3, '1000 m or less for 3 consecutive hours', hour_min[k + dt.timedelta(hours=2)]))
        for t, lv, rule, vis in qual:
            if t <= now_local <= _hold_until(t) and lv >= best:
                if lv > best:
                    why = []
                best = lv
                w = {'station': icao, 'at': f_local(t), 'level': ['none', 'watch', 'alert', 'severe'][lv], 'rule': rule, 'visM': vis, 'heldUntil': f_local(_hold_until(t))}
                if any(r.get('fromStore') and r.get('time_local') == t for r in rows):
                    w['fromStoredRecord'] = True
                why.append(w)
    return best, why


# ------------------------------------------------------------------------------------------------ the hourly store
_HOUR_RE = re.compile(r'"(\d{4}-\d{2}-\d{2}T\d{2})"\s*:\s*(\{[^{}]*\})')
_STN_RE = re.compile(r'"([A-Z0-9]{3,5})"\s*:\s*\{(?=\s*"\d{4}-\d{2}-\d{2}T\d{2}"|\s*\})')


def salvage_obs(raw):
    """the complete station hour records that can still be read from a damaged month file (a file cut short by a partial sync or a disk fault): every
    "YYYY-MM-DDTHH":{flat record} that is whole, under the station it follows. Returns {'stations': {icao: {hour key: record}}, 'n': records recovered}."""
    stations, n, cur = {}, 0, None
    marks = [(m.start(), 's', m.group(1)) for m in _STN_RE.finditer(raw or '')] + [(m.start(), 'h', m) for m in _HOUR_RE.finditer(raw or '')]
    for _, kind, v in sorted(marks, key=lambda x: x[0]):
        if kind == 's':
            cur = v
            stations.setdefault(cur, {})
        elif cur:
            try:
                rec = json.loads(v.group(2))
            except ValueError:
                continue
            if isinstance(rec, dict):
                stations[cur][v.group(1)] = rec
                n += 1
    return {'stations': {k: v for k, v in stations.items() if v}, 'n': n}


_HOUR_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}$')
_REC_NUM_KEYS = ('n', 'rain', 'shower', 'ts', 'fog', 'mist', 'haze', 'dust', 'vcts', 'vcsh', 'vv', 'minVis', 'minVisMin', 'gustKt', 'windKt')
_REC_MAX = {'n': 1000, 'minVis': 100000, 'gustKt': 500, 'windKt': 500}          # every other key is a minute of the hour, 0 to 59
_MINUTE_KEYS = ('rain', 'shower', 'ts', 'fog', 'mist', 'haze', 'dust', 'vcts', 'vcsh', 'vv', 'minVisMin')


def _rec_value_ok(k, v):
    """a number that is finite and in the range the store can hold: NaN, infinity and 1e308 pass JSON and a shape check and then stop the run in a time calculation (audit FILE-1)"""
    if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
        return False
    return 0 <= v <= (59 if k in _MINUTE_KEYS else _REC_MAX.get(k, 100000))


_REC_LIMIT = {k: (59 if k in _MINUTE_KEYS else _REC_MAX.get(k, 100000)) for k in _REC_NUM_KEYS}
_BIG_INT = 10 ** 300


def _rec_ok(rec):
    """all(_rec_value_ok(x, rec[x]) for x in _REC_NUM_KEYS if x in rec) in one pass over the keys the record has, which are three or four of the fifteen (25 Oct 2026, stress test T1: the station record of the
    whole season is checked at every run, 11,000 hour records at the end of it). The same answer: a number is an int or a float (a boolean, a string, None, a list are not), not NaN or infinity (a range test says
    no to both) and between 0 and the limit of its key. A record with an integer too large for a float is decided, as a whole, by the original check, which stops with an error for it, or has already said no
    before it reaches it, as it always did."""
    for x, v in rec.items():
        lim = _REC_LIMIT.get(x)
        if lim is None:
            continue
        t = type(v)
        if t is int:
            if v > _BIG_INT or v < -_BIG_INT:
                return all(_rec_value_ok(k, rec[k]) for k in _REC_NUM_KEYS if k in rec)
        elif t is not float:
            return False
        if not (0 <= v <= lim):
            return False
    return True


def _clean_stations(st):
    """(stations kept, number of entries dropped, hour records kept) from the stations block of a month file: a station that is not a table of hours, an hour that is not a record, a number that
    is a string are dropped. Valid JSON of the wrong shape is damage like any other, never a crash on every later run (audit V4, F15)."""
    out, dropped, n = {}, 0, 0
    for icao, hrs in st.items():
        if not isinstance(hrs, dict):
            dropped += 1
            continue
        good = {}
        for k, rec in hrs.items():
            if isinstance(k, str) and _HOUR_KEY_RE.match(k) and isinstance(rec, dict) and _rec_ok(rec):
                good[k] = rec
                n += 1
            else:
                dropped += 1
        out[icao] = good
    return out, dropped, n


def _merge_rec(a, b):
    """two hour records of one station and hour joined: the earliest first minute of each condition, the lowest visibility with its minute, the highest gust and wind, the larger report count"""
    out = dict(a)
    for k, v in b.items():
        if k not in out:
            out[k] = v
        elif k in ('n', 'gustKt', 'windKt'):
            out[k] = max(out[k], v)
        elif k == 'minVis':
            if v < out[k]:
                out['minVis'] = v
                out['minVisMin'] = b.get('minVisMin', out.get('minVisMin', 0))
        elif k == 'minVisMin':
            continue
        elif isinstance(v, (int, float)) and isinstance(out[k], (int, float)):
            out[k] = min(out[k], v)
    return out


class ObsStore:
    """data/weather/obs/YYYY-MM.json: {'stations': {icao: {'YYYY-MM-DDTHH': record}}}. Only adds."""
    FLAGS = (('rain', 'rain'), ('shower', 'shower'), ('thunder', 'ts'), ('fog', 'fog'), ('mist', 'mist'), ('haze', 'haze'), ('dust', 'dust'), ('vc_ts', 'vcts'), ('vc_sh', 'vcsh'))

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, 'obs')
        self.months = {}
        self.dirty = set()
        self.damaged = {}                     # month -> {'path','reason','recoveredHours','keptAs','writeBlocked'}
        self.vanished = set()                 # months with no station file although the ledger holds runs of the month
        self.save_errors = []
        self.purged_log = {}                  # month -> the notes of hour records removed because they were stamped after the engine clock (kept in the month file, audit N2)
        self.purged_keys = set()              # (month, icao, hour key) removed in this run: a save that takes in what another writer put on disk never brings them back
        self.purged_now = []                  # what this run removed, for the alert
        self.rejected_future = []             # rows refused by merge_rows because they were stamped in the future
        self.future_kept = []                 # records found ahead of the clock and left where they are (a clock that cannot be trusted)

    def _path(self, month):
        return os.path.join(self.dir, month + '.json')

    def _load(self, month):
        if month not in self.months:
            path = self._path(month)
            d, status = read_json_status(path)
            clean, n_clean = None, 0
            if status == 'ok' and isinstance(d, dict) and isinstance(d.get('stations'), dict):
                clean, dropped, n_clean = _clean_stations(d['stations'])
                pl = d.get('purged')
                self.purged_log[month] = [x for x in pl if isinstance(x, dict)][-200:] if isinstance(pl, list) else []
                if not dropped:
                    self.months[month] = d['stations']
                    return self.months[month]
                status = 'shape'                                  # valid JSON whose stations or hours are not in the shape the store reads (a string number, a list for a station): audit V4, F15
            if status == 'missing':
                self.months[month] = {}
                if self._ledger_month_has_runs(month):
                    self.vanished.add(month)                      # the ledger holds runs of this month but its station record is gone (audit F12)
            else:
                # a file that is there and does not parse (or has no stations block, or cannot be opened, or holds records of another shape) is NOT a missing file: salvage what can be read,
                # keep the file aside before anything is written over it, and say so (audit D4, WX-05)
                if status == 'shape':
                    self.months[month] = clean
                    rec = {'n': n_clean}
                else:
                    raw = read_text_raw(path) if status != 'unreadable' else ''
                    rec = salvage_obs(raw)
                    self.months[month] = rec['stations']
                self.damaged[month] = {'path': path, 'reason': ('cannot be opened' if status == 'unreadable' else 'does not parse' if status == 'damaged' else 'holds records in another shape' if status == 'shape' else 'has no stations block'),
                                       'recoveredHours': rec['n'], 'keptAs': None, 'writeBlocked': status == 'unreadable'}
                if status != 'unreadable':
                    self.dirty.add(month)                         # the salvaged month is written back, so the next run finds a good file
        return self.months[month]

    def _ledger_month_has_runs(self, month):
        try:
            p = os.path.join(os.path.dirname(self.dir), 'ledger', month + '.jsonl')
            return os.path.getsize(p) > 0
        except OSError:
            return False

    def problems(self):
        """plain alerts about damaged month files met or repaired in this run"""
        out = []
        for m, d in sorted(self.damaged.items()):
            if d.get('writeBlocked'):
                out.append('obs/%s.json cannot be opened: no station hour of that month is written until it can be read' % m)
            else:
                out.append('obs/%s.json %s: %d station hour records were recovered from it%s; the hours after the damage are missing from the record until the stations are read again' % (
                    m, d['reason'], d['recoveredHours'], (', the damaged file is kept as ' + d['keptAs']) if d.get('keptAs') else ' (the damaged file will be kept aside when the record is written)'))
        for m in sorted(self.vanished):
            out.append('obs/%s.json is missing although the ledger holds runs of that month: the station hours before this run are not in the record (a deleted file?); the hours of this run\'s answer are added' % m)
        out.extend(self.save_errors)
        return out

    def coverage(self):
        """{icao: first hour key on record, 'YYYY-MM-DDTHH'}: where the engine's own station record starts"""
        out = {}
        try:
            names = [n[:-5] for n in os.listdir(self.dir) if n.endswith('.json')]
        except OSError:
            names = []
        for m in sorted(set(names) | set(self.months)):
            for icao, hrs in self._load(m).items():
                if hrs:
                    k = min(hrs)
                    if icao not in out or k < out[icao]:
                        out[icao] = k
        return out

    def month_keys(self, start, end):
        out, d = [], dt.datetime(start.year, start.month, 1)
        while d <= end:
            out.append(d.strftime('%Y-%m'))
            d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return out

    def get(self, icao, hour_key):
        return self._load(hour_key[:7]).get(icao, {}).get(hour_key)

    def merge_rows(self, rows_by_station, now=None):
        """add the reports; returns the number of hour records created or changed. With now, a report stamped more than FUTURE_TOL_MIN minutes after it is not added (audit N2): it is counted in
        rejected_future, and a record that nothing real stands behind can never open a fog or rain event when the clock reaches its hour."""
        changed = 0
        lim = (now + dt.timedelta(minutes=FUTURE_TOL_MIN)) if now is not None else None
        for icao, rows in rows_by_station.items():
            per_hour = {}
            for r in rows:
                if r['time_local'] is None:
                    continue
                if lim is not None and r['time_local'] > lim:
                    self.rejected_future.append({'station': icao, 'at': f_local(r['time_local']), 'kind': 'METAR'})
                    continue
                per_hour.setdefault(_hour_key(r['time_local']), []).append(r)
            for hk, rs in per_hour.items():
                month = self._load(hk[:7])
                rec = month.setdefault(icao, {}).get(hk)
                new = dict(rec) if rec else {}
                new['n'] = max(new.get('n', 0), len(rs))
                for r in sorted(rs, key=lambda x: x['time_local']):
                    try:
                        mnt = r['time_local'].minute
                        fl = r['flags']
                        for src, dst in self.FLAGS:
                            if fl.get(src) and (dst not in new or mnt < new[dst]):
                                new[dst] = mnt
                        if r['vv_ft'] is not None and ('vv' not in new or mnt < new['vv']):
                            new['vv'] = mnt
                        v = r['vis_m']
                        if v is not None and ('minVis' not in new or v < new['minVis']):
                            new['minVis'] = v
                            new['minVisMin'] = mnt
                        if r['gust_kt'] is not None and r['gust_kt'] > new.get('gustKt', -1):
                            new['gustKt'] = r['gust_kt']
                        if r['wind_kt'] is not None and r['wind_kt'] > new.get('windKt', -1):
                            new['windKt'] = r['wind_kt']
                    except (TypeError, KeyError, AttributeError):
                        continue                                  # a report whose fields do not compare is skipped, the rest of the hour is kept (audit F3, V4)
                if new != rec:
                    month[icao][hk] = new
                    self.dirty.add(hk[:7])
                    changed += 1
        return changed

    def purge_future(self, now, remove=True):
        """Remove what an earlier run stored from a report stamped after the engine clock (audit N2): a whole station hour that starts later than now plus FUTURE_TOL_MIN minutes, and in the hour
        that holds that moment the first minutes of conditions that lie later than it. Only the months from the engine's own month on can hold such a record, so older months are never read.
        The store only adds, so this is the one removal it makes: each one is noted in the month file (purged) and named in the run's alert. Returns the notes of this call.
        remove=False only looks (a clock that cannot be trusted would call real hours future): the records found are listed in future_kept and nothing is changed."""
        lim = now + dt.timedelta(minutes=FUTURE_TOL_MIN)
        lim_hour = floor_hour(lim).strftime('%Y-%m-%dT%H')
        lim_min = lim.minute
        first = now.strftime('%Y-%m')
        try:
            names = [n[:-5] for n in os.listdir(self.dir) if n.endswith('.json') and not n.startswith('.')]
        except OSError:
            names = []
        notes = []
        for month in sorted(m for m in set(names) | set(self.months) if m >= first):
            stn = self._load(month)
            for icao in list(stn):
                hrs = stn[icao]
                for hk in sorted(hrs):
                    if hk > lim_hour:
                        if remove:
                            del hrs[hk]
                            self.purged_keys.add((month, icao, hk))
                        notes.append({'at': f_local(now), 'station': icao, 'hour': hk, 'what': 'record'})
                    elif hk == lim_hour:
                        rec = hrs[hk]
                        gone = [k for k in _MINUTE_KEYS if k != 'minVisMin' and k in rec and rec[k] > lim_min]
                        if 'minVis' in rec and rec.get('minVisMin', 0) > lim_min:
                            gone += ['minVis', 'minVisMin']
                        if gone:
                            if remove:
                                for k in gone:
                                    rec.pop(k, None)
                                self.purged_keys.add((month, icao, hk))
                            notes.append({'at': f_local(now), 'station': icao, 'hour': hk, 'what': 'fields ' + ', '.join(gone)})
            if remove and any(n['hour'].startswith(month) for n in notes):
                self.dirty.add(month)
                log = self.purged_log.setdefault(month, [])
                log.extend(n for n in notes if n['hour'].startswith(month))
                del log[:-200]
        if remove:
            self.purged_now.extend(notes)
        else:
            self.future_kept = notes
        return notes

    def future_kept_alert(self):
        """the sentence when records stamped after the engine clock were found and left, because the clock could not be trusted, or None"""
        if not self.future_kept:
            return None
        shown = '; '.join('%s %s (%s)' % (n['station'], n['hour'].replace('T', ' ') + ':00', n['what']) for n in self.future_kept[:6])
        return ('%d station hour record(s) are stamped after the engine clock: %s. They were NOT removed, because the clock could not be checked against the servers (or the clock given is behind the laptop '
                'clock); check the laptop clock. They cannot be a report of an hour that has not happened' % (len(self.future_kept), shown))

    def purge_alert(self):
        """the sentence for the health alerts about records removed in this run, or None"""
        if not self.purged_now:
            return None
        shown = '; '.join('%s %s (%s)' % (n['station'], n['hour'].replace('T', ' ') + ':00', n['what']) for n in self.purged_now[:6])
        return ('%d station hour record(s) in the station record were stamped after the engine clock and were removed, because no station could have reported them yet: %s%s. '
                'Each removal is noted in the month file under purged' % (len(self.purged_now), shown, ' and more' if len(self.purged_now) > 6 else ''))

    def _absorb_disk(self, month):
        """take in the hours another writer put in this month's file since this store read it (audit N3): the file is read again under the lock and merged into what is in memory, hour by
        hour, so a save never replaces another writer's hours. A record this run purged is not taken back. Returns how many hours were added or changed."""
        d, status = read_json_status(self._path(month))
        if status != 'ok' or not isinstance(d, dict) or not isinstance(d.get('stations'), dict):
            return 0
        clean, _, _ = _clean_stations(d['stations'])
        mem = self.months[month]
        n = 0
        for icao, hrs in clean.items():
            m = mem.setdefault(icao, {})
            for hk, drec in hrs.items():
                if (month, icao, hk) in self.purged_keys:
                    continue
                cur = m.get(hk)
                if cur is None:
                    m[hk] = drec
                    n += 1
                else:
                    merged = _merge_rec(cur, drec)
                    if merged != cur:
                        m[hk] = merged
                        n += 1
        return n

    def save(self):
        """writes the months that changed. A damaged month is copied aside (name.corrupt-stamp) first; if the copy cannot be made the month is not written. A month that cannot
        be written is kept dirty and named in problems(): the run goes on, the record is not lost silently. The write is made under the cross process lock of the month file, after the file
        has been read again and merged into memory (audit N3), so two writers never replace one another's hours."""
        self.save_errors = []
        failed, saved = set(), 0
        for month in sorted(self.dirty):
            dm = self.damaged.get(month)
            try:
                if dm:
                    if dm.get('writeBlocked'):
                        failed.add(month)
                        continue
                    if not dm.get('keptAs'):
                        dm['keptAs'] = os.path.basename(quarantine_copy(self._path(month)))
                with file_lock(self._path(month)):
                    if not dm:
                        self._absorb_disk(month)
                    doc = {'schema': 1, 'status': 'Reported (METAR and SPECI by the hour; first minute of each condition; added to, never rewritten)', 'stations': self.months[month]}
                    if self.purged_log.get(month):
                        doc['purged'] = self.purged_log[month][-200:]
                    atomic_write_json(self._path(month), doc)
                saved += 1
            except OSError as e:
                failed.add(month)
                self.save_errors.append('obs/%s.json could not be written: %s (the station hours of this run are kept in memory only)' % (month, str(e)[:100]))
        self.dirty = failed
        return saved

    def hours(self, icao, start, end):
        """[(hour start datetime, record)] for the clock hours from start to end inclusive. The key of an hour is written with a format and the month table of a station is looked up when the month changes, not
        for every hour (25 Oct 2026, stress test T1: the season record asks for every hour of every day of the season at every run, 12,000 calls of strftime and of the month lookup a pass)."""
        out = []
        t = floor_hour(start)
        month, table = None, None
        hour = dt.timedelta(hours=1)
        while t <= end:
            hk = '%04d-%02d-%02dT%02d' % (t.year, t.month, t.day, t.hour)
            if hk[:7] != month:
                month = hk[:7]
                table = self._load(month).get(icao, {})
            rec = table.get(hk)
            if rec:
                out.append((t, rec))
            t += hour
        return out


def _rec_flag(rec, hazard):
    """minute of the first condition for a hazard in an hour record, or None"""
    if hazard == 'rain':
        c = [rec[k] for k in ('rain', 'shower', 'ts') if k in rec]
        return min(c) if c else None
    if hazard == 'fog':
        c = [rec[k] for k in ('fog', 'vv') if k in rec]
        if 'minVis' in rec and rec['minVis'] <= FOG_VIS_LE:
            c.append(rec.get('minVisMin', 0))
        return min(c) if c else None
    if hazard == 'dust':
        return rec.get('dust')
    return None


def station_evidence(store, stations, start, end):
    """events evidence from the stations between start and end: [{'hazard','kind':'station','station','time','detail'}], one per station hour"""
    out = []
    for m in stations:
        for t, rec in store.hours(m['icao'], start, end):
            for hz in ('rain', 'fog', 'dust'):
                mnt = _rec_flag(rec, hz)
                if mnt is None:
                    continue
                when = t + dt.timedelta(minutes=mnt)
                if when < start or when > end:
                    continue
                det = {k: rec[k] for k in ('minVis', 'gustKt') if k in rec}
                out.append({'hazard': hz, 'kind': 'station', 'station': m['icao'], 'time': when, 'key': '%s|%s|%s' % (hz, m['icao'], t.strftime('%Y-%m-%dT%H')), 'detail': det})
    return out


def covered_hours(store, stations, t0, t1):
    """True when every whole clock hour between t0 and t1 holds a record of at least one station (the stations reported through it): the record shows a quiet stretch
    and not a gap in the record. Used before evidence that arrives late is allowed to end an event (audit logic D5)."""
    h = floor_hour(t0) + dt.timedelta(hours=1)
    end = floor_hour(t1)
    while h < end:
        hk = h.strftime('%Y-%m-%dT%H')
        if not any(store.get(m['icao'], hk) for m in stations):
            return False
        h += dt.timedelta(hours=1)
    return True


def station_hours_in_span(store, stations, hazard, start, end):
    """{icao: hours} number of clock hours with the hazard reported between start and end"""
    out = {}
    for m in stations:
        n = 0
        for t, rec in store.hours(m['icao'], start, end):
            if _rec_flag(rec, hazard) is not None:
                n += 1
        if n:
            out[m['icao']] = n
    return out
