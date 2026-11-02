# -*- coding: utf-8 -*-
"""WM004 weather channel: the outbox, the log, acknowledgement, hold, approval and escalation (25 Oct 2026).

weather_channel.py decides what the weather asks for and writes a notification record into data/weather/outbox/ for every new or changed trigger. This module is the other half: the
records, the append only log, and what people do with a record.

    python channel_ack.py list [--all]                    open notifications with their timers (the console shows the same)
    python channel_ack.py show NOTIF-20261025-0100-01
    python channel_ack.py ack NOTIF-id --by WEATHER-LEAD   record the acknowledgement, with the time, in channel_log.jsonl
    python channel_ack.py hold NOTIF-id --by ROLE [--minutes 60]   pause the escalation of one record
    python channel_ack.py approve NOTIF-id                 the desk owner approves a draft for a tier above T0: it is queued and sent through the transports that are set
    python channel_ack.py reject NOTIF-id [--note TEXT]    the desk owner rejects a draft: it is closed and nothing is sent
    python channel_ack.py sent NOTIF-id                    the desk owner forwarded a queued record by hand
    python channel_ack.py escalate                         raise every record that is unacknowledged past its time by one tier (the channel does this on every run)

A role is one of the role ids of channel_rules.json. No name, number or address is ever written here: a record holds roles and counts. A read never creates a file.
Escalation: an unacknowledged record past its time rises one tier (T1 to T2 to T3), once per step, and the step is recorded; at T3 it stays and is marked as unacknowledged at the top tier.
Standard library only."""
import os, sys, json, re, time, argparse, datetime as dt, contextlib
import wx_util as U
from wx_util import (HERE, DEFAULT_DATA_DIR, now_local, parse_local, f_local, hours_between, read_json_status, read_json, atomic_write_json, append_durable, file_lock, read_jsonl, short_text)

TIERS = ('T0', 'T1', 'T2', 'T3')
STATUSES = ('draft', 'queued', 'sent', 'acknowledged', 'expired')
CLOSED = ('acknowledged', 'expired')
RULES_NAME, STATE_NAME, LOG_NAME, OUTBOX = 'channel_rules.json', 'channel_state.json', 'channel_log.jsonl', 'outbox'
LOCAL_PATH = os.path.join(HERE, 'channel_local.json')
CONTACTS_PATH = os.path.join(HERE, 'contacts.local.json')
MAX_RECORD_AGE_H = 72
DEFAULT_EXPIRE_AFTER_CLEAR_H = 12
DEFAULT_ACK_MIN = {'T1': 30, 'T2': 20, 'T3': 15}


# ------------------------------------------------------------------------------------------------ files
def outbox_dir(data_dir):
    return os.path.join(data_dir, OUTBOX)


def log_path(data_dir):
    return os.path.join(data_dir, LOG_NAME)


def state_path(data_dir):
    return os.path.join(data_dir, STATE_NAME)


def record_path(data_dir, nid):
    return os.path.join(outbox_dir(data_dir), nid + '.json')


ID_RE = re.compile(r'^NOTIF-\d{8}-\d{4}-\d{2,}$')


def id_time(nid):
    """the minute a record was made, from its id NOTIF-YYYYMMDD-HHMM-NN (a record that does not read still has its name), or None"""
    m = re.match(r'^NOTIF-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-\d{2,}$', nid or '')
    if not m:
        return None
    try:
        return dt.datetime(*[int(x) for x in m.groups()])
    except ValueError:
        return None


def load_records(data_dir):
    """Read only: every readable NOTIF-*.json of the outbox, oldest id first. A file that does not read is skipped (the health check counts it)."""
    out = []
    try:
        names = sorted(n for n in os.listdir(outbox_dir(data_dir)) if n.startswith('NOTIF-') and n.endswith('.json'))
    except OSError:
        return out
    for n in names:
        d = read_json(os.path.join(outbox_dir(data_dir), n), None)
        if isinstance(d, dict) and d.get('id'):
            out.append(d)
    return out


def unreadable_records(data_dir):
    try:
        names = [n for n in os.listdir(outbox_dir(data_dir)) if n.startswith('NOTIF-') and n.endswith('.json')]
    except OSError:
        return 0
    return sum(1 for n in names if not isinstance(read_json(os.path.join(outbox_dir(data_dir), n), None), dict))


INDEX_DIR = 'index'
INDEX_SAVE_EVERY = 1000                                  # closed records read between two saves of the index of a month


