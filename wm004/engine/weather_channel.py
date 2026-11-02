# -*- coding: utf-8 -*-
"""WM004 weather channel (25 Oct 2026): one channel that turns every weather reading and level into triggers, readiness, notifications, acknowledgements and a record.

the owner asked (24 Oct 2026): "treat this as weather channel, that connects every weather reading and forecast to notify and make readiness at its highest". The rain committee still sets the stage (the plan says
so); the channel recommends, notifies, tracks that people acknowledged, escalates when they do not, and records all of it.

    python weather_channel.py [run] [--drill] [--no-toast] [--dry-run] [--now ISO] [--data-dir D] [--weather W] [--staging S] [--conditions C]
    python weather_channel.py simulate --scenario NAME | --all [--json]      run a scenario on a scratch copy and print rung, rules, actions due and notifications
    python weather_channel.py status                                        read only: the current state in words
    python channel_ack.py list | ack | hold | approve | reject | sent | escalate   (people act on the records)

What a run does
  READ      the published weather feed (weather.json, the legacy or the engine copy), the engine staging copy (weather_engine.json), interaction.json, health.json, conditions.json and the
            NCM log. A file whose own asof is older than 3 hours is not current: its readings are no reading, never zero and never calm.
  FACTS     every reading as a named fact with its value, source, time, age and place (rain level, 6 h and 24 h rain, the NCM colours, fog, visibility, wind, gust, dust, pressure fall,
            heat, cold, dry air, the health of the feed and of the engine).
  RULES     data/weather/channel_rules.json: each rule is a condition over the facts, the rung it supports, the plan actions it starts, the tier and the audience (roles), a message.
  LADDER    the readiness ladder is the plan's own stage table (Routine readiness, Enhanced watch, Pre deployment, Active response). A rung rises at once and falls only after 3 consecutive
            clear hourly readings and at least 6 hours at the rung, one rung at a time. An unknown or stale reading never lowers it: the rung is held and a blind alert is raised.
            At the highest rung the channel marks READINESS HIGHEST and builds the full activation pack. It never sets a stage and never lowers one the committee has set.
  NOTIFY    one record in data/weather/outbox/ for every new trigger and every rung change, one per rule and cooldown. T0 (the desk owner) is automatic: a Windows toast on this laptop, the
            outbox file and the entry in the console. T1 to T3 are drafts the desk owner approves, unless autoSend is set for the tier in channel_local.json.
  TRACK     acknowledgement, hold and escalation (channel_ack.py); the append only log channel_log.jsonl; channel_state.json (atomic); channel.json, the compact copy for the console and the site.

Drill mode (channel_rules.json "drill": true, or --drill): every record is marked DRILL and nothing is sent beyond the desk owner and the outbox.
Transports are off unless set: WCH_SMTP_URL, WCH_WEBHOOK_URL, WCH_SMS_URL. Roles resolve to addresses only through contacts.local.json (never committed, never copied into any file here).
No name, number or address is written by this module: records hold roles and counts. Standard library only. See data/weather/SCHEMA.md."""
import os, sys, json, re, argparse, subprocess, base64, string, statistics, smtplib, ssl, urllib.request, urllib.parse, urllib.error, time, datetime as dt
import wx_util as U
from wx_util import (HERE, DEFAULT_DATA_DIR, now_local, parse_local, f_local, floor_hour, hours_between, read_json, read_json_status, atomic_write_json, short_text, nz)
import wx_interact as I
import ncm_log as N
import channel_ack as K

RAIN_DIR = os.path.abspath(os.path.join(HERE, '..'))
CURRENT_H = 3.0                                       # a file older than this is not current (C6b)
STAGES = I.STAGES                                     # the plan's stage table: Routine readiness, Enhanced watch, Pre deployment, Active response
HIGHEST = len(STAGES) - 1
RUNG_GROUPS = {0: ['standing'], 1: ['standing', 'watch'], 2: ['standing', 'watch', 'high'], 3: ['standing', 'watch', 'high', 'red']}
RUNG_NEW_GROUP = {0: 'standing', 1: 'watch', 2: 'high', 3: 'red'}
TIER_OF_RUNG = {1: 'T1', 2: 'T2', 3: 'T3'}
SRC_MODEL, SRC_REPORT, SRC_OFFICIAL = 'Model forecast (Proposed)', 'Reported', 'Reported, official'
TOAST_APP = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'


# ------------------------------------------------------------------------------------------------ small helpers
def same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def plain(text):
    """visible prose without a dash: an en or em dash or a spaced hyphen becomes a comma (IDs and dates keep their hyphens; they are never prose)"""
    t = str(text or '')
    t = re.sub(r'\s*[–—]\s*', ', ', t)
    t = re.sub(r'\s+-\s+', ', ', t)
    return ' '.join(t.split())


class _Safe(dict):
    def __missing__(self, k):
        return ''


def fill(template, ctx):
    return plain(string.Formatter().vformat(template, (), _Safe(ctx)))


def fact(v, label, unit='', src='', at=None, age=None, where=None, when=None, ref=None, note=None):
    return {'v': v, 'label': label, 'unit': unit, 'src': src, 'at': at, 'age': age, 'where': where, 'when': when, 'ref': ref, 'note': note}


def _tm(s):
    return str(s).replace('T', ' ')[:16] if s else None


def fmt_fact(f):
    v = f.get('v')
    if v is None or v == 'unknown':
        s = '%s: no reading%s' % (f['label'], (' (%s)' % f['note']) if f.get('note') else '')
    else:
        if isinstance(v, bool):
            vs = 'yes' if v else 'no'
        elif isinstance(v, float):
            vs = ('%.1f' % v)
        elif isinstance(v, (list, tuple)):
            vs = ', '.join(str(x) for x in v)
        else:
            vs = str(v)
        s = '%s %s%s' % (f['label'], vs, f.get('unit') or '')
    tail = []
    if f.get('src'):
        tail.append(f['src'])
    if f.get('at'):
        tail.append('read %s%s' % (f['at'], (', %.1f h old' % f['age']) if f.get('age') is not None else ''))
    return plain(s + ((' (' + '; '.join(tail) + ')') if tail else ''))


# ------------------------------------------------------------------------------------------------ paths and sources
def resolve_paths(a):
    dd = os.path.abspath(a.data_dir)
    real = same_dir(dd, DEFAULT_DATA_DIR)
    base = RAIN_DIR if real else dd
    return {'data': dd, 'real': real,
            'weather': os.path.abspath(a.weather or os.path.join(base, 'weather.json')),
            'staging': os.path.abspath(a.staging or os.path.join(base, 'weather_engine.json')),
            'conditions': os.path.abspath(a.conditions or os.path.join(base, 'conditions.json')),
            'channel_out': os.path.abspath(a.channel_out or os.path.join(base, 'channel.json')),
            'interaction': os.path.join(dd, 'interaction.json'), 'health': os.path.join(dd, 'health.json')}


def _read_src(name, path, now, current_h):
    doc, st = read_json_status(path)
    s = {'name': name, 'file': os.path.basename(path), 'path': path, 'status': st, 'doc': doc if (st == 'ok' and isinstance(doc, dict)) else None, 'asof': None, 'ageH': None, 'current': False, 'note': None}
    if st == 'ok' and s['doc'] is None:
        s['status'] = 'damaged'
    if s['doc'] is not None:
        a = None
        if name == 'health' and isinstance(s['doc'].get('lastRun'), dict):
            a = s['doc']['lastRun'].get('now')
        a = a or s['doc'].get('asof')
        try:
            d = parse_local(a)
        except (ValueError, TypeError):
            d = None
        if d is None:
            s['note'] = 'no readable asof'
        else:
            age = hours_between(d, now)
            if age < -0.25:
                s['note'] = 'asof %s is in the future: not used' % a
            else:
                s['asof'], s['ageH'] = f_local(d), round(max(0.0, age), 2)
    s['current'] = bool(s['doc'] is not None and s['ageH'] is not None and s['ageH'] <= current_h)
    return s


def read_sources(P, now, current_h=CURRENT_H):
    out, cache = {}, {}
    for name in ('weather', 'staging', 'interaction', 'conditions', 'health'):
        key = os.path.normcase(os.path.abspath(P[name]))
        if key not in cache:
            cache[key] = _read_src(name, P[name], now, current_h)
        out[name] = cache[key]
    return out


def read_live_snapshot(d, now):
    """What the NCM live reader said when the engine last asked the site (ncm_auto.py writes it). None when it was not asked in the last 3 hours: then WC-34 speaks, not this."""
    doc, status = read_json_status(os.path.join(d, 'ncm_live_state.json'))
    if status != 'ok' or not isinstance(doc, dict):
        return None
    try:
        age = hours_between(parse_local(doc.get('fetchedAt')), now)
    except (ValueError, TypeError):
        return None
    return doc if -0.25 <= age <= 3.0 else None


def merge_live(out, live, now):
    """A live warning raises the same rules a logged one does, even before it reaches the log: same hazards, same colours, same rule that a warning counts unless it is known not to touch RAK."""
    if not live or live.get('state') != 'WARNING':
        return
    for it in live.get('items') or []:
        c = it.get('colour')
        if c not in N.LEVELS or c == 'none' or (it.get('rak') == 'not touched' and not it.get('named')):
            continue
        try:
            if it.get('to') and parse_local(it['to']) < now:
                continue
        except (ValueError, TypeError):
            pass
        for h in it.get('hazards') or []:
            if h not in ('rain', 'fog', 'dust', 'wind'):
                continue
            cur = out['levels'].get(h)
            if N.RANK[c] > N.RANK.get(cur, 0):
                out['levels'][h] = c
                out['ids'][h] = list(out['ids'].get(h) or []) + ['live %s' % it.get('id')]
                out['strong'][h] = {'id': 'live %s' % it.get('id'), 'areas': 'RAK %s' % (it.get('rak') or 'not known'), 'from': _uae_of(it.get('from')), 'to': _uae_of(it.get('to')), 'read': live.get('readAt'), 'level': c}


def _uae_of(s):
    try:
        return f_local(parse_local(s)) if s else None
    except (ValueError, TypeError):
        return None


def read_ncm(P, now, weather_src):
    """the NCM log read through ncm_log.py: the colours in force or issued (an entry that names Ras Al Khaimah or does not say), the age of the freshest warnings reading, unreadable lines.
    Without a log, the ncm block of weather.json is used and says so. A warning of the live reader counts at once, and its snapshot is kept for the facts."""
    d = P['data']
    out = {'exists': os.path.exists(N.log_path(d)), 'entries': [], 'bad': 0, 'levels': {}, 'ids': {}, 'ageH': None, 'unreadable': None, 'strong': {}, 'fallback': False, 'note': None, 'refused': [], 'refusedUnreadable': False}
    if out['exists']:
        entries, bad = N.load(d)
        out['entries'], out['bad'] = entries, bad
    entries = out['entries']
    if entries:
        s = N.summarize(entries, now)
        for h in ('rain', 'fog', 'dust'):
            out['levels'][h] = s['levels'][h]
            out['ids'][h] = s['ids'][h]
        lv, ids = N.level_for(entries, now, 'wind', require_rak=True, include_upcoming=True)
        out['levels']['wind'], out['ids']['wind'] = lv, ids
        ages = [x for x in (s.get('warningsAgeHours'), s.get('autoAgeHours')) if x is not None]
        out['ageH'] = min(ages) if ages else None
        out['manualAgeH'], out['autoAgeH'] = s.get('warningsAgeHours'), s.get('autoAgeHours')
        un = N.unreadable_status(d, out['bad'])
        out['unreadable'] = bool(un and un.get('pending'))
        for h in ('rain', 'fog', 'dust', 'wind'):
            pool = [e for e in (N.in_force_at(entries, now, h) + N.upcoming_at(entries, now, h)) if e.get('rakNamed') != 'no']
            if pool:
                e = max(pool, key=lambda x: (N.RANK[x['level']], x['read'], x['id']))
                out['strong'][h] = {'id': e['id'], 'areas': short_text(e.get('areas') or '', 70), 'from': e.get('from'), 'to': e.get('to'), 'read': e.get('read'), 'level': e['level']}
    try:                                               # a typed reading that the log refused is an official warning nobody can see in the log: read apart from the log
        rs = N.refused_status(d, entries, now)
        out['refused'] = list(rs.get('open') or [])
        out['refusedUnreadable'] = bool((rs.get('unreadable') or {}).get('open'))
    except Exception:
        out['refused'], out['refusedUnreadable'] = None, None
    if not entries and weather_src and weather_src.get('doc') and isinstance(weather_src['doc'].get('ncm'), dict):
        n = weather_src['doc']['ncm']
        out['fallback'] = True
        out['note'] = 'no NCM log: the ncm block of %s is used' % weather_src['file']
        if n.get('level') in N.LEVELS:
            out['levels']['rain'] = n['level']
        try:
            out['ageH'] = round(max(0.0, hours_between(parse_local(n['asof']), now)), 1) if n.get('asof') else None
        except (ValueError, TypeError):
            out['ageH'] = None
    # last, so that the fallback above can never write a calm level over a live warning
    out['live'] = read_live_snapshot(d, now)
    merge_live(out, out['live'], now)
    return out


# ------------------------------------------------------------------------------------------------ facts
def _max24_from_hourly(hourly):
    """the plan's rain in 24 hours (the heaviest 24 hours that begin within the next 24 hours) from the hourly list of a legacy feed, [[time, mm], ...]. A window with an hour that has no reading is not counted
    as if the hour were dry: it is skipped. None when no window is complete."""
    if not isinstance(hourly, list):
        return None
    vals = []
    for x in hourly:
        vals.append(nz(x[1]) if isinstance(x, (list, tuple)) and len(x) > 1 else None)
    best = None
    for s0 in range(0, min(24, len(vals) - 23) if len(vals) >= 24 else 0):
        w = vals[s0:s0 + 24]
        if any(v is None for v in w):
            continue
        t = sum(w)
        best = t if best is None or t > best else best
    return best


def _side(hz, key, side):
    h = hz.get(key) if isinstance(hz, dict) else None
    x = h.get(side) if isinstance(h, dict) else None
    return x if isinstance(x, dict) else {}


def _plus(f, **kw):
    f.update(kw)
    return f


