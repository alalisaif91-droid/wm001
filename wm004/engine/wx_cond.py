# -*- coding: utf-8 -*-
"""Conditions for the WM004 weather engine (24 Oct 2026, the request of the department head: "speed wind, RH% everything must reflect here"): wind speed and direction, gusts, relative humidity, temperature,
dew point, pressure, cloud, visibility, CAPE, rain chance, UV and dust, read from the models (status Model forecast, Proposed), from the aviation reports of OMRK, OMSJ and OMFJ
(status Reported) and from the CAMS air quality model (Model forecast, Proposed, weak). Pure functions: no files, no clock of their own; the network only through the fetcher
the engine passes in. Standard library only.

The one rule above all (the plan has no failure tolerance): a reading that is missing is NO READING. It is None in an array and null in a value, never 0, never calm, never clear.
A variable of a model with fewer than 90 percent of the next 72 hours present is not used. A band of models needs 3 models. A call that fails leaves that point with an error
string and nothing else: the values of an earlier run are never carried into a new file.

Sources of the numbers (data/weather/params_calibration.json, the scout of 24 Oct 2026): which variables and models are usable, the models left out of a band, the tide of the
pressure, the thresholds of wx_levels.py."""
import math, datetime as dt, urllib.parse
from wx_util import nz, med, r1, f_local, hour_label, floor_hour, haversine_km, POINTS, TZ_NAME
import wx_models as M
import wx_season as S

OM_URL = 'https://api.open-meteo.com/v1/forecast'
AQ_URL = 'https://air-quality-api.open-meteo.com/v1/air-quality'
ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
COND_PAST_DAYS, COND_FORECAST_DAYS = 2, 7
AQ_PAST_DAYS, AQ_FORECAST_DAYS = 2, 5
WIN = 72
MIN_SHARE = 0.9              # a variable or a model with less than this share of the next 72 hours present is not used (the 90 percent rule of the engine)
MIN_MODELS = 3               # a band needs three models
FRESH_H = 3.0                # a station that has reported in the last three hours is fresh (the engine's SILENT_HOURS)
KT_KMH = 1.852
INHG_HPA = 33.8639
MPS_KMH = 3.6
SCHEMA = 1

# key, Open-Meteo variable, decimals in the file (0 is a whole number). The best_match call, one per point.
BM = [('temp', 'temperature_2m', 1), ('feels', 'apparent_temperature', 1), ('rh', 'relative_humidity_2m', 0), ('dew', 'dew_point_2m', 1), ('ws', 'wind_speed_10m', 1),
      ('wd', 'wind_direction_10m', 0), ('gust', 'wind_gusts_10m', 1), ('pres', 'pressure_msl', 1), ('presSurface', 'surface_pressure', 1), ('cloud', 'cloud_cover', 0),
      ('cloudLow', 'cloud_cover_low', 0), ('vis', 'visibility', 0), ('cape', 'cape', 0), ('pop', 'precipitation_probability', 0), ('code', 'weather_code', 0),
      ('soil', 'soil_moisture_0_to_7cm', 3), ('uv', 'uv_index', 1)]
BM_VAR = {k: v for k, v, _ in BM}
DECIMALS = {k: d for k, _, d in BM}
AQ = [('dust', 'dust', 1), ('pm10', 'pm10', 1), ('pm25', 'pm2_5', 1), ('aod', 'aerosol_optical_depth', 2)]
DECIMALS.update({k: d for k, _, d in AQ})
# arrays of the 72 hours that go into the files; soil moisture, UV and surface pressure are values of the current hour only (UV also as the daily maximum)
HOURLY_KEYS = ['temp', 'feels', 'rh', 'dew', 'ws', 'wd', 'gust', 'pres', 'cloud', 'cloudLow', 'vis', 'cape', 'pop', 'code', 'dust', 'pm10', 'aod']
NOW_KEYS = ['temp', 'feels', 'rh', 'dew', 'ws', 'wd', 'gust', 'pres', 'presSurface', 'cloud', 'cloudLow', 'vis', 'cape', 'pop', 'code', 'soil', 'uv', 'dust', 'pm10', 'pm25', 'aod']
# physical bounds: a value outside is a fault of the feed and is no value
BOUNDS = {'temp': (-40.0, 60.0), 'feels': (-60.0, 75.0), 'rh': (0.0, 100.5), 'dew': (-60.0, 45.0), 'ws': (0.0, 300.0), 'wd': (0.0, 360.0), 'gust': (0.0, 400.0), 'pres': (850.0, 1100.0),
          'presSurface': (400.0, 1100.0), 'cloud': (0.0, 100.0), 'cloudLow': (0.0, 100.0), 'vis': (0.0, 200000.0), 'cape': (0.0, 20000.0), 'pop': (0.0, 100.0), 'code': (0.0, 99.0),
          'soil': (0.0, 1.0), 'uv': (0.0, 20.0), 'dust': (0.0, 20000.0), 'pm10': (0.0, 20000.0), 'pm25': (0.0, 20000.0), 'aod': (0.0, 10.0)}
UNITS = {'time': 'Asia/Dubai local time, YYYY-MM-DDTHH:MM', 'temp': 'deg C', 'feels': 'deg C', 'dew': 'deg C', 'rh': '%', 'ws': 'km/h', 'wd': 'deg (direction the wind comes from)', 'gust': 'km/h',
         'windKt': 'kt', 'pres': 'hPa', 'presSurface': 'hPa', 'presChange': 'hPa', 'cloud': '%', 'cloudLow': '%', 'vis': 'm', 'cape': 'J/kg', 'pop': '%', 'code': 'WMO weather code',
         'soil': 'm3/m3', 'uv': 'index', 'dust': 'ug/m3', 'pm10': 'ug/m3', 'pm25': 'ug/m3', 'aod': 'optical depth, no unit', 'rain': 'mm', 'age': 'minutes', 'distance': 'km',
         'ceiling': 'ft above ground', 'cloudBase': 'ft above ground'}
# the independent models of the bands (the scout validated these six; ecmwf_ifs, the 9 km IFS, is read for rain and fog and is left out of the bands because it is the same family as
# ecmwf_ifs025; jma_seamless and ecmwf_aifs025_single were not scouted and read 4 to 6 deg C cool or return no gust)
BAND_MODELS = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless', 'gem_seamless', 'meteofrance_seamless', 'ukmo_seamless']
BAND_KEYS = ['ws', 'gust', 'temp', 'rh', 'pres']
PUBLISHED_BANDS = ('ws', 'gust', 'temp', 'rh')          # the pressure band is worked out (the 24 hour change and the level read it) and is not in the files: the pressure array of best_match is
BAND_VAR = {'ws': 'wind_speed_10m', 'gust': 'wind_gusts_10m', 'temp': 'temperature_2m', 'rh': 'relative_humidity_2m', 'pres': 'pressure_msl', 'dew': 'dew_point_2m', 'cape': 'cape'}
EXCLUDE = {'gust': {'gfs_seamless': 'its gust is under its own wind speed in 58 percent of hours'},
           'rh': {'meteofrance_seamless': 'it reads 14 to 17 points low against the METAR at OMRK and OMFJ'},
           'dew': {'meteofrance_seamless': 'it reads 3 to 4 deg C dry at OMRK and OMFJ'}}
