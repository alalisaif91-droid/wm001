# -*- coding: utf-8 -*-
"""The levels of the read only hazards of the weather channel (24 Oct 2026): wind, gust, humidity (fog prone and dry), heat, cold, pressure fall, dust and visibility, each none, watch,
alert or unknown, with the basis in words. Status of every threshold: Proposed. The rain plan has NO value for any of these parameters (its only weather triggers are rain in 6 and 24
hours), so each carries planValue null and says so. The thresholds come from the scout of 24 Oct 2026, data/weather/params_calibration.json, thresholds T01 to T10, and are not tuned here.

unknown is never none. A level of none is only given when every place that could say otherwise was read: all eight points for the forecast side, at least one fresh station for the
observed side. A watch or alert found on what was read stands, whatever was not read. These levels are read only: they do not change the weather stage (the stage stays rain and fog only);
they are inputs for the committee and for the weather channel's triggers.

Forecast values are the median of the usable models (the models of wx_cond.BAND_MODELS, gfs_seamless left out of the gust, meteofrance_seamless out of the humidity); where fewer than three
models are usable the forecast side says it rests on best_match alone. Observed values are the aviation reports of the last three hours at the stations that have reported in that time."""
import datetime as dt
import wx_cond as C
import wx_interact as I
from wx_util import nz, r1

RANK = {'none': 0, 'watch': 1, 'alert': 2}
STATUS = 'Proposed'
SRC_FILE = 'data/weather/params_calibration.json'
# every threshold in one place: (watch, alert). 'below' thresholds read at or below.
TH = {
    'wind': {'id': 'T01', 'unit': 'km/h', 'obs': (30.0, 40.0), 'fc': (25.0, 30.0)},
    'gust': {'id': 'T02', 'unit': 'km/h', 'obs': (40.0, 50.0), 'fc': (50.0, 60.0)},
    'dry': {'id': 'T04', 'unit': '%', 'below': (20.0, 10.0)},
    'fogProne': {'id': 'T03', 'spreadEvening': 5.0, 'rhNight': 90.0, 'windNightKt': 10.0},
    'cold': {'id': 'T05', 'unit': 'deg C', 'below': (10.0, 8.0)},
    'heat': {'id': 'T06', 'unit': 'deg C', 'at': (40.0, 42.0)},
    'pressure': {'id': 'T07', 'unit': 'hPa', 'fall24': (4.0, 6.0), 'fall3Adjusted': (2.0, 3.0)},
    'dust': {'id': 'T08', 'unit': 'ug/m3', 'fc': (300.0, 500.0), 'pm10Alert': 300.0, 'obsVisWatch': 5000, 'obsVisAlert': 3000, 'obsPeakAlertKmh': 40.0},
    'visibility': {'id': 'T09', 'unit': 'm', 'watchBelow': 5000, 'alertAtOrBelow': 1000}}
PLAN_NOTE = 'the rain plan has no trigger for this parameter (its weather triggers are rain in 6 and 24 hours); the thresholds are Proposed from the scout of 24 Oct 2026'
HEAT_SEASON = (10, 11, 12, 1, 2, 3)


def short(name):
    return (name or '').split(' (')[0]


def level_by(value, watch, alert, below=False):
    if value is None:
        return None
    if below:
        return 'alert' if value <= alert else ('watch' if value <= watch else 'none')
    return 'alert' if value >= alert else ('watch' if value >= watch else 'none')


def combine(sides):
    """The level of a hazard from its sides: the highest above none stands; else unknown when any side is unknown; else none."""
    top = 'none'
    for s in sides:
        if RANK.get(s['level'], -1) > RANK[top]:
            top = s['level']
    if top != 'none':
        return top
    return 'unknown' if any(s['level'] == 'unknown' for s in sides) else 'none'


def _hazard(key, level, forecast, observed, thresholds, extra=None):
    ids = TH[key]['id'] if key in TH else None
    out = {'level': level, 'status': STATUS, 'readOnly': True, 'inStage': False, 'evidence': ids, 'source': SRC_FILE, 'planValue': None, 'planNote': PLAN_NOTE, 'thresholds': thresholds,
           'forecast': forecast, 'observed': observed}
    if extra:
        out.update(extra)
    return out


