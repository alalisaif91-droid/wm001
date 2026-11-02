# -*- coding: utf-8 -*-
"""Shared helpers for the WM004 weather engine: the clock, durable file writes, tolerant reads, simple statistics, geometry
and the constants every module agrees on. Standard library only.

Time: every time the engine writes is Ras Al Khaimah local time (Asia/Dubai, UTC+4, no daylight saving), as a naive string
'YYYY-MM-DD HH:MM' or 'YYYY-MM-DDTHH:MM'. The clock is taken from UTC plus four hours, never from the laptop's own time zone,
so a wrong zone setting on the laptop cannot shift the forecast window (audit finding G22)."""
import os, json, math, time, tempfile, datetime as dt, statistics, contextlib, threading, random
try:
    import msvcrt                                 # Windows: byte range locks
except ImportError:
    msvcrt = None
try:
    import fcntl                                  # elsewhere: flock
except ImportError:
    fcntl = None

UTC_OFFSET_H = 4
TZ_NAME = 'Asia/Dubai'
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, 'data', 'weather')
DEFAULT_OUT = os.path.join(HERE, '..', 'weather.json')

# the eight rain points of the plan (copied from weather_feed.py of 17 Oct 2026; the names keep the prefixes the plan builders match on)
POINTS = [
    ('RAK city (MRF, Sectors 3 and 4)', 25.7549, 55.9705),
    ('Al Rams (1B)', 25.8713, 56.0218),
    ('Al Burairat, Wadi Al Beeh front (2B)', 25.7708, 56.0397),
    ('Jebel Jais road, Wadi Shehah (1C)', 25.9357, 56.1176),
    ('Khatt, Wadi Naqab front (5A)', 25.6433, 56.0105),
    ('Al Jazeera Al Hamra (6A)', 25.6952, 55.7945),
    ('Showkah and Kadrah (8A)', 25.0925, 56.1193),
    ('Al Munai (9A)', 24.9231, 56.1496),
]
THRESH6 = {'watch': 10.0, 'high': 25.0}          # the plan values: mm in 6 h at any point (Proposed, S17; AL10 and AL11). Never changed here.


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def now_local():
    return utc_now() + dt.timedelta(hours=UTC_OFFSET_H)


def parse_local(s):
    """Any ISO like string or datetime to a naive Asia/Dubai datetime. A string with an offset or Z is converted; a naive one is
    taken as local time already."""
    if s is None or s == '':
        return None
    if isinstance(s, dt.datetime):
        d = s
    else:
        t = str(s).strip().replace('/', '-')
        if len(t) == 10:
            t += 'T00:00'
        d = dt.datetime.fromisoformat(t)
    if d.tzinfo is not None:
        d = (d.astimezone(dt.timezone.utc) + dt.timedelta(hours=UTC_OFFSET_H)).replace(tzinfo=None)
    return d


def utc_to_local(d):
    """a naive UTC datetime to naive local"""
    return d + dt.timedelta(hours=UTC_OFFSET_H)


def f_local(d):
    return d.strftime('%Y-%m-%d %H:%M') if d else None


def f_iso(d):
    return d.strftime('%Y-%m-%dT%H:%M') if d else None


def hour_label(d):
    return d.strftime('%Y-%m-%dT%H:00')


def floor_hour(d):
    return d.replace(minute=0, second=0, microsecond=0)


def hours_between(a, b):
    return (b - a).total_seconds() / 3600.0


