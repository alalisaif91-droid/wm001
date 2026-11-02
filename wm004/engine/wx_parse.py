# -*- coding: utf-8 -*-
"""METAR and TAF parsing for the WM004 weather engine (aviation reports: status Reported).

Reads the raw report text first and the structured fields of the aviationweather.gov JSON second, so a report that the JSON
decodes badly (visibility in statute miles, 'visib' as the string '6+', VRB wind) is still read correctly. Handles CAVOK, NSC,
NCD, 9999, 0000, the M prefix of negative temperatures, directional minimum visibility (2400N), runway visual range, vicinity
codes (VCSH, VCTS, VCFG), the intensity signs, and the trend part of a METAR (BECMG, TEMPO, NOSIG) which is a forecast and is never
read as present weather. Standard library only."""
import re, datetime as dt
from wx_util import utc_to_local, utc_now

SM_M = 1609.344
DIRS = ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')
DESCR = ('MI', 'BC', 'PR', 'DR', 'BL', 'SH', 'TS', 'FZ')
PHEN = ('DZ', 'RA', 'SN', 'SG', 'IC', 'PL', 'GR', 'GS', 'UP', 'BR', 'FG', 'FU', 'VA', 'DU', 'SA', 'HZ', 'PO', 'SQ', 'FC', 'SS', 'DS')
WX_RE = re.compile(r'^(\+|-|VC)?((?:MI|BC|PR|DR|BL|SH|TS|FZ){0,2})((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)*)$')
WIND_RE = re.compile(r'^(\d{3}|VRB|///)(\d{2,3}|//)(?:G(\d{2,3}))?(KT|MPS|KMH)$')
VAR_RE = re.compile(r'^(\d{3})V(\d{3})$')
CLOUD_RE = re.compile(r'^(FEW|SCT|BKN|OVC)(\d{3}|///)(CB|TCU|///)?$')
VV_RE = re.compile(r'^VV(\d{3}|///)$')
TEMP_RE = re.compile(r'^(M?\d{2})/(M?\d{2}|//)?$')
RVR_RE = re.compile(r'^R\d{2}[LCR]?/[PM]?\d{4}(?:V[PM]?\d{4})?(?:FT)?[UDN]?$')
TREND_WORDS = ('NOSIG', 'BECMG', 'TEMPO', 'RMK', 'TREND')


def sm_to_m(v):
    """statute miles (a number, or a string such as '6+' or 'P6SM') to metres, rounded the way METARs step visibility:
    10 km or more is 10000; 5 to 9 km in whole km; 0.8 to 5 km in 100 m; under 800 m in 50 m. '6+' means six miles or more."""
    if v is None or v == '':
        return None
    if isinstance(v, str):
        s = v.strip().upper().replace('SM', '')
        if s.startswith('P') or s.endswith('+'):
            return 10000
        try:
            v = float(s)
        except ValueError:
            return None
    m = float(v) * SM_M
    if m != m or m < 0:
        return None                                       # a negative visibility is a sentinel for no value (-1), never a reading (audit WX-20); NaN is no value either
    if m >= 9500:
        return 10000
    if m >= 5000:
        return int(round(m / 1000.0) * 1000)
    if m >= 800:
        return int(round(m / 100.0) * 100)
    return int(round(m / 50.0) * 50)


def _temp(tok):
    if tok is None or tok in ('//', ''):
        return None
    neg = tok.startswith('M')
    n = int(tok[1:] if neg else tok)
    return -n if neg else n


def parse_wx_token(tok):
    """One present weather group to {raw, intensity, vicinity, descr, phen} or None when it is not a weather group."""
    m = WX_RE.match(tok)
    if not m or not tok:
        return None
    pre, desc, ph = m.group(1), m.group(2), m.group(3)
    descr = [desc[i:i + 2] for i in range(0, len(desc), 2)]
    phen = [ph[i:i + 2] for i in range(0, len(ph), 2)]
    if not descr and not phen:
        return None
    # a bare descriptor that is not a weather group on its own (MI, BC, PR, DR, BL, FZ alone) is not a report
    if not phen and not any(d in ('SH', 'TS') for d in descr):
        return None
    return {'raw': tok, 'intensity': pre if pre in ('+', '-') else '', 'vicinity': pre == 'VC', 'descr': descr, 'phen': phen}


