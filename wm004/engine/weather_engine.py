# -*- coding: utf-8 -*-
"""WM004 weather engine: reads the weather for the rain plan WA-PSD-PLN-05, records it, and ties it to the plan and the console.
(24 Oct 2026. Replaces the one model reader weather_feed.py, which is now a thin wrapper around this.)

What it does each run
  READ     Open-Meteo for each of the 8 plan points and every usable model (ecmwf_ifs, ecmwf_ifs025, gfs_seamless, icon_seamless, gem_seamless, meteofrance_seamless,
           ukmo_seamless, jma_seamless, ecmwf_aifs025_single), 2 past days and 8 forecast days, retried 3 times; aviation reports and forecasts of OMRK, OMFJ, OMSJ from
           aviationweather.gov; the NCM reading log (data/weather/ncm_log.jsonl) and, where it can, the NCM bulletin and warnings page (ncm_auto.py).
  LEVELS   rain: none, watch, high or unknown (the plan thresholds on the median of the models by default; the Proposed vote of 7 models is worked out beside it and is
           applied only with --level-rule proposed); fog: none, watch, alert, severe or unknown (the calibrated dew point spread ladder, the stations and NCM).
           unknown, never none, when too little was read (any plan point unread, no station reporting, an NCM page colour nobody has logged): a failed read must never read
           as no rain or no fog.
  RECORD   one line in ledger/YYYY-MM.jsonl (never rewritten), events.json (rain, fog and dust events as a state machine), season.json (from 1 Oct 2026),
           verification.json (the ledger against what happened), health.json. A record file that exists and cannot be read is kept aside and reported, never overwritten.
  LINK     interaction.json: for each sector S1 to S9 its rain and fog level beside what the plan places there, the stage the weather alone would call (with a fog line beside
           it), the actions the weather asks of the plan (with a fog row), and last season's reports. The console stage and the plan stage table stay the owners of the stage.
  WRITE    weather.json (schema 2, written atomically; every key the console, the plan builders and the public card read is kept). It is written before the bookkeeping that
           follows it (the ledger line, health.json), and a failure of any bookkeeping file is an alert in weather.json, never a reason to leave the weather unpublished.

Exit code 0: weather.json was written. 2: weather.json was written but no model answered at any point (modelLevel and rainLevel are unknown; the NCM, station and fog blocks
are fresh). 1: nothing was published (weather.json could not be written, another run holds the lock, or an unexpected error); the previous weather.json is then left as it was.

Second review of 24 Oct 2026 (V1 to V12, F2 to F21, WXR-01 to WXR-13; each has a test named review ... in test_weather.py): the plan thresholds are applied to the median across the models of each
model's own figure; a proposed vote above the plan rule is an alert, a stage flag and an actions row; worstMax6 is null when the level is unknown; an answer on another time axis is not used;
a hazard that a pending page colour, a damaged state file or an unreadable log line could be about is unknown until a reading that names it is logged; fog is unknown when no west coast station reports.

Usage: python weather_engine.py [--out PATH] [--data-dir PATH] [--fixture DIR] [--now ISO] [--no-write-ledger] [--dry-run] [--level-rule plan|proposed] [--wait-lock SECONDS]
All times are Ras Al Khaimah local time (UTC+4). Status words: Model forecast (Proposed), Model analysis (Recomputed), Reported, Reported official, Verified.
Standard library only. See data/weather/SCHEMA.md for every file and key."""
import os, sys, json, math, re, time, socket, ssl, argparse, subprocess, threading, http.client, datetime as dt, urllib.request, urllib.parse, urllib.error
from wx_util import (POINTS, THRESH6, HERE, DEFAULT_DATA_DIR, DEFAULT_OUT, TZ_NAME, now_local, utc_now, parse_local, f_local, f_iso, hour_label, floor_hour, hours_between,
                     read_json, atomic_write_json, atomic_write_text, med, r1, short_text, nz, list_quarantined, clean_stale_tmp, pid_alive, scrub_text)
import wx_models as M
import wx_obs as O
import wx_events as E
import wx_season as S
import wx_interact as I
import ncm_log as N
import ncm_auto as A
import weather_verify as V
import wx_cond as C
import wx_condblock as K
import wx_condrec as R
import wx_wind as WF

UA = 'WM004-weather-engine/2 (PSD Waste Management, Ras Al Khaimah; internal use)'
OM_FORECAST = 'https://api.open-meteo.com/v1/forecast'
AWC = 'https://aviationweather.gov/api/data/'
PAST_DAYS, FORECAST_DAYS = 2, 8
OBS_HOURS = 30
FOG_PREFIXES = I.FOG_POINT_PREFIXES
RULE_MODELS = ['ecmwf_ifs', 'ecmwf_ifs025']
FOG_NAMES = M.FOG_NAMES
SCHEMA = 2
SKEW_LIMIT_MIN = 10          # a laptop clock more than this many minutes off the servers' is replaced by the servers' time for the run (audit WX-06)
SOURCE_TEXT = ('Open-Meteo multi model forecast (ECMWF IFS, GFS, ICON, GEM, Meteo-France, UK Met Office, JMA, AIFS), model forecast, not an official warning; '
               'aviation reports from aviationweather.gov (Reported); NCM readings from ncm_log.jsonl (Reported, official); '
               'conditions (wind, humidity, temperature, pressure, cloud, visibility, CAPE, dust): Open-Meteo best_match, the CAMS air quality model and the wind field on a 0.1 degree grid (Model forecast, Proposed) and the aviation reports (Reported)')
WEATHER_MAX_BYTES = 255000          # weather.json stays under 260 KB (24 Oct 2026: the conditions block was added; it was 220 KB before)
CONDITIONS_MAX_BYTES = 198000       # conditions.json stays under 200 KB (200,000 bytes; 2 KB of margin)


# ------------------------------------------------------------------------------------------------ fetching
class Fetcher:
    """One place for every outside call: retries, a time budget, a circuit breaker for a dead host, and fixture mode (every answer read from a saved file).
    Never raises: a failure comes back as (None, info) with the reason in plain words. A body cut short, a body of the wrong kind and an answer that takes too long
    are failed attempts like any other (audit WX-09, WX-19). The total time of one attempt is bounded inside the read, not only the idle time of the socket."""

    def __init__(self, fixture_dir=None, timeout=25, retries=3, backoff=(1.5, 3.0, 6.0), sleep=time.sleep, deadline_s=300, breaker_after=3, opener=None, attempt_cap_s=60):
        self.fixture_dir = fixture_dir
        self.timeout, self.retries, self.backoff, self.sleep = timeout, retries, backoff, sleep
        self.deadline = time.monotonic() + deadline_s
        self.attempt_cap_s = attempt_cap_s
        self.breaker_after = breaker_after
        self.opener = opener or urllib.request.urlopen
        self.calls = []
        self.fail_streak = {}
        self.rate_streak = {}                 # calls in a row that ended in HTTP 429 (a rate limit), by host: after breaker_after of them the host is left alone for the rest of the run (25 Oct 2026, stress test)
        self.server_dates = []
        self.cache = {}                       # successful answers of this run, so a run that corrects its clock reads the same answers again without a new call

    def has(self, key):
        return bool(self.fixture_dir) and any(os.path.exists(os.path.join(self.fixture_dir, key + ext)) for ext in ('.json', '.bin', '.txt'))

    def _done(self, info, t0):
        info['ms'] = int((time.monotonic() - t0) * 1000)
        self.calls.append(info)
        return info

    def _read_body(self, r, attempt_deadline):
        """the body of an answer, with the total time of the attempt bounded: a server that sends a byte a second cannot hold the run for ever (audit WX-19)"""
        rd = getattr(r, 'read1', None)
        if rd is None:
            return r.read()
        chunks = []
        while True:
            if time.monotonic() > attempt_deadline:
                raise TimeoutError('the answer took longer than the time allowed')
            chunk = rd(65536)
            if not chunk:
                break
            chunks.append(chunk)
        body = b''.join(chunks)
        if getattr(r, 'length', None):
            raise http.client.IncompleteRead(body, r.length)         # the answer ended before the length it announced
        return body

    def _open_bounded(self, req, timeout, attempt_deadline):
        """urlopen with the time of the whole attempt bounded, the answer headers included. The socket timeout is the time between two bytes, so a server (or a proxy) that sends one
        header line a second never trips it and could hold the run past its budget (audit F20). The open runs in a helper thread; when the attempt time is used up the call fails with
        a timeout and the late answer, if it ever comes, is closed unread."""
        box = {}

        def work():
            try:
                r = self.opener(req, timeout=timeout)
            except BaseException as e:                       # carried to the caller, whatever it was
                box['e'] = e
                return
            box['r'] = r
            if box.get('abandoned'):
                try:
                    r.close()
                except Exception:
                    pass
        left = attempt_deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('the time allowed for the answer was used up')
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(left)
        if th.is_alive():
            box['abandoned'] = True
            raise TimeoutError('the answer did not arrive within the time allowed')
        if 'e' in box:
            raise box['e']
        return box['r']

    def _get(self, key, url, mode, headers=None):
        t0 = time.monotonic()
        host = urllib.parse.urlparse(url).netloc
        info = {'key': key, 'host': host, 'status': None, 'attempts': 0, 'error': None}
        if self.fixture_dir:
            for ext in (('.json',) if mode == 'json' else ('.bin', '.json')):
                p = os.path.join(self.fixture_dir, key + ext)
                if os.path.exists(p):
                    try:
                        with open(p, 'rb') as f:
                            body = f.read()
                        info['status'] = 200
                        if mode == 'json':
                            data = json.loads(body.decode('utf-8'))
                            if isinstance(data, dict) and '__error__' in data:
                                info['status'], info['error'] = data.get('__status__', 500), str(data['__error__'])[:120]
                                return None, self._done(info, t0)
                            if isinstance(data, dict) and data.get('__status__') == 204:
                                info['status'], info['error'] = 204, 'no data (HTTP 204)'
                                return None, self._done(info, t0)
                            return data, self._done(info, t0)
                        return body, self._done(info, t0)
                    except (OSError, ValueError) as e:
                        info['error'] = 'fixture unreadable: %s' % str(e)[:80]
                        return None, self._done(info, t0)
            info['error'] = 'no fixture'
            return None, self._done(info, t0)
        if (mode, url) in self.cache:
            data, info0 = self.cache[(mode, url)]
            return data, dict(info0, cached=True)
        for attempt in range(1, self.retries + 1):
            now_m = time.monotonic()
            if now_m > self.deadline:
                info['error'] = 'time budget used'
                break
            if self.fail_streak.get(host, 0) >= self.breaker_after:
                info['error'] = 'skipped: %s did not answer the last %d calls' % (host, self.fail_streak[host])
                break
            rkey = '.'.join(host.split('.')[-2:])         # open-meteo.com: the forecast, air quality and archive hosts share one limit (it is the address of the caller that is counted)
            if self.rate_streak.get(rkey, 0) >= self.breaker_after:
                # A rate limit answer is not a dead host, but the answers that follow it are the same until the limit lifts, and three tries with a wait between them for each of 36 calls kept a run
                # 281 s at the limit of the day (stress test T7b, 25 Oct 2026) and only added to the load that caused it. The run goes on without the host and says so; the next hour tries again.
                info['error'] = 'skipped: %s answered 429 (a rate limit) to the last %d calls' % (rkey, self.rate_streak[rkey])
                break
            info['attempts'] = attempt
            retry, conn_fail = False, False
            attempt_deadline = min(self.deadline, now_m + self.attempt_cap_s)
            try:
                req = urllib.request.Request(url, headers=dict({'User-Agent': UA, 'Accept': '*/*'}, **(headers or {})))
                with self._open_bounded(req, max(1.0, min(self.timeout, attempt_deadline - now_m)), attempt_deadline) as r:
                    status = getattr(r, 'status', 200)
                    body = self._read_body(r, attempt_deadline)
                    date = r.headers.get('Date') if getattr(r, 'headers', None) else None
                if date:
                    self.server_dates.append(date)
                info['status'] = status
                self.fail_streak[host] = 0
                self.rate_streak['.'.join(host.split('.')[-2:])] = 0
                if status == 204 or not body:
                    info['error'] = 'no data (HTTP %s)' % status
                    return None, self._done(info, t0)
                if mode == 'json':
                    data = json.loads(body.decode('utf-8'))
                    self._done(info, t0)
                    self.cache[(mode, url)] = (data, dict(info))
                    return data, info
                self._done(info, t0)
                self.cache[(mode, url)] = (body, dict(info))
                return body, info
            except urllib.error.HTTPError as e:
                info['status'] = e.code
                reason = ''
                try:
                    j = json.loads(e.read().decode('utf-8'))
                    reason = str(j.get('reason') or '')
                except Exception:
                    pass
                info['error'] = ('HTTP %s %s' % (e.code, reason)).strip()[:120]
                retry = e.code in (429, 500, 502, 503, 504)
                conn_fail = False        # the host answered: an error for one request is not a dead host, so three bad points in a row must not stop the healthy ones (audit F14)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
                info['error'] = ('connection failed: %s' % getattr(e, 'reason', e)).strip()[:120]
                retry, conn_fail = True, True
            except http.client.HTTPException as e:
                # IncompleteRead (a body cut short), BadStatusLine, RemoteDisconnected and the like: a failed attempt, retried (audit WX-09)
                info['error'] = ('connection failed: incomplete or invalid HTTP answer (%s)' % type(e).__name__)[:120]
                retry, conn_fail = True, True
            except ValueError as e:
                info['error'] = 'invalid JSON in the answer: %s' % str(e)[:60]
            except Exception as e:                                       # never raises: whatever it was, it is a failed call with a reason
                info['error'] = ('unexpected error reading the answer: %s: %s' % (type(e).__name__, e))[:120]
            if conn_fail:
                info['_conn'] = True
            if not retry:
                break
            if attempt < self.retries:
                self.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        if info.pop('_conn', False):
            self.fail_streak[host] = self.fail_streak.get(host, 0) + 1
        if info.get('status') == 429 and info.get('error') and not str(info['error']).startswith('skipped'):
            rk = '.'.join(host.split('.')[-2:])
            self.rate_streak[rk] = self.rate_streak.get(rk, 0) + 1
        return None, self._done(info, t0)

    def get_json(self, key, url, headers=None):
        return self._get(key, url, 'json', headers)

    def get_bytes(self, key, url, headers=None):
        return self._get(key, url, 'bytes', headers)

    def time_left(self):
        return self.deadline - time.monotonic()

    def clock_skew_minutes(self):
        """minutes the laptop clock is ahead (+) or behind (-) the servers, from the HTTP Date headers"""
        out = []
        for d in self.server_dates[-5:]:
            try:
                t = dt.datetime.strptime(d, '%a, %d %b %Y %H:%M:%S GMT')
                out.append((utc_now() - t).total_seconds() / 60.0)
            except ValueError:
                pass
        return round(med(out), 1) if out else None


# ------------------------------------------------------------------------------------------------ models
def om_url(lat, lon, models, hourly):
    q = {'latitude': lat, 'longitude': lon, 'hourly': ','.join(hourly), 'past_days': PAST_DAYS, 'forecast_days': FORECAST_DAYS, 'timezone': TZ_NAME, 'wind_speed_unit': 'kmh'}
    if models:
        q['models'] = ','.join(models)
    return OM_FORECAST + '?' + urllib.parse.urlencode(q, safe=',')


