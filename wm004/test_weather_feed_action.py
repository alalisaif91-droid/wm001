# -*- coding: utf-8 -*-
"""Tests of wm004/weather_feed_action.py, the writer of wm004/weather.json run by .github/workflows/wm004-feed.yml (29 Oct 2026).
Standard library only, no network: every Open-Meteo reply is built here the way the best match model gives it (whole days from 00:00, Ras Al Khaimah time).
Run: python3 -m unittest wm004/test_weather_feed_action.py   (or python3 wm004/test_weather_feed_action.py)

What is checked: a full 96 hour reply gives 72 hours a point and next72; a null hour inside the window makes the point unread, never 0 mm; a reply cut at 50 hours gives next72 null and
hoursInWindow 50; a missing gust or probability list is null, never 0; ncm.json is aged at every run (30 hours old is stale and unknown); a page colour nobody has logged makes the level
unknown; 5 of 8 points failing writes nothing and exits 1; the 20 minute freshness skip and FEED_FORCE=1; the request; the public NCM text; the shape the console and the card read."""
import os, sys, json, shutil, tempfile, unittest, datetime as dt, io, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import weather_feed_action as AC

NOW = dt.datetime(2026, 10, 29, 6, 20)          # Ras Al Khaimah time of the run in most tests


def reply(now=NOW, hours=96, rain=0.1, null_hours=(), gust=True, prob=True, start=None):
    """an Open-Meteo hourly reply: `hours` hourly rows from 00:00 of the day of `now` (or from `start`)"""
    d0 = start or now.replace(hour=0, minute=0, second=0, microsecond=0)
    t = [(d0 + dt.timedelta(hours=i)).strftime('%Y-%m-%dT%H:00') for i in range(hours)]
    p = [rain] * hours
    for i in null_hours:
        p[i] = None
    h = {'time': t, 'precipitation': p}
    if prob:
        h['precipitation_probability'] = [10] * hours
    if gust:
        h['wind_gusts_10m'] = [20.4] * hours
    return {'hourly': h}


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='wm004feed_')
        self.out = os.path.join(self.dir, 'weather.json')
        self._env = os.environ.pop('FEED_FORCE', None)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        if self._env is not None:
            os.environ['FEED_FORCE'] = self._env
        else:
            os.environ.pop('FEED_FORCE', None)

    def run_main(self, fetcher, now=NOW):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = AC.main([self.out], fetcher=fetcher, now=now)
        return code, buf.getvalue()

    def doc(self):
        with open(self.out, encoding='utf-8') as f:
            return json.load(f)

    def raw(self):
        with open(self.out, 'rb') as f:
            return f.read()

    def put(self, name, obj):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
            json.dump(obj, f)