def _basis(label, unit, level, fc, ob, th_fc, th_ob, nd=1):
    parts = []
    for name, s, th in (('Forecast', fc, th_fc), ('Observed', ob, th_ob)):
        if s is None:
            continue
        if s['level'] == 'unknown':
            parts.append('%s: not read well enough (%s).' % (name, s.get('why') or 'no reading'))
        else:
            v = s.get('value')
            txt = '%s: %s' % (name, s.get('text') or ('%s %s' % (('%.*f' % (nd, v)) if v is not None else 'no value', unit)))
            if th:
                txt += ' (watch %s, alert %s, Proposed)' % th
            parts.append(txt + '.')
    head = '%s level %s.' % (label, level)
    tail = ' Unknown is never none: a level of none needs every point read and a station that has reported in the last 3 hours.' if level == 'unknown' else ''
    return head + ' ' + ' '.join(parts) + tail + ' No plan value exists for this parameter; the thresholds are Proposed (%s).' % SRC_FILE


# ------------------------------------------------------------------------------------------------ sides
def fc_side(points, key, watch, alert, mode='max', label=None, unit='', nd=1):
    """the forecast side: the highest (or lowest) value of the median series over the next 72 hours at any point against the thresholds. none needs every point read."""
    worst, unread, srcs = None, [], set()
    for p in points:
        if not p.get('ok'):
            unread.append(short(p.get('name')))
            continue
        series, src, n = C._headline(p['_bands'], p['_arr'], key, p['_i0'], p['_labels'])
        vals = [(v, i) for i, v in enumerate(series or []) if v is not None]
        if not vals:
            unread.append(short(p['name']))
            continue
        srcs.add(src)
        v, i = max(vals, key=lambda x: (x[0], -x[1])) if mode == 'max' else min(vals, key=lambda x: (x[0], x[1]))
        better = worst is None or (v > worst['raw'] if mode == 'max' else v < worst['raw'])
        if better:
            worst = {'raw': v, 'value': round(v, nd), 'at': p['_labels'][i], 'point': short(p['name']), 'src': src}
    if worst is None:
        return {'level': 'unknown', 'why': 'the forecast was not read at any of the %d points' % len(points), 'unreadPoints': unread}
    lv = level_by(worst.pop('raw'), watch, alert, below=(mode == 'min'))          # the level is decided on the value as it is, never on the rounded figure
    worst = dict(worst, level=lv, unreadPoints=unread, sources=sorted(srcs))
    worst['text'] = 'the %s of the next 72 hours is %.*f %s at %s on %s (%s)' % ('highest value' if mode == 'max' else 'lowest value', nd, worst['value'], unit, worst['point'], worst['at'].replace('T', ' '), worst['src'])
    if lv == 'none' and unread:
        worst['level'] = 'unknown'
        worst['why'] = 'the forecast was not read at %s; %s' % (', '.join(unread), worst['text'])
    return worst


def fresh_stations(stations):
    return [s for s in stations.values() if s.get('ok') and s.get('fresh')]


def silent_note(stations):
    silent = [k for k, s in stations.items() if not (s.get('ok') and s.get('fresh'))]
    return silent


def obs_max_side(stations, field, watch, alert, unit, what, nd=1, mode='max', block='last3h'):
    """the observed side: the highest (or lowest) value in the last three hours at a fresh station. unknown when no station is fresh."""
    when = ('in the last %g hours' % C.FRESH_H) if block == 'last3h' else 'so far today'
    fresh = [s for s in fresh_stations(stations) if s[block].get(field) is not None]
    if not fresh:
        why = 'no station that has reported in the last %g hours has %s %s%s' % (C.FRESH_H, what, when, (' (silent: %s)' % ', '.join(silent_note(stations))) if silent_note(stations) else '')
        return {'level': 'unknown', 'why': why}
    pick = (lambda s: s[block][field])
    best = max(fresh, key=pick) if mode == 'max' else min(fresh, key=pick)
    v = pick(best)
    lv = level_by(v, watch, alert, below=(mode == 'min'))
    silent = silent_note(stations)
    return {'level': lv, 'value': round(v, nd), 'station': best['icao'], 'stationsRead': [s['icao'] for s in fresh], 'silent': silent,
            'text': 'the %s %s %s is %.*f %s at %s (%d of %d stations reporting)' % ('highest' if mode == 'max' else 'lowest', what, when, nd, v, unit, best['icao'], len(fresh), len(stations))}