def _blank_point(idx):
    name, lat, lon = POINTS[idx]
    return {'idx': idx, 'name': name, 'lat': lat, 'lon': lon, 'slug': S.slug(name), 'ok': False, 'error': None, 'times': [], 'i0': None, 'models': {}, 'fog': {}, 'point': None, 'ens': None,
            'band': None, 'probMax24': None, 'calls': [], 'daily': {}, 'past': {}, 'past_vec': {}, 'gust_past': {}, 'series': {}}


def read_point_safe(fetcher, idx, now):
    """read_point, with a net under it: an answer of a shape nobody expected fails that one point, with the reason, and the other points are still published (audit WX-10)"""
    try:
        return read_point(fetcher, idx, now)
    except Exception as e:
        pt = _blank_point(idx)
        pt['error'] = ('the answer could not be read (%s: %s)' % (type(e).__name__, e))[:120]
        pt['models'] = {m: {'usable': False, 'reason': 'answer could not be read'} for m in M.ALL_MODELS}
        return pt


def read_point(fetcher, idx, now):
    name, lat, lon = POINTS[idx]
    pt = _blank_point(idx)
    sl = pt['slug']
    now_label = hour_label(now)
    data, info = fetcher.get_json('om_all_' + sl, om_url(lat, lon, M.ALL_MODELS, M.OM_VARS))
    pt['calls'].append(info)
    times, series = [], {}
    if data is not None:
        ok_z, zt, stated = M.zone_check(data)
        if ok_z and not stated:
            # the engine asks for timezone=Asia/Dubai and reads every label as local time: an answer that states neither a zone nor an offset (a proxy that strips them) cannot be checked, and a UTC
            # axis would slide the window by four hours and drop a storm that is about to arrive. It is not used, the point is unread, the level is unknown (audit F2b)
            ok_z, zt = False, 'the answer states neither a time zone nor a utc offset, so its time axis cannot be checked against Ras Al Khaimah time (Asia/Dubai was asked for): it is not used'
        if not ok_z:
            # an answer on another time axis (UTC, say) would slide the window by hours and drop a storm that is about to come: it is not used, the point is unread, the level is unknown (audit F2)
            pt['error'] = zt
            pt['zoneRejected'] = zt
            pt['models'] = {m: {'usable': False, 'reason': 'answer not on Ras Al Khaimah time'} for m in M.ALL_MODELS}
            return pt
        pt['zoneStated'] = stated
        times, series = M.split_models(data, M.ALL_MODELS)
    elif info.get('status') == 400:
        # one bad model name fails the whole call: ask for the models one at a time
        for m in M.ALL_MODELS:
            d1, i1 = fetcher.get_json('om_one_%s_%s' % (sl, m), om_url(lat, lon, [m], M.OM_VARS))
            pt['calls'].append(i1)
            if d1 is not None and (not M.zone_check(d1)[0] or not M.zone_check(d1)[2]):
                pt['zoneRejected'] = M.zone_check(d1)[1] or 'the answer states neither a time zone nor a utc offset, so its time axis cannot be checked: it is not used'
                continue
            if d1 is not None:
                t1, s1 = M.split_models(d1, [m])
                if not times:
                    times = t1
                if t1 == times:
                    series[m] = s1[m]
    if not times:
        pt['error'] = info.get('error') or ('the answer from Open-Meteo had an unexpected shape (no hourly time axis)' if data is not None else 'no answer')
        pt['models'] = {m: {'usable': False, 'reason': 'no answer from Open-Meteo' if data is None else 'answer of an unexpected shape'} for m in M.ALL_MODELS}
        return pt
    i0 = M.first_index(times, now_label)
    if i0 is None:
        pt['error'] = 'the time axis of the answer ends before now (%s); a stale answer is not used' % now_label
        pt['models'] = {m: {'usable': False, 'reason': 'time axis ends before now'} for m in M.ALL_MODELS}
        return pt
    pt['times'], pt['i0'] = times, i0
    pt['series'] = series                                                     # the raw series of every model: the conditions bands (wx_cond) read wind, gust, temperature, humidity and pressure from them
    for m in M.ALL_MODELS:
        pt['models'][m] = M.analyse_model(times, series.get(m), i0)
        pt['fog'][m] = M.fog_model(times, series.get(m), i0)
    usable = [r for r in pt['models'].values() if r['usable']]
    if not usable:
        pt['error'] = 'no usable model (every model failed the 90 percent rule): %s' % '; '.join('%s: %s' % (m, r.get('reason')) for m, r in list(pt['models'].items())[:3])
        return pt
    pt['ok'] = True
    mid, lo, hi = M.hourly_band(usable)
    pt['band'] = (mid, lo, hi)
    pt['gap24'] = sum(1 for x in mid[:24] if x is None)                      # hours of the next 24 for which no usable model has a value (audit NEW-3)
    pt['point'] = M.summarise_models(times, i0, usable, mid)               # the figures of the point are the median of the models' own figures; the hourly median is for display (audit V1)
    g = [r['gustMax24'] for r in usable if r['gustMax24'] is not None]
    pt['point']['gustMax24'] = round(med(g)) if g else None
    pt['ens'] = M.ensemble(usable)
    pt['past'] = M.past_series(times, i0, usable)
    pt['past_vec'] = M.past_vectors(times, i0, {m: r for m, r in pt['models'].items() if r['usable']})
    # model gusts of the past hours (median over the usable models) for the events
    for k in range(max(0, i0 - 48), i0 + 1):
        xs = [r['_gust'][k] for r in usable if k < len(r['_gust']) and r['_gust'][k] is not None]
        if xs and k < len(times):
            pt['gust_past'][times[k]] = med(xs)
    d0 = now.strftime('%Y-%m-%d')
    days = [(dt.datetime.strptime(d0, '%Y-%m-%d') + dt.timedelta(days=k)).strftime('%Y-%m-%d') for k in range(-2, 8)]
    pt['daily'] = M.day_totals(times, {m: r['_p'] for m, r in pt['models'].items() if r['usable']}, days)
    # best match for the rain chance and the weather code (the field is often 0 for convective rain: shown, not trusted)
    d2, i2 = fetcher.get_json('om_bm_' + sl, om_url(lat, lon, ['best_match'], ['precipitation_probability', 'weather_code']))
    pt['calls'].append(i2)
    if isinstance(d2, dict) and not M.zone_check(d2)[0]:
        d2 = None                                                              # a probability on another time axis is not shown
    if isinstance(d2, dict):
        h = d2.get('hourly') if isinstance(d2.get('hourly'), dict) else {}
        t2 = h.get('time') if isinstance(h.get('time'), list) else []
        j0 = M.first_index(t2, now_label)
        pr = [nz(x) for x in M._ser(h, 'precipitation_probability')]
        if j0 is not None and pr:
            seg = [x for x in pr[j0:j0 + 24] if x is not None]
            pt['probMax24'] = max(seg) if seg else None
            cd = [nz(x) for x in M._ser(h, 'weather_code')]
            segc = [x for x in cd[j0:j0 + 24] if x is not None]
            pt['weatherCodeMax24'] = int(max(segc)) if segc else None
    return pt


def point_level(pt, rule='plan', headline=None):
    """the rain level at one point, on the same rule as the headline so that a sector row never says less than the headline says about a place a model of the vote points at
    (audit logic D2). Plan rule: the plan thresholds on the point's own median. Proposed vote: the vote of the models that cover the point, with the same one model safeguard,
    and a point where a model that makes the headline vote reaches the threshold shows the headline level, because the models of the vote may stand at different points; the plan
    floor on the median stands under it. unknown when the point was not read, or when it shows none and fewer than 3 models read it."""
    if not pt['ok']:
        return 'unknown'
    usable = [m for m in M.ALL_MODELS if pt['models'][m]['usable']]
    x = pt['point'].get('max6Raw')
    if x is None:
        x = pt['point']['max6'] or 0.0
    plan = 'high' if x >= THRESH6['high'] else ('watch' if x >= THRESH6['watch'] else 'none')
    if rule == 'plan':
        lv = plan
    else:
        voters = [m for m in M.VOTE_MODELS if pt['models'][m]['usable']]
        n5 = sum(1 for m in voters if (pt['models'][m]['max6_48'] or 0) >= M.PROPOSED['watch']['mm6h'])
        n25 = sum(1 for m in voters if (pt['models'][m]['max6_48'] or 0) >= M.PROPOSED['high']['mm6h'])
        vote = 'high' if n25 >= M.PROPOSED['high']['votes'] else ('watch' if n5 >= M.PROPOSED['watch']['votes'] else 'none')
        if vote == 'none' and n25 >= 1:
            vote = 'watch'                                      # the safeguard of the headline: one voting model at the high value is a watch, never silence
        hv = (headline or {}).get('voteLevel', 'none')
        if M.LEVEL_RANK[hv] >= 2 and n25 >= 1:
            vote = 'high'
        elif M.LEVEL_RANK[hv] >= 1 and n5 >= 1 and M.LEVEL_RANK[vote] < 1:
            vote = 'watch'
        lv = max([vote, plan], key=lambda a: M.LEVEL_RANK[a])
    if lv == 'none' and len(usable) < 3:
        return 'unknown'
    return lv


# ------------------------------------------------------------------------------------------------ fog
def fog_assessment(points, rows_by_station, ncm_sum, now, store=None, obs=None, stations=None, page_pending=None):
    """fogLevel (none, watch, alert, severe, unknown) from the calibrated forecast ladder, the observed ladder and the NCM fog colour in force or issued (Proposed).
    With the hourly store, the observed side keeps what the stations reported before a METAR feed failed; with obs, a level of none is withheld (unknown) when no station reported
    in the last 3 hours, and with page_pending when the NCM warnings page shows colours nobody has logged (audit WX-03, D1)."""
    rule_pts = [p for p in points if p['name'].startswith(FOG_PREFIXES)]
    by_day, evaluated, used_models = {}, 0, set()
    point_days = {}
    for p in rule_pts:
        f, used = None, None
        if p['ok'] or p.get('i0') is not None:
            for m in RULE_MODELS:
                fm = (p.get('fog') or {}).get(m)
                if fm and sum(1 for x in fm['_spread'][p['i0']:p['i0'] + 72] if x is not None) >= 60:
                    f, used = fm, m
                    break
        if not f:
            continue
        evaluated += 1
        used_models.add(used)
        lv = M.fog_rule_levels(p['times'], f, p['i0'])
        point_days[p['idx']] = lv
        for d, (l, lab, s, w) in lv.items():
            cur = by_day.get(d)
            if cur is None or l > cur['lv'] or (l == cur['lv'] and l > 0 and s < cur['spread']):
                by_day[d] = {'lv': l, 'at': lab, 'spread': round(s, 2), 'wind': round(w, 1), 'point': p['name'], 'model': used}
    forecast_ok = evaluated >= 3
    # which windows count now: 02:00 to 10:00 of a day (the rule hours and the hold to 10:00) that touch the next 24 hours
    end24 = now + dt.timedelta(hours=24)

    def active(d):
        d0 = dt.datetime.strptime(d, '%Y-%m-%d')
        return d0 + dt.timedelta(hours=2) <= end24 and d0 + dt.timedelta(hours=10) >= now
    f_lv, f_why = 0, None
    for d, e in by_day.items():
        if active(d) and e['lv'] > f_lv:
            f_lv, f_why = e['lv'], dict(e, date=d)
    point_level_now = {}
    for p in rule_pts:
        best = 0
        for d, (l, lab, s, w) in (point_days.get(p['idx']) or {}).items():
            if active(d):
                best = max(best, l)
        point_level_now[p['idx']] = FOG_NAMES[best] if p['idx'] in point_days else None
    obs_lv, obs_why = O.observed_fog(rows_by_station, now, store=store, stations=stations)
    fresh = obs['any']['stationsFresh'] if obs else None
    west_all = (obs['any'].get('westStations') or []) if obs else []
    west_fresh = (obs['any'].get('westFresh') or []) if obs else []
    ncm_col = ncm_sum['levels']['fog']
    ncm_lv = {'none': 0, 'yellow': 1, 'orange': 2, 'red': 3}[ncm_col]
    lv = max(f_lv, obs_lv, ncm_lv)
    level = FOG_NAMES[lv]
    reasons = []
    if lv == 0:
        if not forecast_ok:
            reasons.append('the dew point spread of ECMWF could be read at only %d of %d west plain points (3 are needed) and no station or NCM warning shows fog' % (evaluated, len(rule_pts)))
        if fresh == 0:
            reasons.append('no station reported in the last %g hours (the stations are the only observed check on fog) and no forecast or NCM warning shows fog' % O.SILENT_HOURS)
        elif obs and west_all and not west_fresh:
            # the fog of seven seasons was at the west coast stations: a station on the east coast that reports clear cannot rule fog out (audit F4)
            reasons.append('no west coast station (%s) reported in the last %g hours (all the fog on record is on the west coast, so a station on the east coast cannot rule it out) and no forecast or NCM warning shows fog'
                           % (', '.join(west_all), O.SILENT_HOURS))
        if page_pending:
            reasons.append(page_pending.get('fogReason') or ('the NCM warnings page shows warning colours (%s) that nobody has logged, so an NCM fog warning cannot be ruled out' % ', '.join(page_pending.get('colours') or ['colour not read'])))
    unknown_reason = '; '.join(reasons) or None
    if unknown_reason:
        level = 'unknown'
    parts = []
    if f_why:
        parts.append('forecast %s: %s spread %.2f C with wind %.0f km/h at %s on %s' % (FOG_NAMES[f_lv], f_why['model'], f_why['spread'], f_why['wind'], f_why['point'].split(' (')[0], f_why['at'].replace('T', ' ')))
    elif forecast_ok:
        parts.append('forecast none: no hour 02:00 to 08:00 in the next 24 h has a spread of 1.0 C or less with wind 10 kt (19 km/h) or less (%s at %d west plain points)' % (', '.join(sorted(used_models)), evaluated))
    else:
        parts.append('forecast not read at enough points (%d of %d)' % (evaluated, len(rule_pts)))
    stored = any(w.get('fromStoredRecord') for w in obs_why)
    if fresh == 0:
        parts.append('observed: no station reported in the last %g hours%s' % (O.SILENT_HOURS, ('; the level %s is held from the stored record of earlier reports: ' % FOG_NAMES[obs_lv] + '; '.join('%s %s %s (%s)' % (w['station'], w['at'], w['rule'], w['level']) for w in obs_why[:3])) if obs_why else ', so the observed side could not be checked'))
    else:
        parts.append('observed %s%s' % (FOG_NAMES[obs_lv], (': ' + '; '.join('%s %s %s (%s)%s' % (w['station'], w['at'], w['rule'], w['level'], ' from the stored record' if w.get('fromStoredRecord') else '') for w in obs_why[:3])) if obs_why else ' (no qualifying report held at this hour)'))
        if obs and west_all and not west_fresh:
            parts.append('the west coast stations (%s) did not report in the last %g hours, only %s did' % (', '.join(west_all), O.SILENT_HOURS, ', '.join(obs['any'].get('freshStations') or []) or 'none'))
    parts.append('NCM fog colour in force or issued: %s%s' % (ncm_col, (' (' + ', '.join(ncm_sum['ids']['fog']) + ')') if ncm_sum['ids']['fog'] else ''))
    basis = ('Proposed calibrated ladder (calibration.json, 24 Oct 2026): watch spread 1.0 C or less, alert 0.5 C or less, severe 0.25 C or less, with wind 10 kt (19 km/h) or less at any hour 02:00 to 08:00 '
             'at the west plain points; observed rules from the stations; an NCM colour sets the level at least as high (yellow watch, orange alert, red severe); held to 10:00. Now: ' + '. '.join(parts) + '.')
    if unknown_reason:
        basis += ' Level unknown, not none: ' + unknown_reason + '.'
    out_of_season = now.month not in (10, 11, 12, 1, 2, 3)
    if out_of_season:
        basis += ' Outside the season the ladder was fitted on (October to March): more of its flags are false then, so read a flag as a prompt to read the visibility reports, not as a warning.'
    today = now.strftime('%Y-%m-%d')
    days = [(dt.datetime.strptime(today, '%Y-%m-%d') + dt.timedelta(days=k)).strftime('%Y-%m-%d') for k in range(3)]
    # a day the forecast could not be made for is null, never none: with too few west plain points read, a level of none for a day is not a forecast, only a level above none stands (audit F16)
    fog_days = [{'d': d, 'l': (FOG_NAMES[by_day[d]['lv']] if (d in by_day and (forecast_ok or by_day[d]['lv'] > 0)) else ('none' if forecast_ok else None))} for d in days]
    # 29 Oct 2026 (fourth audit, FOG-AIRPORTS-NONE): the observed side is none only when it was checked. With no station reported in the last 3 hours, or with both west coast stations silent (all the fog on
    # record is on the west coast), an observed level of none is unknown, as the forecast side is when too few points were read. A level above none held from the stored record of earlier reports stands.
    obs_name = 'unknown' if (obs_lv == 0 and (fresh == 0 or (obs and west_all and not west_fresh))) else FOG_NAMES[obs_lv]
    return {'level': level, 'basis': basis, 'forecastLevel': FOG_NAMES[f_lv] if forecast_ok else 'unknown', 'observedLevel': obs_name, 'ncmLevel': ncm_col, 'forecastOk': forecast_ok,
            'forecastWhy': f_why, 'observedWhy': obs_why, 'byDay': by_day, 'pointLevels': point_level_now, 'evaluated': evaluated, 'rulePoints': len(rule_pts), 'fogDays': fog_days,
            'unknownReason': unknown_reason, 'unknownReasons': reasons, 'models': sorted(used_models), 'fLv': f_lv, 'oLv': obs_lv, 'stationsFresh': fresh, 'observedFromStore': stored,
            'pagePending': bool(page_pending), 'outOfSeason': out_of_season, 'westFresh': west_fresh}


