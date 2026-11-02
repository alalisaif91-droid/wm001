# -*- coding: utf-8 -*-
"""Forecast verification for the WM004 weather engine: what the ledger forecast against what happened. Writes data/weather/verification.json.

For every past day since 1 Oct 2026 with a ledger run about 24 hours ahead and one about 6 hours ahead of the start of that day, the forecast total of the day
(the worst point; the median and the highest of the models) is compared with the model analysis total of the day (the worst point of the season record) and with
the station rain report, at 1 mm and 10 mm a day. Fog is compared with the fog hours the stations reported, at the watch and alert levels.
Hit: forecast and happened. Miss: happened, not forecast. False alarm: forecast, did not happen. Correct negative: neither. Bias: mean forecast minus mean analysis (mm).

The analysis is a model, not a gauge, and no rain gauge register is on file (gap, owner Wet Utilities with NCM). Station reports carry no rainfall amount, so
the station comparison is rain or no rain, at 1 mm only. The scores are Recomputed from the ledger and are not a claim of skill: few days are scored."""
import os, sys, json, argparse, bisect, datetime as dt
from wx_util import read_json, atomic_write_json, f_local, parse_local, now_local, DEFAULT_DATA_DIR, r1
import wx_events as E
import wx_condrec as R
from wx_obs import ObsStore, load_stations

LEADS = {'24h': 24.0, '6h': 6.0}
TOL_H = 4.0
SEASON_START = '2026-10-01'
FOG_RANK = {'none': 0, 'watch': 1, 'alert': 2, 'severe': 3}
MIN_DAYS = 30


def _score(pairs):
    """pairs: [(forecast bool, happened bool)] to the four counts"""
    h = sum(1 for f, o in pairs if f and o)
    m = sum(1 for f, o in pairs if (not f) and o)
    fa = sum(1 for f, o in pairs if f and not o)
    cn = sum(1 for f, o in pairs if (not f) and (not o))
    return {'hit': h, 'miss': m, 'falseAlarm': fa, 'correctNegative': cn, 'n': len(pairs)}


class RunList(list):
    """the runs of the ledger as [(time, line)] in order of time, with the times kept in a list of their own so that pick_run can find the runs near a time without looking at every run"""

    def __init__(self, items):
        super().__init__(items)
        self.times = [t for t, _ in self]


def pick_run(runs, target, tol=TOL_H):
    """the run nearest to `target` within `tol` hours; of two at the same distance the earlier in the list. On a RunList (sorted by time) only the runs inside the window are looked at (25 Oct 2026, stress test
    T1: every day and lead looked at every run of the season, 2.6 million comparisons for a season of 4,380 runs); on a plain list every run is, as before. The same answer either way."""
    best = None
    if isinstance(runs, RunList):
        lo = bisect.bisect_left(runs.times, target - dt.timedelta(hours=tol))
        hi = bisect.bisect_right(runs.times, target + dt.timedelta(hours=tol))
        it = runs[lo:hi]
    else:
        it = runs
    for t, r in it:
        d = abs((t - target).total_seconds()) / 3600.0
        if d <= tol and (best is None or d < best[0]):
            best = (d, t, r)
    return best


def _day_forecast(run, day_offset, key):
    vals = []
    pts = run.get('points')
    for p in pts if isinstance(pts, list) else []:
        arr = p.get(key) if isinstance(p, dict) else None
        if isinstance(arr, list) and len(arr) > day_offset and isinstance(arr[day_offset], (int, float)) and not isinstance(arr[day_offset], bool):
            vals.append(arr[day_offset])
    return max(vals) if vals else None


def pick_spec():
    """what the verification asks of the ledger: the run nearest, within TOL_H hours, to the start of each day from SEASON_START minus each lead (the rain verification and the conditions verification use the same
    leads). The ledger reader that keeps only the runs that can answer this (wx_events.read_ledger_picks) is made for this spec."""
    return {'start': SEASON_START, 'leads': sorted(set(LEADS.values()) | set(R.VERIFY_LEADS.values())), 'tol': TOL_H}