# ------------------------------------------------------------------------------------------------ the hazards
def wind_level(points, stations):
    th = TH['wind']
    fc = fc_side(points, 'ws', th['fc'][0], th['fc'][1], 'max', 'wind speed', 'km/h')
    ob = obs_max_side(stations, 'windMaxKmh', th['obs'][0], th['obs'][1], 'km/h', 'mean wind')
    lv = combine([fc, ob])
    return _hazard('wind', lv, fc, ob, {'forecast': {'watch': th['fc'][0], 'alert': th['fc'][1], 'unit': 'km/h', 'rule': 'median of the usable models, 10 m, hourly mean'},
                                        'observed': {'watch': th['obs'][0], 'alert': th['obs'][1], 'unit': 'km/h', 'rule': 'METAR mean wind'}},
                   {'label': 'Wind speed: model forecast (Proposed) and station reports (Reported)', 'basis': _basis('Wind speed', 'km/h', lv, fc, ob, tuple(th['fc']), tuple(th['obs']))})


def gust_level(points, stations):
    th = TH['gust']
    fc = fc_side(points, 'gust', th['fc'][0], th['fc'][1], 'max', 'wind gust', 'km/h')
    ob = obs_max_side(stations, 'peakMaxKmh', th['obs'][0], th['obs'][1], 'km/h', 'peak wind (the higher of the mean and a reported gust)')
    lv = combine([fc, ob])
    return _hazard('gust', lv, fc, ob, {'forecast': {'watch': th['fc'][0], 'alert': th['fc'][1], 'unit': 'km/h', 'rule': 'median of the usable models without gfs_seamless; a model gust is not a METAR gust, so the forecast values sit 10 km/h above the observed ones'},
                                        'observed': {'watch': th['obs'][0], 'alert': th['obs'][1], 'unit': 'km/h', 'rule': 'METAR peak wind: the higher of the mean and a reported gust'}},
                   {'label': 'Wind gust: model forecast (Proposed, the weakest set) and station reports (Reported)', 'basis': _basis('Wind gust', 'km/h', lv, fc, ob, tuple(th['fc']), tuple(th['obs'])),
                    'caveat': 'Proposed from a thin record: METAR carries a gust in only 0.1 to 0.7 percent of reports; the set needs a season of recorded forecasts and a gust measured at site (owner: Wet Utilities with NCM)'})


def evening_label(now):
    """the 20:00 that counts: today's while it has not passed, else tomorrow's"""
    d = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if now > d:
        d += dt.timedelta(days=1)
    return d


