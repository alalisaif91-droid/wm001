# -*- coding: utf-8 -*-
"""The recording side of the WM004 weather engine: the run ledger and the event register.

LEDGER (data/weather/ledger/YYYY-MM.jsonl): one line per engine run, appended with a single write, flush and fsync, never rewritten. A half written last line
is skipped when reading and left where it is. Every line stays under 12 KB and carries no hourly arrays.

EVENTS (data/weather/events.json): a state machine for rain, fog and dust events. One event can be open per hazard, never two. An event opens on the first
trigger and closes after a quiet period with no trigger: 12 hours for rain, 3 hours for fog and for dust. The quiet period counts only hours the engine
actually watched: a run that read nothing, or a gap between runs longer than the limit, restarts it, so an outage can never close an event by itself.
The machine survives restarts (all state is in the file), duplicate runs (a trigger is recorded once, evidence inside a closed event is ignored) and clock
jumps (time never moves back past the last run; a jump is reported as an alert). A file that exists and cannot be read is never taken for a missing one: it is kept
aside (events.json.corrupt-stamp), the ids it held are not issued again, and the alert stays until a person has dealt with the kept file (audit D4, WX-04).
Evidence of a station or of NCM that arrives later than the clear time after the last trigger, with the hours between covered by station reports, closes the open
event at last trigger plus the clear time and opens a new one: after a sleeping laptop, two nights are two events (audit logic D5)."""
import os, re, json, datetime as dt, uuid
from wx_util import (read_json, read_json_status, read_text_raw, quarantine_copy, read_jsonl, atomic_write_json, append_durable, f_local, parse_local, floor_hour, hours_between, med, r1)
import wx_models as M

CLEAR_H = {'rain': 12, 'fog': 3, 'dust': 3}
GAP_LIMIT_H = {'rain': 6.0, 'fog': 3.0, 'dust': 3.0}
RANK = {'rain': {'none': 0, 'watch': 1, 'high': 2}, 'fog': {'none': 0, 'watch': 1, 'alert': 2, 'severe': 3}, 'dust': {'none': 0, 'reported': 1}}
NCM_TO_LEVEL = {'rain': {'yellow': 'watch', 'orange': 'high', 'red': 'high'}, 'fog': {'yellow': 'watch', 'orange': 'alert', 'red': 'severe'}, 'dust': {'yellow': 'reported', 'orange': 'reported', 'red': 'reported'}}
LEDGER_MAX = 12000
LABELS = {'rain': 'Rain', 'fog': 'Fog', 'dust': 'Dust'}


# ------------------------------------------------------------------------------------------------ ledger
def ledger_path(data_dir, when):
    return os.path.join(data_dir, 'ledger', when.strftime('%Y-%m') + '.jsonl')


def _compact(d):
    return json.dumps(d, ensure_ascii=False, separators=(',', ':'))


def fit_ledger_line(line, limit=LEDGER_MAX):
    """Serialise one ledger line under `limit` bytes. If it is too long, optional detail is dropped in a fixed order and the line says what was dropped
    ('trimmed'); the figures that verification reads (per point ensemble numbers, day totals, levels) are dropped last."""
    s = _compact(line)
    if len(s.encode('utf-8')) <= limit:
        return s
    trimmed = []

    def drop_points(key):
        return lambda d: [p.pop(key, None) for p in d.get('points', [])]
    def drop_cond(key):
        return lambda d: [r.pop(key, None) for r in ((d.get('cond') or {}).get('pts') or []) if isinstance(r, dict)]
    steps = [('conditions extremes per point', drop_cond('x')),
             ('fog detail per point', drop_points('fog')),
             ('observation detail', lambda d: d.pop('obs', None)),
             ('per model figures', drop_points('m')),
             ('sources detail', lambda d: [v.pop('errors', None) for v in d.get('sources', {}).values() if isinstance(v, dict)]),
             ('conditions of the stations and the points now', lambda d: (d.get('cond') or {}).pop('stn', None) if isinstance(d.get('cond'), dict) else None)]
    for name, fn in steps:
        fn(line)
        trimmed.append(name)
        line['trimmed'] = trimmed
        s = _compact(line)
        if len(s.encode('utf-8')) <= limit:
            return s
    # last resort: keep identity, time and levels only
    keep = {k: line.get(k) for k in ('v', 'run', 'now', 'started', 'finished', 'level', 'fog', 'quality', 'ncm')}
    keep['trimmed'] = trimmed + ['everything but identity and levels']
    return _compact(keep)


def append_ledger(data_dir, line, finished):
    path = ledger_path(data_dir, finished)
    s = fit_ledger_line(line)
    append_durable(path, s)
    return path, len(s.encode('utf-8'))


def read_ledger(data_dir, since=None, until=None):
    """all readable ledger lines between two datetimes (by the line's 'now'), oldest first. Read only; half written lines are skipped."""
    folder = os.path.join(data_dir, 'ledger')
    out, bad = [], 0
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.jsonl'))
    except OSError:
        return out, bad
    for n in names:
        # the ledger is in monthly files (rolling files): a month that cannot hold a line of the window is not opened. Before this (stress test T6, 25 Oct 2026) every run parsed every month ever written to
        # take the few lines of the season: 8.9 s for 15,000 lines of which 634 were wanted. A line is in the file of the month it finished in, which is the month of its `now` or the next one, so a day
        # of margin is kept at each end. A file whose name is not a month is read, as before.
        try:
            y, m = int(n[:4]), int(n[5:7])
            m0 = dt.datetime(y, m, 1) - dt.timedelta(days=1)
            m1 = dt.datetime(y + (m == 12), (m % 12) + 1, 1) + dt.timedelta(days=1)
            if (since and m1 <= since) or (until and m0 > until):
                continue
        except ValueError:
            pass
        rows, b = read_jsonl(os.path.join(folder, n))
        bad += b
        for r in rows:
            try:
                t = parse_local(r.get('now'))
            except ValueError:
                continue
            if t is None or (since and t < since) or (until and t > until):
                continue
            out.append(r)
    out.sort(key=lambda r: r.get('now') or '')
    return out, bad


