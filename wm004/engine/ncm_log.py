# -*- coding: utf-8 -*-
"""NCM reading log for WM004: one structured line per reading of the National Center of Meteorology warnings (status Reported, official).

The log is data/weather/ncm_log.jsonl. It is append only: an entry is never edited or removed, a correction is a new entry.
Reading commands (list, latest, inforce) never create a file or a folder.

  python ncm_log.py add --level none|yellow|orange|red --hazard rain|fog|dust|wind|sea|heat|other[,more] --areas TEXT
                        --rak-named yes|no|unknown --from ISO --to ISO --text TEXT --source URL --by TEXT
                        [--read ISO] [--kind warning|bulletin|outlook] [--issued ISO] [--data-dir DIR]
  python ncm_log.py list   [--hazard H] [--since ISO] [--json]
  python ncm_log.py latest [--hazard H] [--json]
  python ncm_log.py inforce [--at ISO] [--json]
  python ncm_log.py import-barq [--barq-log PATH]        backfills the readings written in BARQ/log.md, once
  python ncm_log.py refused [--json]                     the typed readings the log refused that are still open
  python ncm_log.py clear-refused [--id ID] [--note T]    acknowledge them (every open one without --id); the lines stay in ncm_refused.jsonl

A level is never written without a source URL. A colour level with no end time stays in force for NO_END_HOURS after it was read,
and the entry says so (windowKnown false). A reading of level none states that no colour was in force for those hazards when it
was read; a later manual reading of none ends an earlier colour entry for the hazards the none names, and only those; an automatic one
(by engine) never does, and neither does a none that rests on NCM's page message, whoever logged it (no_person). A colour that has been issued but starts later is listed as upcoming and counts in the level from the time it was
read, up to UPCOMING_HOURS (24) before its start, because an NCM warning is issued ahead of its window.
Automatic readings are appended with by=engine and never replace, edit or hide a manual entry."""
import os, re, sys, json, argparse, datetime as dt
from wx_util import (DEFAULT_DATA_DIR, HERE, now_local, parse_local, f_local, append_durable, append_jsonl_locked, file_lock, read_jsonl, hours_between, short_text)

LEVELS = ('none', 'yellow', 'orange', 'red')
RANK = {'none': 0, 'yellow': 1, 'orange': 2, 'red': 3}
HAZARDS = ('rain', 'fog', 'dust', 'wind', 'sea', 'heat', 'other')
KINDS = ('warning', 'bulletin', 'outlook')
STATUS = 'Reported, official'
NO_END_HOURS = 12
LOG_NAME = 'ncm_log.jsonl'
REFUSED_NAME = 'ncm_refused.jsonl'
PAGE_MESSAGE = re.compile(r'\bno warnings? for now\b|msg-no-warnings|لا (?:يوجد|توجد) (?:أي )?تحذير(?:ات)? (?:حالي|الآن)', re.I)     # NCM's message over an empty map. Six failures print it (site_ncm/SCHEMA.md section 4), so it proves nothing
OLD_BULLETIN_AREAS = 'as the bulletin words it: coastal, internal and mountain areas'                                     # on the rows of the log up to 31 Oct 2026: it said it was the bulletin's wording, so it never leaves the log
BULLETIN_AREAS = 'no sector read: a bulletin, not a warning'
DEFAULT_BARQ_LOG = os.path.join(HERE, '..', 'BARQ', 'log.md')


def log_path(data_dir):
    return os.path.join(data_dir, LOG_NAME)


def refused_path(data_dir):
    return os.path.join(data_dir, REFUSED_NAME)


class Refusal(ValueError):
    """a manual entry the log will not take. A ValueError, so every caller that catches ValueError still does; kind says why (level, hazard, source, text, rak-named, kind, time, duplicate)."""
    def __init__(self, kind, msg):
        ValueError.__init__(self, msg)
        self.kind = kind


def _entry_readable(r):
    """a line that parses as JSON but whose times cannot be read (a hand edit such as read '24 Oct 2026 12:10') is skipped and counted, never a crash on every run
    until someone edits an append only log (audit WX-11). Also normalises the shape the readers rely on."""
    try:
        if not isinstance(r.get('read'), str):
            return False
        parse_local(r['read'])
        for k in ('from', 'to'):
            if r.get(k):
                if not isinstance(r[k], str):
                    return False
                parse_local(r[k])
            else:
                r[k] = None
    except (ValueError, TypeError):
        return False
    r['id'] = str(r['id'])
    r['hazard'] = parse_hazards(r.get('hazard'))
    for k in ('text', 'source', 'areas'):
        if not isinstance(r.get(k), str):
            r[k] = '' if r.get(k) is None else str(r[k])
    return True


def load(data_dir):
    """(entries sorted by read time then id, number of unreadable lines). Read only."""
    rows, bad = read_jsonl(log_path(data_dir))
    ok = [r for r in rows if r.get('id') and r.get('read') and r.get('level') in LEVELS and _entry_readable(r)]
    bad += len(rows) - len(ok)
    ok.sort(key=lambda r: (r['read'], r['id']))
    return ok, bad


COVER_HAZARDS = ('rain', 'fog', 'dust')


def page_message_none(e):
    """True for a none that rests on NCM's page message. The log is append only and Barq logged this every day until 1 Nov 2026 as by 'manual, through weather_feed.py', so it is found by its words when it is read, not by who wrote it."""
    return e.get('level') == 'none' and bool(PAGE_MESSAGE.search(str(e.get('text') or '')))


