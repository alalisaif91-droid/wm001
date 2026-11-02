# -*- coding: utf-8 -*-
"""The link between the weather platform and the rain plan and console: what the weather asks of the plan, sector by sector (interaction.json).

Everything about the plan is read from the plan's own files at every run and never retyped: pump counts from data/pumpops.json (planBasis), crews and hire
tankers from manpower.json, the gangs and plant by sector from the DIST table of records.py (read with ast, not imported), the actions from actions17.json and
BARQ/actions.md, the sector geometry from data/sectors.geojson, last season's reports from data/re_summary.json and data/re_points.json.
No name, employee number or pay is read or written: action text is cut to a short title before the first bracket, so a person named in an action never
travels (A27 names its survey team in brackets).

Fog: the plan has no fog procedure. That is written as a gap with its owner, and the measures listed are Proposed for a decision, never plan content. The fog level is
nevertheless carried into the interaction: a fog line on the stage block, a fog row in the actions due now, the sector rows and a summary (audit WX-16). Every reader of a plan file
is tolerant of a file whose shape changed: the piece is named in planGaps and in the health alerts, and the rest is still published (audit WX-11, WX-15)."""
import os, re, ast, json, datetime as dt
from wx_util import read_json, read_json_status, f_local, parse_local, point_in_polygon, haversine_km, POINTS, short_text, HERE, r1

ROOT = os.path.abspath(os.path.join(HERE, '..'))
BARQ = os.path.join(ROOT, 'BARQ')
SECTOR_NAMES = {'1': 'Sector 1', '2': 'Sector 2', '3': 'Sector 3', '4': 'Sector 4', '5': 'Sector 5', '6': 'Sector 6', '7': 'Sector 7', '8': 'Sector 8', '9': 'Sector 9'}
STAGES = ['Routine readiness', 'Enhanced watch', 'Pre deployment', 'Active response']
RAIN_STAGE = {'none': 0, 'watch': 1, 'high': 2}
# the call table of the plan builders (share_blocks.py lines 70 to 77, build_plan_split.py lines 365 to 374), read at run time; this copy is the fallback
PLAN_CALL_FALLBACK = {'RAK city': '3 and 4', 'Al Rams': '1', 'Al Burairat': '2', 'Jebel Jais': '1', 'Khatt': '5', 'Al Jazeera': '6', 'Showkah': '8', 'Al Munai': '9'}
FOG_POINT_PREFIXES = ('RAK city', 'Al Rams', 'Al Burairat', 'Khatt', 'Al Jazeera')       # the west plain: where the calibrated fog rule applies
MONTHS = {m: i + 1 for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}


def _p(*parts):
    return os.path.join(HERE, *parts)


# ------------------------------------------------------------------------------------------------ plan data
def plan_call_table():
    for fn in ('share_blocks.py', 'build_plan_split.py'):
        try:
            with open(_p(fn), encoding='utf-8') as f:
                txt = f.read()
        except OSError:
            continue
        m = re.search(r"_CALL = \{(.*?)\}\n", txt, re.S)
        if not m:
            continue
        found = re.findall(r"'([^']+)':\s*\('([^']+)'", m.group(1))
        if len(found) >= 8:
            return {k: v for k, v in found}, fn + ' (_CALL)'
    return dict(PLAN_CALL_FALLBACK), 'embedded copy of the call table in share_blocks.py lines 70 to 77 (the file could not be read)'


def point_key(name, table):
    for k in table:
        if name.startswith(k):
            return k
    return None


def load_sector_geometry():
    """{sector number: geometry} from data/sectors.geojson; a file or a feature of another shape gives fewer sectors, never an exception (audit F9)"""
    g = read_json(_p('data', 'sectors.geojson'), None)
    out = {}
    feats = g.get('features') if isinstance(g, dict) else None
    for f in feats if isinstance(feats, list) else []:
        try:
            n = str(((f.get('properties') or {}).get('attributes') or {}).get('n') or '')
            geom = f.get('geometry')
            if n and isinstance(geom, dict) and geom.get('type') in ('Polygon', 'MultiPolygon') and isinstance(geom.get('coordinates'), list):
                out[n] = geom
        except (AttributeError, TypeError):
            continue
    return out


def _ring_area_centroid(ring):
    """(lat, lon, area) of one ring by the shoelace formula; the area centroid, not the mean of the vertices (audit logic D13)"""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cr = x0 * y1 - x1 * y0
        a += cr
        cx += (x0 + x1) * cr
        cy += (y0 + y1) * cr
    a *= 0.5
    if abs(a) < 1e-12:
        return sum(p[1] for p in ring) / n, sum(p[0] for p in ring) / n, 0.0
    return cy / (6.0 * a), cx / (6.0 * a), abs(a)


def _centroid(geom):
    """(lat, lon) of the centre of the area of a sector outline (the largest polygon of a multi polygon)"""
    rings = [geom['coordinates'][0]] if geom['type'] == 'Polygon' else [poly[0] for poly in geom['coordinates'] if poly]
    best = max((_ring_area_centroid(r) for r in rings if len(r) >= 3), key=lambda t: t[2])
    return best[0], best[1]


def sector_of(lon, lat, geoms):
    for n, geom in geoms.items():
        try:
            if point_in_polygon(lon, lat, geom):
                return n
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            continue
    return None


def sector_map():
    """{'1': {'points': [indices], 'mapping': ..., 'unmapped': bool, 'nearest': {...}}} for S1 to S9, from the plan's call table, checked against the geometry"""
    table, table_src = plan_call_table()
    geoms = load_sector_geometry()
    sectors = {n: {'points': [], 'geometryPoints': [], 'mapping': None, 'unmapped': False, 'nearest': None, 'notes': []} for n in map(str, range(1, 10))}
    for i, (name, lat, lon) in enumerate(POINTS):
        k = point_key(name, table)
        called = re.findall(r'\d+', table.get(k, '')) if k else []
        geo = sector_of(lon, lat, geoms) if geoms else None
        for n in called:
            if n in sectors:
                sectors[n]['points'].append(i)
        if geo and geo in sectors:
            sectors[geo]['geometryPoints'].append(i)
        if geo and called and geo not in called:
            for n in called:
                sectors[n]['notes'].append('the point %s lies inside Sector %s by the geometry; the plan call table assigns it to Sector %s' % (name.split(' (')[0], geo, n))
        if geo and k and geo in called and len(called) > 1:
            for n in called:
                if n != geo:
                    sectors[n]['notes'].append('shared: the point %s is inside Sector %s and the plan call table also assigns it to Sector %s' % (name.split(' (')[0], geo, n))
    for n, s in sectors.items():
        if s['points']:
            s['mapping'] = 'plan call table (%s)' % table_src
            if not s['geometryPoints'] or set(s['points']) - set(s['geometryPoints']):
                s['shared'] = True
        else:
            s['unmapped'] = True
            s['mapping'] = 'unmapped: no rain point is assigned to this sector'
            if n in geoms:
                try:
                    clat, clon = _centroid(geoms[n])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue                                       # an outline that cannot be measured: no nearest point, and the sector says so
                best = min(range(len(POINTS)), key=lambda i: haversine_km(clat, clon, POINTS[i][1], POINTS[i][2]))
                s['nearest'] = {'point': best, 'name': POINTS[best][0], 'distanceKm': round(haversine_km(clat, clon, POINTS[best][1], POINTS[best][2]), 1), 'from': 'the centre of the area of the sector outline'}
    return sectors, table_src, bool(geoms)


def _ast_assign(path, names):
    out = {}
    try:
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _scen_ok(sc):
    return isinstance(sc, dict) and sc and all(isinstance(v, dict) for v in sc.values())


