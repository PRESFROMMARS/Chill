"""Bounded process, driver and diagnostic primitives; no work at import time."""
import ctypes
import hashlib
import logging
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import time
from concurrent.futures import Future


_DEVICE_ID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{8}-[0-9a-f]{16}|[0-9a-f]{40})(?![0-9a-f])", re.I)
_USER_PATH = re.compile(r"(?P<root>(?:[a-z]:)?[\\/]+Users[\\/]+)[^\\/\r\n\"'<>:]+", re.I)


def redact_diagnostics(text: str) -> str:
    return _USER_PATH.sub(r"\g<root><REDACTED>", _DEVICE_ID.sub("<REDACTED>", text))


class InstanceRunningError(RuntimeError):
    code = "instance_running"

    def __init__(self):
        super().__init__("检测到另一个程序实例正在运行，请勿多开 / Another instance controls this device")


class ProcessFileLock:
    """Nonblocking OS lease. Never unlink lock files (which would split leases)."""

    def __init__(self, path):
        self.path = Path(path)
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        try:
            os.set_inheritable(stream.fileno(), False)
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            import errno
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise InstanceRunningError() from exc
            raise
        self._stream = stream
        return self

    def close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


def device_lock(udid):
    # Independent of --config: two config paths must not control one iPhone.
    key = hashlib.sha256(udid.replace("-", "").casefold().encode("utf-8")).hexdigest()
    return ProcessFileLock(Path(tempfile.gettempdir())/"piggy-device-locks"/(key+".lock"))


class DaemonDriverExecutor:
    """Bounded daemon calls, never registered with futures' interpreter join hook.

    A running native call cannot be killed. Its Future remains unfinished until
    the actual call returns; shutdown only cancels work that has not started.
    """

    def __init__(self, max_workers=8):
        self._limit = max_workers
        self._lock = threading.Lock()
        self._work = {}
        self._closed = False

    def submit(self, function, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("driver executor is closed")
            if len(self._work) >= self._limit:
                raise RuntimeError("too many unfinished synchronous driver calls")
            future = Future()

            def run():
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            result = function(*args, **kwargs)
                        except BaseException as exc:
                            future.set_exception(exc)
                        else:
                            future.set_result(result)
                finally:
                    with self._lock:
                        self._work.pop(future, None)

            worker = threading.Thread(target=run, name="PiggySyncDriver", daemon=True)
            self._work[future] = worker
            try:
                worker.start()
            except BaseException:
                self._work.pop(future, None)
                raise
            return future

    def shutdown(self, wait=False, cancel_futures=True):
        with self._lock:
            self._closed = True
            work = list(self._work.items())
        if cancel_futures:
            for future, _ in work:
                future.cancel()
        if wait:
            for _, worker in work:
                worker.join()


class ParentWatchdog:
    """Managed-child-only watchdog; Windows handles also avoid PID-reuse races."""

    def __init__(self, parent_pid, interval=.5, on_exit=None):
        if parent_pid <= 1 or parent_pid == os.getpid():
            raise ValueError("invalid managed tunnel parent PID")
        self.parent_pid = parent_pid
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._kernel = self._parent_handle = self._job = None
        self._on_exit = on_exit or self._terminate_group

    def _windows_handles(self):
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        self._kernel = kernel
        self._parent_handle = kernel.OpenProcess(0x00100000, False, self.parent_pid)  # SYNCHRONIZE
        if not self._parent_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        # Kill-on-close covers descendants even if a driver creates new groups.
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_mem", ctypes.c_size_t),
                        ("job_mem", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.AssignProcessToJobObject.restype = w.BOOL
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        if not (kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and
                kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
            error = ctypes.get_last_error()
            kernel.CloseHandle(job)
            raise ctypes.WinError(error)
        # Intentionally process-owned: closing this handle kills our own job.
        # The OS closes it on process exit, including os._exit / TerminateProcess.
        self._job = job

    def _alive(self):
        if self._kernel is not None:
            return self._kernel.WaitForSingleObject(self._parent_handle, 0) == 258  # WAIT_TIMEOUT
        if os.getppid() != self.parent_pid:
            return False
        try:
            os.kill(self.parent_pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def _terminate_group():
        if os.name != "nt" and os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(1)

    def start(self):
        try:
            if os.name == "nt":
                self._windows_handles()
            if not self._alive():
                raise RuntimeError("managed tunnel parent already exited")
            def watch():
                while not self._stop.wait(self.interval):
                    if not self._alive():
                        self._on_exit()
                        return
            self._thread = threading.Thread(target=watch, name="PiggyParentWatchdog", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval+.1)
        if self._parent_handle is not None:
            self._kernel.CloseHandle(self._parent_handle)
            self._parent_handle = None


class BoundedFaultLog(logging.Handler):
    """One file, UTF-8 summaries only, strict byte ceiling, no backup pile."""

    MAX_BYTES = 256*1024

    def __init__(self, path):
        super().__init__(logging.WARNING)
        self.path = Path(path)
        self._last = None
        self._last_at = -float("inf")

    def emit(self, record):
        if not record.name.startswith(("engine", "piggy")) or (
                record.levelno < logging.ERROR and not getattr(record, "fault", False)):
            return
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += ": "+type(record.exc_info[1]).__name__+": "+str(record.exc_info[1])
            message = redact_diagnostics("".join(c for c in message if c in "\n\t" or c.isprintable()))[-4096:]
            key = (record.levelno, message)
            now = time.monotonic()
            if key == self._last and now-self._last_at < 30:
                return
            line = (time.strftime("%Y-%m-%d %H:%M:%S")+" "+record.levelname+" "+message+"\n").encode("utf-8")
            with ProcessFileLock(self.path.with_suffix(".log.lock")):
                previous = b""
                if self.path.exists():
                    with self.path.open("rb") as stream:
                        stream.seek(max(0, os.fstat(stream.fileno()).st_size-self.MAX_BYTES))
                        previous = stream.read(self.MAX_BYTES)
                    previous = redact_diagnostics(previous.decode("utf-8", errors="ignore")).encode("utf-8")
                content = (previous+line)[-self.MAX_BYTES:]
                # Keep the first UTF-8 codepoint intact after byte truncation.
                while content and content[0] & 0xc0 == 0x80:
                    content = content[1:]
                with self.path.open("wb") as stream:
                    stream.write(content)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
            self._last, self._last_at = key, now
        except (OSError, ValueError, InstanceRunningError):
            # Diagnostics must not make an already failing shutdown fail again.
            pass