def feed_none(e):
    """True for a none entered through weather_feed.py. Only Barq used that path, from NCM's page; a person at the desk logs with ncm_log.py add. Words alone did not find every such row (independent review, H1 second round)."""
    return e.get('level') == 'none' and e.get('by') == 'manual, through weather_feed.py'


def no_person(e):
    """True when no reading by a person stands behind an entry: an automatic one (by engine), or a none that rests on the page message. Neither ends a colour, neither counts as a person's reading beside a reader that is down
    (independent review H1: a page none ended a colour the live reader logged, and stood for 3 hours under a failed reader), and neither covers a page colour, a refusal or an unreadable line."""
    return e.get('by') == 'engine' or page_message_none(e) or feed_none(e)


def unreadable_status(data_dir, bad=None):
    """bad: the count of unreadable lines that load() has just returned for this log, when the caller has it. load() and this function count the same lines, so a log with none is not read a second time
    (25 Oct 2026, stress test T1: the NCM log was parsed twice at every run of the engine and of the channel, and it grows by about 5 entries a day).
    What the lines of the log that cannot be read mean for the levels (audit F10). The log is the only channel for official warnings, so a line that does not read (cut in half, a
    hand typed read time) may be one. None when every line reads. Otherwise {'lines', 'pendingHazards', 'pending', 'coveredBy', 'namedHazards'}: it is dealt with when a person has logged a
    warning reading that names the hazard AFTER the last unreadable line (by position in the file: the log is append only, so a later line is a later reading). Until then the hazards no
    such reading names are unknown, not none. Read only."""
    if bad == 0:
        return None
    try:
        with open(log_path(data_dir), encoding='utf-8', errors='replace') as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return None
    n_bad, after = 0, []
    for ln in lines:
        ok = None
        try:
            o = json.loads(ln)
            if isinstance(o, dict) and o.get('id') and o.get('read') and o.get('level') in LEVELS and _entry_readable(o):
                ok = o
        except ValueError:
            ok = None
        if ok is None:
            n_bad += 1
            after = []
        else:
            after.append(ok)
    if not n_bad:
        return None
    named, by = set(), []
    for e in after:
        if e.get('kind') == 'warning' and not no_person(e):
            named |= set(e['hazard'])
            by.append(e['id'])
    pend = [h for h in COVER_HAZARDS if h not in named]
    return {'lines': n_bad, 'pendingHazards': pend, 'pending': bool(pend), 'coveredBy': by[:5], 'namedHazards': sorted(named & set(COVER_HAZARDS))}


def unreadable_text(st):
    return ('%d line(s) of ncm_log.jsonl cannot be read and may hold an NCM warning: the %s level(s) are unknown, not none, until a person logs a reading of the NCM page that names %s '
            '(ncm_log.py add), made after the damage; or the line is repaired' % (st['lines'], ', '.join(st['pendingHazards']) or 'rain, fog and dust', ', '.join(st['pendingHazards']) or 'rain, fog and dust'))


def parse_hazards(s):
    if isinstance(s, (list, tuple)):
        items = [str(x).strip().lower() for x in s]
    else:
        items = [x.strip().lower() for x in str(s or '').split(',')]
    items = [x for x in items if x]
    seen = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen


def next_id(entries, read_dt):
    prefix = 'NCM-' + read_dt.strftime('%Y%m%d') + '-'
    n = 0
    for e in entries:
        if str(e.get('id', '')).startswith(prefix):
            try:
                n = max(n, int(e['id'].split('-')[-1]))
            except ValueError:
                pass
    return '%s%03d' % (prefix, n + 1)


def build_entry(entries, level, hazard, text, source, areas='', rak_named='unknown', t_from=None, t_to=None, by='manual', read=None,
                kind='warning', issued=None, now=None, extra=None):
    """Validate and build one entry (not written). Raises ValueError with the reason in plain words."""
    now = now or now_local()
    level = (level or '').strip().lower()
    if level not in LEVELS:
        raise Refusal('level', 'level must be one of %s, got %r' % (', '.join(LEVELS), level))
    hz = parse_hazards(hazard)
    if not hz:
        raise Refusal('hazard', 'hazard is required: one or more of %s separated by commas' % ', '.join(HAZARDS))
    bad = [h for h in hz if h not in HAZARDS]
    if bad:
        raise Refusal('hazard', 'unknown hazard %s; use %s' % (', '.join(bad), ', '.join(HAZARDS)))
    if not source or not str(source).strip().lower().startswith(('http://', 'https://')):
        raise Refusal('source', 'a level is never written without a source URL (--source https://...)')
    if not text or not str(text).strip():
        raise Refusal('text', 'text is required: what the NCM page said, in a sentence')
    rak = (rak_named or 'unknown').strip().lower()
    if rak not in ('yes', 'no', 'unknown'):
        raise Refusal('rak-named', 'rak-named must be yes, no or unknown')
    if kind not in KINDS:
        raise Refusal('kind', 'kind must be one of %s' % ', '.join(KINDS))
    try:
        read_dt = parse_local(read) if read else now
        f_dt = parse_local(t_from) if t_from else None
        t_dt = parse_local(t_to) if t_to else None
        i_dt = parse_local(issued) if issued else None
    except ValueError as e:
        raise Refusal('time', 'a time could not be read: %s (use YYYY-MM-DD HH:MM, local Asia/Dubai, or with an offset)' % e)
    if read_dt is None:
        raise Refusal('time', 'read time missing')
    if read_dt > now + dt.timedelta(minutes=5):
        raise Refusal('time', 'the read time %s is in the future' % f_local(read_dt))
    if f_dt and t_dt and t_dt < f_dt:
        raise Refusal('time', '--to is before --from')
    window_known = True
    if level != 'none':
        if f_dt is None:
            f_dt = read_dt
        if t_dt is None:
            window_known = False
    e = {'id': next_id(entries, read_dt), 'read': f_local(read_dt), 'level': level, 'hazard': hz, 'areas': (areas or '').strip(), 'rakNamed': rak,
         'from': f_local(f_dt), 'to': f_local(t_dt), 'windowKnown': window_known, 'text': ' '.join(str(text).split()), 'source': str(source).strip(),
         'by': (by or 'manual').strip(), 'kind': kind, 'status': STATUS, 'logged': f_local(now)}
    if i_dt:
        e['issued'] = f_local(i_dt)
    if extra:
        e.update(extra)
    return e