def _pump_text(sc, style):
    """the pump totals as the plan file holds them: counted, screened, red, maximum (read, never retyped; audit D10)"""
    g = lambda k: (sc.get(k) or {}).get('pumps') if isinstance(sc.get(k), dict) else None
    if style == 'source':
        return ', '.join('%s %s' % (v, lab) for v, lab in ((g('counted'), 'counted'), (g('screened'), 'screened'), (g('red'), 'at red'), (g('full'), 'at most')) if v is not None)
    return 'counted %s, screened %s, red %s, maximum %s; the %s and the %s are spread by the planner weights (pumpops.json planBasis)' % (g('counted'), g('screened'), g('red'), g('full'), g('red'), g('full'))


def plan_resources():
    """read the plan's resource files: {'dist','dcols','staging','pumpops','manpower','tankers','sources':[...], 'missing':[...]}. A file that is there in another shape is a named gap."""
    out = {'sources': [], 'missing': []}
    rec = _ast_assign(_p('records.py'), ('DIST', 'DCOLS', 'STAGING'))
    if isinstance(rec.get('DIST'), dict) and isinstance(rec.get('DCOLS'), (list, tuple)):
        out['dist'], out['dcols'], out['staging'] = rec['DIST'], rec['DCOLS'], rec.get('STAGING', {}) if isinstance(rec.get('STAGING', {}), dict) else {}
        out['sources'].append('records.py DIST (Table 8.1, Proposed) and STAGING')
    else:
        out['missing'].append('records.py DIST not readable: gangs and plant by sector are not shown')
    po = read_json(_p('data', 'pumpops.json'), None)
    pb = po.get('planBasis') if isinstance(po, dict) else None
    bad_dist = []
    if isinstance(pb, dict) and _scen_ok(pb.get('scenarios')):
        for k in ('counted', 'screened', 'red', 'full'):
            d_ = (pb['scenarios'].get(k) or {}).get('dist') if isinstance(pb['scenarios'].get(k), dict) else None
            if k in pb['scenarios'] and not (isinstance(d_, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in d_)):
                bad_dist.append(k)
    if isinstance(pb, dict) and _scen_ok(pb.get('scenarios')) and bad_dist:
        out['missing'].append('data/pumpops.json planBasis: the by sector list (dist) of the scenario(s) %s is not a list of numbers: pump spreads by sector are not shown' % ', '.join(bad_dist))
    elif isinstance(pb, dict) and _scen_ok(pb.get('scenarios')):
        out['pumpops'] = pb
        out['sources'].append('data/pumpops.json planBasis (%s)' % _pump_text(pb['scenarios'], 'source'))
    else:
        out['missing'].append('data/pumpops.json planBasis not readable (missing, damaged or in another shape): pump spreads by sector are not shown')
    mp = read_json(_p('manpower.json'), None)
    if isinstance(mp, dict) and isinstance(mp.get('rows'), list) and all(isinstance(r, dict) for r in mp['rows']):
        out['manpower'] = mp
        out['sources'].append('manpower.json (crews, hire tankers by sector, Proposed)')
    else:
        out['missing'].append('manpower.json not readable (missing, damaged or in another shape): crews and hire tankers by sector are not shown')
    tk = read_json(_p('tankers.json'), None)
    if isinstance(tk, dict) and isinstance(tk.get('tiers'), dict):
        out['tankers'] = tk
        out['sources'].append('tankers.json (tiers)')
    elif tk is not None:
        out['missing'].append('tankers.json is not in the shape the engine reads (tiers): hire tanker totals are not shown')
    return out


def sector_resources(n, res):
    """what the plan places in sector n, all read from the plan files. Placed or not is not on any register: that is stated, not guessed."""
    r = {'sector': n}
    i = int(n) - 1
    notes = []
    try:
        if res.get('dist') and n in res['dist']:
            row = dict(zip(res['dcols'], res['dist'][n]))
            r['stagingPoint'] = (res.get('staging') or {}).get(n)
            r['gangs'] = row.get('Gangs')
            r['supervisors'] = row.get('Supervisors')
            r['labour'] = row.get('Labour')
            r['plant'] = {k: row.get(k) for k in ('Pickups 3 t', 'Tippers', 'Wheel loaders', 'Backhoes', 'Dozers', 'Generators', 'Tower lights', 'Barrier sets') if k in row}
            r['sewageTankers'] = row.get('Sewage tankers')
            r['pumpsTable81'] = row.get('Pumps')
    except (TypeError, ValueError, KeyError) as e:
        notes.append('Table 8.1 row of sector %s not readable: %s' % (n, str(e)[:60]))
    pb = res.get('pumpops')
    if pb:
        try:
            sc = pb.get('scenarios', {})
            r['pumps'] = {k: (sc[k]['dist'][i] if k in sc and isinstance(sc[k].get('dist'), list) and len(sc[k]['dist']) > i else None) for k in ('counted', 'screened', 'red', 'full')}
            r['pumpsNote'] = _pump_text(sc, 'note')
            for key, scen in (('crewAtCounted', 'counted'), ('crewAtRed', 'red')):
                rows = sc.get(scen, {}).get('rows') if isinstance(sc.get(scen), dict) else None
                crew = next((x for x in (rows or []) if isinstance(x, dict) and x.get('s') == int(n)), None)
                if crew:
                    r[key] = {k: crew.get(k) for k in ('sup', 'teams', 'techs', 'work')}
        except (TypeError, ValueError, KeyError, IndexError) as e:
            notes.append('pump spread of sector %s not readable: %s' % (n, str(e)[:60]))
    mp = res.get('manpower')
    if mp:
        row = next((x for x in mp.get('rows', []) if isinstance(x, dict) and str(x.get('sector')) == n), None)
        if row:
            r['hireTankers'] = {'orange': row.get('tankersOrange'), 'red': row.get('tankersRed'), 'tankerPoints': row.get('tankerPoints')}
            r['labourDay'] = row.get('labourDay')
            r['hoseM'] = row.get('hoseM')
            r['demandVsPlan'] = {'counted': row.get('demandCounted'), 'red': row.get('demandRed'), 'gapCounted': row.get('gapCounted'), 'gapRed': row.get('gapRed')}
    r['placed'] = 'unknown: no register of placement is on file; the unit readiness status of A20 is not yet carried on the console'
    if notes:
        r['notes'] = notes
    return r


# ------------------------------------------------------------------------------------------------ actions
def _due(s):
    m = re.search(r'(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})', s or '')
    if not m:
        return None
    try:
        return dt.datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def title_of(text):
    """a short title: the text up to the first bracket, semicolon or colon, so a name in brackets never travels; cut at a word, never in the middle of one (audit D10)"""
    t = re.split(r'[(;:]', text or '')[0]
    t = ' '.join(t.split()).strip().rstrip(',')
    if len(t) <= 120:
        return t
    return t[:117].rsplit(' ', 1)[0].rstrip(',') + ' ...'