def fog_prone(points, stations, now, reads):
    """humidity, fog prone (T03). Observed watch: at 20:00 the dew point spread is 5 deg C or less at OMRK or OMSJ (from 20:00 to 10:00). Observed alert: 02:00 to 08:00 with RH 90 percent or more and
    wind 10 kt or less at OMRK or OMSJ (held to 10:00). Forecast watch: the same spread at 20:00 on ecmwf_ifs025 at the west plain points. A watch means prone, not forecast: fog is 4 to 5 percent
    of nights. The fog level of the engine (the ladder at 1.0, 0.5, 0.25 deg C) is separate and unchanged."""
    th = TH['fogProne']
    west = [s for s in stations.values() if 'west' in str(s.get('side') or '').lower() and s.get('icao') in ('OMRK', 'OMSJ')]
    h = now.hour
    in_window = h >= 20 or h < 10
    # observed
    ob = {'level': 'none', 'text': 'outside 20:00 to 10:00 the fog prone readings do not apply'}
    if in_window:
        ev = now.replace(hour=20, minute=0, second=0, microsecond=0) - (dt.timedelta(days=1) if h < 20 else dt.timedelta(0))
        lab = ev.strftime('%Y-%m-%dT%H:00')
        got, spreads = 0, {}
        for s in west:
            if not s.get('ok'):
                continue
            hrs = s['hours']
            if lab in hrs['t']:
                j = hrs['t'].index(lab)
                t_, d_ = hrs['temp'][j], hrs['dew'][j]
                if t_ is not None and d_ is not None:
                    got += 1
                    spreads[s['icao']] = round(t_ - d_, 1)
        night_hit = []
        if 2 <= h <= 9:
            day = now.strftime('%Y-%m-%d')
            for s in west:
                if not s.get('ok'):
                    continue
                hrs = s['hours']
                for j, l in enumerate(hrs['t']):
                    if l[:10] == day and 2 <= int(l[11:13]) <= 8 and hrs['rh'][j] is not None and hrs['kt'][j] is not None:
                        if hrs['rh'][j] >= th['rhNight'] and hrs['kt'][j] <= th['windNightKt']:
                            night_hit.append('%s %s' % (s['icao'], l[11:16]))
        if night_hit:
            ob = {'level': 'alert', 'value': None, 'text': 'RH %g percent or more with wind %g kt (%.0f km/h) or less between 02:00 and 08:00 at %s' % (th['rhNight'], th['windNightKt'], th['windNightKt'] * 1.852, ', '.join(night_hit[:4])), 'stations': night_hit[:6]}
        elif spreads and min(spreads.values()) <= th['spreadEvening']:
            ob = {'level': 'watch', 'value': min(spreads.values()), 'text': 'the dew point spread at %s was %s deg C at 20:00 (5 deg C or less: fog prone, not forecast)' % (', '.join('%s %s' % (k, v) for k, v in sorted(spreads.items())), '/'.join(str(v) for v in spreads.values())), 'spreads': spreads}
        elif got:
            ob = {'level': 'none', 'value': min(spreads.values()), 'text': 'the dew point spread at 20:00 was %s deg C at %s (over 5 deg C)' % ('/'.join(str(v) for v in spreads.values()), ', '.join(sorted(spreads))), 'spreads': spreads}
        else:
            ob = {'level': 'unknown', 'why': 'no west coast station (OMRK, OMSJ) has a temperature and dew point for 20:00 in the last 24 hours'}
    # forecast
    el = evening_label(now)
    fc = {'level': 'unknown', 'why': 'ecmwf_ifs025 could not be read at the west plain points'}
    lab = el.strftime('%Y-%m-%dT%H:00')
    spreads, west_pts = [], 0
    for i, rd in enumerate(reads or []):
        if not rd or not (rd.get('name') or '').startswith(I.FOG_POINT_PREFIXES):
            continue
        west_pts += 1
        f = (rd.get('fog') or {}).get('ecmwf_ifs025')
        times = rd.get('times') or []
        if not f or lab not in times:
            continue
        sp = f['_spread'][times.index(lab)] if times.index(lab) < len(f['_spread']) else None
        if sp is not None:
            spreads.append((round(sp, 1), short(rd['name'])))
    if spreads:
        lo = min(spreads)
        lv = 'watch' if lo[0] <= th['spreadEvening'] else 'none'
        fc = {'level': lv, 'value': lo[0], 'point': lo[1], 'at': lab, 'pointsRead': len(spreads), 'text': 'ecmwf_ifs025 has a dew point spread of %.1f deg C at %s at %s on %s (5 deg C or less is fog prone, not forecast)' % (lo[0], lo[1], lab[11:16], lab[:10])}
        if lv == 'none' and len(spreads) < west_pts:
            fc = {'level': 'unknown', 'why': 'ecmwf_ifs025 was read at %d of %d west plain points; %s' % (len(spreads), west_pts, fc['text'])}
    return fc, ob


def dry_level(points, stations):
    th = TH['dry']['below']
    fc = fc_side(points, 'rh', th[0], th[1], 'min', 'relative humidity', '%', nd=0)
    ob = obs_max_side(stations, 'rhMin', th[0], th[1], '%', 'relative humidity (worked out from the whole degrees of temperature and dew point)', nd=0, mode='min')
    return fc, ob