# the pressure tide: the mean 3 hour change of QNH by the local hour of the later reading, Oct to Mar of seven seasons (params_calibration.json T07, rawFallHoursByClock). A fall of
# 3 hPa in 3 hours is the afternoon tide at 13:00 to 15:00 in 95 percent of cases, so only the change beyond the tide means anything.
TIDE3 = {
    'OMRK': [-0.3, -0.58, -0.74, -0.66, -0.51, -0.1, 0.53, 1.21, 1.66, 1.72, 1.29, 0.29, -1.05, -2.19, -2.55, -2.11, -1.18, -0.16, 0.61, 1.11, 1.34, 1.27, 0.87, 0.28],
    'OMSJ': [-0.31, -0.68, -0.79, -0.72, -0.48, -0.14, 0.52, 1.18, 1.77, 1.87, 1.54, 0.5, -0.84, -2.1, -2.54, -2.22, -1.33, -0.33, 0.46, 1.01, 1.3, 1.25, 0.88, 0.27],
    'OMFJ': [-0.42, -0.79, -0.9, -0.76, -0.45, -0.07, 0.62, 1.29, 1.84, 1.89, 1.5, 0.48, -0.82, -2.04, -2.5, -2.17, -1.35, -0.33, 0.46, 1.05, 1.34, 1.24, 0.82, 0.17]}
WX_WORDS = {'RA': 'rain', 'DZ': 'drizzle', 'SN': 'snow', 'SG': 'snow grains', 'IC': 'ice crystals', 'PL': 'ice pellets', 'GR': 'hail', 'GS': 'small hail', 'UP': 'unknown precipitation',
            'BR': 'mist', 'FG': 'fog', 'FU': 'smoke', 'VA': 'volcanic ash', 'DU': 'dust', 'SA': 'sand', 'HZ': 'haze', 'PO': 'dust whirls', 'SQ': 'squalls', 'FC': 'funnel cloud',
            'SS': 'sandstorm', 'DS': 'duststorm'}
DESCR_WORDS = {'MI': 'shallow', 'BC': 'patches of', 'PR': 'partial', 'DR': 'low drifting', 'BL': 'blowing', 'SH': 'showers of', 'TS': 'thunderstorm with', 'FZ': 'freezing'}
SM_M = 1609.344


# ------------------------------------------------------------------------------------------------ conversions and small arithmetic
def kt_to_kmh(kt):
    return None if kt is None else kt * KT_KMH


def kmh_to_kt(kmh):
    return None if kmh is None else kmh / KT_KMH


def mps_to_kmh(v):
    return None if v is None else v * MPS_KMH


def inhg_to_hpa(v):
    return None if v is None else v * INHG_HPA


def pressure_hpa(v):
    """A pressure as hPa, whatever way it was written. A reading of 850 to 1100 is hPa (the Q group, the AWC altim field); 24 to 33 is inches of mercury (29.90); 2400 to 3300 is the
    digits of an A group (A2990 is 29.90). Anything else is no reading, never a guess."""
    v = nz(v)
    if v is None:
        return None
    if 850.0 <= v <= 1100.0:
        return round(v, 1)
    if 24.0 <= v <= 33.0:
        return round(v * INHG_HPA, 1)
    if 2400.0 <= v <= 3300.0:
        return round(v / 100.0 * INHG_HPA, 1)
    return None


MAGNUS_A, MAGNUS_B = 17.625, 243.04            # Alduchov and Eskridge (1996), for temperatures of -40 to 50 deg C, within 0.4 percent


def magnus_rh(t, td):
    """Relative humidity in percent from air temperature and dew point (deg C): RH = 100 * exp(17.625 * Td / (243.04 + Td)) / exp(17.625 * T / (243.04 + T)).
    A METAR gives whole degrees, so the result is good to a few points. A dew point more than 1 deg C above the temperature cannot be (a fault of the report): no reading. A dew
    point within 1 deg C above it is rounding and reads 100."""
    t, td = nz(t), nz(td)
    if t is None or td is None:
        return None
    if td > t + 1.0:
        return None
    td = min(td, t)
    return round(min(100.0, 100.0 * math.exp(MAGNUS_A * td / (MAGNUS_B + td) - MAGNUS_A * t / (MAGNUS_B + t))), 1)


def circ_diff(a, b):
    """the smallest angle between two directions, 0 to 180"""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def circ_median(degs):
    """The circular median of directions in degrees: the direction of the set that has the least summed angular distance to the others (ties go to the one nearest the circular
    mean). An average of 350 and 10 is 180, which no model said; the median of 350, 355 and 10 is 355. None when the set is empty."""
    xs = [d % 360.0 for d in degs if d is not None]
    if not xs:
        return None
    sx = sum(math.cos(math.radians(x)) for x in xs)
    sy = sum(math.sin(math.radians(x)) for x in xs)
    mean = math.degrees(math.atan2(sy, sx)) % 360.0 if (sx or sy) else None
    best = min(xs, key=lambda c: (round(sum(circ_diff(c, x) for x in xs), 6), circ_diff(c, mean) if mean is not None else 0.0))
    return round(best, 1)


def rnd(x, nd):
    if x is None:
        return None
    return int(round(x)) if nd == 0 else round(x, nd)


def clean_value(key, v):
    """a number in the bounds of its key, or None. Visibility below zero is a sentinel for no value, CAPE below zero is zero."""
    v = nz(v)
    if v is None:
        return None
    if key == 'cape' and v < 0:
        return 0.0
    lo, hi = BOUNDS.get(key, (-1e9, 1e9))
    if v < lo or v > hi:
        return None
    if key == 'rh' and v > 100.0:
        v = 100.0
    return v


def hour_floor_label(t):
    return t.strftime('%Y-%m-%dT%H:00')


def _arr(vals, key):
    nd = DECIMALS.get(key, 1)
    return [rnd(v, nd) for v in vals]


# ------------------------------------------------------------------------------------------------ the calls
def cond_url(lat, lon):
    q = {'latitude': lat, 'longitude': lon, 'hourly': ','.join(v for _, v, _ in BM), 'past_days': COND_PAST_DAYS, 'forecast_days': COND_FORECAST_DAYS, 'timezone': TZ_NAME, 'models': 'best_match', 'wind_speed_unit': 'kmh'}
    return OM_URL + '?' + urllib.parse.urlencode(q, safe=',')