# ------------------------------------------------------------------------------------------------ files
def atomic_write_text(path, text):
    """temp file in the same folder, flush, fsync, then os.replace; a reader sees the old file or the new one, never half of one.
    On Windows the replace can fail for a moment while OneDrive or a reader holds the target, so it is retried."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    text, _ = utf8_safe(text)                          # a lone surrogate would stop the write and freeze the file for as long as the text stays in the answer (audit N4)
    fd, tmp = tempfile.mkstemp(prefix='.tmp_', suffix='.part', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        last = None
        pause = 0.05
        for _ in range(30):                             # up to about 10 s (was 8 x 0.25 s): OneDrive, an antivirus scan or a reader on a loaded machine can hold the target for longer than 2 s
            try:
                os.replace(tmp, path)
                last = None
                break
            except PermissionError as e:
                last = e
                time.sleep(pause)
                pause = min(pause * 1.5, 0.5)
        if last is not None:
            raise last
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, indent=None):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent, separators=(',', ':') if indent is None else None, allow_nan=False))


LOCK_TIMEOUT_S = 120.0                # was 30 s until 25 Oct 2026 (audit C0): see file_lock for the cause of the N3 failure under load


class LockTimeout(OSError):
    """another process held the lock for longer than the timeout: nothing was written"""


_HELD_LOCKS = {}                      # (lock file, thread) -> depth: a thread that already holds a lock takes it again without waiting for itself (an append inside a locked id allocation)


def lock_file_for(target):
    """the lock file beside a data file: .<name>.lock in the same folder (a hidden file, never read by any reader)"""
    # the lock file is named from the real path in the file system's own case, so that two spellings of one file (a relative and an absolute path, C: and c:, a short 8.3 name, a junction)
    # take the same lock; the name is the one the other process would work out
    d, n = os.path.split(os.path.normcase(os.path.realpath(os.path.abspath(target))))
    return os.path.join(d, '.' + n + '.lock')


# ---- the operating system lock -------------------------------------------------------------------------------------------------------------------------------------
# Why N3 failed once under load (25 Oct 2026, reproduced with 24 busy processes beside 12 writers): the lock was taken by polling (try, sleep 3 to 15 ms, try again). A writer that had
# just given the lock up asked for it again within microseconds, while the 11 others slept, so the holder was served first nearly every time (no queue, no fairness), and on a machine
# with every core busy a sleeping process needs a whole scheduler turn to wake up and look. 2,400 appends took 7 minutes instead of 2 seconds, single waits reached 27 s, and two writers
# passed the 30 s limit and stopped with LockTimeout (the process that "exited non zero"). Nothing was wrong with the lock file, the byte range, the dead process handling or the test.
# The cure is to stop polling. On Windows the waiter now asks the kernel for the byte range lock and sleeps inside the kernel until it is granted (LockFileEx on an overlapped handle, with
# the timeout on the wait): the file system keeps waiting requests in the order they came, hands the lock to the first waiter at the moment of the unlock, so the holder cannot take it
# back, and nobody wakes up to look in vain. The limit is longer (120 s), and it counts from the request, so a long queue behind a slow holder is not the same as a hung holder.
# Elsewhere (flock) and on Windows if ctypes cannot be set up, the wait polls with a backoff from 0.5 ms to 20 ms and a random spread. The operating system gives the lock up when the
# process that holds it ends, however it ends, so a killed process leaves no stale lock.
_WIN = None
if os.name == 'nt':
    try:
        import ctypes
        from ctypes import wintypes

        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t), ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

        _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        _k32.CreateFileW.restype = wintypes.HANDLE
        _k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        _k32.CreateEventW.restype = wintypes.HANDLE
        _k32.ResetEvent.argtypes = [wintypes.HANDLE]
        _k32.ResetEvent.restype = wintypes.BOOL
        _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
        _k32.LockFileEx.restype = wintypes.BOOL
        _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
        _k32.UnlockFileEx.restype = wintypes.BOOL
        _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _k32.WaitForSingleObject.restype = wintypes.DWORD
        _k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        _k32.CancelIoEx.restype = wintypes.BOOL
        _k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        _k32.GetOverlappedResult.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32.CloseHandle.restype = wintypes.BOOL
        _k32.GetCurrentThread.argtypes = []
        _k32.GetCurrentThread.restype = wintypes.HANDLE
        _k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
        _k32.GetThreadPriority.restype = ctypes.c_int
        _k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        _k32.SetThreadPriority.restype = wintypes.BOOL
        _INVALID = ctypes.c_void_p(-1).value
        _WIN = True
    except Exception:                                  # no ctypes: the polling path below takes over
        _WIN = None


def _boost():
    """Windows: raise the calling thread to THREAD_PRIORITY_HIGHEST for the time it waits for and holds a lock, and return what to put back (None when nothing was changed). The cause of the
    slowness under load was a priority inversion: the thread that holds the lock, or the next one in the queue, has the same priority as every busy process on the machine, so it waits in
    the ready queue for seconds while all the others wait for it. The code that runs at the raised priority is one short write; a thread that waits in the kernel uses no processor."""
    if not _WIN:
        return None
    try:
        h = _k32.GetCurrentThread()
        old = _k32.GetThreadPriority(h)
        if old == 0x7FFFFFFF or old >= 2:                  # THREAD_PRIORITY_ERROR_RETURN, or already high
            return None
        return old if _k32.SetThreadPriority(h, 2) else None
    except Exception:
        return None


def _unboost(old):
    if old is not None:
        try:
            _k32.SetThreadPriority(_k32.GetCurrentThread(), old)
        except Exception:
            pass


class _WinLock:
    """one byte at the start of the lock file, held through a kernel wait; the handle belongs to this object, so two threads of one process queue like two processes"""
    ERROR_IO_PENDING = 997
    WAIT_OBJECT_0 = 0

    def __init__(self, path):
        self.h = _k32.CreateFileW(path, 0xC0000000, 7, None, 4, 0x80 | 0x40000000, None)     # read+write, share all, OPEN_ALWAYS, NORMAL | OVERLAPPED
        if self.h is None or self.h == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        self.ev = _k32.CreateEventW(None, True, False, None)
        if not self.ev:
            e = ctypes.get_last_error()
            _k32.CloseHandle(self.h)
            self.h = None
            raise ctypes.WinError(e)
        self.locked = False

    def acquire(self, timeout):
        """True when granted; False when the time ran out (nothing is held then)"""
        ov = _OVERLAPPED()
        ov.hEvent = self.ev
        _k32.ResetEvent(self.ev)
        if _k32.LockFileEx(self.h, 0x2, 0, 1, 0, ctypes.byref(ov)):                         # LOCKFILE_EXCLUSIVE_LOCK, granted at once
            self.locked = True
            return True
        err = ctypes.get_last_error()
        if err != self.ERROR_IO_PENDING:
            raise ctypes.WinError(err)
        ms = int(max(0.0, timeout) * 1000)
        n = wintypes.DWORD(0)
        if _k32.WaitForSingleObject(self.ev, ms) == self.WAIT_OBJECT_0:
            if _k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), False):
                self.locked = True
                return True
            raise ctypes.WinError(ctypes.get_last_error())
        _k32.CancelIoEx(self.h, ctypes.byref(ov))                                           # out of time: withdraw the request, and wait until the kernel has finished with the structure
        if _k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), True):       # it was granted in the instant before the cancel: the lock is ours
            self.locked = True
            return True
        return False

    def release(self):
        if self.locked:
            self.locked = False
            ov = _OVERLAPPED()
            _k32.UnlockFileEx(self.h, 0, 1, 0, ctypes.byref(ov))

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)                                                        # closing the handle gives up any lock still held, as does the end of the process
            self.h = None
        if self.ev:
            _k32.CloseHandle(self.ev)
            self.ev = None


class _PollLock:
    """flock, or msvcrt.locking when ctypes is not there: try, and sleep with a backoff when it is taken"""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)
        self.locked = False

    def _try(self):
        if msvcrt is not None:
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)        # one byte at the start of the lock file; a byte range lock may lie past the end of an empty file
        else:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self, timeout):
        t_end = time.monotonic() + max(0.0, timeout)
        pause = 0.0005
        while True:
            try:
                self._try()
                self.locked = True
                return True
            except OSError:
                if time.monotonic() >= t_end:
                    return False
                time.sleep(pause * (0.5 + random.random()))
                pause = min(pause * 1.6, 0.02)

    def release(self):
        if self.locked:
            self.locked = False
            try:
                if msvcrt is not None:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def _open_lock(lp):
    if _WIN:
        try:
            return _WinLock(lp)
        except OSError:
            pass                                                # the kernel wait could not be set up: poll instead
    return _PollLock(lp)


@contextlib.contextmanager
def file_lock(target, timeout=None):
    """A cross process lock for a data file (audit N3). Windows has no atomic O_APPEND (the C runtime seeks to the end and then writes), so two processes that append at the same
    instant overwrote one another's line and drew the same id. The lock is a byte range lock on a lock file beside the data file (see the note above for how it is waited for). The
    operating system gives it up when the process that holds it ends, however it ends, so a killed process leaves no stale lock and nobody waits for a timeout. Waits up to `timeout`
    seconds (LOCK_TIMEOUT_S when None), then raises LockTimeout (an OSError): nothing is written and the caller says so. A thread that holds the lock may take it again."""
    if timeout is None:
        timeout = LOCK_TIMEOUT_S
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)         # in the case the caller wrote it, before the lock name is worked out
    lp = lock_file_for(target)
    key = (lp, threading.get_ident())
    if key in _HELD_LOCKS:
        _HELD_LOCKS[key] += 1
        try:
            yield lp
        finally:
            _HELD_LOCKS[key] -= 1
        return
    boosted = _boost()
    lk = None
    try:
        lk = _open_lock(lp)
        if not lk.acquire(timeout):
            raise LockTimeout('%s is locked by another process for more than %.0f seconds: nothing was written' % (os.path.basename(target), timeout))
        _HELD_LOCKS[key] = 1
        try:
            yield lp
        finally:
            _HELD_LOCKS.pop(key, None)
            lk.release()
    finally:
        if lk is not None:
            lk.close()
        _unboost(boosted)


def utf8_safe(text):
    """(text that can be written as UTF-8, how many characters were replaced). A lone surrogate (a \\ud800 escape in a METAR or an NCM text, valid JSON) cannot be encoded: it is
    replaced by U+FFFD and counted, so that one bad character never stops a run or a record (audit N4)."""
    try:
        text.encode('utf-8')
        return text, 0
    except UnicodeEncodeError:
        out = []
        n = 0
        for ch in text:
            if '\ud800' <= ch <= '\udfff':
                out.append('�')
                n += 1
            else:
                out.append(ch)
        return ''.join(out), n


def scrub_text(o):
    """in place: every string in a dict or list that holds a lone surrogate gets U+FFFD instead. Returns how many characters were replaced."""
    n = 0
    stack = [o]
    while stack:
        x = stack.pop()
        items = list(x.items()) if isinstance(x, dict) else (list(enumerate(x)) if isinstance(x, list) else ())
        for k, v in items:
            if isinstance(v, str):
                s, c = utf8_safe(v)
                if c:
                    x[k] = s
                    n += c
            elif isinstance(v, (dict, list)):
                stack.append(v)
    return n


def _write_all(fd, b):
    view = memoryview(b)
    while len(view):
        n = os.write(fd, view)
        view = view[n:]


def append_durable(path, text, timeout=LOCK_TIMEOUT_S, sync=True):
    """Append one line with a single write, flush and fsync, under the cross process lock of file_lock (audit N3). Earlier lines are never touched. If the file does not end with a
    newline (a crash left half a line), the new line starts on a fresh line, so the half line stays as it was and stays bad: a reader skips it and counts it, a writer never extends it.
    sync=False leaves out the fsync; only a test of the lock, which takes the disk out of a contention run, passes it."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = text if text.endswith('\n') else text + '\n'
    data, _ = utf8_safe(data)
    b = data.encode('utf-8')
    fd = None
    try:
        with file_lock(path, timeout):
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o644)       # binary: on Windows a text mode write turns \n into \r\n (audit V12)
            size = os.fstat(fd).st_size
            if size > 0:
                os.lseek(fd, size - 1, os.SEEK_SET)
                if os.read(fd, 1) != b'\n':
                    b = b'\n' + b
            _write_all(fd, b)
        # The lock covers the check of the last byte and the write, which is all that must not overlap. The fsync, which takes tens of milliseconds, is made after the lock is given up, so
        # that a writer that goes on appending cannot keep the others waiting for it: the data is in the file for every other writer and reader from the write on, and the fsync flushes it,
        # with whatever other writers have appended since, to the disk.
        if sync:
            os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)


