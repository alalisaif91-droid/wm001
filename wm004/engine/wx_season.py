# -*- coding: utf-8 -*-
"""The season record of the WM004 weather engine: data/weather/season.json, from 1 Oct 2026.

One row per day: the median and the highest model analysis rainfall at each of the eight points (Model analysis, Recomputed; not a rain gauge),
whether each station reported rain, the hours of fog each station reported, the highest NCM colour in force that day for rain, fog and dust, and the
event ids of the day. Days older than the forecast window are read once from the Open-Meteo archive (ERA5 reanalysis) and, for the few days the archive
does not have yet, from the models' own past hours; both are cached in data/weather/season_cache.json so a day is fetched once.
The season total per point, the fog hours and fog days per station and month, and a comparison with last season taken from calibration.json close it."""
import os, datetime as dt, urllib.parse
from wx_util import read_json, read_json_status, quarantine_copy, atomic_write_json, f_local, med, r1, POINTS, TZ_NAME, floor_hour
import wx_models as M
import ncm_log as N

SEASON_START = '2026-10-01'
LAST_SEASON = ('2025-10-01', '2026-03-31')          # the season the calibration compares with (Oct 2025 to Mar 2026)


def _d(x):
    """a dict, or an empty one: a calibration.json whose shape changed gives gaps, never a crash (audit WX-11)"""
    return x if isinstance(x, dict) else {}
ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
RANKC = N.RANK


def slug(name):
    s = ''.join(c.lower() if c.isalnum() else '_' for c in name.split('(')[0].split(',')[0]).strip('_')
    while '__' in s:
        s = s.replace('__', '_')
    return s


def point_slugs():
    return [slug(p[0]) for p in POINTS]