def _summary(rec, m, z):
    """what a scan needs of a record that is closed: enough for the season record and the counts, and the size and time of the file it was made from"""
    return {'id': rec['id'], 'rule': rec.get('rule'), 'tier': rec.get('tier'), 'baseTier': rec.get('baseTier'), 'status': rec.get('status'), 'created': rec.get('created'), 'drill': bool(rec.get('drill')),
            'ackMinutes': rec.get('ackMinutes'), 'm': m, 'z': z}


def scan_outbox(data_dir, write_index=True):
    """One pass over the outbox that does not read what it does not need (25 Oct 2026, the stress test: every run read every record of the outbox about ten times, so a run took longer with every
    notification ever made: 4 s at 400 records, minutes at 20,000). Returns {'open': the full records that are not closed, oldest id first, 'summaries': one small dict for every readable record (open
    or closed), oldest id first, 'unreadable': files that did not read, 'files': NOTIF files, 'read': files read in full}.
    A closed record never changes, so its summary is kept in outbox/index/YYYYMM.json with the size and time of the file it came from; a file that still has that size and time is not opened. Every
    open record is read in full, because the channel acts on it. The index is a cache made by this function and nothing else reads it: a missing or damaged index is made again from the records, a
    record that is not in it is read, and one whose file changed is read again. A folder that cannot be written leaves the index as it was."""
    folder = outbox_dir(data_dir)
    try:
        listing = list(os.scandir(folder))
    except OSError:
        return {'open': [], 'summaries': [], 'unreadable': 0, 'files': 0, 'read': 0}
    entries = [e for e in listing if e.name.startswith('NOTIF-') and e.name.endswith('.json')]
    if write_index:                                          # a temp file that a killed run left behind (an hour old at least) is removed, as the engine does in its own folder (stress test T5)
        t_now = time.time()
        for e in listing:
            if e.name.startswith('.tmp_') and e.name.endswith('.part'):
                try:
                    if t_now - e.stat().st_mtime > 3600:
                        os.remove(e.path)
                except OSError:
                    pass
    by_month = {}
    for e in entries:
        by_month.setdefault(e.name[6:12], []).append(e)
    summaries, open_, unreadable, nread = [], [], 0, 0
    for mk, es in by_month.items():
        ip = os.path.join(folder, INDEX_DIR, mk + '.json')
        idx = read_json(ip, None)
        old = idx.get('r') if isinstance(idx, dict) and isinstance(idx.get('r'), dict) else {}
        new, changed = {}, not isinstance(idx, dict)
        month_reads = 0
        for e in es:
            nid = e.name[:-5]
            try:
                st = e.stat()
                m, z = st.st_mtime_ns, st.st_size
            except OSError:
                unreadable += 1
                continue
            s = old.get(nid)
            if isinstance(s, dict) and s.get('m') == m and s.get('z') == z and s.get('status') in CLOSED and s.get('id') == nid:
                new[nid] = s
                summaries.append(s)
                continue
            rec = read_json(e.path, None)
            nread += 1
            month_reads += 1
            if not (isinstance(rec, dict) and rec.get('id')):
                unreadable += 1
                continue
            s = _summary(rec, m, z)
            summaries.append(s)
            if rec.get('status') in CLOSED:
                new[nid] = s
            else:
                open_.append(rec)
            if write_index and month_reads % INDEX_SAVE_EVERY == 0:
                # a long first pass (an index that is lost, an outbox of thousands) keeps what it has read so far: a run that is stopped (the engine stops the channel after 90 s) does not lose the work, and the
                # next run goes on from there (stress test T6). The entries of the month not reached yet stay as they were.
                try:
                    os.makedirs(os.path.dirname(ip), exist_ok=True)
                    atomic_write_json(ip, {'schema': 1, 'note': 'a cache of closed records made by channel_ack.scan_outbox; delete it and it is made again', 'r': dict(old, **new)})
                except OSError:
                    pass
        if set(old) != set(new) or any(old.get(k) != v for k, v in new.items()):
            changed = True
        if write_index and changed:
            try:
                os.makedirs(os.path.dirname(ip), exist_ok=True)
                atomic_write_json(ip, {'schema': 1, 'note': 'a cache of closed records made by channel_ack.scan_outbox; delete it and it is made again', 'r': new})
            except OSError:
                pass
    summaries.sort(key=lambda x: x['id'])
    open_.sort(key=lambda x: x['id'])
    return {'open': open_, 'summaries': summaries, 'unreadable': unreadable, 'files': len(entries), 'read': nread}


def load_record(data_dir, nid):
    d = read_json(record_path(data_dir, nid), None)
    return d if isinstance(d, dict) else None


def save_record(data_dir, rec):
    atomic_write_json(record_path(data_dir, rec['id']), rec, indent=1)