def read_actions(now):
    """actions A1 to A30 from actions17.json, with the status of BARQ/actions.md where it has the action. The status is the register's own word (open, done, late) or unknown
    (the Barq register stops at A17); an action that is open and past its due date is flagged lateByDueDate, which is Recomputed here and is not the register's word (audit D9).
    listRead says whether any action list was read at all: an empty list must not read as 0 actions due (audit WX-15)."""
    out = {'items': [], 'sources': [], 'notes': [], 'listRead': False, 'statusAsOf': None, 'statusAgeDays': None, 'skipped': 0}
    rows = read_json(_p('actions17.json'), None)
    if not isinstance(rows, list) or not rows:
        out['notes'].append('actions17.json is missing, damaged or empty: no action list was read, so no action can be called due or not due (owner: the plan builder)')
        return out
    good = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 3 and isinstance(r[0], str) and isinstance(r[1], str)]
    out['skipped'] = len(rows) - len(good)
    if out['skipped']:
        out['notes'].append('%d rows of actions17.json are not in the shape the engine reads and were skipped (owner: the plan builder)' % out['skipped'])
    if not good:
        out['notes'].append('actions17.json holds no readable action row: no action list was read (owner: the plan builder)')
        return out
    rows = good
    out['listRead'] = True
    out['sources'].append('actions17.json (A1 to A%d: owner, evidence, due date)' % len(rows))
    status, updated = {}, None
    try:
        with open(os.path.join(BARQ, 'actions.md'), encoding='utf-8') as f:
            txt = f.read()
        for ln in txt.splitlines():
            if ln.startswith('| A'):
                c = [x.strip() for x in ln.strip().strip('|').split('|')]
                if len(c) >= 7 and re.match(r'^A\d+$', c[0]):
                    status[c[0]] = {'status': c[4].lower(), 'updated': c[6]}
        m = re.search(r'Counts on (\d{1,2} \w{3} \d{4})', txt)
        updated = m.group(1) if m else None
        out['sources'].append('BARQ/actions.md (status of %d actions, last counted %s)' % (len(status), updated))
    except OSError:
        out['notes'].append('BARQ/actions.md is not readable: no action status (owner: Barq)')
    ud = _due(updated)
    if ud:
        out['statusAsOf'] = ud.strftime('%Y-%m-%d')
        out['statusAgeDays'] = (now.date() - ud.date()).days
    last_id = max([int(k[1:]) for k in status] or [0])
    if status and last_id < len(rows):
        out['notes'].append('The Barq register stops at A%d and was last updated %s%s: the status of A%d to A%d is unknown, not open (owner: Barq, and the committee for evidence)' % (
            last_id, updated, (' (%d days ago)' % out['statusAgeDays']) if out['statusAgeDays'] is not None else '', last_id + 1, len(rows)))
    elif status and out['statusAgeDays'] is not None and out['statusAgeDays'] > 3:
        out['notes'].append('The Barq register was last updated %s (%d days ago): its statuses are that old (owner: Barq)' % (updated, out['statusAgeDays']))
    for r in rows:
        aid = r[0]
        due_s = r[4] if len(r) > 4 and isinstance(r[4], str) else ''
        due = _due(due_s)
        st = (status.get(aid) or {}).get('status') or 'unknown'
        recurring = 'every' in (due_s or '').lower()
        passed = bool(due and due.date() < now.date() and not recurring)
        out['items'].append({'id': aid, 'title': title_of(r[1]), 'owner': str(r[2]), 'due': due_s, 'dueDate': due.strftime('%Y-%m-%d') if due else None, 'recurring': recurring,
                             'status': st, 'dueDatePassed': passed, 'lateByDueDate': bool(passed and st == 'open'), '_text': r[1]})
    return out


_STAGE_NORM = lambda t: ' '.join(re.sub(r'[-_]+', ' ', str(t or '').lower()).split())


def _stage_in(text):
    """the stage a text names: the first one named (so 'Active response (stepped up from Routine readiness)' reads Active response); a text that names three or more is a pasted
    ladder, not a stage, and reads as none (audit logic D9)"""
    t = _STAGE_NORM(text)
    hits = sorted((t.find(_STAGE_NORM(st)), st) for st in STAGES if _STAGE_NORM(st) in t)
    if not hits or len(hits) >= 3:
        return None
    return hits[0][1]


STAGE_STALE_H = 24


def read_stage_file(now=None):
    """any stage file Barq writes (stage.json, stage.md, stage.txt). None exists on 24 Oct 2026; the console stage is a per device record the engine cannot read.
    The stage name is read forgivingly (case, hyphens); the time of the file (its own asof, else its file time) gives its age, and a file older than 24 hours is stale: it is shown,
    not compared with the weather stage as if it were current (audit logic D9)."""
    for name in ('stage.json', 'stage.md', 'stage.txt'):
        pth = os.path.join(BARQ, name)
        if not os.path.exists(pth):
            continue
        try:
            asof = None
            if name.endswith('.json'):
                d = read_json(pth, {})
                d = d if isinstance(d, dict) else {}
                stage = _stage_in(d.get('stage'))
                try:
                    asof = parse_local(d.get('asof')) if d.get('asof') else None
                except (ValueError, TypeError):
                    asof = None
            else:
                with open(pth, encoding='utf-8') as f:
                    t = f.read()
                m = re.search(r'(?im)^\W*(?:current\s+)?stage\s*[:=]\s*(.+)$', t)
                stage = _stage_in(m.group(1)) if m else _stage_in(t)
                ma = re.search(r'(?im)(?:as of|asof|updated)\s*[:=]?\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?)', t)
                try:
                    asof = parse_local(ma.group(1)) if ma else None
                except (ValueError, TypeError):
                    asof = None
            src = 'its own asof'
            if asof is None:
                # the file time, in Ras Al Khaimah time (the engine's own): through UTC, whatever the laptop zone is
                asof = dt.datetime.fromtimestamp(os.path.getmtime(pth), tz=dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)
                src = 'the file time'
            age = round((now - asof).total_seconds() / 3600.0, 1) if now else None
            return {'stage': stage, 'asof': f_local(asof), 'file': name, 'ageHours': age, 'ageFrom': src, 'stale': bool(age is not None and age > STAGE_STALE_H)}
        except OSError:
            continue
    return None


def readiness(now):
    a = read_actions(now)
    c = {'done': 0, 'open': 0, 'late': 0, 'unknown': 0}
    for it in a['items']:
        c[it['status'] if it['status'] in c else 'unknown'] += 1
    txt = 'counts from actions17.json and BARQ/actions.md; the status is the register\'s own word; unknown means the Barq register does not hold the action'
    if a['statusAsOf']:
        txt += '; the register was last counted %s (%d days ago), so every status is that old; openPastDue is open and past its due date, Recomputed' % (a['statusAsOf'], a['statusAgeDays'])
    return {'asof': f_local(now), 'actions': c, 'total': len(a['items']), 'dueDatePassed': sum(1 for it in a['items'] if it['dueDatePassed']), 'openPastDue': sum(1 for it in a['items'] if it['lateByDueDate']),
            'statusAsOf': a['statusAsOf'], 'statusAgeDays': a['statusAgeDays'], 'listRead': a['listRead'], 'stageFile': read_stage_file(now), 'note': txt, 'sources': a['sources']}


def _valid_triggers(lst):
    good = [t for t in lst if isinstance(t, dict) and isinstance(t.get('id'), str) and isinstance(t.get('rule'), str) and t.get('when') in ('standing', 'watch', 'high', 'red')]
    return good, len(lst) - len(good)


def load_triggers_checked(data_dir):
    """(triggers, problems): data/weather/action_triggers.json of the data folder in use; a folder without a usable one (a scratch folder, a test) falls back to the one that ships
    with the engine. An entry without an id, a rule or a known 'when' is skipped and counted, never a KeyError (audit WX-11)."""
    from wx_util import DEFAULT_DATA_DIR
    problems = []
    for folder in (data_dir, DEFAULT_DATA_DIR):
        d = read_json(os.path.join(folder, 'action_triggers.json'), None)
        if isinstance(d, dict) and isinstance(d.get('triggers'), list) and d['triggers']:
            good, bad = _valid_triggers(d['triggers'])
            if bad:
                problems.append('action_triggers.json: %d entries without an id, a rule or a known "when" were skipped (owner: the weather desk)' % bad)
            if good:
                return good, problems
    return None, problems


