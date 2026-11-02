# -*- coding: utf-8 -*-
"""The rain status on the public website (17 Oct 2026, the owner: "open within my website in small scale, we are sure what is going on").
Writes wm004/status.json and wm004/index.html into the website clone (Desktop\\wm001-site) from the laptop's
feed (rain emergency\\weather.json, written by weather_feed.py), the Wet Utilities live register (data/psd_pumps_live.json, the
15 minute task) and the actions register; then commits and pushes main and gh-pages with --push. No name, phone or personal data
leaves this script: the card shows the stage the record supports, the feed age, the NCM reading, the rain at the eight points,
the pumps on standby and the late actions count. The cloud routine's feed had no model data (Open Meteo unreachable from
there), so the laptop is the writer of record for the website feed.
24 Oct 2026: the feed is written by weather_engine.py (weather.json schema 2). The card keeps every field it had and adds the rain and fog hazard levels (Model forecast,
Proposed, and Reported, never an official warning unless it is the NCM entry), the reading quality and the state of the engine. The stage uses the engine one rule."""
import os, sys, json, re, shutil, datetime as dt, subprocess
import ncm_log as N

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
SITE = os.environ.get('WM001_SITE', '.')
OUT = os.path.join(SITE, 'wm004')
LINK = 'https://alalisaif91-droid.github.io/wm001/wm004/console/'


READER_SILENT_MIN = 90                 # the same limit as the console, the board and WC-43
NCM_CURRENT_H = 3                      # beside a reader with nothing fresh, a none a person logged stands this long: WX_CURRENT_H in wx3/core.js


def reader_down(snapshot, now):
    """why the NCM reader has nothing fresh, from what ncm_auto.py last fetched, or None when it has. A read file that says NO READING, is silent, is ahead of the clock, or could not be fetched all count; so does no record at all."""
    if not isinstance(snapshot, dict) or not snapshot.get('fetched'):
        return 'the NCM reader file could not be read'
    try:
        age = (now - dt.datetime.strptime(str(snapshot.get('readAt'))[:16], '%Y-%m-%d %H:%M')).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 'the NCM reader file has no read time'
    if age > READER_SILENT_MIN:
        return 'the NCM reader has been silent for %d minutes' % age
    if age < -10:
        return 'the read time of the NCM reader is ahead of the clock'
    if snapshot.get('state') == 'NO READING':
        return 'the NCM reader says NO READING'
    return None


def reader_guard(w, snapshot, now):
    """The card's NCM block beside a reader with nothing fresh (independent review, F2). A none that was automatic, or that a person logged over 3 hours ago, is no reading: NCM may have spoken since and the reader cannot say. A colour stands.
    The stage cannot rest on it either (stage_of reads readerDownWhy). Returns the record, unchanged when the reader is fresh or the block holds no none."""
    why = reader_down(snapshot, now)
    n = w.get('ncm') if isinstance(w.get('ncm'), dict) else {}
    if not why or not n:
        return w
    age = _elapsed_h(n.get('asof'), now)
    auto = by_role(n.get('by')) == 'engine' or bool(n.get('auto'))
    lv = n.get('levels') if isinstance(n.get('levels'), dict) else {}
    if not (auto or age is None or age > NCM_CURRENT_H) or (n.get('level') != 'none' and 'none' not in lv.values()):
        return w
    x, b = dict(w), dict(n)
    b['level'] = 'unknown' if b.get('level') == 'none' else b.get('level')
    b['levels'] = {h: ('unknown' if v == 'none' else v) for h, v in lv.items()}
    b['stale'] = True
    x['ncm'], x['readerDownWhy'] = b, why
    return x