def _daterange(a, b):
    d = dt.datetime.strptime(a, '%Y-%m-%d')
    e = dt.datetime.strptime(b, '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += dt.timedelta(days=1)


def _fetch_archive(fetcher, idx, start, end):
    lat, lon = POINTS[idx][1], POINTS[idx][2]
    q = urllib.parse.urlencode({'latitude': lat, 'longitude': lon, 'start_date': start, 'end_date': (dt.datetime.strptime(end, '%Y-%m-%d') + dt.timedelta(days=1)).strftime('%Y-%m-%d'),
                                'hourly': 'precipitation', 'models': 'era5', 'timezone': TZ_NAME})
    data, info = fetcher.get_json('om_archive_' + slug(POINTS[idx][0]), ARCHIVE_URL + '?' + q)
    if data is None:
        return None, info
    ok_z, zt, _ = M.zone_check(data)
    if not ok_z:
        return None, dict(info, error=zt)                          # days cut on another time axis would be the wrong days (audit F2)
    times, ms = M.split_models(data, ['era5'])
    r = ms.get('era5')
    if not r or 'precipitation' not in r:
        info = dict(info, error='archive answer has no precipitation')
        return None, info
    p = [None if v is None else max(0.0, float(v)) for v in r['precipitation']]
    days = M.day_totals(times, {'era5': p}, list(_daterange(start, end)))
    return {d: v for d, v in days.items() if v['n'] == 1}, info


def _fetch_past_models(fetcher, idx, days_back):
    lat, lon = POINTS[idx][1], POINTS[idx][2]
    q = urllib.parse.urlencode({'latitude': lat, 'longitude': lon, 'hourly': 'precipitation', 'models': ','.join(M.ALL_MODELS), 'past_days': min(days_back, 14),
                                'forecast_days': 1, 'timezone': TZ_NAME})
    data, info = fetcher.get_json('om_past_' + slug(POINTS[idx][0]), FORECAST_URL + '?' + q)
    if data is None:
        return None, info
    ok_z, zt, _ = M.zone_check(data)
    if not ok_z:
        return None, dict(info, error=zt)
    times, ms = M.split_models(data, M.ALL_MODELS)
    pm = {}
    for m, v in ms.items():
        if v and 'precipitation' in v:
            pm[m] = [None if x is None else max(0.0, float(x)) for x in v['precipitation']]
    days = sorted({t[:10] for t in times})
    return M.day_totals(times, pm, days), info


def _row_stations(store, stations, day):
    d0 = dt.datetime.strptime(day, '%Y-%m-%d')
    out = {}
    for m in stations:
        hrs = store.hours(m['icao'], d0, d0 + dt.timedelta(hours=23))
        if not hrs:
            out[m['icao']] = {'rain': None, 'fogH': None, 'obsH': 0}
            continue
        rain = any(any(k in rec for k in ('rain', 'shower', 'ts')) for _, rec in hrs)
        fog = sum(1 for _, rec in hrs if ('fog' in rec or 'vv' in rec or rec.get('minVis', 99999) < 1000))
        out[m['icao']] = {'rain': rain, 'fogH': fog, 'obsH': len(hrs)}
    return out


def _row_ncm(entries, day):
    d0 = dt.datetime.strptime(day, '%Y-%m-%d')
    d1 = d0 + dt.timedelta(days=1)
    best = {'rain': 'none', 'fog': 'none', 'dust': 'none'}
    for e in entries:
        if e['level'] == 'none' or e.get('rakNamed') == 'no':
            continue
        s, en = N.window(e)
        if s < d1 and en >= d0:
            for h in ('rain', 'fog', 'dust'):
                if h in e['hazard'] and RANKC[e['level']] > RANKC[best[h]]:
                    best[h] = e['level']
    return best


def _fog_months(store, stations, start_day, end_day):
    out = {}
    for m in stations:
        months = {}
        for day in _daterange(start_day, end_day):
            d0 = dt.datetime.strptime(day, '%Y-%m-%d')
            hrs = store.hours(m['icao'], d0, d0 + dt.timedelta(hours=23))
            mk = day[:7]
            row = months.setdefault(mk, {'fogHours': 0, 'fogDays': 0, 'obsHours': 0})
            fh = sum(1 for _, rec in hrs if ('fog' in rec or 'vv' in rec or rec.get('minVis', 99999) < 1000))
            row['fogHours'] += fh
            row['fogDays'] += 1 if fh else 0
            row['obsHours'] += len(hrs)
        out[m['icao']] = months
    return out


def _fog_months_from_rows(rows, stations):
    """_fog_months from the station figures of the rows the build has just made (25 Oct 2026, stress test T1: the season record read the station record three times, once for the rows, once for the fog months and
    once for the fog days, so a run late in the season cost more than a run early in it). The same numbers: fogH and obsH of a row are the sums _fog_months makes from the same hours, a day with no report is 0."""
    out = {}
    for m in stations:
        months = {}
        for r in rows:
            st = (r.get('station') or {}).get(m['icao']) or {}
            row = months.setdefault(r['d'][:7], {'fogHours': 0, 'fogDays': 0, 'obsHours': 0})
            fh = st.get('fogH') or 0
            row['fogHours'] += fh
            row['fogDays'] += 1 if fh else 0
            row['obsHours'] += st.get('obsH') or 0
        out[m['icao']] = months
    return out


def _fog_day_set_from_rows(rows, stations):
    """_fog_day_set from the rows (see _fog_months_from_rows): the days on which a station has an hour of fog"""
    out = {}
    for m in stations:
        out[m['icao']] = {r['d'] for r in rows if ((r.get('station') or {}).get(m['icao']) or {}).get('fogH')}
    return out


def _fog_day_set(store, stations, start_day, end_day):
    out = {}
    for m in stations:
        days = set()
        for day in _daterange(start_day, end_day):
            d0 = dt.datetime.strptime(day, '%Y-%m-%d')
            if any(('fog' in rec or 'vv' in rec or rec.get('minVis', 99999) < 1000) for _, rec in store.hours(m['icao'], d0, d0 + dt.timedelta(hours=23))):
                days.add(day)
        out[m['icao']] = days
    return out


def fog_before_record(cal, coverage, store_days=None):
    """fog this season before the engine's own station record starts, from the IEM archive episodes in calibration.json (Reported). An episode counts when it ENDS before the first
    hour of the record (on the first day of the record that hour is 04:00, say, so fog of 02:00 to 03:00 that morning is counted); an episode that begins before the record and
    runs into it is left to the record from its first hour. A day the record already counts as a fog day is not counted again (audit logic D14)."""
    eps = _d(_d(_d(cal).get('fog')).get('thisSeasonToDate')).get('episodes')
    eps = eps if isinstance(eps, dict) else {}
    out = {}
    for icao in ('OMRK', 'OMSJ', 'OMFJ'):
        first_key = coverage.get(icao) or '9999-99-99T99'
        first_dt = None
        try:
            first_dt = dt.datetime.strptime(first_key, '%Y-%m-%dT%H')
        except ValueError:
            pass
        hrs, days = 0, set()
        for e in eps.get(icao) or []:
            if not isinstance(e, dict):
                continue
            d = str(e.get('start', ''))[:10]
            try:
                end_t = dt.datetime.strptime(str(e.get('end') or e.get('start'))[:13], '%Y-%m-%d %H')
            except ValueError:
                end_t = None
            before = (end_t < first_dt) if (end_t is not None and first_dt is not None) else (d < first_key[:10])
            if d and before:
                hrs += e.get('fogHours') or 0
                if d not in (store_days or {}).get(icao, set()):
                    days.add(d)
        out[icao] = {'fogHours': hrs, 'fogDays': len(days), 'daysBefore': (coverage.get(icao) or 'no record')[:10], 'days': sorted(days)}
    return out


def rain_before_record(cal, coverage):
    """days this season on which a station reported rain before the engine's own station record starts, from the IEM archive days in calibration.json (Reported). The days
    from the start of the season to the day before the record starts; an absent record is not a zero (audit logic D4)."""
    rd = _d(_d(cal).get('observed')).get('rainDaysLastSeason')
    rd = rd if isinstance(rd, dict) else {}
    out = {}
    for icao in ('OMRK', 'OMSJ', 'OMFJ'):
        first = (coverage.get(icao) or '9999')[:10]
        days = sorted({d for d in (rd.get(icao) or []) if isinstance(d, str) and SEASON_START <= d < first})
        out[icao] = {'rainDays': len(days), 'days': days, 'daysBefore': (coverage.get(icao) or 'no record')[:10]}
    return out


def last_season_block(cal, fog_this):
    """last season's figures from calibration.json for the same months and stations (Reported; the archive has no observations 5 Mar to 23 Apr 2026)"""
    if not cal:
        return {'available': False, 'note': 'calibration.json is not on file; no comparison with last season (owner: the weather desk)'}
    if not isinstance(cal, dict):
        return {'available': False, 'note': 'calibration.json is not in the shape the engine reads (a named gap, owner: the weather desk); no comparison with last season'}
    fog_ls = _d(_d(cal.get('fog')).get('lastSeason'))
    out = {'available': True, 'source': 'data/weather/calibration.json (24 Oct 2026)', 'season': fog_ls.get('season', 'Oct 2025 to Mar 2026'),
           'coverageNote': fog_ls.get('observedCoverageNote')}
    obs = _d(cal.get('observed'))
    cols = obs.get('byMonthColumns') if isinstance(obs.get('byMonthColumns'), list) else []
    by_month = _d(obs.get('byMonth'))
    daily = _d(_d(obs.get('lastSeasonDaily')).get('days'))
    dcols = _d(obs.get('lastSeasonDaily')).get('columns') if isinstance(_d(obs.get('lastSeasonDaily')).get('columns'), list) else []
    fi = cols.index('fogHours') if 'fogHours' in cols else None
    oi = cols.index('obsHours') if 'obsHours' in cols else None
    dfi = dcols.index('fogHours') if 'fogHours' in dcols else None
    stn = {}
    for icao in ('OMRK', 'OMSJ', 'OMFJ'):
        rows = {}
        for ym in ('2025-10', '2025-11', '2025-12', '2026-01', '2026-02', '2026-03'):
            rec = _d(by_month.get(icao)).get(ym)
            fd = 0
            if dfi is not None:
                for day, d in daily.items():
                    if day.startswith(ym) and isinstance(d, dict) and isinstance(d.get(icao), list) and len(d[icao]) > dfi and d[icao][dfi]:
                        fd += 1
            if isinstance(rec, list) and fi is not None and len(rec) > fi:
                rows[ym] = {'fogHours': rec[fi], 'fogDays': fd, 'obsHours': rec[oi] if (oi is not None and len(rec) > oi) else None}
        stn[icao] = rows
    out['fogByStationMonth'] = stn
    out['fogLastSeasonTotals'] = {k: v for k, v in fog_ls.items() if k in ('OMRK', 'OMSJ', 'OMFJ')}
    rd = _d(obs.get('rainDaysLastSeason'))
    # the list in calibration.json also holds days of this season (16 and 18 Oct 2026 at Fujairah): only the days of last season are last season
    out['stationRainDays'] = {k: len([d for d in v if isinstance(d, str) and LAST_SEASON[0] <= d <= LAST_SEASON[1]]) for k, v in rd.items() if isinstance(v, list)}
    out['stationRainDaysThisSeason'] = {k: sorted(d for d in v if isinstance(d, str) and d >= SEASON_START) for k, v in rd.items() if isinstance(v, list)}
    p24 = lambda e: e.get('peak24hMm', e.get('peak24mm'))
    pulses = [e for e in (cal.get('events') if isinstance(cal.get('events'), list) else []) if isinstance(e, dict) and p24(e) is not None]
    if pulses:
        h = max(pulses, key=p24)
        out['heaviestPulse'] = {'date': h.get('name') or h.get('date') or h.get('id'), 'peak6mm': h.get('peak6hMm', h.get('peak6mm')), 'peak24mm': p24(h), 'reports': h.get('reports'),
                                'label': 'Model analysis (Recomputed), 8 points, not a gauge'}
    return out


def cal_ok(cal):
    return isinstance(cal, dict) and isinstance(_d(cal.get('observed')).get('rainDaysLastSeason'), dict)


def build(data_dir, now, fetcher, run_days, today_partial, store, stations, ncm_entries, register, calibration, can_fetch=True, cond_extra=None):
    """update season.json; returns (season dict, list of problems). run_days: {day: [per point {'median','max','n'}]} from this run's models.
    cond_extra (24 Oct 2026, conditions): {'row': function day -> keys added to the row of that day, 'top': keys added to the season} from wx_condrec.season_extra: the temperature, humidity, wind, gust
    and pressure of each point and the observed extremes of each station."""
    cache_path = os.path.join(data_dir, 'season_cache.json')
    cache, cstatus = read_json_status(cache_path)
    problems = []
    cache_damaged = False
    if cstatus == 'ok' and isinstance(cache, dict) and isinstance(cache.get('days'), dict):
        pass
    else:
        if cstatus != 'missing':
            # a cache that is there and does not read is kept aside before it is rebuilt, and the run says so (audit D4)
            cache_damaged = True
            problems.append('season_cache.json %s: the cached days are fetched again and the damaged file is kept aside when the cache is written' % ('cannot be opened' if cstatus == 'unreadable' else 'does not parse'))
        cache = {'schema': 1, 'days': {}}
    cdays = cache['days']
    today = now.strftime('%Y-%m-%d')
    days = list(_daterange(SEASON_START, today))
    n_pts = len(POINTS)
    dirty = False
    # 1. finish the days that this run's models hold completely
    for d in days:
        if d >= today:
            continue
        have = cdays.get(d) or {}
        pts = run_days.get(d)
        if pts:
            for i in range(n_pts):
                if str(i) not in have and pts[i] and pts[i].get('n'):
                    have[str(i)] = {'med': pts[i]['median'], 'max': pts[i]['max'], 'n': pts[i]['n'], 'src': 'models, analysis hours of a run', 'medRaw': pts[i].get('medianRaw'), 'maxRaw': pts[i].get('maxRaw')}
                    dirty = True
            if have:
                cdays[d] = have
    # 2. days still missing: archive once, then the models' own past hours
    missing = [d for d in days if d < today and any(str(i) not in (cdays.get(d) or {}) for i in range(n_pts))]
    if missing and can_fetch:
        for i in range(n_pts):
            need = [d for d in missing if str(i) not in (cdays.get(d) or {})]
            if not need:
                continue
            got, info = _fetch_archive(fetcher, i, need[0], need[-1])
            if got is None:
                if info.get('error') != 'no fixture':
                    problems.append('archive %s: %s' % (POINTS[i][0].split(' (')[0], info.get('error') or info.get('status')))
            else:
                for d, v in got.items():
                    if d in need:
                        cdays.setdefault(d, {})[str(i)] = {'med': v['median'], 'max': v['max'], 'n': 1, 'src': 'archive ERA5 (reanalysis)', 'medRaw': v.get('medianRaw'), 'maxRaw': v.get('maxRaw')}
                        dirty = True
        missing = [d for d in days if d < today and any(str(i) not in (cdays.get(d) or {}) for i in range(n_pts))]
        if missing:
            back = (now - dt.datetime.strptime(missing[0], '%Y-%m-%d')).days + 1
            for i in range(n_pts):
                need = [d for d in missing if str(i) not in (cdays.get(d) or {})]
                if not need:
                    continue
                got, info = _fetch_past_models(fetcher, i, back)
                if got is None:
                    if info.get('error') != 'no fixture':
                        problems.append('past hours %s: %s' % (POINTS[i][0].split(' (')[0], info.get('error') or info.get('status')))
                    continue
                for d in need:
                    v = got.get(d)
                    if v and v['n']:
                        cdays.setdefault(d, {})[str(i)] = {'med': v['median'], 'max': v['max'], 'n': v['n'], 'src': 'models, past hours (forecast analysis)', 'medRaw': v.get('medianRaw'), 'maxRaw': v.get('maxRaw')}
                        dirty = True
    if dirty or cache_damaged:
        cache['updated'] = f_local(now)
        try:
            if cache_damaged and cstatus != 'unreadable':
                quarantine_copy(cache_path)
            if not (cache_damaged and cstatus == 'unreadable'):
                atomic_write_json(cache_path, cache)
        except OSError as e:
            problems.append('season_cache.json could not be written: %s' % str(e)[:100])
    # 3. the rows
    rows = []
    gaps = []
    totals = [{'median': 0.0, 'max': 0.0, 'days': 0} for _ in range(n_pts)]
    for d in days:
        have = cdays.get(d) or {}
        row = {'d': d}
        if d == today:
            row['partial'] = True
            row['med'] = [r1(today_partial.get(i)) if today_partial.get(i) is not None else None for i in range(n_pts)]
            row['max'] = None
            row['n'] = None
            row['src'] = 'models, hours so far today (the median of the models\' own sums)'
        else:
            row['med'] = [have[str(i)]['med'] if str(i) in have else None for i in range(n_pts)]
            row['max'] = [have[str(i)]['max'] if str(i) in have else None for i in range(n_pts)]
            row['medRaw'] = [have[str(i)].get('medRaw') if str(i) in have else None for i in range(n_pts)]       # unrounded, for the verification thresholds (audit logic D8)
            row['n'] = [have[str(i)]['n'] if str(i) in have else None for i in range(n_pts)]
            srcs = sorted({have[k]['src'] for k in have})
            row['src'] = '; '.join(srcs) if srcs else None
            if not srcs:
                gaps.append(d)
            for i in range(n_pts):
                if str(i) in have and have[str(i)]['med'] is not None:
                    totals[i]['median'] += have[str(i)]['med']
                    totals[i]['max'] += have[str(i)]['max'] or 0.0
                    totals[i]['days'] += 1
        row['station'] = _row_stations(store, stations, d)
        row['ncm'] = _row_ncm(ncm_entries, d)
        row['events'] = register.ids_on(d) if register else {}
        if cond_extra:
            try:
                row.update(cond_extra['row'](d) or {})
            except Exception as e:                                          # the conditions of a day are a side record: one that cannot be read never stops the rain record
                problems.append('conditions of %s could not be added to the season record (%s)' % (d, type(e).__name__))
        rows.append(row)
    fog = _fog_months_from_rows(rows, stations)
    cov = store.coverage()
    before = fog_before_record(calibration, cov, _fog_day_set_from_rows(rows, stations))
    rain_before = rain_before_record(calibration, cov)
    last = last_season_block(calibration, fog)
    worst = max(range(n_pts), key=lambda i: totals[i]['median']) if any(t['days'] for t in totals) else None
    station_rain_days = {m['icao']: sum(1 for r in rows if (r['station'].get(m['icao']) or {}).get('rain')) for m in stations}
    station_rain_total = {k: v + (rain_before.get(k) or {}).get('rainDays', 0) for k, v in station_rain_days.items()}
    record_start = (cov.get('OMRK') or 'no record')[:10]
    lines = []
    if worst is not None:
        lines.append('This season to %s: the model analysis total (median of the models, sum of the days) is highest at %s with %.1f mm over %d days (Model analysis, Recomputed; no rain gauge register on file).'
                     % (today, POINTS[worst][0], totals[worst]['median'], totals[worst]['days']))
    if cal_ok(calibration):
        lines.append('Stations reported rain, from 1 Oct to %s, on ' % today + ', '.join('%s %d days' % (k, v) for k, v in station_rain_total.items()) + ' (Reported; METARs carry no amount; the engine\'s own station record starts %s and the days before it come from the IEM archive in calibration.json).' % record_start)
    else:
        lines.append('Stations reported rain, in the engine\'s own record from %s to %s, on ' % (record_start, today) + ', '.join('%s %d days' % (k, v) for k, v in station_rain_days.items())
                     + ' (Reported; METARs carry no amount). The days from 1 Oct to the day before %s are not on record: they are not zero, they are not known (calibration.json is not usable; owner: the weather desk).' % record_start)
    if last.get('available'):
        hp = last.get('heaviestPulse')
        lines.append('Last season in full (%s), against this season to date: stations reported rain on %s days (Reported; the archive has no observations 5 Mar to 23 Apr 2026)%s.'
                     % (last['season'], ', '.join('%s %d' % (k, v) for k, v in (last.get('stationRainDays') or {}).items()) or 'an unknown number of',
                        ('; heaviest model analysis pulse %s, peak 6 h %s mm and peak 24 h %s mm, %s register reports (calibration.json)' % (hp['date'], hp['peak6mm'], hp['peak24mm'], hp['reports'])) if hp else ''))
        ls = last.get('fogLastSeasonTotals') or {}
        fl = []
        for icao in ('OMRK', 'OMSJ'):
            t_ = sum(v['fogHours'] for v in fog.get(icao, {}).values()) + before[icao]['fogHours']
            td = sum(v['fogDays'] for v in fog.get(icao, {}).values()) + before[icao]['fogDays']
            l_ = ls.get(icao) or {}
            fl.append('%s %d fog hours on %d days this season against %s hours on %s days last season' % (icao, t_, td, l_.get('fogHours', 'no'), l_.get('fogDays', 'no')))
        start = (cov.get('OMRK') or 'no record')[:10]
        lines.append('Fog, this season to date against last season in full: ' + '; '.join(fl) + ' (Reported; the engine\'s own station record starts %s, the days before come from the IEM archive in calibration.json; last season is to 5 Mar 2026 only).' % start)
    season = {'schema': 1, 'from': SEASON_START, 'updated': f_local(now), 'status': 'Model analysis (Recomputed) for rain; Reported for stations and NCM; the analysis is a model and no rain gauge register is on file (gap, owner Wet Utilities with NCM)',
              'points': [p[0] for p in POINTS], 'stations': [m['icao'] for m in stations], 'days': rows,
              'total': {'perPoint': [{'name': POINTS[i][0], 'median': round(totals[i]['median'], 1), 'max': round(totals[i]['max'], 1), 'days': totals[i]['days']} for i in range(n_pts)],
                        'daysWithData': sum(1 for r in rows if r['d'] != today and r['src']), 'daysTotal': len(rows) - 1, 'gapDays': gaps,
                        'note': 'median: the sum of the daily medians over the models; max: the sum of the daily highest model. Today is not in the totals until it is complete.'},
              'rainBeforeEngineRecord': rain_before,
              'fog': {'stationsByMonth': fog, 'beforeEngineRecord': before, 'engineRecordStarts': {k: v[:10] for k, v in cov.items()}, 'lastSeason': last.get('fogByStationMonth'), 'note': 'fog hour: FG, FZFG, BCFG, MIFG, PRFG or vertical visibility reported, or visibility under 1000 m; the same definition as calibration.json'},
              'lastSeason': last, 'comparison': lines, 'problems': problems}
    if cond_extra:
        season.update(cond_extra.get('top') or {})
    try:
        atomic_write_json(os.path.join(data_dir, 'season.json'), season, indent=0)
    except OSError as e:
        problems.append('season.json could not be written: %s' % str(e)[:100])
    return season, problems


def summary(season):
    if not season:
        return None
    t = season['total']
    worst = max(t['perPoint'], key=lambda p: p['median']) if t['perPoint'] else None
    return {'from': season['from'], 'updated': season['updated'], 'daysWithData': t['daysWithData'], 'daysTotal': t['daysTotal'], 'gapDays': len(t['gapDays']),
            'worstPoint': worst, 'comparison': season.get('comparison'), 'file': 'data/weather/season.json'}