def is_duplicate(entries, e):
    keys = ('read', 'level', 'hazard', 'from', 'to', 'text', 'kind')
    return any(all(x.get(k) == e.get(k) for k in keys) for x in entries)


def add_entry(data_dir, now=None, write=True, **kw):
    """Validate, number and append one entry. Under the cross process lock of the log (audit N3) the log is read, the id is drawn, the duplicate check is made and the line is written,
    so two writers that act at the same instant (a person, Barq, the engine's own automatic entry) can neither lose a line nor draw the same id. Raises Refusal (a ValueError) with the
    reason in plain words when the entry is not taken; the caller keeps the refused request (record_refusal) and never drops it. write=False builds and checks only."""
    path = log_path(data_dir)

    def make(_rows):
        entries, _ = load(data_dir)
        e = build_entry(entries, now=now, **kw)
        if is_duplicate(entries, e):
            raise Refusal('duplicate', 'duplicate: an identical entry is already in the log (%s)' % next((x['id'] for x in entries if x['read'] == e['read'] and x['text'] == e['text']), '?'))
        return e, e
    if not write:
        return make(None)[1]
    return append_jsonl_locked(path, make)


# ------------------------------------------------------------------------------------------------ refused entries (audit N1, NEW-2)
# A manual entry the log refuses (a typo in the hazard or the level, a source that is not a link, a time that cannot be read, a duplicate) is never dropped: the request and the reason are
# kept in ncm_refused.jsonl (append only, like the log), weather_feed.py exits 3, and the engine puts a high alert in weather.json and health.json and in the NCM block, which stays until
# a valid reading for the same level is logged, or a person acknowledges it (python ncm_log.py clear-refused). A typed red must never vanish.
def public_reason(reason):
    """the reason in words for a screen: a command line flag is named by what it is"""
    r = str(reason or '')
    for a, b in (('(--source https://...)', '(a link that begins with https://)'), ('--to is before --from', 'the end time is before the start time'),
                 ('--source', 'the source'), ('--from', 'the start time'), ('--to', 'the end time'), ('--', '')):
        r = r.replace(a, b)
    return short_text(r, 220).rstrip('.')


def _req(kw):
    """the request as typed, short: level, hazard, areas, rak-named, from, to, text (cut), source, by, read, kind, issued"""
    def g(k, n=200):
        return short_text(kw.get(k), n) if kw.get(k) not in (None, '') else None
    return {'level': g('level', 40), 'hazard': g('hazard', 80), 'areas': g('areas'), 'rakNamed': g('rak_named', 20), 'from': g('t_from', 40), 'to': g('t_to', 40), 'read': g('read', 40),
            'kind': g('kind', 20), 'issued': g('issued', 40), 'text': g('text', 400), 'source': g('source', 200), 'by': g('by', 80)}


def _pending_hazards(request):
    """which of rain, fog and dust a refused reading could be about, so that their levels are unknown, not none, until it is dealt with: the hazards it names, or all three when a hazard
    word is not one of ours (it may have meant rain) or none is given. A refused none changes no level."""
    lvl = (request.get('level') or '').strip().lower()
    if lvl == 'none':
        return []
    toks = parse_hazards(request.get('hazard'))
    if not toks or any(t not in HAZARDS for t in toks):
        return list(COVER_HAZARDS)
    return [h for h in COVER_HAZARDS if h in toks]


def _next_seq(rows, prefix):
    n = 0
    for r in rows:
        i = str(r.get('id', ''))
        if i.startswith(prefix):
            try:
                n = max(n, int(i.split('-')[-1]))
            except ValueError:
                pass
    return n + 1


def record_refusal(data_dir, refusal, request_kw, via, now=None):
    """append the refused request to ncm_refused.jsonl (under the lock; the id NCMR-YYYYMMDD-NNN is drawn under it). Returns the line. A duplicate is written as resolved: the identical entry
    is in the log, nothing was lost, so it raises no alert. Raises OSError when the file cannot be written (the caller says so)."""
    import re
    now = now or now_local()
    kind = getattr(refusal, 'kind', 'other')
    base = {'kind': 'refused', 'at': f_local(now), 'reason': short_text(str(refusal), 300).rstrip('.'), 'reasonKind': kind, 'via': via, 'request': _req(request_kw),
            'status': 'Reported, official request not logged'}
    if kind == 'duplicate':
        m = re.search(r'\((NCM-\d{8}-\d+)\)', str(refusal))
        base['resolved'] = {'by': 'duplicate', 'entry': m.group(1) if m else None, 'note': 'the identical entry is already in ncm_log.jsonl; nothing was lost'}
    prefix = 'NCMR-' + now.strftime('%Y%m%d') + '-'

    def make(rows):
        line = dict(base, id='%s%03d' % (prefix, _next_seq(rows, prefix)))
        return line, line
    return append_jsonl_locked(refused_path(data_dir), make)