def stage_of(w):
    """The stage the record supports. Schema 2 of weather.json (the weather engine, 24 Oct 2026) carries the stage the weather alone would call under the plan
    triggers; that one rule is used here so the card and the engine never disagree. A feed that could not be read is 'Not determined', never Routine."""
    st = ((w.get('interaction') or {}).get('stage') or {})
    ws = st.get('weatherStage')
    if ws == 'Routine readiness' and w.get('readerDownWhy'):
        return 'Not determined', 'The last NCM reading is not current and %s, so no stage is proposed from the weather alone' % w['readerDownWhy']
    if ws in ('Routine readiness', 'Enhanced watch', 'Pre deployment', 'Active response'):
        by = st.get('setBy') or []
        return ws, ('The weather alone under the plan triggers: ' + '; '.join(by)) if by else 'The weather alone under the plan triggers: no input calls a stage'
    if ws == 'unknown' or w.get('modelLevel') == 'unknown':
        # the reason is the engine's own: a model that could not be read, a typed NCM reading that the log refused, an NCM page colour that nobody has logged; the card never blames the model for the NCM
        n1 = w.get('ncm') or {}
        if w.get('modelLevel') == 'unknown':
            return 'Not determined', 'The weather model could not be read at the last run, so no stage is proposed from it'
        if (n1.get('refused') or {}).get('open') or any(isinstance(x, dict) and x.get('kind') == 'ncm-refused' for x in ((w.get('health') or {}).get('highAlerts') or [])):
            return 'Not determined', 'An NCM reading was typed and refused and is not in the log, so no stage is proposed from the weather alone'
        if (n1.get('pageShows') or {}).get('pending'):
            return 'Not determined', 'The NCM warnings page shows a warning colour that nobody has logged, so no stage is proposed from the weather alone'
        return 'Not determined', 'The weather could not be read well enough at the last run, so no stage is proposed from it'
    n0 = w.get('ncm') or {}
    ncm = n0.get('level', 'unknown')
    lvl = w.get('modelLevel', 'none')
    silent = w.get('engineSilent')
    calm_blocked = None
    if isinstance(silent, dict):
        if silent.get('ageHours') is not None:
            calm_blocked = ('The weather engine has not written for %d hours (last %s), so the colour of the NCM warnings page and the hazard levels cannot be checked and no stage is proposed from the weather alone'
                            % (round(silent['ageHours']), silent['asof']))
        else:
            # 29 Oct 2026 (CARD-ENGINE-MISSING): a record that is missing or damaged is the same as a silent engine, never Routine readiness
            calm_blocked = ('The weather engine record %s, so the colour of the NCM warnings page and the hazard levels cannot be checked and no stage is proposed from the weather alone'
                            % (silent.get('why') or 'cannot be read'))
    if ncm == 'red': return 'Active response', 'NCM red alert'
    if ncm == 'orange' or lvl == 'high': return 'Pre deployment', 'NCM orange or the model above 25 mm in 6 h'
    if ncm == 'yellow' or lvl == 'watch': return 'Enhanced watch', 'NCM yellow or the model above 10 mm in 6 h'
    # 24 Oct 2026 (verification NEW-1): an NCM level that was never read, or read long ago, is not "no NCM signal"
    age = None
    try:
        age = (_local_now() - dt.datetime.strptime(n0.get('asof') or '', '%Y-%m-%d %H:%M')).total_seconds() / 3600.0
    except ValueError:
        pass
    if calm_blocked:
        # second audit, CARD-ENGINE-OLD: with the engine silent, only a call for MORE readiness stands (red, orange, yellow and the model above are handled above); a calm stage is never given
        return 'Not determined', calm_blocked
    if ncm not in ('none', 'yellow', 'orange', 'red'):
        # an NCM level that was not read (unknown, missing, anything else) is no reading: the card never says Routine readiness, and never says there is no NCM signal, beside it (audit NEW-1)
        return 'Not determined', 'The NCM alert level has not been read, so no stage is proposed from the weather alone; the model is under 10 mm in 6 h'
    if age is not None and age > 24:
        return 'Routine readiness', 'The last NCM reading (none) is %d hours old and is not current; the model is under 10 mm in 6 h' % round(age)
    if age is not None and age > 3:
        # 28 Oct 2026 (H1): a reading of 11 hours is said to be 11 hours old, so the card never reads as a fresh all clear
        return 'Routine readiness', 'The last NCM reading shows no alert for Ras Al Khaimah and is %d hours old; the model is under 10 mm in 6 h' % round(age)
    return 'Routine readiness', 'The last NCM reading shows no alert for Ras Al Khaimah; the model is under 10 mm in 6 h'


def plain(x, n=600):
    """text typed by a person (an NCM reading) for the public card: no markup, no control characters, cut short (audit NEW-4). The page also writes it as text, never as HTML."""
    t = ''.join(ch for ch in str(x or '') if ch.isprintable()).replace('<', ' ').replace('>', ' ')
    t = ' '.join(t.split())
    return t if len(t) <= n else t[:n - 1].rstrip() + '.'


