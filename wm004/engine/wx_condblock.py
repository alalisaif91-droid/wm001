# -*- coding: utf-8 -*-
"""The conditions block of the WM004 weather engine, assembled: the points, the stations, the comparison, the levels, the health words, and the compact forms that go into weather.json,
conditions.json, interaction.json and the ledger. Pure functions (see wx_cond.py for the readers and wx_levels.py for the levels). Standard library only."""
import json
import wx_cond as C
import wx_levels as L
import wx_wind as WF
from wx_util import f_local, TZ_NAME

STATIONS_NOTE = ('the stations are airports, not the rain points; METAR wind is a 10 minute mean in whole knots, temperature and dew point are whole degrees, pressure is the QNH in whole hPa; '
                 'a gust is in a report only when it passes the mean by 10 kt (0.1 to 0.7 percent of reports), so no gust means none reported, never calm; relative humidity is worked out '
                 'from temperature and dew point (Magnus: RH = 100 * exp(17.625 * Td / (243.04 + Td)) / exp(17.625 * T / (243.04 + T)), Recomputed) and is good to a few points')
COMPARISON_NOTE = ('each point has the station nearest to it (nearestStation, with the distance); comparison is model minus observed at the hour of the latest report (Recomputed): temperature in deg C, relative humidity in %, '
                   'wind (the 10 minute mean) and gust in km/h, pressure (QNH) in hPa; the observed gust is the gust the report carries and is null when it carries none; peak is the higher of the mean '
                   'and a reported gust, and diff.peak is the model gust minus that peak; the station is an airport, so a difference is partly place and height, not only model error')
STATUS_TEXT = ('Model forecast (Proposed) for the points; Reported for the stations (aviation reports, METAR and SPECI); Recomputed for the comparison, the relative humidity of the stations and '
               'the arithmetic of the levels; the levels are Proposed. No outside feed is an official warning: only an NCM entry is official')
GAPS = [{'what': 'No gust is measured at site: a model gust is not a METAR gust and METAR carries a gust in 0.1 to 0.7 percent of reports', 'owner': 'Wet Utilities with NCM; a season of recorded forecasts closes it'},
        {'what': 'No rain gauge register is on file', 'owner': 'Wet Utilities with NCM'},
        {'what': 'NCM automatic station observations (26 stations in the area, 15 minute data) sit behind a token page and are not read', 'owner': 'Wet Utilities with NCM'},
        {'what': 'Model visibility has no skill and is shown, never used for a level', 'owner': 'the weather desk'},
        {'what': 'Heat thresholds are Proposed from October to March only; summer heat belongs to the summer arrangements', 'owner': 'HSE'},
        {'what': 'The rain plan has no trigger for wind, gust, humidity, temperature, pressure or dust: the levels are read only inputs and are not plan content', 'owner': 'Waste Management with LOD'}]
NOTES = ['a missing reading is no reading: null in a value, None in an array, never 0 and never calm',
         'the arrays of a point start at the current hour and hold 72 hours, one per hour, on Ras Al Khaimah time; a band is [lowest model, median of the models, highest model]',
         'wind direction is the direction the wind comes from; calm is a speed of 0 with no direction, a variable wind has a speed and no direction',
         'CAPE differs by a factor of 3 between models: it is shown per model (capeByModel) and never sets a level',
         'the thresholds of the levels are Proposed from the scout of 24 Oct 2026 (data/weather/params_calibration.json); the rain plan has no trigger for these parameters']
LEVEL_KEYS = ('wind', 'gust', 'humidity', 'heat', 'cold', 'pressure', 'dust', 'visibility')


def short_name(n):
    return (n or '').split(' (')[0]


def _newest(stn_blocks):
    ok = [s for s in stn_blocks.values() if s.get('ok')]
    return min(ok, key=lambda s: s['ageMin']) if ok else None


