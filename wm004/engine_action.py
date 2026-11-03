# -*- coding: utf-8 -*-
"""WM004 weather engine, standby on GitHub's servers (2 Nov 2026, the owner: "i need it to auto and live all time").
The department laptop runs the full engine with the plan registers and publishes engine.json, conditions.json and channel.json. It stops when the laptop sleeps, and the level cards of the
console then fall to NO READING. This job runs on every tick of the clock. While the published engine record is under LIMIT_MIN minutes old it does nothing. When it is older, it runs the same
engine here, writes the same three files through the same screens the laptop uses, and brings status.json up to date, so the console and the status card stay live with no laptop.
The run happens outside the repository (RUN), so nothing unscreened can reach a public file. The plan registers are not in this public repository, so the engine says their gaps in its own
output; the rain, fog, wind, gust, dust and pressure levels do not need them. A run that reads no model answer publishes unknown, never none.
FORCE=true runs it whatever the age: a push of this file or of wm004/engine.trigger proves the path.
  python3 wm004/engine_action.py"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, 'engine')
RUN = os.environ.get('WM004_RUN') or os.path.join(os.path.expanduser('~'), 'wm004run')
APP = os.path.join(RUN, 'app')
LIMIT_MIN = 100     # the laptop publishes at the hour what it read at 20 past, so a healthy record can reach 100 minutes
FLAG = os.environ.get('GITHUB_OUTPUT')


def now_uae():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=4)


def age_min(path):
    """minutes since the asof of a published file, or None when it cannot be read"""
    try:
        with open(path, encoding='utf-8') as f:
            a = json.load(f)['asof']
        return (now_uae() - dt.datetime.strptime(str(a)[:16], '%Y-%m-%d %H:%M')).total_seconds() / 60.0
    except (OSError, ValueError, KeyError, TypeError):
        return None


def failed(path):
    """the published record is a failed read: no model point answered, or the run says it was not ok (3 Nov 2026: the laptop lost its network at 13:59, published a run
    with 0 of 8 points, and the standby stayed out for 100 minutes because the record was young)"""
    try:
        with open(path, encoding='utf-8') as f:
            a = json.load(f)
    except (OSError, ValueError):
        return False
    lr = (a.get('health') or {}).get('lastRun') or {}
    return a.get('readingQuality') in ('degraded', 'none') or lr.get('ok') is False


def said(ran):
    if FLAG:
        with open(FLAG, 'a', encoding='utf-8') as f:
            f.write('ran=%s\n' % ('true' if ran else 'false'))


def main():
    force = os.environ.get('FORCE') == 'true'
    age = age_min(os.path.join(HERE, 'engine.json'))
    bad = failed(os.path.join(HERE, 'engine.json'))
    if not force and not bad and age is not None and age < LIMIT_MIN:
        print('the published engine record is %d minutes old: the standby stays out' % age)
        said(False)
        return 0
    print('running the engine here: published record %s minutes old, failed read %s, force %s' % ('unknown' if age is None else int(age), bad, force))
    os.makedirs(APP, exist_ok=True)
    shutil.copytree(CODE, APP, dirs_exist_ok=True)          # the code and the fixed rules over the state kept from the last run
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    out = os.path.join(RUN, 'weather_engine.json')
    r = subprocess.run([sys.executable, os.path.join(APP, 'weather_engine.py'), '--out', out, '--deadline', '150'], cwd=APP, env=env, timeout=300)
    print('engine exit code', r.returncode)
    if r.returncode == 1 or not os.path.exists(out):
        print('the engine wrote nothing: the published files stay as they are, and the console shows their age')
        said(True)
        return 1
    sys.path.insert(0, APP)
    os.environ['WM001_SITE'] = os.path.dirname(HERE)
    import mkwebstatus as M
    for line in (M.copy_conditions(os.path.join(RUN, 'conditions.json'), HERE), M.copy_channel(os.path.join(RUN, 'channel.json'), HERE), M.copy_engine(out, HERE)):
        print(line[1])
    shutil.copy(os.path.join(HERE, 'weather.json'), os.path.join(RUN, 'weather.json'))
    with open(os.path.join(RUN, 'weather.json'), encoding='utf-8') as f:
        w = json.load(f)
    try:
        with open(os.path.join(HERE, 'status.json'), encoding='utf-8') as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    pumps = old.get('pumps')
    try:
        with open(os.path.join(HERE, 'live_index.json'), encoding='utf-8') as f:
            pumps = json.load(f).get('pumps') or pumps
    except (OSError, ValueError):
        pass
    status, stage = M.card_status(w, pumps, old.get('actions'), now_uae())
    status['writer'] = 'GitHub standby: the plan registers are read on the laptop only'
    with open(os.path.join(HERE, 'status.json'), 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    print('status written', status['written'], stage)
    said(True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