@contextlib.contextmanager
def locked_record(data_dir, nid):
    os.makedirs(outbox_dir(data_dir), exist_ok=True)
    with file_lock(record_path(data_dir, nid)):
        yield


def create_record(data_dir, now, fields):
    """allocate the next id NOTIF-YYYYMMDD-HHMM-NN for this minute and write the record in one step under a lock, so that two runs in one minute never draw the same id"""
    os.makedirs(outbox_dir(data_dir), exist_ok=True)
    prefix = 'NOTIF-%s-%s-' % (now.strftime('%Y%m%d'), now.strftime('%H%M'))
    with file_lock(os.path.join(outbox_dir(data_dir), 'ids')):
        n = 0
        for name in os.listdir(outbox_dir(data_dir)):
            if name.startswith(prefix) and name.endswith('.json'):
                try:
                    n = max(n, int(name[len(prefix):-5]))
                except ValueError:
                    pass
        rec = dict(fields, id='%s%02d' % (prefix, n + 1))
        save_record(data_dir, rec)
    return rec


def read_log(data_dir):
    return read_jsonl(log_path(data_dir))


def append_log_many(data_dir, kind_fields, now, snap=None):
    """the lines of several state changes in one write and one fsync (kind_fields: [(kind, fields)]): the same lines append_log would write one at a time, contiguous in the file. A pass that escalates
    hundreds of records writes its lines in groups of ten (stress test T3: 25 ms a rising record, a third of it the fsync of its log line)"""
    out = []
    for kind, fields in kind_fields:
        line = {'t': f_local(now), 'kind': kind}
        line.update(fields)
        if snap is not None:
            line['snap'] = snap
        out.append(json.dumps(line, ensure_ascii=False, separators=(',', ':')))
    if out:
        append_durable(log_path(data_dir), chr(10).join(out))