def build_facts(src, ncm, P, now, rules_doc, prev=None):
    """every reading the rules use, as a named fact: {'v', 'label', 'unit', 'src', 'at', 'age', 'where', 'when', 'ref', 'note'}; v is None when there is no reading. Returns (facts, notes)."""
    F, notes = {}, []
    docs, seen = [], set()
    for n in ('staging', 'weather'):
        s = src[n]
        if id(s) in seen or s['doc'] is None:
            continue
        seen.add(id(s))
        docs.append(s)
    cur = [s for s in docs if s['current'] and 'modelLevel' in s['doc']]
    # ---- rain
    known = [(I.RAIN_STAGE[s['doc']['modelLevel']], s) for s in cur if s['doc'].get('modelLevel') in I.RAIN_STAGE]
    if known:
        r, s = max(known, key=lambda x: x[0])
        F['rain.model'] = fact(['none', 'watch', 'high'][r], 'model rain level', '', SRC_MODEL, s['asof'], s['ageH'], ref='%s modelLevel' % s['file'])
    else:
        why = 'the feed says unknown' if cur else ('no weather feed is current (last %s)' % ', '.join('%s %s' % (s['file'], s['asof'] or 'no asof') for s in docs) if docs else 'no weather feed file')
        F['rain.model'] = fact(None, 'model rain level', '', SRC_MODEL, note=why, ref='weather.json')
    w6, w24, npts, vote, ia_note = None, None, None, None, None
    for s in cur:
        d = s['doc']
        pts = d.get('points') if isinstance(d.get('points'), list) else None
        if pts is not None:
            ok = [p for p in pts if isinstance(p, dict) and 'error' not in p]
            npts = max(npts if npts is not None else 0, len(ok))
            for p in ok:
                x6 = nz(p.get('max6'))
                if x6 is not None and (w6 is None or x6 > w6[0]):
                    w6 = (x6, p.get('name'), p.get('peakAt'), s)
                x24 = nz(p.get('max24Within24'))
                if x24 is None:
                    x24 = _max24_from_hourly(p.get('hourly'))
                if x24 is None:
                    x24 = nz(p.get('next24'))
                if x24 is not None and (w24 is None or x24 > w24[0]):
                    w24 = (x24, p.get('name'), None, s)
        m6 = nz(d.get('worstMax6'))
        if m6 is not None and (w6 is None or m6 >= w6[0]):
            w6 = (m6, (w6[1] if w6 else None), (w6[2] if w6 else None), s)
    F['rain.worst6'] = fact(w6[0] if w6 else None, 'worst 6 h rain at a point', ' mm', SRC_MODEL, w6[3]['asof'] if w6 else None, w6[3]['ageH'] if w6 else None, where=short_text((w6[1] or '').split(' (')[0], 60) if w6 else None,
                            when=('peak hour ' + _tm(w6[2])) if (w6 and w6[2]) else None, ref='weather.json worstMax6', note=None if w6 else 'no current rain reading')
    F['rain.worst24'] = fact(w24[0] if w24 else None, 'rain in 24 h at the worst point', ' mm', SRC_MODEL, w24[3]['asof'] if w24 else None, w24[3]['ageH'] if w24 else None,
                             where=short_text((w24[1] or '').split(' (')[0], 60) if w24 else None, when='the next 24 hours' if w24 else None, ref='weather.json points', note=None if w24 else 'no current rain reading')
    band = I.rain_band(w24[0]) if w24 else None
    F['rain.band24'] = fact(band, 'plan 24 h band at the worst point', '', SRC_MODEL, F['rain.worst24']['at'], F['rain.worst24']['age'], where=F['rain.worst24']['where'], ref='plan section 6.1 bands 5, 15 and 30 mm')
    mv = F['rain.model']['v']
    parts = [x for x in ((I.RAIN_STAGE.get(mv) if mv else None), band) if x is not None]
    pr = max(parts) if parts else None
    F['rain.planRung'] = fact(pr, 'plan rain rung (the higher of the model level and the 24 h band)', '', SRC_MODEL, F['rain.worst24']['at'] or F['rain.model']['at'], F['rain.worst24']['age'] if F['rain.worst24']['at'] else F['rain.model']['age'],
                              where=F['rain.worst24']['where'] or F['rain.worst6']['where'], when=F['rain.worst6']['when'] or F['rain.worst24']['when'], ref='plan section 6.1')
    if pr is not None:
        F['rain.planRung']['label'] = 'plan rain rung (model level %s, %s mm in 24 h at the worst point; rung' % (mv or 'not read', ('%.1f' % w24[0]) if w24 else 'not read')
        F['rain.planRung']['unit'] = ')'
    F['rain.pointsRead'] = fact(npts, 'rain points read', ' of 8', SRC_MODEL, cur[0]['asof'] if cur else None, cur[0]['ageH'] if cur else None, ref='weather.json points', note=None if npts is not None else 'no current rain reading')
    # ---- the engine record (interaction.json, or the interaction block of a current engine copy)
    ia, ia_src = None, None
    if src['interaction']['current']:
        ia, ia_src = src['interaction']['doc'], src['interaction']
    else:
        for n in ('staging', 'weather'):
            s = src[n]
            if s['current'] and isinstance(s['doc'].get('interaction'), dict) and isinstance(s['doc']['interaction'].get('hazards'), dict):
                ia, ia_src = s['doc']['interaction'], s
                break
    hz = ia.get('hazards') if isinstance(ia, dict) and isinstance(ia.get('hazards'), dict) else None
    at, age = (ia_src['asof'], ia_src['ageH']) if ia_src else (None, None)
    nr = 'no current engine record (interaction.json older than 3 h or missing)'
    pv = (ia.get('proposedVote') if ia else None)
    if isinstance(pv, dict) and pv.get('level') in I.RAIN_STAGE:
        F['rain.voteAbove'] = fact(bool(pv.get('aboveRule')), 'proposed vote above the plan rule', '', SRC_MODEL, at, age, ref='interaction.json proposedVote')
        F['rain.voteLevel'] = fact(pv['level'], 'proposed vote of the models', '', SRC_MODEL, at, age, where=F['rain.worst6']['where'], ref='interaction.json proposedVote')
    else:
        F['rain.voteAbove'] = fact(None, 'proposed vote above the plan rule', '', SRC_MODEL, note=nr)
        F['rain.voteLevel'] = fact(None, 'proposed vote of the models', '', SRC_MODEL, note=nr)
    rob = ((hz or {}).get('rain') or {}).get('observed') if hz else None
    if isinstance(rob, dict) and rob.get('rainNow') is not None and isinstance(rob.get('stations'), list):
        F['rain.stationRain'] = fact(list(rob['stations']), 'stations reporting rain or thunder', '', SRC_REPORT, at, age, where=', '.join(rob['stations']) or None, when='now', ref='interaction.json hazards.rain.observed')
    else:
        F['rain.stationRain'] = fact(None, 'stations reporting rain or thunder', '', SRC_REPORT, note=nr if not hz else 'no station reported at this run')
    # ---- the NCM
    def ncm_fact(key, label, hazard_list):
        best, strong, ids = 'none', None, []
        known_any = False
        for h in hazard_list:
            lv = ncm['levels'].get(h)
            if lv in N.LEVELS:
                known_any = True
                if N.RANK[lv] > N.RANK[best]:
                    best, strong = lv, ncm['strong'].get(h)
                if lv != 'none':
                    ids += ncm['ids'].get(h, [])
        if not known_any:
            return fact(None, label, '', SRC_OFFICIAL, note='no NCM reading on record', ref='ncm_log.jsonl')
        where = ('Ras Al Khaimah; NCM areas: %s' % strong['areas']) if (strong and strong.get('areas')) else ('Ras Al Khaimah' if best != 'none' else None)
        when = ('%s to %s' % (_tm(strong['from']), _tm(strong['to']) if strong.get('to') else 'end not stated')) if strong and strong.get('from') else None
        return fact(best, label, '', SRC_OFFICIAL, (strong or {}).get('read'), (round(hours_between(parse_local(strong['read']), now), 1) if strong and strong.get('read') else None), where=where, when=when,
                    ref='ncm_log.jsonl ' + ', '.join(sorted(set(ids))[:3]))
    F['ncm.rain'] = ncm_fact('rain', 'NCM rain colour in force or issued', ['rain'])
    other = ncm_fact('other', 'NCM colour in force or issued for fog, dust or wind', ['fog', 'dust', 'wind'])
    F['ncm.otherMax'] = other
    F['ncm.ageH'] = fact(ncm['ageH'], 'age of the freshest NCM warnings reading', ' h', SRC_OFFICIAL, None, None, ref='ncm_log.jsonl', note=None if ncm['ageH'] is not None else 'the NCM warnings have never been read, or there is no log',
                         when=('last read %.1f h before this run' % ncm['ageH']) if ncm['ageH'] is not None else 'no NCM warnings reading on record')
    pend = None
    if hz and isinstance(((hz.get('rain') or {}).get('ncm')), dict):
        pend = bool(hz['rain']['ncm'].get('pagePending'))
    lv_ = ncm.get('live') or {}
    lv_ok = bool(lv_.get('fetched'))
    if lv_ok and (lv_.get('state') == 'TEXT WARNING' or (lv_.get('colourNotRead') or 0) > 0):
        pend = True                                     # a warning the reader saw with no colour: the levels are unknown, not none, from this run on
    F['ncm.pagePending'] = fact(pend, 'NCM page colour not logged', '', SRC_OFFICIAL, at, age, ref='interaction.json hazards.rain.ncm.pagePending', note=None if pend is not None else nr)
    F['ncm.readerAgeMin'] = fact(lv_.get('ageMinAtFetch') if lv_ok else None, 'minutes since the NCM live reader last wrote', ' min', SRC_OFFICIAL, lv_.get('readAt'), None, where='the NCM reader on the site',
                                 when=('read %s' % lv_.get('readAt')) if lv_ok else 'not asked at the last run', ref='ncm_live_state.json', note=None if lv_ok else 'the reader state was not fetched at the last run')
    F['ncm.readerState'] = fact(lv_.get('state') if lv_ok else None, 'state of the NCM live reader', '', SRC_OFFICIAL, lv_.get('readAt'), None, where='the NCM reader on the site', when=('read %s' % lv_.get('readAt')) if lv_ok else None,
                                ref='ncm_live_state.json', note=None if lv_ok else 'the reader state was not fetched at the last run')
    # the file could not be fetched at all: the laptop asked and got nothing. This is what WC-43 means by silent, and with no file there is no age to read (independent review, F12). No snapshot at all is no reading, not false.
    F['ncm.readerUnreachable'] = fact(None if not lv_ else (not lv_ok), 'NCM reader file not fetched', '', SRC_OFFICIAL, lv_.get('fetchedAt'), None, where='the NCM reader on the site', when=('asked %s' % lv_.get('fetchedAt')) if lv_ else 'not asked at the last run',
                                      ref='ncm_live_state.json', note=(lv_.get('error') if lv_ and not lv_ok else None) if lv_ else 'the reader state was not asked at the last run')
    # a warning in force that the list cannot answer for: the hazard is not read, so rain, fog and dust are unknown, not none (independent review, F3, F4, F5)
    hz_open = None
    if lv_ok and lv_.get('state') == 'WARNING':
        hz_open = bool(lv_.get('tickerNotNews')) or lv_.get('tickerRead') is False or any('other' in (x.get('hazards') or []) and (x.get('rak') != 'not touched' or x.get('named')) for x in (lv_.get('items') or []) if isinstance(x, dict))
    elif lv_ok:
        hz_open = False
    F['ncm.hazardNotRead'] = fact(hz_open, 'NCM warning in force whose hazard is not read', '', SRC_OFFICIAL, lv_.get('readAt'), None, where='the NCM reader on the site', when=('read %s' % lv_.get('readAt')) if lv_ok else None, ref='ncm_live_state.json',
                                  note=None if lv_ok else 'the reader state was not fetched at the last run')
    F['ncm.unreadable'] = fact(ncm['unreadable'], 'NCM log line unreadable', '', SRC_OFFICIAL, ref='ncm_log.jsonl', note=None if ncm['unreadable'] is not None else 'no NCM log')
    ref_open = ncm.get('refused')
    rank_ = {'yellow': 1, 'orange': 2, 'red': 3}
    best = None
    for it in ref_open or []:
        lv = str(it.get('level') or '').strip().lower()
        hzs = N.parse_hazards(it.get('hazard'))
        if lv in rank_ and (not hzs or 'rain' in hzs or any(h not in N.HAZARDS for h in hzs)) and (best is None or rank_[lv] > rank_[best[0]]):
            best = (lv, it)
    if ref_open is None:
        F['ncm.refusedRain'] = fact(None, 'typed NCM reading refused by the log', '', SRC_OFFICIAL, note='the file of refused readings could not be read', ref='ncm_refused.jsonl')
    else:
        F['ncm.refusedRain'] = fact(best[0] if best else 'none', 'typed NCM rain reading that the log refused', '', 'Reported by a person, not logged', best[1].get('at') if best else None, None,
                                    where=('typed for %s' % (best[1].get('hazard') or 'no hazard named')) if best else None, when=('typed at %s' % best[1].get('at')) if best else None, ref='ncm_refused.jsonl ' + (best[1].get('id') if best else ''))
    anyref = (bool(ref_open) or bool(ncm.get('refusedUnreadable'))) if ref_open is not None else None
    F['ncm.refusedAny'] = fact(anyref, 'typed NCM reading refused', '', 'Reported by a person, not logged', where='the NCM log and the file of refused readings',
                               when=(ref_open[0].get('at') if ref_open else None), ref='ncm_refused.jsonl', note=None if anyref is not None else 'the file of refused readings could not be read')
    # ---- fog, visibility
    def lvl(x):
        return x if x in ('none', 'watch', 'alert', 'severe') else None

    def hnum(key, side, field='value'):
        return nz(_side(hz, key, side).get(field)) if hz else None

    def place(key, side):
        sd = _side(hz, key, side) if hz else {}
        w = sd.get('point') or sd.get('station')
        return short_text(w, 60) if w else None
    F['fog.level'] = fact(lvl(((hz or {}).get('fog') or {}).get('level')) if hz else None, 'fog level', '', 'Model forecast (Proposed ladder), Reported, Reported, official', at, age,
                          where='the west plain points and the stations', when='02:00 to 08:00 and now', ref='interaction.json hazards.fog', note=None if hz and lvl(((hz or {}).get('fog') or {}).get('level')) else (nr if not hz else 'the engine says the fog level is unknown'))
    vo = _side(hz, 'visibility', 'observed') if hz else {}
    F['vis.level'] = fact(lvl(vo.get('level')) if vo else None, 'observed visibility level', '', SRC_REPORT, at, age, where=vo.get('station'), when='last 3 hours', ref='interaction.json hazards.visibility', note=None if vo else nr)
    F['vis.obs'] = fact(nz(vo.get('value')) if vo else None, 'lowest observed visibility', ' m', SRC_REPORT, at, age, where=vo.get('station'), ref='interaction.json hazards.visibility')
    # ---- wind, gust, dust, pressure, temperature, humidity
    def num_fact(name, label, unit, key, side, srcl, when_key='at', neg=False):
        v = hnum(key, side)
        if neg and v is not None:
            v = -v
        sd = _side(hz, key, side) if hz else {}
        when = _tm(sd.get(when_key)) if (side == 'forecast' and sd.get(when_key)) else ('last 3 hours' if side == 'observed' else None)
        F[name] = fact(v, label, unit, srcl, at, age, where=place(key, side), when=when, ref='interaction.json hazards.%s.%s' % (key, side), note=None if v is not None else (nr if not hz else 'not read at this run'))
    num_fact('wind.fc', 'forecast wind, mean', ' km/h', 'wind', 'forecast', SRC_MODEL)
    num_fact('wind.obs', 'observed wind, mean', ' km/h', 'wind', 'observed', SRC_REPORT)
    num_fact('gust.fc', 'forecast gust', ' km/h', 'gust', 'forecast', SRC_MODEL)
    num_fact('gust.obs', 'observed peak wind', ' km/h', 'gust', 'observed', SRC_REPORT)
    if F['gust.fc']['v'] is None and not hz:                     # a legacy feed carries the best match gust of the next 24 h: one model, used only when there is no engine record
        gs = [(nz(p.get('gustMax24')), p.get('name'), s) for s in cur for p in (s['doc'].get('points') or []) if isinstance(p, dict) and 'error' not in p and nz(p.get('gustMax24')) is not None]
        if gs:
            g = max(gs, key=lambda x: x[0])
            F['gust.fc'] = fact(g[0], 'forecast gust (one model, best match)', ' km/h', SRC_MODEL, g[2]['asof'], g[2]['ageH'], where=short_text((g[1] or '').split(' (')[0], 60), when='the next 24 hours', ref='weather.json points gustMax24')
    dc = _side(hz, 'dust', 'forecast') if hz else {}
    F['dust.fc'] = fact(nz(dc.get('value')) if dc else None, 'CAMS forecast dust', ' ug/m3', 'Model forecast (Proposed, weak)', at, age, where=short_text(dc.get('point') or '', 60) or None, when=_tm(dc.get('at')), ref='interaction.json hazards.dust.forecast', note=None if dc else nr)
    pm = dc.get('pm10') if isinstance(dc.get('pm10'), dict) else {}
    F['dust.pm10'] = fact(nz(pm.get('value')), 'CAMS forecast pm10', ' ug/m3', 'Model forecast (Proposed, weak)', at, age, where=short_text(pm.get('point') or '', 60) or None, when=_tm(pm.get('at')), ref='interaction.json hazards.dust.forecast.pm10')
    do = _side(hz, 'dust', 'observed') if hz else {}
    F['dust.obsLevel'] = fact(lvl(do.get('level')) if do else None, 'observed dust level', '', SRC_REPORT, at, age, where=', '.join(do.get('stationsRead') or []) or None, when='last 3 hours', ref='interaction.json hazards.dust.observed', note=None if do else nr)
    num_fact('press.fcFall', 'forecast 24 h pressure fall', ' hPa', 'pressure', 'forecast', SRC_MODEL, neg=True)
    po = _side(hz, 'pressure', 'observed') if hz else {}
    F['press.obsLevel'] = fact(lvl(po.get('level')) if po else None, 'observed pressure fall level', '', SRC_REPORT, at, age, where=short_text(po.get('text') or '', 90) or None, when='last 24 hours', ref='interaction.json hazards.pressure.observed', note=None if po else nr)
    num_fact('heat.fc', 'forecast highest temperature', ' deg C', 'heat', 'forecast', SRC_MODEL)
    num_fact('heat.obs', 'highest temperature today', ' deg C', 'heat', 'observed', SRC_REPORT)
    num_fact('cold.fc', 'forecast lowest temperature', ' deg C', 'cold', 'forecast', SRC_MODEL)
    num_fact('cold.obs', 'lowest temperature today', ' deg C', 'cold', 'observed', SRC_REPORT)
    hm = (hz or {}).get('humidity') if hz else None
    dry = hm.get('dry') if isinstance(hm, dict) and isinstance(hm.get('dry'), dict) else {}
    for nm, sd, lab, sl in (('dry.fc', dry.get('forecast'), 'forecast lowest relative humidity', SRC_MODEL), ('dry.obs', dry.get('observed'), 'lowest relative humidity, last 3 hours', SRC_REPORT)):
        sd = sd if isinstance(sd, dict) else {}
        F[nm] = fact(nz(sd.get('value')), lab, ' %', sl, at, age, where=short_text(sd.get('point') or sd.get('station') or '', 60) or None, when=_tm(sd.get('at')) if nm == 'dry.fc' else 'last 3 hours',
                     ref='interaction.json hazards.humidity.dry', note=None if sd else nr)
    fp = hm.get('fogProne') if isinstance(hm, dict) and isinstance(hm.get('fogProne'), dict) else {}
    F['fogprone.level'] = fact(lvl(fp.get('level')) if fp else None, 'fog prone evening level', '', 'Model forecast (Proposed) and Reported', at, age, where='the west coast stations (OMRK, OMSJ) and the west plain points',
                               when='20:00', ref='interaction.json hazards.humidity.fogProne', note=None if fp else nr)
    # ---- the health of the feed, the engine and the channel
    ages = [(s['ageH'], s) for s in docs if s['ageH'] is not None]
    fa = min(ages, key=lambda x: x[0]) if ages else None
    F['feed.ageH'] = fact(fa[0] if fa else None, 'age of the freshest weather feed', ' h', 'file asof', fa[1]['asof'] if fa else None, fa[0] if fa else None, where=fa[1]['file'] if fa else None,
                          when=('last read %s' % fa[1]['asof']) if fa else None, ref='weather.json asof', note=None if fa else 'no weather feed file')
    hs = src['health']
    F['engine.ageH'] = fact(hs['ageH'], 'age of the last engine run', ' h', 'health.json lastRun', hs['asof'], hs['ageH'], where='the weather engine', when=('last run %s' % hs['asof']) if hs['asof'] else 'no engine run on record', ref='health.json lastRun.now',
                            note=None if hs['ageH'] is not None else ('health.json is %s' % hs['status'] if hs['status'] != 'ok' else (hs['note'] or 'no engine run on record')))
    q, qs = None, None
    if hs['current'] and isinstance(hs['doc'].get('readingQuality'), str):
        q, qs = hs['doc']['readingQuality'], hs
    else:
        for s in cur:
            hb = s['doc'].get('health')
            if isinstance(hb, dict) and isinstance(hb.get('readingQuality'), str):
                q, qs = hb['readingQuality'], s
                break
    alerts = (qs['doc'].get('alerts') if qs and isinstance(qs['doc'].get('alerts'), list) else None) or ((qs['doc'].get('health') or {}).get('alerts') if qs and isinstance(qs['doc'].get('health'), dict) else None) or []
    F['health.quality'] = fact(q, 'engine reading quality', '', 'engine health record', qs['asof'] if qs else None, qs['ageH'] if qs else None, where=('; '.join(short_text(x, 80) for x in alerts[:2]) or None) if q else None,
                               when=qs['asof'] if qs else None, ref='health.json readingQuality', note=None if q else 'no current health record')
    cs = src['conditions']
    F['conditions.current'] = fact(bool(cs['current']), 'conditions.json current', '', 'file asof', cs['asof'], cs['ageH'], where='conditions.json', when=('as of %s' % cs['asof']) if cs['asof'] else 'no time on record', ref='conditions.json asof',
                                   note=None if cs['current'] else ('the file is %s' % cs['status'] if cs['status'] != 'ok' else (cs['note'] or 'older than 3 hours')))
    # 28 Oct 2026 (F11): an NCM log counts as a current input only while its freshest reading is inside the NCM limit (freshness.ncmReadHours, 24 h); a log whose last reading is 30 hours old is not a current
    # input, so the channel that has nothing else current says so (WC-35) instead of being held quiet by an old line
    ncm_lim = float((rules_doc.get('freshness') or {}).get('ncmReadHours', 24))
    ncm_cur = bool(ncm['entries']) and ncm.get('ageH') is not None and ncm['ageH'] <= ncm_lim
    anycur = any(src[n]['current'] for n in ('weather', 'staging', 'interaction', 'health')) or ncm_cur
    F['channel.noReading'] = fact(not anycur, 'the channel has a current input', '', 'all input files', None, None, where='the weather channel', when=None, ref='all input files',
                                  note='no weather feed, engine record or NCM log is readable and current')
    F['channel.noReading']['label'] = 'no current input of any kind'                 # read as 'no current input of any kind yes' in a notice (NOTICE-TEXT, 29 Oct 2026)
    return F, notes