def humidity_level(points, stations, now, reads):
    fp_fc, fp_ob = fog_prone(points, stations, now, reads)
    d_fc, d_ob = dry_level(points, stations)
    lv_fp = combine([fp_fc, fp_ob])
    lv_dry = combine([d_fc, d_ob])
    lv = combine([{'level': lv_fp}, {'level': lv_dry}])
    th_d = TH['dry']['below']
    fp_basis = 'Fog prone %s. Forecast: %s. Observed: %s. A watch means prone, not forecast: fog is 4 to 5 percent of nights; the fog level of the engine is separate.' % (
        lv_fp, fp_fc.get('text') or ('not read well enough (%s)' % fp_fc.get('why')), fp_ob.get('text') or ('not read well enough (%s)' % fp_ob.get('why')))
    dry_basis = _basis('Dry air', '%', lv_dry, d_fc, d_ob, (th_d[0], th_d[1]), (th_d[0], th_d[1]), 0).replace('(watch 20.0, alert 10.0, Proposed)', '(watch at or below 20, alert at or below 10, Proposed)')
    basis = 'Humidity level %s: fog prone %s, dry air %s. %s %s' % (lv, lv_fp, lv_dry, fp_basis, dry_basis)
    return {'level': lv, 'status': STATUS, 'readOnly': True, 'inStage': False, 'evidence': 'T03, T04', 'source': SRC_FILE, 'planValue': None, 'planNote': PLAN_NOTE, 'label': 'Humidity: fog prone and dry air, model forecast (Proposed) and station reports (Reported)',
            'basis': basis, 'fogProne': {'level': lv_fp, 'forecast': fp_fc, 'observed': fp_ob, 'basis': fp_basis, 'thresholds': {'eveningSpreadDegC': TH['fogProne']['spreadEvening'], 'nightRhPercent': TH['fogProne']['rhNight'], 'nightWindKt': TH['fogProne']['windNightKt']}},
            'dry': {'level': lv_dry, 'forecast': d_fc, 'observed': d_ob, 'basis': dry_basis, 'thresholds': {'watchAtOrBelowPercent': th_d[0], 'alertAtOrBelowPercent': th_d[1]}, 'note': 'a reading band, not a trigger: the record shows no hazard that dry air predicts in this plan'},
            'forecast': {'level': combine([fp_fc, d_fc])}, 'observed': {'level': combine([fp_ob, d_ob])}, 'thresholds': {'fogProne': 'see fogProne', 'dry': 'see dry'}}


def heat_cold(points, stations, now):
    """heat (T06) and cold (T05): the daily maximum 40 and 42 deg C, the daily minimum 10 and 8 deg C, forecast on the median series, observed on today's reports at the stations"""
    h, c = TH['heat']['at'], TH['cold']['below']
    fc_h = fc_side(points, 'temp', h[0], h[1], 'max', 'temperature', 'deg C')
    ob_h = obs_max_side(stations, 'tMax', h[0], h[1], 'deg C', 'temperature', block='today')
    fc_c = fc_side(points, 'temp', c[0], c[1], 'min', 'temperature', 'deg C')
    ob_c = obs_max_side(stations, 'tMin', c[0], c[1], 'deg C', 'temperature', mode='min', block='today')
    lh, lc = combine([fc_h, ob_h]), combine([fc_c, ob_c])
    season = now.month in HEAT_SEASON
    heat = _hazard('heat', lh, fc_h, ob_h, {'forecast': {'watchAtOrAbove': h[0], 'alertAtOrAbove': h[1], 'unit': 'deg C', 'rule': 'daily maximum, median of the usable models'},
                                             'observed': {'watchAtOrAbove': h[0], 'alertAtOrAbove': h[1], 'unit': 'deg C', 'rule': 'daily maximum of the reports of today'}},
                   {'label': 'Heat: model forecast (Proposed) and station reports (Reported)', 'basis': _basis('Heat', 'deg C', lh, fc_h, ob_h, tuple(h), tuple(h)),
                    'season': 'the record supports a heat alert only from October to March and not above 42 deg C; summer heat belongs to the summer arrangements (owner: HSE)', 'inSeasonRecord': season})
    cold = _hazard('cold', lc, fc_c, ob_c, {'forecast': {'watchAtOrBelow': c[0], 'alertAtOrBelow': c[1], 'unit': 'deg C', 'rule': 'lowest hour, median of the usable models'},
                                             'observed': {'watchAtOrBelow': c[0], 'alertAtOrBelow': c[1], 'unit': 'deg C', 'rule': 'daily minimum of the reports of today'}},
                   {'label': 'Cold: model forecast (Proposed) and station reports (Reported)', 'basis': _basis('Cold', 'deg C', lc, fc_c, ob_c, (c[0], c[1]), (c[0], c[1])).replace('watch %s, alert %s' % (c[0], c[1]), 'watch at or below %g, alert at or below %g' % (c[0], c[1])),
                    'note': 'cold matters inland and on the west coast only (Fujairah has no day at 10 deg C or less in seven seasons)'})
    return heat, cold