def wx_flags(wx):
    """Classify parsed weather groups. Vicinity groups (VCSH, VCTS, VCFG) never count as weather at the station."""
    f = dict(rain=False, shower=False, thunder=False, fog=False, mist=False, haze=False, dust=False, vc_ts=False, vc_sh=False, vc_fg=False, other_precip=False)
    for w in wx or []:
        d, p, vc = w['descr'], w['phen'], w['vicinity']
        if vc:
            if 'TS' in d:
                f['vc_ts'] = True
            if 'SH' in d:
                f['vc_sh'] = True
            if 'FG' in p:
                f['vc_fg'] = True
            continue
        if 'RA' in p or 'DZ' in p:
            f['rain'] = True
        if 'SH' in d:
            f['shower'] = True
        if 'TS' in d:
            f['thunder'] = True
        if 'GR' in p or 'GS' in p:
            f['thunder'] = True
        if 'SN' in p or 'SG' in p or 'IC' in p or 'PL' in p or 'UP' in p:
            f['other_precip'] = True
        if 'FG' in p:
            f['fog'] = True
        if 'BR' in p:
            f['mist'] = True
        if 'HZ' in p:
            f['haze'] = True
        if 'DU' in p or 'SS' in p or 'DS' in p or ('SA' in p):
            f['dust'] = True
    f['rain_any'] = f['rain'] or f['shower'] or f['thunder']
    return f