def try_add(data_dir, via, now=None, write=True, **kw):
    """add_entry, with the refusal kept. Returns (entry or None, refusal line or None, message or None). Never raises for a refusal or for a log that cannot be written: a lock that times out
    or a disk that is full is a refusal too (the request is then kept in the refused file when that can be written)."""
    now = now or now_local()
    try:
        return add_entry(data_dir, now=now, write=write, **kw), None, None
    except ValueError as ex:
        msg = str(ex)
        ref = ex
    except OSError as ex:
        msg = 'the log could not be written (%s: %s)' % (type(ex).__name__, str(ex)[:120])
        ref = Refusal('write', msg)
    line = None
    if write:
        try:
            line = record_refusal(data_dir, ref, kw, via, now)
        except OSError as ex:
            msg += '; and the refused request could not be kept either (%s)' % str(ex)[:80]
    return None, line, msg


def acknowledge_refused(data_dir, ref=None, now=None, note=None):
    """append an acknowledgement for one refused request (ref = its id) or for every open one (ref None): the alert goes, the lines stay. Returns the ids acknowledged."""
    now = now or now_local()
    st = refused_status(data_dir, load(data_dir)[0], now)
    open_ids = [x['id'] for x in st['open']]
    ids = open_ids if ref is None else ([ref] if ref in open_ids else [])
    if not ids and not (ref is None and (st.get('unreadable') or {}).get('open')):
        return []
    prefix = 'NCMA-' + now.strftime('%Y%m%d') + '-'

    def make(rows):
        line = {'kind': 'ack', 'at': f_local(now), 'ref': ref or 'all', 'note': short_text(note, 200) if note else None, 'id': '%s%03d' % (prefix, _next_seq(rows, prefix))}
        return line, line
    append_jsonl_locked(refused_path(data_dir), make)
    if ref is None and (st.get('unreadable') or {}).get('open'):
        ids = ids + ['the unreadable line(s) of ncm_refused.jsonl']
    return ids


def refused_status(data_dir, entries, now):
    """What the refused requests mean now. Read only; creates nothing. entries are the readable lines of the log. Returns
    {'open': [...], 'resolved': [...], 'count': n, 'unreadable': {'lines','open'} or None}. A refusal is open until
      (a) a person has acknowledged it (an ack line for its id, or for all, written after it), or
      (b) a valid manual reading of the same level has been logged since (and, when the refused request named hazards we know, one of the same hazards): the typed reading is then in the log.
    A request whose level was a typo (reed) is cleared only by (a): nobody can say which level it was. An automatic entry and a backfill never clear one."""
    try:
        with open(refused_path(data_dir), encoding='utf-8', errors='replace') as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return {'open': [], 'resolved': [], 'count': 0, 'unreadable': None}
    refs, acks, bad_open, n_bad = [], [], False, 0
    for ln in lines:
        try:
            o = json.loads(ln)
        except ValueError:
            o = None
        if not isinstance(o, dict) or o.get('kind') not in ('refused', 'ack') or not isinstance(o.get('at'), str) or not o.get('id'):
            n_bad += 1
            bad_open = True
            continue
        if o['kind'] == 'ack':
            acks.append(o)
            if o.get('ref') == 'all':
                bad_open = False                              # an acknowledgement of everything, written after the last unreadable line, deals with it
        else:
            refs.append(o)
    out_open, out_res = [], []
    manual = [e for e in entries if not no_person(e) and not e.get('auto') and not e.get('backfillKey')]
    for r in refs:
        req = r.get('request') if isinstance(r.get('request'), dict) else {}
        lvl = str(req.get('level') or '').strip().lower()
        item = {'id': r['id'], 'at': r['at'], 'reason': r.get('reason') or '', 'reasonKind': r.get('reasonKind'), 'level': req.get('level'), 'hazard': req.get('hazard'),
                'levelValid': lvl in LEVELS, 'pendingHazards': _pending_hazards(req), 'via': r.get('via')}
        if isinstance(r.get('resolved'), dict):
            item['resolvedBy'] = r['resolved'].get('entry') or 'duplicate'
            out_res.append(item)
            continue
        if page_message_none({'level': lvl, 'text': req.get('text')}):      # a request that rests on the page message was no reading, so its refusal holds nothing back. The later page none that used to clear it no longer clears anything
            item['resolvedBy'] = 'page message, not a reading'
            out_res.append(item)
            continue
        by = next((a for a in acks if a['at'] >= r['at'] and a.get('ref') in (r['id'], 'all')), None)
        if by:
            item['resolvedBy'] = by['id']
            out_res.append(item)
            continue
        if lvl in LEVELS:
            toks = [t for t in parse_hazards(req.get('hazard')) if t in HAZARDS]
            clr = None
            for e in manual:
                if e['level'] != lvl or (e.get('logged') or e['read']) < r['at']:
                    continue
                if toks and not (set(toks) & set(e['hazard'])):
                    continue
                clr = e
                break
            if clr:
                item['resolvedBy'] = clr['id']
                out_res.append(item)
                continue
        item['text'] = refused_text(item)
        out_open.append(item)
    return {'open': out_open, 'resolved': out_res, 'count': len(refs), 'unreadable': ({'lines': n_bad, 'open': bad_open} if n_bad else None)}