def compact_level(h):
    """a level for the files that a screen reads: the word, the basis, the two sides in short, the thresholds. The full object stays in interaction.json."""
    def side(s):
        if s is None:
            return None
        return {k: s.get(k) for k in ('level', 'value', 'point', 'station', 'at', 'why') if s.get(k) is not None}
    out = {'level': h['level'], 'basis': h.get('basis'), 'forecast': side(h.get('forecast')), 'observed': side(h.get('observed')), 'thresholds': h.get('thresholds'), 'readOnly': True,
           'status': h.get('status'), 'planValue': None}
    if 'fogProne' in h:
        out['fogProne'] = h['fogProne']['level']
        out['dry'] = h['dry']['level']
    return out


def condition_alerts(cond, pts, stn_blocks):
    """plain alert words for the failure modes of the conditions: a call that failed, a variable with too few values, a band with too few models, no fresh station. Nothing is said when a
    test supplied no answer at all (skipped)."""
    al = []
    src = cond['sources']
    f = src['forecast']
    if f['status'] in ('partial', 'error'):
        al.append('conditions: the forecast of wind, humidity, temperature, pressure, cloud and visibility was not read at %d of %d points (%s): %s; those points show no reading, and no level of none is given while a point is unread'
                  % (f['pointsTotal'] - f['pointsRead'], f['pointsTotal'], ', '.join(f['failed']), '; '.join(f['errors'])))
    a = src['air']
    if a['status'] in ('partial', 'error'):
        al.append('conditions: the dust forecast (CAMS air quality) was not read at %d of %d points (%s): %s; dust shows no reading there' % (a['pointsTotal'] - a['pointsRead'], a['pointsTotal'], ', '.join(a['failed']), '; '.join(a['errors'])))
    expected = [('temp', 'temperature'), ('rh', 'relative humidity'), ('dew', 'dew point'), ('ws', 'wind speed'), ('wd', 'wind direction'), ('gust', 'wind gust'), ('pres', 'pressure'), ('cloud', 'cloud cover')]
    for key, label in expected:
        bad = [p for p in pts if p.get('ok') and key in (p.get('unusable') or {})]
        if bad:
            al.append('conditions: %s had too few values in the next 72 hours at %d points (%s): %s; it shows no reading there' % (label, len(bad), ', '.join(short_name(p['name']) for p in bad[:4]), bad[0]['unusable'][key]))
    for key, label in (('ws', 'wind speed'), ('gust', 'wind gust'), ('temp', 'temperature'), ('rh', 'relative humidity'), ('pres', 'pressure')):
        thin = [short_name(p['name']) for p in pts if p.get('ok') and p.get('bandN', {}).get(key, 0) < C.MIN_MODELS]
        if thin:
            al.append('conditions: the %s band has fewer than %d usable models at %d points (%s): best_match alone is used there, and the extremes say so' % (label, C.MIN_MODELS, len(thin), ', '.join(thin[:4])))
    if stn_blocks and not any(s.get('ok') and s.get('fresh') for s in stn_blocks.values()):
        al.append('conditions: no station has reported in the last %g hours: the observed wind, humidity, temperature, pressure and visibility are unknown, not calm' % C.FRESH_H)
    w = src.get('wind')
    if w and w['status'] == 'error':
        al.append('conditions: the wind field (model wind on a 0.1 degree grid, %d nodes) was not read%s: the map shows no wind movement and says so; the wind of the eight points is not affected'
                  % (WF.N_NODES, (' (%s)' % '; '.join(w['errors'][:2])) if w.get('errors') else ''))
    elif w and w['status'] == 'partial':
        al.append('conditions: the wind field was read at %d of %d nodes%s: a node with no reading is null on the map (no reading, never calm)'
                  % (w['nodesRead'], w['nodesTotal'], (' (%s)' % '; '.join(w['errors'][:2])) if w.get('errors') else ''))
    chk = (cond['health'].get('windfield') or {}).get('check') or {}
    if chk.get('agree') == 'no':
        al.append('conditions: the wind field disagrees with the wind of a plan point read from the same model cell (largest difference %s km/h in speed, %s deg in direction against %s km/h and %s deg allowed): '
                  'the map and the point do not show one model; owner: the weather desk'
                  % (chk['maxDiffSameCell'].get('ws'), chk['maxDiffSameCell'].get('dir'), chk['toleranceKmh'], chk['toleranceDeg']))
    return al