def run(data_dir, now=None, write=True, store=None, stations=None, compact=True, reader=None):
    """reader: 'picks' (the default: only the runs that can be the nearest run to a target, from a cache of a few rows a day: the cost of a run does not grow with the season), 'compact' (every run of the season cut to the
    fields read) or 'full' (the whole ledger as written). compact=False means 'full'. The three give the same verification.json (a test says so)."""
    now = now or now_local()
    reader = reader or ('picks' if compact else 'full')
    season = read_json(os.path.join(data_dir, 'season.json'), None)
    season = season if isinstance(season, dict) else {}
    rows_by_day = {r['d']: r for r in (season.get('days') if isinstance(season.get('days'), list) else []) if isinstance(r, dict) and isinstance(r.get('d'), str)}
    since = dt.datetime.strptime(SEASON_START, '%Y-%m-%d') - dt.timedelta(days=2)
    n_runs = None
    if reader == 'picks':
        ledger, bad, n_runs = E.read_ledger_picks(data_dir, since=since, until=now, spec=pick_spec())
    elif reader == 'compact':
        ledger, bad = E.read_ledger_compact(data_dir, since=since, until=now)
    else:
        ledger, bad = E.read_ledger(data_dir, since=since, until=now)
    runs = []
    for r in ledger:
        if r.get('ok') is False:
            continue                                  # a run that read no model has no forecast: it must not be the nearest run and drop the day (audit logic D8)
        try:
            runs.append((parse_local(r['now']), r))
        except (ValueError, KeyError, TypeError):
            continue
    runs.sort(key=lambda x: x[0])                       # by time (the ledger is read in the order of the text of `now`, the same order for every line the engine writes)
    runs = RunList(runs)
    if n_runs is None:
        n_runs = len(runs)
    store = store or ObsStore(data_dir)
    stations = stations or load_stations(data_dir)
    today = now.strftime('%Y-%m-%d')
    days = sorted(d for d in rows_by_day if SEASON_START <= d < today)
    rain_pairs = {lead: [] for lead in LEADS}
    fog_pairs = {lead: [] for lead in LEADS}
    detail = []
    for d in days:
        d0 = dt.datetime.strptime(d, '%Y-%m-%d')
        row = rows_by_day[d]
        lst = lambda v: [x for x in (v if isinstance(v, list) else []) if isinstance(x, (int, float)) and not isinstance(x, bool)]
        med = lst(row.get('med'))
        a_med = max(med) if med else None
        raw = lst(row.get('medRaw'))
        a_raw = max(raw) if (raw and len(raw) == len(med)) else a_med          # the unrounded analysis when the season record has it: 9.96 is under 10 mm
        a_max_l = lst(row.get('max'))
        a_max = max(a_max_l) if a_max_l else None
        st = row.get('station') or {}
        covered = [k for k, v in st.items() if v.get('obsH', 0) >= 18]
        st_rain = any(st[k].get('rain') for k in covered) if covered else None
        st_fog_west = any((st[k].get('fogH') or 0) > 0 for k in covered if k in ('OMRK', 'OMSJ')) if any(k in covered for k in ('OMRK', 'OMSJ')) else None
        for lead, hrs in LEADS.items():
            pr = pick_run(runs, d0 - dt.timedelta(hours=hrs))
            if not pr:
                continue
            _, t, r = pr
            off = (d0.date() - t.date()).days
            fm = _day_forecast(r, off, 'dm')
            fx = _day_forecast(r, off, 'dx')
            rec = {'day': d, 'lead': lead, 'run': r.get('run'), 'runAt': r.get('now'), 'forecastMedianMm': fm, 'forecastMaxMm': fx, 'analysisMm': r1(a_med), 'analysisRawMm': a_raw, 'stationRain': st_rain}
            if fm is not None and a_med is not None:
                rain_pairs[lead].append(rec)
            fd = {x.get('d'): x.get('l') for x in (r.get('fogDays') if isinstance(r.get('fogDays'), list) else []) if isinstance(x, dict)}
            if d in fd and fd[d] is not None and st_fog_west is not None:             # a fog forecast the engine could not make (l is null) is unknown: it is not scored as none (audit D5)
                fog_pairs[lead].append({'day': d, 'lead': lead, 'forecast': fd[d], 'stationFog': st_fog_west})
                rec['fogForecast'] = fd[d]
                rec['stationFog'] = st_fog_west
            detail.append(rec)
    out_rain = {}
    for lead, recs in rain_pairs.items():
        o = {'daysScored': len(recs)}
        for stat, key in (('median', 'forecastMedianMm'), ('max', 'forecastMaxMm')):
            s = {}
            for thr in (1.0, 10.0):
                prs = [(x[key] is not None and x[key] >= thr, (x['analysisRawMm'] if x.get('analysisRawMm') is not None else x['analysisMm']) >= thr) for x in recs if x[key] is not None]
                s['analysis%gmm' % thr] = _score(prs)
            prs = [(x[key] >= 1.0, x['stationRain']) for x in recs if x[key] is not None and x['stationRain'] is not None]
            s['station1mm'] = _score(prs)
            bias = [(x[key] - x['analysisMm']) for x in recs if x[key] is not None]
            s['biasMm'] = r1(sum(bias) / len(bias)) if bias else None
            o[stat] = s
        out_rain[lead] = o
    out_fog = {}
    for lead, recs in fog_pairs.items():
        o = {'daysScored': len(recs)}
        for lvl in ('watch', 'alert'):
            o[lvl] = _score([(FOG_RANK.get(x['forecast'], 0) >= FOG_RANK[lvl], x['stationFog']) for x in recs])
        out_fog[lead] = o
    cv = None
    try:
        od, _od_status = R.read_obs_daily(data_dir)
        cv = R.conditions_verification(runs, od, stations, today, pick_run, SEASON_START)
    except Exception as e:                       # a side record: one that cannot be scored never stops the rain verification
        cv = {'status': 'not scored in this run (%s: %s)' % (type(e).__name__, str(e)[:80]), 'leads': {}, 'statements': []}
    n_days = max([v['daysScored'] for v in out_rain.values()] + [0])
    statements = ['The analysis is a model (Model analysis, Recomputed), not a gauge: a day the analysis calls dry may have been wet at a street.',
                  'No rain gauge register is on file (gap, owner Wet Utilities with NCM). Station reports carry no rainfall amount: the station comparison is rain or no rain, at 1 mm only.',
                  'Fog is compared with the fog hours of OMRK and OMSJ (the west coast, where the calibration found fog); the incident register has no fog type.']
    if n_days < MIN_DAYS:
        statements.append('%d days are scored so far; fewer than %d days is too few to judge how good the forecast is. The counts are a record, not a skill score.' % (n_days, MIN_DAYS))
    res = {'schema': 1, 'asof': f_local(now), 'status': 'Recomputed from the ledger: Model forecast (Proposed) against Model analysis (Recomputed) and station reports (Reported)',
           'method': {'leads': {k: 'run within %g h of %g h before the start of the day' % (TOL_H, v) for k, v in LEADS.items()},
                      'forecast': 'the worst of the eight points of the forecast total of the day: the median of the models, and the highest model',
                      'truth': 'the worst point of the model analysis total of the day (season.json); a station rain flag (obs, 18 or more hours observed); fog hours of OMRK or OMSJ',
                      'thresholds': 'rain 1 mm and 10 mm a day; fog watch and alert level'},
           'seasonFrom': SEASON_START, 'daysInRecord': len(days), 'ledgerRuns': n_runs, 'ledgerUnreadableLines': bad, 'rain': out_rain, 'fog': out_fog,
           'recent': detail[-16:], 'statements': statements, 'conditions': cv,
           'gaps': [{'what': 'No rain gauge register', 'owner': 'Wet Utilities with NCM'}, {'what': 'No fog type in the incident register', 'owner': 'the incident register owner'},
                    {'what': 'METARs carry no rainfall amount', 'owner': 'NCM'}]}
    if write:
        try:
            atomic_write_json(os.path.join(data_dir, 'verification.json'), res, indent=1)
        except OSError as e:
            res['writeError'] = str(e)[:100]
    return res