def append_jsonl_locked(path, make, timeout=LOCK_TIMEOUT_S):
    """The pattern of every append only log that gives its lines ids (ncm_log.jsonl, ncm_refused.jsonl, the channel log): under one lock the log is read, make(rows) builds the line from
    what is in it (the next id, the duplicate check) and the line is appended, so two writers can never draw the same id or both pass a duplicate check. make returns
    (object or None, result); None writes nothing. Returns result. rows are the readable lines (a cut line is not among them)."""
    path = os.path.abspath(path)
    with file_lock(path, timeout):
        rows, _ = read_jsonl(path)
        obj, result = make(rows)
        if obj is not None:
            append_durable(path, json.dumps(obj, ensure_ascii=False, separators=(',', ':')), timeout)
        return result


def read_json(path, default=None):
    """Read only: a missing, empty or broken file gives the default and nothing is created."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_json_status(path):
    """Read only. Returns (data, status). status: 'ok'; 'missing' (there is no file); 'damaged' (the file is there and does not parse, or is empty);
    'unreadable' (the file is there and cannot be opened). A file that exists but does not parse is never the same as a missing one: a record the engine
    keeps (events.json, an obs month, season_cache.json) must be set aside before anything is written over it (audit D4, WX-04, WX-05)."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        return None, 'missing'
    except OSError:
        return None, 'unreadable'
    try:
        return json.loads(raw.decode('utf-8-sig')), 'ok'                 # a byte order mark (an editor, Excel) is not damage
    except (ValueError, UnicodeDecodeError):
        return None, 'damaged'