def aq_url(lat, lon):
    q = {'latitude': lat, 'longitude': lon, 'hourly': ','.join(v for _, v, _ in AQ), 'past_days': AQ_PAST_DAYS, 'forecast_days': AQ_FORECAST_DAYS, 'timezone': TZ_NAME}
    return AQ_URL + '?' + urllib.parse.urlencode(q, safe=',')


def archive_url(lat, lon, start, end):
    q = {'latitude': lat, 'longitude': lon, 'start_date': start, 'end_date': end, 'hourly': 'temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,pressure_msl', 'timezone': TZ_NAME}
    return ARCHIVE_URL + '?' + urllib.parse.urlencode(q, safe=',')


def _axis(data, now_label):
    """(times, hourly dict, error). The answer must be on Ras Al Khaimah time and must state it; its axis must reach now."""
    if not isinstance(data, dict):
        return None, None, 'the answer is not an object'
    ok_z, zt, stated = M.zone_check(data)
    if not ok_z:
        return None, None, zt
    if not stated:
        return None, None, 'the answer states neither a time zone nor a utc offset, so its time axis cannot be checked against Ras Al Khaimah time: it is not used'
    h = data.get('hourly') if isinstance(data.get('hourly'), dict) else None
    times = h.get('time') if h else None
    if not isinstance(times, list) or not times or not all(isinstance(t, str) for t in times):
        return None, None, 'the answer has no hourly time axis'
    return times, h, None


def wind_unit_problem(data):
    """text when an answer states a wind speed unit other than km/h (the request asks for kmh; an answer in m/s or mph would be 3.6 or 1.6 times out and would read as calm), else None. An answer that
    states no unit is not refused: the request fixed it."""
    u = data.get('hourly_units') if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    for k in ('wind_speed_10m', 'wind_gusts_10m'):
        if k in u and u[k] != 'km/h':
            return 'the answer states %s in %s and not km/h, so its wind would be out by a factor: it is not used' % (k, str(u[k])[:12])
    return None


def _series(h, var, key, n):
    s = h.get(var)
    if not isinstance(s, list):
        s = h.get(var + '_best_match')
    if not isinstance(s, list):
        return None
    vals = [clean_value(key, x) for x in s]
    vals += [None] * (n - len(vals))
    return vals[:n]


def parse_bestmatch(data, now_label):
    """The best_match answer of one point. Returns {'ok', 'error', 'times', 'i0', 'arr' {key: list over the whole axis or None}, 'unusable' {key: reason}}. A key whose next 72 hours are
    less than 90 percent present is None and named in unusable. A visibility that never changes is a constant of the model, not information, and is not used."""
    times, h, err = _axis(data, now_label)
    if err:
        return {'ok': False, 'error': err}
    err = wind_unit_problem(data)
    if err:
        return {'ok': False, 'error': err}
    i0 = M.first_index(times, now_label)
    if i0 is None:
        return {'ok': False, 'error': 'the time axis of the answer ends before now (%s); a stale answer is not used' % now_label}
    n = len(times)
    arr, unusable = {}, {}
    for key, var, _ in BM:
        vals = _series(h, var, key, n)
        if vals is None:
            arr[key], unusable[key] = None, 'not in the answer'
            continue
        win = vals[i0:i0 + WIN]
        nn = sum(1 for x in win if x is not None)
        if nn < MIN_SHARE * WIN:
            arr[key], unusable[key] = None, 'only %d percent of the next 72 hours has values' % round(100.0 * nn / WIN)
            continue
        if key == 'vis' and max(x for x in win if x is not None) - min(x for x in win if x is not None) == 0:
            arr[key], unusable[key] = None, 'the visibility of the model never changes: not information'
            continue
        arr[key] = vals
    cell = None
    if isinstance(data.get('latitude'), (int, float)) and isinstance(data.get('longitude'), (int, float)):
        cell = (round(data['latitude'], 4), round(data['longitude'], 4))          # the model cell the answer was served from: the wind field check (wx_wind.check_points) compares it with the cell of the nearest grid node
    return {'ok': True, 'error': None, 'times': times, 'i0': i0, 'arr': arr, 'unusable': unusable, 'cell': cell}


def parse_air(data, now_label):
    times, h, err = _axis(data, now_label)
    if err:
        return {'ok': False, 'error': err}
    i0 = M.first_index(times, now_label)
    if i0 is None:
        return {'ok': False, 'error': 'the time axis of the air quality answer ends before now (%s)' % now_label}
    n = len(times)
    arr, unusable = {}, {}
    for key, var, _ in AQ:
        vals = _series(h, var, key, n)
        if vals is None:
            arr[key], unusable[key] = None, 'not in the answer'
            continue
        nn = sum(1 for x in vals[i0:i0 + WIN] if x is not None)
        if nn < MIN_SHARE * min(WIN, max(1, n - i0)) or n - i0 < WIN * MIN_SHARE:
            arr[key], unusable[key] = None, 'only %d of the next 72 hours have values' % nn
            continue
        arr[key] = vals
    cell = None
    if isinstance(data.get('latitude'), (int, float)) and isinstance(data.get('longitude'), (int, float)):
        cell = [round(data['latitude'], 2), round(data['longitude'], 2)]
    return {'ok': True, 'error': None, 'times': times, 'i0': i0, 'arr': arr, 'unusable': unusable, 'cell': cell}


def read_point_conditions(fetcher, idx, now):
    """The two calls of one point: best_match with every variable, and the air quality model. Never raises. A call that fails leaves bm or air with ok False and the reason; the
    other call is still used. 'no fixture' (a test that did not supply it) is skipped, not an error."""
    name, lat, lon = POINTS[idx]
    sl = S.slug(name)
    nl = hour_floor_label(now)
    r = {'idx': idx, 'name': name, 'lat': lat, 'lon': lon, 'slug': sl, 'bm': None, 'air': None, 'bmCall': None, 'airCall': None}
    try:
        data, info = fetcher.get_json('om_cond_' + sl, cond_url(lat, lon))
        r['bmCall'] = info
        r['bm'] = parse_bestmatch(data, nl) if data is not None else {'ok': False, 'error': info.get('error') or 'no answer', 'skipped': info.get('error') == 'no fixture'}
    except Exception as e:                                             # an answer of a shape nobody expected: that point has no conditions, the others are read
        r['bm'] = {'ok': False, 'error': ('the answer could not be read (%s: %s)' % (type(e).__name__, e))[:140]}
    try:
        data, info = fetcher.get_json('om_air_' + sl, aq_url(lat, lon))
        r['airCall'] = info
        r['air'] = parse_air(data, nl) if data is not None else {'ok': False, 'error': info.get('error') or 'no answer', 'skipped': info.get('error') == 'no fixture'}
    except Exception as e:
        r['air'] = {'ok': False, 'error': ('the air quality answer could not be read (%s: %s)' % (type(e).__name__, e))[:140]}
    return r


