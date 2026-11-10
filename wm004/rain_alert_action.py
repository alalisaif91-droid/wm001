# -*- coding: utf-8 -*-
"""WM004 rain on the way: a message to the owner's phone when the model forecasts rain (10 Nov 2026, the owner: "i want a message telling me if any rains in the way").

It runs after every rain feed run on GitHub's servers (wm004-feed.yml, every 30 minutes), so it works with no laptop awake. It reads the wm004/weather.json the feed has
just written and keeps one GitHub issue open for one rain event, assigned to the owner: the GitHub app on the phone shows the issue when it opens, and a comment that names
the owner when the rain grows to a higher tier, when it comes back after a close, and when the forecast clears and the issue closes. NCM warnings have their own issues from
the NCM reader (label wm004-ncm); this one is the model forecast only, and it says so. The repository is public: the message holds the model figures, the rain point names
and the NCM colour word, never NCM's own words.

Tiers (Proposed, the owner may change them; the watch and high figures in 6 h are the plan's, read from weather.json):
  1 rain on the way   any point forecasts ON_WAY_72 mm or more in the next 72 hours
  2 watch             any point reaches the watch threshold in 6 hours
  3 high              any point reaches the high threshold in 6 hours
It closes when every one of the eight points was read and forecasts under CLEAR_72 mm in 72 hours, on CLEAR_RUNS forecasts in a row that carry different times (the same
file read twice counts once). A feed that is old, stamped ahead of the clock, or that read under 6 of the 8 points opens nothing and closes nothing. After a close, rain on
the way again waits REOPEN_H hours; a watch or a heavy rain inside that time reopens the same issue with a message. The state is kept in the issue itself (a marker in its
body), so no file is committed. Every write is made before the message about it, so a failed write never repeats a message.
  python3 wm004/rain_alert_action.py wm004/weather.json [--dry]"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = 'alalisaif91-droid/wm001'
OWNER = 'alalisaif91-droid'
LABEL = 'wm004-rain'
ON_WAY_72 = 5.0          # mm in 72 h at one point: Proposed, half the plan's 6 hour watch figure, as an early word
CLEAR_72 = 2.0           # mm in 72 h at every point to call the forecast clear
CLEAR_RUNS = 2           # two clear forecasts in a row, with different times, before the issue closes
MAX_AGE_MIN = 90         # an older feed is not read
AHEAD_MIN = 10           # a feed stamped more than this ahead of the clock is not read
MIN_POINTS = 6
REOPEN_H = 6
COLOURS = ('none', 'yellow', 'orange', 'red', 'unknown')
CARD = 'https://alalisaif91-droid.github.io/wm001/wm004/'
CONSOLE = 'https://alalisaif91-droid.github.io/wm001/wm004/console/'
NAMES = {1: 'Rain on the way', 2: 'Rain watch', 3: 'Heavy rain'}
MARK = re.compile(r'<!-- wm004-rain tier=([123]) clear=(\d+) asof=([0-9: -]+?) -->')


def now_uae():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)


def parse(s):
    try:
        return dt.datetime.strptime(str(s).replace('T', ' ')[:16], '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def assess(w, now):
    """(usable, why, facts). facts: tier, the highest 72 h point, the highest 24 h and 6 h figures, the earliest peak, the points with rain, the NCM colour, clear."""
    if not isinstance(w, dict):
        return False, 'the feed is not a record', None
    a = parse(w.get('asof'))
    if a is None:
        return False, 'the feed has no readable time', None
    age = (now - a).total_seconds() / 60.0
    if age > MAX_AGE_MIN:
        return False, 'the feed is %d minutes old' % age, None
    if age < -AHEAD_MIN:
        return False, 'the feed is stamped ahead of the clock', None
    if (num(w.get('pointsRead')) or 0) < MIN_POINTS:
        return False, 'the feed read %s of %s points' % (w.get('pointsRead'), w.get('pointsTotal')), None
    th = w.get('thresholds') if isinstance(w.get('thresholds'), dict) else {}
    watch, high = num(th.get('watch')), num(th.get('high'))
    if watch is None or high is None:
        return False, 'the feed holds no plan thresholds', None
    allp = [p for p in (w.get('points') or []) if isinstance(p, dict)]
    read = [p for p in allp if not p.get('error') and (num(p.get('next72')) is not None or num(p.get('max6')) is not None)]
    if not read:
        return False, 'the feed holds no point figures', None
    m6 = max(num(p.get('max6')) or 0.0 for p in read)
    with72 = [p for p in read if num(p.get('next72')) is not None]
    m72p = max(with72, key=lambda p: p['next72']) if with72 else None
    m72 = m72p['next72'] if m72p else 0.0
    tier = 3 if m6 >= high else 2 if m6 >= watch else 1 if m72 >= ON_WAY_72 else 0
    full = len(allp) == (num(w.get('pointsTotal')) or len(allp)) and len(with72) == len(allp) and num(w.get('pointsRead')) == len(allp)
    wet = sorted([p for p in with72 if p['next72'] >= 1.0], key=lambda p: -p['next72'])
    peaks = sorted(str(p['peakAt']) for p in wet if p.get('peakAt') and (num(p.get('peakMm')) or 0) > 0)
    n = w.get('ncm') if isinstance(w.get('ncm'), dict) else {}
    lv = n.get('levelsInForce') if isinstance(n.get('levelsInForce'), dict) else (n.get('levels') if isinstance(n.get('levels'), dict) else {})
    colour = lv.get('rain') if lv.get('rain') in COLOURS else 'not read'
    return True, '', {'tier': tier, 'max72': m72, 'at72': (m72p or {}).get('name', ''), 'max24': max(num(p.get('next24')) or 0.0 for p in read), 'max6': m6,
                      'peak': peaks[0] if peaks else None, 'prob': max(num(p.get('probMax24')) or 0.0 for p in read), 'wet': wet, 'ncm': colour,
                      'asof': str(w.get('asof'))[:16], 'watch': watch, 'high': high, 'clear': full and m72 < CLEAR_72}


def fmt(s):
    d = parse(s)
    return d.strftime('%a %d %b %H:%M') if d else 'not given'


def title(f, tier):
    return '%s: up to %.1f mm in 72 h at %s (forecast)' % (NAMES[tier], f['max72'], f['at72'].split(' (')[0] or 'a rain point')


def body(f, tier, clear):
    lines = ['**%s** in the model forecast of %s UAE.' % (NAMES[tier], fmt(f['asof'])), '',
             '| | |', '|---|---|',
             '| Most in 72 h | %.1f mm at %s |' % (f['max72'], f['at72']),
             '| Most in 24 h | %.1f mm |' % f['max24'],
             '| Most in 6 h | %.1f mm (the plan watches at %g mm and calls high at %g mm) |' % (f['max6'], f['watch'], f['high']),
             '| Earliest peak | %s UAE |' % fmt(f['peak']),
             '| Highest chance in 24 h | %d percent |' % f['prob'],
             '| NCM rain colour now | %s |' % f['ncm'], '', 'Points with 1 mm or more in 72 h:', '']
    lines += ['- %s: %.1f mm in 72 h, %.1f mm in 24 h' % (p.get('name', ''), p['next72'], num(p.get('next24')) or 0.0) for p in f['wet'][:8]] or ['- none']
    lines += ['', 'Model forecast (Proposed), Open-Meteo best match. It is not an official warning: NCM warnings come in their own messages. The rain committee sets the stage.',
              '', 'Status: %s  Console: %s' % (CARD, CONSOLE), '', '<!-- wm004-rain tier=%d clear=%d asof=%s -->' % (tier, clear, f['asof'])]
    return '\n'.join(lines)


def note(text):
    return '@%s %s' % (OWNER, text)          # the owner's own login, so a comment reaches the phone as a mention


class Issues:
    """GitHub issues through the REST API with the standard library. Never raises: a failed call returns None and is kept in errors."""

    def __init__(self, token, repo=REPO, opener=None):
        self.token, self.repo, self.opener, self.errors = token, repo, opener, []

    def call(self, method, path, data=None):
        req = urllib.request.Request('https://api.github.com/repos/%s%s' % (self.repo, path), method=method, data=json.dumps(data).encode('utf-8') if data is not None else None,
                                     headers={'Authorization': 'Bearer %s' % self.token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                                              'User-Agent': 'wm004-rain-alert', 'Content-Type': 'application/json'})
        try:
            if self.opener:
                return self.opener(req)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode('utf-8') or 'null')
        except urllib.error.HTTPError as e:
            self.errors.append('%s %s: HTTP %s' % (method, path.split('?')[0], e.code))
        except Exception as e:
            self.errors.append('%s %s: %s' % (method, path.split('?')[0], type(e).__name__))
        return None


def ok(r):
    return isinstance(r, dict)


def step(w, gh, now, log=print):
    """One run. Returns what it did: nothing, opened, reopened, raised, kept, same, clearing, closed."""
    usable, why, f = assess(w, now)
    if not usable:
        log('rain alert: nothing done, %s' % why)
        return 'nothing'
    got = gh.call('GET', '/issues?labels=%s&state=all&per_page=10&sort=created&direction=desc' % LABEL)
    if not isinstance(got, list):
        log('rain alert: GitHub did not answer, nothing done')
        return 'nothing'
    issues = [i for i in got if isinstance(i, dict) and not i.get('pull_request')]
    cur = next((i for i in issues if i.get('state') == 'open'), None)
    if cur is None:
        if f['tier'] == 0:
            log('rain alert: no rain on the way (most %.1f mm in 72 h)' % f['max72'])
            return 'nothing'
        last = next((i for i in issues if i.get('state') == 'closed'), None)
        m = MARK.search((last or {}).get('body') or '')
        closed = parse((last or {}).get('closed_at') or '')
        if last and m and closed and (now - dt.timedelta(hours=4) - closed) < dt.timedelta(hours=REOPEN_H):
            if f['tier'] == 1:
                log('rain alert: rain on the way again, under %d h after issue %s closed; not reopened' % (REOPEN_H, last.get('number')))
                return 'nothing'
            n = last['number']
            if not ok(gh.call('PATCH', '/issues/%d' % n, {'state': 'open', 'title': title(f, f['tier']), 'body': body(f, f['tier'], 0)})):
                return 'nothing'
            gh.call('POST', '/issues/%d/comments' % n, {'body': note('**%s** again: %.1f mm in 6 h at most, %.1f mm in 72 h at %s. Forecast of %s UAE.' % (NAMES[f['tier']], f['max6'], f['max72'], f['at72'], fmt(f['asof'])))})
            log('rain alert: issue %d reopened at tier %d' % (n, f['tier']))
            return 'reopened'
        if gh.call('GET', '/labels/%s' % LABEL) is None:
            gh.errors[:] = gh.errors[:-1]
            gh.call('POST', '/labels', {'name': LABEL, 'color': '1d76db', 'description': 'WM004 rain on the way, model forecast'})
        r = gh.call('POST', '/issues', {'title': title(f, f['tier']), 'body': body(f, f['tier'], 0), 'assignees': [OWNER], 'labels': [LABEL]})
        log('rain alert: opened %s, %s' % (r.get('number') if ok(r) else 'none', title(f, f['tier'])))
        return 'opened' if ok(r) else 'nothing'
    m = MARK.search(cur.get('body') or '')
    was, clear, seen = (int(m.group(1)), int(m.group(2)), m.group(3).strip()) if m else (1, 0, '')
    n = cur['number']
    if f['tier'] > was:
        if not ok(gh.call('PATCH', '/issues/%d' % n, {'title': title(f, f['tier']), 'body': body(f, f['tier'], 0)})):
            return 'nothing'
        gh.call('POST', '/issues/%d/comments' % n, {'body': note('Raised to **%s**: %.1f mm in 6 h at most, %.1f mm in 72 h at %s. Forecast of %s UAE.' % (NAMES[f['tier']], f['max6'], f['max72'], f['at72'], fmt(f['asof'])))})
        log('rain alert: issue %d raised from tier %d to %d' % (n, was, f['tier']))
        return 'raised'
    if f['asof'] == seen:
        log('rain alert: issue %d, the same forecast as the last run (%s); nothing changed' % (n, seen))
        return 'same'
    if f['clear']:
        if clear + 1 >= CLEAR_RUNS:
            if not ok(gh.call('PATCH', '/issues/%d' % n, {'state': 'closed', 'state_reason': 'completed', 'body': body(f, was, clear + 1)})):
                return 'nothing'
            gh.call('POST', '/issues/%d/comments' % n, {'body': note('Forecast clear: all eight rain points are under %g mm in the next 72 h (forecast of %s UAE). Closed.' % (CLEAR_72, fmt(f['asof'])))})
            log('rain alert: issue %d closed, forecast clear' % n)
            return 'closed'
        gh.call('PATCH', '/issues/%d' % n, {'body': body(f, was, clear + 1)})
        log('rain alert: issue %d clear on %d of %d forecasts' % (n, clear + 1, CLEAR_RUNS))
        return 'clearing'
    gh.call('PATCH', '/issues/%d' % n, {'title': title(f, was), 'body': body(f, was, 0)})      # the figures stay current; an edit sends nothing to the phone
    log('rain alert: issue %d kept at tier %d' % (n, was))
    return 'kept'


def main(argv):
    path = argv[1] if len(argv) > 1 else 'wm004/weather.json'
    try:
        with open(path, encoding='utf-8') as fh:
            w = json.load(fh)
    except (OSError, ValueError) as e:
        print('rain alert: the feed could not be read (%s), nothing done' % type(e).__name__)
        return 0
    try:
        if '--dry' in argv:
            usable, why, f = assess(w, now_uae())
            print('usable' if usable else 'not usable: ' + why)
            if usable:
                print('tier', f['tier'], '|', title(f, f['tier']) if f['tier'] else 'no rain on the way (most %.1f mm in 72 h)' % f['max72'])
                if f['tier']:
                    print(body(f, f['tier'], 0))
            return 0
        token = os.environ.get('GITHUB_TOKEN')
        if not token:
            print('::warning::rain alert: no token, nothing done')
            return 0
        gh = Issues(token)
        step(w, gh, now_uae())
        for e in gh.errors:
            print('::warning::rain alert: GitHub %s' % e)
    except Exception as e:                       # an alert never fails the feed
        print('::warning::rain alert: %s, nothing done' % type(e).__name__)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