class Window(unittest.TestCase):
    def test_full_96_hour_reply(self):
        p = AC.summarise(reply(), NOW)
        self.assertEqual(p['hoursInWindow'], 72)
        self.assertEqual(len(p['hourly']), 72)
        self.assertEqual(p['hourly'][0][0], '2026-10-29T06:00')
        self.assertEqual(p['hourly'][-1][0], '2026-11-01T05:00')
        self.assertEqual(p['next6'], 0.6)
        self.assertEqual(p['next24'], 2.4)
        self.assertEqual(p['next72'], 7.2)
        self.assertEqual(p['max6'], 0.6)
        self.assertEqual(p['probMax24'], 10)
        self.assertEqual(p['gustMax24'], 20)

    def test_late_evening_still_holds_72_hours(self):
        late = dt.datetime(2026, 10, 29, 23, 50)
        p = AC.summarise(reply(late), late)
        self.assertEqual(p['hoursInWindow'], 72)
        self.assertEqual(p['next72'], 7.2)

    def test_null_hour_inside_window_makes_point_unread(self):
        # hour 30 of the day is inside the window that starts at 06:00
        with self.assertRaises(ValueError) as cm:
            AC.summarise(reply(null_hours=(30,)), NOW)
        self.assertIn('no value', str(cm.exception))
        pt = AC.one(AC.POINTS[0], fetcher=lambda la, lo: reply(null_hours=(30,)), now=NOW)
        self.assertIn('error', pt)
        self.assertNotIn('next72', pt)

    def test_null_hour_before_the_window_does_not_matter(self):
        p = AC.summarise(reply(null_hours=(2,)), NOW)          # 02:00 is before the 06:00 run
        self.assertEqual(p['next72'], 7.2)

    def test_reply_cut_at_50_hours(self):
        now0 = dt.datetime(2026, 10, 29, 0, 20)
        p = AC.summarise(reply(now0, hours=50), now0)
        self.assertEqual(p['hoursInWindow'], 50)
        self.assertIsNone(p['next72'], 'a sum over 50 hours is not the 72 hour total')
        self.assertEqual(p['next24'], 2.4)
        self.assertEqual(len(p['hourly']), 50)
        later = AC.summarise(reply(NOW, hours=50), NOW)
        self.assertEqual(later['hoursInWindow'], 44)
        self.assertIsNone(later['next72'])

    def test_fewer_than_24_hours_is_unread(self):
        with self.assertRaises(ValueError):
            AC.summarise(reply(hours=20), NOW)

    def test_missing_gust_list_is_null_not_zero(self):
        p = AC.summarise(reply(gust=False), NOW)
        self.assertIsNone(p['gustMax24'])
        self.assertEqual(p['next72'], 7.2, 'the point is still read')
        q = AC.summarise(reply(prob=False), NOW)
        self.assertIsNone(q['probMax24'])

    def test_one_missing_gust_hour_in_24_is_null(self):
        r = reply()
        r['hourly']['wind_gusts_10m'][10] = None                # 10:00, inside the next 24 hours
        r['hourly']['precipitation_probability'][12] = None
        p = AC.summarise(r, NOW)
        self.assertIsNone(p['gustMax24'])
        self.assertIsNone(p['probMax24'])
        r2 = reply()
        r2['hourly']['wind_gusts_10m'][60] = None               # beyond the next 24 hours: the 24 hour maximum stands
        self.assertEqual(AC.summarise(r2, NOW)['gustMax24'], 20)

    def test_reply_without_the_hour_of_the_run_is_unread(self):
        old = reply(start=dt.datetime(2026, 10, 24, 0, 0))     # every hour is before the run
        with self.assertRaises(ValueError):
            AC.summarise(old, NOW)
        ahead = reply(start=dt.datetime(2026, 10, 30, 0, 0))   # starts the day after
        with self.assertRaises(ValueError):
            AC.summarise(ahead, NOW)

    def test_levels_from_the_worst_six_hours(self):
        r = reply(rain=0.0)
        for i in range(30, 36):
            r['hourly']['precipitation'][i] = 2.0                # 12 mm in 6 h
        p = AC.summarise(r, NOW)
        self.assertEqual(p['max6'], 12.0)
        self.assertEqual(p['peakMm'], 2.0)
        self.assertEqual(p['peakAt'], '2026-10-30T06:00')


class Request(unittest.TestCase):
    def test_request_asks_four_days_best_match_dubai_time(self):
        seen = []

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"hourly": {}}'
        orig = AC.urllib.request.urlopen
        AC.urllib.request.urlopen = lambda url, timeout=0: (seen.append((url, timeout)), R())[1]
        try:
            self.assertEqual(AC.fetch(25.7, 55.9), {'hourly': {}})
        finally:
            AC.urllib.request.urlopen = orig
        url = seen[0][0]
        for part in ('forecast_days=4', 'models=best_match', 'timezone=Asia%2FDubai', 'precipitation%2Cprecipitation_probability%2Cwind_gusts_10m', 'api.open-meteo.com/v1/forecast'):
            self.assertIn(part, url)

    def test_request_retries_three_times_then_fails(self):
        calls = []

        def boom(url, timeout=0):
            calls.append(url)
            raise OSError('reset')
        orig, osl = AC.urllib.request.urlopen, AC.time.sleep
        AC.urllib.request.urlopen, AC.time.sleep = boom, (lambda s: None)
        try:
            with self.assertRaises(OSError):
                AC.fetch(25.7, 55.9)
        finally:
            AC.urllib.request.urlopen, AC.time.sleep = orig, osl
        self.assertEqual(len(calls), 3)