# ------------------------------------------------------------------------------------------------ the rule language
def _lv(scale, v):
    try:
        return scale.index(v)
    except (ValueError, AttributeError):
        return None


def eval_cond(c, facts, scales=None):
    """(fired, [names of the facts that made it true]). A fact with no reading is never true for a comparison: no reading is not calm and not a trigger, except for the op 'unknown'."""
    if not isinstance(c, dict):
        return False, []
    if 'all' in c:
        names, ok = [], True
        for x in c['all']:
            r, n = eval_cond(x, facts, scales)
            ok = ok and r
            names += n
        return (ok, names if ok else [])
    if 'any' in c:
        names, ok = [], False
        for x in c['any']:
            r, n = eval_cond(x, facts, scales)
            if r:
                ok = True
                names += n
        return ok, names
    if 'not' in c:
        r, _ = eval_cond(c['not'], facts, scales)
        return (not r), []
    name, op, val = c.get('fact'), c.get('op'), c.get('value')
    f = facts.get(name)
    v = f['v'] if isinstance(f, dict) else None
    unknown = v is None or v == 'unknown'
    if op == 'unknown':
        return unknown, [name]
    if op == 'known':
        return (not unknown), [name]
    if unknown:
        return False, []
    try:
        if op == 'nonempty':
            r = isinstance(v, (list, tuple, str)) and len(v) > 0
        elif op == '==':
            r = v == val
        elif op == '!=':
            r = v != val
        elif op == 'in':
            r = v in val
        elif op in ('>=', '>', '<=', '<'):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                r = False
            else:
                r = {'>=': v >= val, '>': v > val, '<=': v <= val, '<': v < val}[op]
        elif op == 'level>=':
            sc = (scales or {}).get(c.get('scale'))
            a, b = _lv(sc, v), _lv(sc, val)
            r = a is not None and b is not None and a >= b
        else:
            r = False
    except TypeError:
        r = False
    return bool(r), [name] if r else []


def evaluate(rules_doc, facts, phase=1):
    """the result of every rule of the phase: {'id', 'rule', 'fired', 'names'}"""
    out = []
    for r in rules_doc['rulesOk']:
        if r.get('kind') == 'rung-change' or int(r.get('phase', 1)) != phase:
            continue
        fired, names = eval_cond(r['when'], facts, rules_doc.get('scales'))
        out.append({'id': r['id'], 'rule': r, 'fired': fired, 'names': sorted(set(names), key=names.index)})
    return out


def evidence_of(res, facts, limit=3):
    items = []
    for n in res['names'][:limit]:
        f = facts.get(n)
        if f:
            items.append({'fact': n, 'text': fmt_fact(f), 'value': f['v'], 'src': f.get('src'), 'at': f.get('at'), 'ageH': f.get('age'), 'ref': f.get('ref')})
    return items


# ------------------------------------------------------------------------------------------------ the ladder
def step_ladder(prev, cand, blind, now, cfg):
    """One step of the readiness ladder. prev: the ladder of the last run ({} on the first), cand: the highest rung the rules support now, blind: some reading is missing or stale.
    Rises at once. Falls only after fallClearReadings consecutive clear hourly readings and fallMinHoursAtRung hours at the rung, one rung at a time. An unknown or stale reading never lowers it.
    Returns (ladder, change) where change is None or {'from', 'to', 'why'}."""
    clear_n = int(cfg.get('fallClearReadings', 3))
    min_h = float(cfg.get('fallMinHoursAtRung', 6))
    hour = floor_hour(now).strftime('%Y-%m-%dT%H')
    lad = {k: prev.get(k) for k in ('rung', 'since', 'clear', 'lastHour', 'windowPeak', 'blindSince')} if prev else {}
    change = None
    if not prev or lad.get('rung') is None:
        lad.update({'rung': cand, 'since': f_local(now), 'clear': 0, 'lastHour': None, 'windowPeak': 0, 'blindSince': f_local(now) if blind else None})
        if cand > 0:
            change = {'from': 0, 'to': cand, 'why': 'the first reading of the channel already supports %s' % STAGES[cand]}
        return lad, change
    rung = lad['rung']
    if cand > rung:
        change = {'from': rung, 'to': cand, 'why': 'rise: the evidence supports %s' % STAGES[cand]}
        lad.update({'rung': cand, 'since': f_local(now), 'clear': 0, 'windowPeak': 0, 'lastHour': None})
    elif blind:
        lad['clear'] = 0                                    # a gap breaks the run of clear readings
        lad['windowPeak'] = 0
    elif cand >= rung:
        lad['clear'] = 0
        lad['windowPeak'] = 0
    else:                                                   # a clear reading: the evidence supports less than the rung
        if lad.get('lastHour') != hour:                     # one count per clock hour: a second run in the same hour changes nothing
            try:
                gap_h = hours_between(parse_local(str(lad['lastHour']).replace('T', ' ') + ':00'), floor_hour(now)) if lad.get('lastHour') else None
            except (ValueError, TypeError):
                gap_h = None
            if gap_h is not None and gap_h > 2:             # hours with no reading at all (the laptop was off) break the run: the readings must be consecutive
                lad['clear'], lad['windowPeak'] = 0, 0
            lad['clear'] = int(lad.get('clear') or 0) + 1
            lad['windowPeak'] = max(int(lad.get('windowPeak') or 0), cand)
            lad['lastHour'] = hour
        try:
            at_rung_h = hours_between(parse_local(lad['since']), now)
        except (ValueError, TypeError):
            at_rung_h = 0
        if lad['clear'] >= clear_n and at_rung_h >= min_h:
            target = max(int(lad.get('windowPeak') or 0), rung - 1) if cfg.get('stepDownOneRung', True) else int(lad.get('windowPeak') or 0)
            change = {'from': rung, 'to': target, 'why': 'fall: %d clear hourly readings and %.1f hours at %s' % (lad['clear'], at_rung_h, STAGES[rung])}
            lad.update({'rung': target, 'since': f_local(now), 'windowPeak': 0})
    if blind:
        lad['blindSince'] = lad.get('blindSince') or f_local(now)
    else:
        lad['blindSince'] = None
    return lad, change