def refused_text(item):
    """the sentence for the card, the alerts and the NCM block: what was typed, why it was refused, when, what it does to the levels and what clears it"""
    lvl = item.get('level') or 'no level'
    hz = item.get('hazard') or 'no hazard'
    pend = item.get('pendingHazards') or []
    effect = (('The %s level(s) read unknown, not none, until it is dealt with. ' % ', '.join(pend)) if pend else 'It changes no level. ')
    clear = ('a valid reading for %s is logged (ncm_log.py add), or' % lvl) if item.get('levelValid') else 'a person acknowledges it, because the level typed is not one of none, yellow, orange or red, or'
    return ('An NCM reading was typed and refused: %s for %s, at %s. Reason: %s. It is not in the log. %sThe alert stays until %s the refusal is acknowledged (python ncm_log.py clear-refused).'
            % (lvl, hz, item.get('at'), public_reason(item.get('reason')), effect, clear))


def unreadable_refused_text(u):
    return ('%d line(s) of ncm_refused.jsonl cannot be read and may hold an NCM reading that was typed and refused: the alert stays until a person acknowledges it (python ncm_log.py clear-refused)' % u['lines'])


# ------------------------------------------------------------------------------------------------ reading the log
def window(e):
    """(start, end) of the time an entry's colour is in force, as naive local datetimes"""
    read = parse_local(e['read'])
    start = parse_local(e['from']) if e.get('from') else read
    if e.get('to'):
        end = parse_local(e['to'])
    else:
        end = read + dt.timedelta(hours=NO_END_HOURS)
    return start, end


def _ended_for(entries, e, hz, t):
    """True when a later manual reading of none that names the hazard hz has ended the colour entry e at time t. A none for rain does not end the fog
    half of an entry that holds both (audit D3); an automatic (by engine) none, or one that rests on the page message, never ends anything."""
    read = parse_local(e['read'])
    for o in entries:
        if o['level'] != 'none' or o.get('kind') != 'warning' or no_person(o):
            continue
        if o['read'] <= e['read'] or parse_local(o['read']) > t:
            continue
        if hz in o['hazard'] and parse_local(o['read']) > read:
            return True
    return False


def effective_end(entries, e, hz):
    """the end of the colour of entry e for the hazard hz: the end of its window, or the read time of the first later manual none that names hz"""
    start, end = window(e)
    read = parse_local(e['read'])
    for o in entries:
        if o['level'] != 'none' or o.get('kind') != 'warning' or no_person(o) or o['read'] <= e['read']:
            continue
        if hz in o['hazard']:
            ro = parse_local(o['read'])
            if read < ro < end:
                end = ro
    return end


def live_hazards(entries, e, t):
    """the hazards of the colour entry e that no later manual none has ended at time t"""
    return [hz for hz in e['hazard'] if not _ended_for(entries, e, hz, t)]


def in_force_at(entries, t, hazard=None):
    """Entries with a colour whose window holds t, and that no later manual reading of none has ended for the hazard asked about (for any of the entry's own
    hazards when none is asked about). Never hides an entry because an automatic (by engine) reading of none came after it."""
    out = []
    for e in entries:
        if e['level'] == 'none':
            continue
        if hazard and hazard not in e['hazard']:
            continue
        start, end = window(e)
        if not (start <= t <= end):
            continue
        hzs = [hazard] if hazard else e['hazard']
        if any(not _ended_for(entries, e, hz, t) for hz in hzs):
            out.append(e)
    return out


UPCOMING_HOURS = 24


def upcoming_at(entries, t, hazard=None, horizon_h=UPCOMING_HOURS):
    """Colour entries that were read at or before t, whose window opens after t (within horizon_h hours), and that no later manual none has ended. An NCM
    warning is issued ahead of its start so that people prepare: it is not silence until the first minute of its window (audit D2, D19). The old feed showed the
    level as soon as it was read."""
    out = []
    for e in entries:
        if e['level'] == 'none':
            continue
        if hazard and hazard not in e['hazard']:
            continue
        if parse_local(e['read']) > t:
            continue
        start, end = window(e)
        if not (start > t and end >= t):
            continue
        if hours_between(t, start) > horizon_h:
            continue
        hzs = [hazard] if hazard else e['hazard']
        if all(_ended_for(entries, e, hz, t) for hz in hzs):
            continue
        out.append(e)
    return out


def level_for(entries, t, hazard, require_rak=True, include_upcoming=False):
    """highest colour in force at t for a hazard (with include_upcoming, also the colours issued and starting within UPCOMING_HOURS). An entry that names
    Ras Al Khaimah or does not say counts; one that says it does not name it does not."""
    best = 'none'
    which = []
    pool = list(in_force_at(entries, t, hazard))
    if include_upcoming:
        pool += upcoming_at(entries, t, hazard)
    for e in pool:
        if require_rak and e.get('rakNamed') == 'no':
            continue
        which.append(e['id'])
        if RANK[e['level']] > RANK[best]:
            best = e['level']
    return best, which


def latest_entry(entries, hazard=None, kind=None):
    best = None
    for e in entries:
        if hazard and hazard not in e['hazard']:
            continue
        if kind and e.get('kind') != kind:
            continue
        if best is None or (e['read'], e['id']) > (best['read'], best['id']):
            best = e
    return best


