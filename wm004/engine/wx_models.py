# -*- coding: utf-8 -*-
"""Model reading for the WM004 weather engine: Open-Meteo responses to per model, per point and ensemble figures, the rain level rule
and the fog diagnostics. Pure functions: no network, no files. Status of everything here: Model forecast (Proposed); the hours before now
are Model analysis (Recomputed).

Conventions kept from the old feed so no reader breaks: the value at a time label is the rain of the hour that ends at that label;
the forward windows start at the label of the current clock hour; null is never zero (a window that holds a null has no total).
Rain below zero (ECMWF 0.25 degree returns -0.1 mm at times) is clamped to zero and counted."""
import datetime as dt
from wx_util import nz, med, avg, r1, r1_safe, THRESH6

OM_VARS = ['precipitation', 'wind_gusts_10m', 'cape', 'visibility', 'relative_humidity_2m', 'dew_point_2m', 'temperature_2m', 'wind_speed_10m',
           'cloud_cover_low', 'weather_code', 'pressure_msl']          # pressure_msl added 24 Oct 2026 (weather channel): the conditions bands and the pressure tendency read it
# the seven models of the calibrated vote (calibration.json, proposed.rule); the two extras are read and shown, they do not vote
VOTE_MODELS = ['ecmwf_ifs', 'ecmwf_ifs025', 'gfs_seamless', 'icon_seamless', 'gem_seamless', 'meteofrance_seamless', 'ukmo_seamless']
EXTRA_MODELS = ['jma_seamless', 'ecmwf_aifs025_single']
ALL_MODELS = VOTE_MODELS + EXTRA_MODELS
WIN = 72
MAX_MM_HOUR = 350.0          # more rain in one hour than any hour on record: a value above it is a fault of the feed and is treated as no value (audit F15)
PROPOSED = {'watch': {'mm6h': 5.0, 'votes': 2, 'of': 7, 'horizonH': 48}, 'high': {'mm6h': 25.0, 'votes': 3, 'of': 7, 'horizonH': 48}}
LEVEL_RANK = {'none': 0, 'watch': 1, 'high': 2}
FOG_RANK = {'none': 0, 'watch': 1, 'alert': 2, 'severe': 3}
FOG_NAMES = ['none', 'watch', 'alert', 'severe']
KT_KMH = 1.852


def _ser(ms, name):
    """a series of a model as a list; anything else (a string, a number, a missing key) is no series"""
    v = ms.get(name) if isinstance(ms, dict) else None
    return v if isinstance(v, list) else []


def split_models(data, models):
    """(time axis, {model: {variable: list} or None when the model is not in the response}). Handles the suffixed keys of a multi model call
    and the plain keys of a single model call. Valid JSON of the wrong shape (a list for the body, a string for the hourly block, a time axis that is not a list
    of strings, a series that is not a list) gives an empty axis or a missing series, never an exception: one damaged answer must not stop the other points (WX-10)."""
    h = data.get('hourly') if isinstance(data, dict) else None
    h = h if isinstance(h, dict) else {}
    times = h.get('time')
    times = times if isinstance(times, list) and all(isinstance(t, str) for t in times) else []
    out = {}
    for m in models:
        d = {}
        for v in OM_VARS:
            k = v + '_' + m
            if isinstance(h.get(k), list):
                d[v] = h[k]
        out[m] = d or None
    if len(models) == 1 and out[models[0]] is None and isinstance(h.get('precipitation'), list):
        out[models[0]] = {v: h[v] for v in OM_VARS if isinstance(h.get(v), list)}
    return times, out


def zone_check(data):
    """(ok, text, stated): whether an Open-Meteo answer is on Ras Al Khaimah time. The engine asks for timezone=Asia/Dubai and reads every time label as local time; an answer on
    another axis (the parameter ignored, a UTC answer: utc_offset_seconds 0) would slide the 72 hour window by hours and drop a storm that is about to arrive (audit F2). Not ok when the answer
    states a time zone or an offset that is not Asia/Dubai or 14400 seconds. stated is False when the answer states neither (it cannot be checked)."""
    if not isinstance(data, dict):
        return True, None, False
    tz, off = data.get('timezone'), data.get('utc_offset_seconds')
    if tz is None and off is None:
        return True, None, False
    bad = []
    if tz is not None and tz != 'Asia/Dubai':
        bad.append('timezone %s' % str(tz)[:30])
    if off is not None and (isinstance(off, bool) or not isinstance(off, (int, float)) or off != 14400):
        bad.append('utc_offset_seconds %s' % str(off)[:12])
    if bad:
        return False, 'the answer is not on Ras Al Khaimah time (%s; Asia/Dubai, 14400 s expected): its hours would be read at the wrong time of day, so it is not used' % ', '.join(bad), True
    return True, None, True


def first_index(times, now_label):
    for i, t in enumerate(times):
        if isinstance(t, str) and t >= now_label:
            return i
    return None


def _wsum(seg_list, a, n):
    seg = seg_list[a:a + n]
    if len(seg) < n or any(v is None for v in seg):
        return None
    return sum(seg)


