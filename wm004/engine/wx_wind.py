# -*- coding: utf-8 -*-
"""The wind field of the WM004 weather engine (24 Oct 2026, the request of the department head: "wind movement is required, show it in the map"): the model wind on a regular grid over the emirate, so that the map can
show the wind moving and not only eight numbers. Status Model forecast (Proposed). Pure functions: no files, no clock of their own; the network only through the fetcher the engine passes in.
Standard library only.

The grid. 0.1 degree, longitude 55.55 to 56.45 (10 columns) and latitude 24.75 to 26.15 (15 rows): 150 nodes, with the two rain map panels (north latitude 25.56 to 26.10, south 24.82 to
25.36, longitude 55.68 to 56.32) inside it. 0.1 degree is about 10 km, which is the resolution of the model: a node is a model value, never a measurement, and the nodes that fall on the
same model cell carry the same wind (the block says how many distinct cells lie under the 150 nodes). Nothing finer is claimed.

The call. Open-Meteo takes a comma separated list of locations in one call and answers with a list in the order asked. The documented limit is 100 locations a call (a test of 24 Oct 2026 saw
150 accepted, which is not relied on): the 150 nodes go in two calls, 100 and 50. best_match, hourly wind_speed_10m (km/h), wind_direction_10m and wind_gusts_10m, from the hour before
now (past_hours 1) for 48 hours (forecast_hours 48): 49 hourly stamps.

The convention (written out in the file as well). Direction is where the wind comes FROM, degrees clockwise from north. u is toward the east and v toward the north, so that
u = -speed * sin(direction) and v = -speed * cos(direction): a wind from the north (0) blows toward the south (v negative), a wind from the east (90) blows toward the west (u negative).
Calm is a speed of 0 and gives u = v = 0. A speed with no direction is not calm and gives no u and no v. u, v and the gust are stored as whole numbers in steps of 0.1 km/h (value times 10).

The one rule above all (the plan has no failure tolerance): a node that failed, or has no value at an hour, is null, never 0, never calm. A call that fails leaves the nodes it carried null;
nothing from an earlier run is ever put in a new file. When no node was read the block carries no values and says so."""
import math, urllib.parse, datetime as dt
from wx_util import nz, f_local, haversine_km, POINTS, r1
import wx_models as M
import wx_cond as C

OM_URL = C.OM_URL
LAT0, LON0 = 24.75, 55.55
DLAT, DLON = 0.1, 0.1
NX, NY = 10, 15
N_NODES = NX * NY
MAX_PER_CALL = 100                       # the documented limit of locations in one call
PAST_HOURS, FORECAST_HOURS = 1, 48
SCALE = 10                               # integers in steps of 0.1 km/h
VARS = 'wind_speed_10m,wind_direction_10m,wind_gusts_10m'
MIN_SHARE = C.MIN_SHARE                  # a node with fewer than 90 percent of the hours present is not used (the rule of the points)
STATUS = 'Model forecast (Proposed)'
MODEL_TEXT = 'Open-Meteo best_match: the best model Open-Meteo has for each node (the answer does not name it)'
CONVENTION = ('direction is where the wind comes FROM, degrees clockwise from north; u is toward the east and v toward the north: u = -speed * sin(direction), v = -speed * cos(direction); '
              'a wind from the north (0) has v negative, a wind from the east (90) has u negative; calm is a speed of 0 and gives u = 0 and v = 0; a speed with no direction gives null; '
              'u, v and gust are whole numbers in steps of 0.1 km/h (divide by 10 for km/h); a node or an hour with no reading is null, never 0')
ORDER = 'row major from the south west corner: node = j * nx + i, i along longitude (east), j along latitude (north); node latitude = lat0 + j * dlat, node longitude = lon0 + i * dlon'
# the schedules of the hours a file may hold, richest first: (name, hours up to which every hour is kept, text). Beyond that hour every third hour is kept, and the last stamp always.
SCHEDULES = [('hourly', None, 'every hour'),
             ('h24_3h', 24, 'every hour to 24 hours ahead, then every 3 hours'),
             ('h12_3h', 12, 'every hour to 12 hours ahead, then every 3 hours'),
             ('h6_3h', 6, 'every hour to 6 hours ahead, then every 3 hours'),
             ('3h', 0, 'every 3 hours')]