def summarize(entries, now):
    """What weather.json and the engine need from the log at time `now`. The manual reading age counts readings by a person (or the Barq backfill);
    an automatic page read (by engine) is reported apart and does not refresh it, because an empty page is not proof that the NCM service answered."""
    last = latest_entry(entries)
    warn_any = latest_entry(entries, kind='warning')
    manual = [e for e in entries if e.get('kind') == 'warning' and not no_person(e)]
    auto = [e for e in entries if e.get('kind') == 'warning' and no_person(e)]
    last_warn = latest_entry(manual) if manual else None
    last_auto = latest_entry(auto) if auto else None
    levels, levels_now, ids = {}, {}, {}
    for h in ('rain', 'fog', 'dust'):
        lv, which = level_for(entries, now, h, require_rak=True, include_upcoming=True)
        levels[h] = lv
        ids[h] = which
        levels_now[h] = level_for(entries, now, h, require_rak=True)[0]
    in_force = in_force_at(entries, now)
    upcoming = upcoming_at(entries, now)
    rain_colour = [e for e in in_force_at(entries, now, 'rain') + upcoming_at(entries, now, 'rain') if e.get('rakNamed') != 'no']
    age = lambda e: round(hours_between(parse_local(e['read']), now), 1) if e else None
    return {'latest': last, 'latestWarningAny': warn_any, 'latestWarning': last_warn, 'latestAuto': last_auto, 'latestBulletin': latest_entry(entries, kind='bulletin'), 'ageHours': age(last),
            'warningsAgeHours': age(last_warn), 'autoAgeHours': age(last_auto), 'levels': levels, 'levelsInForce': levels_now, 'ids': ids, 'inForce': in_force, 'upcoming': upcoming,
            'rainColour': rain_colour, 'count': len(entries)}


def own_words(block):
    """True when the text of a block was written by the engine or the live reader, not typed or pasted by a person"""
    by = str((block or {}).get('by') or '').lower()
    return by.startswith('engine') or 'live reader' in by


def public_reading_text(block):
    """The sentence of a reading for a public file (decision of 30 Oct 2026: NCM's words are never in a public file). What a person logs can hold NCM's own sentences, and several entries do, so that text stays in the log and the
    public sentence is built from the facts. A sentence the engine or the live reader wrote is our own and stays. The lead of a pending colour or a refusal is the engine's own too; only what follows it is the reading."""
    n = block if isinstance(block, dict) else {}
    t = ' '.join(str(n.get('text') or '').split())
    if not n.get('asof'):
        return t                                        # no reading on record: the words are our own placeholder
    hz = n.get('hazard')
    hz = ', '.join(hz) if isinstance(hz, (list, tuple)) else str(hz or '')
    level = n.get('levelAtReading') or n.get('level') or 'level not read'
    facts = 'NCM reading of %s: %s%s.' % (n['asof'], level, (' for ' + hz) if hz else '')
    lead, sep, tail = t.partition('Last logged reading:')
    if sep:
        return '%s Last logged reading: %s' % (lead.strip(), tail.strip() if own_words(n) else facts)
    return t if own_words(n) else facts


def public_block(block):
    """The ncm block for what leaves this laptop in the console: the claude.ai copy can be read by anyone with the link, so the same rule as a public file holds (decision of 30 Oct 2026). The sentence is built from the facts, and the areas, which are NCM's own words, stay in the log. A sentence of the engine or the live reader is our own and stays."""
    n = dict(block or {})
    n['text'] = public_reading_text(n)
    if not own_words(n):
        n.pop('areas', None)
    elif n.get('areas') == OLD_BULLETIN_AREAS:
        n['areas'] = BULLETIN_AREAS
    return n


def public_log_row(entry):
    """(text, areas) of one line of the log for the console, by the rule of public_block. The areas are empty when they could be NCM's words; the board shows NCM's text behind the code."""
    e = entry if isinstance(entry, dict) else {}
    own = own_words(e)
    if not own and not e.get('read'):
        return '', ''
    areas = e.get('areas') if own else ''
    return public_reading_text({'asof': e.get('read'), 'level': e.get('level'), 'hazard': e.get('hazard'), 'by': e.get('by'), 'text': e.get('text')}), (BULLETIN_AREAS if areas == OLD_BULLETIN_AREAS else areas)