def wind_state(read, wf):
    """the state of the wind field as a source: status ok, partial, error or skipped, the nodes read, the calls and the reasons of the calls that failed"""
    if read is None or (wf or {}).get('status') == 'skipped':
        return {'status': 'skipped', 'nodesRead': 0, 'nodesTotal': WF.N_NODES, 'errors': [], 'calls': 0, 'callsOk': 0}
    st = (wf or {}).get('status')
    errs = []
    for e in read.get('errors') or []:
        if e not in errs:
            errs.append(e)
    if not (wf or {}).get('ok') and (wf or {}).get('error') and (wf or {}).get('error') not in errs:
        errs.append(wf['error'])
    return {'status': {'ok': 'ok', 'partial': 'partial'}.get(st, 'error'), 'nodesRead': (wf or {}).get('nodesRead') or 0, 'nodesTotal': WF.N_NODES, 'errors': errs[:3], 'calls': read.get('nCalls'),
            'callsOk': read.get('okCalls')}


def assemble(now, reads_cond, reads_rain, stations_meta, stn_blocks, wind_read=None):
    """The whole conditions block (the public subset that goes into weather.json and conditions.json), the full levels (for interaction.json) and the working points. Returns (cond, levels, pts).
    The working keys of the points (those that start with an underscore) never reach cond. wind_read: wx_wind.read_windfield (None when the grid was not read: the block says skipped)."""
    pts = []
    for rc in reads_cond:
        rr = reads_rain[rc['idx']] if reads_rain and rc['idx'] < len(reads_rain) else None
        pts.append(C.build_point(rc, rr if (rr and rr.get('series')) else None, (rr or {}).get('daily'), now, stations_meta))
    for p in pts:
        p['comparison'] = C.comparison(p, stn_blocks, now) if p.get('ok') else {'ok': False, 'error': 'the point has no conditions: ' + str(p.get('error'))[:80]}
    levels = L.compute_levels(pts, stn_blocks, now, reads_rain)
    wf, wf_work = WF.build(wind_read, now, pts)
    wf_check = WF.check_points(wf_work, pts)
    st_wind = wind_state(wind_read, wf)
    st_bm, st_air = C.source_state(reads_cond, 'bm'), C.source_state(reads_cond, 'air')
    newest = _newest(stn_blocks)
    fresh = [k for k, s in stn_blocks.items() if s.get('ok') and s.get('fresh')]
    st_stn = {'status': 'ok' if (len(fresh) == len(stn_blocks) and fresh) else ('partial' if fresh else ('error' if stn_blocks else 'skipped')), 'pointsRead': len(fresh), 'pointsTotal': len(stn_blocks),
              'fresh': fresh, 'silent': [k for k in stn_blocks if k not in fresh]}
    got = st_bm['status'] in ('ok', 'partial')
    got_air = st_air['status'] in ('ok', 'partial')
    sources = {
        'forecast': dict(st_bm, label='Model forecast (Proposed)', provider='Open-Meteo best_match, one call per point, 2 past days and 7 forecast days', asof=f_local(now) if got else None, ageMin=0 if got else None),
        'ensemble': {'label': 'Model forecast (Proposed)', 'provider': 'the per model calls of the engine (Open-Meteo)', 'models': C.BAND_MODELS, 'minModels': C.MIN_MODELS, 'minShare': C.MIN_SHARE,
                     'leftOut': dict(C.EXCLUDE), 'note': 'a band is the lowest, the median and the highest of the usable models at each hour; a model or a variable with under 90 percent of the next 72 hours present '
                                                         'is not used; an hour with fewer than 3 models is null; ecmwf_ifs is read for rain and fog and is not in the bands (the same family as ecmwf_ifs025)'},
        'air': dict(st_air, label='Model forecast (Proposed)', provider='Open-Meteo air quality, CAMS: dust, pm10, pm2_5, aerosol optical depth; 8 points give 5 distinct cells', asof=f_local(now) if got_air else None,
                    ageMin=0 if got_air else None),
        'stations': dict(st_stn, label='Reported', provider='aviationweather.gov METAR and SPECI, OMRK, OMSJ and OMFJ', asof=newest['reportTime'] if newest else None, ageMin=newest['ageMin'] if newest else None),
        'wind': dict(st_wind, label=WF.STATUS, provider='Open-Meteo best_match on a 0.1 degree grid (%d nodes in %d calls of at most %d locations)' % (WF.N_NODES, len(WF.split_calls()), WF.MAX_PER_CALL),
                     asof=wf.get('asof') if wf.get('ok') else None, ageMin=wf.get('ageMin') if wf.get('ok') else None)}
    stations_pub = {k: {a: b for a, b in s.items() if a != 'note'} for k, s in stn_blocks.items()}
    params = C.parameter_health(pts, stn_blocks)
    wf_word = {'ok': 'read', 'partial': 'partial: %d of %d nodes' % (st_wind['nodesRead'], WF.N_NODES)}.get(st_wind['status'], 'unknown')
    params['wind field'] = wf_word
    health = {'status': 'ok', 'parameters': params, 'alerts': [], 'levelWords': levels['summary'],
              'windfield': {'status': wf_word, 'nodesRead': st_wind['nodesRead'], 'nodesTotal': WF.N_NODES, 'cells': wf.get('cells'), 'asof': wf.get('asof') if wf.get('ok') else None, 'calls': {'n': st_wind['calls'], 'ok': st_wind['callsOk']},
                            'errors': st_wind['errors'], 'check': wf_check}}
    cond = {'schema': C.SCHEMA, 'asof': f_local(now), 'tz': TZ_NAME, 'status': STATUS_TEXT, 'units': C.UNITS, 'sources': sources, 'points': [C.public_point(p) for p in pts], 'windfield': wf, 'stations': stations_pub,
            'stationsNote': STATIONS_NOTE, 'comparisonNote': COMPARISON_NOTE, 'levels': {k: compact_level(levels[k]) for k in LEVEL_KEYS}, 'levelsNote': levels['note'], 'health': health, 'gaps': GAPS, 'notes': NOTES}
    al = condition_alerts(cond, pts, stn_blocks)
    health['alerts'] = al
    bad = ('partial', 'error')
    all_skipped = st_bm['status'] == 'skipped' and st_air['status'] == 'skipped' and st_wind['status'] == 'skipped'
    health['status'] = 'partial' if (al or st_bm['status'] in bad or st_air['status'] in bad or st_wind['status'] in bad) else ('skipped' if all_skipped else 'ok')
    health['words'] = 'Conditions: forecast %s at %d of %d points, air quality %s at %d of %d, wind field %s at %d of %d nodes, stations %d of %d fresh. %s' % (
        st_bm['status'], st_bm['pointsRead'], st_bm['pointsTotal'], st_air['status'], st_air['pointsRead'], st_air['pointsTotal'], st_wind['status'], st_wind['nodesRead'], st_wind['nodesTotal'], len(fresh), len(stn_blocks),
        'Every parameter is read.' if not al else 'Some are not read: see the alerts; a reading that is missing shows as no reading.')
    return cond, levels, pts