# 29 Oct 2026 (CARD-NCM-TEXT): the engine and the legacy reader write their NCM sentences for the weather desk ("logged with ncm_log.py add", "(python ncm_log.py clear-refused)"); the card is
# public and names no internal command or file, and a sentence ends before "Last logged reading" begins. The same rules as weather_feed_legacy.public_text and the site feed script.
_SUBCMD = r'(?:\s+(?:add|clear-refused|list|show|status))?'
_PUBLIC = ((re.compile(r'\s*\((?:python3?\s+)?ncm_log\.py' + _SUBCMD + r'\)'), ''),
           (re.compile(r'\s+(?:with|through|using)\s+(?:python3?\s+)?ncm_log\.py' + _SUBCMD), ' by the weather desk'),
           (re.compile(r'\bncm_auto_state\.json\b'), 'the record of the automatic NCM page read'),
           (re.compile(r'\bncm_refused\.jsonl\b'), 'the file of refused NCM readings'),
           (re.compile(r'(?:python3?\s+)?\b[\w-]+\.(?:py|cmd|bat|jsonl)\b' + _SUBCMD), 'the weather desk log'))


def public_text(x):
    """a sentence for the public card: no internal command or file name, and a stop before "Last logged reading" so that two sentences never run together"""
    t = ' '.join(str(x or '').split())
    for rx, rep in _PUBLIC:
        t = rx.sub(rep, t)
    t = re.sub(r'(?<=[^\s.!?:;])\s+(Last logged reading:)', r'. \1', t)
    t = ' '.join(t.split())
    return (t[0].upper() + t[1:]) if t else t


def public(x, n=600):
    """plain (no markup, cut short) and public (no internal command or file name)"""
    return plain(public_text(x), n)


def by_role(by):
    """who made an NCM entry, as a role: engine (automatic read), backfill (from the Barq log) or manual. What a person typed in by is never published (house rule: no name on the card)."""
    b = str(by or '').lower()
    if b.startswith('engine'):
        return 'engine'
    if 'live reader' in b:
        return 'live reader'
    if 'backfill' in b:
        return 'backfill'
    return 'manual' if b else None