DEFAULT_SCHEDULE = 'h24_3h'              # 150 nodes x 49 hourly stamps is 76 to 90 KB and cannot sit in a file of 200 KB beside the eight points (about 138 KB)
WEATHER_AHEAD_H = 6                      # weather.json carries the current hour and the next 6 hours
CHECK_TOL_KMH = 0.2                      # two readings of one model cell may differ by the rounding of each (0.05 km/h) and no more
CHECK_TOL_DEG = 1.5


# ------------------------------------------------------------------------------------------------ the grid
def node_ij(n):
    return n % NX, n // NX


def node_index(i, j):
    return j * NX + i


def node_latlon(n):
    i, j = node_ij(n)
    return round(LAT0 + j * DLAT, 2), round(LON0 + i * DLON, 2)


def grid_block():
    return {'lat0': LAT0, 'lon0': LON0, 'dlat': DLAT, 'dlon': DLON, 'nx': NX, 'ny': NY, 'nodes': N_NODES, 'order': ORDER,
            'lat1': round(LAT0 + (NY - 1) * DLAT, 2), 'lon1': round(LON0 + (NX - 1) * DLON, 2)}


def nearest_node(lat, lon):
    """(node, distance in km) of the node nearest a place, or None when the place lies more than half a cell outside the grid"""
    fi, fj = (lon - LON0) / DLON, (lat - LAT0) / DLAT
    if fi < -0.5 or fi > NX - 0.5 or fj < -0.5 or fj > NY - 0.5:
        return None
    i, j = min(NX - 1, max(0, int(math.floor(fi + 0.5)))), min(NY - 1, max(0, int(math.floor(fj + 0.5))))
    n = node_index(i, j)
    la, lo = node_latlon(n)
    return n, round(haversine_km(lat, lon, la, lo), 1)