def fog_recent(entries, ncm_sum):
    """the last fog warnings on record by a person (or backfilled from the Barq log), in force or not, with the window and whether they name Ras Al Khaimah (Reported, official).
    A fog warning with no colour recorded (level none) is listed too, as 'none recorded': the console and A11(c) ask for the fog entries of the log (audit D11)."""
    ids = {e['id'] for e in ncm_sum['inForce']}
    fe = [e for e in entries if 'fog' in e['hazard'] and e.get('kind') == 'warning' and not N.no_person(e)]
    fe = sorted(fe, key=lambda e: (e['read'], e['id']))[-5:][::-1]
    return [{'id': e['id'], 'level': e['level'], 'colour': e['level'] if e['level'] != 'none' else 'none recorded', 'read': e['read'], 'from': e.get('from'), 'to': e.get('to'), 'areas': e.get('areas'),
             'rakNamed': e.get('rakNamed'), 'by': e.get('by'), 'inForce': e['id'] in ids} for e in fe]


# ------------------------------------------------------------------------------------------------ evidence for the event register
def build_evidence(now, rain, fog, store, stations, entries, since):
    ev = {'rain': [], 'fog': [], 'dust': []}
    if rain['level'] in ('watch', 'high'):
        ev['rain'].append({'time': now, 'kind': 'model-level', 'key': 'model|rain|' + f_local(now), 'level': rain['level'], 'detail': {'basis': short_text(rain['basis'], 140)}})
    # a steady rain that the plan's 24 hour band calls a stage while no 6 hour burst reaches the model level is an event too (audit F18)
    if rain.get('bandLevel') in ('watch', 'high') and M.LEVEL_RANK[rain['bandLevel']] > M.LEVEL_RANK.get(rain['level'], 0):
        ev['rain'].append({'time': now, 'kind': 'model-level', 'key': 'band|rain|' + f_local(now), 'level': rain['bandLevel'], 'detail': {'basis': short_text(rain.get('bandBasis') or 'the plan 24 hour rain band', 140)}})
    # the observed ladder (night mist with a small dew point spread and no wind, for example) is a fog level of alert or severe: the event register's own rule opens a fog event on it,
    # and a report with no fog group and a visibility above 1000 m made no evidence before (audit F21)
    for w in fog.get('observedWhy') or []:
        if w.get('level') in ('alert', 'severe') and not w.get('fromStoredRecord'):
            try:
                at = dt.datetime.strptime(w['at'], '%Y-%m-%d %H:%M')
            except (ValueError, KeyError, TypeError):
                continue
            ev['fog'].append({'time': at, 'kind': 'station', 'key': 'ladder|fog|%s|%s' % (w.get('station'), at.strftime('%Y-%m-%dT%H:%M')), 'station': w.get('station'), 'level': w['level'],
                              'detail': {'rule': short_text(w.get('rule'), 80), 'minVis': w.get('visM')}})
    if fog['fLv'] >= 2:
        w = fog.get('forecastWhy') or {}
        try:
            at = dt.datetime.strptime(w.get('at'), '%Y-%m-%dT%H:%M') if w.get('at') else None
        except ValueError:
            at = None
        if at is None or at <= now + dt.timedelta(hours=6):
            ev['fog'].append({'time': now, 'kind': 'model-level', 'key': 'model|fog|' + f_local(now), 'level': FOG_NAMES[fog['fLv']], 'detail': {'point': (w.get('point') or '')[:40], 'spread': w.get('spread')}})
    for e in O.station_evidence(store, stations, since, now):
        lv = None
        if e['hazard'] == 'fog':
            lv = 'severe' if (e['detail'].get('minVis') is not None and e['detail']['minVis'] < 500) else 'alert'
        elif e['hazard'] == 'dust':
            lv = 'reported'
        ev[e['hazard']].append({'time': e['time'], 'kind': 'station', 'key': e['key'], 'station': e['station'], 'level': lv, 'detail': e['detail']})
    for en in entries:
        if en['level'] == 'none' or en.get('rakNamed') == 'no':
            continue
        s, e_ = N.window(en)
        for hz in ('rain', 'fog', 'dust'):
            if hz not in en['hazard']:
                continue
            e_hz = N.effective_end(entries, en, hz)             # a later manual none for this hazard ends the colour of this hazard, and no other
            lo, hi = max(s, since), min(e_hz, now)
            if hi < lo:
                continue
            lvl = E.NCM_TO_LEVEL[hz][en['level']]
            ev[hz].append({'time': lo, 'kind': 'ncm', 'key': 'ncm|%s|%s|start' % (hz, en['id']), 'level': lvl, 'detail': {'id': en['id'], 'colour': en['level']}})
            if hi > lo:
                ev[hz].append({'time': hi, 'kind': 'ncm', 'key': 'ncm|%s|%s|%s' % (hz, en['id'], hi.strftime('%Y-%m-%dT%H')), 'level': lvl, 'detail': {'id': en['id'], 'colour': en['level']}})
    return ev


# ------------------------------------------------------------------------------------------------ health
def source_rollup(calls, prefixes):
    mine = [c for c in calls if any(c['key'].startswith(p) for p in prefixes) and c.get('error') != 'no fixture']
    if not mine:
        return {'status': 'skipped', 'calls': 0}
    okc = sum(1 for c in mine if not c.get('error') and c.get('status') == 200)
    st = 'ok' if okc == len(mine) else ('partial' if okc else 'error')
    errs = []
    for c in mine:
        if c.get('error') and c['error'] not in errs and c['error'] != 'no fixture':
            errs.append(c['error'])
    return {'status': st, 'calls': len(mine), 'ok': okc, 'ms': sum(c.get('ms', 0) for c in mine), 'errors': errs[:3]}


def update_health(data_dir, now, sources, alerts, extra):
    prev = read_json(os.path.join(data_dir, 'health.json'), {}) or {}
    prev = prev if isinstance(prev, dict) else {}
    ps = prev.get('sources') if isinstance(prev.get('sources'), dict) else {}
    out = {}
    for name, s in sources.items():
        p = ps.get(name) if isinstance(ps.get(name), dict) else {}
        o = {'lastAttempt': f_local(now), 'status': s['status'], 'calls': s.get('calls'), 'ms': s.get('ms'), 'lastError': (s.get('errors') or [None])[0] if s['status'] in ('error', 'partial') else p.get('lastError'),
             'lastErrorAt': f_local(now) if s['status'] in ('error', 'partial') else p.get('lastErrorAt')}      # 28 Oct 2026 (L5): the text of an old error beside status ok carries its time
        if s['status'] in ('ok', 'partial'):
            o['lastOk'] = f_local(now)
            o['consecutiveFailures'] = 0
        elif s['status'] == 'error':
            o['lastOk'] = p.get('lastOk')
            o['consecutiveFailures'] = (p.get('consecutiveFailures') or 0) + 1
        else:
            o['lastOk'] = p.get('lastOk')
            o['consecutiveFailures'] = p.get('consecutiveFailures') or 0
        out[name] = o
    h = dict(extra)
    h.update({'schema': 1, 'asof': f_local(now), 'sources': out, 'alerts': alerts})
    return h


def model_alerts(reads, level):
    """a model that never answers, or that disagrees with every other model, is named (audit WX-24). Never changes a level: it says that a model deserves a look."""
    out = []
    ok_pts = [p for p in reads if p['ok']]
    if not ok_pts:
        return out
    for m in M.ALL_MODELS:
        if not any(p['models'].get(m, {}).get('usable') for p in ok_pts):
            reasons = {}
            for p in ok_pts:
                r = p['models'].get(m, {}).get('reason')
                if r:
                    reasons[r] = reasons.get(r, 0) + 1
            top = max(reasons, key=reasons.get) if reasons else 'no reason given'
            out.append('model %s returned no usable rain series at any of the %d points that answered (%s)%s' % (m, len(ok_pts), top, '' if m in M.EXTRA_MODELS else '; it is one of the 7 voting models'))
    imp = {}
    for p in reads:
        for m, r in (p.get('models') or {}).items():
            if isinstance(r, dict) and r.get('implausible'):
                imp[m] = imp.get(m, 0) + r['implausible']
    if imp:
        out.append('implausible rain values (more than %g mm in one hour) were ignored as a fault of the feed: %s' % (M.MAX_MM_HOUR, ', '.join('%s %d hours' % (m, n) for m, n in sorted(imp.items()))))
    votes = level.get('votes') or {}
    if len(level.get('reach5') or []) >= 3:
        for m in level.get('voters') or []:
            v = votes.get(m) or {}
            if v.get('points') and (v.get('max6') or 0) < 0.1:
                out.append('model %s shows no rain at any point in the next 48 h while %d voting models reach %g mm in 6 h (a stuck feed or a real disagreement: the vote counts it as dry)' % (m, len(level['reach5']), M.PROPOSED['watch']['mm6h']))
    if level.get('voteLevel') == 'none' and level.get('planRuleLevel') == 'none':
        for m in M.EXTRA_MODELS:
            best = max([(p['models'][m].get('max6_48') or 0) for p in ok_pts if p['models'].get(m, {}).get('usable')] or [0])
            if best >= THRESH6['watch']:
                out.append('model %s (shown, not in the vote) reaches %.1f mm in 6 h while no voting model reaches the vote values and the plan rule says none' % (m, best))
    return out


# ------------------------------------------------------------------------------------------------ the run
def parse_args(argv):
    ap = argparse.ArgumentParser(description='WM004 weather engine')
    ap.add_argument('--out', default=DEFAULT_OUT, help='weather.json to write (default ../weather.json)')
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR, help='data/weather: ledger, events, season, ncm_log (default data/weather)')
    ap.add_argument('--fixture', default=None, help='read every answer from saved files in this folder; no network')
    ap.add_argument('--now', default=None, help='override the clock, ISO local time (tests)')
    ap.add_argument('--no-write-ledger', action='store_true', help='do not append the ledger line')
    ap.add_argument('--dry-run', action='store_true', help='write nothing at all; print the summary')
    ap.add_argument('--level-rule', choices=('proposed', 'plan'), default='plan', help='rain level rule: the plan thresholds on the median (default; the proposed vote is shown beside it) or the proposed vote')
    ap.add_argument('--no-ncm-auto', action='store_true', help='do not try the automatic NCM bulletin and warnings page read')
    # 28 Oct 2026 (R1): 150 s, not 300. A typical live run takes 36 s with the channel and the ledger maximum is 2 minutes; with every call hanging the run took 5 min 02 s, over the 3 minute rule.
    # The honest unknown output is the same at 150 s (exit 2); the task of web_engine.cmd passes it too.
    ap.add_argument('--deadline', type=int, default=150, help='seconds the outside calls may take in all (a run with every call hanging ends at about this time plus the work after it, exit 2)')
    ap.add_argument('--conditions-out', default=None, help='conditions.json to write (default: conditions.json beside --out)')
    ap.add_argument('--channel', choices=('auto', 'on', 'off'), default='auto', help='run the weather channel at the end of the run (auto: not for a dry run, a fixture run or a given clock; on; off)')
    ap.add_argument('--wait-lock', type=float, default=0.0, help='seconds to wait for another run to finish before giving up with ENGINE BUSY (default 0; weather_feed.py waits when it logs a manual NCM reading)')
    return ap.parse_args(argv)


def _guard(name, fn, default, alerts):
    """run one piece of the bookkeeping or the link to the plan: a failure is named in the alerts and the piece is left out, the weather is still published (audit WX-11, WX-12)"""
    try:
        return fn()
    except Exception as e:
        alerts.append('%s could not be built (%s: %s); the rain, fog, NCM and station blocks are published without it' % (name, type(e).__name__, str(e)[:120]))
        return default


SCRIPT_UNSAFE = re.compile(r'<(?=[/!])')


def _finite(o, counter):
    """in place: a float that is infinite or not a number becomes None; counts them"""
    items = o.items() if isinstance(o, dict) else (enumerate(o) if isinstance(o, list) else ())
    for k, v in list(items):
        if isinstance(v, float) and not math.isfinite(v):
            o[k] = None
            counter[0] += 1
        elif isinstance(v, (dict, list)):
            _finite(v, counter)