def _elapsed_h(since, now):
    """hours from a record stamp to the card time, never negative; None when the stamp does not read"""
    try:
        return max(0.0, (now - dt.datetime.strptime(str(since)[:16], '%Y-%m-%d %H:%M')).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return None


def hazard_ncm(ncm_block, block_level=None):
    """the NCM level of one hazard for the public card (second audit, CARD-NCM-NONE). The engine writes hazards.rain.ncm as {level, stale, pagePending} and hazards.fog.ncm as {level, ids} only: a level of
    none that is stale, that sits beside a warnings page colour nobody has logged, or whose hazard the NCM block (ncm.levels, which the engine turns to unknown while a page colour is not logged) says is
    unknown, is NO READING on the card, never none (the NEW-1 and H1 class). The card page prints 'unknown' as no reading."""
    n = ncm_block if isinstance(ncm_block, dict) else {}
    if n.get('pagePending') or n.get('stale') or block_level == 'unknown':
        return 'unknown'
    return n.get('level')


def card_extras(w, now=None):
    """Rain and fog hazard levels and the engine state for the public card. Model output is labelled Model forecast (Proposed); station reports Reported;
    only the NCM entry is called official. Counts and ages only: no internal file name, no person.
    28 Oct 2026 (H1): the ages of the NCM reading are those of the card time, worked out from the stamp of the reading, never the age that was written into a file hours ago."""
    now = now or _local_now()
    h = ((w.get('interaction') or {}).get('hazards') or {})
    hz = {}
    r, f, d = h.get('rain'), h.get('fog'), h.get('dust')
    nlv = ((w.get('ncm') or {}).get('levels')) or {}
    if r:
        pv = (r.get('model') or {}).get('proposedVote') or {}
        hz['rain'] = {'level': r.get('level'), 'label': 'Model forecast (Proposed), with the NCM colour (official) and the station reports (Reported)', 'model': (r.get('model') or {}).get('level'),
                      'ncm': hazard_ncm(r.get('ncm'), nlv.get('rain')), 'stationRainNow': (r.get('observed') or {}).get('rainNow'),
                      'proposedVote': {'level': pv.get('level'), 'aboveRule': bool(pv.get('aboveRule')), 'applied': bool(pv.get('applied'))}}
    if f:
        hz['fog'] = {'level': f.get('level'), 'label': 'Model forecast (Proposed ladder), with the station reports (Reported) and the NCM colour (official)', 'forecast': f.get('forecastLevel'),
                     'observed': f.get('observedLevel'), 'ncm': hazard_ncm(f.get('ncm'), nlv.get('fog'))}
    if d:
        hz['dust'] = {'level': 'reported only', 'label': 'Station reports (Reported); no level', 'stations': (d.get('reported') or {}).get('stationsNow')}
    out = {'hazards': hz, 'readingQuality': w.get('readingQuality')}
    n = w.get('ncm') or {}
    age_h = n.get('ageHours')
    ea = _elapsed_h(n.get('asof'), now)
    if ea is not None:
        age_h = round(ea, 1)
    warn_h = n.get('warningsAgeHours')
    el = _elapsed_h(w.get('ncmRecordAsof') or w.get('asof'), now)          # the block was worked out at the stamp of its record: add the time since
    if isinstance(warn_h, (int, float)) and el is not None:
        warn_h = round(warn_h + el, 1)
    out['ncmDetail'] = {'ageHours': age_h, 'by': by_role(n.get('by')), 'kind': n.get('kind'), 'warningsAgeHours': warn_h, 'stale': bool(n.get('stale')) or (age_h is None or age_h > 24)}
    # a manual NCM reading that the log refused (typed and not recorded) is a high alert on the card, in the words of the engine: the reason and the time, no name, no typed text (audit N1)
    ha = (w.get('health') or {}).get('highAlerts')
    out['highAlerts'] = [{'kind': plain(x.get('kind'), 40), 'at': plain(x.get('at'), 20), 'text': public(x.get('text'), 700)} for x in ha if isinstance(x, dict)][:5] if isinstance(ha, list) else []
    try:
        hh = json.load(open(os.path.join(HERE, 'data', 'weather', 'health.json'), encoding='utf-8'))
        lr = hh.get('lastRun') or {}
        om = (hh.get('sources') or {}).get('openmeteo') or {}
        out['engine'] = {'ok': lr.get('ok'), 'lastRun': lr.get('now'), 'lastGoodAsof': hh.get('lastGoodAsof'), 'readingQuality': hh.get('readingQuality'), 'pointsRead': hh.get('pointsRead'),
                         'pointsTotal': hh.get('pointsTotal'), 'alertCount': len(hh.get('alerts') or []), 'modelFailuresInARow': om.get('consecutiveFailures'),
                         'ncmWarningsReadingAgeHours': hh.get('ncmWarningsManualAgeHours')}
    except (OSError, ValueError):
        out['engine'] = None
    return out


PERSONAL = re.compile(r'(\+971|\b00971|\b05\d[ -]?\d{3}[ -]?\d{4}\b|[\w.+-]+@[\w-]+\.[\w.-]+)')


def copy_conditions(src, dst_dir, now=None):
    """24 Oct 2026 (weather channel): conditions.json, the public subset of the conditions (wind, humidity, temperature, pressure, cloud, visibility, CAPE, dust: the points, the stations, the levels),
    is copied into the website clone beside status.json. It is copied whole or not at all (a temporary file, then a replace); a file that is missing, does not parse, is not a conditions file or holds
    what looks like a phone number or an e-mail address is not copied and the reason is said. Nothing is committed or pushed here: --push of this script and the hourly task do that.
    Returns (copied, message)."""
    try:
        with open(src, encoding='utf-8') as f:
            raw = f.read()
        d = json.loads(raw)
    except (OSError, ValueError):
        return False, 'conditions.json is missing or does not parse: not copied'
    if not isinstance(d, dict) or not isinstance(d.get('asof'), str) or 'points' not in d:
        return False, 'conditions.json is not a conditions file: not copied'
    m = PERSONAL.search(raw)
    if m:
        return False, 'conditions.json holds what looks like a phone number or an e-mail address (%s): not copied' % m.group(0)[:12]
    try:
        os.makedirs(dst_dir, exist_ok=True)
        tmp = os.path.join(dst_dir, '.conditions.json.part')
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(raw)
        os.replace(tmp, os.path.join(dst_dir, 'conditions.json'))
    except OSError as e:
        return False, 'conditions.json could not be copied (%s)' % str(e)[:80]
    return True, 'conditions.json copied (%d bytes, as of %s, %s)' % (len(raw.encode('utf-8')), d['asof'], _age_words(d['asof'], now or _local_now()))


def _age_words(asof, now):
    """'1.2 h old, current' or '5.0 h old, NOT current': a file whose asof is older than 3 hours is never presented as current"""
    try:
        age = (now - dt.datetime.strptime(str(asof)[:16], '%Y-%m-%d %H:%M')).total_seconds() / 3600.0
    except ValueError:
        return 'age unknown, NOT current'
    return '%.1f h old, %s' % (max(0.0, age), 'current' if -0.25 <= age <= 3.0 else 'NOT current')


def _local_now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)