# ------------------------------------------------------------------------------------------------ bands
def model_lists(pt, key, labels):
    """per model, the series of one variable on the labels, for the models that pass the 90 percent rule and are not left out for this variable. Returns (used, left):
    used {model: list}, left {model: reason}. pt is the read of the point (times and the raw series of every model)."""
    var = BAND_VAR[key]
    times = (pt or {}).get('times') or []
    pos = {t: i for i, t in enumerate(times)}
    series = (pt or {}).get('series') or {}
    used, left = {}, {}
    for m in BAND_MODELS:
        if m in EXCLUDE.get(key, {}):
            left[m] = 'left out: ' + EXCLUDE[key][m]
            continue
        ms = series.get(m)
        s = ms.get(var) if isinstance(ms, dict) else None
        if not isinstance(s, list):
            left[m] = 'no values in the answer'
            continue
        vals = []
        for l in labels:
            j = pos.get(l)
            vals.append(clean_value(key, s[j]) if (j is not None and j < len(s)) else None)
        nn = sum(1 for v in vals if v is not None)
        if nn < MIN_SHARE * len(labels):
            left[m] = 'only %d percent of the next 72 hours has values' % round(100.0 * nn / max(1, len(labels)))
            continue
        used[m] = vals
    return used, left


def make_band(used, n=WIN):
    """min, median and max per hour over the models that have a value at that hour; an hour with fewer than 3 models is None. None when fewer than 3 models are used."""
    if len(used) < MIN_MODELS:
        return None
    lo, md, hi = [], [], []
    for k in range(n):
        xs = [v[k] for v in used.values() if k < len(v) and v[k] is not None]
        if len(xs) >= MIN_MODELS:
            lo.append(min(xs))
            md.append(med(xs))
            hi.append(max(xs))
        else:
            lo.append(None)
            md.append(None)
            hi.append(None)
    return {'min': lo, 'median': md, 'max': hi, 'n': len(used), 'models': sorted(used)}


def point_bands(pt, labels):
    """({key: band or None}, {key: {model: reason}}) for ws, gust, temp, rh and pressure at one point"""
    out, left = {}, {}
    for key in BAND_KEYS:
        used, lf = model_lists(pt, key, labels)
        out[key] = make_band(used, len(labels))
        left[key] = lf
    return out, left


def cape_by_model(pt, labels):
    """the highest CAPE of the next 72 hours of each model (J/kg): the models differ by a factor of 3 and one reads about zero, so CAPE is shown per model and never as one number"""
    times = (pt or {}).get('times') or []
    pos = {t: i for i, t in enumerate(times)}
    out = {}
    for m in M.VOTE_MODELS:
        ms = ((pt or {}).get('series') or {}).get(m)
        s = ms.get('cape') if isinstance(ms, dict) else None
        if not isinstance(s, list):
            continue
        vals = [clean_value('cape', s[pos[l]]) for l in labels if l in pos and pos[l] < len(s)]
        if len(vals) >= MIN_SHARE * len(labels) and sum(1 for v in vals if v is not None) >= MIN_SHARE * len(labels):
            out[m] = int(round(max(v for v in vals if v is not None)))
    return out


# ------------------------------------------------------------------------------------------------ one point
def _ext(vals, labels, mode, nd, src):
    """{'v', 't', 'src'} of the highest or lowest value (the first time it is reached), or None when the series has no value"""
    pairs = [(v, i) for i, v in enumerate(vals or []) if v is not None]
    if not pairs:
        return None
    v, i = (max(pairs, key=lambda p: (p[0], -p[1])) if mode == 'max' else min(pairs, key=lambda p: (p[0], p[1])))
    return {'v': rnd(v, nd), 't': labels[i], 'src': src}


def _headline(bands, bm_arr, key, i0, labels):
    """the series that sets a level and the extremes: the median of the models when 3 or more are used, else best_match alone (said). Returns (series of 72, source text, n models)."""
    b = bands.get(key)
    if b:
        return b['median'], 'median of %d models' % b['n'], b['n']
    a = bm_arr.get(key)
    if a:
        seg = a[i0:i0 + len(labels)]
        seg += [None] * (len(labels) - len(seg))
        return seg, 'best_match alone (under 3 models usable)', 1
    return None, None, 0


def window(arr, i0, n=WIN):
    seg = list(arr[i0:i0 + n])
    seg += [None] * (n - len(seg))
    return seg


def day_stats(times, arr, day):
    """{'tmin', ...} of one calendar day from the arrays of the best_match answer; a quantity with fewer than 22 of the 24 hours is None"""
    idx = [i for i, t in enumerate(times) if t[:10] == day]

    def vals(key):
        a = arr.get(key)
        if a is None:
            return None
        v = [a[i] for i in idx if i < len(a) and a[i] is not None]
        return v if len(v) >= 22 else None
    out = {'date': day}
    t, rh, ws, g, p, c, vi, uv = vals('temp'), vals('rh'), vals('ws'), vals('gust'), vals('pres'), vals('cloud'), vals('vis'), vals('uv')
    out['tmin'] = r1(min(t)) if t else None
    out['tmax'] = r1(max(t)) if t else None
    out['rhMean'] = rnd(sum(rh) / len(rh), 0) if rh else None
    out['rhMin'] = rnd(min(rh), 0) if rh else None
    out['rhMax'] = rnd(max(rh), 0) if rh else None
    out['wsMax'] = r1(max(ws)) if ws else None
    out['gustMax'] = r1(max(g)) if g else None
    out['presMin'] = r1(min(p)) if p else None
    out['presMax'] = r1(max(p)) if p else None
    out['cloudMean'] = rnd(sum(c) / len(c), 0) if c else None
    out['visMin'] = rnd(min(vi), 0) if vi else None
    out['uvMax'] = r1(max(uv)) if uv else None
    return out