def load_triggers(data_dir):
    return load_triggers_checked(data_dir)[0]


STAGE_LEVEL = {'Routine readiness': 0, 'Enhanced watch': 1, 'Pre deployment': 2, 'Active response': 2}
LEVEL_WORD = {0: 'none', 1: 'watch', 2: 'high'}


def fog_row(fog_level, fog_info):
    """the row that puts fog into the actions due now: not a plan action (the plan has no fog procedure) but the situation and the pointer to the Proposed measures"""
    if fog_level in (None, 'none', 'not assessed'):
        return None
    fi = fog_info or {}
    if fog_level == 'unknown':
        title = 'Fog level not read: no station, forecast or NCM reading rules fog out; the rain plan has no fog procedure'
    else:
        title = 'Fog level %s: the rain plan has no fog procedure; the fog measures F1 to F5 are Proposed for a decision' % fog_level
    return {'id': 'FOG', 'kind': 'fog', 'title': title, 'owner': 'Waste Management with LOD', 'due': 'while the fog level is not none', 'status': 'Proposed',
            'rule': 'Fog is reported beside the rain stage. No fog trigger is in the plan; the measures F1 to F5 in actions.fog are Proposed for a decision, never plan actions.',
            'basis': short_text(fi.get('basis') or '', 400) or None, 'why': 'fog level is %s' % fog_level}


def vote_row(v):
    """the row that puts a proposed model vote above the plan rule into the actions due now: not a plan action, but the situation: a none under the plan rule is not no rain when the vote says more (audit V2)"""
    if not v:
        return None
    return {'id': 'VOTE', 'kind': 'vote', 'title': 'The proposed model vote says %s while the plan rule says %s: the vote is not applied until the department head decides; read the models and the NCM page' % (v.get('level'), v.get('planRule')),
            'owner': 'Weather lead', 'due': 'while the vote is above the plan rule', 'status': 'Proposed',
            'rule': 'The proposed vote of the calibration is worked out beside the plan rule and is applied only when the rule is decided. It is listed here so that a level of none is never read as no rain when the vote says more; '
                    'the actions of the level the vote names are in dueOnWatch and dueOnHigh.',
            'basis': short_text(v.get('text') or '', 400) or None, 'why': 'the proposed vote is above the plan rule'}


def actions_block(now, rain_level, ncm_rain, triggers, fog_level, stage=None, fog_info=None, data_dir=None, vote_info=None):
    """what the weather asks of the plan: standing actions and those tied to a level, with their status. The level an action is tied to is the higher of the rain hazard level
    and the level the weather stage implies (Enhanced watch is a watch, Pre deployment and Active response are a high), so the stage and the list of actions cannot differ by two
    stages (audit logic D3). The fog level adds a fog row (audit WX-16)."""
    a = read_actions(now)
    by_id = {x['id']: x for x in a['items']}
    cnt = {'done': 0, 'open': 0, 'late': 0, 'unknown': 0}
    for it in a['items']:
        cnt[it['status'] if it['status'] in cnt else 'unknown'] += 1
    block = {'asof': f_local(now), 'listRead': a['listRead'], 'sources': a['sources'], 'notes': list(a['notes']), 'counts': cnt, 'openPastDue': sum(1 for it in a['items'] if it['lateByDueDate']),
             'statusAsOf': a['statusAsOf'], 'statusAgeDays': a['statusAgeDays'], 'dueNow': [], 'dueOnWatch': [], 'dueOnHigh': [], 'dueOnRed': [], 'standing': []}
    if not a['listRead']:
        block['gap'] = {'what': 'No action list was read (actions17.json is missing, damaged or empty): 0 actions due here does not mean that none is due', 'owner': 'the plan builder'}
    block['fog'] = fog_gap_block(fog_level, data_dir)
    fr = fog_row(fog_level, fog_info)
    vr = vote_row(vote_info)
    if not triggers:
        block['notes'].append('data/weather/action_triggers.json is not readable: no action is tied to a level (owner: the weather desk)')
        if fr:
            block['dueNow'].append(fr)
        if vr:
            block['dueNow'].append(vr)
        return block

    def add_due(row, why):
        """an action that falls due for two reasons (A26 standing and on watch, A29 on high and on red) is listed once, with both reasons"""
        for x in block['dueNow']:
            if x['id'] == row['id'] and x.get('kind') is None:
                if why not in x['why'].split('; '):
                    x['why'] += '; ' + why
                return
        block['dueNow'].append(dict(row, why=why))
    rl = {'none': 0, 'watch': 1, 'high': 2}.get(rain_level, 0)
    stg = (stage or {}).get('weatherStage') if isinstance(stage, dict) else stage
    sl = STAGE_LEVEL.get(stg, 0)
    cur = max(rl, sl)
    red = ncm_rain == 'red'
    if rain_level == 'unknown' and sl == 0:
        block['notes'].append('the rain level is unknown (not read well enough): the actions due now are the standing ones only; read the level before relying on this list')

    def why_for(need):
        return 'rain level is %s' % rain_level if rl >= need else 'the weather stage is %s' % stg
    for t in triggers:
        it = by_id.get(t['id'])
        if not it:
            continue
        row = {'id': t['id'], 'title': it['title'], 'owner': it['owner'], 'due': it['due'], 'status': it['status'], 'rule': t['rule'], 'basis': t.get('basis')}
        if it.get('lateByDueDate'):
            row['lateByDueDate'] = True
        if t.get('detailPattern'):
            try:
                mm = re.search(t['detailPattern'], it.get('_text') or '')
            except re.error:
                mm = None
            if mm and mm.groups():
                row['detail'] = mm.group(1)
        when = t['when']
        if when == 'standing':
            block['standing'].append(row)
            add_due(row, 'standing')
        elif when == 'watch':
            block['dueOnWatch'].append(row)
            if cur >= 1:
                add_due(row, why_for(1))
        elif when == 'high':
            block['dueOnHigh'].append(row)
            if cur >= 2:
                add_due(row, why_for(2))
        elif when == 'red':
            block['dueOnRed'].append(row)
            if red:
                add_due(row, 'NCM red')
    if fr:
        block['dueNow'].append(fr)
    if vr:
        block['dueNow'].append(vr)
    block['rainLevelUsed'] = rain_level
    block['levelUsed'] = LEVEL_WORD[cur]
    block['stageUsed'] = stg
    return block


# ------------------------------------------------------------------------------------------------ fog: the plan has no procedure
_routes_cache = {}


def fog_routes_checked():
    """the routes by sector, read once for a given state of the file (the file is 480 KB and three readers in a run ask for it)"""
    path = _p('data', 'rak_subsectors.geojson')
    try:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        key = (path, None, None)
    if key not in _routes_cache:
        _routes_cache.clear()
        _routes_cache[key] = _fog_routes_raw(path)
    return _routes_cache[key]


def _fog_routes_raw(path):
    """({sector number: collection routes}, None), or (None, the problem in words) when data/rak_subsectors.geojson is missing, damaged, in another shape or holds no route: a count of 0 routes
    printed as Recomputed for a file that was not read is a figure nobody can tell from a real one (audit N6)"""
    g, status = read_json_status(path)
    feats = g.get('features') if isinstance(g, dict) else None
    if status != 'ok' or not isinstance(feats, list):
        return None, ('data/rak_subsectors.geojson %s: the collection routes by sector are not read, so the fog exposure shows no route count (owner: the plan builder)'
                      % ('is missing' if status == 'missing' else 'cannot be read or is not in the shape the engine reads'))
    cnt = {}
    for f in feats:
        try:
            p = f.get('properties') or {}
            if p.get('kind') == 'route' and p.get('sectorNo'):
                cnt[str(p['sectorNo'])] = cnt.get(str(p['sectorNo']), 0) + 1
        except (AttributeError, TypeError):
            continue
    if not cnt:
        return None, 'data/rak_subsectors.geojson holds no collection route (no feature of kind route with a sector number): the fog exposure shows no route count (owner: the plan builder)'
    return cnt, None