PUBLIC_CHANNEL_KEYS = ('schema', 'asof', 'tz', 'validHours', 'staleAfter', 'status', 'rung', 'stage', 'recommendedStage', 'highest', 'banner', 'rungSince', 'reached')


def public_channel(d):
    """what leaves the laptop of the weather channel: the recommended rung, why, the age and the blind state. The timers, the roles, the notifications, the activation pack (sectors, pumps, crews,
    tankers), the owners of the actions and the health of the transports stay on the laptop (they are for the console of the rain committee). The status words say so."""
    out = {k: d.get(k) for k in PUBLIC_CHANNEL_KEYS if k in d}
    out['why'] = [{'rule': w.get('rule'), 'title': w.get('title'), 'rung': w.get('rung'), 'status': w.get('status'), 'evidence': w.get('evidence')} for w in (d.get('why') or [])[:6] if isinstance(w, dict)]
    out['blind'] = {'on': (d.get('blind') or {}).get('on'), 'hours': (d.get('blind') or {}).get('hours')}
    out['actions'] = {'counts': (d.get('actions') or {}).get('counts')}
    out['public'] = True
    out['publicNote'] = 'Public view. The recommendation of the weather channel; the rain committee sets the stage. The notifications, the roles, the activation pack and the owners stay on the laptop. Not current after staleAfter.'
    return out