def summary(v):
    if not v:
        return None
    r24 = (v['rain'].get('24h') or {})
    f24 = (v['fog'].get('24h') or {})
    c24 = (((v.get('conditions') or {}).get('leads') or {}).get('24h') or {})
    cvars = c24.get('variables') or {}
    out = {'asof': v['asof'], 'daysInRecord': v['daysInRecord'], 'daysScoredRain24h': r24.get('daysScored', 0), 'rain24h': (r24.get('median') or {}).get('analysis1mm'),
           'fog24hWatch': f24.get('watch'), 'daysScoredFog24h': f24.get('daysScored', 0), 'statement': v['statements'][-1] if v['statements'] else None, 'file': 'data/weather/verification.json'}
    if v.get('conditions') is not None:
        out['conditions24h'] = {'daysScored': c24.get('daysScored', 0), 'gustPeak': {k: (cvars.get('gustPeak') or {}).get(k) for k in ('n', 'bias', 'mae')}, 'windMax': {k: (cvars.get('windMax') or {}).get(k) for k in ('n', 'bias', 'mae')},
                                'temperatureMax': {k: (cvars.get('temperatureMax') or {}).get(k) for k in ('n', 'bias', 'mae')}, 'humidityMin': {k: (cvars.get('humidityMin') or {}).get(k) for k in ('n', 'bias', 'mae')},
                                'statement': 'the stations are airports, not the rain points; bias is forecast minus observed (Recomputed)'}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='verify the ledger forecasts against what happened')
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    ap.add_argument('--now', default=None)
    ap.add_argument('--no-write', action='store_true')
    a = ap.parse_args(argv)
    v = run(a.data_dir, parse_local(a.now) if a.now else None, write=not a.no_write)
    print(json.dumps(summary(v), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