def _wsum_lb(seg_list, a, n):
    """a lower bound of the sum of a window: the hours that have a value (None when the window is short or has no value at all). Used only to find the worst
    window: one missing hour inside a heavy burst must not remove every window that holds it (audit D12); totals keep the strict _wsum, where null is never zero."""
    seg = seg_list[a:a + n]
    vals = [v for v in seg if v is not None]
    if len(seg) < n or not vals:
        return None
    return sum(vals)


def analyse_model(times, ms, i0):
    """One model at one point. Returns a dict; 'usable' is True only when at least 90 percent of the next 72 hours are not null."""
    r = {'usable': False, 'reason': None}
    if ms is None:
        r['reason'] = 'model not in the response'
        return r
    pr = ms.get('precipitation') if isinstance(ms, dict) else None
    if not isinstance(pr, list):
        r['reason'] = 'no precipitation series'
        return r
    raw = [nz(v) for v in pr]
    neg = sum(1 for v in raw if v is not None and v < 0)
    imp = sum(1 for v in raw if v is not None and v > MAX_MM_HOUR)
    p = [None if v is None else (0.0 if v < 0 else v) for v in raw]
    p = [None if (v is not None and v > MAX_MM_HOUR) else v for v in p]       # an hour above any rain on record is a fault of the feed: no value, never a total of 1e308 (audit F15)
    fut = p[i0:i0 + WIN]
    fut += [None] * (WIN - len(fut))
    nn = sum(1 for v in fut if v is not None)
    r['nonNull72'] = round(nn / float(WIN), 3)
    r['negClamped'] = neg
    r['implausible'] = imp
    if nn < 0.9 * WIN:
        r['reason'] = 'only %d percent of the next 72 h has values' % round(100.0 * nn / WIN)
        return r
    r['usable'] = True
    r['next6'] = _wsum(fut, 0, 6)
    r['next24'] = _wsum(fut, 0, 24)
    r['next72'] = _wsum(fut, 0, 72)
    m6 = [_wsum_lb(fut, a, 6) for a in range(0, WIN - 5)]
    m6 = [x for x in m6 if x is not None]
    r['max6'] = max(m6) if m6 else None
    # the vote looks at bursts that START within the next 48 hours (a 30 mm burst that starts at hour 44 is a 48 hour burst, audit D11d)
    m6b = [_wsum_lb(fut, a, 6) for a in range(0, 48)]
    m6b = [x for x in m6b if x is not None]
    r['max6_48'] = max(m6b) if m6b else None
    # the model's own heaviest 24 hours, and the heaviest 24 hours that begin within the next 24 hours (the plan stage table): the figures of the stage are taken
    # per model and then the median, like the 6 hour figure, so that models that disagree on the hour do not dilute one another (audit V1)
    m24 = [_wsum_lb(fut, a, 24) for a in range(0, WIN - 23)]
    m24 = [x for x in m24 if x is not None]
    r['max24'] = max(m24) if m24 else None
    m24d = [_wsum_lb(fut, a, 24) for a in range(0, 24)]
    m24d = [x for x in m24d if x is not None]
    r['max24w24'] = max(m24d) if m24d else None
    vals = [(v, k) for k, v in enumerate(fut) if v is not None]
    if vals:
        v, k = max(vals, key=lambda x: (x[0], -x[1]))
        r['peakMm'] = v
        r['peakAt'] = times[i0 + k] if i0 + k < len(times) else None
    else:
        r['peakMm'], r['peakAt'] = None, None
    g = [nz(x) for x in _ser(ms, 'wind_gusts_10m')][i0:i0 + 24]
    g = [x for x in g if x is not None]
    r['gustMax24'] = max(g) if g else None
    c = [nz(x) for x in _ser(ms, 'cape')][i0:i0 + 24]
    c = [max(0.0, x) for x in c if x is not None]
    r['capeMax24'] = max(c) if c else None
    r['_p'] = p
    r['_fut'] = fut
    r['_gust'] = [nz(x) for x in _ser(ms, 'wind_gusts_10m')]
    return r


def public_model(r):
    """the figures of one model that go into weather.json (no series)"""
    if not r.get('usable'):
        return {'usable': False, 'reason': r.get('reason')}
    out = {'usable': True, 'next6': r1(r['next6']), 'next24': r1(r['next24']), 'next72': r1(r['next72']), 'max6': r1(r['max6']), 'peakAt': r['peakAt'],
           'peakMm': r1(r['peakMm']), 'gustMax24': None if r['gustMax24'] is None else round(r['gustMax24']),
           'capeMax24': None if r['capeMax24'] is None else round(r['capeMax24']), 'nonNull72': r['nonNull72']}
    if r.get('implausible'):
        out['implausibleHours'] = r['implausible']
    return out


def stats(vals, safe=False):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    rf = r1_safe if safe else r1                    # safe: never rounded up across a threshold (audit DISPLAY-1)
    return {'n': len(vals), 'min': rf(min(vals)), 'median': rf(med(vals)), 'max': rf(max(vals)), 'mean': r1(avg(vals)), 'spread': r1(max(vals) - min(vals))}