def copy_engine(src, dst_dir, now=None):
    """1 Nov 2026 (the owner: "shouldent this reflect the reading ?"): engine.json, the levels and the health of the last engine run, for the level cards of the console, which otherwise show the record baked at build.
    The NCM block stays out: this is a public file. Whole or not at all, like conditions.json. Returns (copied, message)."""
    keep = ('asof', 'tz', 'fetchedUtc', 'schema', 'modelLevel', 'worstMax6', 'worstMax6Median', 'readingQuality', 'levelBasis', 'rainLevel', 'rainBasis', 'fogLevel', 'fogBasis', 'levelText', 'vote', 'coverage', 'health')
    try:
        with open(src, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False, 'engine.json: the engine record is missing or does not parse: not copied'
    if not isinstance(d, dict) or not isinstance(d.get('asof'), str) or 'rainLevel' not in d or 'fogLevel' not in d:
        return False, 'engine.json: not an engine record: not copied'
    raw = json.dumps({k: d[k] for k in keep if k in d}, ensure_ascii=False, separators=(',', ':'))
    m = PERSONAL.search(raw)
    if m:
        return False, 'engine.json holds what looks like a phone number or an e-mail address (%s): not copied' % m.group(0)[:12]
    try:
        os.makedirs(dst_dir, exist_ok=True)
        tmp = os.path.join(dst_dir, '.engine.json.part')
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(raw)
        os.replace(tmp, os.path.join(dst_dir, 'engine.json'))
    except OSError as e:
        return False, 'engine.json could not be copied (%s)' % str(e)[:80]
    return True, 'engine.json copied (%d bytes, as of %s)' % (len(raw.encode('utf-8')), d['asof'])


def copy_channel(src, dst_dir, now=None):
    """25 Oct 2026 (weather channel): channel.json, the compact copy of the channel, into the website clone as a public view (public_channel). Whole or not at all; a file that is missing, does not
    parse, is not a channel file or holds a phone number or an e-mail address is not copied. Nothing is committed or pushed here. The message says how old the file is and whether it is current
    (3 hours). Returns (copied, message)."""
    now = now or _local_now()
    try:
        with open(src, encoding='utf-8') as f:
            raw = f.read()
        d = json.loads(raw)
    except (OSError, ValueError):
        return False, 'channel.json is missing or does not parse: not copied'
    if not isinstance(d, dict) or not isinstance(d.get('asof'), str) or 'rung' not in d:
        return False, 'channel.json is not a channel file: not copied'
    pub = json.dumps(public_channel(d), ensure_ascii=False, separators=(',', ':'))
    m = PERSONAL.search(raw) or PERSONAL.search(pub)
    if m:
        return False, 'channel.json holds what looks like a phone number or an e-mail address (%s): not copied' % m.group(0)[:12]
    try:
        os.makedirs(dst_dir, exist_ok=True)
        tmp = os.path.join(dst_dir, '.channel.json.part')
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(pub)
        os.replace(tmp, os.path.join(dst_dir, 'channel.json'))
    except OSError as e:
        return False, 'channel.json could not be copied (%s)' % str(e)[:80]
    return True, 'channel.json (public view) copied (%d bytes, as of %s, %s)' % (len(pub.encode('utf-8')), d['asof'], _age_words(d['asof'], now))


def card_source(w, now=None):
    """the feed the card reads its hazards and its engine state from: the published feed (weather.json), and for what the legacy file does not carry (the interaction block, the health block, the
    reading quality) the engine staging copy (weather_engine.json) when it is under 3 hours old. The stage and the rain of the card stay those of the published feed."""
    now = now or _local_now()
    # 29 Oct 2026 (CARD-ENGINE-MISSING): a record that is missing, does not parse, is not a record or has a stamp that does not read or is ahead of the clock was returned as no record at all, so
    # the legacy block alone could give Routine readiness beside a page colour nobody can see. It is now the same as a silent engine: Not determined with the reason (stage_of).
    why = None
    try:
        with open(os.path.join(ROOT, 'weather_engine.json'), encoding='utf-8') as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError('not an engine record')
        age = (now - dt.datetime.strptime(str(s.get('asof'))[:16], '%Y-%m-%d %H:%M')).total_seconds() / 3600.0
    except FileNotFoundError:
        why = 'is missing'
    except (OSError, ValueError, TypeError):
        why = 'cannot be read (damaged, not a record, or with no time)'
    if why is None and age < -0.25:
        why = 'is stamped %s, ahead of the clock' % str(s.get('asof'))[:16]
    if why is not None:
        try:
            own = int(w.get('schema') or 1) >= 2
        except (TypeError, ValueError):
            own = False
        if own:
            return w                                   # a published feed that is itself an engine record carries its own interaction block
        x = dict(w)
        x['engineSilent'] = {'asof': None, 'ageHours': None, 'why': why}
        return x
    if age > 3.0:
        # second audit, CARD-ENGINE-OLD: an engine record over 3 hours old is not used for the hazards, but it is no longer silent on the stage line either. The legacy block cannot carry the
        # colour of the NCM warnings page, so the card says Not determined, with the reason, rather than Routine readiness beside an engine that stopped. A published feed that is itself an
        # engine record (schema 2) is not affected: it carries its own interaction block.
        if int(w.get('schema') or 1) >= 2:
            return w
        x = dict(w)
        x['engineSilent'] = {'asof': str(s.get('asof'))[:16], 'ageHours': round(age, 1)}
        return x
    x = dict(w)
    for k in ('interaction', 'health', 'readingQuality'):
        if k not in w and k in s:
            x[k] = s[k]
    # 28 Oct 2026 (H1): the NCM block of the engine record (the colour the warnings page shows and nobody has logged, a refused reading, the age) is taken when it is newer than the one the
    # published feed carries. The legacy reader cannot know the page colour; a card that reads the legacy block alone said Routine readiness beside an engine that said unknown.
    en = s.get('ncm')
    if isinstance(en, dict) and en and str(en.get('asof') or '') >= str((w.get('ncm') or {}).get('asof') or ''):
        x['ncm'] = en
        x['ncmRecordAsof'] = s.get('asof')
    else:
        x['ncmRecordAsof'] = w.get('asof')
    return x


def read_reader_snapshot():
    """what ncm_auto.py last fetched from the site, or None"""
    try:
        with open(os.path.join(HERE, 'data', 'weather', 'ncm_live_state.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def card_status(w, pumps, actions, now):
    """the status card from a feed, the pump counts and the action counts. 2 Nov 2026: main below and the standby engine on GitHub (wm004/engine_action.py) both use it."""
    # 28 Oct 2026 (H1): the stage, the NCM block and the hazards of the card are read from the same source: the published feed with the engine record under it when that is under 3 hours old
    # (card_source). The stage was worked out from the legacy feed alone and said Routine readiness beside an engine that said unknown and a channel that recommended Enhanced watch.
    ws = reader_guard(card_source(w, now), read_reader_snapshot(), now)
    stage, why = stage_of(ws)
    pts = [{'name': p['name'], 'next24': p.get('next24'), 'next72': p.get('next72'), 'max6': p.get('max6'), 'error': bool(p.get('error'))} for p in w['points']]
    status = {'written': now.strftime('%Y-%m-%d %H:%M'), 'tz': 'Asia/Dubai', 'stage': stage, 'stageWhy': why, 'stageNote': 'The stage the record supports; the rain committee sets it',
              'feedAsof': w['asof'], 'modelLevel': w.get('modelLevel'), 'worstMax6': w.get('worstMax6'), 'thresholds': w.get('thresholds'),
              'ncm': {'level': (ws.get('ncm') or {}).get('level'), 'text': ('No current NCM reading: %s.' % ws['readerDownWhy']) if ws.get('readerDownWhy') else public(N.public_reading_text(ws.get('ncm'))), 'asof': (ws.get('ncm') or {}).get('asof'),
                      'levels': (ws.get('ncm') or {}).get('levels'), 'hazard': (ws.get('ncm') or {}).get('hazard'), 'by': by_role((ws.get('ncm') or {}).get('by')),
                      'auto': by_role((ws.get('ncm') or {}).get('by')) == 'engine' or bool((ws.get('ncm') or {}).get('auto'))},         # level is the rain colour; levels says fog and dust, so a fog warning text never sits beside "none" unexplained
              'points': pts, 'pumps': pumps,
              'actions': actions, 'season': '07 Oct 2026 to 31 Mar 2027', 'plan': 'WA-PSD-PLN-05 Draft 2, 09 Oct 2026', 'console': LINK}
    status.update(card_extras(ws, now))
    return status, stage


def level_site():
    """2 Nov 2026: the standby on GitHub can write engine.json, conditions.json and channel.json while the laptop sleeps. Before the laptop writes anything it stands level with origin/main, or the
    first push after a sleep meets those files changed on both sides, the rebase fails, and that commit blocks every later hour. A commit an earlier hour could not push is undone and its files,
    which are written again now, stay in the clone."""
    g = site_git(SITE)
    g('fetch', '-q', 'origin')
    if (g('rev-list', '--count', 'origin/main..HEAD').stdout or '').strip() not in ('0', ''):
        g('reset', '-q', '--mixed', 'origin/main')
    r = g('rebase', '--autostash', 'origin/main')
    if r.returncode:
        g('rebase', '--abort')
    return r.returncode == 0


def site_is_newer(src, dst):
    """the published file is newer than this one: the standby wrote it after the laptop's last good run, so it stays"""
    try:
        with open(src, encoding='utf-8') as f:
            a = json.load(f).get('asof')
        with open(dst, encoding='utf-8') as f:
            b = json.load(f).get('asof')
        return bool(a and b and str(b) > str(a))
    except (OSError, ValueError, AttributeError):
        return False


def main(push=False):
    if push and not level_site():
        print('the site clone could not be brought level with origin/main')
    w = json.load(open(os.path.join(ROOT, 'weather.json'), encoding='utf-8'))
    live = json.load(open(os.path.join(HERE, 'data', 'psd_pumps_live.json'), encoding='utf-8'))
    acts = json.load(open(os.path.join(HERE, 'actions17.json'), encoding='utf-8'))
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)
    import collections
    st = collections.Counter(p['status'] for p in live['pumps'])
    late = 0
    for a in acts:
        try:
            due = dt.datetime.strptime(a[4].split(',')[0].strip(), '%d %b %Y')
            if due.date() < now.date(): late += 1
        except Exception:
            pass
    pumps = {'captured': live.get('captured'), 'total': len(live['pumps']), 'standby': st.get('Standby', 0), 'working': st.get('Working', 0), 'noPump': st.get('Not Received', 0)}
    status, stage = card_status(w, pumps, {'total': len(acts), 'late': late}, now)
    s = json.dumps(status, ensure_ascii=False)
    for bad in ('055 ', '050 ', '054 ', '052 ', '056 '):
        assert bad not in s, 'a phone number in the status'
    os.makedirs(OUT, exist_ok=True)
    json.dump(status, open(os.path.join(OUT, 'status.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    # weather.json in the site stays the cloud routine's (it commits it hourly); the card reads status.json, which carries the laptop feed
    shutil.copy(os.path.join(HERE, 'web', 'wm004_status.html'), os.path.join(OUT, 'index.html'))
    print('status written', status['written'], stage, 'feed', w['asof'])
    for fn, src, rel in ((copy_conditions, 'conditions.json', 'conditions.json'), (copy_channel, 'channel.json', 'channel.json'), (copy_engine, 'weather_engine.json', 'engine.json')):
        if site_is_newer(os.path.join(ROOT, src), os.path.join(OUT, rel)):
            print(rel, 'on the site is newer (the standby wrote it): left as it is')
        else:
            print(fn(os.path.join(ROOT, src), OUT)[1])
    if push:
        # 25 Oct 2026: wm004/weather.json belongs to the GitHub Action (every 30 minutes, always on); the laptop never stages it, so the two writers cannot collide.
        # Everything else under wm004 is the laptop's (card, status, conditions, channel, NCM reading, the console copy behind the access code).
        # feed.trigger: touching it and pushing starts a run of the rain feed Action at once (its own schedule can start late, or not at all for a while on a new workflow)
        try:
            open(os.path.join(SITE, 'wm004', 'feed.trigger'), 'w').write('%s\n' % status['written'])
        except OSError:
            pass
        ok, msg = push_site(site_git(SITE), SITE, 'wm004: rain status %s, %s, feed %s' % (status['written'], stage, w['asof']))
        print(msg)


SITE_RELS = ('status.json', 'index.html', 'conditions.json', 'channel.json', 'engine.json', 'ncm.json', 'console', 'feed.trigger')
# 30 Oct 2026: these belong to the NCM reader on GitHub, or are set by hand at deployment, and have one writer each. The laptop card never stages them, whatever else changes: push_site refuses.
NEVER_STAGED = ('ncm_state.json', 'ncm_index.json', 'ncm_live.enc', 'ncm_history.enc', 'ncm_radar.enc', 'ncm_pub.pem', 'ncm_pub.txt', 'ncm_places.json', 'ncm_crypto.py', 'ncm_reader_action.py', 'ncm.trigger', 'ncm/')


def site_git(site):
    """git in the website clone. 25 Oct 2026: a push stalled for 25 minutes on a bad connection, the task hit its 10 minute limit and the card never refreshed. Every git call has a time limit,
    a stalled transfer is cut after 30 s, and git never waits for a prompt."""
    def g(*a, t=120):
        try:
            return subprocess.run(['git', '-c', 'http.lowSpeedLimit=2000', '-c', 'http.lowSpeedTime=30', '-C', site] + list(a), capture_output=True, text=True, timeout=t,
                                  env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(a, 124, '', 'git %s timed out after %d s' % (a[0], t))
    return g


def push_site(g, site, message, rels=SITE_RELS):
    """Stage the laptop's files under wm004, commit, rebase on origin/main and push main. Returns (pushed or nothing to push, the line for the log).
    29 Oct 2026 (audit SITE-WORKTREE-BLOCKS-HOURLY-PUSH): a tracked file left changed in the clone and not committed (the reviewed Action script copied in at 06:06 for the owner to commit) made
    git refuse every hourly rebase ('cannot rebase: You have unstaged changes'), so no card, NCM reading, channel or feed trigger reached the site from 10:28. The rebase now carries such a change
    aside and puts it back (--autostash): the change stays in the clone, uncommitted, for its owner, and it is never committed or pushed by this script, which stages only its own files. When a
    rebase still fails, the log line names the files that are changed and not committed."""
    for rel in rels:
        if any(rel == n or rel.startswith(n) or rel.startswith('ncm_history') for n in NEVER_STAGED):
            return False, 'refused: %s belongs to the NCM reader and is never staged by the laptop' % rel
    for rel in rels:
        if os.path.exists(os.path.join(site, 'wm004', rel)):
            g('add', 'wm004/' + rel)
    r = g('commit', '-m', message)
    if 'nothing to commit' in (r.stdout + r.stderr):
        return True, 'no change'
    g('fetch', '-q', 'origin')
    r0 = g('rebase', '--autostash', 'origin/main')
    if r0.returncode:
        g('rebase', '--abort')
        dirty = [ln[3:] for ln in (g('status', '--porcelain', '--untracked-files=no').stdout or '').splitlines() if ln.strip()]
        return False, 'REBASE FAILED ' + (r0.stderr or '').strip()[:300] + ((' | changed and not committed in the site clone: ' + ', '.join(dirty[:8])) if dirty else '')
    note = ''
    if 'autostash' in (r0.stdout + r0.stderr).lower() and 'conflict' in (r0.stdout + r0.stderr).lower():
        note = ' | the change left uncommitted in the site clone did not go back cleanly: it is kept in git stash'
    # gh-pages is brought level by sync_site.py (a plain push of main to gh-pages is rejected whenever the cloud routine has committed)
    r1 = g('push', '-q', 'origin', 'main', t=150)
    return (r1.returncode == 0), ('pushed main' if r1.returncode == 0 else 'PUSH FAILED ' + (r1.stderr or '').strip()[:300]) + note


if __name__ == '__main__':
    main(push='--push' in sys.argv)