class Feed(Folder):
    def test_whole_feed_shape_for_the_console_and_the_card(self):
        code, out = self.run_main(lambda la, lo: reply())
        self.assertEqual(code, 0, out)
        d = self.doc()
        self.assertEqual(d['asof'], '2026-10-29 06:20')
        self.assertEqual(d['writer'], 'GitHub Actions, every 30 minutes')
        self.assertEqual(d['tz'], 'Asia/Dubai')
        self.assertEqual((d['pointsRead'], d['pointsTotal'], d['windowHours'], d['unreadPoints']), (8, 8, 72, []))
        self.assertEqual(d['modelLevel'], 'none')
        self.assertEqual(d['worstMax6'], 0.6)
        self.assertEqual(d['thresholds'], {'watch': 10.0, 'high': 25.0})
        self.assertEqual(len(d['points']), 8)
        for p in d['points']:
            for k in ('name', 'lat', 'lon', 'next6', 'next24', 'next72', 'hoursInWindow', 'max6', 'peakAt', 'peakMm', 'probMax24', 'gustMax24', 'hourly'):
                self.assertIn(k, p)
            self.assertEqual(p['hoursInWindow'], 72)
            self.assertEqual(p['next72'], 7.2)
        self.assertFalse(os.path.exists(self.out + '.tmp'), 'the temporary file is replaced, not left')
        for k in ('level', 'text', 'source', 'asof', 'ageHours', 'stale', 'levels', 'levelsInForce', 'inForce', 'refreshed'):
            self.assertIn(k, d['ncm'])

    def test_null_hour_at_two_points_six_of_eight_are_written_with_names(self):
        bad = {AC.POINTS[2][1], AC.POINTS[4][1]}
        code, _ = self.run_main(lambda la, lo: reply(null_hours=(30,)) if la in bad else reply())
        self.assertEqual(code, 0)
        d = self.doc()
        self.assertEqual((d['pointsRead'], d['pointsTotal']), (6, 8))
        self.assertEqual(sorted(d['unreadPoints']), sorted([AC.POINTS[2][0], AC.POINTS[4][0]]))
        for p in d['points']:
            if p['name'] in d['unreadPoints']:
                self.assertIn('error', p)
                self.assertNotIn('next24', p, 'an unread point carries no figure, never 0 mm')

    def test_five_of_eight_failing_writes_nothing_and_exits_1(self):
        with open(self.out, 'w', encoding='utf-8') as f:
            f.write('{"asof": "2026-10-28 23:57", "points": [], "keep": "this file"}')
        before = self.raw()
        mtime = os.path.getmtime(self.out)
        bad = {AC.POINTS[i][1] for i in range(5)}

        def fx(la, lo):
            if la in bad:
                raise OSError('HTTP Error 502')
            return reply()
        code, out = self.run_main(fx)
        self.assertEqual(code, 1)
        self.assertIn('FEED NOT WRITTEN', out)
        self.assertEqual(self.raw(), before, 'the last good file is left as it was')
        self.assertEqual(os.path.getmtime(self.out), mtime)
        self.assertFalse(os.path.exists(self.out + '.tmp'))
        os.remove(self.out)
        code2, _ = self.run_main(fx)
        self.assertEqual(code2, 1)
        self.assertFalse(os.path.exists(self.out), 'nothing is written when there was no file')

    def test_freshness_skip_and_feed_force(self):
        with open(self.out, 'w', encoding='utf-8') as f:
            json.dump({'asof': '2026-10-29 06:05', 'points': []}, f)
        before = self.raw()
        calls = []

        def fx(la, lo):
            calls.append(la)
            return reply()
        code, out = self.run_main(fx)
        self.assertEqual(code, 0)
        self.assertIn('fresh enough', out)
        self.assertEqual(calls, [], 'no call to Open-Meteo for a fresh feed')
        self.assertEqual(self.raw(), before)
        os.environ['FEED_FORCE'] = '1'
        code2, _ = self.run_main(fx)
        self.assertEqual(code2, 0)
        self.assertEqual(len(calls), 8)
        self.assertEqual(self.doc()['asof'], '2026-10-29 06:20')

    def test_feed_older_than_20_minutes_or_ahead_of_the_clock_is_rewritten(self):
        for asof in ('2026-10-29 05:59', '2026-10-29 07:30', 'not a time'):
            with open(self.out, 'w', encoding='utf-8') as f:
                json.dump({'asof': asof, 'points': []}, f)
            code, out = self.run_main(lambda la, lo: reply())
            self.assertEqual(code, 0)
            self.assertEqual(self.doc()['asof'], '2026-10-29 06:20', asof)

    def test_rain_level_watch_and_high(self):
        def storm(mm):
            r = reply(rain=0.0)
            for i in range(30, 36):
                r['hourly']['precipitation'][i] = mm / 6.0
            return r
        self.run_main(lambda la, lo: storm(12.0) if la == AC.POINTS[3][1] else reply(rain=0.0))
        self.assertEqual(self.doc()['modelLevel'], 'watch')
        os.environ['FEED_FORCE'] = '1'
        self.run_main(lambda la, lo: storm(30.0) if la == AC.POINTS[3][1] else reply(rain=0.0))
        self.assertEqual(self.doc()['modelLevel'], 'high')
        self.assertEqual(self.doc()['worstMax6'], 30.0)