def compact_line(r):
    """A ledger line cut to the fields the verification reads and nothing else: the time, the run, whether it read a model, per point the forecast day totals (dm, dx), the fog forecast of each day and per
    point of the conditions block the daily values. weather_verify.run and wx_condrec.conditions_verification read these fields and no others (a test says so)."""
    c = {'now': r.get('now'), 'run': r.get('run'), 'ok': r.get('ok')}
    pts = r.get('points')
    if isinstance(pts, list):
        c['points'] = [{'dm': p.get('dm'), 'dx': p.get('dx')} if isinstance(p, dict) else {} for p in pts]
    fd = r.get('fogDays')
    if isinstance(fd, list):
        c['fogDays'] = [{'d': x.get('d'), 'l': x.get('l')} for x in fd if isinstance(x, dict)]
    cond = r.get('cond')
    if isinstance(cond, dict) and isinstance(cond.get('pts'), list):
        c['cond'] = {'pts': [{'i': p.get('i'), 'd': p.get('d')} for p in cond['pts'] if isinstance(p, dict)]}
    return c


CACHE_CHECK_BYTES = 64


def _cache_check(f, size):
    """a check of the first `size` bytes of an open file without reading them all: the first and the last 64 bytes and 8 places between. It tells a file that was replaced or cut and written again from
    the file the cache was made from; an edit of a few bytes in the middle of a ledger, which is never made by the engine, is not what it looks for"""
    import hashlib
    h = hashlib.sha1()
    for k in range(10):
        pos = 0 if k == 0 else (max(0, size - CACHE_CHECK_BYTES) if k == 9 else (size * k) // 9)
        f.seek(max(0, min(pos, max(0, size - CACHE_CHECK_BYTES))))
        h.update(f.read(min(CACHE_CHECK_BYTES, size)))
    return h.hexdigest()


def _month_compact(path, cache_path):
    """(rows, bad) of one month file as compact lines. The cut lines are kept in cache_path (a derived file: delete it and it is made again): the number of bytes of the ledger that it covers, a check of the
    last bytes of that part, the unreadable lines in it and the compact rows. A run reads only the bytes appended since, so the cost of a run does not grow with the lines already read. The cache is used
    only while the file still begins with what it was made from (the size is not smaller and the check of the first, the last and some bytes between matches); otherwise the month is read again from the start. A last line with no newline
    (a write cut short) is not consumed, and it counts as an unreadable line each time, as it does in read_jsonl."""
    import hashlib
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    c = read_json(cache_path, None)
    rows, bad, off = [], 0, 0
    with open(path, 'rb') as f:
        if isinstance(c, dict) and c.get('v') == 1 and isinstance(c.get('rows'), list) and isinstance(c.get('size'), int) and 0 <= c['size'] <= size:
            ok = True
            if c['size'] > 0:
                ok = _cache_check(f, c['size']) == c.get('check')
            if ok:
                rows, bad, off = c['rows'], int(c.get('bad') or 0), c['size']
        f.seek(off)
        data = f.read()
    cut = data.rfind(b'\n') + 1                                          # the complete lines; what follows the last newline is a line still being written (or cut short)
    new_rows, new_bad = [], 0
    for line in data[:cut].decode('utf-8', 'replace').split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            new_bad += 1
            continue
        if isinstance(o, dict):
            new_rows.append(compact_line(o))
        else:
            new_bad += 1
    tail = data[cut:].decode('utf-8', 'replace').strip()                 # a last line with no newline: read if it is a whole line, counted if it is not, never kept in the cache (the next write ends it)
    tail_rows, tail_bad = [], 0
    if tail:
        try:
            o = json.loads(tail)
            if isinstance(o, dict):
                tail_rows.append(compact_line(o))
            else:
                tail_bad = 1
        except ValueError:
            tail_bad = 1
    if cut > 0:
        rows, bad = rows + new_rows, bad + new_bad
        consumed = off + cut
        with open(path, 'rb') as f:
            chk = _cache_check(f, consumed)
        try:
            atomic_write_json(cache_path, {'v': 1, 'note': 'a cache of the ledger cut to what the verification reads (wx_events.read_ledger_compact); delete it and it is made again', 'size': consumed, 'check': chk, 'bad': bad, 'rows': rows})
        except OSError:
            pass
    return rows + tail_rows, bad + tail_bad


def read_ledger_compact(data_dir, since=None, until=None):
    """read_ledger for the verification (25 Oct 2026, stress test T1: the verification parsed the whole season of the ledger at every run, 38 MB at the end of it, and a run late in the season took ten times
    as long as the first). The same lines, in the same order, for the same window, each cut by compact_line, and the months that cannot hold a line of the window are not opened. The parse of a month is kept
    in ledger/verify-YYYY-MM.json and extended with the bytes appended since (_month_compact), so a run reads the bytes of its own line and the compact rows of the season, about 1 percent of the file."""
    folder = os.path.join(data_dir, 'ledger')
    out, bad = [], 0
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.jsonl'))
    except OSError:
        return out, bad
    for n in names:
        try:
            y, m = int(n[:4]), int(n[5:7])
            m0 = dt.datetime(y, m, 1) - dt.timedelta(days=1)
            m1 = dt.datetime(y + (m == 12), (m % 12) + 1, 1) + dt.timedelta(days=1)
            if (since and m1 <= since) or (until and m0 > until):
                continue
        except ValueError:
            pass
        rows, b = _month_compact(os.path.join(folder, n), os.path.join(folder, 'verify-' + n[:-6] + '.json'))
        bad += b
        for r in rows:
            try:
                t = parse_local(r.get('now'))
            except ValueError:
                continue
            if t is None or (since and t < since) or (until and t > until):
                continue
            out.append(r)
    out.sort(key=lambda r: r.get('now') or '')
    return out, bad


def _pick_targets_near(t, spec):
    """the targets of the spec that a row at time t is within the tolerance of: the start of a day D (from spec['start'] on) minus a lead. The same arithmetic as weather_verify.pick_run."""
    out = []
    tol = spec['tol']
    start = dt.datetime.strptime(spec['start'], '%Y-%m-%d')
    for lead in spec['leads']:
        ld = dt.timedelta(hours=lead)
        lo = t + ld - dt.timedelta(hours=tol)
        d = dt.datetime(lo.year, lo.month, lo.day)
        while d <= t + ld + dt.timedelta(hours=tol):
            if d >= start:
                target = d - ld
                if abs((t - target).total_seconds()) / 3600.0 <= tol:
                    out.append(target)
            d += dt.timedelta(days=1)
    return out


def pick_best_rows(pool, spec):
    """pool: [(time, row)] in the order of the file. The rows of the pool that are the nearest of the pool, within the tolerance, to some target of the spec (the first of two at the same distance), in time order.
    The nearest run of the whole ledger to a target is among them, so a run list made of these rows gives the same answer to weather_verify.pick_run as the whole ledger does (a test says so)."""
    import bisect
    pool = sorted(pool, key=lambda x: x[0])                               # a stable sort: two rows of one time keep the order of the file
    times = [t for t, _ in pool]
    targets = set()
    for t, _ in pool:
        targets.update(_pick_targets_near(t, spec))
    tol = spec['tol']
    keep = set()
    for target in targets:
        lo = bisect.bisect_left(times, target - dt.timedelta(hours=tol))
        hi = bisect.bisect_right(times, target + dt.timedelta(hours=tol))
        best = None
        for i in range(lo, hi):
            d = abs((times[i] - target).total_seconds()) / 3600.0
            if d <= tol and (best is None or d < best[0]):
                best = (d, i)
        if best:
            keep.add(best[1])
    return [pool[i] for i in sorted(keep)]


def _eligible(o):
    """(time, compact row) of a ledger line the verification can use: it read a model (ok is not False) and its time reads; None otherwise"""
    if o.get('ok') is False:
        return None
    try:
        t = parse_local(o.get('now'))
    except ValueError:
        return None
    return None if t is None else (t, compact_line(o))


def _minutes(t):
    return int((t - dt.datetime(1970, 1, 1)).total_seconds() // 60)


def _month_picks(path, cache_path, spec):
    """(kept [(time, compact row)], times [the minute of every row the verification could use], bad) of one month file. The rows kept are the ones that are the nearest run to some target of the spec: about two
    a day, where the whole month is 744 lines of 10 KB (25 Oct 2026, stress test T1: the compact rows of a whole season were 15 MB, parsed at every run, and made a run late in the season cost three times a run in
    October and the process 60 percent larger). The cache is picks-YYYY-MM.json (a derived file: delete it and it is made again from the ledger): the bytes of the ledger it covers, a check of them, the
    unreadable lines, the spec it was made for, the rows kept and the minutes of the rows. A run reads the bytes appended since and decides which rows are kept among the rows kept before and the new ones.
    A cache made for another spec is not used. A last line with no newline is read if it is whole, counted if it is not, and never kept in the cache."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], [], 0
    c = read_json(cache_path, None)
    kept, times, bad, off = [], [], 0, 0
    with open(path, 'rb') as f:
        if (isinstance(c, dict) and c.get('v') == 2 and c.get('spec') == spec and isinstance(c.get('rows'), list) and isinstance(c.get('times'), list) and isinstance(c.get('size'), int)
                and 0 <= c['size'] <= size):
            ok = True
            if c['size'] > 0:
                ok = _cache_check(f, c['size']) == c.get('check')
            if ok:
                try:
                    kept = [(parse_local(r['now']), r) for r in c['rows']]
                    times, bad, off = list(c['times']), int(c.get('bad') or 0), c['size']
                except (ValueError, KeyError, TypeError):
                    kept, times, bad, off = [], [], 0, 0
        f.seek(off)
        data = f.read()
    cut = data.rfind(b'\n') + 1
    new_rows, new_bad = [], 0
    for line in data[:cut].decode('utf-8', 'replace').split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            new_bad += 1
            continue
        if not isinstance(o, dict):
            new_bad += 1
            continue
        e = _eligible(o)
        if e:
            new_rows.append(e)
    tail = data[cut:].decode('utf-8', 'replace').strip()
    tail_rows, tail_bad = [], 0
    if tail:
        try:
            o = json.loads(tail)
            e = _eligible(o) if isinstance(o, dict) else None
            if isinstance(o, dict):
                if e:
                    tail_rows.append(e)
            else:
                tail_bad = 1
        except ValueError:
            tail_bad = 1
    if cut > 0:
        kept = pick_best_rows(kept + new_rows, spec)
        times = times + [_minutes(t) for t, _ in new_rows]
        bad += new_bad
        consumed = off + cut
        with open(path, 'rb') as f:
            chk = _cache_check(f, consumed)
        try:
            atomic_write_json(cache_path, {'v': 2, 'note': 'a cache of the ledger cut to the runs the verification can pick (wx_events.read_ledger_picks); delete it and it is made again', 'spec': spec, 'size': consumed,
                                           'check': chk, 'bad': bad, 'times': times, 'rows': [r for _, r in kept]})
        except OSError:
            pass
    return kept + tail_rows, times + [_minutes(t) for t, _ in tail_rows], bad + tail_bad


def read_ledger_picks(data_dir, since=None, until=None, spec=None):
    """(rows, bad, runs) for the verification: the rows that can be the nearest run to a target of the spec (see pick_best_rows), the unreadable lines, and the number of runs the verification could use in the
    window (a run that read a model, between since and until: the figure verification.json calls ledgerRuns). It answers pick_run for every target the spec names whose tolerance window lies inside
    [since, until] (the verification asks only about days before today, so a clock that is behind, or a ledger line later than now, cannot change an answer); anything else must use read_ledger_compact.
    spec: {'start': 'YYYY-MM-DD', 'leads': [hours], 'tol': hours}."""
    import bisect
    spec = json.loads(json.dumps(spec))                                      # lists and floats as the cache holds them
    folder = os.path.join(data_dir, 'ledger')
    out, bad, runs = [], 0, 0
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.jsonl'))
    except OSError:
        return out, bad, runs
    lo = -((-int((since - dt.datetime(1970, 1, 1)).total_seconds())) // 60) if since else None          # the first whole minute not before since
    hi = _minutes(until) if until else None
    for n in names:
        try:
            y, m = int(n[:4]), int(n[5:7])
            m0 = dt.datetime(y, m, 1) - dt.timedelta(days=1)
            m1 = dt.datetime(y + (m == 12), (m % 12) + 1, 1) + dt.timedelta(days=1)
            if (since and m1 <= since) or (until and m0 > until):
                continue
        except ValueError:
            pass
        kept, times, b = _month_picks(os.path.join(folder, n), os.path.join(folder, 'picks-' + n[:-6] + '.json'), spec)
        bad += b
        times = sorted(times)
        runs += (bisect.bisect_right(times, hi) if hi is not None else len(times)) - (bisect.bisect_left(times, lo) if lo is not None else 0)
        for t, r in kept:
            if (since and t < since) or (until and t > until):
                continue
            out.append(r)
    out.sort(key=lambda r: r.get('now') or '')
    return out, bad, runs


# ------------------------------------------------------------------------------------------------ events
def _new_state():
    return {'lastNow': None, 'machines': {h: {'lastGood': None, 'quietBreak': None, 'lastTrigger': None} for h in CLEAR_H}, 'seq': {}}


class EventRegister:
    def __init__(self, path):
        self.path = path
        self.alerts = []
        self.damaged = None                   # {'reason','keptAs','writeBlocked','idsHeld'} when the file was there and could not be read
        self.vanished = None                  # a sentence when there is no events.json but the ledger holds earlier runs
        self.save_error = None
        d, status = read_json_status(path)
        salvaged_seq, salvaged_now = {}, None
        if status == 'ok' and not self._shape_ok(d):
            status = 'shape'                                            # valid JSON, but events that the register cannot read: kept aside like a damaged file, never a crash on every run
        if status == 'ok':
            self.data = d
        elif status == 'missing':
            self.data = {'schema': 2, 'events': []}
            if self._ledger_has_runs():
                # no events.json, but the ledger holds earlier runs: the register was deleted or never saved. It is started again, the ids come from the ledger, and the run says so (audit F12)
                self.vanished = 'events.json is missing although the ledger holds earlier runs: the event history may have been deleted; a new register was started and its ids continue from the ledger'
                self.data['restartNote'] = {'note': self.vanished}
        else:
            # a file that is there and does not read is not an empty register: keep it aside before anything is written, keep its ids from being issued again, alert
            raw = read_text_raw(path) if status != 'unreadable' else ''
            for m in re.finditer(r'EV-(\d{8})-(\d+)', raw):
                salvaged_seq[m.group(1)] = max(salvaged_seq.get(m.group(1), 0), int(m.group(2)))
            mm = re.search(r'"lastNow"\s*:\s*"([^"]+)"', raw)
            salvaged_now = mm.group(1) if mm else None
            self.data = {'schema': 2, 'events': []}
            self.damaged = {'reason': {'unreadable': 'cannot be opened', 'damaged': 'does not parse'}.get(status, 'is in a shape the register cannot read'), 'keptAs': None, 'writeBlocked': status == 'unreadable',
                            'idsHeld': sum(salvaged_seq.values())}
        st = self.data.get('state')
        base = _new_state()
        if isinstance(st, dict):
            base['lastNow'] = st.get('lastNow')
            base['seq'] = st.get('seq') if isinstance(st.get('seq'), dict) else {}
            for h, m in (st.get('machines') or {}).items():
                if h in base['machines'] and isinstance(m, dict):
                    base['machines'][h].update(m)
        if salvaged_seq:
            for k, v in salvaged_seq.items():
                base['seq'][k] = max(base['seq'].get(k, 0), v)
        if salvaged_now and not base['lastNow']:
            base['lastNow'] = salvaged_now
        self.data['state'] = base
        self.events = self.data['events']
        self._repair()

    @staticmethod
    def _shape_ok(d):
        """an events.json the register can work with: a dict with a list of events, each a dict with a text id, a known hazard and times that read"""
        if not (isinstance(d, dict) and isinstance(d.get('events'), list)):
            return False
        try:
            for e in d['events']:
                if not (isinstance(e, dict) and isinstance(e.get('id'), str) and e.get('hazard') in CLEAR_H and isinstance(e.get('opened'), str)):
                    return False
                for k in ('opened', 'closed', 'lastTrigger'):
                    if e.get(k):
                        parse_local(e[k])
            st = d.get('state')
            if st is not None and not isinstance(st, dict):
                return False
        except (ValueError, TypeError):
            return False
        return True

    def problems(self):
        """plain alerts about a damaged events.json met in this run"""
        d = self.damaged
        if not d:
            return [self.vanished] if self.vanished else []
        if d.get('writeBlocked'):
            return ['events.json cannot be opened: no event is written until it can be read; events seen in this run are kept in memory only']
        return ['events.json %s: a new event register was started, %d event ids it held are not issued again%s; the events it held must be merged back by hand' % (
            d['reason'], d.get('idsHeld') or 0, (', the damaged file is kept as ' + d['keptAs']) if d.get('keptAs') else ' (it will be kept aside when the register is written)')]

    # one open event per hazard: if a hand edit or a crash left two, keep the earliest and close the others as merged
    def _repair(self):
        for h in CLEAR_H:
            opens = sorted([e for e in self.events if e.get('hazard') == h and e.get('status') == 'open'], key=lambda e: e.get('opened') or '')
            for extra in opens[1:]:
                extra['status'] = 'closed'
                extra['closed'] = extra.get('lastTrigger') or extra.get('opened')
                extra['note'] = 'closed as a duplicate open event found at load; the earliest one was kept'
                self.alerts.append('two open %s events found; %s closed as a duplicate' % (h, extra.get('id')))

    def open_event(self, hazard):
        for e in self.events:
            if e.get('hazard') == hazard and e.get('status') == 'open':
                return e
        return None

    def _next_id(self, when):
        key = when.strftime('%Y%m%d')
        seq = self.data['state']['seq']
        held = []
        for e in self.events:
            if str(e.get('id', '')).startswith('EV-' + key + '-'):
                try:
                    held.append(int(str(e['id']).split('-')[-1]))
                except ValueError:
                    pass
        n = max(seq.get(key, 0) if isinstance(seq.get(key, 0), int) else 0, max(held or [0]), self._ledger_seq(key)) + 1
        seq[key] = n
        return 'EV-%s-%02d' % (key, n)

    def _ledger_seq(self, key):
        """the highest sequence number issued on a day, read from the ledger: every event a run opens is named in that run's ledger line, so an events.json that was cut short, replaced or
        deleted can never make an id be issued twice (audit F12). Only the month of the day and the one after are read."""
        folder = os.path.join(os.path.dirname(os.path.abspath(self.path)), 'ledger')
        try:
            y, m = int(key[:4]), int(key[4:6])
            months = {'%04d-%02d' % (y, m), '%04d-%02d' % (y + (m == 12), (m % 12) + 1)}
            names = [n for n in os.listdir(folder) if n.endswith('.jsonl') and n[:7] in months]
        except (OSError, ValueError):
            return 0
        pat = re.compile(r'EV-%s-(\d+)' % re.escape(key))
        best = 0
        for n in names:
            try:
                with open(os.path.join(folder, n), encoding='utf-8', errors='replace') as f:
                    for mm in pat.finditer(f.read()):
                        best = max(best, int(mm.group(1)))
            except OSError:
                continue
        return best

    def _ledger_has_runs(self):
        folder = os.path.join(os.path.dirname(os.path.abspath(self.path)), 'ledger')
        try:
            return any(n.endswith('.jsonl') and os.path.getsize(os.path.join(folder, n)) > 0 for n in os.listdir(folder))
        except OSError:
            return False

    def counts(self):
        """how many events of each hazard are on record and how many were confirmed by a station or an NCM warning: an event that only a model level opened is a forecast flag, so a plain count
        of events overstates the real episodes, above all for fog (audit WXR-05)"""
        out = {}
        for hz in CLEAR_H:
            evs = [e for e in self.events if e.get('hazard') == hz]
            out[hz] = {'total': len(evs), 'confirmed': sum(1 for e in evs if e.get('confirmed')), 'open': sum(1 for e in evs if e.get('status') == 'open')}
        out['note'] = 'confirmed means a station report or an NCM warning came with it; the others rest on a model level alone and are forecast flags, not episodes that happened'
        return out

    def _closed_span_holds(self, hazard, t):
        for c in self.events:
            if c.get('hazard') == hazard and c.get('status') == 'closed' and c.get('closed'):
                if parse_local(c['opened']) <= t <= parse_local(c['closed']):
                    return True
        return False

    def _last_closed_end(self, hazard):
        ends = [parse_local(c['closed']) for c in self.events if c.get('hazard') == hazard and c.get('status') == 'closed' and c.get('closed')]
        return max(ends) if ends else None

    # ----------------------------------------------------------------------------------------- the update
    def update(self, now, evidence, run_ok, ctx=None, trusted_clock=False):
        """now: naive local datetime of this run. evidence: {hazard: [ {time, kind, key, level?, observed?, detail?} ]}.
        run_ok: {hazard: bool} whether this run could see the hazard (models or stations answered). ctx: dict of helpers for the event summary ('covered' (t0, t1) says
        whether station reports cover the hours between). trusted_clock: this run's time was checked against the servers' own clock, so an earlier run that stamped a later time
        was the wrong one: the event clock is reset instead of being held at it (audit WX-07).
        Returns a list of change strings."""
        ctx = ctx or {}
        st = self.data['state']
        changes = []
        try:
            last = parse_local(st['lastNow']) if st.get('lastNow') else None
        except (ValueError, TypeError):
            last = None
        now_eff = now
        if last and trusted_clock and last > now + dt.timedelta(minutes=5):
            self.alerts.append('the event clock was ahead: the last run stamped %s and the servers say it is %s now (a run with a wrong laptop clock); the event clock is reset to the true time' % (st['lastNow'], f_local(now)))
            for m in st['machines'].values():
                for k in ('lastGood', 'quietBreak', 'lastTrigger'):
                    try:
                        if m.get(k) and parse_local(m[k]) > now:
                            m[k] = f_local(now)
                    except (ValueError, TypeError):
                        m[k] = f_local(now)
            future = []
            for e in self.events:
                try:
                    if any(e.get(k) and parse_local(e[k]) > now + dt.timedelta(minutes=5) for k in ('opened', 'lastTrigger', 'closed')):
                        future.append(str(e.get('id')))
                except (ValueError, TypeError):
                    pass
            if future:
                self.alerts.append('event(s) %s carry times later than now, written by the run with the wrong clock: check them by hand' % ', '.join(future))
            last = None
        if last and now < last - dt.timedelta(minutes=5):
            self.alerts.append('the clock moved back: this run is stamped %s, the last run was %s; time is held at the last run' % (f_local(now), f_local(last)))
            now_eff = last
        elif last and now < last:
            now_eff = last
        for hz in CLEAR_H:
            m = st['machines'][hz]
            ok = bool(run_ok.get(hz))
            gl = parse_local(m['lastGood']) if m.get('lastGood') else None
            if ok:
                if gl is None or hours_between(gl, now_eff) > GAP_LIMIT_H[hz]:
                    m['quietBreak'] = f_local(now_eff)
                m['lastGood'] = f_local(now_eff)
            else:
                m['quietBreak'] = f_local(now_eff)
            # a model level is a statement of this run: its time is this run's own time (the evidence the engine builds is stamped now), so when the clock moved back and time is held at the last
            # run, that evidence is held there too and cannot wind the opening of an event back (audit F11)
            evs_in = [dict(e, time=now_eff) if (e['kind'] == 'model-level' and e['time'] == now and now_eff > now) else e for e in (evidence.get(hz) or [])]
            ev_list = sorted([e for e in evs_in if e['time'] <= now_eff + dt.timedelta(seconds=1)], key=lambda e: (e['time'], e.get('key', '')))
            op = self.open_event(hz)
            lookback = ctx.get('lookback_h') or 30
            for e in ev_list:
                # Evidence at or before the end of a closed event belongs to that event or to no event, never to another one (audit EV-1). Every run supplies the whole look back (30 hours), so the
                # evidence of an event that has closed comes again at every run after the next event has opened; without this line it was attached to the new event, which then read the old peak,
                # a first trigger older than its own opening, inflated trigger counts and a station confirmation that only the old event had.
                lce0 = self._last_closed_end(hz)
                if lce0 and e['time'] <= lce0:
                    continue
                if op is not None and e['kind'] in ('station', 'ncm'):
                    lt0 = parse_local(op['lastTrigger']) if op.get('lastTrigger') else parse_local(op['opened'])
                    if e['time'] > lt0 + dt.timedelta(hours=CLEAR_H[hz]) and ctx.get('covered', lambda a, b: True)(lt0, e['time']):
                        # the record shows a quiet period longer than the clear time before this evidence: it is a new event, not the end of the old one (audit logic D5)
                        self._close(op, hz, lt0 + dt.timedelta(hours=CLEAR_H[hz]), now_eff, ctx)
                        changes.append('closed %s %s at %s after %d h with no trigger (found when later evidence was read)' % (hz, op['id'], op['closed'], CLEAR_H[hz]))
                        op = None
                if op is not None:
                    lt0 = parse_local(op['lastTrigger']) if op.get('lastTrigger') else parse_local(op['opened'])
                    gap_h = hours_between(lt0, e['time'])
                    if gap_h > CLEAR_H[hz]:
                        if gap_h > lookback:
                            # more time between the last trigger and this evidence than any station record can cover (the look-back of every run): nothing shows that the hazard went on in between,
                            # and one event of days that holds an outage is not one event. It is closed at the last trigger plus the clear time and the evidence opens a new one (audit F6)
                            self._close(op, hz, lt0 + dt.timedelta(hours=CLEAR_H[hz]), now_eff, ctx)
                            op['closedNote'] = 'closed when evidence arrived %.0f h after its last trigger, a gap that no record covers (an outage); the hours between were not observed' % gap_h
                            changes.append('closed %s %s at %s: the next evidence came %.0f h after its last trigger, after a gap that was not observed' % (hz, op['id'], op['closed'], gap_h))
                            op = None
                        else:
                            # a gap the record cannot show to be quiet: one event, as before, and the event says that it spans hours that were not observed
                            gaps = op.setdefault('spansUnobservedGap', [])
                            if not any(g.get('to') == f_local(e['time']) for g in gaps):
                                gaps.append({'from': f_local(lt0), 'to': f_local(e['time']), 'hours': round(gap_h, 1)})
                            op['gapNote'] = 'this event holds a stretch with no trigger longer than the clear time that the record does not show to be quiet: its duration and opening readiness may describe more than one episode'
                if op is None:
                    if self._closed_span_holds(hz, e['time']):
                        continue                                          # already accounted for by a closed event (a duplicate run, a restart)
                    lce = self._last_closed_end(hz)
                    if lce and e['time'] <= lce:
                        continue
                    op = self._open(hz, e['time'], now_eff, ctx)
                    changes.append('opened %s %s at %s (%s)' % (hz, op['id'], op['opened'], e['kind']))
                elif e['time'] < parse_local(op['opened']):
                    lce = self._last_closed_end(hz)
                    if not lce or e['time'] > lce:
                        op['opened'] = f_local(e['time'])
                self._add_trigger(op, e)
                m['lastTrigger'] = f_local(max(e['time'], parse_local(m['lastTrigger'])) if m.get('lastTrigger') else e['time'])
            if op is not None:
                self._summarise(op, hz, now_eff, ctx)
                lt = parse_local(op['lastTrigger']) if op.get('lastTrigger') else parse_local(op['opened'])
                qb = parse_local(m['quietBreak']) if m.get('quietBreak') else lt
                ref = max(lt, qb)
                if ok and hours_between(ref, now_eff) >= CLEAR_H[hz]:
                    self._close(op, hz, ref + dt.timedelta(hours=CLEAR_H[hz]), now_eff, ctx)
                    changes.append('closed %s %s at %s after %d h with no trigger' % (hz, op['id'], op['closed'], CLEAR_H[hz]))
                else:
                    op['quietHours'] = round(max(0.0, hours_between(ref, now_eff)), 1)
        st['lastNow'] = f_local(now_eff)
        self.data['updated'] = f_local(now_eff)
        return changes

    def _close(self, op, hz, closed_at, now_eff, ctx):
        op['status'] = 'closed'
        op['closed'] = f_local(closed_at)
        op['closedDetected'] = f_local(now_eff)
        self._summarise(op, hz, now_eff, ctx, final=True)
        for k in ('_keys', '_hours'):
            op.pop(k, None)

    def _open(self, hz, when, now_eff, ctx):
        e = {'id': self._next_id(when), 'hazard': hz, 'status': 'open', 'opened': f_local(when), 'closed': None, 'lastTrigger': None, 'triggers': {}, 'triggersSeen': [],
             'confirmed': False, 'peakLevel': 'none', 'peakAt': None, '_keys': []}
        rd = ctx.get('readiness')
        if rd:
            e['readiness'] = {'atOpen': rd(now_eff)}
        self.events.append(e)
        return e

    def _add_trigger(self, ev, e):
        k = e.get('key') or '%s|%s' % (e['kind'], f_local(e['time']))
        keys = ev.setdefault('_keys', [])
        t = f_local(e['time'])
        if k in keys:
            return
        keys.append(k)
        if len(keys) > 3000:
            del keys[:500]
        tr = ev['triggers'].setdefault(e['kind'], {'n': 0, 'first': t, 'last': t})
        tr['n'] += 1
        tr['first'] = min(tr['first'], t)
        tr['last'] = max(tr['last'], t)
        if e.get('detail'):
            tr['detail'] = e['detail']
        if e.get('station'):
            tr.setdefault('stations', [])
            if e['station'] not in tr['stations']:
                tr['stations'].append(e['station'])
        if e['kind'] not in ev['triggersSeen']:
            ev['triggersSeen'].append(e['kind'])
        if e['kind'] in ('station', 'ncm'):
            ev['confirmed'] = True
        if not ev.get('lastTrigger') or t > ev['lastTrigger']:
            ev['lastTrigger'] = t
        lv = e.get('level')
        rk = RANK[ev['hazard']]
        if lv in rk and rk[lv] > rk.get(ev['peakLevel'], 0):
            ev['peakLevel'] = lv
            ev['peakAt'] = t
            ev['_peakRise'] = True

    # ----------------------------------------------------------------------------------------- the summary of an event
    def _summarise(self, ev, hz, now_eff, ctx, final=False):
        opened = parse_local(ev['opened'])
        end = parse_local(ev['lastTrigger']) if (ev.get('status') == 'closed' or final) and ev.get('lastTrigger') else now_eff
        end = max(end, opened)
        ev['durationH'] = round(hours_between(opened, end), 1)
        rd = ctx.get('readiness')
        if rd and ev.pop('_peakRise', False):
            ev.setdefault('readiness', {})['atPeak'] = rd(now_eff)
        elif '_peakRise' in ev:
            ev.pop('_peakRise')
        store, stations = ctx.get('store'), ctx.get('stations') or []
        if store is not None:
            from wx_obs import station_hours_in_span
            span_end = end if ev.get('status') == 'closed' or final else now_eff
            hrs = station_hours_in_span(store, stations, hz, floor_hour(opened), span_end)
            ev['stationHours'] = hrs
            if hz == 'rain':
                ev['stationsRain'] = {k: v for k, v in hrs.items()}
            gk = 0
            for m in stations:
                for t, rec in store.hours(m['icao'], floor_hour(opened), span_end):
                    gk = max(gk, rec.get('gustKt', 0))
            ev['gustMaxKmh'] = round(gk * 1.852) if gk else None
            if hz == 'fog':
                from wx_obs import _rec_flag, FOG_VIS_LE
                best = None
                for m in stations:
                    for t, rec in store.hours(m['icao'], floor_hour(opened), span_end):
                        if _rec_flag(rec, 'fog') is None:
                            continue                          # only the hours in which this station reported fog: a clear station does not set the visibility of a fog event (audit WX-23)
                        mv = rec.get('minVis')
                        if mv is None or mv > FOG_VIS_LE:
                            continue                          # fog reported with no visibility group of 1000 m or less: no visibility figure, not 10000
                        if best is None or mv < best[0]:
                            best = (mv, f_local(t + dt.timedelta(minutes=rec.get('minVisMin', 0))), m['icao'])
                ev['minVisM'] = best[0] if best else None
                ev['minVisAt'] = best[1] if best else None
                ev['minVisStation'] = best[2] if best else None
                ev['minVisNote'] = None if best else 'no station visibility of 1000 m or less in the span (no station reported fog with a visibility group, or the record starts later)'
        if hz == 'rain' and ctx.get('points'):
            acc_end = now_eff
            if ev.get('status') == 'closed' or final:
                # the rain of a closed event runs to the close, not to the last trigger: rain after the last model level trigger was dropped before although the event stayed open 12 hours more (audit WXR-04)
                try:
                    acc_end = max(end, parse_local(ev['closed'])) if ev.get('closed') else end
                except (ValueError, TypeError):
                    acc_end = end
            self._rain_accumulation(ev, opened, acc_end, ctx)
        if ctx.get('ncm_in_span'):
            ev['ncmEntries'] = ctx['ncm_in_span'](hz, opened, end if (ev.get('status') == 'closed' or final) else now_eff)
        if ctx.get('history'):
            ev['history'] = ctx['history'](hz, ev)

    def _rain_accumulation(self, ev, opened, end, ctx):
        """Model analysis of the event span at each point. An hour is kept model by model (a list of the nine values) when the run has them, so that the accumulation is the median across the
        models of each model's own sum, as the season record builds its days, and not the sum of the hourly medians, which is thin when the models disagree on the hour (audit WXR-02). An hour
        kept by an older record or a test is one number, the median, and counts for every model."""
        hrs = ev.setdefault('_hours', {})
        start_label = floor_hour(opened) + dt.timedelta(hours=1)
        end_label = floor_hour(end) + (dt.timedelta(hours=1) if end.minute or end.second else dt.timedelta(0))
        out = []
        now_label = floor_hour(ctx['now']) if ctx.get('now') else end_label
        for p in ctx['points']:
            h = hrs.setdefault(str(p['idx']), {})
            vec = p.get('past_vec') or {}
            for lab, mm in (p.get('past') or {}).items():
                t = dt.datetime.strptime(lab, '%Y-%m-%dT%H:%M')
                if start_label <= t <= end_label and t <= now_label:
                    v = vec.get(lab)
                    h[lab] = list(v) if isinstance(v, list) else mm
            labs = sorted(l for l in h if dt.datetime.strptime(l, '%Y-%m-%dT%H:%M') <= end_label)      # labels after the end of the event are not part of it (audit logic D14)
            entries = [h[l] for l in labs]
            n, ser = M.model_series(entries)
            basis = 'models'
            if n:
                tots = [sum(s) for s in ser if s and all(x is not None for x in s)]
                m6s, pks = [], []
                for s in ser:
                    got = [i for i, x in enumerate(s) if x is not None]
                    if not got:
                        continue
                    m6s.append(max(sum(x for x in s[i:i + 6] if x is not None) for i in range(len(s))))
                    pi = max(got, key=lambda i: (s[i], labs[i]))
                    pks.append((s[pi], labs[pi]))
                if tots:
                    tot = med(tots)
                else:
                    # no model has every hour of the span: the hourly medians are all there is, and it is said
                    meds = [(e if not isinstance(e, list) else med([x for x in e if x is not None])) for e in entries]
                    tot = sum(x for x in meds if x is not None)
                    basis = 'hourly median (no single model has every hour of the span)'
                m6 = med(m6s) if m6s else 0.0
                pk_mm = med([a for a, _ in pks]) if pks else None
                pl = sorted(b for _, b in pks)
                pk = pl[len(pl) // 2] if pl else None
            else:
                tot = sum(h[l] for l in labs)
                m6 = 0.0
                for i in range(len(labs)):
                    seg = labs[i:i + 6]
                    m6 = max(m6, sum(h[l] for l in seg))
                pk = max(labs, key=lambda l: (h[l], l)) if labs else None
                pk_mm = h[pk] if pk else None
                basis = 'median of the models, hour by hour'
            out.append({'name': p['name'], 'accumulationMm': round(tot, 1), 'max6Mm': round(m6, 1), 'peakHour': pk, 'peakMm': round(pk_mm, 1) if pk_mm is not None else None, 'hoursRead': len(labs), 'basis': basis})
        ev['points'] = out
        ev['accumulationTo'] = f_local(min(end_label, now_label))
        ev['pointsNote'] = ('Model analysis (Recomputed): the median across the models of each model\'s own total over the analysis hours of the event, from its first hour to the end of the event (to the close once '
                            'it is closed, so the rain after the last trigger is in it); not a rain gauge (no gauge register is on file, owner Wet Utilities with NCM)')
        gm = ctx.get('gust_past') or {}
        vals = [v for k, v in gm.items() if v is not None and start_label <= dt.datetime.strptime(k, '%Y-%m-%dT%H:%M') <= end_label]
        ev['modelGustMaxKmh'] = round(max(vals)) if vals else None
        best = max(out, key=lambda x: x['accumulationMm']) if out else None
        if best:
            ev['worst'] = {'point': best['name'], 'accumulationMm': best['accumulationMm'], 'max6Mm': max(x['max6Mm'] for x in out),
                           'peakHour': max(out, key=lambda x: (x['peakMm'] or 0))['peakHour'], 'peakMm': max((x['peakMm'] or 0) for x in out)}
            ev['peakRainHour'] = ev['worst']['peakHour']
        ev['peakNote'] = 'peakAt is when the highest level was first reached (readiness.atPeak is taken then); peakRainHour is the heaviest model analysis hour of the event'


    # ----------------------------------------------------------------------------------------- output
    def save(self):
        """writes events.json. A damaged file is copied aside first; if that cannot be done, or the file cannot be opened, nothing is written (the run reports it instead).
        Raises OSError on a failed write: the caller reports it and goes on, a locked file never stops the weather from being published (audit WX-12)."""
        if self.damaged:
            if self.damaged.get('writeBlocked'):
                self.save_error = 'events.json cannot be opened: not written'
                return False
            if not self.damaged.get('keptAs'):
                self.damaged['keptAs'] = os.path.basename(quarantine_copy(self.path))
        self.data['schema'] = 2
        self.data['status'] = 'Rain, fog and dust events as a state machine. Model figures: Model analysis (Recomputed). Station figures: Reported. Levels: Proposed. Rules in SCHEMA.md.'
        self.data['rules'] = {'rain': 'opens on a model level of watch or high, a station report of rain, shower or thunderstorm, or an NCM colour warning that names Ras Al Khaimah or does not say; closes after 12 hours with no trigger',
                              'fog': 'opens on a station report of fog or visibility under 1000 m, an NCM fog colour warning that names Ras Al Khaimah or does not say, or a fog level of alert or severe; closes after 3 hours with no trigger',
                              'dust': 'opens on a station report of dust or an NCM dust colour warning; closes after 3 hours with no trigger',
                              'quietClock': 'a run that read nothing, or a gap between runs longer than %s hours (rain) or %s hours (fog, dust), restarts the quiet period' % (GAP_LIMIT_H['rain'], GAP_LIMIT_H['fog'])}
        atomic_write_json(self.path, self.data, indent=1)
        return True

    def summary(self, hazard):
        """short form of the open event of a hazard for weather.json (None when none is open)"""
        e = self.open_event(hazard)
        if not e:
            return None
        keys = ('id', 'hazard', 'opened', 'lastTrigger', 'durationH', 'quietHours', 'peakLevel', 'peakAt', 'peakRainHour', 'triggersSeen', 'confirmed', 'stationHours', 'worst', 'minVisM', 'minVisAt', 'minVisStation', 'minVisNote', 'gustMaxKmh', 'ncmEntries', 'readiness')
        return {k: e.get(k) for k in keys if k in e}

    def ids_on(self, day):
        """event ids of each hazard that were open at any time on a calendar day"""
        d0 = dt.datetime.strptime(day, '%Y-%m-%d')
        d1 = d0 + dt.timedelta(days=1)
        out = {}
        for e in self.events:
            try:
                o = parse_local(e['opened'])
                c = parse_local(e['closed']) if e.get('closed') else None
                last = parse_local(e['lastTrigger']) if e.get('lastTrigger') else o
                if o < d1 and (c is None or c >= d0) and last >= d0 - dt.timedelta(hours=1):
                    out.setdefault(e['hazard'], []).append(e['id'])
            except (KeyError, ValueError, TypeError):
                continue
        return out


def new_run_id(now):
    return 'WX-%s-%s' % (now.strftime('%Y%m%dT%H%M'), uuid.uuid4().hex[:4])