def ncm_block(summary, now):
    """The weather.json key ncm. level keeps the console's old meaning, the rain colour in force (none, yellow, orange, red), unknown when no reading is on
    record. Text, source and asof come from the latest warning reading of any hazard, by a person or by the engine's page read (by says which); the 5 day
    bulletin is kept apart in 'bulletin' so a bulletin never makes an old warning reading look fresh. The other hazards are in levels."""
    last = summary['latestWarningAny'] or summary['latest']
    shown_why = 'the latest warning reading'
    inf = summary.get('rainColour')
    if inf is None:
        inf = [e for e in summary['inForce'] if 'rain' in e['hazard'] and e.get('rakNamed') != 'no']
    if inf:
        # a colour is in force, or issued and about to start: the text, source and time shown are the strongest rain colour's, so the words never say no warning
        # while the level says orange, and an orange warning that starts in 4 hours is not shown as none (audit D2)
        last = max(inf, key=lambda e: (RANK[e['level']], e['read'], e['id']))
        in_now = any(x['id'] == last['id'] for x in summary['inForce'])
        shown_why = 'the strongest rain colour in force' if in_now else 'the strongest rain colour issued and not yet in force (it starts within %d hours)' % UPCOMING_HOURS
    if not last:
        # the short form carries every key of the full form, so a reader never meets a missing one (audit V10)
        return {'level': 'unknown', 'levelInForce': 'unknown', 'text': 'No NCM reading recorded yet', 'source': 'https://www.ncm.gov.ae', 'asof': None, 'id': None, 'ageHours': None,
                'areas': '', 'hazard': [], 'rakNamed': None, 'levels': {'rain': 'unknown', 'fog': 'unknown', 'dust': 'unknown'}, 'levelsInForce': {'rain': 'unknown', 'fog': 'unknown', 'dust': 'unknown'},
                'inForce': [], 'upcoming': [], 'stale': True, 'by': None, 'auto': False, 'kind': None, 'warningsAgeHours': None, 'autoAgeHours': summary.get('autoAgeHours'), 'shown': 'no reading on record',
                'bulletin': None}
    stale = (summary['warningsAgeHours'] is None) or summary['warningsAgeHours'] > 24
    b = summary.get('latestBulletin')
    up = []
    for e in summary.get('upcoming') or []:
        s0, e0 = window(e)
        up.append({'id': e['id'], 'level': e['level'], 'hazard': e['hazard'], 'from': f_local(s0), 'to': f_local(e0) if e.get('to') else None, 'startsInHours': round(hours_between(now, s0), 1),
                   'rakNamed': e.get('rakNamed'), 'countsInLevel': e.get('rakNamed') != 'no'})
    return {'level': summary['levels']['rain'], 'levelInForce': summary['levelsInForce']['rain'], 'text': short_text(last['text'], 600), 'source': short_text(last['source'], 300), 'asof': last['read'], 'id': last['id'],
            'ageHours': round(hours_between(parse_local(last['read']), now), 1), 'areas': short_text(last.get('areas', ''), 200), 'hazard': last['hazard'], 'rakNamed': last.get('rakNamed'),
            'levels': summary['levels'], 'levelsInForce': summary['levelsInForce'], 'inForce': [e['id'] for e in summary['inForce']], 'upcoming': up, 'stale': stale, 'by': last.get('by'), 'auto': page_message_none(last),
            'kind': last.get('kind'), 'warningsAgeHours': summary['warningsAgeHours'], 'autoAgeHours': summary['autoAgeHours'], 'shown': shown_why,
            'bulletin': ({'id': b['id'], 'asof': b['read'], 'text': b['text'], 'source': b['source']} if b else None)}


# ------------------------------------------------------------------------------------------------ the Barq backfill
# The three readings written in BARQ/log.md. Each is checked against anchor text in the log before it is written; if the anchor is gone the
# reading is refused, not guessed. The wording is Barq's record of what the NCM page said, not the NCM text.
BARQ_READINGS = [
    {'key': 'barq:2026-10-10T08:03', 'anchor': 'NCM read 08:03 from the mobile page (no rain warning for RAK, fog warning 02:00 to 09:00)',
     'read': '2026-10-10 08:03', 'level': 'none', 'hazard': 'fog', 'areas': 'some coastal and internal areas, mainly the west', 'rak': 'no',
     'from': '2026-10-10 02:00', 'to': '2026-10-10 09:00', 'issued': '2026-10-09 23:48',
     'text': 'No rain warning for Ras Al Khaimah. One fog and poor visibility warning in force, issued 09 Oct 2026 23:48, valid 02:00 to 09:00 on 10 Oct 2026; the page showed no colour and did not name Ras Al Khaimah.'},
    {'key': 'barq:2026-10-17T06:28', 'anchor': 'NCM read 06:28 from the warnings map (fog warnings only, 17 Oct 01:35, RAK not named',
     'read': '2026-10-17 06:28', 'level': 'none', 'hazard': 'fog', 'areas': 'not recorded', 'rak': 'no', 'from': '2026-10-17 01:35', 'to': None, 'issued': '2026-10-17 01:35',
     'text': 'Fog warnings only, issued 17 Oct 2026 01:35; no colour recorded; Ras Al Khaimah not named. End of the validity window not recorded.'},
    {'key': 'barq:2026-10-19T14:44', 'anchor': 'NCM read 14:44 in the browser: warning text issued 19 Oct 10:50',
     'read': '2026-10-19 14:44', 'level': 'none', 'hazard': 'rain,wind,dust', 'areas': 'some eastern and southern areas', 'rak': 'no',
     'from': '2026-10-19 14:00', 'to': '2026-10-19 21:00', 'issued': '2026-10-19 10:50',
     'text': 'Warning text issued 19 Oct 2026 10:50: chance of convective cloud with rainfall, fresh to strong winds and blowing dust at up to 50 km/h over some eastern and southern areas, 14:00 to 21:00. The map showed no warning and no colour; Ras Al Khaimah not named. The weather.json stamp of this reading was 19 Oct 14:49.'},
]