NCM_TEXT = ('The NCM warnings page shows warning colours (yellow, orange) that nobody has logged yet, first seen 2026-10-28 22:20, and no person has logged a reading naming rain, fog, dust since: '
            'the rain, fog, dust level(s) are unknown, not none, until the page is read and logged with ncm_log.py add Last logged reading: No rain warning for Ras Al Khaimah.')
READING = {'level': 'none', 'text': 'No rain warning for Ras Al Khaimah. Fog warnings red and yellow 05:15 to 09:00', 'source': 'https://mobile.ncm.gov.ae/maps-warnings?lang=en',
           'asof': '2026-10-29 01:50', 'levels': {'rain': 'none', 'fog': 'red', 'dust': 'none'}, 'hazard': ['rain'], 'areas': '', 'ageHours': 0.0, 'id': 'NCM-20261029-001',
           'writtenAt': '2026-10-29 02:00'}


class Ncm(Folder):
    def ncm_after(self, block, now=NOW):
        self.put('ncm.json', block)
        code, out = self.run_main(lambda la, lo: reply(now), now=now)
        self.assertEqual(code, 0, out)
        return self.doc()['ncm']

    def test_fresh_reading_is_aged_now_and_nothing_is_in_force(self):
        n = self.ncm_after(READING)
        self.assertEqual(n['ageHours'], 4.5, 'the age is worked out at the run, never the 0.0 that ncm.json carries')
        self.assertIs(n['stale'], False)
        self.assertEqual(n['level'], 'none')
        self.assertEqual(n['levelAtReading'], 'none')
        self.assertEqual(n['levelsAtReading']['fog'], 'red', 'what the reading said is kept as read')
        self.assertEqual(n['levelInForce'], 'unknown')
        self.assertEqual(n['levelsInForce'], {'rain': 'unknown', 'fog': 'unknown', 'dust': 'unknown'})
        self.assertEqual((n['inForce'], n['upcoming']), ([], []))
        self.assertEqual(n['refreshed'], '2026-10-29 06:20')
        self.assertIn('nothing is claimed to be in force', n['note'])

    def test_ncm_json_30_hours_old_is_stale_and_unknown(self):
        n = self.ncm_after(dict(READING, asof='2026-10-28 00:20'))
        self.assertEqual(n['ageHours'], 30.0)
        self.assertIs(n['stale'], True)
        self.assertEqual(n['level'], 'unknown')
        self.assertEqual(set(n['levels'].values()), {'unknown'})
        self.assertEqual(n['levelAtReading'], 'none')

    def test_age_grows_from_run_to_run_and_turns_stale(self):
        self.ncm_after(READING)
        os.environ['FEED_FORCE'] = '1'
        later = dt.datetime(2026, 10, 30, 2, 20)              # 24.5 hours after the reading
        n = self.ncm_after(READING, now=later)
        self.assertEqual(n['ageHours'], 24.5)
        self.assertIs(n['stale'], True)
        self.assertEqual(n['level'], 'unknown')

    def test_pending_page_colour_makes_the_level_unknown_and_the_text_public(self):
        blk = dict(READING, text=NCM_TEXT, pageShows={'state': 'warnings', 'colours': ['yellow', 'orange'], 'since': '2026-10-29 03:20', 'seen': '2026-10-29 03:20', 'pending': True,
                                                     'pendingHazards': ['rain', 'fog', 'dust'], 'namedHazards': [],
                                                     'note': 'The NCM warnings page shows warning colours (yellow, orange) that nobody has logged yet ... until the page is read and logged with ncm_log.py add'})
        n = self.ncm_after(blk)
        self.assertEqual(n['level'], 'unknown')
        self.assertEqual(set(n['levels'].values()), {'unknown'})
        self.assertIs(n['stale'], True)
        self.assertEqual(n['levelAtReading'], 'none')
        self.assertIn('nobody has logged', n['note'])
        self.assertNotIn('ncm_log', json.dumps(n))
        self.assertIn('logged by the weather desk. Last logged reading: No rain warning', n['text'])
        self.assertTrue(n['pageShows']['note'].endswith('logged by the weather desk'))

    def test_pending_page_colour_for_fog_only_keeps_a_fresh_rain_colour(self):
        blk = dict(READING, levels={'rain': 'yellow', 'fog': 'none', 'dust': 'none'}, level='yellow',
                   pageShows={'pending': True, 'since': '2026-10-29 03:20', 'pendingHazards': ['fog']})
        n = self.ncm_after(blk)
        self.assertEqual(n['level'], 'yellow')
        self.assertEqual(n['levels'], {'rain': 'yellow', 'fog': 'unknown', 'dust': 'none'})

    def test_reading_with_no_stamp_or_a_stamp_ahead_of_the_clock_is_stale(self):
        for asof in (None, '2026-10-29 09:00', 'yesterday'):
            n = AC.refresh_ncm(dict(READING, asof=asof), NOW)
            self.assertIs(n['stale'], True, asof)
            self.assertEqual(n['level'], 'unknown', asof)
            self.assertIsNone(n['ageHours'], asof)

    def test_no_reading_anywhere(self):
        code, _ = self.run_main(lambda la, lo: reply())
        self.assertEqual(code, 0)
        n = self.doc()['ncm']
        self.assertEqual(n['level'], 'unknown')
        self.assertIs(n['stale'], True)
        self.assertEqual(n['text'], 'No NCM reading recorded yet')

    def test_block_of_the_previous_run_reads_the_same_without_ncm_json(self):
        n1 = self.ncm_after(dict(READING, pageShows={'pending': True, 'since': '2026-10-29 03:20', 'pendingHazards': ['rain', 'fog', 'dust']}))
        os.remove(os.path.join(self.dir, 'ncm.json'))
        os.environ['FEED_FORCE'] = '1'
        code, _ = self.run_main(lambda la, lo: reply())
        self.assertEqual(code, 0)
        n2 = self.doc()['ncm']
        self.assertEqual(n2['levelAtReading'], 'none', 'the reading is not lost when the block passes through twice')
        self.assertEqual(n2['levelsAtReading'], READING['levels'])
        self.assertEqual(n2['level'], 'unknown')
        self.assertEqual(n1['text'], n2['text'])

    def test_newer_ncm_json_wins_and_an_older_one_does_not(self):
        with open(self.out, 'w', encoding='utf-8') as f:
            json.dump({'asof': '2026-10-29 05:00', 'points': [], 'ncm': dict(READING, asof='2026-10-29 04:00', level='yellow', levels={'rain': 'yellow'})}, f)
        n = self.ncm_after(READING)                          # ncm.json of 01:50 is older than the block of 04:00
        self.assertEqual(n['level'], 'yellow')
        self.assertEqual(n['asof'], '2026-10-29 04:00')