def tail_log(data_dir, n):
    """(the last n readable lines of the log, oldest first, the number of lines in the file, lines that did not read among the n). Reads from the end of the file in blocks and stops when it has n lines, so the
    cost is that of n lines and not of the log (25 Oct 2026, the console bake: the log of a season is tens of thousands of lines). The count of lines is made by counting newlines in blocks, without parsing."""
    path = log_path(data_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0, 0
    total = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            total += chunk.count(b'\n')
        # the last line may have no newline (a torn one): it counts as a line
        if size:
            f.seek(size - 1)
            if f.read(1) != b'\n':
                total += 1
        want, pos, buf = n + 1, size, b''
        while pos > 0 and buf.count(b'\n') < want:
            step = min(1 << 16, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    lines = buf.split(b'\n')
    if pos > 0:
        lines = lines[1:]                                   # the first piece is the end of a line that began before the block
    lines = [x for x in lines if x.strip()][-n:]
    rows, bad = [], 0
    for x in lines:
        try:
            o = json.loads(x.decode('utf-8', 'replace'))
            if isinstance(o, dict):
                rows.append(o)
            else:
                bad += 1
        except ValueError:
            bad += 1
    return rows, total, bad


def append_log(data_dir, kind, now, snap=None, **fields):
    """one line per state change; append only, under the cross process lock. snap: the readiness snapshot counts. Returns the line."""
    line = {'t': f_local(now), 'kind': kind}
    line.update(fields)
    if snap is not None:
        line['snap'] = snap
    append_durable(log_path(data_dir), json.dumps(line, ensure_ascii=False, separators=(',', ':')))
    return line


# ------------------------------------------------------------------------------------------------ rules, local settings, contacts
def load_rules(data_dir, path=None):
    """(rules document with the usable rules in doc['rulesOk'], problems). A rule that is not in the shape the channel reads is skipped and named; a file that is missing or damaged is a problem
    and the channel says it is blind rather than running on nothing. Read only. A scratch folder without a rules file uses the one that ships with the channel."""
    problems = []
    cands = [path] if path else [os.path.join(data_dir, RULES_NAME), os.path.join(DEFAULT_DATA_DIR, RULES_NAME)]
    doc, st, used = None, 'missing', None
    for c in cands:
        doc, st = read_json_status(c)
        used = c
        if st == 'ok':
            break
    if st != 'ok' or not isinstance(doc, dict) or not isinstance(doc.get('rules'), list):
        return None, ['channel_rules.json is %s (owner: the weather desk): the channel has no rules' % ('missing' if st == 'missing' else 'damaged or not in the shape the channel reads')]
    roles = doc.get('roles') if isinstance(doc.get('roles'), dict) else {}
    tiers = doc.get('tiers') if isinstance(doc.get('tiers'), dict) else {}
    ok, seen = [], set()
    for r in doc['rules']:
        rid = r.get('id') if isinstance(r, dict) else None
        why = None
        if not isinstance(rid, str) or not rid:
            why = 'a rule has no id'
        elif rid in seen:
            why = 'duplicate id'
        elif r.get('kind') == 'rung-change':
            pass
        elif not isinstance(r.get('when'), dict):
            why = 'no condition'
        elif r.get('rung') not in (0, 1, 2, 3):
            why = 'rung is not 0 to 3'
        elif r.get('tier') not in TIERS:
            why = 'tier is not T0 to T3'
        elif not isinstance(r.get('audience'), list) or any(a not in roles for a in r['audience']):
            why = 'an audience role is not defined in roles'
        elif not isinstance(r.get('message'), dict) or not isinstance(r['message'].get('lines'), list) or len(r['message']['lines']) > 6:
            why = 'the message is missing or has more than 6 lines'
        if why:
            problems.append('%s: %s (skipped)' % (rid or '?', why))
            continue
        seen.add(rid)
        ok.append(r)
    doc = dict(doc)
    doc['rulesOk'] = ok
    doc['rulesSkipped'] = len(doc['rules']) - len(ok)
    doc['path'] = used
    doc['tiersOk'] = {k: v for k, v in tiers.items() if k in TIERS}
    return doc, problems


def load_local(path=None):
    """channel_local.json (autoSend by tier, the toast switch, the desk home). It does not exist until the desk owner makes it; a missing or damaged file means every default: nothing is sent
    beyond the desk owner. Read only."""
    d = read_json(path or LOCAL_PATH, None)
    return d if isinstance(d, dict) else {}


def auto_send(local, tier):
    a = local.get('autoSend') if isinstance(local, dict) else None
    return bool(isinstance(a, dict) and a.get(tier) is True)


def load_contacts(path=None):
    """contacts.local.json: role id to {'email': [...], 'sms': [...]}. Never committed; the channel never copies a value out of it into any file. {} when there is none."""
    d = read_json(path or CONTACTS_PATH, None)
    roles = d.get('roles') if isinstance(d, dict) else None
    return roles if isinstance(roles, dict) else {}


def role_has_contact(contacts, role):
    c = contacts.get(role) if isinstance(contacts, dict) else None
    return bool(isinstance(c, dict) and any(isinstance(v, list) and any(isinstance(x, str) and x.strip() for x in v) for k, v in c.items() if not str(k).startswith('_')))


# ------------------------------------------------------------------------------------------------ records: small helpers
def ack_minutes_for(rules_doc, rule_id, tier):
    """minutes an unacknowledged record of this rule waits at this tier before it rises; None for T0 (the desk owner, automatic)"""
    if tier == 'T0':
        return None
    for r in (rules_doc or {}).get('rulesOk', []):
        if r['id'] == rule_id and tier == r.get('tier') and r.get('ackMin') is not None:
            return r['ackMin']
    t = ((rules_doc or {}).get('tiersOk') or {}).get(tier) or {}
    return t.get('ackMin') if t.get('ackMin') is not None else DEFAULT_ACK_MIN.get(tier)


def next_tier(rules_doc, tier):
    t = ((rules_doc or {}).get('tiersOk') or {}).get(tier) or {}
    n = t.get('next')
    return n if n in TIERS else None


def tier_roles(rules_doc, tier):
    t = ((rules_doc or {}).get('tiersOk') or {}).get(tier) or {}
    return list(t.get('roles') or [])


def minutes_left(rec, now):
    due = rec.get('ackDueAt')
    if not due:
        return None
    try:
        return int(round((parse_local(due) - now).total_seconds() / 60.0))
    except (ValueError, TypeError):
        return None


def snapshot_of(state):
    """the counts that go on every line of the log: the readiness snapshot (rung, rules on, open notifications, unacknowledged, actions due, actions late)"""
    st = state if isinstance(state, dict) else {}
    lad = st.get('ladder') or {}
    return {'rung': lad.get('rung'), 'rulesOn': len([1 for v in (st.get('rules') or {}).values() if isinstance(v, dict) and v.get('on')]),
            'open': (st.get('notifications') or {}).get('open'), 'unacknowledged': (st.get('notifications') or {}).get('unacknowledged'),
            'actionsDue': ((st.get('pack') or {}).get('counts') or {}).get('due'), 'actionsLate': ((st.get('pack') or {}).get('counts') or {}).get('late')}


def _snap(data_dir):
    return snapshot_of(read_json(state_path(data_dir), {}))


# ------------------------------------------------------------------------------------------------ acknowledgement, hold, approval
def ack(data_dir, nid, by, now, rules_doc=None, via='cli'):
    """record the acknowledgement of one record by a role. The role must be in the audience of the record (or be the desk owner). Returns {'ok', 'error' or 'record'}."""
    if rules_doc is None:
        rules_doc, _ = load_rules(data_dir)
    roles = (rules_doc or {}).get('roles') or {}
    if by not in roles:
        return {'ok': False, 'error': 'unknown role %s; the roles are %s' % (by, ', '.join(sorted(roles)))}
    if not ID_RE.match(nid or ''):
        return {'ok': False, 'error': 'not a notification id: %s' % nid}
    with locked_record(data_dir, nid):
        rec = load_record(data_dir, nid)
        if not rec:
            return {'ok': False, 'error': 'no notification %s' % nid}
        if rec.get('status') == 'acknowledged':
            return {'ok': True, 'already': True, 'record': rec}
        if rec.get('status') == 'expired':
            return {'ok': False, 'error': '%s has expired and cannot be acknowledged; the rule raises a new one if the trigger is still on' % nid}
        if by != 'DESK-OWNER' and by not in (rec.get('audience') or []):
            return {'ok': False, 'error': 'role %s is not in the audience of %s (%s)' % (by, nid, ', '.join(rec.get('audience') or []))}
        rec['ack'] = {'by': by, 'at': f_local(now), 'via': via}
        rec['status'] = 'acknowledged'
        rec['ackDueAt'] = None
        try:
            rec['ackMinutes'] = round((now - parse_local(rec['created'])).total_seconds() / 60.0, 1)
        except (ValueError, TypeError, KeyError):
            rec['ackMinutes'] = None
        rec['updated'] = f_local(now)
        save_record(data_dir, rec)
    append_log(data_dir, 'notification_acknowledged', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), tier=rec.get('tier'), by=by, via=via, minutes=rec.get('ackMinutes'), drill=bool(rec.get('drill')))
    return {'ok': True, 'record': rec}


def hold(data_dir, nid, by, now, minutes=60, rules_doc=None):
    """pause the escalation of one record for `minutes`: somebody has it in hand and says so"""
    if rules_doc is None:
        rules_doc, _ = load_rules(data_dir)
    roles = (rules_doc or {}).get('roles') or {}
    if by not in roles:
        return {'ok': False, 'error': 'unknown role %s' % by}
    minutes = max(1, min(int(minutes), 24 * 60))
    with locked_record(data_dir, nid):
        rec = load_record(data_dir, nid)
        if not rec:
            return {'ok': False, 'error': 'no notification %s' % nid}
        if rec.get('status') in CLOSED:
            return {'ok': False, 'error': '%s is %s' % (nid, rec['status'])}
        until = now + dt.timedelta(minutes=minutes)
        rec['hold'] = {'by': by, 'at': f_local(now), 'until': f_local(until)}
        if rec.get('ackDueAt'):
            try:
                if parse_local(rec['ackDueAt']) < until:
                    rec['ackDueAt'] = f_local(until)
            except (ValueError, TypeError):
                pass
        rec['updated'] = f_local(now)
        save_record(data_dir, rec)
    append_log(data_dir, 'notification_held', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), by=by, until=rec['hold']['until'], drill=bool(rec.get('drill')))
    return {'ok': True, 'record': rec}