def failed_block(now, reason):
    """the file written when the whole conditions block could not be built: it says so, at this run's time, and carries no value"""
    return {'schema': C.SCHEMA, 'asof': f_local(now), 'tz': TZ_NAME, 'status': 'no reading: the conditions block could not be built in this run (%s)' % reason, 'units': C.UNITS, 'points': [], 'stations': {}, 'levels': {},
            'windfield': {'ok': False, 'status': 'unknown', 'label': WF.STATUS, 'note': 'no reading: the conditions block could not be built in this run'},
            'sources': {}, 'health': {'status': 'error', 'parameters': {}, 'alerts': ['conditions: the block could not be built in this run (%s); every parameter shows no reading' % reason], 'words': 'No reading.'}, 'gaps': GAPS, 'notes': NOTES}


def hazards_from(levels):
    """the read only hazards for interaction.json: the full objects, keyed wind, gust, humidity, heat, cold, pressure and visibility. Dust is merged into the existing dust hazard by the engine."""
    return {k: dict(levels[k]) for k in ('wind', 'gust', 'humidity', 'heat', 'cold', 'pressure', 'visibility')}


def ledger_block(pts, stn_blocks, levels, sources, wind=None):
    """The compact conditions of a run for the ledger: per point the values of the current hour [temp, RH, wind, gust, pressure, direction], the 72 hour extremes [gust max, wind max, RH min, RH max,
    temperature max, temperature min, pressure change 6 h, 24 h, CAPE max, dust max] and the next three days [tmin, tmax, RH min, RH max, wind max, gust max]; per station the latest values
    [temp, RH, wind, gust, QNH, visibility, direction, age in minutes]; the levels; the wind field in a few numbers (wf: nodes read, distinct cells, speed now [min, mean, max], the highest speed and
    gust of the field). Never an hourly array."""
    v = lambda e: None if not e else e.get('v')
    rows = []
    for p in pts:
        if not p.get('ok'):
            rows.append({'i': p['i'], 'err': str(p.get('error'))[:60]})
            continue
        n, e = p['now'], p['extremes']
        rows.append({'i': p['i'], 'n': [n['temp'], n['rh'], n['ws'], n['gust'], n['pres'], n['wd']],
                     'x': [v(e.get('gustMax')), v(e.get('wsMax')), v(e.get('rhMin')), v(e.get('rhMax')), v(e.get('tempMax')), v(e.get('tempMin')), v(e.get('presChange6h')), v(e.get('presChange24h')),
                           v(e.get('capeMax')), v(e.get('dustMax'))],
                     'd': [[d['tmin'], d['tmax'], d['rhMin'], d['rhMax'], d['wsMax'], d['gustMax']] for d in p['daily'][:3]]})
    stn = {}
    for k, s in stn_blocks.items():
        if not s.get('ok'):
            stn[k] = {'err': str(s.get('error'))[:40]}
            continue
        lt = s['latest']
        stn[k] = [lt['temp'], lt['rh'], lt['wind']['kmh'], lt['wind']['gustKmh'], lt['qnh'], lt['vis'], lt['wind']['dir'], s['ageMin']]
    return {'pts': rows, 'stn': stn, 'lv': {k: levels[k]['level'] for k in LEVEL_KEYS}, 'st': {'fc': sources['forecast']['status'], 'air': sources['air']['status'], 'stn': len(sources['stations']['fresh']),
                                                                                             'wind': (sources.get('wind') or {}).get('status')}, 'wf': WF.ledger_summary(wind)}