def pressure_level(points, stations):
    th = TH['pressure']
    # forecast: the worst 24 hour fall inside the next 72 hours at any point, on the median series
    worst, unread = None, []
    for p in points:
        if not p.get('ok'):
            unread.append(short(p.get('name')))
            continue
        w = (p.get('extremes') or {}).get('presFall24hWorst')
        if w is None:
            unread.append(short(p['name']))
            continue
        if worst is None or w['v'] < worst['value']:
            worst = {'value': w['v'], 'from': w['from'], 'to': w['to'], 'point': short(p['name']), 'src': w['src']}
    if worst is None:
        fc = {'level': 'unknown', 'why': 'no 24 hour pressure change could be worked out at any of the %d points' % len(points)}
    else:
        fall = -worst['value']
        lv = level_by(fall, th['fall24'][0], th['fall24'][1])
        fc = dict(worst, level=lv, unreadPoints=unread, text='the largest 24 hour change of pressure in the next 72 hours is %+.1f hPa at %s (%s to %s, %s)' % (worst['value'], worst['point'], worst['from'].replace('T', ' '), worst['to'].replace('T', ' '), worst['src']))
        if lv == 'none' and unread:
            fc['level'] = 'unknown'
            fc['why'] = 'no 24 hour change could be worked out at %s; %s' % (', '.join(unread), fc['text'])
    # observed: the 24 hour fall, or the 3 hour fall after the tide is taken out, at a fresh station
    fresh = fresh_stations(stations)
    cands, got = [], 0
    for s in fresh:
        lt = s['latest']
        c24 = (lt.get('change24h') or {}).get('hPa')
        t3 = (lt.get('tendency3h') or {}).get('tideAdjustedHPa')
        if c24 is not None or t3 is not None:
            got += 1
        if c24 is not None:
            cands.append((level_by(-c24, th['fall24'][0], th['fall24'][1]), '24 hour change %+.1f hPa at %s' % (c24, s['icao']), -c24, 'fall24'))
        if t3 is not None:
            cands.append((level_by(-t3, th['fall3Adjusted'][0], th['fall3Adjusted'][1]), '3 hour change beyond the tide %+.1f hPa at %s (raw %+.1f)' % (t3, s['icao'], lt['tendency3h']['hPa']), -t3, 'fall3'))
    if not got:
        ob = {'level': 'unknown', 'why': 'no fresh station has a QNH 24 hours and 3 hours earlier to compare%s' % ((' (silent: %s)' % ', '.join(silent_note(stations))) if silent_note(stations) else '')}
    else:
        top = max(cands, key=lambda c: (RANK[c[0]], c[2]))
        ob = {'level': top[0], 'value': round(top[2], 1), 'text': '; '.join(c[1] for c in cands if RANK[c[0]] == RANK[top[0]]) or top[1], 'stationsRead': [s['icao'] for s in fresh], 'readings': [c[1] for c in cands]}
    lv = combine([fc, ob])
    thr = {'forecast': {'watchFall24h': th['fall24'][0], 'alertFall24h': th['fall24'][1], 'unit': 'hPa', 'rule': 'pressure_msl, median of the usable models, 24 hour fall'},
           'observed': {'watchFall24h': th['fall24'][0], 'alertFall24h': th['fall24'][1], 'watchFall3hBeyondTide': th['fall3Adjusted'][0], 'alertFall3hBeyondTide': th['fall3Adjusted'][1], 'unit': 'hPa', 'rule': 'METAR QNH'}}
    basis = _basis('Pressure fall', 'hPa', lv, fc, ob, None, None).replace(' No plan value', ' A fall is a supporting reading: 85 to 88 percent of falls of 4 hPa in 24 hours are not followed by rain within 12 hours, so it raises attention and never sets a stage alone. No plan value')
    return _hazard('pressure', lv, fc, ob, thr, {'label': 'Pressure fall: model forecast (Proposed) and station reports (Reported), a supporting reading', 'basis': basis,
                                                 'caveat': 'the rule of a fall of 3 hPa in 3 hours is not used: 95 percent of such falls are the afternoon pressure tide at 13:00 to 15:00'})


