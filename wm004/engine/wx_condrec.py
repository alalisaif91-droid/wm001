# -*- coding: utf-8 -*-
"""The recording side of the conditions (24 Oct 2026): what the stations observed each day, the season record of temperature, humidity, wind, gust and pressure at the eight points, and the
score of the forecast against the station reports. Nothing recorded is rewritten: the station day record only widens (a maximum can rise, a minimum can fall), a day of the season record
is fetched once and cached, and a record that exists and cannot be read is kept aside before anything is written over it.

Files in data/weather: obs_daily.json (the station day record), season_cond_cache.json (the season days of the eight points, from the archive or from the analysis hours of a run).
The season rows themselves live in season.json (cond and obsExt of each row, condColumns at the top). Status: stations Reported; the points Model analysis (Recomputed), not a gauge and not a
station; the verification Recomputed. Standard library only."""
import os, datetime as dt
from wx_util import read_json_status, quarantine_copy, atomic_write_json, f_local, parse_local, POINTS, r1, med
import wx_cond as C
import wx_season as S

OBS_DAILY = 'obs_daily.json'
CACHE = 'season_cond_cache.json'
MAXK = ('tMax', 'rhMax', 'wsMax', 'peak', 'gustMax', 'pMax')
MINK = ('tMin', 'rhMin', 'pMin', 'visMin')
COLUMNS = ['tMin', 'tMean', 'tMax', 'rhMin', 'rhMean', 'rhMax', 'wsMin', 'wsMean', 'wsMax', 'gustMin', 'gustMean', 'gustMax', 'presMin', 'presMean', 'presMax']
COLUMN_UNITS = {'t': 'deg C', 'rh': '%', 'ws': 'km/h', 'gust': 'km/h', 'pres': 'hPa'}
SOURCES = {'run': 'models, analysis hours of a run (best_match)', 'archive': 'archive reanalysis (Open-Meteo, ERA5 based; the last days are provisional)', 'partial': 'models, analysis hours so far today (best_match)'}
MIN_HOURS = 22               # a day of the season record needs this many of its 24 hours for a quantity
VERIFY_LEADS = {'24h': 24.0, '6h': 6.0}
VERIFY_MIN_DAYS = 30
VERIFY_OBS_HOURS = 18        # a station day counts for the score when the station reported in at least this many hours of it