def approve(data_dir, nid, now, rules_doc=None, local=None, contacts=None, by='DESK-OWNER'):
    """the desk owner approves a draft: its newest delivery is queued and the transports that are set are tried (never in drill mode)"""
    import weather_channel as C
    if rules_doc is None:
        rules_doc, _ = load_rules(data_dir)
    local = load_local() if local is None else local
    contacts = load_contacts() if contacts is None else contacts
    with locked_record(data_dir, nid):
        rec = load_record(data_dir, nid)
        if not rec:
            return {'ok': False, 'error': 'no notification %s' % nid}
        if rec.get('drill'):
            return {'ok': False, 'error': '%s is a DRILL record: it is never sent beyond the desk owner and the outbox' % nid}
        d = next((x for x in reversed(rec.get('deliveries') or []) if x.get('status') == 'draft'), None)
        if not d:
            return {'ok': False, 'error': '%s has no draft to approve (status %s)' % (nid, rec.get('status'))}
        d['status'] = 'queued'
        d['approvedAt'] = f_local(now)
        d['approvedBy'] = by
        rec['status'] = 'queued' if rec.get('status') == 'draft' else rec.get('status')
        C.dispatch_delivery(data_dir, rec, d, now, rules_doc, local, contacts)
        rec['updated'] = f_local(now)
        save_record(data_dir, rec)
    append_log(data_dir, 'notification_approved', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), tier=d.get('tier'), by=by, drill=bool(rec.get('drill')))
    if d.get('status') == 'sent':
        append_log(data_dir, 'notification_sent', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), tier=d.get('tier'), via=d.get('via'), drill=bool(rec.get('drill')))
    return {'ok': True, 'record': rec}