def reader_state(state='NO WARNING IN FORCE', read='2026-10-29T02:10:00Z', **kw):       # 06:10 in Ras Al Khaimah, 10 minutes before NOW
    d = {'schema': 1, 'status': 'Reported', 'state': state, 'reason': 'list_and_ticker_clear', 'readAt': read, 'listAgeH': 3.2, 'limitH': 72, 'highest': 'none', 'hazards': [], 'count': 0, 'itemsMore': 0, 'items': [], 'tickerRead': True,
         'rak': {'status': 'not touched', 'named': False, 'city': False, 'units': []}, 'health': {'lastOk': read, 'consecutiveFailures': 0, 'failingSince': None}}
    d.update(kw)
    return d


class Reader(Folder):
    def block(self, state, now=NOW):
        self.put('ncm_state.json', state)
        code, out = self.run_main(lambda la, lo: reply(now), now=now)
        self.assertEqual(code, 0, out)
        return self.doc()['ncm']

    def test_a_fresh_reader_state_gives_the_block_and_names_itself(self):
        n = self.block(reader_state())
        self.assertEqual((n['level'], n['levelInForce'], n['stale'], n['asof'], n['by']), ('none', 'none', False, '2026-10-29 06:10', 'live reader'))
        self.assertIn('ncm_state.json', n['via'])
        self.assertEqual(n['reader']['state'], 'NO WARNING IN FORCE')

    def test_a_warning_gives_the_rain_colour_and_what_is_in_force(self):
        items = [{'id': '1', 'colour': 'orange', 'hazards': ['rain'], 'from': '2026-10-29T01:00:00Z', 'to': '2026-10-29T09:00:00Z', 'rak': 'touched', 'named': False},
                 {'id': '2', 'colour': 'red', 'hazards': ['rain'], 'from': '2026-10-29T06:00:00Z', 'to': '2026-10-29T12:00:00Z', 'rak': 'not known', 'named': False},
                 {'id': '3', 'colour': 'red', 'hazards': ['fog'], 'from': None, 'to': None, 'rak': 'not touched', 'named': False}]
        n = self.block(reader_state('WARNING', highest='red', hazards=['fog', 'rain'], count=3, items=items, reason='list_items', rak={'status': 'touched', 'named': False, 'city': True, 'units': []}))
        self.assertEqual((n['level'], n['levelInForce'], n['levels']['fog'], n['inForce'], n['upcoming']), ('red', 'orange', 'none', ['1'], ['2']))

    def test_a_colour_not_read_is_unknown(self):
        n = self.block(reader_state('WARNING', highest='not read', count=1, items=[{'id': '1', 'colour': 'not read', 'hazards': ['rain'], 'from': None, 'to': None, 'rak': 'not known', 'named': False}]))
        self.assertEqual(n['level'], 'unknown')

    def test_no_reading_and_text_warning_are_never_calm(self):
        n = self.block(reader_state('NO READING', reason='refused', highest='not read'))
        self.assertEqual((n['level'], n['stale']), ('unknown', True))
        self.assertIn('NO READING', n['text'])
        n = self.block(reader_state('TEXT WARNING', reason='ticker_only', highest='not read'))
        self.assertEqual(set(n['levels'].values()), {'unknown'})

    def test_90_minutes_old_gives_way_to_ncm_json(self):
        self.put('ncm.json', READING)
        n = self.block(reader_state(read='2026-10-29T00:50:00Z'))
        self.assertEqual(n['asof'], READING['asof'])
        self.assertIn('ncm.json', n['via'])
        self.assertNotIn('reader', n)

    def test_a_reader_block_is_not_carried_on_when_the_state_goes_old(self):
        self.block(reader_state('WARNING', highest='red', count=1, items=[{'id': '1', 'colour': 'red', 'hazards': ['rain'], 'from': None, 'to': None, 'rak': 'touched', 'named': False}]))
        os.remove(os.path.join(self.dir, 'ncm_state.json'))
        later = NOW + dt.timedelta(minutes=45)
        code, out = self.run_main(lambda la, lo: reply(later), now=later)
        self.assertEqual(code, 0, out)
        n = self.doc()['ncm']
        self.assertEqual((n['level'], n['asof']), ('unknown', None))

    def test_a_damaged_state_is_ignored(self):
        with open(os.path.join(self.dir, 'ncm_state.json'), 'w') as f:
            f.write('{broken')
        self.put('ncm.json', READING)
        code, out = self.run_main(lambda la, lo: reply(NOW), now=NOW)
        self.assertEqual(code, 0, out)
        self.assertIn('ncm.json', self.doc()['ncm']['via'])

    def test_the_block_holds_no_ncm_words(self):
        n = self.block(reader_state())
        self.assertNotIn('text_en', json.dumps(n))