def _daterange(a, b):
    d = dt.datetime.strptime(a, '%Y-%m-%d')
    e = dt.datetime.strptime(b, '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += dt.timedelta(days=1)


# ------------------------------------------------------------------------------------------------ the station day record
def rows_extremes(rows):
    """the observed extremes of a set of METAR rows of one day: temperature, relative humidity, mean wind, peak (the higher of the mean and a reported gust), reported gust, QNH, visibility, and the
    hours of the day with a report as a bit mask. A quantity with no value in the rows is absent, never zero."""
    vals = [(r, C.row_values(r)) for r in rows if r.get('time_local') is not None]
    out = {}

    def pick(k):
        return [v[k] for _, v in vals if v[k] is not None]
    for key, k, fn in (('tMax', 'temp', max), ('tMin', 'temp', min), ('rhMax', 'rh', max), ('rhMin', 'rh', min), ('wsMax', 'kmh', max), ('peak', 'peakKmh', max), ('gustMax', 'gustKmh', max), ('pMax', 'qnh', max),
                       ('pMin', 'qnh', min), ('visMin', 'vis', min)):
        x = pick(k)
        if x:
            out[key] = fn(x)
    mask = 0
    for r, _ in vals:
        mask |= 1 << r['time_local'].hour
    out['hours'] = mask
    return out


def merge_day(a, b):
    """two day records of one station joined: a maximum can only rise, a minimum can only fall, the hours with a report are the union"""
    out = dict(a)
    for k, v in b.items():
        if v is None:
            continue
        if k == 'hours':
            out['hours'] = int(out.get('hours', 0)) | int(v)
        elif k in MAXK:
            out[k] = v if k not in out or v > out[k] else out[k]
        elif k in MINK:
            out[k] = v if k not in out or v < out[k] else out[k]
        elif k not in out:
            out[k] = v
    return out


def obs_hours(rec):
    return bin(int(rec.get('hours', 0))).count('1') if isinstance(rec, dict) else 0


def read_obs_daily(data_dir):
    """(record, status): the station day record as it is on disk. status ok, missing, damaged or unreadable; the record is a usable empty one unless ok."""
    d, status = read_json_status(os.path.join(data_dir, OBS_DAILY))
    if status == 'ok' and isinstance(d, dict) and isinstance(d.get('stations'), dict) and all(isinstance(v, dict) for v in d['stations'].values()):
        return d, 'ok'
    if status == 'ok':
        status = 'damaged'
    return {'schema': 1, 'stations': {}}, status


def update_obs_daily(data_dir, rows_by_station, now, write=True):
    """merge the reports of this run (the last 30 hours) into the station day record and write it. Returns (record, problems). A damaged file is copied aside before it is written over (the
    copy stays until a person has dealt with it, and the engine says so); a file that cannot be opened is not written and is said."""
    problems = []
    rec, status = read_obs_daily(data_dir)
    path = os.path.join(data_dir, OBS_DAILY)
    block = status in ('unreadable',)
    if status in ('damaged', 'unreadable'):
        problems.append('obs_daily.json %s: the station day record is started again%s (owner: the weather desk)' % ('cannot be opened' if status == 'unreadable' else 'does not parse',
                                                                                                             '' if status == 'unreadable' else ' and the damaged file is kept aside'))
    for icao, rows in rows_by_station.items():
        per_day = {}
        for r in rows:
            t = r.get('time_local')
            if t is None or t > now + dt.timedelta(minutes=10):
                continue
            per_day.setdefault(t.strftime('%Y-%m-%d'), []).append(r)
        days = rec['stations'].setdefault(icao, {})
        for day, rs in per_day.items():
            new = rows_extremes(rs)
            days[day] = merge_day(days.get(day) or {}, new)
    rec['schema'] = 1
    rec['updated'] = f_local(now)
    rec['note'] = ('what the aviation reports of each station gave on each day (Reported): extremes of temperature, relative humidity (Magnus, Recomputed), mean wind (km/h), peak wind (the higher of the mean and a '
                   'reported gust), reported gust, QNH, visibility, and the hours of the day with a report as a bit mask (bit 0 is 00:00). A day only widens as more reports arrive. The stations are airports.')
    if write and not block:
        try:
            if status == 'damaged':
                quarantine_copy(path)
            atomic_write_json(path, rec)
        except OSError as e:
            problems.append('obs_daily.json could not be written (%s)' % str(e)[:100])
    return rec, problems


# ------------------------------------------------------------------------------------------------ the season record of the points
def stat15(t, rh, ws, g, p, minimum=MIN_HOURS):
    """[min, mean, max] of temperature, relative humidity, wind speed, gust and pressure from lists of hourly values; a quantity with fewer than `minimum` values is three Nones. Never a zero for a gap."""
    out = []
    for vals in (t, rh, ws, g, p):
        v = [x for x in (vals or []) if x is not None]
        if len(v) >= minimum:
            out += [r1(min(v)), r1(sum(v) / len(v)), r1(max(v))]
        else:
            out += [None, None, None]
    return out


def day_from_arrays(times, arr, day, minimum=MIN_HOURS, upto=None):
    """the 15 numbers of one day of one point from the arrays of the best_match answer (analysis hours). upto: the last label that counts (today so far)."""
    idx = [i for i, t in enumerate(times) if t[:10] == day and (upto is None or t <= upto)]

    def get(key):
        a = arr.get(key)
        return [a[i] for i in idx if a and i < len(a)] if a else []
    return stat15(get('temp'), get('rh'), get('ws'), get('gust'), get('pres'), minimum)


def archive_days(fetcher, idx, start, end):
    """the days start to end of one point from the archive: {day: 15 numbers} for the days that have all 24 hours of a quantity, plus the call info. None when the call failed."""
    lat, lon = POINTS[idx][1], POINTS[idx][2]
    data, info = fetcher.get_json('om_carchive_' + S.slug(POINTS[idx][0]), C.archive_url(lat, lon, start, end))
    if data is None:
        return None, info
    times, h, err = C._axis(data, '0000-00-00T00:00')
    if err:
        return None, dict(info, error=err)
    arr = {}
    for key, var in (('temp', 'temperature_2m'), ('rh', 'relative_humidity_2m'), ('ws', 'wind_speed_10m'), ('gust', 'wind_gusts_10m'), ('pres', 'pressure_msl')):
        s = h.get(var)
        arr[key] = [C.clean_value(key, x) for x in s] if isinstance(s, list) else None
    out = {}
    for day in sorted({t[:10] for t in times}):
        if start <= day <= end:
            out[day] = day_from_arrays(times, arr, day)
    return out, info


def season_update(data_dir, now, fetcher, pts, write=True, can_fetch=True):
    """Bring the season record of the eight points up to date. A day is cached once: the days before today that this run holds completely (the two past days of the best_match answer) come from the
    run's analysis hours; older days still missing come from the archive in one call per point; today is shown so far and never cached. Returns {'columns', 'days': {day: [15 numbers or None per
    point]}, 'src': {day: text}, 'problems': [...]}. The cache is kept aside before it is rebuilt when it is damaged."""
    cache_path = os.path.join(data_dir, CACHE)
    cache, st = read_json_status(cache_path)
    problems = []
    damaged = False
    if st == 'ok' and isinstance(cache, dict) and isinstance(cache.get('days'), dict):
        pass
    else:
        if st != 'missing':
            damaged = True
            problems.append('season_cond_cache.json %s: the cached days are fetched again and the damaged file is kept aside when the cache is written' % ('cannot be opened' if st == 'unreadable' else 'does not parse'))
        cache = {'schema': 1, 'days': {}}
    cdays = cache['days']
    today = now.strftime('%Y-%m-%d')
    days = list(_daterange(S.SEASON_START, today))
    n = len(POINTS)
    dirty = False
    # 1. the days this run holds completely
    for p in pts:
        if not p.get('ok'):
            continue
        for d in days:
            if d >= today:
                continue
            have = cdays.setdefault(d, {})
            if str(p['i']) in have:
                continue
            idx = [i for i, t in enumerate(p['_times']) if t[:10] == d]
            if len(idx) == 24:
                s = day_from_arrays(p['_times'], p['_arr'], d)
                if any(x is not None for x in s):
                    have[str(p['i'])] = {'s': s, 'src': SOURCES['run']}
                    dirty = True
    # 2. the days still missing: the archive, once
    if can_fetch:
        for i in range(n):
            need = [d for d in days if d < today and str(i) not in (cdays.get(d) or {})]
            if not need:
                continue
            got, info = archive_days(fetcher, i, need[0], need[-1])
            if got is None:
                if info.get('error') != 'no fixture':
                    problems.append('archive conditions %s: %s' % (POINTS[i][0].split(' (')[0], info.get('error') or info.get('status')))
                continue
            for d, s in got.items():
                if d in need and any(x is not None for x in s):
                    cdays.setdefault(d, {})[str(i)] = {'s': s, 'src': SOURCES['archive']}
                    dirty = True
    if write and (dirty or damaged):
        cache['updated'] = f_local(now)
        try:
            if damaged and st != 'unreadable':
                quarantine_copy(cache_path)
            if not (damaged and st == 'unreadable'):
                atomic_write_json(cache_path, cache)
        except OSError as e:
            problems.append('season_cond_cache.json could not be written: %s' % str(e)[:100])
    out_days, src = {}, {}
    for d in days:
        if d == today:
            row = [None] * n
            for p in pts:
                if p.get('ok'):
                    s = day_from_arrays(p['_times'], p['_arr'], d, minimum=3, upto=now.strftime('%Y-%m-%dT%H:00'))
                    row[p['i']] = s if any(x is not None for x in s) else None
            out_days[d] = row
            src[d] = SOURCES['partial']
        else:
            have = cdays.get(d) or {}
            out_days[d] = [have[str(i)]['s'] if str(i) in have else None for i in range(n)]
            srcs = sorted({have[k]['src'] for k in have})
            src[d] = '; '.join(srcs) if srcs else None
    return {'columns': COLUMNS, 'days': out_days, 'src': src, 'problems': problems}


def season_extra(season, obs_daily):
    """the hooks of wx_season.build: the top level keys and a function that gives the conditions of one day"""
    def row(day):
        out = {'cond': season['days'].get(day), 'condSrc': season['src'].get(day)}
        ext = {}
        for icao, days in (obs_daily.get('stations') or {}).items():
            rec = days.get(day)
            if rec:
                ext[icao] = {k: v for k, v in rec.items() if k != 'hours'}
                ext[icao]['obsH'] = obs_hours(rec)
        out['obsExt'] = ext
        return out
    top = {'condColumns': {'order': COLUMNS, 'units': COLUMN_UNITS, 'note': 'per day and per point: minimum, mean and maximum of temperature (deg C), relative humidity (%), wind speed (km/h), gust (km/h) and pressure (hPa); '
                                                                      'Model analysis (Recomputed): the analysis hours of the best_match model or the archive reanalysis, not a station and not a gauge; null when fewer than 22 of the 24 hours are there'},
           'obsExtNote': 'per day and station (Reported, aviation reports): tMax, tMin (deg C), rhMin, rhMax (%, worked out from the whole degrees), wsMax (mean wind, km/h), peak (the higher of the mean and a reported gust, km/h), '
                         'gustMax (reported gusts only), pMin, pMax (QNH, hPa), visMin (m), obsH (hours of the day with a report); the stations are airports'}
    return {'row': row, 'top': top}


# ------------------------------------------------------------------------------------------------ the score of the forecast against the stations
def nearest_point(station):
    """the index of the rain point nearest a station"""
    d = station.get('distKm') or {}
    return min(range(len(POINTS)), key=lambda i: d.get(POINTS[i][0], 1e9))


VARS = [('temperatureMax', 1, 'tMax', 'deg C', 'the highest hourly temperature of the day on best_match against the highest temperature reported'),
        ('temperatureMin', 0, 'tMin', 'deg C', 'the lowest hourly temperature of the day on best_match against the lowest temperature reported'),
        ('humidityMin', 2, 'rhMin', '%', 'the lowest hourly relative humidity of the day on best_match against the lowest worked out from the reports'),
        ('humidityMax', 3, 'rhMax', '%', 'the highest hourly relative humidity of the day on best_match against the highest worked out from the reports'),
        ('windMax', 4, 'wsMax', 'km/h', 'the highest hourly mean wind of the day on best_match against the highest mean wind reported'),
        ('gustPeak', 5, 'peak', 'km/h', 'the highest model gust of the day on best_match against the METAR peak: the higher of the mean and a reported gust (a model gust is not a METAR gust)')]


def _stats(pairs):
    """pairs [(forecast, observed)] to {'n', 'bias' (forecast minus observed), 'mae'}; None when there are no pairs"""
    if not pairs:
        return {'n': 0, 'bias': None, 'mae': None}
    d = [f - o for f, o in pairs]
    return {'n': len(d), 'bias': r1(sum(d) / len(d)), 'mae': r1(sum(abs(x) for x in d) / len(d))}


def conditions_verification(ledger_runs, obs_daily, stations, today, pick_run, season_start):
    """Forecast against observed for gust, wind, temperature and humidity (Recomputed). For every past day on which a station reported in 18 or more hours, the ledger run about 24 hours (and about
    6 hours) before the start of the day gives the forecast of the day at the rain point nearest the station (the daily values the run recorded: lowest and highest temperature, lowest and highest
    humidity, highest wind, highest gust); the station day record gives what happened. bias is forecast minus observed. The stations are airports, not the rain points: part of every difference is the
    distance between them (given per station). Returns the block for verification.json."""
    rec_st = (obs_daily or {}).get('stations') or {}
    by_lead = {}
    for lead, hrs in VERIFY_LEADS.items():
        pairs = {name: [] for name, *_ in VARS}
        by_stn = {}
        days_scored = set()
        for st in stations:
            icao = st['icao']
            pi = nearest_point(st)
            for day, rec in sorted((rec_st.get(icao) or {}).items()):
                if not (season_start <= day < today) or obs_hours(rec) < VERIFY_OBS_HOURS:
                    continue
                d0 = dt.datetime.strptime(day, '%Y-%m-%d')
                pr = pick_run(ledger_runs, d0 - dt.timedelta(hours=hrs))
                if not pr:
                    continue
                _, t, run = pr
                off = (d0.date() - t.date()).days
                pts = ((run.get('cond') or {}).get('pts') if isinstance(run.get('cond'), dict) else None) or []
                prow = next((p for p in pts if isinstance(p, dict) and p.get('i') == pi), None)
                dl = prow.get('d') if prow else None
                if not isinstance(dl, list) or not (0 <= off < len(dl)) or not isinstance(dl[off], list):
                    continue
                got = False
                for name, k, key, unit, _ in VARS:
                    f = dl[off][k] if k < len(dl[off]) else None
                    o = rec.get(key)
                    if isinstance(f, (int, float)) and not isinstance(f, bool) and isinstance(o, (int, float)):
                        pairs[name].append((f, o))
                        by_stn.setdefault(icao, {}).setdefault(name, []).append((f, o))
                        got = True
                if got:
                    days_scored.add(day)
        by_lead[lead] = {'daysScored': len(days_scored), 'variables': {name: dict(_stats(pairs[name]), unit=unit, what=what) for name, _, _, unit, what in VARS},
                         'byStation': {icao: {name: _stats(p) for name, p in v.items()} for icao, v in by_stn.items()}}
    n_days = max([v['daysScored'] for v in by_lead.values()] + [0])
    statements = ['The stations are airports, not the rain points: each forecast is the value of the rain point nearest the station (%s), so part of every difference is the distance and the height between them.'
                  % ', '.join('%s %s km from %s' % (st['icao'], (st.get('distKm') or {}).get(POINTS[nearest_point(st)][0]), POINTS[nearest_point(st)][0].split(' (')[0]) for st in stations),
                  'A model gust is not a METAR gust: METAR carries a gust in 0.1 to 0.7 percent of reports, so the observed value is the peak (the higher of the mean and a reported gust) and the model reads higher by design.',
                  'Bias is forecast minus observed; the scores are Recomputed from the ledger and the station day record and are not a claim of skill.']
    if n_days < VERIFY_MIN_DAYS:
        statements.append('%d days are scored so far; fewer than %d days is too few to judge how good the forecast is. The counts are a record, not a skill score.' % (n_days, VERIFY_MIN_DAYS))
    return {'status': 'Recomputed from the ledger (Model forecast, Proposed) against the aviation reports (Reported)', 'method': {'leads': {k: 'run within 4 h of %g h before the start of the day' % v for k, v in VERIFY_LEADS.items()},
                                                                                                                              'station': 'a station day with %d or more hours of reports' % VERIFY_OBS_HOURS},
            'leads': by_lead, 'stations': {st['icao']: {'nearestPoint': POINTS[nearest_point(st)][0], 'distKm': (st.get('distKm') or {}).get(POINTS[nearest_point(st)][0])} for st in stations}, 'statements': statements}