_PERSONAL = re.compile(r'(\+971|\b00971|\b05\d[ -]?\d{3}[ -]?\d{4}\b|[\w.+-]+@[\w-]+\.[\w.-]+)')


def scrub_note(note, n=120):
    """28 Oct 2026 (audit L5): the free text of a rejection note is written into the record and the channel log; a phone number or an e-mail address typed in it is removed, control characters are dropped, the length is cut"""
    t = ''.join(ch for ch in str(note or '') if ch.isprintable())
    t = _PERSONAL.sub('[removed]', t)
    return short_text(' '.join(t.split()), n)


def reject(data_dir, nid, now, note='', by='DESK-OWNER'):
    note = scrub_note(note)
    with locked_record(data_dir, nid):
        rec = load_record(data_dir, nid)
        if not rec:
            return {'ok': False, 'error': 'no notification %s' % nid}
        if rec.get('status') in CLOSED:
            return {'ok': False, 'error': '%s is %s' % (nid, rec['status'])}
        for d in rec.get('deliveries') or []:
            if d.get('status') in ('draft', 'queued'):
                d['status'] = 'rejected'
        rec['status'] = 'expired'
        rec['expiredReason'] = 'rejected by the desk owner' + ((': ' + short_text(note, 120)) if note else '')
        rec['ackDueAt'] = None
        rec['updated'] = f_local(now)
        save_record(data_dir, rec)
    append_log(data_dir, 'notification_expired', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), reason=rec['expiredReason'], drill=bool(rec.get('drill')))
    return {'ok': True, 'record': rec}


def mark_sent(data_dir, nid, now, by='DESK-OWNER'):
    """the desk owner forwarded a queued record by hand (no transport is set): it is sent, from now"""
    with locked_record(data_dir, nid):
        rec = load_record(data_dir, nid)
        if not rec:
            return {'ok': False, 'error': 'no notification %s' % nid}
        d = next((x for x in reversed(rec.get('deliveries') or []) if x.get('status') == 'queued'), None)
        if not d:
            return {'ok': False, 'error': '%s has nothing queued' % nid}
        d['status'] = 'sent'
        d['at'] = f_local(now)
        d['via'] = sorted(set((d.get('via') or []) + ['by hand']))
        rec['status'] = 'sent'
        rec['updated'] = f_local(now)
        save_record(data_dir, rec)
    append_log(data_dir, 'notification_sent', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), tier=d.get('tier'), via=d['via'], drill=bool(rec.get('drill')))
    return {'ok': True, 'record': rec}


# ------------------------------------------------------------------------------------------------ escalation and expiry
def escalate(data_dir, now, rules_doc=None, local=None, contacts=None, notify=None, records=None):
    """Raise by one tier every record that is not acknowledged, not held, not closed, and past its acknowledgement time. One step per record per call: a record that is two steps late
    rises once now and again at the next call (the next time is counted from this one). The step is written into the record and into the log. The new tier's message is a draft for the desk owner to
    approve, or is queued at once when autoSend is set for that tier (never in drill mode). notify(text) tells the desk owner (T0). Returns the list of events."""
    import weather_channel as C
    if rules_doc is None:
        rules_doc, _ = load_rules(data_dir)
    local = load_local() if local is None else local
    contacts = load_contacts() if contacts is None else contacts
    events = []
    snap = _snap(data_dir)                                   # the snapshot on the lines of the pass: read once, not for every line
    pending = []                                             # the log lines of the steps taken, written in groups of ten and at the end, even if the pass is cut short

    def log_line(kind, **fields):
        pending.append((kind, fields))
        if len(pending) >= 10:
            flush()

    def flush():
        if pending:
            batch = list(pending)
            del pending[:]
            append_log_many(data_dir, batch, now, snap=snap)
    try:
        return _escalate_pass(data_dir, now, rules_doc, local, contacts, notify, records, events, log_line, C)
    finally:
        flush()