def hourly_band(usable):
    """per hour of the next 72: median, lowest and highest of the usable models (None where no model has a value)"""
    mid, lo, hi = [], [], []
    for k in range(WIN):
        xs = [m['_fut'][k] for m in usable if m['_fut'][k] is not None]
        mid.append(med(xs))
        lo.append(min(xs) if xs else None)
        hi.append(max(xs) if xs else None)
    return mid, lo, hi


def class_of(x, watch, high):
    return 2 if x >= high else (1 if x >= watch else 0)


def ensemble(usable, thresholds=THRESH6):
    """ensemble figures over the usable models of one point"""
    e = {'models': len(usable)}
    for k in ('next6', 'next24', 'next72', 'max6'):
        e[k] = stats([m[k] for m in usable], safe=(k == 'max6'))
    m6 = [m['max6'] for m in usable if m['max6'] is not None]
    e['atWatch'] = sum(1 for x in m6 if x >= thresholds['watch'])
    e['atHigh'] = sum(1 for x in m6 if x >= thresholds['high'])
    e['atProposedWatch'] = sum(1 for x in m6 if x >= PROPOSED['watch']['mm6h'])
    if m6:
        cls = [class_of(x, thresholds['watch'], thresholds['high']) for x in m6]
        e['agree'] = round(max(cls.count(c) for c in (0, 1, 2)) / float(len(cls)), 2)
    else:
        e['agree'] = None
    g = [m['gustMax24'] for m in usable if m['gustMax24'] is not None]
    e['gustMax24'] = stats(g)
    c = [m['capeMax24'] for m in usable if m['capeMax24'] is not None]
    e['capeMax24'] = stats(c)
    return e


def summarise_series(times, i0, mid):
    """the old summarise(): next6, next24, next72, max6, peak, from the median series; windows that hold a missing hour have no total"""
    def s(n):
        seg = mid[:n]
        if len(seg) < n or any(x is None for x in seg):
            return None                                   # an hour with no value in every model: the total is not known, never a sum of the hours that were read (audit D13c)
        return round(sum(seg), 1)
    m6 = []
    for a in range(0, max(1, len(mid) - 5)):
        w = mid[a:a + 6]
        if len(w) == 6 and all(x is not None for x in w):
            m6.append(sum(w))
    m24 = []
    for a in range(0, max(1, len(mid) - 23)):
        w = mid[a:a + 24]
        if len(w) == 24 and all(x is not None for x in w):
            m24.append(sum(w))
    m24d = []
    for a in range(0, min(24, max(1, len(mid) - 23))):        # the heaviest 24 hours that begin within the next 24 hours: "rain expected in 24 hours" of the plan stage table
        w = mid[a:a + 24]
        if len(w) == 24 and all(x is not None for x in w):
            m24d.append(sum(w))
    vals = [(v, k) for k, v in enumerate(mid) if v is not None]
    out = {'next6': s(6), 'next24': s(24), 'next72': s(72), 'max6': round(max(m6), 1) if m6 else None, 'max24': round(max(m24), 1) if m24 else None,
           'max24Within24': round(max(m24d), 1) if m24d else None,
           'max6Raw': round(max(m6), 6) if m6 else None, 'max24Raw': round(max(m24), 6) if m24 else None, 'max24Within24Raw': round(max(m24d), 6) if m24d else None}
    if vals:
        v, k = max(vals, key=lambda x: (x[0], -x[1]))
        out['peakAt'] = times[i0 + k]
        out['peakMm'] = round(v, 1)
    else:
        out['peakAt'], out['peakMm'] = None, None
    out['hourly'] = [[times[i0 + k], None if mid[k] is None else round(mid[k], 1)] for k in range(min(WIN, len(mid))) if i0 + k < len(times)]
    return out