class Refused(Folder):
    def block(self, extra):
        self.put('ncm.json', dict(READING, **extra))
        code, out = self.run_main(lambda la, lo: reply(NOW), now=NOW)
        self.assertEqual(code, 0, out)
        return self.doc()['ncm']

    def test_a_refusal_makes_the_hazards_it_names_unknown_and_the_block_stale(self):
        n = self.block({'level': 'yellow', 'levels': {'rain': 'yellow', 'fog': 'none'}, 'refused': {'open': [{'at': '2026-10-29 05:00', 'level': 'red', 'hazard': 'rain', 'pendingHazards': ['rain']}], 'note': 'x'}})
        self.assertEqual((n['stale'], n['level'], n['levels']['fog'], n['levelAtReading']), (True, 'unknown', 'none', 'yellow'))
        self.assertIn('refused', n['note'])

    def test_unreadable_refused_lines_make_every_hazard_unknown(self):
        n = self.block({'refused': {'open': [], 'unreadableLines': 2}})
        self.assertEqual((n['stale'], set(n['levels'].values())), (True, {'unknown'}))

    def test_no_internal_name_in_a_refused_note_or_by(self):
        n = self.block({'by': 'manual, through weather_feed.py', 'refused': {'open': [{'at': 'x', 'text': 'kept in ncm_refused.jsonl, python ncm_log.py clear-refused', 'reason': 'see ncm_log.py', 'pendingHazards': ['rain']}],
                                                                              'note': 'a reading typed by a person and refused by the log (ncm_refused.jsonl) is a high alert until python ncm_log.py clear-refused acknowledges it'}})
        blob = json.dumps(n)
        for w in ('ncm_refused.jsonl', 'ncm_log.py', 'weather_feed.py'):
            self.assertNotIn(w, blob)
        self.assertEqual(n['by'], 'manual')


class PublicText(unittest.TestCase):
    def test_no_internal_command_and_sentences_do_not_run_together(self):
        t = AC.public_text(NCM_TEXT)
        self.assertNotIn('ncm_log', t)
        self.assertNotIn('.py', t)
        self.assertIn('until the page is read and logged by the weather desk. Last logged reading: No rain warning for Ras Al Khaimah.', t)
        self.assertEqual(AC.public_text(t), t, 'the same text twice gives the same text')
        u = AC.public_text('ncm_auto_state.json cannot be read, so the levels are unknown until a person has logged a reading that names rain (ncm_log.py add) made after 2026-10-29 03:20')
        self.assertTrue(u.startswith('The record of the automatic NCM page read cannot be read'))
        self.assertNotIn('(', u)
        self.assertEqual(AC.public_text(None), '')
        self.assertEqual(AC.public_text('No rain warning for Ras Al Khaimah.'), 'No rain warning for Ras Al Khaimah.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