def fog_routes():
    return fog_routes_checked()[0] or {}


def _calibration(data_dir):
    from wx_util import DEFAULT_DATA_DIR
    for folder in (data_dir, DEFAULT_DATA_DIR):
        if folder:
            c = read_json(os.path.join(folder, 'calibration.json'), None)
            if isinstance(c, dict):
                return c
    return None


def _fog_hours_complete_seasons(cal, icao, last_complete=2025):
    """the fog hours of the complete seasons on record in calibration.json (the seasons keyed 2019 to 2025: Oct 2019 to Mar 2026), and how many seasons that is. Read, not retyped."""
    f = _dict(_dict(_dict(cal).get('fog')).get('frequency')).get(icao)
    by = _dict(_dict(f).get('fogHoursBySeason'))
    ks = [k for k in by if str(k).isdigit() and int(k) <= last_complete and isinstance(by[k], (int, float))]
    return (sum(by[k] for k in ks), len(ks)) if ks else (None, 0)


def _dict(x):
    return x if isinstance(x, dict) else {}


def _ncm_station_visibility(data_dir):
    from wx_util import DEFAULT_DATA_DIR
    for folder in (data_dir, DEFAULT_DATA_DIR):
        if not folder:
            continue
        d = read_json(os.path.join(folder, 'fixtures', 'ncm_aws_24h_rak_area_summary_2026-10-24.json'), None)
        if isinstance(d, dict) and isinstance(d.get('stations_total'), int) and isinstance(d.get('stations_with_any_visibility_value'), int):
            return d
    return None


def fog_gap_block(fog_level, data_dir=None):
    routes, routes_problem = fog_routes_checked()
    west = {n: (routes.get(n, 0) if routes is not None else None) for n in ('1', '2', '3', '4', '5', '6')}
    cal = _calibration(data_dir)
    h_rk, n_rk = _fog_hours_complete_seasons(cal, 'OMRK')
    h_sj, n_sj = _fog_hours_complete_seasons(cal, 'OMSJ')
    h_fj, _ = _fog_hours_complete_seasons(cal, 'OMFJ')
    if h_rk is not None and h_sj is not None:
        evidence = 'fog is a west coast problem: %d fog hours at OMRK and %d at OMSJ in %d seasons, %s at OMFJ (calibration.json); Sectors 8 and 9 have no fog evidence' % (
            h_rk, h_sj, max(n_rk, n_sj), 'none' if not h_fj else '%d' % h_fj)
    else:
        evidence = 'fog is a west coast problem (the fog hours by station are not read: calibration.json is missing or in another shape; owner: the weather desk); Sectors 8 and 9 have no fog evidence'
    hist = history_block()
    reports = ((hist.get('allSectors') or {}).get('reports')) if hist.get('available') else None
    aws = _ncm_station_visibility(data_dir)
    if aws:
        names = ', '.join(str(x).split(' (')[0] for x in (aws.get('stations_with_visibility') or [])[:4])
        aws_txt = 'No observed visibility other than the airports OMRK, OMSJ and OMFJ; the NCM stations report visibility at %d of %d sites in the response read on 24 Oct 2026%s' % (
            aws['stations_with_any_visibility_value'], aws['stations_total'], (' (%s)' % names) if names else '')
    else:
        aws_txt = 'No observed visibility other than the airports OMRK, OMSJ and OMFJ is read; the NCM station response is not on file (owner: NCM)'
    return {
        'planProcedure': 'none',
        'gap': {'what': 'The rain plan WA-PSD-PLN-05 has no fog procedure: no fog, visibility or dust text in its builders (audit of 24 Oct 2026), no stage trigger, no action.',
                'owner': 'Waste Management with LOD', 'status': 'named gap'},
        'otherGaps': [
            {'what': 'No fog or visibility threshold in metres is published by NCM in what was read; the thresholds here are the calibration of 24 Oct 2026 (Proposed)', 'owner': 'Wet Utilities with NCM'},
            {'what': 'Shift start times and the staff bus timetable are not on file', 'owner': 'Collection Manager'},
            {'what': ('The incident register has no fog, mist or visibility field: none of its %s records mentions them, so fog cannot be set against reports' % '{:,}'.format(reports)) if reports
                    else 'The incident register has no fog, mist or visibility field, so fog cannot be set against reports', 'owner': 'the incident register owner'},
            {'what': aws_txt, 'owner': 'NCM'}],
        'exposure': {'sectors': ['S1', 'S2', 'S3', 'S4', 'S5', 'S6'], 'routesBySector': west, 'routesTotal': sum(west.values()) if routes is not None else None, 'evidence': evidence,
                     'routesSource': ('data/rak_subsectors.geojson (collection routes by sector), Recomputed' if routes is not None else 'not read: ' + routes_problem)},
        'proposedMeasures': [
            {'id': 'F1', 'measure': 'Night and early collection start: hold the start of collection rounds in the exposed sectors while a station reports visibility under 1000 m, and restart when it reports more', 'for': 'Collection Manager', 'status': 'Proposed for a decision'},
            {'id': 'F2', 'measure': 'Tanker and staff bus moves: no non essential move of hire tankers, own tankers and the staff buses while visibility is under 1000 m; essential moves as RAK Police advises', 'for': 'LOD with the Collection Manager', 'status': 'Proposed for a decision'},
            {'id': 'F3', 'measure': 'Read the fog level and the OMRK and OMSJ visibility at the start of every shift and tell the sector duty leads; the levels are watch, alert and severe in weather.json (fogLevel)', 'for': 'Weather lead', 'status': 'Proposed for a decision'},
            {'id': 'F4', 'measure': 'Speed advice to drivers in fog: the wording is agreed with RAK Police; no speed figure is on file and none is proposed here', 'for': 'Director with RAK Police', 'status': 'Proposed for a decision'},
            {'id': 'F5', 'measure': 'Record fog on the incident register as its own type, so that the next season can be read against reports', 'for': 'the incident register owner', 'status': 'Proposed for a decision'}],
        'levelNow': fog_level,
        'status': 'Proposed; not plan content. The plan stays silent on fog until the committee decides.'}


def fog_stage_line(fog):
    """the fog line of the stage block: shown beside the stage, never inside it (the plan has no fog trigger)"""
    if not fog:
        return None
    lvl = fog.get('level')
    why = None
    ow = fog.get('observedWhy') or []
    if ow:
        w = ow[0]
        why = 'observed: %s %s, %s' % (w.get('station'), w.get('at'), w.get('rule'))
    elif fog.get('forecastWhy'):
        fw = fog['forecastWhy']
        why = 'forecast: %s spread %s C at %s' % (fw.get('model'), fw.get('spread'), str(fw.get('point') or '').split(' (')[0])
    elif fog.get('ncmIds'):
        why = 'NCM fog colour in force (%s)' % ', '.join(fog['ncmIds'])
    return {'level': lvl, 'status': 'Proposed (the fog ladder); Reported (stations); Reported, official (NCM)', 'staged': False, 'why': why,
            'line': 'Fog level %s%s' % (lvl, (': ' + why) if why else ''),
            'note': 'The rain plan has no fog procedure and no fog stage trigger, so fog is shown beside the stage and does not change it; the fog measures F1 to F5 are Proposed for a decision (actions.fog) and the fog row of the actions due now points to them.'}