def json_size(o):
    """the size in bytes of the compact JSON of o as UTF-8; a lone surrogate in a text (a METAR with a stray escape) counts as one replacement character, it never stops the count"""
    return len(json.dumps(o, ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'replace'))


def with_windfield(cond, wf):
    """a shallow copy of the block with another windfield"""
    c = dict(cond)
    c['windfield'] = wf
    return c


def fit_conditions(cond, limit):
    """The block for conditions.json, under `limit` bytes. First the wind field is put on the default schedule (every hour to 24 hours ahead, then every 3 hours: 150 nodes by 49 hourly stamps is 76 to 90
    KB and cannot sit in the file beside the eight points) and, if the file is still too long, on the next thinner schedule, down to every 3 hours; the points and the stations are not touched while a
    thinner wind field fits. Only when even that is too long are the secondary arrays of the points trimmed, as shrink_for_weather does. The field is never cut by nodes: a node is whole or null.
    Returns (block, notes, trimmed): notes say how the wind field was thinned (a design choice, not a loss of a reading); trimmed lists what was cut from the points and the stations (a loss, an alert)."""
    wf = cond.get('windfield')
    notes, trimmed = [], []
    best = cond
    if wf and wf.get('ok'):
        names = [s[0] for s in WF.SCHEDULES]
        chosen = None
        for nm in names:                                           # the richest schedule that fits (every hour when the points are small enough, which they are not today)
            cand = with_windfield(cond, WF.thin(wf, nm))
            if json_size(cand) <= limit:
                chosen = (nm, cand)
                break
        if chosen is None:
            chosen = (names[-1], with_windfield(cond, WF.thin(wf, names[-1])))
        best = chosen[1]
        if names.index(chosen[0]) > names.index(WF.DEFAULT_SCHEDULE):
            notes.append('the wind field was thinned to the schedule %s (%s) to keep conditions.json under its size limit' % (chosen[0], [s[2] for s in WF.SCHEDULES if s[0] == chosen[0]][0]))
    if json_size(best) > limit:
        best, trimmed = shrink_for_weather(best, limit)
    return best, notes, trimmed