def proposed_vote_block(level, rule, vote_gap):
    """The proposed model vote as one block that every reader can show and use (audit V2): the console header, alarms AL10 and AL11, the weather channel's triggers and the card. Carried in
    weather.json and interaction.json as proposedVote, with its basis in words. It is worked out on every run, whether or not it is applied: rule says which rule sets modelLevel (plan, the
    default, or proposed). The default rule is a decision for the department head and is not changed here: aboveRule says the vote reads higher than the rule that is applied, which is the case a
    reader must not mistake for calm. Status: Model forecast (Proposed)."""
    P = M.PROPOSED
    vl, pl = level['voteLevel'], level['planRuleLevel']
    voters, n5, n25 = level['voters'], level['reach5'], level['reach25']
    models = list(n25) if vl == 'high' else (list(n5) if (vl == 'watch' and len(n5) >= P['watch']['votes']) else (list(n25) if vl == 'watch' else []))
    if vl == 'high':
        head = 'The proposed model vote says high: %d of %d voting models reach %g mm in 6 h at a point in the next %d h (%s).' % (len(n25), len(voters), P['high']['mm6h'], P['high']['horizonH'], ', '.join(n25))
    elif vl == 'watch' and len(n5) >= P['watch']['votes']:
        head = 'The proposed model vote says watch: %d of %d voting models reach %g mm in 6 h at a point in the next %d h (%s).' % (len(n5), len(voters), P['watch']['mm6h'], P['watch']['horizonH'], ', '.join(n5))
    elif vl == 'watch':
        head = ('The proposed model vote says watch by the added safeguard: one voting model (%s) reaches %g mm in 6 h at a point in the next %d h, which is a watch on its own.'
                % (', '.join(n25), P['high']['mm6h'], P['high']['horizonH']))
    else:
        head = ('The proposed model vote says none: %d of %d voting models reach %g mm in 6 h and %d reach %g mm at any point in the next %d h.'
                % (len(n5), len(voters), P['watch']['mm6h'], len(n25), P['high']['mm6h'], P['high']['horizonH']))
    fig = level.get('worstMax6Median')
    plan = ' The plan rule (%g mm for a watch and %g mm for a high in 6 h, on the median of the models) says %s%s.' % (THRESH6['watch'], THRESH6['high'], pl, (' at %.1f mm' % fig) if fig is not None else '')
    applied = rule == 'proposed'
    tail = (' The vote is applied to modelLevel (--level-rule proposed).' if applied else
            ' The vote is Proposed and is not applied until the department head decides, so a %s here is not a reading of no rain.' % pl if vote_gap else
            ' The vote is Proposed and is not applied until the department head decides.')
    return {'status': 'Model forecast (Proposed)', 'level': vl, 'planRuleLevel': pl, 'applied': applied, 'aboveRule': bool(vote_gap), 'higherThanPlanRule': M.LEVEL_RANK[vl] > M.LEVEL_RANK[pl], 'rule': rule,
            'models': models, 'voters': list(voters), 'reach5': list(n5), 'reach25': list(n25), 'figureMm6h': level.get('worstMax6Vote'), 'planFigureMm6h': fig,
            'thresholds': {'watch': dict(P['watch']), 'high': dict(P['high'])}, 'basis': head + plan + tail,
            'decision': {'status': 'open', 'owner': 'department head', 'default': 'the plan rule: 10 mm and 25 mm in 6 h on the median of the models', 'recommendation': 'apply the vote for watch',
                         'recommendationStatus': 'Proposed', 'howToApply': 'run the engine with --level-rule proposed; the plan values of 10 and 25 mm are unchanged', 'where': 'data/weather/SCHEMA.md, decision at the top'}}


LEVEL_TEXT = {'none': 'none', 'watch': 'watch', 'high': 'high', 'unknown': 'not read well enough (unknown: a failed or partial read, never no rain)'}
FOG_TEXT = {'none': 'none', 'watch': 'watch', 'alert': 'alert', 'severe': 'severe', 'unknown': 'not read well enough (unknown: no station, forecast or NCM reading rules fog out, never no fog)'}