def dust_level(points, stations):
    th = TH['dust']
    # forecast: CAMS dust, and pm10 for the alert
    worst, pm_worst, unread = None, None, []
    for p in points:
        e = p.get('extremes') or {}
        d, pm = e.get('dustMax'), e.get('pm10Max')
        if d is None and not p.get('hourly', {}).get('dust'):
            unread.append(short(p.get('name')))
            continue
        if d and (worst is None or d['v'] > worst['value']):
            worst = {'value': d['v'], 'at': d['t'], 'point': short(p['name'])}
        if pm and (pm_worst is None or pm['v'] > pm_worst['value']):
            pm_worst = {'value': pm['v'], 'at': pm['t'], 'point': short(p['name'])}
    if worst is None:
        fc = {'level': 'unknown', 'why': 'the CAMS dust forecast was not read at any of the %d points' % len(points)}
    else:
        lv = level_by(worst['value'], th['fc'][0], th['fc'][1])
        if pm_worst and pm_worst['value'] >= th['pm10Alert']:
            lv = 'alert'
        fc = dict(worst, level=lv, pm10=pm_worst, unreadPoints=unread, text='CAMS dust reaches %.1f ug/m3 at %s on %s%s (Model forecast, Proposed; CAMS has a high background: median 36 to 43 ug/m3)' % (
            worst['value'], worst['point'], worst['at'].replace('T', ' '), ('; pm10 reaches %.1f ug/m3 at %s' % (pm_worst['value'], pm_worst['point'])) if pm_worst else ''))
        if lv == 'none' and unread:
            fc['level'] = 'unknown'
            fc['why'] = 'dust was not read at %s; %s' % (', '.join(unread), fc['text'])
    # observed
    fresh = fresh_stations(stations)
    if not fresh:
        ob = {'level': 'unknown', 'why': 'no station has reported in the last %g hours%s' % (C.FRESH_H, (' (silent: %s)' % ', '.join(silent_note(stations))) if silent_note(stations) else '')}
    else:
        hits = []
        for s in fresh:
            l3 = s['last3h']
            lv = 'none'
            why = None
            if l3['dust']:
                lv, why = 'watch', 'a dust code in a report of the last %g hours' % C.FRESH_H
                if (l3['dustVisMinM'] is not None and l3['dustVisMinM'] < th['obsVisAlert']):
                    lv, why = 'alert', 'a dust code with visibility %d m (under %d m)' % (l3['dustVisMinM'], th['obsVisAlert'])
                elif l3['dustPeakKmh'] is not None and l3['dustPeakKmh'] >= th['obsPeakAlertKmh']:
                    lv, why = 'alert', 'a dust code with a peak wind of %.0f km/h (%g or more)' % (l3['dustPeakKmh'], th['obsPeakAlertKmh'])
            elif l3['lowVisNoFog']:
                lv, why = 'watch', 'visibility under %d m with no fog, mist or rain reported' % th['obsVisWatch']
            if lv != 'none':
                hits.append((lv, s['icao'], why))
        if hits:
            top = max(hits, key=lambda h: RANK[h[0]])
            ob = {'level': top[0], 'text': '; '.join('%s: %s' % (h[1], h[2]) for h in hits if h[0] == top[0]), 'stations': [h[1] for h in hits], 'stationsRead': [s['icao'] for s in fresh]}
        else:
            ob = {'level': 'none', 'text': 'no dust code and no low visibility without fog, mist or rain in the last %g hours at %s' % (C.FRESH_H, ', '.join(s['icao'] for s in fresh)), 'stationsRead': [s['icao'] for s in fresh]}
    lv = combine([fc, ob])
    thr = {'forecast': {'watch': th['fc'][0], 'alert': th['fc'][1], 'pm10Alert': th['pm10Alert'], 'unit': 'ug/m3', 'rule': 'CAMS dust at 10 m, and pm10 for the alert'},
           'observed': {'watch': 'a dust code (DU, BLDU, DRDU, DS, SS), or visibility under 5,000 m with no fog, mist or rain', 'alert': 'a dust code with visibility under 3,000 m, or dust with a peak wind of 40 km/h or more at the same station'}}
    basis = _basis('Dust', 'ug/m3', lv, fc, ob, None, None).replace(' No plan value', ' Weak: CAMS dust cannot separate the dust hours the airports reported from its own background (the alert level caught 3 of 10, 4 of 21 and 2 of 26 observed episodes since 2022, and about three in four alarms had no dust within 6 hours); the observed signals are better. No plan value')
    return _hazard('dust', lv, fc, ob, thr, {'label': 'Dust: station reports (Reported) and the CAMS forecast (Model forecast, Proposed, weak)', 'basis': basis,
                                             'caveat': 'weak signal: read the station reports and the wind before acting on the forecast alone'})