def for_weather(cond):
    """the block for weather.json: the same, with the wind field reduced to the current hour and the next 6 hours (the whole field is in conditions.json, which the console fetches)"""
    return with_windfield(cond, WF.reduce_for_weather(cond.get('windfield')))


def shrink_for_weather(cond, limit):
    """A copy of the block for weather.json that is made smaller step by step until it is under `limit` bytes: the secondary hourly arrays go first (cloud, low cloud, rain chance, code, feels like), then
    the pm10 and optical depth arrays, then the hourly arrays of the stations, and last the whole 72 hour arrays (the full block stays in conditions.json, which the console fetches). What was left out
    is named in 'trimmed'. Returns (copy, trimmed list)."""
    c = json.loads(json.dumps(cond, ensure_ascii=False))
    trimmed = []
    steps = [('hourly cloud, low cloud, rain chance, weather code and apparent temperature', lambda: [p['hourly'].pop(k, None) for p in c['points'] if p.get('hourly') for k in ('cloud', 'cloudLow', 'pop', 'code', 'feels')]),
             ('hourly pm10 and optical depth', lambda: [p['hourly'].pop(k, None) for p in c['points'] if p.get('hourly') for k in ('pm10', 'aod')]),
             ('hourly wind direction, visibility and CAPE', lambda: [p['hourly'].pop(k, None) for p in c['points'] if p.get('hourly') for k in ('wd', 'vis', 'cape')]),
             ('the 24 hour arrays of the stations', lambda: [s.pop('hours', None) for s in c['stations'].values()]),
             ('the humidity and gust bands', lambda: [p['bands'].pop(k, None) for p in c['points'] if isinstance(p.get('bands'), dict) for k in ('rh', 'gust')]),
             ('the wind and temperature bands', lambda: [p.pop('bands', None) for p in c['points']]),
             ('the rest of the hourly arrays', lambda: [p.pop('hourly', None) for p in c['points']])]
    for name, fn in steps:
        if json_size(c) <= limit:
            break
        fn()
        trimmed.append(name)
    if trimmed:
        c['trimmed'] = 'left out of this copy to keep weather.json under its size limit; the full block is in conditions.json: ' + '; '.join(trimmed)
    return c, trimmed