def parse_metar(raw, ref=None):
    """Parse one raw METAR or SPECI. `ref` is a naive UTC datetime near the report, used only to place the day of month.
    Never raises on bad input: unreadable parts stay None and are listed in 'unparsed'."""
    out = {'raw': raw, 'type': None, 'station': None, 'time_utc': None, 'auto': False, 'cor': False, 'nil': False, 'cavok': False,
           'wind_dir': None, 'wind_vrb': False, 'wind_kt': None, 'gust_kt': None, 'wind_var': None, 'vis_m': None, 'vis_cap': False,
           'vis_min_m': None, 'vis_min_dir': None, 'rvr': [], 'wx': [], 'clouds': [], 'vv_ft': None, 'nsc': False, 'temp_c': None, 'dew_c': None,
           'qnh_hpa': None, 'altim_inhg': None, 'trend': None, 'remarks': None, 'unparsed': []}
    toks = (raw or '').replace('=', ' ').split()
    i = 0
    if i < len(toks) and toks[i] in ('METAR', 'SPECI'):
        out['type'] = toks[i]
        i += 1
    if i < len(toks) and re.match(r'^[A-Z]{4}$', toks[i]) and toks[i] not in ('AUTO', 'NSC', 'NCD', 'NOSIG', 'CAVOK'):
        out['station'] = toks[i]
        i += 1
    in_trend = False
    seen_vis = False
    while i < len(toks):
        t = toks[i]
        i += 1
        if t in ('COR', 'CCA', 'CCB'):
            out['cor'] = True
            continue
        if t == 'AUTO':
            out['auto'] = True
            continue
        if t == 'NIL':
            out['nil'] = True
            continue
        m = re.match(r'^(\d{2})(\d{2})(\d{2})Z$', t)
        if m and out['time_utc'] is None:
            day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
            base = ref or utc_now()
            out['time_utc'] = resolve_day(day, hh, mm, base)
            continue
        if t in ('NOSIG', 'BECMG', 'TEMPO', 'TREND'):
            in_trend = True
            out['trend'] = t if out['trend'] is None else out['trend']
            continue
        if t == 'RMK':
            out['remarks'] = ' '.join(toks[i:])
            break
        if in_trend:
            continue                                       # the trend part is a forecast, never present weather
        if t == 'CAVOK':
            out['cavok'] = True
            out['vis_m'] = 10000
            out['vis_cap'] = True
            seen_vis = True
            continue
        m = WIND_RE.match(t)
        if m and out['wind_kt'] is None and not out['wind_vrb'] and out['wind_dir'] is None:
            d, s, g, unit = m.groups()
            out['wind_vrb'] = d == 'VRB'
            out['wind_dir'] = int(d) if d.isdigit() else None
            k = {'KT': 1.0, 'MPS': 1.943844, 'KMH': 0.539957}[unit]
            out['wind_kt'] = round(int(s) * k) if s.isdigit() else None
            out['gust_kt'] = round(int(g) * k) if g else None
            continue
        m = VAR_RE.match(t)
        if m:
            out['wind_var'] = [int(m.group(1)), int(m.group(2))]
            continue
        if not seen_vis:
            mv = re.match(r'^(\d{4})$', t)
            if mv:
                v = int(mv.group(1))
                out['vis_m'] = 10000 if v >= 9999 else v
                out['vis_cap'] = v >= 9999
                seen_vis = True
                continue
            ms = re.match(r'^(M|P)?(\d+)(?:/(\d+))?SM$', t)               # US style: 10SM, M1/4SM, P6SM, and a whole number before a fraction
            if ms:
                whole, frac = ms.group(2), ms.group(3)
                val = float(whole) / float(frac) if frac else float(whole)
                if i >= 2 and re.match(r'^\d+$', toks[i - 2]) and frac:
                    val += float(toks[i - 2])
                out['vis_m'] = sm_to_m('P6' if ms.group(1) == 'P' else val)
                out['vis_cap'] = ms.group(1) == 'P'
                seen_vis = True
                continue
            if re.match(r'^\d$', t) and i < len(toks) and re.match(r'^\d+/\d+SM$', toks[i]):
                continue                                                  # the whole mile of '1 1/2SM'; the next token carries the rest
        md = re.match(r'^(\d{4})(NDV|NE|NW|SE|SW|N|E|S|W)$', t)
        if md:
            out['vis_min_m'] = 10000 if int(md.group(1)) >= 9999 else int(md.group(1))
            out['vis_min_dir'] = md.group(2)
            continue
        if RVR_RE.match(t):
            out['rvr'].append(t)
            continue
        if t in ('NSC', 'NCD', 'SKC', 'CLR'):
            out['nsc'] = True
            continue
        m = VV_RE.match(t)
        if m:
            out['vv_ft'] = int(m.group(1)) * 100 if m.group(1).isdigit() else -1
            continue
        m = CLOUD_RE.match(t)
        if m:
            out['clouds'].append({'cover': m.group(1), 'base_ft': int(m.group(2)) * 100 if m.group(2).isdigit() else None,
                                  'type': m.group(3) if m.group(3) in ('CB', 'TCU') else None})
            continue
        m = TEMP_RE.match(t)
        if m and out['temp_c'] is None and re.match(r'^M?\d{2}/', t):
            out['temp_c'] = _temp(m.group(1))
            out['dew_c'] = _temp(m.group(2))
            continue
        m = re.match(r'^Q(\d{4})$', t)
        if m:
            out['qnh_hpa'] = int(m.group(1))
            continue
        m = re.match(r'^A(\d{4})$', t)
        if m:
            out['altim_inhg'] = int(m.group(1)) / 100.0
            continue
        if t.startswith('RE') or t in ('WS', 'ALL', 'RWY') or re.match(r'^WS', t) or re.match(r'^(R\d{2}[LCR]?)$', t):
            continue                                                       # recent weather, wind shear, runway words: not read here
        w = parse_wx_token(t)
        if w:
            out['wx'].append(w)
            continue
        out['unparsed'].append(t)
    return out


def resolve_day(day, hh, mm, ref):
    """place a day-of-month, hour, minute near a reference datetime (naive UTC). Hour 24 means the start of the next day."""
    base = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    for delta_months in (0, -1, 1):
        y, mth = base.year, base.month + delta_months
        while mth < 1:
            mth += 12
            y -= 1
        while mth > 12:
            mth -= 12
            y += 1
        try:
            cand = dt.datetime(y, mth, day) + dt.timedelta(hours=hh, minutes=mm)
        except ValueError:
            continue
        if abs((cand - ref).total_seconds()) <= 16 * 86400:
            return cand
    return None