def summarise_models(times, i0, usable, mid):
    """The figures of one point that set the level, the stage and the sheets (audit V1, WXR-01). summarise_series works on the hourly median series: the median of the models taken
    hour by hour, then summed. When the models agree on the amount but not on the hour (normal for convective rain) that series is thin: nine models that each forecast 30 mm in
    one hour, at nine different hours, give an hourly median of 0 in every hour. The plan thresholds are therefore applied to the median ACROSS the models of each model's own
    figure (its own worst 6 h, its own heaviest 24 h, its own totals), as the documentation always said ('the median of the usable models'). The figure used is the higher of
    that and the one from the hourly median series, so no figure is ever lower than the old reading gave. The hourly median series stays in 'hourly' for display, and both
    figures are kept apart (max6ModelMedian, max6HourlyMedian) so that a reader can see which is which. Unrounded values (…Raw) decide a level (audit WXR-08)."""
    base = summarise_series(times, i0, mid)
    out = dict(base)

    def mm(key):
        return med([m[key] for m in usable if m.get(key) is not None])

    def pick(model_med, hourly_raw):
        c = [x for x in (model_med, hourly_raw) if x is not None]
        return max(c) if c else None
    for key, mk, bk, rk in (('max6', 'max6', 'max6Raw', 'max6'), ('max24', 'max24', 'max24Raw', 'max24'), ('max24Within24', 'max24w24', 'max24Within24Raw', 'max24Within24')):
        pm = mm(mk)
        x = pick(pm, base.get(bk))
        out[key] = r1_safe(x)                                      # never rounded up across a threshold: the level is decided on the unrounded figure (audit DISPLAY-1)
        out[key + 'Raw'] = round(x, 6) if x is not None else None
        out[key + 'ModelMedian'] = r1(pm)
        out[key + 'HourlyMedian'] = base.get(key)
    for k in ('next6', 'next24', 'next72'):
        v = mm(k)
        out[k] = r1(v) if v is not None else base[k]            # the median of the models' own totals (as the season record does); the hourly sum only when no model has a whole window
    pk = [(m['peakMm'], m['peakAt']) for m in usable if m.get('peakMm') is not None]
    if pk:
        out['peakMm'] = r1(med([a for a, _ in pk]))
        labs = sorted(b for _, b in pk if b)
        out['peakAt'] = labs[len(labs) // 2] if labs else base['peakAt']
    out['figureBasis'] = ('the median across the usable models of each model\'s own figure, or the figure of the hourly median series when that is higher; hourly is the median of the models hour by hour')
    return out


def day_labels(day):
    """the 24 time labels whose hours make up a calendar day: the value at a label is the hour that ends there, so day D holds
    D 01:00 up to D+1 00:00"""
    d0 = dt.datetime.strptime(day, '%Y-%m-%d')
    return [(d0 + dt.timedelta(hours=h)).strftime('%Y-%m-%dT%H:00') for h in range(1, 25)]


def day_totals(times, p_by_model, days):
    """{day: {'median','max','n'}} over the models that have all 24 hours of that day. p_by_model: {model: clamped hourly list}"""
    idx = {t: i for i, t in enumerate(times)}
    out = {}
    for d in days:
        labs = day_labels(d)
        tot = []
        for m, p in p_by_model.items():
            seg = [p[idx[l]] if l in idx and idx[l] < len(p) else None for l in labs]
            if all(v is not None for v in seg):
                tot.append(sum(seg))
        m_raw = med(tot)
        out[d] = {'median': r1(m_raw), 'max': r1(max(tot)) if tot else None, 'n': len(tot), 'medianRaw': m_raw, 'maxRaw': max(tot) if tot else None}
    return out


def past_series(times, i0, usable, hours=48):
    """model analysis of the hours before the current label: {label: median mm over the usable models} (Model analysis, Recomputed)"""
    out = {}
    lo = max(0, i0 - hours)
    for k in range(lo, i0 + 1):
        xs = []
        for m in usable:
            p = m['_p']
            if k < len(p) and p[k] is not None:
                xs.append(p[k])
        if xs and k < len(times):
            out[times[k]] = med(xs)
    return out


def model_series(entries):
    """per model series from the hour entries of an event or a day: an entry is a list of model values (ALL_MODELS order, None where a model has no value) or a plain number (the median alone,
    kept by an older record, which counts for every model). Returns (number of models, [one series per model])."""
    n = max([len(e) for e in entries if isinstance(e, list)] or [0])
    if n == 0:
        return 0, []
    out = []
    for j in range(n):
        out.append([(e[j] if j < len(e) else None) if isinstance(e, list) else e for e in entries])
    return n, out


def entries_total(entries):
    """the median over the models of each model's own total over the entries; a model with a missing hour has no total. None when no model has every hour."""
    n, ser = model_series(entries)
    tots = [sum(s) for s in ser if s and all(x is not None for x in s)]
    return med(tots) if tots else None


def past_vectors(times, i0, by_name, hours=48):
    """model analysis of the hours before the current label, kept model by model: {label: [mm per model in ALL_MODELS order, None where a model has no value]}. The event register adds
    the hours up model by model and takes the median of the totals, so that the accumulation of an event is not diluted when the models disagree on the hour (audit WXR-02).
    by_name: {model: analysed record} (usable models only)."""
    out = {}
    lo = max(0, i0 - hours)
    for k in range(lo, i0 + 1):
        if k >= len(times):
            break
        vec, any_v = [], False
        for m in ALL_MODELS:
            r = by_name.get(m)
            p = r['_p'] if r else None
            v = p[k] if (p is not None and k < len(p)) else None
            vec.append(None if v is None else round(v, 4))
            any_v = any_v or v is not None
        if any_v:
            out[times[k]] = vec
    return out


# ------------------------------------------------------------------------------------------------ rain level
def _kth_highest(vals, k):
    vs = sorted(vals, reverse=True)
    return vs[k - 1] if len(vs) >= k else (vs[-1] if vs else 0.0)


def rain_level(point_reads, thresholds=THRESH6, rule='plan'):
    """The model level for rain: none, watch, high or unknown (fail safe). The plan rule is the default: modelLevel follows the plan thresholds until the owner
    decides on the proposed vote (audit logic D1). The vote is always worked out and published as voteLevel, so the difference is visible, and rule='proposed' applies it.

    Plan rule: watch at 10 mm and high at 25 mm in 6 h at any point, on the ensemble median of the usable models, next 72 h. The figure of a point is the median across the models
    of each model's own worst 6 h (summarise_models), or the worst 6 h of the hourly median series when that is higher: models that agree on the amount but not on the hour do not dilute
    one another (audit V1). A vote above the plan rule is not applied here but is published (voteLevel) and the engine raises it as an alert, a stage flag and an actions row (audit V2).
    Proposed vote (calibration.json): HIGH when 3 of the 7 voting models reach 25 mm in 6 h at any point in the next 48 h, else WATCH when 2 reach 5 mm, with the plan
    thresholds on the median as a floor. The floor is the median: a lone model at 10 to 24.9 mm with the rest dry is none under either rule (the plan text of AL10 reads
    one model; the median is what the engine has always used, audit D11c).
    unknown, never none, when any of the plan points has fewer than 3 usable models, or no model answered: a failed read of one point must not read as no rain, so a
    storm over the Jebel Jais road cannot be hidden by an HTTP 500 there (audit WX-01). A watch or high found on thin data stands, only a none is withheld.
    worstMax6 is the 6 h figure that justifies the level under the rule in use (the median for the plan rule), so a consumer's own thresholds never contradict it, and None when the
    level is unknown (audit V3); worstMax6Median is always the ensemble median worst 6 h of the points that were read (None when none was).
    point_reads: list of {'idx','name','ok','models':{name: analysed},'point': summary or None}"""
    n_points = len(point_reads)
    usable_by_point = [[m for m in ALL_MODELS if (pr.get('models') or {}).get(m, {}).get('usable')] for pr in point_reads]
    pts3 = sum(1 for u in usable_by_point if len(u) >= 3)
    answered = any(usable_by_point)
    # plan rule on the median: the figure of a point is the median across the models of each model's own worst 6 h (or the hourly median series when that is higher), decided on the
    # unrounded value (audit V1, WXR-08)
    worst, worst_name, have_fig = 0.0, None, False
    for pr in point_reads:
        pt = pr.get('point')
        x = None
        if pt:
            x = pt.get('max6Raw') if pt.get('max6Raw') is not None else pt.get('max6')
        if x is not None:
            have_fig = True
            if x > worst:
                worst, worst_name = x, pr['name']
    plan_level = 'high' if worst >= thresholds['high'] else ('watch' if worst >= thresholds['watch'] else 'none')
    # vote
    votes = {}
    for m in VOTE_MODELS:
        best, where, n_pts = None, None, 0
        for pr in point_reads:
            r = (pr.get('models') or {}).get(m)
            if r and r.get('usable') and r.get('max6_48') is not None:
                n_pts += 1
                if best is None or r['max6_48'] > best:
                    best, where = r['max6_48'], pr['name']
        votes[m] = {'max6': r1(best), 'at': where, 'points': n_pts}
    voters = [m for m in VOTE_MODELS if votes[m]['points'] > 0]
    n5 = [m for m in voters if votes[m]['max6'] >= PROPOSED['watch']['mm6h']]
    n25 = [m for m in voters if votes[m]['max6'] >= PROPOSED['high']['mm6h']]
    vote_level = 'high' if len(n25) >= PROPOSED['high']['votes'] else ('watch' if len(n5) >= PROPOSED['watch']['votes'] else 'none')
    solo_note = ''
    if vote_level == 'none' and len(n25) == 1:
        # an added safeguard, not in the calibration: one model alone at the plan's high value is a watch, never silence
        vote_level = 'watch'
        solo_note = ' Added safeguard: one voting model (%s) reaches %g mm in 6 h, which is a watch on its own.' % (n25[0], PROPOSED['high']['mm6h'])
    vals = [votes[m]['max6'] for m in voters]
    if vote_level == 'high':
        vote_fig = _kth_highest([votes[m]['max6'] for m in n25], PROPOSED['high']['votes'])
    elif vote_level == 'watch':
        vote_fig = _kth_highest(vals, PROPOSED['watch']['votes']) if len(n5) >= PROPOSED['watch']['votes'] else _kth_highest(vals, 1)
    else:
        vote_fig = 0.0
    if rule == 'plan':
        level = plan_level
    else:
        level = max([vote_level, plan_level], key=lambda x: LEVEL_RANK[x])
    unread = [pr['name'].split(' (')[0] for pr, u in zip(point_reads, usable_by_point) if len(u) < 3]
    # hours of the next 24 for which no model has a value at a point: a hole in the feed. A burst in those hours would not show, so a level of none is withheld (audit NEW-3)
    gap_pts = {pr['name'].split(' (')[0]: pr['gap24'] for pr in point_reads if pr.get('ok') and isinstance(pr.get('gap24'), int) and pr['gap24'] > 0}
    reason = None
    if not answered:
        reason = 'no model answered at any point'
    elif pts3 < n_points:
        reason = 'only %d of %d points have 3 or more usable models (all %d are needed to call no rain; not read well enough: %s)' % (pts3, n_points, n_points, '; '.join(unread))
    elif rule != 'plan' and len(voters) < 3 and level == 'none':
        reason = 'only %d voting models answered (3 are needed to rule out a high)' % len(voters)
    elif gap_pts and level == 'none':
        reason = 'no model has a value for some hours of the next 24 (%s): a hole in the feed, so a burst in those hours cannot be ruled out' % '; '.join('%s %d h' % (k, v) for k, v in sorted(gap_pts.items()))
    final = level
    if level == 'none' and reason:
        final = 'unknown'
    quality = 'none' if not answered else ('degraded' if (pts3 < n_points or gap_pts) else ('partial' if any(len(u) < 5 for u in usable_by_point) else 'full'))
    worst_for_rule = worst if rule == 'plan' else max(worst, vote_fig)
    if rule == 'plan':
        rule_text = ('Plan rule: %g mm in 6 h is watch and %g mm in 6 h is high, on the ensemble median at any of the points, next 72 h: the median across the usable models of each model\'s own '
                     'worst 6 h (or the worst 6 h of the hourly median of the models when that is higher). The proposed vote of the calibration is worked out beside it (modelLevelProposedVote); '
                     'it is not applied until the department head decides, and a vote above the plan rule is raised as an alert, a flag on the stage and a row in the actions due now.'
                     % (thresholds['watch'], thresholds['high']))
        active = {'watch': thresholds['watch'], 'high': thresholds['high'], 'rule': 'plan thresholds on the median of the models'}
    else:
        rule_text = ('Proposed vote rule (calibration of 24 Oct 2026): high when %d of the %d voting models reach %g mm in 6 h at any point in the next %d h, else watch when %d reach %g mm; '
                     'floor: the plan thresholds (%g and %g mm in 6 h) on the ensemble median over 72 h. The plan values in thresholds are unchanged.'
                     % (PROPOSED['high']['votes'], PROPOSED['high']['of'], PROPOSED['high']['mm6h'], PROPOSED['high']['horizonH'], PROPOSED['watch']['votes'],
                        PROPOSED['watch']['mm6h'], thresholds['watch'], thresholds['high']))
        active = {'watch': PROPOSED['watch']['mm6h'], 'high': PROPOSED['high']['mm6h'], 'rule': 'proposed vote: 2 of 7 voting models reach the watch value, 3 of 7 the high value; plan thresholds on the median as a floor'}
    state = ('%d of %d voting models reach %g mm in 6 h, %d reach %g mm (models read: %s); %s; %d of %d points have 3 or more usable models.'
             % (len(n5), len(voters), PROPOSED['watch']['mm6h'], len(n25), PROPOSED['high']['mm6h'], ', '.join(voters) or 'none',
                ('ensemble median worst 6 h %.1f mm%s' % (worst, (' at ' + worst_name) if worst_name else '')) if have_fig else 'no point was read, so there is no worst 6 h figure',
                pts3, n_points))
    state += solo_note
    if rule == 'plan' and vote_level != 'none' and LEVEL_RANK[vote_level] > LEVEL_RANK[plan_level]:
        state += ' The proposed vote says %s; the plan rule says %s and is applied.' % (vote_level, plan_level)
    if final == 'unknown':
        state += ' Level unknown, not none: ' + reason + '.'
    elif reason and final != 'none':
        state += ' Coverage is thin (%s) but a %s found on the data read stands.' % (reason, final)
    if unread and final != 'unknown':
        state += ' Points with fewer than 3 usable models: ' + '; '.join(unread) + '.'
    # worstMax6 is null when the level is unknown (a zero printed beside "not read well enough" reads as no rain: audit V3); worstMax6Median is the figure of the points that were read
    if gap_pts and final != 'unknown':
        state += ' Hours of the next 24 with no value from any model (%s): a hole in the feed.' % '; '.join('%s %d h' % (k, v) for k, v in sorted(gap_pts.items()))
    return {'level': final, 'basis': rule_text + ' Now: ' + state, 'voteLevel': vote_level, 'planRuleLevel': plan_level,
            'worstMax6': None if (final == 'unknown' or not have_fig) else r1_safe(worst_for_rule), 'worstMax6Median': r1_safe(worst) if have_fig else None,
            'worstMax6Vote': r1_safe(vote_fig), 'worstAt': worst_name, 'gapHours24': gap_pts, 'votes': votes, 'voters': voters, 'reach5': n5, 'reach25': n25, 'pointsWith3': pts3, 'pointsTotal': n_points,
            'answered': answered, 'unknownReason': reason if final == 'unknown' else None, 'readingQuality': quality, 'rule': rule, 'activeThresholds': active}


def proposed_thresholds_block(level_info):
    rule = level_info.get('rule') or 'plan'
    return {'status': 'Proposed', 'source': 'data/weather/calibration.json (24 Oct 2026), rain and fog triggers calibrated on last season',
            'planValues': {'watch': THRESH6['watch'], 'high': THRESH6['high'], 'basis': 'the plan: mm in 6 h at any point; printed in the plan documents and the console and not changed here'},
            'proposed': {'watch': {'mm6h': PROPOSED['watch']['mm6h'], 'rule': '2 of the 7 voting models reach 5 mm in 6 h at any of the 8 points in the next 48 h'},
                         'high': {'mm6h': PROPOSED['high']['mm6h'], 'rule': '3 of the 7 voting models reach 25 mm in 6 h at any of the 8 points in the next 48 h (threshold unchanged)'},
                         'voters': VOTE_MODELS, 'extrasShownNotVoting': EXTRA_MODELS,
                         'ncm': 'an NCM warning (Reported, official) sets the hazard level at least as high as its colour, whatever the models say',
                         'safeguard': 'added here, not in the calibration: one voting model alone at 25 mm in 6 h in the next 48 h is a watch, never silence'},
            'evidence': 'On the reanalysis, watch (10 mm in 6 h) was reached on 6 days and high on 4, false alarm days 0 of 212; one day ahead the vote at 5 mm gave watch on 11 days, high on 4, false alarm days 0, and flagged 4 of 5 register events.',
            'applied': rule,
            'decision': ('The plan documents and the console keep 10 and 25 mm. modelLevel follows the plan rule (the median of the models) until the department head decides on the proposed vote; '
                         'modelLevelProposedVote shows what the vote says, so a difference is visible. Run the engine with --level-rule proposed to apply the vote.') if rule == 'plan' else
                        ('The proposed vote is applied to modelLevel (--level-rule proposed) and is labelled Proposed; the plan documents and the console keep 10 and 25 mm. '
                         'modelLevelPlanRule is the plan threshold result for comparison.'),
            'resultNow': {'proposedVoteLevel': level_info.get('voteLevel'), 'planRuleLevel': level_info.get('planRuleLevel'), 'reach5': level_info.get('reach5'), 'reach25': level_info.get('reach25'),
                          'appliedRule': rule}}


# ------------------------------------------------------------------------------------------------ fog from the models
def _is_night(hour):
    return hour >= 22 or hour <= 9


def fog_model(times, ms, i0, past_hours=48):
    """Fog and visibility figures of one model at one point for the next 72 h and the past 48 h (Proposed diagnostic).
    fog hour = model visibility under 1000 m, or weather code 45 or 48; mist or haze hour = visibility under 5000 m;
    diagnostic fog risk hour = temperature minus dew point at most 1.5 C, wind at most 10 km/h, humidity at least 93 percent, night or early morning.
    A visibility series that is constant (GFS holds 24140 m) or null is not information and is not used."""
    if ms is None:
        return None
    n = len(times)

    def ser(name):
        s = ms.get(name)
        return [nz(v) for v in s] if isinstance(s, list) and s else [None] * n
    vis, code, temp, dew, wind, rh, low = ser('visibility'), ser('weather_code'), ser('temperature_2m'), ser('dew_point_2m'), ser('wind_speed_10m'), ser('relative_humidity_2m'), ser('cloud_cover_low')
    vis = [None if (v is not None and v < 0) else v for v in vis]            # -1 is a sentinel for no value, not a visibility (audit WX-20)
    for lst in (vis, code, temp, dew, wind, rh, low):
        if len(lst) < n:
            lst.extend([None] * (n - len(lst)))
    fut_idx = range(i0, min(n, i0 + WIN))
    past_idx = range(max(0, i0 - past_hours), i0)
    vv = [vis[i] for i in fut_idx if vis[i] is not None]
    vis_ok = len(vv) >= 0.9 * WIN and (max(vv) - min(vv)) > 0
    out = {'visInformative': vis_ok, 'hasCode': any(c is not None for c in code[i0:i0 + WIN])}

    def fog_at(i):
        if code[i] is not None and int(code[i]) in (45, 48):
            return True
        return bool(vis_ok and vis[i] is not None and vis[i] < 1000)

    def mist_at(i):
        return bool(vis_ok and vis[i] is not None and vis[i] < 5000)

    def diag_at(i):
        if temp[i] is None or dew[i] is None or wind[i] is None or rh[i] is None:
            return False
        h = int(times[i][11:13])
        return (temp[i] - dew[i]) <= 1.5 and wind[i] <= 10.0 and rh[i] >= 93.0 and _is_night(h)
    for tag, idx in (('72', fut_idx), ('Past48', past_idx)):
        fog = [i for i in idx if fog_at(i)]
        mist = [i for i in idx if mist_at(i)]
        diag = [i for i in idx if diag_at(i)]
        out['fogHours' + tag] = len(fog)
        out['mistHours' + tag] = len(mist)
        out['diagHours' + tag] = len(diag)
        out['firstFog' + tag] = times[fog[0]] if fog else None
        out['lastFog' + tag] = times[fog[-1]] if fog else None
        best, run, prev = 0, 0, None
        for i in fog:
            run = run + 1 if prev is not None and i == prev + 1 else 1
            best = max(best, run)
            prev = i
        out['longest' + tag] = best
    out['fogHours24'] = len([i for i in range(i0, min(n, i0 + 24)) if fog_at(i)])
    out['mistHours24'] = len([i for i in range(i0, min(n, i0 + 24)) if mist_at(i)])
    out['diagHours24'] = len([i for i in range(i0, min(n, i0 + 24)) if diag_at(i)])
    vis_win = [(vis[i], i) for i in fut_idx if vis[i] is not None] if vis_ok else []
    if vis_win:
        v, k = min(vis_win)
        out['minVisM'] = round(v)
        out['minVisAt'] = times[k]
    else:
        out['minVisM'], out['minVisAt'] = None, None
    out['_vis72'] = [vis[i] if vis_ok else None for i in fut_idx]
    out['_spread'] = [(temp[i] - dew[i]) if (temp[i] is not None and dew[i] is not None) else None for i in range(n)]
    out['_wind'] = wind
    out['_rh'] = rh
    out['_low'] = low
    out['_code'] = code
    return out


def public_fog_model(f):
    if not f:
        return None
    keys = ('visInformative', 'fogHours24', 'fogHours72', 'fogHoursPast48', 'mistHours24', 'mistHours72', 'mistHoursPast48', 'diagHours24', 'diagHours72', 'diagHoursPast48',
            'firstFog72', 'lastFog72', 'longest72', 'minVisM', 'minVisAt')
    return {k: f.get(k) for k in keys}


def fog_ensemble(fogs):
    """ensemble figures of the fog diagnostics over the models of one point. fogs: {model: fog_model dict}"""
    ms = {m: f for m, f in fogs.items() if f}
    vis_models = [m for m, f in ms.items() if f['visInformative']]
    code_models = [m for m, f in ms.items() if f['hasCode']]
    fog24 = [m for m in ms if ms[m]['fogHours24'] > 0]
    fog72 = [m for m in ms if ms[m]['fogHours72'] > 0]
    mist24 = [m for m in vis_models if ms[m]['mistHours24'] > 0]
    diag24 = [m for m in ms if ms[m]['diagHours24'] > 0]
    firsts = sorted(f['firstFog72'] for f in ms.values() if f['firstFog72'])
    lasts = sorted(f['lastFog72'] for f in ms.values() if f['lastFog72'])
    minv = [(f['minVisM'], f['minVisAt']) for f in ms.values() if f['minVisM'] is not None]
    mv = min(minv) if minv else (None, None)
    denom = max(1, len(ms))
    return {'models': len(ms), 'visModels': vis_models, 'codeModels': len(code_models), 'fog24': fog24, 'fog72': fog72, 'mist24': mist24, 'diag24': diag24,
            'agreeFog72': round(len(fog72) / float(denom), 2), 'firstFog': firsts[0] if firsts else None, 'lastFog': lasts[-1] if lasts else None,
            'minVisM': mv[0], 'minVisAt': mv[1], 'longestH': max([f['longest72'] for f in ms.values()] or [0]),
            'note': 'visibility is real only for models that vary it (ecmwf_ifs, ukmo_seamless); the calibration found the visibility field catches 16 percent of fog nights, so it is a diagnostic and does not set the level'}


def vis_band(fogs):
    """per hour of the next 72: lowest, median and highest model visibility, rounded to 100 m (None where no model has real visibility)"""
    usable = [f for f in fogs.values() if f and f['visInformative']]
    lo, mid, hi = [], [], []
    for k in range(WIN):
        xs = [f['_vis72'][k] for f in usable if k < len(f['_vis72']) and f['_vis72'][k] is not None]
        if xs:
            lo.append(int(round(min(xs), -2)))
            mid.append(int(round(med(xs), -2)))
            hi.append(int(round(max(xs), -2)))
        else:
            lo.append(None)
            mid.append(None)
            hi.append(None)
    return lo, mid, hi


def fog_rule_levels(times, f, i0, back_h=30):
    """The calibrated forecast ladder on one model's dew point spread (Proposed, calibration.json fog.levels): at any hour 02:00 to 08:00 with wind
    10 kt or less, spread <= 1.0 C watch, <= 0.5 C alert, <= 0.25 C severe. Returns {date: (level 0..3, label, spread, wind_kmh)} for the days that
    have such hours between now minus back_h and the end of the window."""
    spread, wind = f['_spread'], f['_wind']
    out = {}
    for i in range(max(0, i0 - back_h), min(len(times), i0 + WIN)):
        hh = int(times[i][11:13])
        if not (2 <= hh <= 8):
            continue
        s, w = spread[i], wind[i]
        if s is None or w is None or w > 10 * KT_KMH:
            continue
        lv = 3 if s <= 0.25 else (2 if s <= 0.5 else (1 if s <= 1.0 else 0))
        d = times[i][:10]
        if d not in out or lv > out[d][0] or (lv == out[d][0] and lv > 0 and s < out[d][2]):
            out[d] = (lv, times[i], s, w)
    return out