# ------------------------------------------------------------------------------------------------ stage from the weather alone
RAIN_BAND_EDGES = (5, 15, 30)       # plan section 6.1: up to 5 mm routine, over 5 to 15 Enhanced watch, over 15 to 30 Pre deployment, over 30 Active response (a test ties them to the plan's own table, share_blocks.bands)


def rain_band(x):
    """the stage index (0 to 3) the plan's 24 hour bands give for x mm in 24 hours"""
    return 0 if x <= RAIN_BAND_EDGES[0] else (1 if x <= RAIN_BAND_EDGES[1] else (2 if x <= RAIN_BAND_EDGES[2] else 3))


def stage_block(rain_level_model, ncm_rain, worst_max6, worst_max24, worst_point24, plan_thresholds, rain_known, console_stage=None, ncm_upcoming=False, fog=None, flags=None,
                station_rain=None, vote=None):
    """The stage the weather alone would call under the plan's own triggers (section 6.1 and the stage table). An input to the committee, never the stage.
    24 h bands at the worst point, on the heaviest 24 hours that begin within the next 24 hours (rain expected in 24 hours): up to 5 mm Routine; over 5 to 15 Enhanced watch;
    over 15 to 30 Pre deployment; over 30 Active response. A burst of the plan watch value in 6 h is Enhanced watch and of the plan high value Pre deployment. NCM yellow, orange,
    red are Enhanced watch, Pre deployment, Active response, from the time the warning is read (a warning issued for later counts). The model level is Enhanced watch for watch and
    Pre deployment for high, as on the public card. Fog is a line beside the stage, not an input to it. flags: sentences that must travel with the stage (an NCM page colour that
    nobody has logged, for example)."""
    band = rain_band
    ins = []
    best = 0
    if worst_max24 is not None:
        b = band(worst_max24)
        ins.append({'input': 'rain in 24 h at the worst point (ensemble median, the heaviest 24 hours that begin within the next 24 hours)', 'value': '%.1f mm at %s' % (worst_max24, worst_point24 or 'n/a'), 'stage': STAGES[b],
                    'planTrigger': 'plan section 6.1: up to 5 mm routine; 5 to 15 mm Enhanced watch; 15 to 30 mm Pre deployment; over 30 mm Active response'})
        best = max(best, b)
    if worst_max6 is not None:
        b = 2 if worst_max6 >= plan_thresholds['high'] else (1 if worst_max6 >= plan_thresholds['watch'] else 0)
        ins.append({'input': 'worst 6 h burst at any point (ensemble median)', 'value': '%.1f mm' % worst_max6, 'stage': STAGES[b],
                    'planTrigger': 'plan section 6.1: %g mm in 6 hours raises Enhanced watch and %g mm Pre deployment' % (plan_thresholds['watch'], plan_thresholds['high'])})
        best = max(best, b)
    if rain_level_model in RAIN_STAGE:
        b = RAIN_STAGE[rain_level_model]
        ins.append({'input': 'model level (plan rule unless the proposed vote is applied)', 'value': rain_level_model, 'stage': STAGES[b], 'planTrigger': 'the public card rule of 17 Oct 2026: model watch is Enhanced watch, model high is Pre deployment'})
        best = max(best, b)
    nb = {'yellow': 1, 'orange': 2, 'red': 3}.get(ncm_rain, 0)
    ins.append({'input': 'NCM rain colour issued, not yet in force (Reported, official)' if (ncm_upcoming and nb) else 'NCM rain colour in force (Reported, official)', 'value': ncm_rain or 'none', 'stage': STAGES[nb],
                'planTrigger': 'plan stage table: yellow is Enhanced watch, orange Pre deployment, red Active response'})
    best = max(best, nb)
    if station_rain:
        # a station report of rain makes the rain hazard level a watch: the stage follows it, so the level and the stage are not a step apart (audit V11)
        ins.append({'input': 'a station reports rain now (Reported): %s' % ', '.join(station_rain), 'value': 'rain reported', 'stage': STAGES[1],
                    'planTrigger': 'the engine rule: a station report of rain, a shower or a thunderstorm is a rain watch, as the rain hazard level says'})
        best = max(best, 1)
    unknown = (not rain_known) and best == 0
    stage = 'unknown' if unknown else STAGES[best]
    setters = [i['input'] for i in ins if STAGES.index(i['stage']) == best and best > 0] if not unknown else []
    out = {'weatherStage': stage, 'status': 'Proposed (the weather alone, under the plan triggers); the stage is set by the rain committee',
           'rule': 'The highest of: the 24 h band at the worst point, the 6 h burst thresholds of the plan, the model level, and the NCM colour; unknown, never Routine, when the model could not be read (or an NCM page colour is not logged) and nothing else calls a stage.',
           'inputs': ins, 'setBy': setters, 'flags': list(flags or []),
           'owners': 'The console stage logic and the plan stage table own the stage. This is an input and a flag when the two differ.',
           'consoleStage': 'not readable: the console keeps its stage per device (browser storage wma_rain_stage_v1) and no stage file is written by Barq',
           'differsFromRecorded': None, 'fog': fog_stage_line(fog), 'vote': vote}
    if console_stage and console_stage.get('stage') in STAGES and stage in STAGES:
        out['recordedStage'] = console_stage
        if console_stage.get('stale'):
            out['recordedStageNote'] = 'the recorded stage (%s, %s) is %s hours old and is not compared with the weather stage as if it were current' % (console_stage['stage'], console_stage.get('asof'), console_stage.get('ageHours'))
        else:
            out['differsFromRecorded'] = STAGES.index(console_stage['stage']) != STAGES.index(stage)
            if out['differsFromRecorded']:
                out['flag'] = 'The weather alone calls %s; the recorded stage is %s.' % (stage, console_stage['stage'])
    return out


# ------------------------------------------------------------------------------------------------ history (S24 incident register)
_hist_cache = {}


def history_block():
    if 'h' in _hist_cache:
        return _hist_cache['h']
    try:
        return _history_block_raw()
    except Exception as e:                                   # the last net: a history that cannot be built is a named gap, never a stopped link to the plan (audit F9)
        msg = 'the history extracts of the incident register could not be read (%s: %s); owner: the register owner' % (type(e).__name__, str(e)[:80])
        h = {'available': False, 'note': msg, 'problems': [msg]}
        _hist_cache['h'] = h
        return h