def split_calls(n_nodes=N_NODES, max_per_call=MAX_PER_CALL):
    """The nodes in as few calls as the limit allows, on whole rows where it can: [[0..99], [100..149]] for the 150 nodes. No call holds more than max_per_call locations, every node is in
    exactly one call, in order."""
    per = (max_per_call // NX) * NX or max_per_call
    return [list(range(s, min(s + per, n_nodes))) for s in range(0, n_nodes, per)]


def wind_url(idxs):
    q = {'latitude': ','.join('%.2f' % node_latlon(n)[0] for n in idxs), 'longitude': ','.join('%.2f' % node_latlon(n)[1] for n in idxs), 'hourly': VARS, 'past_hours': PAST_HOURS,
         'forecast_hours': FORECAST_HOURS, 'timezone': 'Asia/Dubai', 'models': 'best_match', 'wind_speed_unit': 'kmh'}
    return OM_URL + '?' + urllib.parse.urlencode(q, safe=',')


# ------------------------------------------------------------------------------------------------ the arithmetic
def uv(speed, direction):
    """(u, v) in km/h from a speed (km/h) and the direction the wind comes from (degrees). Calm (speed 0) is (0, 0) whatever the direction. A speed with no direction, or no speed, is (None, None)."""
    s = nz(speed)
    if s is None or s < 0:
        return None, None
    if s == 0:
        return 0.0, 0.0
    d = nz(direction)
    if d is None:
        return None, None
    r = math.radians(d % 360.0)
    return -s * math.sin(r), -s * math.cos(r)


def pack(x):
    """a number in km/h as the whole number of the file (0.1 km/h steps); None stays None"""
    return None if x is None else int(round(x * SCALE))


def unpack(i):
    return None if i is None else i / float(SCALE)


def from_uv(u, v):
    """(speed km/h, direction the wind comes from in degrees 0 to 360) of a u and v in km/h; direction None for calm or no value"""
    if u is None or v is None:
        return None, None
    s = math.hypot(u, v)
    if s == 0:
        return 0.0, None
    return s, (math.degrees(math.atan2(-u, -v)) % 360.0)


def decode(wf, h, n):
    """(speed, direction from, gust) in km/h and degrees of node n at hour index h of a windfield block, or Nones"""
    s, d = from_uv(unpack(wf['u'][h][n]), unpack(wf['v'][h][n]))
    return s, d, unpack(wf['gust'][h][n])


# ------------------------------------------------------------------------------------------------ reading
def window_labels(now):
    """the hour stamps a good answer holds: the hour before the current hour to 47 hours after it (49 stamps)"""
    base = now.replace(minute=0, second=0, microsecond=0)
    return [(base + dt.timedelta(hours=k)).strftime('%Y-%m-%dT%H:00') for k in range(-PAST_HOURS, FORECAST_HOURS)]


def parse_chunk(data, idxs, now_label, labels):
    """One call. Returns {'ok', 'error', 'times' (the stamps used), 'nodes' {node: {'ws', 'wd', 'gust', 'cell'}}, 'dropped' [node], 'badNodes' [(node, why)]}. The answer holds one object per location in the
    order asked, or one object when one location was asked. The count must match, because the order is the only thing that says which node an object is. A node whose object is not on Ras Al
    Khaimah time, or whose axis differs from the first one, or that has under 90 percent of the hours, is null. The axis must hold the current hour."""
    objs = data if isinstance(data, list) else ([data] if isinstance(data, dict) else None)
    if objs is None:
        return {'ok': False, 'error': 'the answer is neither a list nor an object'}
    if len(objs) != len(idxs):
        return {'ok': False, 'error': 'the answer holds %d locations and %d were asked for, so its order cannot be trusted: it is not used' % (len(objs), len(idxs))}
    axis, nodes, bad, dropped = None, {}, [], []
    for n, o in zip(idxs, objs):
        times, h, err = C._axis(o, now_label)
        if err:
            bad.append((n, err))
            continue
        err = C.wind_unit_problem(o)
        if err:
            bad.append((n, err))
            continue
        if axis is None:
            axis = times
            if now_label not in axis:
                return {'ok': False, 'error': 'the time axis of the answer does not hold the current hour (%s): a stale or a shifted answer is not used' % now_label}
        elif times != axis:
            bad.append((n, 'its time axis differs from the first location of the call'))
            continue
        pos = {t: i for i, t in enumerate(axis)}
        cols = {}
        for key, var in (('ws', 'wind_speed_10m'), ('wd', 'wind_direction_10m'), ('gust', 'wind_gusts_10m')):
            s = C._series(h, var, key, len(axis))
            cols[key] = None if s is None else [s[pos[l]] if l in pos else None for l in labels]
        if cols['ws'] is None or cols['wd'] is None:
            bad.append((n, 'no wind speed or direction in the answer'))
            continue
        if sum(1 for x in cols['ws'] if x is not None) < MIN_SHARE * len(labels):
            dropped.append(n)
            continue
        cell = None
        if isinstance(o.get('latitude'), (int, float)) and isinstance(o.get('longitude'), (int, float)):
            cell = (round(o['latitude'], 4), round(o['longitude'], 4))
        nodes[n] = {'ws': cols['ws'], 'wd': cols['wd'], 'gust': cols['gust'] if cols['gust'] is not None else [None] * len(labels), 'cell': cell}
    if axis is None:
        return {'ok': False, 'error': bad[0][1] if bad else 'no location could be read'}
    in_axis = set(axis)
    return {'ok': True, 'error': None, 'times': [l for l in labels if l in in_axis], 'nodes': nodes, 'dropped': dropped, 'badNodes': bad}


def read_windfield(fetcher, now):
    """The calls of the whole grid. Never raises. Returns {'calls', 'nCalls', 'okCalls', 'errors', 'skipped', 'labels', 'nodes' {node: working series}, 'dropped', 'badNodes'}. A call that fails leaves
    its nodes absent (null in the file); the other call is still used. 'no fixture' (a test that supplied nothing) is skipped, not an error."""
    now_label = C.hour_floor_label(now)
    labels = window_labels(now)
    calls = split_calls()
    r = {'calls': [], 'nCalls': len(calls), 'okCalls': 0, 'errors': [], 'skipped': False, 'labels': labels, 'nodes': {}, 'dropped': [], 'badNodes': [], 'when': now}
    skipped = 0
    for k, idxs in enumerate(calls):
        try:
            data, info = fetcher.get_json('om_wind_%d' % k, wind_url(idxs))
            r['calls'].append(info)
            if data is None:
                if info.get('error') == 'no fixture':
                    skipped += 1
                else:
                    r['errors'].append('call %d of %d (%d nodes): %s' % (k + 1, len(calls), len(idxs), info.get('error') or 'no answer'))
                continue
            pc = parse_chunk(data, idxs, now_label, labels)
        except Exception as e:                                          # an answer of a shape nobody expected: those nodes have no reading, the other call is still read
            pc = {'ok': False, 'error': ('the answer could not be read (%s: %s)' % (type(e).__name__, e))[:140]}
        if not pc['ok']:
            r['errors'].append('call %d of %d (%d nodes): %s' % (k + 1, len(calls), len(idxs), pc['error']))
            continue
        r['okCalls'] += 1
        r['nodes'].update(pc['nodes'])
        r['dropped'] += pc['dropped']
        r['badNodes'] += pc['badNodes']
    r['skipped'] = skipped == len(calls)
    return r


# ------------------------------------------------------------------------------------------------ the block
def _now_index(labels, now):
    nl = C.hour_floor_label(now)
    return labels.index(nl) if nl in labels else None


def build(read, now, point_blocks=None):
    """The windfield block of the file (all the hours the answer held), with the working series kept apart for the checks. Returns (block, work). The block is {'ok': False, ...} with no values when no
    node has a reading at the current hour. status: ok (every node), partial (some nodes), unknown (none), skipped (a test supplied no answer)."""
    base = {'label': STATUS, 'status': None, 'asof': f_local(now), 'model': MODEL_TEXT, 'grid': grid_block(), 'convention': CONVENTION}
    if read is None or read.get('skipped'):
        return dict(base, ok=False, status='skipped', note='no answer was supplied for the wind field in this run'), None
    labels, nodes = read['labels'], read['nodes']
    ni = _now_index(labels, now)
    have_now = [n for n, d in nodes.items() if ni is not None and uv(d['ws'][ni], d['wd'][ni])[0] is not None]
    if ni is None or not have_now:
        why = '; '.join(read['errors']) or ('no node has a wind speed and a direction at the current hour (%s)' % ', '.join('node %d: %s' % b for b in read['badNodes'][:2]) if read['badNodes'] else 'no node has a reading at the current hour')
        return dict(base, ok=False, status='unknown', error=why[:300], nodesRead=0, nodesTotal=N_NODES,
                    note='no reading: the wind field of this run could not be read, so it carries no values and the map shows no wind movement; the eight points are unaffected'), None
    H = len(labels)
    U = [[None] * N_NODES for _ in range(H)]
    V = [[None] * N_NODES for _ in range(H)]
    G = [[None] * N_NODES for _ in range(H)]
    for n, d in nodes.items():
        for h in range(H):
            u, v = uv(d['ws'][h], d['wd'][h])
            U[h][n], V[h][n] = pack(u), pack(v)
            g = nz(d['gust'][h])
            G[h][n] = None if g is None else pack(g)
    cells = {d['cell'] for n, d in nodes.items() if d.get('cell') and n in have_now}
    speeds = [d['ws'][ni] for n, d in nodes.items() if n in have_now]
    mx_s = max(((d['ws'][h], h, n) for n, d in nodes.items() for h in range(H) if d['ws'][h] is not None and uv(d['ws'][h], d['wd'][h])[0] is not None), default=None)
    mx_g = max(((d['gust'][h], h, n) for n, d in nodes.items() for h in range(H) if d['gust'][h] is not None), default=None)
    ext = lambda t: None if t is None else {'v': r1(t[0]), 't': labels[t[1]], 'node': t[2], 'lat': node_latlon(t[2])[0], 'lon': node_latlon(t[2])[1]}
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    n_read = len(have_now)
    status = 'ok' if n_read == N_NODES else 'partial'
    block = dict(base, ok=True, status=status, ageMin=max(0, int((now - hour_start).total_seconds() // 60)), hours=list(labels), current=labels[ni], scale=SCALE, unit='km/h, whole numbers in steps of 0.1 (value x 10)',
                 u=U, v=V, gust=G, nodesRead=n_read, nodesTotal=N_NODES, nodesDropped=len(read['dropped']), cells=len(cells),
                 calls={'n': read['nCalls'], 'ok': read['okCalls'], 'maxNodesPerCall': MAX_PER_CALL},
                 resolution='about 0.1 degree (about 10 km, the model resolution): %d distinct model cells lie under the %d nodes; a node is a model value, not a measurement, and nodes on one cell carry one wind' % (len(cells), N_NODES),
                 summary={'nowSpeedKmh': [r1(min(speeds)), r1(sum(speeds) / len(speeds)), r1(max(speeds))], 'maxSpeed': ext(mx_s), 'maxGust': ext(mx_g)})
    if status == 'partial':
        block['note'] = '%d of %d nodes have a reading at the current hour; the others are null (no reading, never calm)' % (n_read, N_NODES)
    work = {'labels': labels, 'ni': ni, 'nodes': nodes}
    return block, work


def check_points(work, point_blocks):
    """The grid node nearest each of the eight plan points against the wind of that point from its own call (the best_match call of the conditions), at the current hour. Two readings of one model
    cell must agree to the rounding; a node in the neighbouring cell differs by place, which is said, not scored. Returns {'compared', 'sameCell', 'agree' (yes, no, not comparable), 'maxDiffSameCell'
    {ws, gust, dir}, 'rows'}. Recomputed."""
    rows = []
    if not work:
        return {'compared': 0, 'sameCell': 0, 'agree': 'not comparable', 'rows': [], 'note': 'the wind field was not read'}
    ni, nodes = work['ni'], work['nodes']
    for p in point_blocks or []:
        if not p.get('ok'):
            continue
        nn = nearest_node(p['lat'], p['lon'])
        if nn is None:
            continue
        n, dist = nn
        d = nodes.get(n)
        now_v = p.get('now') or {}
        if not d or d['ws'][ni] is None:
            rows.append({'point': (p['name'] or '').split(' (')[0], 'node': n, 'distKm': dist, 'error': 'the node has no reading'})
            continue
        cell_p = tuple(p['_cell']) if p.get('_cell') else None
        same = None if (cell_p is None or d.get('cell') is None) else (cell_p == d['cell'])
        row = {'point': (p['name'] or '').split(' (')[0], 'node': n, 'nodeLat': node_latlon(n)[0], 'nodeLon': node_latlon(n)[1], 'distKm': dist, 'sameCell': same}
        for key, mine, theirs in (('ws', now_v.get('ws'), d['ws'][ni]), ('gust', now_v.get('gust'), d['gust'][ni])):
            row[key] = {'point': mine, 'node': None if theirs is None else round(theirs, 1), 'diff': None if (mine is None or theirs is None) else round(theirs - mine, 1)}
        wd_p, wd_n = now_v.get('wd'), d['wd'][ni]
        row['dir'] = {'point': wd_p, 'node': wd_n, 'diff': None if (wd_p is None or wd_n is None) else round(C.circ_diff(wd_n, wd_p), 1)}
        rows.append(row)
    same = [r for r in rows if r.get('sameCell')]
    mx = {'ws': None, 'gust': None, 'dir': None}
    for k in mx:
        xs = [abs(r[k]['diff']) for r in same if r.get(k) and r[k]['diff'] is not None and (k != 'dir' or ((r['ws']['node'] or 0) >= 3.0))]
        mx[k] = max(xs) if xs else None
    off = (mx['ws'] is not None and mx['ws'] > CHECK_TOL_KMH) or (mx['gust'] is not None and mx['gust'] > CHECK_TOL_KMH) or (mx['dir'] is not None and mx['dir'] > CHECK_TOL_DEG)
    agree = 'no' if off else ('yes' if same else 'not comparable')
    other = [abs(r['ws']['diff']) for r in rows if r.get('sameCell') is False and r.get('ws') and r['ws']['diff'] is not None]
    # the rows as they go into health: short, [point value, node value, node minus point] for speed and gust in km/h and the angle between the directions in degrees
    short = [({'point': r['point'], 'node': r['node'], 'distKm': r['distKm'], 'error': r['error']} if 'error' in r else
              {'point': r['point'], 'node': r['node'], 'distKm': r['distKm'], 'sameCell': r['sameCell'], 'ws': [r['ws']['point'], r['ws']['node'], r['ws']['diff']],
               'gust': [r['gust']['point'], r['gust']['node'], r['gust']['diff']], 'dir': [r['dir']['point'], r['dir']['node'], r['dir']['diff']]}) for r in rows]
    return {'status': 'Recomputed', 'compared': len([r for r in rows if 'error' not in r]), 'sameCell': len(same), 'agree': agree, 'maxDiffSameCell': mx,
            'maxWsDiffOtherCell': max(other) if other else None, 'toleranceKmh': CHECK_TOL_KMH, 'toleranceDeg': CHECK_TOL_DEG, 'rows': short,
            'rowsKey': 'ws, gust: [the point, the nearest node, node minus point] in km/h; dir: [the point, the node, the angle between them] in degrees; sameCell says whether both were served from one model cell',
            'note': ('each plan point is compared with the grid node nearest to it; a node in the same model cell must agree with the point to the rounding (%.1f km/h, %.1f deg); a node in the neighbouring cell '
                     'is up to about 7 km away and differs by place, which is not scored' % (CHECK_TOL_KMH, CHECK_TOL_DEG))}


# ------------------------------------------------------------------------------------------------ the compact forms
def keep_indexes(labels, now_label, upto):
    """the indexes a schedule keeps: every hour up to `upto` hours after the current hour (all when upto is None), then every third hour counted from the current hour, and always the last stamp"""
    if now_label not in labels:
        return list(range(len(labels)))
    base = labels.index(now_label)
    keep = []
    for idx in range(len(labels)):
        k = idx - base
        if upto is None or k <= upto or k % 3 == 0 or idx == len(labels) - 1:
            keep.append(idx)
    return keep


def subset(wf, keep, **extra):
    """a copy of a windfield block with only the hours at the indexes in keep"""
    out = {k: v for k, v in wf.items() if k not in ('hours', 'u', 'v', 'gust')}
    out['hours'] = [wf['hours'][i] for i in keep]
    for k in ('u', 'v', 'gust'):
        out[k] = [wf[k][i] for i in keep]
    out.update(extra)
    return out


def thin(wf, name):
    """the block on a schedule of SCHEDULES; the hours kept are said in 'thinned' unless the schedule is every hour"""
    if not wf or not wf.get('ok'):
        return wf
    for nm, upto, text in SCHEDULES:
        if nm == name:
            keep = keep_indexes(wf['hours'], wf['current'], upto)
            if len(keep) == len(wf['hours']):
                return wf
            return subset(wf, keep, thinned='hours kept: %s (150 nodes by 49 hourly stamps is more than a 200 KB file can hold beside the eight points); the hours in between are to be interpolated in time; the stamps are not evenly spaced' % text)
    raise ValueError('unknown schedule ' + name)


def reduce_for_weather(wf):
    """the copy in weather.json for the first paint without a network: the current hour and the next 6 hours"""
    if not wf or not wf.get('ok'):
        return wf
    labels = wf['hours']
    base = labels.index(wf['current'])
    keep = [i for i in range(len(labels)) if 0 <= i - base <= WEATHER_AHEAD_H]
    return subset(wf, keep, reduced='the current hour and the next %d hours, for the first paint; the whole field is in conditions.json' % WEATHER_AHEAD_H)


def ledger_summary(wf):
    """the compact form for the ledger line: nodes read, distinct cells, the speed now [min, mean, max] and the highest speed and gust of the field with their hours. Never an array."""
    if not wf or not wf.get('ok'):
        return {'status': (wf or {}).get('status') or 'none'}
    s = wf['summary']
    return {'status': wf['status'], 'n': wf['nodesRead'], 'of': wf['nodesTotal'], 'cells': wf['cells'], 'now': s['nowSpeedKmh'], 'max': [(s['maxSpeed'] or {}).get('v'), (s['maxGust'] or {}).get('v')]}