def read_text_raw(path):
    """the text of a file for a salvage attempt; '' when it cannot be read"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def quarantine_copy(path):
    """copy a damaged record to <name>.corrupt-<stamp> beside it, so that what is left of it is never lost when the file is rewritten. Returns the new path;
    raises OSError when the copy cannot be made (the caller then refuses to write over the record)."""
    import shutil
    stamp = time.strftime('%Y%m%dT%H%M%S')
    dest, n = '%s.corrupt-%s' % (path, stamp), 1
    while os.path.exists(dest):
        n += 1
        dest = '%s.corrupt-%s-%d' % (path, stamp, n)
    shutil.copy2(path, dest)
    return dest


def list_quarantined(folder):
    """names (relative to folder) of the damaged records kept aside in folder and its obs and ledger folders"""
    out = []
    for sub in ('', 'obs'):
        d = os.path.join(folder, sub) if sub else folder
        try:
            for n in sorted(os.listdir(d)):
                if '.corrupt-' in n:
                    out.append(os.path.join(sub, n) if sub else n)
        except OSError:
            pass
    return out


def clean_stale_tmp(folder, max_age_s=3600):
    """remove .tmp_*.part files that a killed run left behind (older than max_age_s). Returns how many were removed."""
    n = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if name.startswith('.tmp_') and name.endswith('.part'):
            p = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    return n


def pid_alive(pid):
    """True when a process with this id is running. Windows: OpenProcess (os.kill with 0 would send a Ctrl+C event there). None when it cannot be told."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenProcess(0x1000, 0, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return True if ctypes.get_last_error() == 5 else False      # 5 is access denied: the process exists
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259                    # STILL_ACTIVE
                return None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def read_jsonl(path):
    """Read only. Returns (rows, bad_line_count). A half written last line, or any line that is not a JSON object, is skipped and counted."""
    rows, bad = [], 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    if isinstance(o, dict):
                        rows.append(o)
                    else:
                        bad += 1
                except ValueError:
                    bad += 1
    except OSError:
        pass
    return rows, bad