def visibility_level(stations):
    th = TH['visibility']
    fresh = fresh_stations(stations)
    if not fresh:
        ob = {'level': 'unknown', 'why': 'no station has reported in the last %g hours%s' % (C.FRESH_H, (' (silent: %s)' % ', '.join(silent_note(stations))) if silent_note(stations) else '')}
    else:
        lows = [(s['last3h']['visMinM'], s['icao']) for s in fresh if s['last3h'].get('visMinM') is not None]
        fog = [s['icao'] for s in fresh if s['last3h']['fog']]
        if not lows and not fog:
            ob = {'level': 'unknown', 'why': 'the reports of the last %g hours carry no visibility' % C.FRESH_H}
        else:
            v, icao = min(lows) if lows else (None, None)
            lv = 'alert' if (fog or (v is not None and v <= th['alertAtOrBelow'])) else ('watch' if (v is not None and v < th['watchBelow']) else 'none')
            ob = {'level': lv, 'value': v, 'station': icao, 'fog': fog, 'stationsRead': [s['icao'] for s in fresh],
                  'text': 'the lowest visibility in the last %g hours is %s at %s%s (METAR visibility stops at 10 km: 10,000 m means 10 km or more)' % (
                      C.FRESH_H, ('%d m' % v) if v is not None else 'not reported', icao or 'no station', ('; fog reported at ' + ', '.join(fog)) if fog else '')}
    thr = {'observed': {'watchBelow': th['watchBelow'], 'alertAtOrBelow': th['alertAtOrBelow'], 'unit': 'm', 'rule': 'METAR prevailing visibility, or any FG, BCFG, MIFG, PRFG'}, 'forecast': 'none: model visibility has no skill and is shown, never used'}
    lv = ob['level']
    basis = 'Visibility level %s. Observed: %s. No forecast trigger: model visibility has no skill (best_match found 7 of 85 low visibility hours at OMRK in 92 days). The fog level of the engine is separate. No plan value exists; the thresholds are Proposed (%s).' % (
        lv, ob.get('text') or ('not read well enough (%s)' % ob.get('why')), SRC_FILE)
    return _hazard('visibility', lv, None, ob, thr, {'label': 'Visibility: station reports only (Reported)', 'basis': basis})


def compute_levels(points, stations, now, reads=None):
    """every read only hazard, keyed wind, gust, humidity, heat, cold, pressure, dust, visibility; plus 'summary', the words for a screen"""
    heat, cold = heat_cold(points, stations, now)
    out = {'wind': wind_level(points, stations), 'gust': gust_level(points, stations), 'humidity': humidity_level(points, stations, now, reads), 'heat': heat, 'cold': cold,
           'pressure': pressure_level(points, stations), 'dust': dust_level(points, stations), 'visibility': visibility_level(stations)}
    words = []
    names = {'wind': 'Wind', 'gust': 'Gust', 'humidity': 'Humidity', 'heat': 'Heat', 'cold': 'Cold', 'pressure': 'Pressure fall', 'dust': 'Dust', 'visibility': 'Visibility'}
    for k, nm in names.items():
        lv = out[k]['level']
        words.append('%s: %s' % (nm, 'not read well enough (unknown, never none)' if lv == 'unknown' else lv))
    out['summary'] = words
    out['status'] = 'Proposed'
    out['note'] = 'read only: these levels do not change the weather stage, which stays rain and fog; unknown is never none; no plan value exists for any of them'
    return out


def level_words():
    return ['none', 'watch', 'alert', 'unknown']