def import_barq(data_dir, barq_log=None, now=None, dry=False):
    """Backfill the readings of BARQ/log.md once. Returns a list of (key, 'written NCM-id' | 'already in the log' | 'refused: reason')."""
    now = now or now_local()
    path = barq_log or DEFAULT_BARQ_LOG
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        return [('all', 'refused: the Barq log cannot be read (%s)' % e)]
    text = ' '.join(text.split())
    results = []
    for r in BARQ_READINGS:
        entries, _ = load(data_dir)
        if any(x.get('backfillKey') == r['key'] for x in entries):
            results.append((r['key'], 'already in the log'))
            continue
        if ' '.join(r['anchor'].split()) not in text:
            results.append((r['key'], 'refused: the anchor text is not in the Barq log any more'))
            continue
        try:
            e = add_entry(data_dir, now=now, write=not dry, level=r['level'], hazard=r['hazard'], text=r['text'], source='https://www.ncm.gov.ae', areas=r['areas'], rak_named=r['rak'],
                          t_from=r['from'], t_to=r['to'], by='Barq, backfilled from the Barq log', read=r['read'], kind='warning', issued=r['issued'],
                          extra={'backfillKey': r['key'], 'note': 'backfilled from the Barq log (BARQ/log.md); colour not recorded by Barq, level none means no colour on the map'})
        except (ValueError, OSError) as ex:
            results.append((r['key'], 'refused: %s' % ex))
            continue
        results.append((r['key'], ('would write ' if dry else 'written ') + e['id']))
    return results


# ------------------------------------------------------------------------------------------------ CLI
def _fmt(e):
    return '%s  read %s  %-6s %-14s %s%s  | %s' % (e['id'], e['read'], e['level'], ','.join(e['hazard']),
                                                 ('%s to %s' % (e.get('from') or '?', e.get('to') or 'end not given')), '' if e.get('rakNamed') != 'no' else ' (RAK not named)', e['text'][:110])


def main(argv=None):
    ap = argparse.ArgumentParser(description='NCM reading log (append only)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('add', 'list', 'latest', 'inforce', 'import-barq', 'refused', 'clear-refused'):
        p = sub.add_parser(name)
        p.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
        p.add_argument('--json', action='store_true')
        p.add_argument('--now', default=None, help='override the clock (tests)')
        if name == 'add':
            p.add_argument('--level', required=True)
            p.add_argument('--hazard', required=True)
            p.add_argument('--areas', default='')
            p.add_argument('--rak-named', default='unknown')
            p.add_argument('--from', dest='t_from', default=None)
            p.add_argument('--to', dest='t_to', default=None)
            p.add_argument('--text', required=True)
            p.add_argument('--source', default=None)
            p.add_argument('--by', default='manual')
            p.add_argument('--read', default=None)
            p.add_argument('--kind', default='warning')
            p.add_argument('--issued', default=None)
        if name in ('list', 'latest'):
            p.add_argument('--hazard', default=None)
            if name == 'list':
                p.add_argument('--since', default=None)
        if name == 'inforce':
            p.add_argument('--at', default=None)
        if name == 'import-barq':
            p.add_argument('--barq-log', default=None)
            p.add_argument('--dry-run', action='store_true')
        if name == 'clear-refused':
            p.add_argument('--id', default=None, help='acknowledge this one refused request (default: every open one)')
            p.add_argument('--note', default=None)
    a = ap.parse_args(argv)
    now = parse_local(a.now) if a.now else now_local()
    if a.cmd == 'add':
        e, line, msg = try_add(a.data_dir, 'ncm_log.py add', now=now, level=a.level, hazard=a.hazard, text=a.text, source=a.source, areas=a.areas, rak_named=a.rak_named,
                               t_from=a.t_from, t_to=a.t_to, by=a.by, read=a.read, kind=a.kind, issued=a.issued)
        if e is None:
            print('REFUSED: %s' % msg, file=sys.stderr)
            if line:
                print('The refused request is kept (%s in ncm_refused.jsonl). %s' % (line['id'], 'Nothing was lost: the identical entry is in the log.' if line.get('resolved') else
                      'It stays a high alert in weather.json until a valid reading for the same level is logged, or: python ncm_log.py clear-refused'), file=sys.stderr)
            return 2
        print(json.dumps(e, ensure_ascii=False) if a.json else 'written ' + _fmt(e))
        return 0
    if a.cmd in ('refused', 'clear-refused'):
        if a.cmd == 'clear-refused':
            ids = acknowledge_refused(a.data_dir, a.id, now=now, note=a.note)
            print('acknowledged: %s' % (', '.join(ids) if ids else 'nothing was open'))
            return 0
        st = refused_status(a.data_dir, load(a.data_dir)[0], now)
        if a.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            for x in st['open']:
                print('OPEN  %s' % x['text'])
            print('%d open, %d dealt with, %d refused in all%s' % (len(st['open']), len(st['resolved']), st['count'], (', %d unreadable line(s)' % st['unreadable']['lines']) if st['unreadable'] else ''))
        return 0
    entries, bad = load(a.data_dir)
    if a.cmd == 'list':
        rows = [e for e in entries if (not a.hazard or a.hazard in e['hazard']) and (not a.since or e['read'] >= f_local(parse_local(a.since)))]
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for e in rows:
                print(_fmt(e))
            print('%d entries%s' % (len(rows), (', %d unreadable lines skipped' % bad) if bad else ''))
        return 0
    if a.cmd == 'latest':
        e = latest_entry(entries, a.hazard)
        if not e:
            print('no entry' + (' for ' + a.hazard if a.hazard else ''))
            return 1
        print(json.dumps(e, ensure_ascii=False) if a.json else _fmt(e) + '\n  age %.1f h' % hours_between(parse_local(e['read']), now))
        return 0
    if a.cmd == 'inforce':
        t = parse_local(a.at) if a.at else now
        rows = in_force_at(entries, t)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for e in rows:
                print(_fmt(e))
            print('%d in force at %s' % (len(rows), f_local(t)))
        return 0
    if a.cmd == 'import-barq':
        for k, r in import_barq(a.data_dir, a.barq_log, now=now, dry=a.dry_run):
            print('%s: %s' % (k, r))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