# ------------------------------------------------------------------------------------------------ numbers
def nz(v):
    """a number or None; NaN and non numbers become None. Null is never treated as zero."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x):
    return None if x is None else round(x, 1)


PLAN_EDGES = (5.0, 10.0, 15.0, 25.0, 30.0)          # the 6 h thresholds of the plan (10, 25), the proposed vote (5, 25) and the 24 h bands of the plan table (5, 15, 30), in mm


def r1_safe(x, edges=PLAN_EDGES):
    """round to 0.1 mm for display, but never up across a threshold: 9.99 mm shows as 9.9, not 10.0, because the level is decided on the unrounded figure and a screen that prints 10.0 beside
    a level of none contradicts it (audit DISPLAY-1). A figure that is already at or over an edge rounds as usual."""
    if x is None:
        return None
    r = round(x, 1)
    for e in edges:
        if x < e <= r:
            return round(e - 0.1, 1)
    return r


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi:
            inside = not inside
        j = i
    return inside


def point_in_polygon(lon, lat, geom):
    """GeoJSON Polygon or MultiPolygon"""
    if not geom:
        return False
    polys = geom['coordinates'] if geom.get('type') == 'MultiPolygon' else [geom['coordinates']]
    for poly in polys:
        if poly and point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def short_text(s, n=160):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 1].rstrip() + '.'