def _history_block_raw():
    s = read_json(_p('data', 're_summary.json'), None)
    pts = read_json(_p('data', 're_points.json'), None)
    detail = read_json(_p('data', 're_detail.json'), None)
    trend = read_json(_p('data', 're_trend.json'), None)
    geoms = load_sector_geometry()
    if not isinstance(s, dict):
        h = {'available': False, 'note': 'data/re_summary.json is not readable: no comparison with last season (owner: the register owner)',
             'problems': ['data/re_summary.json is missing or not in the shape the engine reads: no comparison with last season (owner: the register owner)']}
        _hist_cache['h'] = h
        return h
    # every extract of the incident register is read tolerantly: one that has changed shape loses its own figures, named in problems, and never the actions list or the event record (audit F9)
    problems = []
    pbs = s.get('peakBySector')
    pbs = pbs if isinstance(pbs, dict) else {}
    by = {n: {'reports': 0, 'flooding': 0, 'peakDayReports': pbs.get(n)} for n in map(str, range(1, 10))}
    outside = 0
    pts_list = pts.get('points') if isinstance(pts, dict) else None
    if isinstance(pts_list, list) and geoms:
        bad_pts = 0
        for p in pts_list:
            try:
                lon, lat, cat = p[0], p[1], p[2]
                n = sector_of(lon, lat, geoms)
            except (IndexError, TypeError, KeyError):
                bad_pts += 1
                continue
            if n is None:
                outside += 1
                continue
            by[n]['reports'] += 1
            if cat == 'F':
                by[n]['flooding'] += 1
        if bad_pts:
            problems.append('data/re_points.json: %d point(s) of the register are not in the shape the engine reads and are left out of the counts by sector (owner: the register owner)' % bad_pts)
    else:
        problems.append('data/re_points.json or data/sectors.geojson is missing or in another shape: the counts of last season\'s reports by sector are not shown (owner: the register owner)')
        for n in by:
            by[n]['reports'], by[n]['flooding'] = None, None
    allrep = None
    try:
        row = next(x for x in detail['response']['rows'] if x.get('label') == 'All reports')
        allrep = {'fieldClosureMedianH': row['field']['median'], 'fieldClosures': row['field']['n'], 'reports': row.get('n')}
    except (TypeError, KeyError, StopIteration, AttributeError, IndexError):
        allrep = None
        if detail is not None:
            problems.append('data/re_detail.json is not in the shape the engine reads: the closure time of all reports is not shown (owner: the register owner)')
    tr = None
    try:
        wk = trend['weeks']
        pk = max(wk, key=lambda x: (x.get('flooding') or 0) + (x.get('roads') or 0))
        tr = {'weeks': len(wk), 'days': len(trend.get('days') or []), 'peakWeek': pk.get('week'), 'peakWeekFlooding': pk.get('flooding'), 'peakWeekRoads': pk.get('roads'), 'peakWeekCritical': pk.get('critical')}
    except (TypeError, KeyError, ValueError, AttributeError, IndexError):
        tr = None
        problems.append('data/re_trend.json is %s: the weekly trend is not shown (owner: the register owner)' % ('missing or cannot be read' if trend is None else 'not in the shape the engine reads'))
    h = {'available': True, 'status': 'Recomputed (counts by point in polygon of data/re_points.json on data/sectors.geojson; peak day counts and the critical median from data/re_summary.json; closure median of all reports from data/re_detail.json; weeks from data/re_trend.json)',
         'source': 'S24 incident register, m85.ae/re extract of 14 Oct 2026', 'period': s.get('period'), 'bySector': by, 'outsideSectors': outside,
         'allSectors': {'reports': s.get('total'), 'flooding': s.get('flooding'), 'critical': s.get('critical'), 'peakDay': s.get('peakDay'), 'peakDayReports': s.get('peakDayReports'),
                        'criticalMedianClosureH': s.get('critMedianH'), 'allReportsClosure': allrep},
         'trend': tr,
         'gap': 'The median closure time by sector is not in re_summary.json, re_detail.json or re_trend.json: the files give the critical median (%s h) and the median of all reports (%s h on the field closures) for all sectors. The register workbook holds the closure times (owner: the register owner).' % (
             s.get('critMedianH'), (allrep or {}).get('fieldClosureMedianH', 'not read')),
         'fog': 'The register has no fog, mist or visibility field: no comparison for fog (named gap, owner: the register owner)', 'problems': problems}
    _hist_cache['h'] = h
    return h


def history_for(hazard, sectors):
    try:
        h = history_block()
    except Exception as e:                                   # whatever an extract held: no history for this call, never a failed event record (audit F9)
        return {'available': False, 'note': 'the history extracts of the incident register could not be read (%s: %s); owner: the register owner' % (type(e).__name__, str(e)[:80]), 'problems': []}
    if not h.get('available'):
        return h
    if hazard == 'fog':
        return {'note': h['fog'], 'allSectors': None}
    sel = {('S' + n): h['bySector'][n] for n in (sectors or []) if n in h['bySector']}
    return {'status': h['status'], 'source': h['source'], 'sectors': sel, 'allSectors': h['allSectors'], 'gap': h['gap']}


def plan_totals(res):
    """the plan's own totals, read from its files, and a check that the by-sector figures add up to them"""
    out = {}
    pb, mp, tk = res.get('pumpops'), res.get('manpower'), res.get('tankers')
    try:
        if pb:
            sc = pb.get('scenarios', {})
            out['pumps'] = {k: sc[k].get('pumps') for k in ('counted', 'screened', 'red', 'full') if k in sc}
            out['pumpsBySectorSum'] = {k: sum(x for x in sc[k].get('dist', []) if isinstance(x, (int, float))) for k in ('counted', 'screened', 'red', 'full') if k in sc}
    except (TypeError, AttributeError):
        out.pop('pumps', None)
        out.pop('pumpsBySectorSum', None)
    try:
        if tk:
            tiers = tk.get('tiers') if isinstance(tk.get('tiers'), dict) else {}
            cases = tk.get('cases') if isinstance(tk.get('cases'), dict) else {}
            out['hireTankers'] = {'reserved': tiers.get('reserved'), 'contingency': tiers.get('contingency'), 'total': tiers.get('total'),
                                  'atOrange': cases.get('orange'), 'atRed': cases.get('red'), 'source': 'tankers.json (tiers, cases)'}
        if mp:
            rows = [r for r in (mp.get('rows') or []) if isinstance(r, dict)]
            num = lambda v: v if isinstance(v, (int, float)) else 0
            out['hireTankersBySectorSum'] = {'orange': sum(num(r.get('tankersOrange')) for r in rows), 'red': sum(num(r.get('tankersRed')) for r in rows), 'source': 'manpower.json rows'}
            out['gangsAndLabour'] = {'supervisorsDay': (mp.get('total') or {}).get('supervisors'), 'labourDay': (mp.get('total') or {}).get('labourDay'), 'nightLabour': (mp.get('night') or {}).get('labour')}
    except (TypeError, AttributeError):
        out.pop('hireTankers', None)
        out.pop('hireTankersBySectorSum', None)
    checks = []
    if out.get('hireTankers') and out.get('hireTankersBySectorSum'):
        a, b = out['hireTankers'], out['hireTankersBySectorSum']
        if (a['atOrange'], a['atRed']) != (b['orange'], b['red']):
            checks.append('hire tankers by sector (%s at orange, %s at red) do not add up to tankers.json (%s, %s): the plan files disagree' % (b['orange'], b['red'], a['atOrange'], a['atRed']))
    if out.get('pumps') and out.get('pumpsBySectorSum') and out['pumps'] != out['pumpsBySectorSum']:
        checks.append('pump counts by sector do not add up to the plan totals in pumpops.json')
    out['checks'] = checks or ['the by sector figures add up to the plan totals']
    return out