def update_history(state_ladder, now, rung):
    """hourly map of the highest rung of each hour (kept 8 days), the highest in the last 24 hours and 7 days"""
    hist = dict(state_ladder.get('history') or {})
    hour = floor_hour(now).strftime('%Y-%m-%dT%H')
    hist[hour] = max(int(hist.get(hour, 0)), int(rung))
    cut = (now - dt.timedelta(days=8)).strftime('%Y-%m-%dT%H')
    hist = {k: v for k, v in hist.items() if k >= cut}
    def top(days):
        lo = (now - dt.timedelta(days=days)).strftime('%Y-%m-%dT%H')
        rows = [(v, k) for k, v in hist.items() if k > lo]
        if not rows:
            return {'rung': None, 'stage': None, 'at': None}
        v = max(r[0] for r in rows)
        k = min(k for r, k in rows if r == v)
        return {'rung': v, 'stage': STAGES[v], 'at': k.replace('T', ' ') + ':00'}
    return hist, top(1), top(7)


# ------------------------------------------------------------------------------------------------ actions and the pack
def _late(it):
    st = it.get('status')
    if st == 'done':
        return 'no'
    if st == 'late' or it.get('lateByDueDate'):
        return 'yes'
    if st == 'unknown' and it.get('dueDatePassed'):
        return 'unknown'
    return 'no'


def action_pack(rung, now, data_dir, rules_doc):
    """every action A1 to A30 the plan ties to this rung (action_triggers.json), with its owner, due time, status (from BARQ/actions.md, which is only as old as it says) and whether it is late.
    The text of an action is cut to a short title before the first bracket, so no person named in an action travels. Returns {'rung', 'stage', 'rows', 'newAtRung', 'counts', 'notes'}."""
    a = I.read_actions(now)
    as_of = a.get('statusAsOf')
    if as_of and as_of > now.strftime('%Y-%m-%d'):
        # the register is dated after this clock (a replay of March, a drill on a date ahead of the register): what it says cannot be known at that time, so its words are not used and
        # an action is late only by its own due date, as the clock sees it (25 Oct 2026: the March storm replay showed four actions late that did not exist yet)
        for it in a['items']:
            it['status'], it['lateByDueDate'] = 'unknown', False
        a['notes'] = list(a['notes']) + ['The action register is dated %s, after this clock (%s): its statuses cannot be known at this time and are not used (owner: Barq)' % (as_of, f_local(now)[:10])]
    trig = I.load_triggers(data_dir) or []
    by_id = {x['id']: x for x in a['items']}
    groups = RUNG_GROUPS.get(rung, ['standing'])
    rows, order = {}, []
    for t in trig:
        it = by_id.get(t['id'])
        if not it or t['when'] not in groups:
            continue
        if t['id'] not in rows:
            rows[t['id']] = {'id': t['id'], 'title': it['title'], 'owner': it['owner'], 'due': it['due'], 'status': it['status'], 'late': _late(it), 'why': [], 'rule': t['rule']}
            order.append(t['id'])
        rows[t['id']]['why'].append(t['when'])
        if t.get('detailPattern'):
            try:
                mm = re.search(t['detailPattern'], it.get('_text') or '')
            except re.error:
                mm = None
            if mm and mm.groups():
                rows[t['id']]['detail'] = mm.group(1)
    rl = [rows[i] for i in order]
    new = [r['id'] for r in rl if RUNG_NEW_GROUP[rung] in r['why']]
    cnt = {'due': len(rl), 'late': sum(1 for r in rl if r['late'] == 'yes'), 'lateUnknown': sum(1 for r in rl if r['late'] == 'unknown'), 'done': sum(1 for r in rl if r['status'] == 'done'),
           'statusUnknown': sum(1 for r in rl if r['status'] == 'unknown')}
    notes = list(a['notes'])
    if not a['listRead']:
        notes.append('No action list was read: no action can be called due or not due (owner: the plan builder)')
    return {'rung': rung, 'stage': STAGES[rung], 'rows': rl, 'newAtRung': new, 'counts': cnt, 'notes': notes, 'statusAsOf': a.get('statusAsOf'), 'statusAgeDays': a.get('statusAgeDays'), 'items': a['items'], 'listRead': a['listRead']}


def activation_pack(pack, rules_doc, contacts, src, now):
    """READINESS HIGHEST: the full activation pack. All actions of the rung, every action A1 to A30 that is open or late (not tied by the plan, listed for the committee), the audience of every
    role with whether it has a contact, the sector exposure from interaction.json, and what the plan puts in each sector (pumps, crews, tankers), read from the plan files, never retyped."""
    roles = rules_doc.get('roles') or {}
    aud = [{'role': r, 'label': v.get('label'), 'tier': v.get('tier'), 'contact': ('laptop' if v.get('tier') == 'T0' else ('yes' if K.role_has_contact(contacts, r) else 'no')), 'contactOwner': v.get('contactOwner')} for r, v in roles.items()]
    tied = {r['id'] for r in pack['rows']}
    rest = []
    for it in pack.get('items') or []:
        if it['id'] in tied:
            continue
        rest.append({'id': it['id'], 'title': it['title'], 'owner': it['owner'], 'due': it['due'], 'status': it['status'], 'late': _late(it)})
    res = I.plan_resources()
    expo = {}
    ia = src['interaction']['doc'] if src['interaction']['doc'] else None
    ia_cur = src['interaction']['current']
    if ia and isinstance(ia.get('sectors'), list):
        for s in ia['sectors']:
            if isinstance(s, dict) and s.get('sector'):
                expo[str(s['sector']).lstrip('S')] = s
    sectors = []
    for n in sorted(I.SECTOR_NAMES, key=int):
        r = I.sector_resources(n, res)
        e = expo.get(n) or {}
        rain, fog = (e.get('rain') or {}), (e.get('fog') or {})
        sectors.append({'sector': 'S' + n, 'name': I.SECTOR_NAMES[n], 'points': e.get('pointsShown') or e.get('points'), 'stagingPoint': r.get('stagingPoint'),
                        'rainLevel': rain.get('level') if ia_cur else None, 'next24Mm': rain.get('next24Mm') if ia_cur else None, 'max6Mm': rain.get('max6Mm') if ia_cur else None,
                        'fogLevel': fog.get('level') if ia_cur else None,
                        'pumps': r.get('pumps'), 'crewAtRed': r.get('crewAtRed'), 'gangs': r.get('gangs'), 'supervisors': r.get('supervisors'), 'labour': r.get('labour'),
                        'hireTankers': r.get('hireTankers'), 'placed': r.get('placed')})
    totals = None
    try:
        totals = I.plan_totals(res)
    except Exception:
        totals = None
    gaps = [{'what': 'Placement of pumps, tankers and gangs by sector is not on any register: the figures are what the plan allocates, not what stands ready', 'owner': 'Collection Manager with LOD (A20)'}]
    if res.get('missing'):
        gaps += [{'what': m, 'owner': 'the plan builder'} for m in res['missing'][:3]]
    if not ia_cur:
        gaps.append({'what': 'the sector exposure (rain and fog level by sector) is not current: interaction.json is older than 3 hours or missing', 'owner': 'Weather lead'})
    return {'banner': 'READINESS HIGHEST', 'rung': HIGHEST, 'stage': STAGES[HIGHEST], 'actionsOfRung': pack['rows'], 'otherActions': rest, 'audience': aud, 'sectors': sectors,
            'planTotals': {'pumps': (totals or {}).get('pumps'), 'hireTankers': (totals or {}).get('hireTankers'), 'gangsAndLabour': (totals or {}).get('gangsAndLabour')} if totals else None,
            'gaps': gaps, 'basis': 'The actions of the rung are those the plan ties to it in action_triggers.json; the others are listed so that nothing open is hidden. Sector figures are read from the plan files. The committee sets the stage.'}


def committee_stage(now):
    """the stage the committee has set, if a stage file exists (BARQ/stage.json, stage.md or stage.txt); the console keeps its own stage per device, which the channel cannot read"""
    try:
        s = I.read_stage_file(now)
    except Exception:
        s = None
    return s


# ------------------------------------------------------------------------------------------------ transports and toast
def _toast_script(title, body):
    esc = lambda t: str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace("'", "''")
    xml = '<toast><visual><binding template="ToastGeneric"><text>%s</text><text>%s</text></binding></visual></toast>' % (esc(title), esc(body))
    return ("$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
            "$null=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
            "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml('%s');"
            "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($t)" % (xml, TOAST_APP))


def send_toast(title, body, runner=None, timeout=20):
    """a Windows toast on this laptop through PowerShell. Returns {'ok', 'error'}. The text is passed encoded, never on a command line. runner is replaced in tests."""
    runner = runner or subprocess.run
    if os.name != 'nt' and runner is subprocess.run:
        return {'ok': False, 'error': 'not Windows'}
    enc = base64.b64encode(_toast_script(short_text(title, 120), short_text(body, 240)).encode('utf-16-le')).decode('ascii')
    try:
        r = runner(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', enc], capture_output=True, timeout=timeout)
        rc = getattr(r, 'returncode', 0)
        return {'ok': rc == 0, 'error': None if rc == 0 else 'powershell exit %s' % rc}
    except Exception as e:
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:80])}


def send_email(url, recipients, subject, body, timeout=15):
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    sender = (q.get('from') or ['wm004-weather-channel@localhost.invalid'])[0]
    msg = 'From: %s\r\nTo: %s\r\nSubject: %s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n' % (sender, ', '.join(recipients), plain(subject), body)
    port = u.port or (465 if u.scheme == 'smtps' else 25)
    if u.scheme == 'smtps':
        s = smtplib.SMTP_SSL(u.hostname, port, timeout=timeout, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(u.hostname, port, timeout=timeout)
    try:
        if q.get('starttls') and u.scheme != 'smtps':
            s.starttls(context=ssl.create_default_context())
        if u.username:
            s.login(urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ''))
        s.sendmail(sender, list(recipients), msg.encode('utf-8'))
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return len(recipients)