def build_point(read, pt, rain_daily, now, stations):
    """The conditions block of one point. read: read_point_conditions; pt: the rain read of the same point (its models' raw series, for the bands); rain_daily: {date: {'median','max'}};
    stations: the station metadata with distKm. A point whose best_match call failed has only name, lat, lon and error: nothing from an earlier run is ever put here."""
    name, lat, lon = read['name'], read['lat'], read['lon']
    bm, aq = read['bm'] or {'ok': False, 'error': 'no answer'}, read['air'] or {'ok': False, 'error': 'no answer'}
    near = None
    if stations:
        s = min(stations, key=lambda m: (m.get('distKm') or {}).get(name, 1e9))
        if (s.get('distKm') or {}).get(name) is not None:
            near = {'icao': s['icao'], 'distKm': s['distKm'][name]}
    if not bm['ok']:
        out = {'i': read['idx'], 'name': name, 'lat': lat, 'lon': lon, 'ok': False, 'error': bm['error'], 'nearestStation': near}
        if aq['ok']:
            out['air'] = _air_only(aq, now)
        if aq.get('error') and not aq['ok']:
            out['airError'] = aq['error']
        out['skipped'] = bool(bm.get('skipped'))
        return out
    times, i0, arr = bm['times'], bm['i0'], bm['arr']
    labels = [times[i0 + k] if i0 + k < len(times) else None for k in range(WIN)]
    if any(l is None for l in labels):
        labels = [l or (dt.datetime.strptime(times[i0], '%Y-%m-%dT%H:%M') + dt.timedelta(hours=k)).strftime('%Y-%m-%dT%H:%M') for k, l in enumerate(labels)]
    hour_start = dt.datetime.strptime(times[i0], '%Y-%m-%dT%H:%M')
    age_min = max(0, int((now - hour_start).total_seconds() // 60))
    # air quality on the labels of the point
    air_arr, air_unusable = {}, {}
    if aq['ok']:
        pos = {t: i for i, t in enumerate(aq['times'])}
        for key in ('dust', 'pm10', 'pm25', 'aod'):
            a = aq['arr'].get(key)
            if a is None:
                air_arr[key] = None
                continue
            seg = [a[pos[l]] if (l in pos and pos[l] < len(a)) else None for l in labels]
            nn = sum(1 for x in seg if x is not None)
            air_arr[key] = seg if nn >= MIN_SHARE * WIN else None
            if air_arr[key] is None:
                air_unusable[key] = 'only %d of the next 72 hours have values' % nn
        air_unusable.update(aq['unusable'])
    else:
        for key in ('dust', 'pm10', 'pm25', 'aod'):
            air_arr[key] = None
        air_unusable = {'dust': aq.get('error') or 'no answer', 'pm10': aq.get('error') or 'no answer'}
    bands, left = point_bands(pt, labels) if pt else ({k: None for k in BAND_KEYS}, {k: {} for k in BAND_KEYS})
    now_vals = {}
    for key in NOW_KEYS:
        if key in ('dust', 'pm10', 'pm25', 'aod'):
            a = air_arr.get(key)
            v = a[0] if a else None
        else:
            a = arr.get(key)
            v = a[i0] if (a and i0 < len(a)) else None
        now_vals[key] = rnd(v, DECIMALS.get(key, 1))
    hourly = {'t': labels}
    for key in HOURLY_KEYS:
        if key in ('dust', 'pm10', 'aod'):
            a = air_arr.get(key)
            hourly[key] = _arr(a, key) if a else None
        else:
            a = arr.get(key)
            hourly[key] = _arr(window(a, i0), key) if a else None
    band_out, band_n, band_left = {}, {}, {}
    for key in BAND_KEYS:
        b = bands.get(key)
        if key in PUBLISHED_BANDS:
            band_out[key] = [_arr(b['min'], key), _arr(b['median'], key), _arr(b['max'], key)] if b else None
        band_n[key] = b['n'] if b else 0
        band_left[key] = {m: r for m, r in (left.get(key) or {}).items() if not r.startswith('left out:')}      # the models left out by rule are named once, in sources.ensemble.leftOut
    # the extremes of the next 72 hours
    ex = {}
    for name_, key, mode in (('gustMax', 'gust', 'max'), ('wsMax', 'ws', 'max'), ('rhMin', 'rh', 'min'), ('rhMax', 'rh', 'max'), ('tempMax', 'temp', 'max'), ('tempMin', 'temp', 'min')):
        series, src, n = _headline(bands, arr, key, i0, labels)
        e = _ext(series, labels, mode, DECIMALS[key], src) if series else None
        if e is not None and key in ('gust', 'ws') and bands.get(key):
            hi = _ext(bands[key]['max'], labels, 'max', 1, 'highest of %d models' % bands[key]['n'])
            e['highest'] = hi
        ex[name_] = e
    vis_a = arr.get('vis')
    ex['visMin'] = _ext(window(vis_a, i0), labels, 'min', 0, 'best_match') if vis_a else None
    cape_a = arr.get('cape')
    ex['capeMax'] = _ext(window(cape_a, i0), labels, 'max', 0, 'best_match') if cape_a else None
    dust_a = air_arr.get('dust')
    ex['dustMax'] = _ext(dust_a, labels, 'max', 1, 'CAMS') if dust_a else None
    pm_a = air_arr.get('pm10')
    ex['pm10Max'] = _ext(pm_a, labels, 'max', 1, 'CAMS') if pm_a else None
    series, src, n = _headline(bands, arr, 'pres', i0, labels)
    p6 = p24 = worst = None
    if series:
        if series[0] is not None and series[6] is not None:
            p6 = {'v': round(series[6] - series[0], 1), 'from': labels[0], 'to': labels[6], 'src': src}
        if series[0] is not None and series[24] is not None:
            p24 = {'v': round(series[24] - series[0], 1), 'from': labels[0], 'to': labels[24], 'src': src}
        falls = [(series[k + 24] - series[k], k) for k in range(0, WIN - 24) if series[k] is not None and series[k + 24] is not None]
        if falls:
            v, k = min(falls)
            worst = {'v': round(v, 1), 'from': labels[k], 'to': labels[k + 24], 'src': src}
    ex['presChange6h'], ex['presChange24h'], ex['presFall24hWorst'] = p6, p24, worst
    # the seven days
    today = now.strftime('%Y-%m-%d')
    days = [(dt.datetime.strptime(today, '%Y-%m-%d') + dt.timedelta(days=k)).strftime('%Y-%m-%d') for k in range(COND_FORECAST_DAYS)]
    daily = []
    for d in days:
        row = day_stats(times, arr, d)
        rd = (rain_daily or {}).get(d) or {}
        row['rain'] = rd.get('median')
        row['rainMax'] = rd.get('max')
        daily.append(row)
    unusable = {k: v for k, v in bm['unusable'].items()}
    for k, v in air_unusable.items():
        unusable[k] = v
    return {'i': read['idx'], 'name': name, 'lat': lat, 'lon': lon, 'ok': True, 'nearestStation': near,
            'now': dict(now_vals, at=times[i0], ageMin=age_min), 'hourly': hourly, 'bands': band_out, 'bandN': band_n, 'daily': daily, 'extremes': ex,
            'capeByModel': cape_by_model(pt, labels) if pt else {}, 'unusable': unusable, 'bandLeftOut': {k: v for k, v in band_left.items() if v},
            'airCell': aq.get('cell') if aq['ok'] else None,
            '_labels': labels, '_bands': bands, '_arr': arr, '_i0': i0, '_air': air_arr, '_times': times, '_cell': bm.get('cell')}


def _air_only(aq, now):
    return {'cell': aq.get('cell'), 'note': 'the best_match call failed at this point; only the air quality answer was read'}


# ------------------------------------------------------------------------------------------------ the stations
def plain_wx(wx):
    """present weather groups (parsed) in plain words: 'heavy thunderstorm with rain', 'blowing dust', 'mist'; empty when there is no weather"""
    words = []
    for w in wx or []:
        parts = []
        if w.get('vicinity'):
            parts.append('in the vicinity:')
        elif w.get('intensity') == '+':
            parts.append('heavy')
        elif w.get('intensity') == '-':
            parts.append('light')
        for d in w.get('descr') or []:
            parts.append(DESCR_WORDS.get(d, d))
        for p in w.get('phen') or []:
            parts.append(WX_WORDS.get(p, p))
        words.append(' '.join(parts).replace(': ', ': ').strip())
    return '; '.join(words)


def ceiling_ft(row):
    """the lowest broken or overcast layer, or the vertical visibility, in feet above ground; None when there is none (a clear sky and CAVOK have no ceiling)"""
    bases = [c['base_ft'] for c in (row.get('clouds') or []) if c.get('cover') in ('BKN', 'OVC') and c.get('base_ft') is not None]
    if row.get('vv_ft') is not None and row['vv_ft'] >= 0:
        bases.append(row['vv_ft'])
    return min(bases) if bases else None


def flight_category(row):
    """VFR, MVFR, IFR or LIFR from the visibility and the ceiling (the US definition the AWC uses: LIFR under 1 mile or 500 ft; IFR under 3 miles or 1,000 ft; MVFR up to 5 miles or 3,000 ft). The
    category the service sends is used when it is one of the four; otherwise it is worked out; None when there is no visibility."""
    fc = row.get('flt_cat')
    if fc in ('VFR', 'MVFR', 'IFR', 'LIFR'):
        return fc, 'aviationweather.gov'
    v = row.get('vis_m')
    if v is None:
        return None, None
    c = ceiling_ft(row)
    if v < 1 * SM_M or (c is not None and c < 500):
        return 'LIFR', 'worked out'
    if v < 3 * SM_M or (c is not None and c < 1000):
        return 'IFR', 'worked out'
    if v <= 5 * SM_M or (c is not None and c <= 3000):
        return 'MVFR', 'worked out'
    return 'VFR', 'worked out'


def row_values(row):
    """the figures of one METAR row in the units of the file. Calm is speed 0 with no direction; a variable wind (VRB) has speed and no direction; a gust that was not reported is None,
    never zero. Pressure is the Q group, or the A group in inches converted to hPa. RH is worked out from the whole degrees of temperature and dew point (Magnus)."""
    kt, gk = nz(row.get('wind_kt')), nz(row.get('gust_kt'))
    vrb = bool(row.get('wind_vrb'))
    d = nz(row.get('wind_dir'))
    calm = (kt == 0 and not vrb)
    if calm or vrb:
        d = None
    elif d is not None and d == 0:
        d = 360.0
    peak_kt = max(x for x in (kt, gk) if x is not None) if (kt is not None or gk is not None) else None
    t, td = nz(row.get('temp_c')), nz(row.get('dew_c'))
    q, q_src = pressure_hpa(row.get('qnh_hpa')), 'Q group'
    if q is None and nz(row.get('altim_inhg')) is not None:
        q, q_src = pressure_hpa(row.get('altim_inhg')), 'A group (inches of mercury, converted)'
    if q is None:
        q_src = None
    return {'dir': None if d is None else int(round(d)), 'calm': calm, 'vrb': vrb, 'kt': None if kt is None else int(round(kt)), 'kmh': None if kt is None else r1(kt_to_kmh(kt)),
            'gustKt': None if gk is None else int(round(gk)), 'gustKmh': None if gk is None else r1(kt_to_kmh(gk)), 'gustReported': gk is not None,
            'peakKmh': None if peak_kt is None else r1(kt_to_kmh(peak_kt)), 'temp': None if t is None else r1(t), 'dew': None if td is None else r1(td), 'rh': magnus_rh(t, td),
            'qnh': q, 'qnhSource': q_src, 'vis': None if row.get('vis_m') is None else int(row['vis_m']), 'visCap': bool(row.get('vis_cap')) or (row.get('vis_m') == 10000)}


def _nearest(rows, target, tol_min, ok):
    best = None
    for r in rows:
        t = r.get('time_local')
        if t is None or not ok(r):
            continue
        d = abs((t - target).total_seconds()) / 60.0
        if d <= tol_min and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def tide_adjust(icao, hour, raw_change):
    """the 3 hour change of QNH with the mean change of that hour of the day taken out; None when the station has no tide table"""
    t = TIDE3.get(icao)
    if t is None or raw_change is None:
        return None
    return round(raw_change - t[hour % 24], 2)


def station_block(meta, rows, now):
    """One station: the latest report in full, the last 24 hours as hourly arrays, the extremes of today and of the 24 hours, the pressure tendency, and the last three hours for the levels.
    Reported (METAR and SPECI, aviationweather.gov). A station with no report in the answer is {'ok': False, 'error'}: no reading, never calm."""
    icao = meta['icao']
    out = {'icao': icao, 'name': meta.get('name'), 'lat': meta.get('lat'), 'lon': meta.get('lon'), 'elev': meta.get('elev'), 'side': meta.get('side'), 'distKm': meta.get('distKm'),
           'status': 'Reported', 'ok': False}
    rs = sorted([r for r in rows if r.get('time_local') is not None and r['time_local'] <= now + dt.timedelta(minutes=10)], key=lambda r: r['time_local'])
    if not rs:
        out['error'] = 'no report in the answer'
        return out
    last = rs[-1]
    age = int(round((now - last['time_local']).total_seconds() / 60.0))
    v = row_values(last)
    fc, fc_src = flight_category(last)
    clouds = [{'cover': c['cover'], 'baseFt': c['base_ft'], 'type': c.get('type')} for c in (last.get('clouds') or [])]
    cloud_text = 'CAVOK' if last.get('cavok') else ('no significant cloud' if (last.get('nsc') and not clouds) else (' '.join('%s%s%s' % (c['cover'], '%03d' % (c['baseFt'] // 100) if c['baseFt'] is not None else '///', c['type'] or '') for c in clouds) or None))
    # tendency: the 3 hour change of QNH (raw, and with the tide of the hour taken out) and the 24 hour change
    has_q = lambda r: row_values(r)['qnh'] is not None
    p3 = _nearest(rs, last['time_local'] - dt.timedelta(hours=3), 40, has_q)
    p24 = _nearest(rs, last['time_local'] - dt.timedelta(hours=24), 60, has_q)
    t3 = t24 = None
    if v['qnh'] is not None and p3 is not None and p3 is not last:
        raw = round(v['qnh'] - row_values(p3)['qnh'], 1)
        span = round((last['time_local'] - p3['time_local']).total_seconds() / 60.0)
        adj = tide_adjust(icao, last['time_local'].hour, raw) if span and abs(span - 180) <= 40 else None
        t3 = {'hPa': raw, 'spanMin': span, 'from': f_local(p3['time_local']), 'tideAdjustedHPa': adj,
              'note': 'the raw 3 hour change is mostly the daily pressure tide (about minus 2.5 hPa at 14:00 and plus 1.7 at 09:00); only the change beyond the tide means anything'}
    if v['qnh'] is not None and p24 is not None and p24 is not last:
        t24 = {'hPa': round(v['qnh'] - row_values(p24)['qnh'], 1), 'from': f_local(p24['time_local'])}
    latest = {'time': f_local(last['time_local']), 'ageMin': age, 'type': last.get('type'), 'wind': {'dir': v['dir'], 'calm': v['calm'], 'variable': v['vrb'], 'kt': v['kt'], 'kmh': v['kmh'], 'gustKt': v['gustKt'],
                                                                                                   'gustKmh': v['gustKmh'], 'gustReported': v['gustReported'], 'peakKmh': v['peakKmh'],
                                                                                                   'text': ('calm' if v['calm'] else ('variable, %s kt' % v['kt'] if v['vrb'] else None))},
              'temp': v['temp'], 'dew': v['dew'], 'spread': None if (v['temp'] is None or v['dew'] is None) else r1(v['temp'] - v['dew']), 'rh': v['rh'], 'qnh': v['qnh'], 'qnhSource': v['qnhSource'],
              'tendency3h': t3, 'change24h': t24, 'vis': v['vis'], 'visCap': v['visCap'], 'cavok': bool(last.get('cavok')), 'cloud': clouds, 'cloudText': cloud_text, 'ceilingFt': ceiling_ft(last),
              'wx': last.get('wx_text') or None, 'wxText': plain_wx(last.get('wx')) or None, 'fltCat': fc, 'fltCatSource': fc_src, 'raw': last.get('raw')}
    # the last 24 hours as hourly slots; the last slot is the current hour
    cur = floor_hour(now)
    slots = [cur - dt.timedelta(hours=23 - k) for k in range(24)]
    arrays = {k: [] for k in ('dir', 'kt', 'kmh', 'gustKmh', 'peakKmh', 'temp', 'dew', 'rh', 'pres', 'vis')}
    labels = []
    for s in slots:
        labels.append(s.strftime('%Y-%m-%dT%H:00'))
        in_slot = [r for r in rs if s <= r['time_local'] < s + dt.timedelta(hours=1)]
        if not in_slot:
            for k in arrays:
                arrays[k].append(None)
            continue
        main = min(in_slot, key=lambda r: (r['time_local'] - s))                     # the report nearest the top of the hour is the hourly sample
        mv = row_values(main)
        arrays['dir'].append(mv['dir'])
        arrays['kt'].append(mv['kt'])
        arrays['kmh'].append(mv['kmh'])
        gs = [row_values(r)['gustKmh'] for r in in_slot if row_values(r)['gustKmh'] is not None]
        pk = [row_values(r)['peakKmh'] for r in in_slot if row_values(r)['peakKmh'] is not None]
        arrays['gustKmh'].append(max(gs) if gs else None)                           # no gust reported in the hour is None, not zero
        arrays['peakKmh'].append(max(pk) if pk else None)
        arrays['temp'].append(mv['temp'])
        arrays['dew'].append(mv['dew'])
        arrays['rh'].append(mv['rh'])
        arrays['pres'].append(mv['qnh'])
        arrays['vis'].append(mv['vis'])
    hours = dict(arrays, t=labels)

    def ext(rowset):
        vals = [row_values(r) for r in rowset]
        pick = lambda k: [x[k] for x in vals if x[k] is not None]
        t, rh, ws, pk, q, vi = pick('temp'), pick('rh'), pick('kmh'), pick('peakKmh'), pick('qnh'), pick('vis')
        gu = pick('gustKmh')
        return {'n': len(rowset), 'tMax': max(t) if t else None, 'tMin': min(t) if t else None, 'rhMin': min(rh) if rh else None, 'rhMax': max(rh) if rh else None,
                'windMaxKmh': max(ws) if ws else None, 'peakMaxKmh': max(pk) if pk else None, 'gustMaxKmh': max(gu) if gu else None, 'presMin': min(q) if q else None, 'presMax': max(q) if q else None,
                'visMinM': min(vi) if vi else None}
    last24 = [r for r in rs if r['time_local'] >= now - dt.timedelta(hours=24)]
    today = [r for r in rs if r['time_local'].date() == now.date()]
    last3 = [r for r in rs if r['time_local'] >= now - dt.timedelta(hours=FRESH_H)]
    l3 = ext(last3)
    fl = lambda r: r.get('flags') or {}
    dust_rows = [r for r in last3 if fl(r).get('dust')]
    l3.update({'dust': bool(dust_rows), 'dustVisMinM': min([r['vis_m'] for r in dust_rows if r.get('vis_m') is not None] or [None]) if any(r.get('vis_m') is not None for r in dust_rows) else None,
               'dustPeakKmh': max([row_values(r)['peakKmh'] for r in dust_rows if row_values(r)['peakKmh'] is not None] or [None]) if any(row_values(r)['peakKmh'] is not None for r in dust_rows) else None,
               'lowVisNoFog': any((r.get('vis_m') is not None and r['vis_m'] < 5000 and not fl(r).get('fog') and not fl(r).get('mist') and not fl(r).get('rain_any')) for r in last3),
               'fog': any(fl(r).get('fog') or r.get('vv_ft') is not None for r in last3), 'rain': any(fl(r).get('rain_any') for r in last3)})
    out.update({'ok': True, 'reportTime': latest['time'], 'ageMin': age, 'fresh': age <= FRESH_H * 60, 'latest': latest, 'hours': hours, 'extremes24': ext(last24), 'today': ext(today), 'last3h': l3,
                'reports24': len(last24), 'note': 'an airport, not a rain point; METAR wind is a 10 minute mean in whole knots, temperature and dew point are whole degrees, pressure is the QNH in whole hPa; '
                                                  'a gust is in a report only when it passes the mean by 10 kt (0.1 to 0.7 percent of reports), so no gust means none reported, never calm'})
    return out


def station_blocks(stations, rows_by_station, now):
    out = {}
    for m in stations:
        try:
            out[m['icao']] = station_block(m, rows_by_station.get(m['icao'], []), now)
        except Exception as e:                                         # one station whose reports cannot be read is a station with no reading
            out[m['icao']] = {'icao': m['icao'], 'name': m.get('name'), 'ok': False, 'status': 'Reported', 'error': 'the reports could not be read (%s: %s)' % (type(e).__name__, str(e)[:60])}
    return out


# ------------------------------------------------------------------------------------------------ model against observed
def model_at(pt_block, when):
    """the best_match values of the hour nearest to `when` (a datetime) from the arrays of a point, or None"""
    if not pt_block.get('ok'):
        return None
    times, arr = pt_block['_times'], pt_block['_arr']
    lab = hour_floor_label(when + dt.timedelta(minutes=30))
    try:
        j = times.index(lab)
    except ValueError:
        return None
    g = lambda k: (arr[k][j] if arr.get(k) and j < len(arr[k]) else None)
    return {'at': lab, 'temp': g('temp'), 'rh': g('rh'), 'ws': g('ws'), 'gust': g('gust'), 'pres': g('pres')}


def comparison(pt_block, stn_blocks, now):
    """Model now against observed now at the nearest station (Recomputed). The station is an airport at the distance given, not the rain point. The model value is best_match at the hour
    nearest the report; the observed wind is the 10 minute mean of the report. 29 Oct 2026 (COND-GUST-KEY): observed.gust is the gust the report carries and is null when it carries none (a METAR
    carries a gust in under 1 percent of reports); the peak of the report (the higher of the mean and a reported gust) has its own key, observed.peak, and diff.peak is the model gust minus that
    peak. The gust key used to hold the peak, so a report with no gust read as a gust equal to the mean wind. A station with no fresh report gives no comparison."""
    near = pt_block.get('nearestStation')
    if not near:
        return {'ok': False, 'error': 'no station is known for this point'}
    s = stn_blocks.get(near['icao'])
    if not s or not s.get('ok'):
        return {'ok': False, 'station': near['icao'], 'distKm': near['distKm'], 'error': 'no report from %s' % near['icao']}
    if not s.get('fresh'):
        return {'ok': False, 'station': near['icao'], 'distKm': near['distKm'], 'error': '%s has not reported for %.1f hours' % (near['icao'], s['ageMin'] / 60.0)}
    when = dt.datetime.strptime(s['latest']['time'], '%Y-%m-%d %H:%M')
    m = model_at(pt_block, when)
    if not m:
        return {'ok': False, 'station': near['icao'], 'distKm': near['distKm'], 'error': 'the model has no value for the hour of the report'}
    lt = s['latest']
    wd = lt['wind']
    obs = {'temp': lt['temp'], 'rh': lt['rh'], 'ws': wd['kmh'], 'gust': wd.get('gustKmh') if wd.get('gustReported') else None, 'peak': wd.get('peakKmh'), 'pres': lt['qnh']}
    mod = {'temp': m['temp'], 'rh': m['rh'], 'ws': m['ws'], 'gust': m['gust'], 'pres': m['pres']}
    diff = {k: (None if (mod[k] is None or obs[k] is None) else round(mod[k] - obs[k], 1)) for k in mod}
    diff['peak'] = None if (mod['gust'] is None or obs['peak'] is None) else round(mod['gust'] - obs['peak'], 1)
    return {'ok': True, 'status': 'Recomputed', 'station': near['icao'], 'distKm': near['distKm'], 'elevM': s.get('elev'), 'reportTime': lt['time'], 'modelHour': m['at'], 'model': {k: rnd(v, 1) if v is not None else None for k, v in mod.items()},
            'observed': obs, 'diff': diff}


# ------------------------------------------------------------------------------------------------ the whole block
def source_state(reads, key):
    """per call kind: how many points answered, which did not, and the reasons (skipped when a test supplied nothing)"""
    n = len(reads)
    items = [(r['name'], r.get(key) or {'ok': False, 'error': 'no answer'}) for r in reads]
    if all(x.get('skipped') for _, x in items):
        return {'status': 'skipped', 'pointsRead': 0, 'pointsTotal': n, 'failed': [], 'errors': []}
    ok = [nm for nm, x in items if x.get('ok')]
    bad = [(nm, x.get('error') or 'no answer') for nm, x in items if not x.get('ok')]
    errs = []
    for _, e in bad:
        if e not in errs:
            errs.append(e)
    return {'status': 'ok' if len(ok) == n else ('partial' if ok else 'error'), 'pointsRead': len(ok), 'pointsTotal': n, 'failed': [nm.split(' (')[0] for nm, _ in bad], 'errors': errs[:3]}


PARAM_NAMES = [('temp', 'temperature'), ('feels', 'apparent temperature'), ('rh', 'relative humidity'), ('dew', 'dew point'), ('ws', 'wind speed'), ('wd', 'wind direction'), ('gust', 'wind gust'),
               ('pres', 'pressure'), ('cloud', 'cloud cover'), ('vis', 'visibility'), ('cape', 'CAPE'), ('pop', 'rain chance'), ('dust', 'dust')]


def parameter_health(points, stations):
    """for every parameter: read (all points), partial (some points) or unknown (no point). The stations are counted apart: a parameter observed at the airports is read when a fresh station has it."""
    out = {}
    n = len(points)
    for key, label in PARAM_NAMES:
        got = 0
        for p in points:
            if not p.get('ok') and key != 'dust':
                continue
            if key == 'dust':
                if (p.get('hourly') or {}).get('dust') or (p.get('air') or {}).get('ok'):
                    got += 1
                continue
            if (p.get('hourly') or {}).get(key) is not None:
                got += 1
        out[label] = 'read' if got == n and n else ('partial: %d of %d points' % (got, n) if got else 'unknown')
    st_ok = [s for s in stations.values() if s.get('ok') and s.get('fresh')]
    obs = {}
    for label, f in (('wind', lambda s: s['latest']['wind']['kmh'] is not None), ('temperature', lambda s: s['latest']['temp'] is not None), ('dew point', lambda s: s['latest']['dew'] is not None),
                     ('relative humidity', lambda s: s['latest']['rh'] is not None), ('pressure', lambda s: s['latest']['qnh'] is not None), ('visibility', lambda s: s['latest']['vis'] is not None)):
        got = sum(1 for s in st_ok if f(s))
        obs[label] = 'read' if got == len(stations) and got else ('partial: %d of %d stations' % (got, len(stations)) if got else 'unknown')
    out['_stations'] = obs
    return out


def public_point(p, drop=()):
    """a point block without the working keys (those that start with an underscore); drop names keys to leave out as well"""
    return {k: v for k, v in p.items() if not k.startswith('_') and k not in drop}