# ------------------------------------------------------------------------------------------------ AWC records
def _numeric(v):
    """a finite number, or None: a string, a boolean, a list or NaN where a number belongs is no value"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if v == v and v not in (float('inf'), float('-inf')) else None


def norm_metar_record(rec):
    """One aviationweather.gov JSON METAR record to the engine's observation row. The raw text is read first; the structured fields
    only fill what the text did not give."""
    if not isinstance(rec, dict):
        raise TypeError('a METAR record that is not an object')
    obs_t = None
    if rec.get('obsTime') is not None:
        try:
            obs_t = dt.datetime.fromtimestamp(float(rec['obsTime']), tz=dt.timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError, OverflowError):
            obs_t = None
    if obs_t is None and rec.get('reportTime'):
        try:
            obs_t = dt.datetime.fromisoformat(str(rec['reportTime']).replace('Z', '+00:00')).astimezone(dt.timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError, OSError):
            obs_t = None
    # every structured field is checked for its type before it is used: valid JSON with a string where a number belongs (wspd '', wgst '25', an icaoId that is a list) is a field that is
    # not there, never an exception that stops the run for as long as the record stays in the 30 hour answer (audit F3, V4)
    raw_ob = rec.get('rawOb') if isinstance(rec.get('rawOb'), str) else ''
    icao_j = rec.get('icaoId') if isinstance(rec.get('icaoId'), str) and rec.get('icaoId') else None
    p = parse_metar(raw_ob, ref=obs_t)
    t_utc = obs_t or p['time_utc']
    o = {'station': icao_j or p['station'], 'type': (rec.get('metarType') if isinstance(rec.get('metarType'), str) and rec.get('metarType') else None) or p['type'] or 'METAR', 'time_utc': t_utc,
         'time_local': utc_to_local(t_utc) if t_utc else None, 'raw': raw_ob, 'cavok': p['cavok'], 'wx': p['wx'],
         'clouds': p['clouds'], 'nsc': p['nsc'], 'vv_ft': p['vv_ft'], 'wind_vrb': p['wind_vrb'], 'wind_dir': p['wind_dir'], 'wind_kt': p['wind_kt'],
         'gust_kt': p['gust_kt'], 'vis_m': p['vis_m'], 'vis_cap': p['vis_cap'], 'vis_min_m': p['vis_min_m'], 'vis_min_dir': p['vis_min_dir'],
         'temp_c': p['temp_c'], 'dew_c': p['dew_c'], 'qnh_hpa': p['qnh_hpa'], 'source': 'raw text', 'nil': p['nil'],
         'altim_inhg': p['altim_inhg'], 'flt_cat': rec.get('fltCat') if isinstance(rec.get('fltCat'), str) else None}      # the A group in inches and the flight category the service sends (24 Oct 2026, conditions)
    filled = []
    vz = rec.get('visib')
    if o['vis_m'] is None and vz not in (None, '') and isinstance(vz, (int, float, str)) and not isinstance(vz, bool):
        o['vis_m'] = sm_to_m(vz)
        o['vis_cap'] = isinstance(vz, str) and str(vz).strip().endswith('+')
        filled.append('visibility')
    if o['temp_c'] is None and _numeric(rec.get('temp')) is not None:
        o['temp_c'] = _numeric(rec.get('temp'))
        filled.append('temperature')
    if o['dew_c'] is None and _numeric(rec.get('dewp')) is not None:
        o['dew_c'] = _numeric(rec.get('dewp'))
        filled.append('dew point')
    if o['wind_kt'] is None and _numeric(rec.get('wspd')) is not None and not o['wind_vrb']:
        o['wind_kt'] = _numeric(rec.get('wspd'))
        filled.append('wind speed')
    if o['wind_dir'] is None and not o['wind_vrb'] and _numeric(rec.get('wdir')) is not None:
        o['wind_dir'] = _numeric(rec.get('wdir'))
    if o['wind_dir'] is None and str(rec.get('wdir')).upper() == 'VRB':
        o['wind_vrb'] = True
    if o['gust_kt'] is None and _numeric(rec.get('wgst')) is not None:
        o['gust_kt'] = _numeric(rec.get('wgst'))
    if o['qnh_hpa'] is None and _numeric(rec.get('altim')) is not None:
        o['qnh_hpa'] = _numeric(rec.get('altim'))
    if not o['wx'] and not p['raw'] and isinstance(rec.get('wxString'), str) and rec.get('wxString'):
        for tok in rec['wxString'].split():
            w = parse_wx_token(tok)
            if w:
                o['wx'].append(w)
        filled.append('present weather')
    if not o['raw']:
        o['source'] = 'structured fields'
    elif filled:
        o['source'] = 'raw text, with ' + ', '.join(filled) + ' from the structured fields'
    o['flags'] = wx_flags(o['wx'])
    o['spread_c'] = (o['temp_c'] - o['dew_c']) if (o['temp_c'] is not None and o['dew_c'] is not None) else None
    o['wx_text'] = ' '.join(w['raw'] for w in o['wx'])
    return o


# ------------------------------------------------------------------------------------------------ TAF
def _taf_period(tok, ref):
    m = re.match(r'^(\d{2})(\d{2})/(\d{2})(\d{2})$', tok)
    if not m:
        return None
    d1, h1, d2, h2 = (int(x) for x in m.groups())
    a = resolve_day(d1, h1, 0, ref)
    b = resolve_day(d2, h2, 0, ref)
    if a and b and b < a:
        b = b + dt.timedelta(days=31)
    return a, b


def parse_taf(raw, issue=None):
    """Parse a raw TAF into groups: BASE, FM, BECMG, TEMPO, PROB30, PROB40 and 'PROB30 TEMPO'. Times are naive UTC."""
    toks = (raw or '').replace('=', ' ').split()
    res = {'raw': raw, 'station': None, 'issue_utc': issue, 'valid_from': None, 'valid_to': None, 'amd': False, 'groups': [], 'unparsed': []}
    i = 0
    while i < len(toks) and toks[i] in ('TAF', 'AMD', 'COR', 'RTD'):
        if toks[i] == 'AMD':
            res['amd'] = True
        i += 1
    if i < len(toks) and re.match(r'^[A-Z]{4}$', toks[i]):
        res['station'] = toks[i]
        i += 1
    if i < len(toks):
        m = re.match(r'^(\d{2})(\d{2})(\d{2})Z$', toks[i])
        if m:
            res['issue_utc'] = res['issue_utc'] or resolve_day(int(m.group(1)), int(m.group(2)), int(m.group(3)), issue or utc_now())
            i += 1
    ref = res['issue_utc'] or utc_now()
    if i < len(toks):
        per = _taf_period(toks[i], ref)
        if per:
            res['valid_from'], res['valid_to'] = per
            i += 1
    cur = {'type': 'BASE', 'prob': None, 'from': res['valid_from'], 'to': res['valid_to'], 'wx': [], 'clouds': [], 'vis_m': None, 'vis_cap': False,
           'cavok': False, 'nsw': False, 'wind_kt': None, 'gust_kt': None, 'wind_dir': None, 'vv_ft': None, 'raw': []}
    groups = [cur]

    def new(kind, prob, a, b):
        g = {'type': kind, 'prob': prob, 'from': a, 'to': b, 'wx': [], 'clouds': [], 'vis_m': None, 'vis_cap': False, 'cavok': False, 'nsw': False,
             'wind_kt': None, 'gust_kt': None, 'wind_dir': None, 'vv_ft': None, 'raw': []}
        groups.append(g)
        return g

    pending_prob = None
    while i < len(toks):
        t = toks[i]
        i += 1
        m = re.match(r'^PROB(\d{2})$', t)
        if m:
            pending_prob = int(m.group(1))
            # PROB30 2410/2415 or PROB30 TEMPO 2410/2415
            if i < len(toks) and toks[i] == 'TEMPO':
                i += 1
                per = _taf_period(toks[i], ref) if i < len(toks) else None
                if per:
                    i += 1
                cur = new('PROB%d TEMPO' % pending_prob, pending_prob, per[0] if per else None, per[1] if per else None)
            else:
                per = _taf_period(toks[i], ref) if i < len(toks) else None
                if per:
                    i += 1
                cur = new('PROB%d' % pending_prob, pending_prob, per[0] if per else None, per[1] if per else None)
            cur['raw'].append(t)
            pending_prob = None
            continue
        if t in ('BECMG', 'TEMPO'):
            per = _taf_period(toks[i], ref) if i < len(toks) else None
            if per:
                i += 1
            cur = new(t, None, per[0] if per else None, per[1] if per else None)
            continue
        m = re.match(r'^FM(\d{2})(\d{2})(\d{2})$', t)
        if m:
            a = resolve_day(int(m.group(1)), int(m.group(2)), int(m.group(3)), ref)
            cur = new('FM', None, a, res['valid_to'])
            continue
        if re.match(r'^T[XN]M?\d{2}/\d{4}Z$', t):
            continue
        cur['raw'].append(t)
        if t == 'CAVOK':
            cur['cavok'] = True
            cur['vis_m'] = 10000
            cur['vis_cap'] = True
            continue
        if t == 'NSW':
            cur['nsw'] = True
            continue
        m = WIND_RE.match(t)
        if m:
            d, s, g, unit = m.groups()
            k = {'KT': 1.0, 'MPS': 1.943844, 'KMH': 0.539957}[unit]
            cur['wind_dir'] = int(d) if d.isdigit() else None
            cur['wind_kt'] = round(int(s) * k) if s.isdigit() else None
            cur['gust_kt'] = round(int(g) * k) if g else None
            continue
        if re.match(r'^\d{4}$', t):
            v = int(t)
            cur['vis_m'] = 10000 if v >= 9999 else v
            cur['vis_cap'] = v >= 9999
            continue
        ms = re.match(r'^(P)?(\d+)(?:/(\d+))?SM$', t)
        if ms:
            val = float(ms.group(2)) / float(ms.group(3)) if ms.group(3) else float(ms.group(2))
            cur['vis_m'] = sm_to_m('P6' if ms.group(1) else val)
            cur['vis_cap'] = bool(ms.group(1))
            continue
        if t in ('NSC', 'NCD', 'SKC'):
            continue
        m = VV_RE.match(t)
        if m:
            cur['vv_ft'] = int(m.group(1)) * 100 if m.group(1).isdigit() else -1
            continue
        m = CLOUD_RE.match(t)
        if m:
            cur['clouds'].append({'cover': m.group(1), 'base_ft': int(m.group(2)) * 100 if m.group(2).isdigit() else None,
                                  'type': m.group(3) if m.group(3) in ('CB', 'TCU') else None})
            continue
        w = parse_wx_token(t)
        if w:
            cur['wx'].append(w)
            continue
        res['unparsed'].append(t)
    for k, g in enumerate(groups):
        g['flags'] = wx_flags(g['wx'])
        g['convective_cloud'] = any(c.get('type') in ('CB', 'TCU') for c in g['clouds'])
        g['wx_text'] = ' '.join(w['raw'] for w in g['wx'])
        g['raw'] = ' '.join(g['raw'])
    _persist(groups, res['valid_to'])
    res['groups'] = groups
    return res


def _persist(groups, valid_to):
    """BECMG and FM conditions hold until the next BECMG or FM group (or the end of the TAF); TEMPO and PROB groups only inside their own period."""
    changes = [g for g in groups if g['type'] in ('BECMG', 'FM') and g.get('from')]
    for g in groups:
        g['persist_to'] = g.get('to')
        if g['type'] in ('BECMG', 'FM') and g.get('from'):
            later = [c['from'] for c in changes if c['from'] > g['from']]
            g['persist_to'] = min(later) if later else (valid_to or g.get('to'))


def taf_from_json(rec):
    """Fallback when the raw TAF is missing or does not parse: build the same groups from the structured 'fcsts' list."""
    def ts(v):
        try:
            return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            return None
    res = {'raw': rec.get('rawTAF'), 'station': rec.get('icaoId'), 'issue_utc': ts(rec.get('issueTime')) if isinstance(rec.get('issueTime'), (int, float)) else None,
           'valid_from': ts(rec.get('validTimeFrom')), 'valid_to': ts(rec.get('validTimeTo')), 'amd': False, 'groups': [], 'unparsed': [], 'source': 'structured fields'}
    if res['issue_utc'] is None and rec.get('issueTime'):
        try:
            res['issue_utc'] = dt.datetime.fromisoformat(str(rec['issueTime']).replace('Z', '+00:00')).astimezone(dt.timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
    fc = rec.get('fcsts')
    for f in fc if isinstance(fc, list) else []:                   # a list of anything else, or entries that are not objects, give no groups, never an exception (audit V4)
        if not isinstance(f, dict):
            continue
        change = f.get('fcstChange')
        prob = f.get('probability') if isinstance(f.get('probability'), (int, float)) and not isinstance(f.get('probability'), bool) else None
        kind = 'BASE' if not change else (('PROB%s' % prob) if change == 'PROB' else str(change))
        wx = []
        for tok in str(f.get('wxString') or '').split():
            w = parse_wx_token(tok)
            if w:
                wx.append(w)
        vis = f.get('visib')
        vis = vis if isinstance(vis, (int, float, str)) and not isinstance(vis, bool) else None
        cl = f.get('clouds')
        clouds = [{'cover': c.get('cover'), 'base_ft': c.get('base'), 'type': c.get('type') if c.get('type') in ('CB', 'TCU') else None} for c in (cl if isinstance(cl, list) else []) if isinstance(c, dict)]
        g = {'type': kind, 'prob': prob, 'from': ts(f.get('timeFrom')), 'to': ts(f.get('timeTo')), 'wx': wx, 'clouds': clouds,
             'vis_m': sm_to_m(vis) if vis not in (None, '') else None, 'vis_cap': isinstance(vis, str) and vis.endswith('+'), 'cavok': False, 'nsw': False,
             'wind_kt': _numeric(f.get('wspd')), 'gust_kt': _numeric(f.get('wgst')), 'wind_dir': f.get('wdir') if isinstance(f.get('wdir'), (int, float)) else None,
             'vv_ft': _numeric(f.get('vertVis')), 'raw': ''}
        g['flags'] = wx_flags(wx)
        g['convective_cloud'] = any(c.get('type') in ('CB', 'TCU') for c in clouds)
        g['wx_text'] = ' '.join(w['raw'] for w in wx)
        res['groups'].append(g)
    _persist(res['groups'], res['valid_to'])
    return res


def taf_flags(taf, now_utc, hours=24):
    """What the TAF says for the next `hours` from now_utc (naive UTC): rain, fog, mist, haze, dust, thunder and the lowest visibility.
    A group counts when its period overlaps the window. PROB groups are kept apart: rainForecast includes them,
    rainForecastFirm does not."""
    end = now_utc + dt.timedelta(hours=hours)
    out = dict(rainForecast=False, rainForecastFirm=False, rainProbMax=None, thunderForecast=False, convectiveCloud=False, fogForecast=False,
               fogForecastFirm=False, mistForecast=False, hazeForecast=False, dustForecast=False, minVisM=None, minVisType=None, groupsInWindow=0, vcShTs=False)
    for g in taf.get('groups', []):
        a, b = g.get('from'), g.get('persist_to') or g.get('to')
        if a is None or b is None:
            continue
        if b <= now_utc or a >= end:
            continue
        out['groupsInWindow'] += 1
        f = g['flags']
        firm = not (g.get('type') or '').startswith('PROB')
        if f['rain'] or f['shower'] or f['thunder']:
            out['rainForecast'] = True
            if firm:
                out['rainForecastFirm'] = True
            if g.get('prob') is not None:
                out['rainProbMax'] = max(out['rainProbMax'] or 0, g['prob'])
        if f['thunder']:
            out['thunderForecast'] = True
        if f['vc_ts'] or f['vc_sh']:
            out['vcShTs'] = True
        if g.get('convective_cloud'):
            out['convectiveCloud'] = True
        fogg = f['fog'] or (g.get('vis_m') is not None and g['vis_m'] < 1000) or (g.get('vv_ft') is not None)
        if fogg:
            out['fogForecast'] = True
            if firm:
                out['fogForecastFirm'] = True
        if f['mist']:
            out['mistForecast'] = True
        if f['haze']:
            out['hazeForecast'] = True
        if f['dust']:
            out['dustForecast'] = True
        if g.get('vis_m') is not None and (out['minVisM'] is None or g['vis_m'] < out['minVisM']):
            out['minVisM'] = g['vis_m']
            out['minVisType'] = g['type']
    return out