# ------------------------------------------------------------------------------------------------ the whole block
def build(now, point_levels, fog_point_levels, hazards, stage_in, triggers, point_summaries, sector_fog_extra, stage_pre=None, data_dir=None, extra_gaps=None, fog_info=None, vote_info=None):
    """interaction.json: hazards, sectors with levels and resources, the stage, the actions, the fog gap, the history.
    point_levels: {point index: rain level none|watch|high|unknown}; fog_point_levels: {point index: fog level name or None};
    hazards: {'rain': {...}, 'fog': {...}, 'dust': {...}}; stage_in: kwargs for stage_block; point_summaries: {idx: {'next24','max6','ens'...}}.
    extra_gaps: named gaps found by the engine (a plan file read in another shape). fog_info: the fog assessment, for the fog line and the fog row."""
    sectors, table_src, geo_ok = sector_map()
    res = plan_resources()
    hist = history_block()
    plan_file_gaps = []
    if not geo_ok:
        plan_file_gaps.append('data/sectors.geojson is missing or cannot be read: the sector geometry was not checked, and a sector with no point of its own has no nearest point (owner: the plan builder)')
    if str(table_src).startswith('embedded'):
        plan_file_gaps.append('the plan call table could not be read from share_blocks.py or build_plan_split.py: the embedded copy of it is used, which may be out of date (owner: the plan builder)')
    plan_file_gaps.extend(hist.get('problems') or [])
    _rts, _rts_problem = fog_routes_checked()
    if _rts_problem:
        plan_file_gaps.append(_rts_problem)
    out_sectors = []
    ncm_rain = (hazards['rain'].get('ncm') or {}).get('level')
    ncm_fog = (hazards['fog'].get('ncm') or {}).get('level')
    fog_ncm_level = {'yellow': 'watch', 'orange': 'alert', 'red': 'severe'}.get(ncm_fog, 'none')
    rank_fog = {'none': 0, 'watch': 1, 'alert': 2, 'severe': 3}
    for n in map(str, range(1, 10)):
        s = sectors[n]
        idxs = list(s['points'])
        basis = 'points in the sector (%s)' % '; '.join(POINTS[i][0].split(' (')[0] for i in idxs) if idxs else None
        if s['unmapped'] and s['nearest']:
            idxs = [s['nearest']['point']]
            basis = 'unmapped: no point of its own; the nearest point %s at %.1f km (%s) is shown instead' % (s['nearest']['name'].split(' (')[0], s['nearest']['distanceKm'], s['nearest']['from'])
        levels = [point_levels.get(i) for i in idxs]
        rl = 'unknown' if (not levels or any(l == 'unknown' for l in levels) and not any(l in ('watch', 'high') for l in levels)) else max(levels, key=lambda x: RAIN_STAGE.get(x, 0))
        ncm_to = {'yellow': 'watch', 'orange': 'high', 'red': 'high'}.get(ncm_rain, 'none')
        comb = rl if RAIN_STAGE.get(rl, -1) >= RAIN_STAGE[ncm_to] else ncm_to
        if rl == 'unknown' and ncm_to == 'none':
            comb = 'unknown'
        fl_pts = [fog_point_levels.get(i) for i in idxs if fog_point_levels.get(i) is not None]
        evidence = n in ('1', '2', '3', '4', '5', '6') or (s['unmapped'] and idxs and POINTS[idxs[0]][0].startswith(FOG_POINT_PREFIXES))
        if evidence:
            fmodel = max(fl_pts, key=lambda x: rank_fog[x]) if fl_pts else 'unknown'
            fobs = hazards['fog'].get('observedLevel', 'none')
            # 29 Oct 2026 (fourth audit, FOG-AIRPORTS-NONE): the engine gives an observed level of unknown when no station reported in the last 3 hours (or both west coast stations are silent);
            # it ranks as nothing found and a sector row that would read none reads unknown
            fobs_r = fobs if fobs in rank_fog else 'none'
            fl = max([fmodel if fmodel != 'unknown' else 'none', fobs_r, fog_ncm_level], key=lambda x: rank_fog[x])
            if (fmodel == 'unknown' or fobs not in rank_fog) and fl == 'none':
                fl = 'unknown'
            if fl == 'none' and hazards['fog'].get('level') == 'unknown':
                fl = 'unknown'                 # the headline fog level could not be read (no station answered, or a page colour is not logged): a sector row never reads none under it
            fog_row_ = {'level': fl, 'forecast': fmodel, 'observed': fobs, 'ncm': fog_ncm_level,
                        'evidence': ('west plain: the calibrated fog rule applies' if n in ('1', '2', '3', '4', '5', '6') else
                                     'unmapped sector: the rule of its nearest point, a west plain point (%s), is shown; the sector is not in the fog exposure list S1 to S6 (owner: Wet Utilities, who place the sector)' % POINTS[idxs[0]][0].split(' (')[0])}
        else:
            fl = fog_ncm_level if fog_ncm_level != 'none' else 'not assessed'
            fog_row_ = {'level': fl, 'forecast': None, 'observed': None, 'ncm': fog_ncm_level, 'evidence': 'east coast and mountains: no fog evidence in seven seasons (OMFJ), no rule applies; shows an NCM fog warning only'}
        ps = [point_summaries.get(i) or {} for i in idxs]
        if hazards['rain'].get('level') == 'unknown' and comb == 'none':
            comb = 'unknown'
        row = {'sector': 'S' + n, 'name': SECTOR_NAMES[n], 'points': [POINTS[i][0] for i in (s['points'] or [])], 'pointsShown': [POINTS[i][0] for i in idxs], 'unmapped': s['unmapped'], 'mappingBasis': basis or s['mapping'],
               'mappingNotes': s['notes'], 'nearestPoint': s['nearest'],
               'rain': {'level': comb, 'model': rl, 'ncm': ncm_to, 'next24Mm': r1(max([p.get('next24') for p in ps if p.get('next24') is not None] or [None])) if any(p.get('next24') is not None for p in ps) else None,
                        'max6Mm': r1(max([p.get('max6') for p in ps if p.get('max6') is not None] or [None])) if any(p.get('max6') is not None for p in ps) else None},
               'fog': fog_row_, 'resources': sector_resources(n, res),
               'history': {'reportsLastSeason': hist.get('bySector', {}).get(n, {}).get('reports'), 'floodingReports': hist.get('bySector', {}).get(n, {}).get('flooding'),
                           'peakDayReports': hist.get('bySector', {}).get(n, {}).get('peakDayReports')} if hist.get('available') else None}
        out_sectors.append(row)
    stg = stage_pre or stage_block(**stage_in, console_stage=read_stage_file(now))
    totals = plan_totals(res)
    acts = actions_block(now, hazards['rain']['level'], ncm_rain, triggers, hazards['fog']['level'], stage=stg, fog_info=fog_info, data_dir=data_dir, vote_info=vote_info)
    gaps_found = list(res['missing']) + list(extra_gaps or []) + plan_file_gaps
    if not acts.get('listRead'):
        gaps_found.append('actions17.json: no action list was read, so the actions due now cannot be listed (owner: the plan builder)')
    if triggers is None:
        gaps_found.append('data/weather/action_triggers.json: no usable entry, so no action is tied to a level (owner: the weather desk)')
    fog_sec = {r['sector']: r['fog']['level'] for r in out_sectors if r['fog']['level'] != 'not assessed'}
    out = {'schema': 1, 'asof': f_local(now), 'status': 'Model forecast (Proposed), Reported and Reported, official, each labelled where it appears; plan figures are read from the plan files, never retyped',
           'hazards': hazards, 'sectors': out_sectors, 'stage': stg, 'actions': acts, 'vote': vote_info, 'history': hist, 'fog': {'level': hazards['fog']['level'], 'basis': hazards['fog'].get('basis'), 'stageLine': (stg or {}).get('fog'),
                                                                                                       'sectors': fog_sec, 'dueNowRow': any(x.get('id') == 'FOG' for x in acts.get('dueNow', [])),
                                                                                                       'planProcedure': 'none', 'measures': [m['id'] for m in acts['fog']['proposedMeasures']]},
           'planSources': res['sources'], 'planGaps': gaps_found, 'planTotals': totals, 'sectorMapSource': table_src, 'geometryChecked': geo_ok,
           'gaps': [{'what': 'No rain gauge register is on file', 'owner': 'Wet Utilities with NCM'},
                    {'what': 'Placement of pumps, tankers and gangs by sector is not on any register; the resources shown are the plan, not what stands ready', 'owner': 'Collection Manager with LOD (A20)'},
                    {'what': 'No rain point for the upper Wadi Bih catchment (mostly in Oman) and none for Sector 7', 'owner': 'Director (A3) for the catchment; Wet Utilities for Sector 7'},
                    {'what': 'The console stage and the manual alert are kept per device and cannot be read by the engine', 'owner': 'Console owner'}]}
    return out