def _escalate_pass(data_dir, now, rules_doc, local, contacts, notify, records, events, log_line, C):
    for rec0 in (records if records is not None else load_records(data_dir)):
        nid = rec0['id']
        if rec0.get('status') in CLOSED or not rec0.get('ackDueAt'):
            continue
        with locked_record(data_dir, nid):
            rec = load_record(data_dir, nid)
            if not rec or rec.get('status') in CLOSED or not rec.get('ackDueAt'):
                continue
            if rec.get('cleared'):
                continue                    # the trigger is off (25 Oct 2026): nothing is left to act on, so the record stops climbing; it stays open for an acknowledgement until it expires (the live run of that day escalated two cleared blind alerts to the Director)
            h = rec.get('hold')
            try:
                if h and parse_local(h['until']) > now:
                    continue
                if parse_local(rec['ackDueAt']) > now:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            cur = rec.get('tier')
            nt = next_tier(rules_doc, cur)
            if nt is None:
                rec['topTierUnacknowledged'] = f_local(now)
                rec['ackDueAt'] = None
                rec['updated'] = f_local(now)
                save_record(data_dir, rec)
                ev = {'kind': 'notification_unacknowledged_top', 'id': nid, 'rule': rec.get('rule'), 'tier': cur}
                events.append(ev)
                log_line(ev['kind'], id=nid, rule=rec.get('rule'), tier=cur, drill=bool(rec.get('drill')))
                if notify:
                    notify('%s%s is unacknowledged at the top tier (%s). Nobody above the Director is left to tell.' % ('DRILL: ' if rec.get('drill') else '', nid, cur))
                continue
            rec['tier'] = nt
            aud = list(rec.get('audience') or [])
            for r in tier_roles(rules_doc, nt):
                if r not in aud:
                    aud.append(r)
            rec['audience'] = aud
            rec.setdefault('escalations', []).append({'from': cur, 'to': nt, 'at': f_local(now), 'reason': 'not acknowledged within %s minutes' % (ack_minutes_for(rules_doc, rec.get('rule'), cur) or '?')})
            am = ack_minutes_for(rules_doc, rec.get('rule'), nt) or DEFAULT_ACK_MIN.get(nt) or 15
            rec['ackDueAt'] = f_local(now + dt.timedelta(minutes=am))
            d = {'tier': nt, 'status': 'draft', 'at': f_local(now), 'via': [], 'reason': 'escalation'}
            rec.setdefault('deliveries', []).append(d)
            if not rec.get('drill') and auto_send(local, nt):
                d['status'] = 'queued'
                C.dispatch_delivery(data_dir, rec, d, now, rules_doc, local, contacts)
            if d['status'] == 'sent':
                rec['status'] = 'sent'
            elif d['status'] == 'queued' and rec.get('status') == 'draft':
                rec['status'] = 'queued'
            rec['updated'] = f_local(now)
            save_record(data_dir, rec)
            ev = {'kind': 'notification_escalated', 'id': nid, 'rule': rec.get('rule'), 'from': cur, 'to': nt}
            events.append(ev)
        log_line('notification_escalated', id=nid, rule=rec.get('rule'), **{'from': cur, 'to': nt, 'drill': bool(rec.get('drill'))})
        if d.get('status') == 'sent':
            log_line('notification_sent', id=nid, rule=rec.get('rule'), tier=nt, via=d.get('via'), drill=bool(rec.get('drill')))
        if notify:
            notify('%s%s was not acknowledged and rose from %s to %s%s.' % ('DRILL: ' if rec.get('drill') else '', nid, cur, nt, '' if d['status'] != 'draft' else ' (a draft for you to approve)'))
    return events


def expire_old(data_dir, now, rules_doc=None, records=None):
    """close records nobody needs any more: unacknowledged and the trigger has been off for the rule's hold time (12 h by default), or older than 72 h. Returns the list of ids."""
    out = []
    for rec0 in (records if records is not None else load_records(data_dir)):
        nid = rec0['id']
        if rec0.get('status') in CLOSED:
            continue
        why = None
        try:
            if rec0.get('cleared') and hours_between(parse_local(rec0['cleared']), now) >= float(rec0.get('expireAfterClearH') or DEFAULT_EXPIRE_AFTER_CLEAR_H):
                why = 'the trigger has been off since %s and nobody acknowledged it' % rec0['cleared']
            elif hours_between(parse_local(rec0['created']), now) >= MAX_RECORD_AGE_H:
                why = 'older than %d hours and not acknowledged' % MAX_RECORD_AGE_H
        except (ValueError, TypeError, KeyError):
            why = None
        if not why:
            continue
        with locked_record(data_dir, nid):
            rec = load_record(data_dir, nid)
            if not rec or rec.get('status') in CLOSED:
                continue
            rec['status'] = 'expired'
            rec['expiredReason'] = why
            rec['ackDueAt'] = None
            rec['updated'] = f_local(now)
            save_record(data_dir, rec)
        append_log(data_dir, 'notification_expired', now, snap=_snap(data_dir), id=nid, rule=rec.get('rule'), reason=why, drill=bool(rec.get('drill')))
        out.append(nid)
    return out