def run(argv=None, fetcher=None, runner=None):
    """returns (exit code, result dict). Never raises for an outside failure."""
    a = parse_args(argv if argv is not None else sys.argv[1:])
    now_given = bool(a.now)
    now = parse_local(a.now) if a.now else now_local()
    write = not a.dry_run
    data_dir = os.path.abspath(a.data_dir)
    out_path = os.path.abspath(a.out)
    fetcher = fetcher or Fetcher(fixture_dir=a.fixture, deadline_s=a.deadline)
    alerts, notes = [], []
    prev_weather = read_json(out_path, None)
    prev_weather = prev_weather if isinstance(prev_weather, dict) else None
    try:
        prev_asof = parse_local(prev_weather['asof']) if prev_weather and prev_weather.get('asof') else None
    except (ValueError, TypeError):
        prev_asof = None
    prev_health = read_json(os.path.join(data_dir, 'health.json'), {})
    prev_health = prev_health if isinstance(prev_health, dict) else {}

    # --- models and stations first: their answers carry the servers' clock
    reads = [read_point_safe(fetcher, i, now) for i in range(len(POINTS))]
    st_problems = []
    stations = O.load_stations(data_dir, st_problems)
    metar, mi = fetcher.get_json('awc_metar', AWC + 'metar?' + urllib.parse.urlencode({'ids': ','.join(s['icao'] for s in stations), 'format': 'json', 'hours': OBS_HOURS}, safe=','))
    taf, ti = fetcher.get_json('awc_taf', AWC + 'taf?' + urllib.parse.urlencode({'ids': ','.join(s['icao'] for s in stations), 'format': 'json'}, safe=','))

    # --- the clock: a laptop clock more than 10 minutes off the servers' is not used (the forecast window would slide: audit WX-06). The run takes the servers' time, says so,
    #     and tells the event register that this run's time is trusted (audit WX-07). A clock given with --now is the caller's choice and is never corrected.
    skew = fetcher.clock_skew_minutes()
    corrected, refused = False, False
    if skew is None and not now_given and not a.fixture:
        # no answer carried a Date header: the clock cannot be checked, and a laptop clock that is hours off would drop an imminent storm without a word (audit V9, F11)
        alerts.append('the laptop clock could not be checked against the servers (no answer carried a Date header): if it is wrong the forecast window is wrong; check the laptop clock')
    if skew is not None and abs(skew) > SKEW_LIMIT_MIN:
        if now_given:
            alerts.append('the laptop clock is %.0f minutes %s the servers; the clock given with --now was used as it is' % (abs(skew), 'ahead of' if skew > 0 else 'behind'))
        else:
            cand = (now - dt.timedelta(minutes=skew)).replace(second=0, microsecond=0)
            if prev_asof and cand < prev_asof - dt.timedelta(minutes=5):
                # a correction that would put this run before the last published one is far more likely a stale Date header (an answer from a cache) than a laptop clock that was ahead:
                # the record is never wound back by it (audit F11)
                refused = True
                alerts.append('the servers\' Date says %s, which is before the last weather.json (%s): a Date from a cached answer is more likely than a laptop clock that was ahead, so the laptop clock was kept and the record was not wound back'
                              % (f_local(cand), f_local(prev_asof)))
            else:
                now = cand
                corrected = True
                reads = [read_point_safe(fetcher, i, now) for i in range(len(POINTS))]        # the answers are cached: the same answers, read against the right clock
                alerts.append('the laptop clock is %.0f minutes %s the servers: the servers\' time (%s) was used for this run; correct the laptop clock' % (abs(skew), 'ahead of' if skew > 0 else 'behind', f_local(now)))
    trusted = skew is not None and not now_given and not refused

    def clock_now():
        if now_given:
            return now
        return utc_now() + dt.timedelta(hours=4) - dt.timedelta(minutes=(skew if corrected else 0))
    run_id = E.new_run_id(now)
    started = clock_now()

    # --- conditions: best_match with every variable and the CAMS air quality model, two calls per point. Read only: a call that fails leaves that point with no reading (never a value of an earlier run)
    cond_reads = [C.read_point_conditions(fetcher, i, now) for i in range(len(POINTS))]
    # --- the wind field: model wind on a 0.1 degree grid (150 nodes, two calls) for the wind movement on the map. Read only, never raises: a call that fails leaves its nodes null and the block says so
    wind_read = WF.read_windfield(fetcher, now)

    # --- NCM: the automatic read first (it only ever appends), then the log
    _check_lock()                                              # a run that was suspended and whose lock was taken over writes nothing (audit F13)
    ncm_auto = {'bulletin': {'ran': False}, 'warnings': {'ran': False}, 'appended': []}
    if not a.no_ncm_auto:
        ncm_auto = A.run(data_dir, fetcher, now, runner=runner or A.run_bounded, write=write)
    entries, ncm_bad = N.load(data_dir)
    page = A.pending_page_warnings(data_dir, entries, now)
    logd = N.unreadable_status(data_dir, ncm_bad)
    page_on = bool(page and page['pending'])
    logd_on = bool(logd and logd['pending'])
    # a manual reading that the log refused (a typo in the hazard or the level, a source that is not a link, a bad time) is kept in ncm_refused.jsonl and stays a high alert until a valid
    # reading for the same level is logged or a person acknowledges it: a typed red must never vanish (audit N1, NEW-2)
    ncm_ref = N.refused_status(data_dir, entries, now)
    ref_open = ncm_ref['open']
    ref_unread = ncm_ref['unreadable'] if (ncm_ref.get('unreadable') or {}).get('open') else None
    high_alerts = [{'id': it['id'], 'kind': 'ncm-refused', 'at': it['at'], 'level': it.get('level'), 'hazard': it.get('hazard'), 'text': it['text']} for it in ref_open]
    if ref_unread:
        high_alerts.append({'id': 'ncm_refused.jsonl', 'kind': 'ncm-refused', 'at': f_local(now), 'level': None, 'hazard': None, 'text': N.unreadable_refused_text(ref_unread)})
    ha_texts = {ha['text'] for ha in high_alerts}
    # what nobody has logged: the colours of the NCM warnings page (or the state file that remembers them), and the lines of the log that cannot be read. Each makes the hazards it could be about
    # unknown, not none, until a person logs a reading that names the hazard (audit D1, F7, F8, F10)
    pend = {'rain': [], 'fog': [], 'dust': []}
    if page_on:
        for h in page['pendingHazards']:
            pend[h].append(A.page_pending_text(page))
    if logd_on:
        for h in logd['pendingHazards']:
            pend[h].append(N.unreadable_text(logd))
    for it in ref_open:
        for h in it['pendingHazards']:
            pend[h].append(it['text'])
    if ref_unread:
        for h in N.COVER_HAZARDS:
            pend[h].append(N.unreadable_refused_text(ref_unread))
    page_pending = ({'hazards': [h for h in ('rain', 'fog', 'dust') if pend[h]], 'colours': (page or {}).get('colours') or [], 'since': (page or {}).get('since'), 'reasons': sorted({t for v in pend.values() for t in v}),
                     'fogReason': '; '.join(dict.fromkeys(pend['fog'])) or None, 'page': page_on, 'log': logd_on, 'refused': bool(ref_open or ref_unread)} if any(pend.values()) else None)
    rain_pend, fog_pend = bool(pend['rain']), bool(pend['fog'])
    ncm_sum = N.summarize(entries, now)
    ncm = N.ncm_block(ncm_sum, now)
    ncm['levels'] = dict(ncm['levels'])
    if isinstance(ncm.get('levelsInForce'), dict):
        ncm['levelsInForce'] = dict(ncm['levelsInForce'])
    if page:
        ncm['pageShows'] = {'state': 'warnings' if not page.get('stateUnreadable') else 'state file unreadable', 'colours': page['colours'], 'since': page['since'], 'seen': page['seen'], 'pending': page_on,
                            'pendingHazards': page['pendingHazards'], 'coveredBy': page['coveredBy'], 'namedHazards': page.get('namedHazards'),
                            'note': (A.page_pending_text(page) if page_on else 'the NCM warnings page showed warning colours; a person has logged a reading naming rain, fog and dust since (%s)' % ', '.join(page['coveredBy']))}
    if logd:
        ncm['unreadableLines'] = {'lines': logd['lines'], 'pending': logd_on, 'pendingHazards': logd['pendingHazards'], 'coveredBy': logd['coveredBy'], 'note': N.unreadable_text(logd)}
    if page_pending:
        for h in ('rain', 'fog', 'dust'):
            if pend[h] and ncm['levels'].get(h) == 'none':
                ncm['levels'][h] = 'unknown'
        if rain_pend and ncm.get('level') == 'none':
            ncm['level'] = 'unknown'
        ncm['stale'] = True
        if page_on:
            ncm['text'] = '%s Last logged reading: %s' % (A.page_pending_text(page), ncm.get('text') or 'none')
        for t in page_pending['reasons']:
            if t not in alerts and t not in ha_texts:
                alerts.append(t)
    ncm['refused'] = {'open': [{'id': it['id'], 'at': it['at'], 'level': it.get('level'), 'hazard': it.get('hazard'), 'reason': it.get('reason'), 'reasonKind': it.get('reasonKind'),
                                'pendingHazards': it['pendingHazards'], 'text': it['text']} for it in ref_open],
                      'count': ncm_ref['count'], 'dealtWith': len(ncm_ref['resolved']), 'unreadableLines': (ref_unread or {}).get('lines'),
                      'note': 'a reading typed by a person and refused by the log (ncm_refused.jsonl) is a high alert until a valid reading for the same level is logged or python ncm_log.py clear-refused acknowledges it'}
    if ref_open or ref_unread:
        # the NCM text shown beside the level leads with the refusal, so the words never say all clear beside a typed warning that is not in the log (audit N1)
        lead = ref_open[0]['text'] if ref_open else N.unreadable_refused_text(ref_unread)
        ncm['text'] = '%s Last logged reading: %s' % (lead, ncm.get('text') or 'none')
        ncm['stale'] = True
    if high_alerts:
        for ha in reversed(high_alerts):
            if ha['text'] not in alerts:
                alerts.insert(0, ha['text'])
    st_auto = (ncm_auto.get('state') or {})

    # --- models: levels
    level = M.rain_level([{'idx': p['idx'], 'name': p['name'], 'ok': p['ok'], 'models': p['models'], 'point': p['point'], 'gap24': p.get('gap24')} for p in reads], THRESH6, a.level_rule)
    for p in reads:
        p['rainLevel'] = point_level(p, a.level_rule, level)
    any_ok = any(p['ok'] for p in reads)

    # --- observations
    rows_by_station, obs_problems = ({}, [])
    if metar is not None:
        rows_by_station, obs_problems = O.parse_metar_response(metar)
    tafs, taf_problems = ({}, [])
    if taf is not None:
        tafs, taf_problems = O.parse_taf_response(taf)
    # a METAR or TAF stamped more than 10 minutes after the engine clock is not a report yet: it is not merged into the station record and not used in any summary, it is counted and named in an
    # alert (station and stamp), so it can never open a fog or rain event when the clock reaches its hour (audit N2)
    rows_by_station, fut_m = O.split_future(rows_by_station, now)
    tafs, fut_t = O.split_future_tafs(tafs, now)
    future_rejected = fut_m + fut_t
    store = O.ObsStore(data_dir)
    # a record that an earlier run stored from such a report is removed (and noted in the month file) while it is still ahead of the clock. Only on a clock that can be trusted: one checked against the
    # servers' Date, or a clock given with --now that is not behind the laptop's (a replay must never remove real hours): a clock that is behind would call real records future
    purge_ok = trusted or (now_given and now >= utc_now() + dt.timedelta(hours=4) - dt.timedelta(minutes=O.FUTURE_TOL_MIN))
    if purge_ok:
        _guard('the station record purge', lambda: store.purge_future(now), None, alerts)
    else:
        _guard('the station record check', lambda: store.purge_future(now, remove=False), None, alerts)
    store.merge_rows(rows_by_station, now=now)
    future_rejected += store.rejected_future
    if write:
        try:
            store.save()
        except Exception as e:
            alerts.append('the station record could not be written (%s: %s)' % (type(e).__name__, str(e)[:100]))
    alerts.extend(store.problems())
    for x in (O.future_alert(future_rejected, now), store.purge_alert(), store.future_kept_alert()):
        if x:
            alerts.append(x)
    obs = O.obs_block(stations, rows_by_station, now)
    obs['rejectedFuture'] = {'n': len(future_rejected), 'items': future_rejected[:6], 'toleranceMin': O.FUTURE_TOL_MIN, 'purgedRecords': len(store.purged_now),
                             'note': 'reports stamped more than %d minutes after the engine clock are not stored and not used; a stored record of the kind is removed (noted in the month file)' % O.FUTURE_TOL_MIN}
    tafb = O.taf_block(stations, tafs, now)
    for st in stations:
        s = obs['stations'][st['icao']]
        if not s['ok']:
            alerts.append('station %s did not answer (%s)' % (st['icao'], mi.get('error') or s.get('error') or 'no report'))
        elif s.get('silent'):
            alerts.append('station %s silent for %.0f hours (last report %s)' % (st['icao'], s['silentHours'], s['lastReport']))
    fresh_n = obs['any']['stationsFresh']

    # --- fog
    fog = fog_assessment(reads, rows_by_station, ncm_sum, now, store=store, obs=obs, stations=stations, page_pending=({'fogReason': page_pending['fogReason'], 'colours': page_pending['colours']} if fog_pend else None))
    fog_info = {'level': fog['level'], 'basis': fog['basis'], 'observedWhy': fog['observedWhy'], 'forecastWhy': fog['forecastWhy'], 'ncmIds': ncm_sum['ids']['fog']}

    # --- conditions: the stations' 24 hours and latest values, the points' 72 hours with their bands, the comparison, the read only levels (wind, gust, humidity, heat, cold, pressure, dust, visibility)
    stn_cond = _guard('the station conditions', lambda: C.station_blocks(stations, rows_by_station, now), {}, alerts)
    cond_pack = _guard('the conditions block', lambda: K.assemble(now, cond_reads, reads, stations, stn_cond, wind_read), None, alerts)
    if cond_pack:
        cond, cond_levels, cond_pts = cond_pack
        for t in cond['health']['alerts']:
            if t not in alerts:
                alerts.append(t)
    else:
        cond, cond_levels, cond_pts = K.failed_block(now, 'see the alerts'), None, []
    obs_daily, od_problems = ({'schema': 1, 'stations': {}}, [])
    if write:
        obs_daily, od_problems = _guard('the station day record', lambda: R.update_obs_daily(data_dir, rows_by_station, now, write=True), ({'schema': 1, 'stations': {}}, []), alerts)
    else:
        obs_daily = R.read_obs_daily(data_dir)[0]
    for t in od_problems:
        alerts.append(t)

    # --- the combined rain level: model, NCM (in force, or issued and about to start) and what the stations report
    ncm_rain = ncm_sum['levels']['rain']
    ncm_rain_now = ncm_sum['levelsInForce']['rain']
    ncm_to = {'none': 'none', 'yellow': 'watch', 'orange': 'high', 'red': 'high'}[ncm_rain]
    rain_now = obs['any'].get('rainNow')
    comb = level['level']
    why = ['model %s' % level['level']]
    if ncm_to != 'none' and (comb == 'unknown' or M.LEVEL_RANK.get(ncm_to, 0) > M.LEVEL_RANK.get(comb, -1)):
        comb = ncm_to                                  # an NCM colour sets the level at least as high; a none from NCM never turns unknown into none
    if ncm_to != 'none':
        why.append('NCM %s %s (%s)' % (ncm_rain, 'in force' if ncm_rain == ncm_rain_now else 'issued and starting within %d hours' % N.UPCOMING_HOURS, ', '.join(ncm_sum['ids']['rain'])))
    if rain_now and M.LEVEL_RANK.get(comb, -1) < 1:
        comb = 'watch'
        why.append('a station reports rain now (%s)' % ', '.join(obs['any']['rainStations']))
    if rain_pend and comb == 'none':
        comb = 'unknown'
        why.append('a reading that nobody has logged (an NCM warnings page colour, or an unreadable line of the NCM log) could be a rain warning')

    # --- the stage the weather alone would call (an input to the committee), then readiness, sectors, interaction
    worst24, worst24_pt = None, None
    for p in reads:
        if not p['ok']:
            continue
        x24 = p['point'].get('max24Within24Raw') if p['point'].get('max24Within24Raw') is not None else p['point'].get('max24Within24')
        if x24 is not None and (worst24 is None or x24 > worst24):
            worst24, worst24_pt = x24, p['name']
    stage_flags = []
    if page_pending:
        for t in page_pending['reasons']:
            stage_flags.append(t)
    for ha in high_alerts:
        if ha['text'] not in stage_flags:
            stage_flags.append(ha['text'])
    if ncm_rain != ncm_rain_now:
        stage_flags.append('An NCM %s rain warning has been issued and starts within %d hours; it counts from the time it was read.' % (ncm_rain, N.UPCOMING_HOURS))
    # the proposed vote above the plan rule: not applied until the department head decides, but never silent (audit V2)
    vote_gap = a.level_rule == 'plan' and M.LEVEL_RANK[level['voteLevel']] > M.LEVEL_RANK[level['planRuleLevel']]
    vote_info = None
    if vote_gap:
        vmods = level['reach25'] if level['voteLevel'] == 'high' else level['reach5']
        vote_info = {'level': level['voteLevel'], 'planRule': level['planRuleLevel'], 'models': list(vmods), 'applied': False,
                     'text': ('The proposed model vote says %s (%d of %d voting models reach %g mm in 6 h: %s) while the plan rule says %s and is the one applied. The vote is Proposed and is not applied until the department head decides, '
                              'so a %s here is not a reading of no rain: read the models and the NCM page before relying on it.'
                              % (level['voteLevel'], len(vmods), len(level['voters']), M.PROPOSED['high' if level['voteLevel'] == 'high' else 'watch']['mm6h'], ', '.join(vmods), level['planRuleLevel'], level['planRuleLevel']))}
        stage_flags.append(vote_info['text'])
    proposed_vote = proposed_vote_block(level, a.level_rule, vote_gap)
    # the plan's 24 hour band can call a stage that no 6 hour burst reaches (a steady rain): the rain hazard level then says what the stage says (audit F18)
    band_idx = I.rain_band(worst24) if worst24 is not None else 0
    band_level = I.LEVEL_WORD[I.STAGE_LEVEL[I.STAGES[band_idx]]]
    band_basis = ('the plan 24 hour band: %.1f mm in the heaviest 24 hours that begin within the next 24 hours at %s calls %s' % (worst24, (worst24_pt or 'n/a').split(' (')[0], I.STAGES[band_idx])) if band_idx else None
    if band_idx and M.LEVEL_RANK[band_level] > M.LEVEL_RANK.get(comb, -1):                  # unknown ranks below none: a positive stands over it
        why.append(band_basis)
        comb = band_level
    if band_idx and M.LEVEL_RANK[band_level] > M.LEVEL_RANK.get(level['level'], 0) and level['level'] != 'unknown':
        alerts.append('%s, while the 6 hour model level is %s (a steady rain, not a burst): the console header and AL10 and AL11 read the model level and show no signal; the rain hazard level is %s' % (band_basis[0].upper() + band_basis[1:], level['level'], comb))
    rain_basis = 'Rain hazard level %s: the highest of %s. %s' % (comb, '; '.join(why), level['basis'])
    stage_in = {'rain_level_model': level['level'], 'ncm_rain': ncm_rain, 'worst_max6': level['worstMax6Median'], 'worst_max24': worst24, 'worst_point24': worst24_pt, 'plan_thresholds': THRESH6,
                'rain_known': level['level'] != 'unknown' and not rain_pend, 'ncm_upcoming': ncm_rain != ncm_rain_now, 'fog': fog_info, 'flags': stage_flags,
                'station_rain': list(obs['any'].get('rainStations') or []) if rain_now else [], 'vote': vote_info}
    stage_dict = I.stage_block(**stage_in, console_stage=I.read_stage_file(now))

    def readiness_fn(t):
        r = I.readiness(t)
        sf = r.get('stageFile') or {}
        r['stage'] = sf.get('stage')
        r['stageNote'] = None if sf else 'no stage file is written by Barq; the console keeps its stage per device'
        r['weatherStage'] = stage_dict['weatherStage']
        r['fogLevel'] = fog['level']
        return r
    sect_map = _guard('the sector map', lambda: I.sector_map()[0], {}, alerts)
    pt_sector = {}
    for n, s in sect_map.items():
        for i in s['points']:
            pt_sector.setdefault(POINTS[i][0], []).append(n)

    def history_fn(hz, ev):
        # the history extracts of the incident register are a side file: one that has changed shape must not take the event record down with it (audit F9)
        try:
            if hz == 'rain':
                names = [p['name'] for p in ev.get('points', []) if (p.get('accumulationMm') or 0) >= 0.5]
                secs = sorted({n for nm in names for n in pt_sector.get(nm, [])})
            else:
                secs = []
            return I.history_for(hz, secs)
        except Exception as e:
            return {'available': False, 'note': 'the history of the incident register could not be read for this event (%s: %s); owner: the register owner' % (type(e).__name__, str(e)[:80])}

    def ncm_in_span(hz, t0, t1):
        ids = []
        for en in entries:
            if en['level'] == 'none' or hz not in en['hazard']:
                continue
            s, e_ = N.window(en)
            if s <= t1 and N.effective_end(entries, en, hz) >= t0 and e_ >= t0:
                ids.append(en['id'])
        return ids

    # --- events
    reg = E.EventRegister(os.path.join(data_dir, 'events.json'))
    since = now - dt.timedelta(hours=OBS_HOURS)
    gust_past = {}
    for p in reads:
        for lab, g in (p.get('gust_past') or {}).items():
            gust_past[lab] = max(gust_past.get(lab, 0), g)
    ctx = {'now': now, 'points': [{'idx': p['idx'], 'name': p['name'], 'past': p['past'], 'past_vec': p.get('past_vec') or {}} for p in reads if p['ok']], 'gust_past': gust_past, 'store': store, 'stations': stations,
           'ncm_in_span': ncm_in_span, 'history': history_fn, 'readiness': readiness_fn, 'covered': lambda t0, t1: O.covered_hours(store, stations, t0, t1), 'lookback_h': OBS_HOURS,
           'models': list(M.ALL_MODELS)}
    # the quiet clock of an event counts only the hours the engine could see the hazard: models for rain, a west coast station reporting for fog (the east coast alone cannot rule fog out),
    # any station for dust (a feed that fails is not a quiet hour)
    west_ok = bool(obs['any'].get('westFresh')) if obs['any'].get('westStations') else fresh_n > 0
    run_ok = {'rain': level['level'] != 'unknown', 'fog': fog['level'] != 'unknown' and west_ok, 'dust': fresh_n > 0}

    def do_events():
        ev_in = build_evidence(now, {'level': level['level'], 'basis': level['basis'], 'bandLevel': band_level if band_idx else None, 'bandBasis': band_basis}, fog, store, stations, entries, since)
        return reg.update(now, ev_in, run_ok, ctx, trusted_clock=trusted)
    changes = _guard('the event register', do_events, [], alerts)
    alerts.extend(reg.alerts)
    _check_lock()
    if write:
        try:
            reg.save()
        except Exception as e:
            alerts.append('events.json could not be written (%s: %s); the events of this run are not recorded, the weather is published' % (type(e).__name__, str(e)[:100]))
    alerts.extend(reg.problems())
    if reg.save_error:
        alerts.append(reg.save_error)

    # --- season
    run_days = {}
    for d in {d for p in reads if p['ok'] for d in p['daily']}:
        run_days[d] = [(reads[i]['daily'].get(d) if reads[i]['ok'] else None) for i in range(len(POINTS))]
    today = now.strftime('%Y-%m-%d')
    today_partial = {}
    for p in reads:
        if not p['ok']:
            continue
        labs = M.day_labels(today)
        vecs = [v for l, v in (p.get('past_vec') or {}).items() if l in labs and l <= hour_label(now)]
        tot = [v for l, v in p['past'].items() if l in labs and l <= hour_label(now)]
        mt = M.entries_total(vecs) if vecs else None            # the median of the models' own sums, as the finished days are (audit WXR-02)
        today_partial[p['idx']] = mt if mt is not None else (sum(tot) if tot else None)
    calibration = read_json(os.path.join(data_dir, 'calibration.json'), None)
    if write and not isinstance(calibration, dict):
        alerts.append('calibration.json is missing or cannot be read: the comparison with last season, the fog evidence by station and the fog before the engine record are not shown (owner: the weather desk)')
    season, season_problems, verification = None, [], None
    cond_extra = None
    if write:
        def _season_cond():
            cs = R.season_update(data_dir, now, fetcher, cond_pts, write=True)
            for t in cs['problems'][:3]:
                alerts.append('season conditions: ' + t)
            return R.season_extra(cs, obs_daily)
        cond_extra = _guard('the season conditions', _season_cond, None, alerts)
        cond_extra = _guard('the channel season record', lambda: _merge_extra(cond_extra, data_dir), cond_extra, alerts)
    if write:
        sb = _guard('the season record', lambda: S.build(data_dir, now, fetcher, run_days, today_partial, store, stations, entries, reg, calibration, can_fetch=True, cond_extra=cond_extra), None, alerts)
        if sb:
            season, season_problems = sb
        verification = _guard('the verification', lambda: V.run(data_dir, now, write=True, store=store, stations=stations), None, alerts)
    for sp in season_problems[:3]:
        alerts.append('season record: ' + sp)
    if verification:
        if verification.get('ledgerUnreadableLines'):
            alerts.append('%d unreadable line(s) in the ledger were skipped (the lines stay where they are; see verification.json)' % verification['ledgerUnreadableLines'])
        if verification.get('writeError'):
            alerts.append('verification.json could not be written: %s' % verification['writeError'])

    # --- health alerts
    n_none = sum(1 for p in reads if not p['ok'])
    n_lt3 = sum(1 for p in reads if p['ok'] and sum(1 for r in p['models'].values() if r['usable']) < 3)
    if n_none:
        alerts.append('%d of %d points have no model' % (n_none, len(reads)))
    if n_lt3:
        alerts.append('%d of %d points have fewer than 3 usable models' % (n_lt3, len(reads)))
    if level.get('unknownReason'):
        alerts.append('rain level unknown: ' + level['unknownReason'])
    elif level.get('gapHours24'):
        alerts.append('no model has a value for some hours of the next 24 (%s): a hole in the feed; the level shown rests on the hours that have values' % '; '.join('%s %d h' % (k, v) for k, v in sorted(level['gapHours24'].items())))
    if fog.get('unknownReason'):
        alerts.append('fog level unknown: ' + fog['unknownReason'])
    if not any_ok:
        alerts.append('no model answered at any point: modelLevel and rainLevel are unknown and weather.json carries the NCM, station and fog readings of this run without a model forecast')
    # runs that could not be written to the ledger are lost for good: the alert stays in weather.json and the health alerts for three days after the last failure, then it is a note (audit F21)
    lg0 = prev_health.get('ledger') if isinstance(prev_health.get('ledger'), dict) else {}
    ledger_alert_early = False
    if lg0.get('failures'):
        msg0 = '%d run(s) are missing from the ledger: the ledger could not be written at %s (%s)' % (lg0['failures'], lg0.get('lastFailure'), lg0.get('lastError'))
        try:
            recent0 = hours_between(parse_local(lg0.get('lastFailure')), now) <= 72
        except (ValueError, TypeError):
            recent0 = True
        if recent0:
            alerts.append(msg0)
            ledger_alert_early = True
    if vote_info:
        alerts.append(vote_info['text'])
    for p in reads:
        if p.get('zoneRejected'):
            alerts.append('%s: %s' % (p['name'].split(' (')[0], p['zoneRejected']))
    n_unstated = sum(1 for p in reads if p.get('zoneStated') is False)
    if n_unstated:
        alerts.append('%d Open-Meteo answer(s) state no time zone and no utc offset, so their time axis could not be checked against Ras Al Khaimah time' % n_unstated)
    alerts.extend(model_alerts(reads, level)[:4])
    if vote_gap:
        notes.append('the proposed vote says %s (%s) while the plan rule says %s and is applied until the department head decides on the vote (modelLevelProposedVote)' % (
            level['voteLevel'], ', '.join(level['reach5'] if level['voteLevel'] == 'watch' else level['reach25']), level['planRuleLevel']))
    wage = ncm_sum['warningsAgeHours']
    auto_txt = ''
    if ncm_sum['autoAgeHours'] is not None:
        auto_txt = ' (an automatic page read %.0f hours ago showed no warning; it does not count as a reading by a person)' % ncm_sum['autoAgeHours']
    live_run = ncm_auto.get('live') or {}
    if live_run.get('ok') and live_run.get('ageMin') is not None and live_run['ageMin'] <= 90:
        # 1 Nov 2026 (independent review, STANDING-PERSON-AGE-ALARM): NCM is being read, so this is the age of a person's reading, a note, not an alarm that stands every calm day and hides a real one
        if wage is None or wage > 12:
            notes.append('%s; the NCM reader read NCM %.0f minutes before this run' % ('No person has logged an NCM reading' if wage is None else 'No person has logged an NCM reading for %.0f hours' % wage, live_run['ageMin']))
    elif wage is None:
        alerts.append('NCM warnings never read by a person: no reading in the log' + auto_txt)
    elif wage > 12:
        alerts.append('NCM warnings not read for %.0f hours%s' % (wage, auto_txt))
    if ncm_auto.get('warnings', {}).get('alert') and not page_pending:
        alerts.append(ncm_auto['warnings']['alert'])
    if ncm_auto.get('error'):
        alerts.append(ncm_auto['error'])
    for k in ('bulletin', 'warnings'):
        e = (ncm_auto.get(k) or {}).get('error')
        if e and e != 'no fixture' and not e.startswith('not written'):
            notes.append('NCM %s automatic read: %s' % (k, e))
    for pr in (obs_problems + taf_problems + st_problems)[:4]:
        alerts.append(pr)
    if skew is not None and abs(skew) > SKEW_LIMIT_MIN and not corrected and not now_given:
        alerts.append('the laptop clock is %.0f minutes %s the servers: forecast windows may be shifted' % (abs(skew), 'ahead of' if skew > 0 else 'behind'))
    if ncm_bad:
        alerts.append('%d unreadable lines in ncm_log.jsonl were skipped' % ncm_bad)
    if prev_asof and now < prev_asof - dt.timedelta(minutes=5):
        alerts.append('the clock is earlier than the last weather.json (%s): check the laptop clock' % f_local(prev_asof))
    for q in list_quarantined(data_dir):
        alerts.append('a damaged record is kept aside in data/weather: %s (merge it back by hand or remove it; this alert stays until it is gone)' % q.replace('\\', '/'))
    if data_dir and write:
        clean_stale_tmp(data_dir)
        clean_stale_tmp(os.path.dirname(out_path))

    # --- ledger line
    finished = clock_now()
    src = {'openmeteo': source_rollup(fetcher.calls, ('om_all_', 'om_one_')), 'openmeteo_bm': source_rollup(fetcher.calls, ('om_bm_',)),
           'openmeteo_archive': source_rollup(fetcher.calls, ('om_archive_', 'om_past_')), 'awc_metar': source_rollup(fetcher.calls, ('awc_metar',)),
           'awc_taf': source_rollup(fetcher.calls, ('awc_taf',))}
    # an answer that came but held no report for a station is not a clean source (audit WX-21)
    miss_taf = [st['icao'] for st in stations if not tafs.get(st['icao'])]
    if taf is not None and src['awc_taf']['status'] == 'ok' and miss_taf:
        src['awc_taf']['status'] = 'partial' if len(miss_taf) < len(stations) else 'error'
        src['awc_taf'].setdefault('errors', []).append('no TAF in the answer for %s' % ', '.join(miss_taf))
        alerts.append('no TAF received for %s' % ', '.join(miss_taf))
    elif taf is None:
        # an answer that is not JSON (an HTML page), a refusal (429, 404) or a body cut short: no TAF, and the run says so (audit F19)
        alerts.append('the TAF answer could not be read (%s): no aerodrome forecast was used in this run' % (ti.get('error') or 'no answer'))
    miss_metar = [st['icao'] for st in stations if not obs['stations'][st['icao']]['ok']]
    if metar is not None and src['awc_metar']['status'] == 'ok' and miss_metar:
        src['awc_metar']['status'] = 'partial' if len(miss_metar) < len(stations) else 'error'
        src['awc_metar'].setdefault('errors', []).append('no report in the answer for %s' % ', '.join(miss_metar))
    b, w = ncm_auto.get('bulletin', {}), ncm_auto.get('warnings', {})

    def _auto_src(x):
        if not x.get('ran') or x.get('error') == 'no fixture':
            return {'status': 'skipped', 'calls': 0}
        return {'status': 'ok' if x.get('ok') else 'error', 'calls': 1, 'ms': x.get('ms'), 'errors': [x.get('error')] if x.get('error') else []}
    src['ncm_bulletin'] = _auto_src(b)
    src['ncm_warnings'] = _auto_src(w)
    src['openmeteo_cond'] = source_rollup(fetcher.calls, ('om_cond_',))
    src['openmeteo_air'] = source_rollup(fetcher.calls, ('om_air_',))
    src['openmeteo_cond_archive'] = source_rollup(fetcher.calls, ('om_carchive_',))
    src['openmeteo_wind'] = source_rollup(fetcher.calls, ('om_wind_',))
    models_per_point = [sum(1 for r in p['models'].values() if r['usable']) for p in reads]
    src['openmeteo']['models'] = {m: sum(1 for p in reads if p['models'].get(m, {}).get('usable')) for m in M.ALL_MODELS}
    model_status = []
    for m in M.ALL_MODELS:
        n = sum(1 for p in reads if p['models'].get(m, {}).get('usable'))
        reasons = sorted({p['models'][m].get('reason') for p in reads if m in p['models'] and not p['models'][m].get('usable') and p['models'][m].get('reason')})
        model_status.append({'name': m, 'votes': m in M.VOTE_MODELS, 'pointsUsable': n, 'reasons': reasons[:2]})
    # the overall reading quality: the models, lowered when a station did not report, and 'degraded' when no model was read but something else was
    quality = level['readingQuality']
    if any_ok and fresh_n < obs['any']['stationsTotal'] and quality == 'full':
        quality = 'partial'
    if page_pending and quality in ('full', 'partial'):
        quality = 'degraded'
    if not any_ok:
        quality = 'degraded' if (fresh_n > 0 or not ncm.get('stale')) else 'none'
    ledger = {'v': 2, 'run': run_id, 'now': f_local(now), 'started': f_local(started), 'finished': f_local(finished), 'ok': any_ok, 'quality': quality, 'sources': src,
              'level': {'model': level['level'], 'plan': level['planRuleLevel'], 'vote': level['voteLevel'], 'rain': comb, 'worst6': level['worstMax6'], 'rule': a.level_rule,
                        'basis': short_text(level['basis'].split(' Now: ')[-1], 420)},
              'fog': {'level': fog['level'], 'forecast': fog['forecastLevel'], 'observed': fog['observedLevel'], 'ncm': fog['ncmLevel']},
              'ncm': {'id': ncm.get('id'), 'ageH': ncm.get('ageHours'), 'manualAgeH': wage, 'rain': ncm_rain, 'fog': ncm_sum['levels']['fog'], 'pagePending': bool(page_pending), 'refusedOpen': len(ref_open) + (1 if ref_unread else 0)}, 'fogDays': fog['fogDays'],
              'clock': {'skewMin': skew, 'corrected': corrected},
              'points': [], 'obs': {}, 'events': changes}
    for p in reads:
        row = {'i': p['idx']}
        if p['ok']:
            row['m'] = {m: [r1(r['next6']), r1(r['next24']), r1(r['next72']), r1(r['max6']), r1(r['peakMm'])] for m, r in p['models'].items() if r['usable']}
            e = p['ens']
            row['e'] = {'n': e['models'], 'n6': [e['next6']['min'], e['next6']['median'], e['next6']['max']] if e['next6'] else None, 'n24': [e['next24']['min'], e['next24']['median'], e['next24']['max']] if e['next24'] else None,
                        'n72': [e['next72']['min'], e['next72']['median'], e['next72']['max']] if e['next72'] else None, 'x6': [e['max6']['min'], e['max6']['median'], e['max6']['max']] if e['max6'] else None,
                        'aw': e['atWatch'], 'ah': e['atHigh'], 'ap': e['atProposedWatch'], 'ag': e['agree']}
            d0 = now.strftime('%Y-%m-%d')
            ds = [(dt.datetime.strptime(d0, '%Y-%m-%d') + dt.timedelta(days=k)).strftime('%Y-%m-%d') for k in range(3)]
            r3 = lambda x: None if x is None else round(x, 3)
            # the forecast day totals are kept unrounded (3 decimals) so that verification compares them with the unrounded analysis on the same footing (audit WXR-08)
            row['dm'] = [r3(p['daily'].get(d, {}).get('medianRaw')) if p['daily'].get(d, {}).get('medianRaw') is not None else p['daily'].get(d, {}).get('median') for d in ds]
            row['dx'] = [r3(p['daily'].get(d, {}).get('maxRaw')) if p['daily'].get(d, {}).get('maxRaw') is not None else p['daily'].get(d, {}).get('max') for d in ds]
            row['p6'] = p['point'].get('max6')
            fe = M.fog_ensemble(p['fog'])
            row['fog'] = {'f24': len(fe['fog24']), 'f72': len(fe['fog72']), 'dg': len(fe['diag24']), 'mv': fe['minVisM']}
        else:
            row['err'] = (p['error'] or '')[:80]
        ledger['points'].append(row)
    ledger['obs'] = {k: {'n': v.get('n24'), 'last': v.get('lastReport'), 'vis': v.get('visM'), 'rainNow': v.get('rainNow'), 'rainH': v.get('rainAnyHours24'), 'fogH': v.get('fogHours24'),
                         'dustH': v.get('dustHours24'), 'minVis': v.get('minVisM24'), 'gust': v.get('maxGustKmh24')} if v.get('ok') else {'err': v.get('error')} for k, v in obs['stations'].items()}

    ledger['cond'] = K.ledger_block(cond_pts, stn_cond, cond_levels, cond['sources'], cond.get('windfield')) if (cond_pack and cond_levels) else {'err': 'the conditions block was not built in this run'}

    # --- interaction
    rain_rq = level['readingQuality']
    if rain_pend:
        rain_rq = 'degraded'
    elif fresh_n == 0 and rain_rq in ('full',):
        rain_rq = 'partial'
    fog_rq = 'degraded' if fog_pend else ('full' if fog['forecastOk'] and fresh_n >= 2 else ('partial' if fog['forecastOk'] or fresh_n else 'none'))
    hazards = {
        'rain': {'level': comb, 'label': 'Rain: model forecast (Proposed) with the NCM colour (Reported, official) and the station reports (Reported)', 'basis': rain_basis, 'readingQuality': rain_rq,
                 'ageHours': 0.0, 'asof': f_local(now), 'model': {'level': level['level'], 'planRuleLevel': level['planRuleLevel'], 'voteLevel': level['voteLevel'], 'status': 'Model forecast (Proposed)', 'rule': a.level_rule,
                                                                  'figureMm6h': level['worstMax6'],
                                                                  'proposedVote': {'level': level['voteLevel'], 'applied': a.level_rule == 'proposed', 'aboveRule': bool(vote_gap), 'models': (vote_info or {}).get('models'),
                                                                                   'note': 'worked out beside the plan rule; applied to modelLevel only when the rule is decided (--level-rule proposed); when it is above the plan rule it is an alert, a flag on the stage and a row in the actions due now'}},
                 'ncm': {'level': ncm_rain, 'levelInForce': ncm_rain_now, 'status': 'Reported, official', 'ids': ncm_sum['ids']['rain'], 'upcoming': [e['id'] for e in ncm_sum['upcoming'] if 'rain' in e['hazard']],
                         'ageHours': wage, 'stale': ncm.get('stale'), 'pagePending': rain_pend},
                 'observed': {'rainNow': rain_now, 'stations': obs['any']['rainStations'], 'status': 'Reported'}},
        'fog': {'level': fog['level'], 'label': 'Fog: model forecast (Proposed diagnostic ladder), station reports (Reported) and the NCM colour (Reported, official)', 'basis': fog['basis'],
                'readingQuality': fog_rq, 'ageHours': 0.0, 'asof': f_local(now), 'forecastLevel': fog['forecastLevel'], 'observedLevel': fog['observedLevel'], 'stationsFresh': fresh_n,
                'ncm': {'level': ncm_sum['levels']['fog'], 'status': 'Reported, official', 'ids': ncm_sum['ids']['fog']}},
        'dust': {'level': None, 'label': 'Dust: reported only (Reported); no level, no calibration proposes one', 'reported': {'stationsNow': obs['any']['dustStations'], 'hours24': {k: v.get('dustHours24') for k, v in obs['stations'].items() if v.get('ok')},
                                                                                                                              'tafDust24': tafb['any'].get('dust')}, 'ncm': {'level': ncm_sum['levels']['dust'], 'status': 'Reported, official'}},
    }
    hazards['rain']['model']['proposedVote']['basis'] = proposed_vote['basis']
    if cond_levels:
        # the read only hazards of the weather channel: wind, gust, humidity, heat, cold, pressure, visibility, and the level of dust that was reported only before. They do not change the stage.
        for k, h in K.hazards_from(cond_levels).items():
            hazards[k] = dict(h, asof=f_local(now))
        dz = cond_levels['dust']
        hazards['dust'].update({'level': dz['level'], 'label': dz['label'], 'basis': dz['basis'], 'forecast': dz['forecast'], 'observed': dz['observed'], 'thresholds': dz['thresholds'], 'status': dz['status'],
                                'readOnly': True, 'inStage': False, 'planValue': None, 'planNote': dz['planNote'], 'caveat': dz['caveat'], 'asof': f_local(now), 'evidence': dz['evidence']})
        hazards['dust']['ncm'] = {'level': ncm_sum['levels']['dust'], 'status': 'Reported, official'}
    triggers, trig_problems = I.load_triggers_checked(data_dir)

    def do_interaction():
        return I.build(now, {p['idx']: p['rainLevel'] for p in reads}, fog['pointLevels'], hazards, stage_in, triggers, {p['idx']: (p['point'] or {}) for p in reads}, None,
                       stage_pre=stage_dict, data_dir=data_dir, extra_gaps=trig_problems + st_problems, fog_info=fog_info, vote_info=vote_info)
    interaction = _guard('the interaction block', do_interaction, None, alerts)
    if interaction is None:
        interaction = {'schema': 1, 'asof': f_local(now), 'status': 'the link to the plan could not be built in this run', 'hazards': hazards, 'stage': stage_dict, 'sectors': [],
                       'actions': {'listRead': False, 'dueNow': [], 'notes': ['the interaction block could not be built in this run: no action list is shown']},
                       'planGaps': ['the interaction block could not be built in this run (see the health alerts)'], 'gaps': []}
    else:
        acts = interaction.get('actions') or {}
        if not acts.get('listRead'):
            alerts.append('no action list was read (actions17.json): the actions due now cannot be listed, so "0 actions due" is not reassurance (owner: the plan builder)')
        for g in (interaction.get('planGaps') or [])[:4]:
            if not g.startswith('actions17.json: no action list'):
                alerts.append('plan file gap: ' + g)
    interaction['events'] = {'rain': reg.summary('rain'), 'fog': reg.summary('fog'), 'dust': reg.summary('dust')}
    interaction['proposedVote'] = proposed_vote                     # the same block in interaction.json and in weather.json (audit V2)
    scrubbed = scrub_text(interaction)
    if write:
        try:
            atomic_write_json(os.path.join(data_dir, 'interaction.json'), interaction, indent=1)
        except Exception as e:
            alerts.append('interaction.json could not be written (%s: %s)' % (type(e).__name__, str(e)[:100]))

    # --- weather.json
    points_out = []
    for p in reads:
        if not p['ok']:
            points_out.append({'name': p['name'], 'lat': p['lat'], 'lon': p['lon'], 'error': (p['error'] or 'no data')[:120]})
            continue
        pt = p['point']
        mid, lo, hi = p['band']
        ens = dict(p['ens'])
        ens['perModel'] = {m: M.public_model(r) for m, r in p['models'].items()}
        ens['voters'] = [m for m in M.VOTE_MODELS if p['models'][m]['usable']]
        points_out.append({'name': p['name'], 'lat': p['lat'], 'lon': p['lon'], 'next6': pt['next6'], 'next24': pt['next24'], 'next72': pt['next72'], 'max6': pt['max6'], 'peakAt': pt['peakAt'],
                           'peakMm': pt['peakMm'], 'probMax24': p['probMax24'] if p['probMax24'] is not None else None, 'probMax24Read': p['probMax24'] is not None, 'weatherCodeMax24': p.get('weatherCodeMax24'),
                           'gustMax24': pt['gustMax24'], 'hourly': pt['hourly'], 'max24': pt['max24'], 'max24Within24': pt.get('max24Within24'),
                           'rainLevel': p['rainLevel'], 'modelsUsable': sum(1 for r in p['models'].values() if r['usable']), 'ens': ens,
                           'hourlyBand': {'from': pt['hourly'][0][0] if pt['hourly'] else None, 'median': [r1(x) for x in mid], 'min': [r1(x) for x in lo], 'max': [r1(x) for x in hi]},
                           'daily': [{'d': d, 'median': v['median'], 'max': v['max'], 'n': v['n']} for d, v in sorted(p['daily'].items()) if d >= now.strftime('%Y-%m-%d')][:7]})
    fog_points = []
    for p in reads:
        fe = M.fog_ensemble(p['fog']) if p.get('fog') else None
        if not p['ok'] or not fe:
            fog_points.append({'name': p['name'], 'error': p['error']})
            continue
        lo, mid, hi = M.vis_band(p['fog'])
        fog_points.append({'name': p['name'], 'rulePoint': p['name'].startswith(FOG_PREFIXES), 'ruleLevelNow': fog['pointLevels'].get(p['idx']), 'ens': {k: v for k, v in fe.items() if k != 'note'},
                           'models': {m: {'vi': f['visInformative'], 'f24': f['fogHours24'], 'f72': f['fogHours72'], 'm24': f['mistHours24'], 'd24': f['diagHours24'], 'min': f['minVisM']} for m, f in p['fog'].items() if f},
                           'visBandM': {'from': p['point']['hourly'][0][0] if p['point'] and p['point']['hourly'] else None, 'min': lo, 'median': mid, 'max': hi}})
    worst = level['worstMax6']
    by_station_fog = {}
    for st in stations:
        try:
            by_station_fog[st['icao']] = FOG_NAMES[O.observed_fog({st['icao']: rows_by_station.get(st['icao'], [])}, now, store=store, stations=[st])[0]]
        except Exception:
            by_station_fog[st['icao']] = None
    fog_block = {'status': 'Model forecast (Proposed diagnostic) for the models; Reported for the stations; Reported, official for NCM', 'level': fog['level'], 'basis': fog['basis'],
                 'forecast': {'level': fog['forecastLevel'], 'models': fog['models'], 'pointsEvaluated': fog['evaluated'], 'pointsRule': fog['rulePoints'], 'why': fog['forecastWhy'],
                              'byDay': [{'d': d, **{k: v for k, v in e.items() if k != 'lv'}, 'level': FOG_NAMES[e['lv']]} for d, e in sorted(fog['byDay'].items())],
                              'note': 'calibrated ladder on the dew point spread; the model visibility field is a diagnostic only (it caught 16 percent of fog nights); the forecast side shows the 02:00 to 08:00 window of the next morning from 10:00 onward, so it can read alert at any hour of the day',
                              'calibratedSeason': 'October to March', 'outOfSeason': fog['outOfSeason']},
                 'observed': {'level': fog['observedLevel'], 'why': fog['observedWhy'], 'stationsFresh': fresh_n, 'westStationsFresh': fog['westFresh'], 'fromStoredRecord': fog['observedFromStore'], 'byStation': by_station_fog,
                              'note': 'the observed level is the highest of the stations (the calibration reads the three together); byStation says which station sets it',
                              'stations': {k: {x: v.get(x) for x in ('fogNow', 'mistNow', 'hazeNow', 'visM', 'fogHours24', 'mistHours24', 'minVisM24', 'minVisAt24', 'minVisNote')} for k, v in obs['stations'].items() if v.get('ok')},
                              'taf': {k: v.get('fogForecast') for k, v in tafb['stations'].items() if v.get('ok')}},
                 'official': {'ncmLevel': fog['ncmLevel'], 'recent': fog_recent(entries, ncm_sum), 'inForce': [{'id': e['id'], 'level': e['level'], 'from': e.get('from'), 'to': e.get('to'), 'areas': e.get('areas'), 'rakNamed': e.get('rakNamed')} for e in ncm_sum['inForce'] if 'fog' in e['hazard']],
                              'upcoming': [{'id': e['id'], 'level': e['level'], 'from': e.get('from'), 'to': e.get('to'), 'rakNamed': e.get('rakNamed')} for e in ncm_sum['upcoming'] if 'fog' in e['hazard']],
                              'rakNamed': 'yes, no or unknown per entry; an entry that says it does not name Ras Al Khaimah does not set the level'},
                 'points': fog_points}
    ncm['text'] = short_text(ncm.get('text'), 1200)                       # the whole text stays in ncm_log.jsonl; weather.json is embedded by the console and stays under its size limit (audit N5)
    # the interaction block that goes into weather.json carries the read only hazards in their compact form (the word, the basis, the two sides in short); the full objects are in interaction.json
    iw = dict(interaction)
    if cond_levels and isinstance(interaction.get('hazards'), dict):
        hz = dict(interaction['hazards'])
        for k in ('wind', 'gust', 'humidity', 'heat', 'cold', 'pressure', 'visibility'):
            if isinstance(hz.get(k), dict):
                hz[k] = dict(K.compact_level(hz[k]), asof=hz[k].get('asof'), label=hz[k].get('label'))
        iw['hazards'] = hz
    # conditions.json: the public subset (asof, units, points with now, hourly arrays, bands, daily and extremes, the stations, the levels with their basis, the health words); no ledger, no events, no NCM text.
    # It is written before weather.json so that a failure to write it is an alert in weather.json. A file that is too long is trimmed in steps and says so. The earlier file is replaced whole or not at all.
    cond_path = os.path.abspath(a.conditions_out) if a.conditions_out else os.path.join(os.path.dirname(out_path), 'conditions.json')
    # The whole wind field (49 hourly stamps by 150 nodes, 76 to 90 KB) never fits beside the eight points: the field goes on the schedule "every hour to 24 hours ahead, then every 3 hours" (said inside
    # the field), thinner when the file needs it, and the points are trimmed only when even that is not enough. The notes say what was left out.
    cond_file = cond
    if K.json_size(cond_file) > CONDITIONS_MAX_BYTES:
        cond_file, fit_notes, tr_ = K.fit_conditions(cond, CONDITIONS_MAX_BYTES)
        notes.extend(fit_notes)
        if tr_:
            alerts.append('conditions.json was over %d KB and was trimmed in steps (%s); the readings of the stations and the levels are whole' % (CONDITIONS_MAX_BYTES // 1000, '; '.join(tr_)))
    cond_w = K.for_weather(cond)                                  # weather.json carries the wind field of the current hour and the next 6 hours only (the first paint without a network)
    cond_written = False
    if write:
        try:
            scrub_text(cond_file)
            atomic_write_json(cond_path, cond_file)
            cond_written = True
        except Exception as e:
            alerts.append('conditions.json could not be written (%s: %s): the file on disk is the earlier one and carries its own time; the console shows no reading beyond that' % (type(e).__name__, str(e)[:100]))
    weather = {'asof': f_local(now), 'tz': TZ_NAME, 'source': SOURCE_TEXT, 'thresholds': dict(THRESH6), 'modelLevel': level['level'], 'worstMax6': worst, 'points': points_out, 'ncm': ncm,
               'schema': SCHEMA, 'fetchedUtc': (utc_now()).strftime('%Y-%m-%dT%H:%M:%SZ'), 'readingQuality': quality, 'levelBasis': level['basis'], 'modelLevelPlanRule': level['planRuleLevel'],
               'modelLevelProposedVote': level['voteLevel'], 'proposedVote': proposed_vote, 'levelRule': a.level_rule, 'activeThresholds': level['activeThresholds'], 'worstMax6Median': level['worstMax6Median'],
               'rainLevel': comb, 'rainBasis': rain_basis, 'vote': {'voters': level['voters'], 'reach5': level['reach5'], 'reach25': level['reach25'], 'perModel': level['votes']},
               'levelText': {'modelLevel': 'Model rain: ' + LEVEL_TEXT[level['level']], 'rainLevel': 'Rain hazard: ' + LEVEL_TEXT[comb], 'fogLevel': 'Fog: ' + FOG_TEXT[fog['level']]},
               'clock': {'skewMinutes': skew, 'corrected': corrected, 'note': ('the laptop clock was off by more than %d minutes: the servers\' time was used for this run' % SKEW_LIMIT_MIN) if corrected else None},
               'proposedThresholds': M.proposed_thresholds_block(level), 'models': model_status,
               'coverage': {'points': len(reads), 'pointsRead': sum(1 for p in reads if p['ok']), 'pointsWith3Models': level['pointsWith3'], 'modelsPerPoint': models_per_point,
                            'stationsAnswered': obs['any']['stationsAnswered'], 'stationsFresh': fresh_n, 'stationsTotal': obs['any']['stationsTotal']},
               'fogLevel': fog['level'], 'fogBasis': fog['basis'], 'fog': fog_block, 'obs': obs, 'taf': tafb, 'openEvent': {h: reg.summary(h) for h in ('rain', 'fog', 'dust')},
               'eventCounts': reg.counts(),
               'season': S.summary(season) if season else None, 'verification': V.summary(verification) if verification else None, 'interaction': iw, 'conditions': cond_w}
    lg = prev_health.get('ledger') if isinstance(prev_health.get('ledger'), dict) else {}
    # the model feed's own last good time: this run when a model answered, else what health.json (or an earlier weather.json that held a model reading) says
    last_good = f_local(now) if any_ok else (prev_health.get('lastGoodAsof') or (f_local(prev_asof) if (prev_asof and prev_weather and prev_weather.get('modelLevel') not in (None, 'unknown')) else None))
    try:
        feed_age = 0.0 if any_ok else (round(hours_between(parse_local(last_good), now), 1) if last_good else None)
    except (ValueError, TypeError):
        feed_age = None
    health = update_health(data_dir, now, src, alerts, {'lastRun': {'id': run_id, 'now': f_local(now), 'ok': any_ok, 'exit': 0 if any_ok else 2, 'published': True, 'quality': quality},
                                                         'readingQuality': quality, 'modelsPerPoint': models_per_point, 'pointsRead': sum(1 for p in reads if p['ok']), 'pointsTotal': len(reads),
                                                         'ncmAgeHours': ncm.get('ageHours'), 'ncmWarningsManualAgeHours': wage, 'ncmAutoAgeHours': ncm_sum['autoAgeHours'],
                                                         'feedAgeHours': feed_age, 'lastGoodAsof': last_good, 'clockSkewMinutes': skew, 'clockCorrected': corrected, 'stationsFresh': fresh_n,
                                                         'ledger': lg, 'notes': notes, 'highAlerts': high_alerts, 'conditions': {k: cond['health'].get(k) for k in ('status', 'parameters', 'alerts', 'levelWords', 'words', 'windfield')},
                                                         'futureReports': {'rejectedThisRun': len(future_rejected), 'purgedThisRun': len(store.purged_now),
                                                                           'rejectedTotal': ((prev_health.get('futureReports') or {}).get('rejectedTotal') or 0) + len(future_rejected) if isinstance(prev_health.get('futureReports'), dict) else len(future_rejected),
                                                                           'lastAt': f_local(now) if (future_rejected or store.purged_now) else ((prev_health.get('futureReports') or {}).get('lastAt') if isinstance(prev_health.get('futureReports'), dict) else None),
                                                                           'items': future_rejected[:6]}})
    weather['health'] = {k: health[k] for k in ('asof', 'lastRun', 'readingQuality', 'modelsPerPoint', 'pointsRead', 'pointsTotal', 'ncmAgeHours', 'ncmWarningsManualAgeHours', 'ncmAutoAgeHours', 'feedAgeHours', 'lastGoodAsof', 'alerts', 'sources', 'clockSkewMinutes', 'highAlerts', 'conditions')}

    def dump_():
        n_sur = scrub_text(weather) + scrubbed
        if n_sur:
            # a lone surrogate (a \ud800 escape in a METAR or an NCM text, valid JSON) cannot be written as UTF-8: it is replaced by U+FFFD, said once, and the run publishes (audit N4)
            msg0 = '%d character(s) that cannot be written as text (a lone surrogate in a METAR or an NCM text) were replaced by a replacement character in the records of this run; the original stays in the source' % n_sur
            if msg0 not in alerts:
                alerts.append(msg0)
        try:
            s_ = json.dumps(weather, ensure_ascii=False, separators=(',', ':'), allow_nan=False)      # a NaN would break JSON.parse in the console: never written
        except ValueError:
            # a number JSON cannot carry (infinite, not a number) is written as null and the run says so: a run that publishes nothing is worse than a null (audit F15)
            n_ = [0]
            _finite(weather, n_)
            msg_ = '%d number(s) that cannot be written as JSON (infinite or not a number) were replaced by null in weather.json' % n_[0]
            if msg_ not in alerts:
                alerts.append(msg_)
            s_ = json.dumps(weather, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        # the console builder embeds weather.json raw in a script block: a free text (a manual NCM text) holding </script> or <!-- would end the script, so a < before / or ! is written as <,
        # which every JSON reader decodes back to < (audit V5)
        return SCRIPT_UNSAFE.sub(r'\\u003c', s_)

    def serialise():
        body_ = dump_()
        size_ = len(body_.encode('utf-8'))
        if size_ > WEATHER_MAX_BYTES and isinstance(weather.get('conditions'), dict) and weather['conditions'].get('points'):
            # keep the file under 260 KB: the console embeds it. The conditions copy is trimmed first (the secondary hourly arrays, then the rest of the arrays; the full block is in conditions.json,
            # which the console fetches), and it says what it left out
            others = size_ - K.json_size(weather['conditions'])
            weather['conditions'], _tr = K.shrink_for_weather(cond_w, max(10000, WEATHER_MAX_BYTES - others - 3000))
            body_ = dump_()
            size_ = len(body_.encode('utf-8'))
        if size_ > WEATHER_MAX_BYTES:                     # then the heavy parts of the interaction block
            for sct in weather['interaction'].get('sectors', []):
                sct.pop('resources', None)
            weather['interaction'].pop('history', None)
            weather['interaction']['trimmed'] = 'resources and history left out of this copy to keep weather.json under its size limit; the full block is in interaction.json'
            body_ = dump_()
            size_ = len(body_.encode('utf-8'))
        return body_, size_
    body, size = serialise()

    # --- publish first, then the bookkeeping that depends on whether it worked (audit WX-12, WX-13, WX-14)
    published, publish_error = False, None
    _check_lock()
    if write:
        try:
            atomic_write_text(out_path, body)
            published = True
        except Exception as e:
            publish_error = ('%s: %s' % (type(e).__name__, e))[:150]
    ledger_info, ledger_error = None, None
    if write and not a.no_write_ledger:
        ledger['published'] = published
        scrub_text(ledger)
        try:
            ledger_info = E.append_ledger(data_dir, ledger, finished)
        except Exception as e:
            ledger_error = ('%s: %s' % (type(e).__name__, e))[:120]
            print('LEDGER FAILED: %s' % ledger_error, file=sys.stderr)
    if lg.get('failures') and not ledger_error and not ledger_alert_early:
        notes.append('%d run(s) are missing from the ledger: the ledger could not be written at %s (%s)' % (lg['failures'], lg.get('lastFailure'), lg.get('lastError')))
    if ledger_error:
        n_fail = (lg.get('failures') or 0) + 1
        lg = {'failures': n_fail, 'lastFailure': f_local(now), 'lastError': ledger_error}
        alerts.append('the ledger line of this run could not be written (%s); %d run(s) are missing from the ledger since the failures began, so the record is being lost until it is fixed' % (ledger_error, n_fail))
        health['ledger'] = lg
        if published:
            body, size = serialise()                      # weather.json carries the alert too (a second write, only when the ledger failed)
            try:
                atomic_write_text(out_path, body)
            except Exception as e:
                published, publish_error = False, ('%s: %s' % (type(e).__name__, e))[:150]
    if write and not published:
        alerts.append('weather.json could not be written (%s): the file on disk is still the earlier one (%s)' % (publish_error, f_local(prev_asof) if prev_asof else 'none'))
        health['lastRun'].update({'ok': False, 'exit': 1, 'published': False})
        health['lastGoodAsof'] = prev_health.get('lastGoodAsof') or (f_local(prev_asof) if prev_asof else None)
        try:
            health['feedAgeHours'] = round(hours_between(parse_local(health['lastGoodAsof']), now), 1) if health['lastGoodAsof'] else None
        except (ValueError, TypeError):
            health['feedAgeHours'] = None
        health['weatherBytes'] = prev_health.get('weatherBytes')
    else:
        health['weatherBytes'] = size if write else None
    if write:
        scrub_text(health)
        try:
            atomic_write_json(os.path.join(data_dir, 'health.json'), health, indent=1)
        except Exception as e:
            print('HEALTH FAILED: %s' % e, file=sys.stderr)
            # health.json keeps the old lastRun, so the card's engine panel would show an old run as the current one: weather.json says so (a second write, as for the ledger), audit N9
            alerts.append('health.json could not be written (%s: %s): the engine panel of the card reads the earlier health.json and shows an older run as the last; this run is the one in weather.json' % (type(e).__name__, str(e)[:100]))
            if published:
                body, size = serialise()
                try:
                    atomic_write_text(out_path, body)
                except Exception as e2:
                    print('WEATHER SECOND WRITE FAILED: %s' % e2, file=sys.stderr)
    if write and not published:
        code = 1
    elif not any_ok:
        code = 2
    else:
        code = 0
    res = {'code': code, 'run': run_id, 'now': f_local(now), 'level': level['level'], 'rainLevel': comb, 'fogLevel': fog['level'], 'worstMax6': worst, 'quality': quality, 'bytes': size,
           'pointsRead': sum(1 for p in reads if p['ok']), 'alerts': alerts, 'changes': changes, 'ledger': ledger_info, 'weather': weather, 'ledgerLine': ledger, 'out': out_path,
           'ncmAuto': ncm_auto, 'weatherStage': interaction['stage']['weatherStage'], 'published': published, 'publishError': publish_error, 'conditions': cond, 'conditionsWritten': cond_written,
           'conditionsPath': cond_path, 'conditionsBytes': K.json_size(cond_file), 'conditionsFileWindfield': cond_file.get('windfield'), 'conditionLevels': {k: v['level'] for k, v in (cond_levels or {}).items() if isinstance(v, dict) and 'level' in v}}
    return code, res


LOCK_STALE_S = 900


def _lock_pid(path):
    try:
        with open(path, encoding='utf-8') as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


LOCK_EMPTY_STALE_S = 120
_HELD_LOCK = [None]


class LockLost(BaseException):
    """this run's lock was taken over (a run suspended by a sleeping laptop, then taken for hung): it must not write anything. A BaseException so that no 'except Exception' of the
    bookkeeping swallows it (audit F13)"""


def _check_lock():
    lock = _HELD_LOCK[0]
    if lock and _lock_pid(lock) != os.getpid():
        raise LockLost('the lock of this run was taken over by another run: this run writes nothing more')


def _lock_is_stale(path):
    """a lock is stale when it is older than 15 minutes (a hung run), when the process that made it no longer exists (a killed run: no 15 minute wait, audit WX-18), or when it holds no
    process id at all and is a couple of minutes old (a write that failed, a full disk: audit F13)"""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    if age > LOCK_STALE_S:
        return True
    pid = _lock_pid(path)
    if pid is None:
        return age > LOCK_EMPTY_STALE_S
    return pid != os.getpid() and pid_alive(pid) is False


def _take_over(path):
    """take a stale lock over without a race: only the holder of the claim file may remove it, and it checks again under the claim that the lock is still the stale one, so a
    second starter can never remove the new lock of the first (audit WX-18)"""
    claim = path + '.takeover'
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(claim) > 60:           # a claim left by a takeover that crashed
                os.remove(claim)
        except OSError:
            pass
        return False
    except OSError:
        return False
    try:
        if _lock_is_stale(path):
            os.remove(path)
            return True
        return False
    except OSError:
        return False
    finally:
        try:
            os.remove(claim)
        except OSError:
            pass


def acquire_lock(data_dir):
    """one engine at a time: a second run (Barq by hand while the hourly task runs) would race on events.json and the ledger. A stale lock (older than 15 minutes, or left
    by a process that is gone) is taken over atomically. Returns the lock path, or None when another run holds it."""
    path = os.path.join(data_dir, '.engine.lock')
    os.makedirs(data_dir, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not (_lock_is_stale(path) and _take_over(path)):
                return None
            continue
        try:
            os.write(fd, ('%d %s' % (os.getpid(), f_local(now_local()))).encode())
        except OSError:
            # a lock file with nothing in it would block every run for a quarter of an hour: take it away again, and say the lock could not be written (audit F13)
            os.close(fd)
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        os.close(fd)
        return path
    return None


def acquire_lock_wait(data_dir, wait_s=0.0, poll_s=1.0):
    """acquire_lock, polling for up to wait_s seconds while another run holds it (a manual NCM reading that arrives while the scheduled run is working must be published
    after it, not dropped: audit F5)"""
    end = time.monotonic() + max(0.0, wait_s)
    while True:
        lock = acquire_lock(data_dir)
        if lock or time.monotonic() >= end:
            return lock
        time.sleep(max(0.05, min(poll_s, end - time.monotonic())))


def release_lock(lock):
    """remove the lock only if it is ours"""
    try:
        if _lock_pid(lock) in (None, os.getpid()):
            os.remove(lock)
    except OSError:
        pass


def crash_note(data_dir, now, text):
    """an unexpected error is recorded in health.json (when the folder can be written), so the card and the next reader see it. health.json keeps the last good time the
    last good run wrote: the crash happened before this run published anything."""
    try:
        p = os.path.join(data_dir, 'health.json')
        h = read_json(p, {}) or {}
        h['asof'] = f_local(now)
        h['lastRun'] = {'id': None, 'now': f_local(now), 'ok': False, 'exit': 1, 'published': False, 'quality': 'none', 'error': text[:300]}
        h['alerts'] = ['the engine stopped on an error: ' + text[:200]] + [a for a in (h.get('alerts') or []) if not a.startswith('the engine stopped')]
        atomic_write_json(p, h, indent=1)
    except Exception:
        pass


CHANNEL_TIMEOUT_S = 90               # the weather channel may take this long at the end of a run; then it is stopped and the engine goes on (25 Oct 2026)


def _merge_extra(cond_extra, data_dir):
    """the season record gains the channel's time at each rung, notifications by tier and median time to acknowledge (weather_channel.season_extra, read only)"""
    import weather_channel as CH
    ex = CH.season_extra(data_dir)
    if not ex:
        return cond_extra
    if not cond_extra:
        return ex
    row0, top0 = cond_extra.get('row'), dict(cond_extra.get('top') or {})
    top0.update(ex['top'])
    return {'row': (lambda d: dict((row0(d) if row0 else None) or {}, **(ex['row'](d) or {}))), 'top': top0}


def _channel_after(a0):
    """25 Oct 2026 (weather channel, C6): run the weather channel once at the end of every engine run, in a separate process that is stopped after CHANNEL_TIMEOUT_S seconds (WCH_CHANNEL_TIMEOUT),
    so that a channel that fails, hangs or is missing never fails or delays the engine: the engine exit code and weather.json are what they would have been. A failure is printed as
    CHANNEL FAILED, written into channel_log.jsonl and into the channel health (channel_state.json). --channel auto (the default) skips a dry run, a fixture run and a run on a given clock;
    on runs it whatever the others say; off never runs it. Returns the failure note, or None."""
    mode = getattr(a0, 'channel', 'auto')
    if mode == 'off' or (mode == 'auto' and (a0.dry_run or a0.fixture or a0.now)):
        return None
    script = os.environ.get('WCH_CHANNEL_SCRIPT') or os.path.join(HERE, 'weather_channel.py')
    try:
        limit = float(os.environ.get('WCH_CHANNEL_TIMEOUT') or CHANNEL_TIMEOUT_S)
    except ValueError:
        limit = CHANNEL_TIMEOUT_S
    data_dir = os.path.abspath(a0.data_dir)
    out = os.path.abspath(a0.out)
    cond = os.path.abspath(a0.conditions_out) if a0.conditions_out else os.path.join(os.path.dirname(out), 'conditions.json')
    cmd = [sys.executable, script, 'run', '--data-dir', data_dir, '--staging', out, '--conditions', cond]
    if a0.now:
        cmd += ['--now', a0.now]
    note = None
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=limit, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        txt = p.stdout.decode('utf-8', 'replace').strip().splitlines()
        if p.returncode == 0:
            print('channel: ' + (txt[0] if txt else 'ran'))
        else:
            err = (p.stderr.decode('utf-8', 'replace').strip().splitlines() or txt or [''])[-1]
            note = 'the weather channel exited with code %d (%s)' % (p.returncode, short_text(err, 100))
    except subprocess.TimeoutExpired:
        note = 'the weather channel did not finish within %d seconds and was stopped' % limit
    except Exception as e:
        note = 'the weather channel could not be started (%s: %s)' % (type(e).__name__, str(e)[:80])
    if note:
        print('CHANNEL FAILED: %s; the engine run is not affected' % note)
        try:
            import weather_channel as CH
            CH.note_failure(data_dir, now_local(), note)
        except Exception as e:
            print('CHANNEL FAILURE NOT RECORDED: %s' % e)
    return note


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    a0 = parse_args(args)
    data_dir = os.path.abspath(a0.data_dir)
    lock = None
    if not a0.dry_run:
        try:
            lock = acquire_lock_wait(data_dir, a0.wait_lock)
        except OSError as e:
            # the data folder cannot be created or written: a clear line on the screen, in the task log and beside weather.json, never a raw traceback with no note anywhere (audit F13)
            msg = 'ENGINE FAILED: the data folder %s cannot be used (%s: %s); nothing was written and weather.json is the earlier one' % (data_dir, type(e).__name__, str(e)[:100])
            print(msg)
            print(msg, file=sys.stderr)
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(a0.out)), 'weather_engine_last_error.txt'), 'w', encoding='utf-8') as f:
                    f.write('%s %s\n' % (f_local(now_local()), msg))
            except OSError:
                pass
            return 1
        if lock is None:
            print('ENGINE BUSY: another run holds %s; nothing was written' % os.path.join(data_dir, '.engine.lock'))
            return 1
    _HELD_LOCK[0] = lock
    try:
        code, r = run(args)
    except LockLost as e:
        print('ENGINE SUPERSEDED: %s; weather.json was not changed by this run' % e)
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        crash_note(data_dir, parse_local(a0.now) if a0.now else now_local(), '%s: %s' % (type(e).__name__, e))
        print('ENGINE FAILED: an unexpected error stopped the run; weather.json was not changed')
        return 1
    finally:
        _HELD_LOCK[0] = None
        if lock:
            release_lock(lock)
    _channel_after(a0)
    if code == 0:
        print('weather.json %s model level %s worst 6 h %s' % (r['now'], r['level'], ('%s mm' % r['worstMax6']) if r['worstMax6'] is not None else 'not read'))
    elif code == 2:
        print('ENGINE DEGRADED: no model answered at any point; weather.json %s was written with modelLevel unknown and the NCM, station and fog readings of this run' % r['now'])
    else:
        print('ENGINE FAILED: weather.json could not be written (%s); the file on disk is the earlier one (%s)' % (r.get('publishError'), r['out']))
    print('rain hazard %s | fog %s | weather stage %s | reading quality %s | %d of %d points read | %d bytes' % (r['rainLevel'], r['fogLevel'], r['weatherStage'], r['quality'], r['pointsRead'], len(POINTS), r['bytes']))
    cw = (r.get('conditions') or {}).get('sources') or {}
    if cw:
        print('conditions: forecast %s %s of %s points | air quality %s | stations %s of %s fresh | wind %s gust %s humidity %s heat %s cold %s pressure %s dust %s visibility %s | %s bytes' % (
            cw['forecast']['status'], cw['forecast']['pointsRead'], cw['forecast']['pointsTotal'], cw['air']['status'], cw['stations']['pointsRead'], cw['stations']['pointsTotal'],
            *[r['conditionLevels'].get(k, 'n/a') for k in ('wind', 'gust', 'humidity', 'heat', 'cold', 'pressure', 'dust', 'visibility')], r.get('conditionsBytes')))
        wfs = cw.get('wind') or {}
        print('wind field: %s, %s of %s nodes read, %s distinct model cells, %s hours in conditions.json' % (wfs.get('status'), wfs.get('nodesRead'), wfs.get('nodesTotal'), (r['conditions'].get('windfield') or {}).get('cells'),
                                                                                                    len((r.get('conditionsFileWindfield') or {}).get('hours') or [])))
    if r['changes']:
        for c in r['changes']:
            print('event: ' + c)
    for al in r['alerts'][:12]:
        print('ALERT: ' + al)
    return code


if __name__ == '__main__':
    sys.exit(main())