def _post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers={'Content-Type': 'application/json; charset=utf-8', 'X-Requested-With': 'wm004-weather-channel'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if not 200 <= r.status < 300:
            raise OSError('HTTP %s' % r.status)
    return 1


def send_sms(url, numbers, text, timeout=15):
    _post(url, {'to': list(numbers), 'text': text}, timeout)
    return len(numbers)


def send_webhook(url, payload, timeout=15):
    return _post(url, payload, timeout)


TRANSPORTS = {'email': send_email, 'sms': send_sms, 'webhook': send_webhook}
ENV = {'email': 'WCH_SMTP_URL', 'webhook': 'WCH_WEBHOOK_URL', 'sms': 'WCH_SMS_URL'}


def transports_configured(local=None):
    off = (local or {}).get('transports') if isinstance((local or {}).get('transports'), dict) else {}
    return {k: bool(os.environ.get(v)) and off.get(k) is not False for k, v in ENV.items()}


def role_addresses(contacts, roles, kind):
    out, missing = [], []
    for r in roles:
        c = contacts.get(r) if isinstance(contacts, dict) else None
        vals = [x for x in (c.get(kind) if isinstance(c, dict) and isinstance(c.get(kind), list) else []) if isinstance(x, str) and x.strip()]
        if vals:
            out += vals
        elif not K.role_has_contact(contacts, r):
            missing.append(r)
    return out, missing


def dispatch_delivery(data_dir, rec, d, now, rules_doc, local, contacts):
    """send one queued delivery (a tier above T0) through the transports that are set. Nothing is sent in drill mode. A role with no contact is reported as a gap with its owner, never skipped
    silently. The delivery becomes sent when at least one transport took it; otherwise it stays queued (the record is in the outbox for the desk owner to forward by hand). Only counts and role ids are written."""
    roles = tier_audience(rules_doc, rec, d.get('tier'))
    roles_all = K.tier_roles(rules_doc, d.get('tier')) or roles
    gaps = []
    for r in sorted(set(roles + roles_all)):
        if not K.role_has_contact(contacts, r):
            gaps.append({'role': r, 'owner': ((rules_doc.get('roles') or {}).get(r) or {}).get('contactOwner') or 'the Director\'s office'})
    d['gaps'] = gaps
    rec['gaps'] = gaps
    if rec.get('drill'):
        d['note'] = 'DRILL: nothing is sent beyond the desk owner and the outbox'
        d['status'] = 'draft' if d.get('status') == 'queued' else d.get('status')
        return d
    cfg = transports_configured(local)
    d['results'] = {}
    text = '%s\n%s' % (rec.get('subject'), rec.get('body'))
    for kind, fn in TRANSPORTS.items():
        if not cfg.get(kind):
            continue
        url = os.environ.get(ENV[kind])
        try:
            if kind == 'webhook':
                n = fn(url, {'id': rec['id'], 'rule': rec.get('rule'), 'tier': d.get('tier'), 'rung': rec.get('rung'), 'audience': roles, 'subject': rec.get('subject'), 'body': rec.get('body'), 'drill': False})
                d['results'][kind] = {'ok': n, 'fail': 0}
                continue
            addr, _ = role_addresses(contacts, roles_all, 'email' if kind == 'email' else 'sms')
            if not addr:
                d['results'][kind] = {'ok': 0, 'fail': 0, 'note': 'no address for the roles of this tier'}
                continue
            n = fn(url, addr, rec.get('subject'), rec.get('body')) if kind == 'email' else fn(url, addr, text)
            d['results'][kind] = {'ok': n, 'fail': 0}
        except Exception as e:
            d['results'][kind] = {'ok': 0, 'fail': 1, 'error': '%s' % type(e).__name__}
    sent_via = sorted(k for k, v in d['results'].items() if v.get('ok'))
    if sent_via:
        d['status'], d['at'], d['via'] = 'sent', f_local(now), sent_via
        rec['status'] = 'sent'
    else:
        d['status'] = 'queued'
        d['note'] = 'no transport took it' if d['results'] or any(cfg.values()) else 'no transport is configured: the record is in the outbox for the desk owner to forward'
    return d


def tier_audience(rules_doc, rec, tier):
    """the roles of the record that belong to this tier"""
    return [r for r in (rec.get('audience') or []) if ((rules_doc.get('roles') or {}).get(r) or {}).get('tier') == tier] or K.tier_roles(rules_doc, tier)


# ------------------------------------------------------------------------------------------------ notifications
def _first_where_when(res, facts, default_where, default_when):
    """where: the places of the facts that made the rule true (up to three, each once); when: the first of their times"""
    wheres, when = [], None
    for n in res['names']:
        f = facts.get(n) or {}
        if f.get('where') and f['where'] not in wheres:
            wheres.append(f['where'])
        when = when or f.get('when') or (('read ' + f['at']) if f.get('at') else None)
    return ('; '.join(wheres[:3]) or default_where), (when or default_when)


def render(rule, ctx, drill):
    m = rule['message']
    lines = [fill(x, ctx) for x in m['lines']][:6]
    subject = fill(m['subject'], ctx)
    if drill:
        subject = 'DRILL: ' + subject
        lines[0] = 'DRILL, an exercise. ' + lines[0]
    return subject, [short_text(x, 230) for x in lines]


def new_record(data_dir, now, rule, rung, tier, subject, lines, evidence, ctx, drill, local, contacts, rules_doc, run_cfg):
    aud = list(rule.get('audience') or [])
    if tier != 'T0':
        for r in K.tier_roles(rules_doc, tier):
            if r not in aud:
                aud.append(r)
    am = K.ack_minutes_for(rules_doc, rule['id'], tier)
    if rule['id'] == 'WC-00':
        am = K.DEFAULT_ACK_MIN.get(tier) if (rung or 0) >= 1 and run_cfg.get('rose') else None          # a rise is tracked until somebody acknowledges it; a fall is information and has no clock
    due = f_local(now + dt.timedelta(minutes=am)) if (am and tier != 'T0') else None
    cfg = transports_configured(local)
    chans = ['toast', 'outbox', 'console'] + ([k for k, v in cfg.items() if v] if tier != 'T0' and not drill else [])
    fields = {'schema': 1, 'rule': rule['id'], 'title': rule.get('title'), 'hazard': rule.get('hazard'), 'rung': rung, 'stage': STAGES[rung] if rung is not None else None, 'tier': tier, 'baseTier': tier,
              'audience': aud, 'channels': chans, 'subject': subject, 'body': '\n'.join(lines), 'bodyLines': lines, 'drill': bool(drill), 'created': f_local(now), 'updated': f_local(now),
              'status': 'draft' if tier != 'T0' else 'sent', 'deliveries': [], 'ack': None, 'ackDueAt': due, 'escalations': [], 'repeat': 0, 'lastTrigger': f_local(now), 'cleared': None,
              'expireAfterClearH': max(K.DEFAULT_EXPIRE_AFTER_CLEAR_H, round((rule.get('cooldownMin') or 0) / 60.0)), 'cooldownMin': rule.get('cooldownMin'),
              'evidence': evidence, 'actions': list(rule.get('actions') or []), 'first': plain(rule.get('first') or ''), 'where': ctx.get('where'), 'when': ctx.get('when'), 'gaps': []}
    rec = K.create_record(data_dir, now, fields)
    return rec


def deliver_t0(data_dir, rec, now, run_cfg, state_health):
    """T0 is automatic and needs no approval: the toast on this laptop, the outbox file (already written) and the entry in the console (channel.json lists it)."""
    d = {'tier': 'T0', 'status': 'sent', 'at': f_local(now), 'via': ['outbox', 'console'], 'toast': 'pending' if run_cfg.get('toast') else 'skipped'}
    rec['deliveries'].insert(0, d)
    return d


def toast_new(data_dir, created, now, run_cfg, health, extra=None):
    """one toast for all the records this run created (the highest tier first), never one per record: a run that raises five triggers is one interruption. The notes of escalation (a record that was not
    acknowledged and rose a tier) go into the same toast, and a run that only escalated sends one toast for all of them: the desk owner is interrupted once a run at most. The result is written into
    each record created."""
    extra = [x for x in (extra or []) if x]
    if (not created and not extra) or not run_cfg.get('toast'):
        return None
    order = sorted(created, key=lambda r: (-(int(r['tier'][1]) if r.get('tier', 'T0')[1:].isdigit() else 0), r['id']))
    if order:
        top = order[0]
        title = ('DRILL: ' if top.get('drill') else '') + ('Weather channel: %d new' % len(order) if len(order) > 1 else top['subject'])
        body = '; '.join(short_text(r['subject'], 70) for r in order[:3]) + ('; and %d more' % (len(order) - 3) if len(order) > 3 else '') if len(order) > 1 else (top['bodyLines'][1] if len(top['bodyLines']) > 1 else top['subject'])
    else:
        title, body = 'Weather channel', ''
    if extra:
        tail = extra[0] if len(extra) == 1 else '%d notifications were not acknowledged and rose a tier or reached the top tier; the first: %s' % (len(extra), extra[0])
        body = (body + '; ' if body else '') + tail
    r_ = send_toast(title, body, run_cfg.get('toast_runner'))
    health['lastToast'] = {'at': f_local(now), 'ok': r_['ok'], 'error': r_.get('error')}
    for rec in order:
        with K.locked_record(data_dir, rec['id']):
            cur = K.load_record(data_dir, rec['id'])
            if cur:
                for d in cur.get('deliveries') or []:
                    if d.get('tier') == 'T0' and d.get('toast') == 'pending':
                        d['toast'] = 'ok' if r_['ok'] else 'failed'
                        if r_['ok'] and 'toast' not in d['via']:
                            d['via'] = ['toast'] + d['via']
                K.save_record(data_dir, cur)
    return r_


# ------------------------------------------------------------------------------------------------ the run
def default_toast(P, now_given, drill=False):
    """a toast is only shown on the real data folder, for a real clock; a scratch folder, a test or a scenario never shows one"""
    return bool(P['real']) and not now_given and os.environ.get('WCH_NO_TOAST') != '1'


def _count_open(data_dir):
    recs = K.load_records(data_dir)
    open_ = [r for r in recs if r.get('status') not in K.CLOSED]
    return recs, open_


def run_channel(P, now, drill=False, dry=False, toast=None, toast_runner=None, rules_path=None, local_path=None, contacts_path=None, lock_timeout=90):
    """Run the channel once. Returns the result dict (state, facts, fired rules, events). Never raises for a bad input: what cannot be read is no reading and the channel says so."""
    data_dir = P['data']
    t_start = time.time()
    if not dry:
        os.makedirs(data_dir, exist_ok=True)
    if dry:
        return _run_inner(P, now, drill, True, toast, toast_runner, rules_path, local_path, contacts_path, t_start)
    with U.file_lock(K.state_path(data_dir), timeout=lock_timeout):
        return _run_inner(P, now, drill, False, toast, toast_runner, rules_path, local_path, contacts_path, t_start)


COPY_NAME = 'channel_state.copy.json'


def _ran_before(data_dir):
    """True when the folder holds anything the channel makes: a log with a line, a record in the outbox"""
    try:
        if os.path.getsize(K.log_path(data_dir)) > 0:
            return True
    except OSError:
        pass
    try:
        return any(e.name.startswith('NOTIF-') for e in os.scandir(K.outbox_dir(data_dir)))
    except OSError:
        return False


def load_prev(P, now, open_records, dry=False):
    """(the state of the last run, a note or None). A state file that is missing or does not read is not the first run when the channel has run before (25 Oct 2026, the stress test: a damaged state
    file made the ladder start again at the rung the evidence of that hour supports, so a storm held at Active response fell to Routine readiness in one run, and every rule that was on was forgotten, so
    each made its notification again). The ladder and the rules come back from, in this order: channel_state.copy.json (a copy of the state that every run writes after the state itself), the last readable line of the
    channel log (its snapshot holds the rung) and channel.json (the compact copy). The clear readings restart from zero, so the recovered ladder can only fall later than the lost one would have, never
    sooner. The rules that were on come back from the open records that are not cleared. The damaged file is kept aside (the engine's convention: <name>.corrupt-<stamp>). The note goes into the
    health of the channel, which says degraded."""
    data_dir = P['data']
    path = K.state_path(data_dir)
    doc, st = read_json_status(path)
    if st == 'ok' and isinstance(doc, dict):
        return doc, None
    if st == 'missing' and not _ran_before(data_dir):
        return {}, None                                      # the first run of the channel on this folder
    kept = None
    if st == 'ok':
        st = 'damaged'                                       # valid JSON that is not an object is not a state
    if st in ('damaged', 'unreadable') and not dry and os.path.exists(path):
        try:
            same = None                                      # a state that stays damaged (the folder cannot be written) is kept aside once, not at every run
            raw = open(path, 'rb').read() if st == 'damaged' else None
            for n in sorted(x for x in os.listdir(data_dir) if x.startswith('channel_state.json.corrupt-')):
                if raw is not None and open(os.path.join(data_dir, n), 'rb').read() == raw:
                    same = n
            kept = same or os.path.basename(U.quarantine_copy(path))
        except OSError:
            kept = None
    prev, src = {}, None
    d2, s2 = read_json_status(os.path.join(data_dir, COPY_NAME))
    if s2 == 'ok' and isinstance(d2, dict) and isinstance(d2.get('ladder'), dict) and d2['ladder'].get('rung') in (0, 1, 2, 3):
        prev, src = d2, 'the copy of the state (%s)' % COPY_NAME
    # what the append only log and the compact copy say, whichever is newer than the state recovered
    cand = []
    try:
        rows, _, _ = K.tail_log(data_dir, 80)
        for x in reversed(rows):
            sn = x.get('snap') if isinstance(x.get('snap'), dict) else None
            if sn and sn.get('rung') in (0, 1, 2, 3) and x.get('t'):
                cand.append((sn['rung'], x['t'], 'the last line of the channel log'))
                break
    except Exception:
        pass
    cj = read_json(P.get('channel_out') or '', None) if P.get('channel_out') else None
    if isinstance(cj, dict) and cj.get('rung') in (0, 1, 2, 3) and cj.get('asof'):
        cand.append((cj['rung'], cj['asof'], 'channel.json'))
    base_rung = prev['ladder']['rung'] if prev else None
    base_at = prev.get('asof') if prev else None
    for rung, t_, name in cand:
        if base_rung is None or (rung > base_rung and (base_at is None or t_ >= base_at)):
            base_rung, src = rung, name if base_rung is None else '%s and %s' % (src, name)
            lad = dict(prev.get('ladder') or {})
            lad.update({'rung': rung, 'since': t_, 'clear': 0, 'lastHour': None, 'windowPeak': 0})
            prev = dict(prev, ladder=lad)
    if not prev:
        prev = {}
    # the rules that were on: every open record that is not cleared is a rule that is on, with its record
    rules = dict(prev.get('rules') or {})
    for r in open_records:
        rid = r.get('rule')
        if rid and rid != 'WC-00' and r.get('status') not in K.CLOSED and not r.get('cleared') and not r.get('drill'):
            sr = rules.get(rid) or {}
            if not sr.get('on') or sr.get('record') != r['id']:
                rules[rid] = dict(sr, on=True, record=r['id'], since=r.get('created'), lastSeen=r.get('lastTrigger') or r.get('created'), offSince=None)
    if rules:
        prev = dict(prev, rules=rules)
    note = 'channel_state.json was %s%s: %s. The clear readings start again from zero, so the rung can only fall later than it would have, never sooner%s' % (
        'missing' if st == 'missing' else ('damaged' if st == 'damaged' else 'unreadable'), (' and is kept aside as %s' % kept) if kept else '',
        ('the ladder and the rules were recovered from %s' % src) if src else 'nothing to recover the ladder from was found, so it starts from the evidence of this run (owner: the weather desk)', '; the season record of the channel was recovered with it' if prev.get('season') else '; the channel season record restarts')
    return prev, note


def reconcile_rules(prev_rules, summaries, now, rules_doc, drill):
    """(the rules state with the latest record of each rule inside its cooldown, the rules that were corrected). The cooldown of a rule is kept by the state (the record of the rule), and the state is the last
    file a run writes after its records (25 Oct 2026, stress test T5: a run stopped between the two made the record and not the state, so the next run found no record for the rule and made a second one inside the
    cooldown, 3 times in 5,000 runs with faults). The outbox is the record of what was made, so a record of a rule made inside the cooldown that the state does not hold, or holds an older one, is taken from the outbox.
    Only records inside their cooldown matter and only they are written into the state. The records of the other mode (drill or live) are not looked at."""
    cd = {r['id']: float(r.get('cooldownMin') or 0) for r in (rules_doc.get('rulesOk') or []) if r.get('id')}
    by_id = {x['id']: x for x in summaries}
    latest = {}
    for x in summaries:
        rid = x.get('rule')
        if not rid or rid == 'WC-00' or rid not in cd or bool(x.get('drill')) != bool(drill) or not x.get('created'):
            continue
        k = (x['created'], x['id'])
        if rid not in latest or k > latest[rid]:
            latest[rid] = k
    out, fixed = dict(prev_rules), []
    for rid, (created, nid) in latest.items():
        try:
            inside = hours_between(parse_local(created), now) * 60.0 < cd[rid]
        except (ValueError, TypeError):
            continue
        if not inside:
            continue
        sr = dict(out.get(rid) or {})
        cur = sr.get('record')
        if cur == nid:
            continue
        cur_key = None
        if cur:
            cs = by_id.get(cur)
            if cs and cs.get('created'):
                cur_key = (cs['created'], cur)
            else:
                t_ = K.id_time(cur)                           # a record that does not read still says when it was made in its name
                cur_key = (f_local(t_), cur) if t_ else None
        if cur_key is not None and cur_key >= (created, nid):
            continue
        sr['record'] = nid
        out[rid] = sr
        fixed.append(rid)
    return out, fixed


def _run_inner(P, now, drill_arg, dry, toast, toast_runner, rules_path, local_path, contacts_path, t_start):
    data_dir = P['data']
    rules_doc, rproblems = K.load_rules(data_dir, rules_path)
    local = K.load_local(local_path)
    contacts = K.load_contacts(contacts_path)
    events, health_alerts = [], []
    # one read of the outbox for the whole run (25 Oct 2026, stress test): the open records, in memory, changed as the run changes them. Every count below comes from here, not from another pass over the files.
    scan0 = K.scan_outbox(data_dir, write_index=not dry)
    live = {r['id']: r for r in scan0['open']}
    prev, recovered = load_prev(P, now, scan0['open'], dry)
    prev = prev if isinstance(prev, dict) else {}
    if recovered:
        health_alerts.append(recovered)
    if rules_doc is None:
        # no rules: the channel is blind and says so; it writes nothing but its state
        st = dict(prev) if prev else {}
        st.update({'schema': 1, 'asof': f_local(now), 'tz': U.TZ_NAME, 'blind': True, 'rulesProblems': rproblems,
                   'health': dict((prev.get('health') or {}), status='blind', rulesLoaded=0, problems=rproblems, lastRun={'at': f_local(now), 'ok': False, 'error': rproblems[0] if rproblems else 'no rules'})})
        if not dry:
            atomic_write_json(K.state_path(data_dir), st, indent=1)
        return {'ok': False, 'state': st, 'fired': [], 'events': [], 'problems': rproblems, 'code': 2}
    drill = bool(drill_arg or rules_doc.get('drill') or local.get('drill'))
    cfg_h = rules_doc.get('hysteresis') or {}
    fresh = rules_doc.get('freshness') or {}
    current_h = float(fresh.get('currentHours', CURRENT_H))
    src = read_sources(P, now, current_h)
    ncm = read_ncm(P, now, src['weather'] if src['weather']['doc'] else src['staging'])
    facts, fnotes = build_facts(src, ncm, P, now, rules_doc, prev)
    snap_state = dict(prev)

    def log(kind, **kw):
        if dry:
            events.append({'kind': kind, **kw})
            return
        open_ = [x for x in live.values() if x.get('status') not in K.CLOSED]
        snap_state['notifications'] = {'open': len(open_), 'unacknowledged': len([x for x in open_ if x.get('baseTier') != 'T0'])}
        K.append_log(data_dir, kind, now, snap=K.snapshot_of(snap_state), drill=drill, **kw)
        events.append({'kind': kind, **kw})

    # ---- drill mode start and end
    prev_drill = bool(prev.get('drill')) if prev else False
    prev_rules = dict(prev.get('rules') or {})
    if prev and prev_drill != drill:
        log('drill_start' if drill else 'drill_end', note='drill mode %s' % ('on' if drill else 'off'))
        prev_rules = {}                                     # the rules of the other mode do not carry over: each mode has its own records
        if not dry:
            for rec in list(live.values()):
                if bool(rec.get('drill')) == prev_drill and rec.get('status') not in K.CLOSED:
                    why_ = None
                    with K.locked_record(data_dir, rec['id']):
                        r2 = K.load_record(data_dir, rec['id'])
                        if r2 and r2.get('status') not in K.CLOSED:
                            r2['status'], r2['expiredReason'], r2['ackDueAt'], r2['updated'] = 'expired', 'drill mode ended' if prev_drill else 'drill mode started', None, f_local(now)
                            K.save_record(data_dir, r2)
                            live.pop(r2['id'], None)
                            why_ = r2['expiredReason']
                    if why_:
                        log('notification_expired', id=rec['id'], rule=rec.get('rule'), reason=why_)       # every record says in the log how it ended (stress T8: the records of a drill were closed without a line each)
    elif not prev and drill:
        log('drill_start', note='drill mode on from the first run')
    prev_rules, taken_from_outbox = reconcile_rules(prev_rules, scan0['summaries'], now, rules_doc, drill)
    if taken_from_outbox:
        health_alerts.append('the state did not hold the latest notification of %s (a run stopped before it wrote its state); it was taken from the outbox, so the cooldown holds (owner: the weather desk)' % ', '.join(sorted(taken_from_outbox)[:6]))
    # ---- the rules: phase 1
    res1 = evaluate(rules_doc, facts, 1)
    blind_rules = [r for r in res1 if r['fired'] and r['rule'].get('blind')]
    cand = max([r['rule']['rung'] for r in res1 if r['fired'] and not r['rule'].get('blind')] or [0])
    prev_lad = prev.get('ladder') or {}
    blind_since = prev_lad.get('blindSince')
    try:
        blind_h = hours_between(parse_local(blind_since), now) if (blind_since and blind_rules) else 0.0
    except (ValueError, TypeError):
        blind_h = 0.0
    facts['blind.any'] = fact(bool(blind_rules), 'channel blind (a reading missing or stale)', '', 'channel rules', None, None, where='the weather channel', when=blind_since and ('since ' + blind_since), ref='blind rules')
    facts['blind.hours'] = fact(round(blind_h, 1), 'hours the channel has held the rung without a clear reading', ' h', 'channel state', None, None, where='the weather channel', when=blind_since and ('since ' + blind_since), ref='channel_state.json ladder.blindSince')
    res2 = evaluate(rules_doc, facts, 2)
    results = res1 + res2
    blind = any(r['fired'] and r['rule'].get('blind') for r in results)
    lad, change = step_ladder(prev_lad, cand, blind, now, cfg_h)
    rung = lad['rung']
    hist, top24, top7 = update_history(prev_lad, now, rung)
    lad['history'] = hist
    pack = action_pack(rung, now, data_dir, rules_doc)
    rules_state = {}
    snap_state.update({'ladder': lad, 'rules': rules_state, 'pack': {'counts': pack['counts']}})      # the readiness snapshot on every line of the log is the state after this step
    # ---- trigger on and off, notifications
    holds = []
    recs_created = []
    health = dict(prev.get('health') or {})
    run_cfg = {'toast': (default_toast(P, False) if toast is None else bool(toast)), 'toast_runner': toast_runner}
    cfg_tr = transports_configured(local)
    for r in results:
        rule = r['rule']
        rid = rule['id']
        sr = dict(prev_rules.get(rid) or {})
        fired = r['fired']
        if fired and not rule.get('blind') and rule['rung'] >= 1:
            holds.append(rid)
        ev = evidence_of(r, facts)
        r['evidence'] = ev
        if fired:
            where, when = _first_where_when(r, facts, 'Ras Al Khaimah', 'now, as of %s' % f_local(now))
            ctx = {'where': where, 'when': when, 'evidence': '; '.join(e['text'] for e in ev) or 'no reading', 'first': plain(rule.get('first') or ''), 'rung': rule['rung'], 'stage': STAGES[rule['rung']],
                   'quality': (facts.get('health.quality') or {}).get('v') or ''}
            if not sr.get('on'):
                log('trigger_on', rule=rid, title=rule.get('title'), rung=rule['rung'], refs=[e['ref'] for e in ev if e.get('ref')], evidence=[e['text'] for e in ev][:3])
                old = K.load_record(data_dir, sr['record']) if (sr.get('record') and not dry) else None
                within = False
                if old and bool(old.get('drill')) == drill:
                    try:
                        within = hours_between(parse_local(old['created']), now) * 60.0 < float(rule.get('cooldownMin') or 0)
                    except (ValueError, TypeError, KeyError):
                        within = False
                unread_hold = False
                if old is None and sr.get('record') and not dry and os.path.exists(K.record_path(data_dir, sr['record'])):
                    t_id = K.id_time(sr['record'])             # the record of this rule is there and does not read: its name says when it was made, and the cooldown still holds (stress test T5)
                    try:
                        unread_hold = bool(t_id) and hours_between(t_id, now) * 60.0 < float(rule.get('cooldownMin') or 0)
                    except (ValueError, TypeError):
                        unread_hold = False
                if unread_hold:
                    log('notification_updated', id=sr['record'], rule=rid, unreadable=True, why='the trigger came back inside the cooldown of %s minutes; the record cannot be read, so it is not changed and no second notification is made (the health counts it as unreadable)' % rule.get('cooldownMin'))
                elif old and within:
                    # a repeated trigger inside the cooldown updates the record and does not send again
                    if not dry:
                        with K.locked_record(data_dir, old['id']):
                            o2 = K.load_record(data_dir, old['id'])
                            if o2:
                                o2['repeat'] = int(o2.get('repeat') or 0) + 1
                                o2['lastTrigger'], o2['updated'], o2['cleared'] = f_local(now), f_local(now), None
                                o2['evidence'] = ev
                                K.save_record(data_dir, o2)
                                live[o2['id']] = o2
                    log('notification_updated', id=old['id'], rule=rid, why='the trigger came back inside the cooldown of %s minutes' % rule.get('cooldownMin'))
                    sr['record'] = old['id']
                else:
                    subject, lines = render(rule, ctx, drill)
                    if dry:
                        events.append({'kind': 'would_create', 'rule': rid, 'subject': subject})
                    else:
                        rec = new_record(data_dir, now, rule, rule['rung'], rule['tier'], subject, lines, ev, ctx, drill, local, contacts, rules_doc, run_cfg)
                        d0 = deliver_t0(data_dir, rec, now, run_cfg, health)
                        if rule['tier'] != 'T0':
                            dd = {'tier': rule['tier'], 'status': 'draft', 'at': f_local(now), 'via': []}
                            rec['deliveries'].append(dd)
                            if not drill and K.auto_send(local, rule['tier']):
                                dd['status'] = 'queued'
                                dispatch_delivery(data_dir, rec, dd, now, rules_doc, local, contacts)
                            else:
                                rec['gaps'] = [{'role': x, 'owner': ((rules_doc.get('roles') or {}).get(x) or {}).get('contactOwner') or 'the Director\'s office'} for x in rec['audience'] if not K.role_has_contact(contacts, x) and x != 'DESK-OWNER']
                        K.save_record(data_dir, rec)
                        live[rec['id']] = rec
                        recs_created.append(rec)
                        sr['record'] = rec['id']
                        log('notification_created', id=rec['id'], rule=rid, tier=rec['tier'], rung=rec['rung'], status=rec['status'])
                        if rec['status'] == 'sent':
                            log('notification_sent', id=rec['id'], rule=rid, tier=rec['tier'], via=[v for dl in rec['deliveries'] for v in (dl.get('via') or [])])
                        else:
                            log('notification_sent', id=rec['id'], rule=rid, tier='T0', via=d0.get('via'))
                sr['on'], sr['since'], sr['offSince'] = True, f_local(now), None
            else:
                sr['on'] = True
                if sr.get('record') and not dry:
                    old = K.load_record(data_dir, sr['record'])
                    if old and old.get('status') not in K.CLOSED:
                        with K.locked_record(data_dir, old['id']):
                            o2 = K.load_record(data_dir, old['id'])
                            if o2 and o2.get('status') not in K.CLOSED:
                                o2['lastTrigger'], o2['evidence'], o2['updated'] = f_local(now), ev, f_local(now)
                                K.save_record(data_dir, o2)
                                live[o2['id']] = o2
            sr['lastSeen'] = f_local(now)
        else:
            if sr.get('on'):
                log('trigger_off', rule=rid, title=rule.get('title'))
                sr['on'], sr['offSince'] = False, f_local(now)
                if sr.get('record') and not dry:
                    with K.locked_record(data_dir, sr['record']):
                        o2 = K.load_record(data_dir, sr['record'])
                        if o2 and o2.get('status') not in K.CLOSED:
                            o2['cleared'], o2['updated'] = f_local(now), f_local(now)
                            K.save_record(data_dir, o2)
                            live[o2['id']] = o2
        if sr:
            rules_state[rid] = sr
    # ---- the rung change notification (always) and the log of the rung
    if change:
        rule0 = next((x for x in rules_doc['rulesOk'] if x.get('kind') == 'rung-change'), None)
        up = change['to'] > change['from']
        tier = TIER_OF_RUNG.get(change['to'], 'T1') if up else 'T1'
        holders = [x for x in results if x['fired'] and not x['rule'].get('blind') and x['rule']['rung'] >= 1]
        evtxt = '; '.join('%s %s' % (x['id'], x['rule'].get('title')) for x in holders[:3]) or change['why']
        ctx = {'where': 'Ras Al Khaimah, the eight rain points of the plan', 'when': 'now, as of %s' % f_local(now), 'evidence': evtxt, 'first': plain((rule0 or {}).get('first') or ''),
               'direction': 'rose' if up else 'fell', 'stage': STAGES[change['to']], 'from_stage': STAGES[change['from']]}
        log('rung_change', **{'from': change['from'], 'to': change['to'], 'fromStage': STAGES[change['from']], 'toStage': STAGES[change['to']], 'why': change['why'], 'rules': [x['id'] for x in holders]})
        if rule0 and not dry:
            for old in list(live.values()):                      # an earlier rung change record is out of date the moment the rung changes again
                if old.get('rule') == 'WC-00' and bool(old.get('drill')) == drill and old.get('status') not in K.CLOSED:
                    with K.locked_record(data_dir, old['id']):
                        o2 = K.load_record(data_dir, old['id'])
                        if o2 and o2.get('status') not in K.CLOSED:
                            o2['status'], o2['expiredReason'], o2['ackDueAt'], o2['updated'] = 'expired', 'superseded by the rung change at %s' % f_local(now), None, f_local(now)
                            K.save_record(data_dir, o2)
                            live.pop(o2['id'], None)
                    log('notification_expired', id=old['id'], rule='WC-00', reason='superseded by the rung change at %s' % f_local(now))
            run_cfg['rose'] = up
            subject, lines = render(rule0, ctx, drill)
            if change['to'] == HIGHEST:
                lines[5] = 'READINESS HIGHEST. The committee sets the stage; the full activation pack is in the channel record.'
                subject = ('DRILL: ' if drill else '') + 'READINESS HIGHEST: Active response recommended'
            rec = new_record(data_dir, now, rule0, change['to'], tier, subject, lines, [{'fact': 'ladder', 'text': change['why'], 'value': change['to'], 'src': 'channel ladder', 'at': f_local(now), 'ageH': 0.0, 'ref': 'channel_state.json ladder'}],
                             ctx, drill, local, contacts, rules_doc, run_cfg)
            d0 = deliver_t0(data_dir, rec, now, run_cfg, health)
            if tier != 'T0':
                dd = {'tier': tier, 'status': 'draft', 'at': f_local(now), 'via': []}
                rec['deliveries'].append(dd)
                if not drill and K.auto_send(local, tier):
                    dd['status'] = 'queued'
                    dispatch_delivery(data_dir, rec, dd, now, rules_doc, local, contacts)
                else:
                    rec['gaps'] = [{'role': x, 'owner': ((rules_doc.get('roles') or {}).get(x) or {}).get('contactOwner') or 'the Director\'s office'} for x in rec['audience'] if not K.role_has_contact(contacts, x) and x != 'DESK-OWNER']
            K.save_record(data_dir, rec)
            live[rec['id']] = rec
            recs_created.append(rec)
            log('notification_created', id=rec['id'], rule='WC-00', tier=rec['tier'], rung=rec['rung'], status=rec['status'])
            log('notification_sent', id=rec['id'], rule='WC-00', tier='T0', via=d0.get('via'))
    # ---- blind: log it when it starts and ends
    was_blind = bool(prev_lad.get('blindSince'))
    if blind and not was_blind:
        log('blind_on', rules=[r['id'] for r in blind_rules], rung=rung, heldRung=rung)
    elif was_blind and not blind:
        log('blind_off', rung=rung)
    # ---- escalation and expiry (a failed step is named, never fatal)
    esc_notes = []                                          # what the desk owner is told about escalation: collected, and sent in the one toast of the run (a day of 100 unacknowledged records was 200 toasts)

    def owner_note(text):
        esc_notes.append(text)
    if not dry:
        try:
            escal = K.escalate(data_dir, now, rules_doc, local, contacts, notify=owner_note, records=list(live.values()))
            expired = K.expire_old(data_dir, now, rules_doc, records=list(live.values()))
            for nid in {e['id'] for e in escal} | set(expired):          # the records the two steps changed are read again; the others stand as they were
                r_ = K.load_record(data_dir, nid)
                if r_ and r_.get('status') not in K.CLOSED:
                    live[nid] = r_
                else:
                    live.pop(nid, None)
        except Exception as e:
            escal, expired = [], []
            health_alerts.append('escalation could not be processed (%s: %s)' % (type(e).__name__, str(e)[:80]))
    else:
        escal, expired = [], []
    if (recs_created or esc_notes) and not dry:
        toast_new(data_dir, recs_created, now, run_cfg, health, extra=esc_notes)
    # ---- the pack
    activation = activation_pack(pack, rules_doc, contacts, src, now) if rung == HIGHEST else None
    comm = committee_stage(now)
    cstage = comm.get('stage') if isinstance(comm, dict) else None
    cur_comm = bool(comm and cstage in STAGES and not comm.get('stale'))
    differs = (STAGES.index(cstage) != rung) if cur_comm else None
    flag = None
    if cur_comm and differs:
        flag = 'The committee stage is %s and the channel recommends %s. The committee decides; the channel does not change a stage.' % (cstage, STAGES[rung])
    elif comm and comm.get('stale'):
        flag = 'The recorded committee stage (%s, %s) is %s hours old and is not compared.' % (cstage, comm.get('asof'), comm.get('ageHours'))
    elif not comm:
        flag = 'No committee stage is on file (the console keeps its stage per device): the channel cannot say whether the stage and the recommendation differ.'
    # ---- the state
    scan1 = scan0 if dry else K.scan_outbox(data_dir)       # the run's changes are in the files now: a second look, which opens only the files that changed
    recs, open_ = scan1['summaries'], scan1['open']
    notif = {'open': len(open_), 'unacknowledged': len([x for x in open_ if x.get('baseTier') != 'T0']), 'drafts': len([x for x in open_ if x.get('status') == 'draft']),
             'byTier': {t: len([x for x in recs if x.get('baseTier') == t]) for t in K.TIERS}, 'timers': K.timers(data_dir, now, open_)[:20]}
    ack_mins = [x['ackMinutes'] for x in recs if isinstance(x.get('ackMinutes'), (int, float)) and not x.get('drill')]
    season = build_season(prev, now, hist, recs, ack_mins)
    h = self_test(P, now, rules_doc, rproblems, local, contacts, src, ncm, recs, open_, health, scan1['unreadable'], t_start, health_alerts)
    why = []
    for x in results:
        if x['fired'] and not x['rule'].get('blind') and x['rule']['rung'] >= 1:
            why.append({'rule': x['id'], 'title': x['rule'].get('title'), 'rung': x['rule']['rung'], 'status': x['rule'].get('status'), 'evidence': [e['text'] for e in x.get('evidence') or []]})
    held_text = None
    if blind and rung > cand:
        held_text = 'The rung is held at %s because a reading is missing or stale (%s); it is not lowered by a missing reading.' % (STAGES[rung], ', '.join(r['id'] for r in blind_rules))
    state = {'schema': 1, 'asof': f_local(now), 'tz': U.TZ_NAME, 'status': 'Proposed (the channel recommends; the rain committee sets the stage)', 'drill': drill,
             'recommendedStage': STAGES[rung],                  # the name the console and the site read: the same words as recommended.stage; the channel recommends, the committee sets
             'validHours': current_h, 'staleAfter': f_local(now + dt.timedelta(hours=current_h)),
             'ladder': dict(lad, name=STAGES[rung], highest24h=top24, highest7d=top7, candidate=cand, blind=blind, blindHours=round(blind_h, 1)),
             'recommended': {'rung': rung, 'stage': STAGES[rung], 'highest': rung == HIGHEST, 'banner': 'READINESS HIGHEST' if rung == HIGHEST else None, 'why': why, 'heldText': held_text,
                             'committeeStage': cstage if cur_comm else None, 'committeeRecorded': comm, 'differs': differs, 'flag': flag, 'sets': 'never: the rain committee sets the stage'},
             'rules': rules_state, 'blindRules': [{'rule': r['id'], 'title': r['rule'].get('title'), 'evidence': [e['text'] for e in r.get('evidence') or []]} for r in blind_rules],
             'facts': {k: {'v': v['v'], 'unit': v['unit'], 'at': v['at'], 'age': v['age'], 'src': v['src'], 'note': v['note']} for k, v in facts.items()},
             'pack': {'rung': rung, 'stage': STAGES[rung], 'rows': pack['rows'], 'newAtRung': pack['newAtRung'], 'counts': pack['counts'], 'notes': pack['notes'], 'statusAsOf': pack['statusAsOf'], 'statusAgeDays': pack['statusAgeDays']},
             'activation': activation, 'notifications': notif, 'escalations': escal, 'expired': expired, 'season': season, 'health': h, 'sources': {k: {'file': v['file'], 'status': v['status'], 'asof': v['asof'], 'ageH': v['ageH'], 'current': v['current']} for k, v in src.items()}}
    snap_state = state
    if not dry:
        U.clean_stale_tmp(data_dir)                          # a temp file that a killed run left in this folder (an hour old at least)
        atomic_write_json(K.state_path(data_dir), state, indent=1)
        try:
            atomic_write_json(os.path.join(data_dir, COPY_NAME), state, indent=1)            # a copy the next run recovers from if the state file is lost or damaged (written after the state: the two are the same)
        except OSError:
            pass
        write_compact(P, state, now)
        patch_season_json(data_dir, season)
    return {'ok': True, 'state': state, 'results': results, 'fired': [r['id'] for r in results if r['fired']], 'events': events, 'created': [r['id'] for r in recs_created], 'code': 0}


# ------------------------------------------------------------------------------------------------ record: season, health, compact copy
def build_season(prev, now, hist, recs, ack_mins):
    """the season record: time at each rung per day (hours with a channel reading, from the hourly map), notifications by tier and the median time to acknowledge"""
    ps = (prev or {}).get('season') or {}
    days = {k: dict(v) for k, v in (ps.get('days') or {}).items()}
    by_day = {}
    for k, v in hist.items():
        d = k[:10]
        by_day.setdefault(d, {})
        by_day[d][str(v)] = by_day[d].get(str(v), 0) + 1
    for d, c in by_day.items():
        days[d] = {'rungHours': {str(r): c.get(str(r), 0) for r in range(HIGHEST + 1)}, 'hoursRead': sum(c.values())}
    notif_days = {}
    for r in recs:
        if r.get('drill'):
            continue
        d = (r.get('created') or '')[:10]
        notif_days.setdefault(d, {t: 0 for t in K.TIERS})
        notif_days[d][r.get('baseTier') or r.get('tier')] = notif_days[d].get(r.get('baseTier') or r.get('tier'), 0) + 1
    for d, c in notif_days.items():
        days.setdefault(d, {})
        days[d]['notifications'] = c
    tot = {t: sum(1 for r in recs if not r.get('drill') and (r.get('baseTier') or r.get('tier')) == t) for t in K.TIERS}
    first = ps.get('from') or (min(days) if days else f_local(now)[:10])
    return {'from': first, 'updated': f_local(now), 'days': dict(sorted(days.items())), 'notificationsByTier': tot, 'acknowledged': len(ack_mins), 'medianAckMinutes': round(statistics.median(ack_mins), 1) if ack_mins else None,
            'note': 'hours are clock hours in which the channel ran and read; an hour with no run is not counted. Drill records are not counted.'}


def self_test(P, now, rules_doc, rproblems, local, contacts, src, ncm, recs, open_, health, unreadable, t_start, alerts):
    roles = [r for r, v in (rules_doc.get('roles') or {}).items() if v.get('tier') != 'T0']          # the desk owner (T0) is told on this laptop: the toast, the outbox, the console. No contact is needed.
    nocontact = [r for r in roles if not K.role_has_contact(contacts, r)]
    cfg = transports_configured(local)
    problems = list(rproblems)
    # every action a rule starts must be an action the plan ties to a weather level
    trig = I.load_triggers(P['data']) or []
    tids = {t['id'] for t in trig}
    for r in rules_doc['rulesOk']:
        for a in r.get('actions') or []:
            if a not in tids:
                problems.append('%s starts %s, which action_triggers.json does not tie to a level' % (r['id'], a))
    top_unack = len([x for x in open_ if x.get('topTierUnacknowledged')])
    ages = [s['ageH'] for s in (src['weather'], src['staging']) if s['ageH'] is not None]
    feed_age = min(ages) if ages else None
    status = 'ok'
    if problems or alerts:
        status = 'degraded'
    h = {'status': status, 'rulesLoaded': len(rules_doc['rulesOk']), 'rulesSkipped': rules_doc.get('rulesSkipped', 0), 'problems': problems[:8] + alerts[:4],
         'contacts': {'rolesTotal': len(roles), 'withContact': len(roles) - len(nocontact), 'withoutContact': nocontact, 'note': 'a role with no contact is a gap with its owner; no value is stored here; the desk owner (T0) needs none: the toast, the outbox and the console reach it on this laptop'},
         'transports': {k: ('configured' if v else 'off') for k, v in cfg.items()}, 'autoSend': {t: K.auto_send(local, t) for t in ('T1', 'T2', 'T3')},
         'lastToast': health.get('lastToast'), 'feedAgeH': feed_age,
         'engineHeartbeat': {'asof': src['health']['asof'], 'ageH': src['health']['ageH']}, 'ncmAgeH': ncm.get('ageH'), 'unacknowledged': len([x for x in open_ if x.get('baseTier') != 'T0']), 'unacknowledgedTopTier': top_unack,
         'recordsUnreadable': unreadable, 'lastRun': {'at': f_local(now), 'ok': True, 'seconds': round(time.time() - t_start, 2)}, 'lastFailure': health.get('lastFailure')}
    return h


BAKE_EVENTS, BAKE_NOTIFICATIONS, BAKE_MAX_BYTES = 200, 100, 50000       # what the console build embeds: the last 200 events and 100 notifications, in at most 50,000 bytes (the console holds 7,413,431 of 7,700,000 bytes
                                                                         # and weather.json grows by about 219,000 when the engine replaces the legacy feed; the rest of the channel is fetched at run time)
_EVENT_COLS = ('t', 'kind', 'rule', 'id', 'tier', 'rung', 'by', 'from', 'to', 'drill')
_NOTE_COLS = ('id', 'rule', 'tier', 'baseTier', 'status', 'subject', 'created', 'dueMin', 'clearedMin', 'drill', 'steps', 'ackBy', 'ackMin')
_ID_PREFIX = 'NOTIF-'


def _short_id(x):
    return x[len(_ID_PREFIX):] if isinstance(x, str) and x.startswith(_ID_PREFIX) else x


def _bake_view(c):
    """channel.json cut to what the page needs before it has fetched anything: the stage and the banner, why, the counts and the first rows of the actions and of the timers, the health; at READINESS
    HIGHEST the actions of the rung and the sectors. The long lists of the activation pack (the other open actions, the audience of every role, the gaps) and the full evidence are fetched at run time."""
    f = lambda d, ks: {k: d.get(k) for k in ks if k in d}
    out = f(c, ('schema', 'asof', 'tz', 'validHours', 'staleAfter', 'status', 'drill', 'rung', 'stage', 'recommendedStage', 'highest', 'banner', 'rungSince', 'heldText', 'committeeStage', 'differs', 'flag', 'reached', 'words'))
    out['why'] = [{'rule': w.get('rule'), 'title': w.get('title'), 'rung': w.get('rung'), 'status': w.get('status'), 'evidence': [short_text(e, 140) for e in (w.get('evidence') or [])[:1]]} for w in (c.get('why') or [])[:6]]
    bl = c.get('blind') if isinstance(c.get('blind'), dict) else {}
    out['blind'] = {'on': bl.get('on'), 'hours': bl.get('hours'), 'rules': [{'rule': r.get('rule'), 'title': r.get('title')} for r in (bl.get('rules') or [])[:6]]}
    ac = c.get('actions') if isinstance(c.get('actions'), dict) else {}
    out['actions'] = {'counts': ac.get('counts'), 'due': [f(r, ('id', 'title', 'status', 'late')) for r in (ac.get('due') or [])[:14]], 'notes': [short_text(x, 160) for x in (ac.get('notes') or [])[:2]]}
    nt = c.get('notifications') if isinstance(c.get('notifications'), dict) else {}
    out['notifications'] = {'open': nt.get('open'), 'unacknowledged': nt.get('unacknowledged'), 'drafts': nt.get('drafts'),
                            'timers': [dict(f(r, ('rule', 'tier', 'status', 'minutesLeft', 'steps', 'held', 'topTier')), id=_short_id(r.get('id'))) for r in (nt.get('timers') or [])[:12]]}
    h = c.get('health') if isinstance(c.get('health'), dict) else {}
    out['health'] = f(h, ('status', 'rulesLoaded', 'feedAgeH', 'ncmAgeH', 'unacknowledged', 'lastRun'))
    ag = c.get('activation') if isinstance(c.get('activation'), dict) else None
    if ag:
        out['activation'] = {'banner': ag.get('banner'), 'stage': ag.get('stage'), 'actionsOfRung': [f(r, ('id', 'title', 'status', 'late')) for r in (ag.get('actionsOfRung') or [])[:16]],
                             'sectors': [f(r, ('sector', 'name', 'rainLevel', 'fogLevel', 'pumps', 'hireTankers')) for r in (ag.get('sectors') or [])[:12]], 'planTotals': ag.get('planTotals')}
    out['leftToRunTime'] = ['activation.otherOpen', 'activation.audience', 'activation.gaps', 'why[].evidence beyond the first', 'season']
    return out


def _minutes_after(t0, t1):
    try:
        return int(round((parse_local(t1) - parse_local(t0)).total_seconds() / 60.0)) if (t0 and t1) else None
    except (ValueError, TypeError):
        return None


def bake_state(data_dir, now=None, events=BAKE_EVENTS, notifications=BAKE_NOTIFICATIONS, max_bytes=BAKE_MAX_BYTES, channel_json=None):
    """The channel as the console build embeds it (25 Oct 2026, the stress test): the compact copy (channel.json) cut to what the first paint needs, the last `events` lines of the channel log and the newest
    `notifications` records, each cut to a short row in columns named once, with the kinds, roles and statuses written once in `tables` and the rows holding their number, the times of a notification as minutes
    after its creation, and the NOTIF- prefix left off the ids (idPrefix). A row is about 50 to 110 bytes, so 200 events and 100 notifications and the state fit in 50 KB. What was left out is counted, with the
    files the page fetches at run time for the rest. Read only. It reads the tail of the log and the newest records, never the whole outbox or the whole log. If the block is over `max_bytes` the two caps are
    lowered in steps until it fits, and it says so. Returns a dict that json.dumps writes."""
    now = now or now_local()
    compact = read_json(channel_json or os.path.join(RAIN_DIR if same_dir(data_dir, DEFAULT_DATA_DIR) else data_dir, 'channel.json'), None)
    state_ok = isinstance(compact, dict)
    view = _bake_view(compact) if state_ok else None
    ne, nn = events, notifications
    trimmed = []
    names = sorted((x for x in os.listdir(K.outbox_dir(data_dir)) if x.startswith('NOTIF-') and x.endswith('.json')), reverse=True) if os.path.isdir(K.outbox_dir(data_dir)) else []
    total_notif = len(names)
    raw_recs = []
    for nm in names[:max(0, nn)]:
        r = read_json(os.path.join(K.outbox_dir(data_dir), nm), None)
        if isinstance(r, dict) and r.get('id'):
            raw_recs.append(r)
    rows, total_events, bad = K.tail_log(data_dir, max(0, ne))
    tables = {'kinds': sorted({x.get('kind') for x in rows if x.get('kind')}), 'roles': sorted({x.get('by') for x in rows if x.get('by')} | {(r.get('ack') or {}).get('by') for r in raw_recs if isinstance(r.get('ack'), dict) and (r.get('ack') or {}).get('by')}),
              'status': list(K.STATUSES)}
    idx = lambda lst, v: (lst.index(v) if v in lst else None)
    recs = []
    for r in raw_recs:
        ack = r.get('ack') if isinstance(r.get('ack'), dict) else {}
        row = {'id': _short_id(r.get('id')), 'rule': r.get('rule'), 'tier': r.get('tier'), 'baseTier': r.get('baseTier'), 'status': idx(tables['status'], r.get('status')), 'subject': short_text(r.get('subject'), 70), 'created': r.get('created'),
               'dueMin': _minutes_after(r.get('created'), r.get('ackDueAt')), 'clearedMin': _minutes_after(r.get('created'), r.get('cleared')), 'drill': 1 if r.get('drill') else 0, 'steps': len(r.get('escalations') or []),
               'ackBy': idx(tables['roles'], ack.get('by')), 'ackMin': _minutes_after(r.get('created'), ack.get('at'))}
        recs.append([row.get(k) for k in _NOTE_COLS])
    evs = []
    for x in rows:
        r_ = [x.get(k) for k in _EVENT_COLS]
        r_[1], r_[3], r_[6], r_[9] = idx(tables['kinds'], x.get('kind')), _short_id(r_[3]), idx(tables['roles'], x.get('by')), 1 if x.get('drill') else 0
        evs.append(r_)
    while True:
        e_, n_ = (evs[-ne:] if ne else []), (recs[:nn] if nn else [])
        out = {'schema': 1, 'asof': f_local(now), 'purpose': 'the channel as the console build embeds it: the state the first paint needs, the last events and notifications in columns; everything else is fetched at run time',
               'state': view, 'stateNote': None if state_ok else 'channel.json is missing or does not read: the channel has no baked state (owner: the weather desk)',
               'idPrefix': _ID_PREFIX, 'columns': {'events': list(_EVENT_COLS), 'notifications': list(_NOTE_COLS)}, 'tables': tables,
               'columnsNote': 'events: kind and by are numbers into tables.kinds and tables.roles; notifications: status and ackBy are numbers into tables.status and tables.roles, dueMin, clearedMin and ackMin are minutes after created; drill is 0 or 1; ids lack the idPrefix',
               'events': e_, 'notifications': n_,
               'omitted': {'events': max(0, total_events - len(e_)), 'notifications': max(0, total_notif - len(n_)), 'unreadableLogLines': bad,
                           'fetch': {'state': 'channel.json', 'events': 'data/weather/channel_log.jsonl', 'notifications': 'data/weather/outbox/'}},
               'caps': {'events': ne, 'notifications': nn}, 'trimmed': trimmed}
        size = len(json.dumps(out, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if size <= max_bytes or (ne <= 10 and nn <= 5):
            out['bytes'] = size
            return out
        ne, nn = max(10, int(ne * 0.7)), max(5, int(nn * 0.7))
        trimmed.append('over %d bytes (%d): events %d, notifications %d' % (max_bytes, size, ne, nn))


def write_compact(P, state, now):
    """channel.json: the compact copy for the console and the site. It carries its own asof and says when it stops being current (3 hours)."""
    rec = state['recommended']
    pack = state['pack']
    acts = [{k: r.get(k) for k in ('id', 'title', 'owner', 'due', 'status', 'late', 'why')} for r in pack['rows']][:14]
    act = state.get('activation')
    out = {'schema': 1, 'asof': state['asof'], 'tz': state['tz'], 'validHours': state['validHours'], 'staleAfter': state['staleAfter'], 'status': state['status'], 'drill': state['drill'],
           'rung': rec['rung'], 'stage': rec['stage'], 'recommendedStage': rec['stage'], 'highest': rec['highest'], 'banner': rec['banner'], 'rungSince': state['ladder'].get('since'), 'why': rec['why'][:6], 'heldText': rec['heldText'],
           'committeeStage': rec['committeeStage'], 'differs': rec['differs'], 'flag': rec['flag'], 'reached': {'h24': state['ladder']['highest24h'], 'd7': state['ladder']['highest7d']},
           'blind': {'on': state['ladder']['blind'], 'hours': state['ladder']['blindHours'], 'rules': state['blindRules'][:6]},
           'actions': {'counts': pack['counts'], 'due': acts, 'notes': pack['notes'][:2]},
           'notifications': {'open': state['notifications']['open'], 'unacknowledged': state['notifications']['unacknowledged'], 'drafts': state['notifications']['drafts'], 'timers': state['notifications']['timers'][:12]},
           'health': {k: state['health'][k] for k in ('status', 'rulesLoaded', 'contacts', 'transports', 'lastToast', 'feedAgeH', 'engineHeartbeat', 'ncmAgeH', 'unacknowledged', 'lastRun')},
           'season': {'from': state['season']['from'], 'notificationsByTier': state['season']['notificationsByTier'], 'medianAckMinutes': state['season']['medianAckMinutes'], 'today': state['season']['days'].get(state['asof'][:10])},
           'words': words_of(state)}
    if act:
        out['activation'] = {'banner': act['banner'], 'stage': act['stage'], 'actionsOfRung': [{k: r.get(k) for k in ('id', 'title', 'owner', 'due', 'status', 'late')} for r in act['actionsOfRung']],
                             'otherOpen': [{k: r.get(k) for k in ('id', 'title', 'owner', 'status', 'late')} for r in act['otherActions'] if r['status'] != 'done'][:30], 'audience': [{k: r[k] for k in ('role', 'label', 'tier', 'contact')} for r in act['audience']],
                             'sectors': act['sectors'], 'planTotals': act['planTotals'], 'gaps': act['gaps']}
    try:
        atomic_write_json(P['channel_out'], out)
    except OSError:
        pass
    return out


def words_of(state):
    rec = state['recommended']
    s = 'Weather channel %s: %s%s.' % (state['asof'], rec['stage'], ' (READINESS HIGHEST)' if rec['highest'] else '')
    if state['drill']:
        s = 'DRILL. ' + s
    if rec['heldText']:
        s += ' ' + rec['heldText']
    n = state['notifications']
    s += ' %d open notification(s), %d awaiting acknowledgement.' % (n['open'], n['unacknowledged'])
    return plain(s)


def patch_season_json(data_dir, season):
    """add the channel block (and the channel columns of each day) to season.json, whole or not at all; the engine puts them back every time it rewrites the season record"""
    path = os.path.join(data_dir, 'season.json')
    if not os.path.exists(path):
        return False
    try:
        with U.file_lock(path, timeout=20):
            doc, st = read_json_status(path)
            if st != 'ok' or not isinstance(doc, dict):
                return False
            doc['channel'] = channel_season_block(season)
            for row in doc.get('days') or []:
                if isinstance(row, dict) and row.get('d') in season['days']:
                    row['channel'] = season['days'][row['d']]
            atomic_write_json(path, doc, indent=0)
        return True
    except OSError:
        return False


def channel_season_block(season):
    return {'from': season['from'], 'updated': season['updated'], 'notificationsByTier': season['notificationsByTier'], 'acknowledged': season['acknowledged'], 'medianAckMinutes': season['medianAckMinutes'],
            'rungHoursByDay': {d: v.get('rungHours') for d, v in season['days'].items() if v.get('rungHours')}, 'note': season['note'], 'file': 'data/weather/channel_state.json'}


def season_extra(data_dir):
    """for the engine: keys to add to season.json, from the channel state (read only; nothing is created)"""
    st = read_json(K.state_path(data_dir), None)
    if not isinstance(st, dict) or not isinstance(st.get('season'), dict):
        return None
    sn = st['season']
    return {'row': lambda day: ({'channel': sn['days'][day]} if day in sn.get('days', {}) else {}), 'top': {'channel': channel_season_block(sn)}}


def note_failure(data_dir, now, text):
    """called by the engine when the channel failed or ran out of time: the channel's own health says so (a failed channel never fails or delays the engine)"""
    path = K.state_path(data_dir)
    try:
        with U.file_lock(path, timeout=10):
            st = read_json(path, {}) if os.path.exists(path) else {}
            st = st if isinstance(st, dict) else {}
            h = dict(st.get('health') or {})
            h['lastFailure'] = {'at': f_local(now), 'text': short_text(text, 200)}
            h['status'] = 'degraded'
            st['health'] = h
            st.setdefault('schema', 1)
            atomic_write_json(path, st, indent=1)
        K.append_log(data_dir, 'channel_failed', now, text=short_text(text, 200))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ command line
def parse_args(argv):
    ap = argparse.ArgumentParser(description='WM004 weather channel')
    ap.add_argument('cmd', nargs='?', default='run', choices=('run', 'simulate', 'status', 'bake'))
    ap.add_argument('--out', default=None, help='bake: the file to write (default: print the size and write nothing)')
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    ap.add_argument('--weather', default=None, help='the published weather feed (default ../weather.json)')
    ap.add_argument('--staging', default=None, help='the engine staging copy (default ../weather_engine.json)')
    ap.add_argument('--conditions', default=None)
    ap.add_argument('--channel-out', default=None, help='channel.json to write (default ../channel.json)')
    ap.add_argument('--rules', default=None)
    ap.add_argument('--local', default=None)
    ap.add_argument('--contacts', default=None)
    ap.add_argument('--now', default=None, help='override the clock, ISO local time (tests, drills)')
    ap.add_argument('--drill', action='store_true', help='mark every record DRILL and send nothing beyond the desk owner and the outbox')
    ap.add_argument('--no-toast', action='store_true')
    ap.add_argument('--toast', action='store_true', help='show the toast even on a scratch folder (a drill on the laptop)')
    ap.add_argument('--dry-run', action='store_true', help='write nothing: print what would happen')
    ap.add_argument('--scenario', default=None)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--keep', action='store_true', help='simulate: keep the scratch folder and print its path')
    return ap.parse_args(argv)


def print_result(res, P):
    st = res['state']
    rec = st['recommended']
    print('weather channel %s | %s%s | blind %s | %d rule(s) on | %d open notification(s) | %d action(s) due, %d late%s' % (
        st['asof'], rec['stage'], ' READINESS HIGHEST' if rec['highest'] else '', 'yes' if st['ladder']['blind'] else 'no', len([1 for v in st['rules'].values() if v.get('on')]),
        st['notifications']['open'], st['pack']['counts']['due'], st['pack']['counts']['late'], ' | DRILL' if st['drill'] else ''))
    for w in rec['why'][:6]:
        print('  holds %s %s: %s' % (w['rule'], w['title'], '; '.join(w['evidence'])[:200]))
    for b in st['blindRules'][:6]:
        print('  blind %s %s' % (b['rule'], b['title']))
    if rec['flag']:
        print('  ' + rec['flag'])
    for c in res.get('created') or []:
        print('  notification %s' % c)


def main(argv=None):
    a = parse_args(list(sys.argv[1:] if argv is None else argv))
    if a.cmd == 'simulate':
        import channel_sim as S
        return S.main_simulate(a)
    P = resolve_paths(a)
    now = parse_local(a.now) if a.now else now_local()
    if a.cmd == 'status':
        st = read_json(K.state_path(P['data']), None)
        if not isinstance(st, dict):
            print('no channel state yet (the channel has not run on %s)' % P['data'])
            return 1
        print(st.get('recommended', {}).get('stage'), '|', words_of(st) if 'recommended' in st else '')
        return 0
    if a.cmd == 'bake':
        out = bake_state(P['data'], now, channel_json=P['channel_out'])
        if a.out:
            atomic_write_json(os.path.abspath(a.out), out)
        print('baked %d bytes: %d events and %d notifications (%s left out), state %s' % (out['bytes'], len(out['events']), len(out['notifications']),
                                                                                         '%d and %d' % (out['omitted']['events'], out['omitted']['notifications']), 'read' if out['state'] else 'missing'))
        return 0
    if a.scenario and not a.drill:
        print('a scenario is synthetic input: it runs only in drill mode (--drill) or with the simulate command')
        return 2
    toast = True if a.toast else (False if a.no_toast else default_toast(P, bool(a.now)))
    try:
        res = run_channel(P, now, drill=a.drill, dry=a.dry_run, toast=toast, rules_path=a.rules, local_path=a.local, contacts_path=a.contacts)
    except U.LockTimeout as e:
        print('CHANNEL BUSY: %s' % e)
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        note_failure(P['data'], now, '%s: %s' % (type(e).__name__, e))
        print('CHANNEL FAILED: %s' % e)
        return 1
    if not res.get('ok'):
        print('CHANNEL BLIND: %s' % '; '.join(res.get('problems') or ['no rules']))
        return res.get('code', 2)
    if a.json:
        print(json.dumps({'rung': res['state']['recommended']['rung'], 'fired': res['fired'], 'created': res['created']}, ensure_ascii=False))
    else:
        print_result(res, P)
    return 0


if __name__ == '__main__':
    sys.exit(main())