def timers(data_dir, now, records=None):
    """what the console shows for every open record: id, rule, tier, status, minutes left to the acknowledgement time, the escalation step reached, hold"""
    out = []
    for rec in (records if records is not None else load_records(data_dir)):
        if rec.get('status') in CLOSED:
            continue
        h = rec.get('hold')
        held = False
        try:
            held = bool(h and parse_local(h['until']) > now)
        except (ValueError, TypeError, KeyError):
            held = False
        out.append({'id': rec['id'], 'rule': rec.get('rule'), 'rung': rec.get('rung'), 'tier': rec.get('tier'), 'baseTier': rec.get('baseTier'), 'status': rec.get('status'), 'created': rec.get('created'),
                    'ackDueAt': rec.get('ackDueAt'), 'minutesLeft': None if rec.get('cleared') else minutes_left(rec, now), 'cleared': rec.get('cleared'), 'steps': len(rec.get('escalations') or []), 'held': held,
                    'topTier': bool(rec.get('topTierUnacknowledged')), 'drill': bool(rec.get('drill')), 'subject': rec.get('subject')})
    return out


# ------------------------------------------------------------------------------------------------ command line
def _line(r):
    left = r.get('minutesLeft')
    clock = ('due in %d min' % left if left is not None and left >= 0 else ('%d min overdue' % -left if left is not None else ('trigger off since %s, no clock' % r['cleared'] if r.get('cleared') else 'no clock')))
    return '%s  %-6s %-3s %-9s %s%s%s  %s' % (r['id'], r.get('rule'), r.get('tier'), r.get('status'), clock, ' HELD' if r.get('held') else '', ' DRILL' if r.get('drill') else '', short_text(r.get('subject') or '', 70))


def main(argv=None):
    ap = argparse.ArgumentParser(description='WM004 weather channel: acknowledge, hold, approve and escalate notifications')
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR)
    ap.add_argument('--now', default=None, help='override the clock, ISO local time (tests)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list')
    p.add_argument('--all', action='store_true')
    p = sub.add_parser('show')
    p.add_argument('id')
    for name in ('ack', 'hold'):
        p = sub.add_parser(name)
        p.add_argument('id')
        p.add_argument('--by', required=True, help='a role id of channel_rules.json')
        if name == 'hold':
            p.add_argument('--minutes', type=int, default=60)
    for name in ('approve', 'reject', 'sent'):
        p = sub.add_parser(name)
        p.add_argument('id')
        if name == 'reject':
            p.add_argument('--note', default='')
    sub.add_parser('escalate')
    a = ap.parse_args(argv)
    data_dir = os.path.abspath(a.data_dir)
    now = parse_local(a.now) if a.now else now_local()
    rules_doc, _ = load_rules(data_dir)
    if a.cmd == 'list':
        recs = load_records(data_dir)
        rows = timers(data_dir, now, recs) if not a.all else [dict(r, minutesLeft=minutes_left(r, now), held=False, steps=len(r.get('escalations') or [])) for r in recs]
        if not rows:
            print('no open notifications' if not a.all else 'no notifications')
        for r in rows:
            print(_line(r))
        return 0
    if a.cmd == 'show':
        r = load_record(data_dir, a.id)
        if not r:
            print('no notification %s' % a.id)
            return 1
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    if a.cmd == 'ack':
        res = ack(data_dir, a.id, a.by, now, rules_doc)
    elif a.cmd == 'hold':
        res = hold(data_dir, a.id, a.by, now, a.minutes, rules_doc)
    elif a.cmd == 'approve':
        res = approve(data_dir, a.id, now, rules_doc)
    elif a.cmd == 'reject':
        res = reject(data_dir, a.id, now, a.note)
    elif a.cmd == 'sent':
        res = mark_sent(data_dir, a.id, now)
    else:
        evs = escalate(data_dir, now, rules_doc, notify=lambda t: print(t))
        print('%d escalation step(s)' % len(evs))
        return 0
    if res.get('ok'):
        r = res['record']
        print('%s %s%s' % (r['id'], r.get('status'), ' (already)' if res.get('already') else ''))
        return 0
    print('NOT DONE: %s' % res.get('error'))
    return 1


if __name__ == '__main__':
    sys.exit(main())
