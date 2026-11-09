"""Chill desktop entry point and validated JavaScript/Python boundary.

Run: python main.py                 (pywebview desktop)
     python main.py --preview --sandbox --config /tmp/piggy-preview.json
The preview serves the SAME API and real Sandbox engine on loopback only.
"""
import engine  # Apply proxy immunity before importing network/UI libraries.

import argparse
import atexit
import ctypes
import functools
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
import urllib.parse
import urllib.request
import ddi_support
from runtime_support import BoundedFaultLog, InstanceRunningError, ParentWatchdog

LOGGER = logging.getLogger("piggy")
ACTIVE_STATES = {"LOCKED_START", "CRUISING_LOOP", "CRUISING_DETOUR"}
DEFAULT_CENTER = [31.0250, 121.4340]
MAX_NODES = 1000
PREFLIGHT_CHECK_COUNT = 7


class FaultHooks:
    """Persist uncaught main/thread failures even when the GUI has no console."""

    def install(self):
        self._previous_sys = sys.excepthook
        self._previous_thread = threading.excepthook
        def system_failure(kind, value, traceback):
            LOGGER.error("Unhandled process exception", exc_info=(kind, value, traceback))
            self._previous_sys(kind, value, traceback)
        def thread_failure(arguments):
            LOGGER.error("Unhandled worker exception", exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback))
            self._previous_thread(arguments)
        self._system, self._thread = system_failure, thread_failure
        sys.excepthook, threading.excepthook = self._system, self._thread

    def close(self):
        if sys.excepthook is self._system:
            sys.excepthook = self._previous_sys
        if threading.excepthook is self._thread:
            threading.excepthook = self._previous_thread


class AppError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _points(value: Any, *, minimum: int = 0, targets: bool = False) -> list[dict]:
    if not isinstance(value, list) or not minimum <= len(value) <= MAX_NODES:
        raise AppError("invalid_points")
    result = []
    for i, item in enumerate(value):
        try:
            lat, lon = engine._point(item)
            if abs(lat) >= 85:
                raise ValueError("polar coordinate")
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise AppError("invalid_points") from None
        point = {"lat": lat, "lon": lon}
        if targets:
            marked = item.get("is_target", False) if isinstance(item, dict) else False
            if not isinstance(marked, bool):
                raise AppError("invalid_points")
            point.update(is_target=marked, id=i)
        result.append(point)
    return result


def _profile(value: Any) -> str:
    if value not in ("standard", "high_cadence"):
        raise AppError("invalid_profile")
    return value


def _target(value: Any) -> float:
    try:
        value = engine._finite(value, "target")
    except (ValueError, TypeError, OverflowError):
        raise AppError("invalid_target") from None
    if not 0.1 <= value <= 100:
        raise AppError("invalid_target")
    return value


def _track_width(value: Any) -> float:
    try:
        width = engine._finite(value, "track_width_m")
    except (ValueError, TypeError, OverflowError):
        raise AppError("invalid_track_width") from None
    if not 1 <= width <= 8:
        raise AppError("invalid_track_width")
    return width


def _extra_distance(value: Any) -> bool:
    if not isinstance(value, bool):
        raise AppError("invalid_preferences")
    return value


class ConfigStore:
    """Validated atomic configuration; failed writes never replace live config."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else Path.home()/".pace_config.json"
        self._lock = threading.RLock()
        self.issue = None
        self._data = {"version": 1, "language": "zh", "profile": "standard", "target_km": 2.0,
                      "track_points": [], "track_width_m": 8.0, "extra_distance": False, "map_center": DEFAULT_CENTER, "map_zoom": 17, "layer": "hybrid"}
        try:
            if self.path.exists():
                if self.path.stat().st_size > 2*1024*1024:
                    raise ValueError("oversized config")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = self._validate({**self._data, **raw})
        except (OSError, ValueError, TypeError, AppError):
            self.issue = "config_load_failed"
            LOGGER.warning("configuration could not be read; defaults loaded", exc_info=True)

    @staticmethod
    def _validate(data: dict) -> dict:
        if data.get("language") not in ("zh", "en") or data.get("layer") not in ("hybrid", "street"):
            raise AppError("invalid_preferences")
        center = _points([data.get("map_center", DEFAULT_CENTER)], minimum=1)[0]
        try:
            zoom = int(data.get("map_zoom", 17))
        except (ValueError, TypeError, OverflowError):
            raise AppError("invalid_preferences") from None
        if not 3 <= zoom <= 20:
            raise AppError("invalid_preferences")
        return {"version": 1, "language": data["language"], "profile": _profile(data["profile"]),
                "target_km": _target(data["target_km"]), "track_points": _points(data["track_points"]),
                "track_width_m": _track_width(data.get("track_width_m", 8.0)),
                "extra_distance": _extra_distance(data.get("extra_distance", False)),
                "map_center": [center["lat"], center["lon"]], "map_zoom": zoom, "layer": data["layer"]}

    def read(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, changes: dict) -> dict:
        with self._lock:
            candidate = self._validate({**self._data, **changes})
            temp_name = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent),
                                                 prefix=".piggy-", suffix=".tmp", delete=False) as file:
                    temp_name = file.name
                    json.dump(candidate, file, ensure_ascii=False, indent=2, allow_nan=False)
                    file.flush()
                    os.fsync(file.fileno())
                if os.name != "nt":
                    os.chmod(temp_name, 0o600)
                os.replace(temp_name, self.path)
            except (OSError, ValueError):
                if temp_name is not None:
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        LOGGER.debug("temporary config cleanup failed", exc_info=True)
                raise AppError("config_save_failed") from None
            self._data = candidate
            self.issue = None
            return self.read()


class WindowsProtection:
    """Windows execution-state owner and process-bound macOS sleep protection."""

    def __init__(self):
        self._stop = threading.Event()
        self._driver_checked = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._caffeinate = None
        self.status = {"platform": sys.platform, "awake": "not_required", "driver": "not_required"}

    @staticmethod
    def _quiet(args: list[str], timeout: float = 6.0) -> subprocess.CompletedProcess:
        encoding = "utf-8"
        if sys.platform == "win32":
            try:
                codepage = int(ctypes.windll.kernel32.GetOEMCP())
                encoding = f"cp{codepage}" if codepage else "utf-8"
            except (AttributeError, OSError, TypeError, ValueError):
                LOGGER.debug("Windows console codepage could not be read")
        return subprocess.run(args, capture_output=True, text=True, encoding=encoding, errors="replace",
                              timeout=timeout, check=False, shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def start(self, *, prepare_driver: bool = True) -> None:
        if sys.platform == "darwin":
            with self._lock:
                if self._stop.is_set() or self._caffeinate is not None and self._caffeinate.poll() is None:
                    return
                try:
                    self._caffeinate = subprocess.Popen(
                        ["/usr/bin/caffeinate", "-di", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        shell=False)
                    self.status["awake"] = "enabled" if self._caffeinate.poll() is None else "failed"
                except OSError:
                    self.status["awake"] = "failed"
                    LOGGER.warning("macOS sleep protection could not start", exc_info=True)
            return
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, args=(prepare_driver,),
                                        name="PiggyWindowsProtection", daemon=True)
        self._thread.start()

    def _set(self, **values: Any) -> None:
        with self._lock:
            self.status.update(values)

    def wait_for_driver(self) -> None:
        if self._thread is not None:
            self._driver_checked.wait()

    def _prepare_apple_service(self) -> str:
        name = "Apple Mobile Device Service"
        query = ["sc.exe", "query", name]

        def state(result):
            match = re.search(r"^\s*STATE\s*:\s*([1-7])\b", result.stdout, re.MULTILINE | re.IGNORECASE)
            return int(match.group(1)) if result.returncode == 0 and match else None

        service = self._quiet(query)
        if service.returncode == 1060:
            return "missing"
        current = state(service)
        if current == 4:
            return "running"
        if current is None:
            LOGGER.warning("Apple Mobile Device Service query failed (exit %s)", service.returncode)
            return "stopped"
        self._set(driver="starting")
        if current != 2:  # SERVICE_START_PENDING
            try:
                configured = self._quiet(["sc.exe", "config", name, "start=", "auto"], timeout=3)
                if configured.returncode:
                    LOGGER.warning("Could not set Apple Mobile Device Service to automatic (exit %s)", configured.returncode)
            except subprocess.TimeoutExpired:
                LOGGER.warning("Setting Apple Mobile Device Service to automatic timed out")
            try:
                started = self._quiet(["sc.exe", "start", name], timeout=3)
            except subprocess.TimeoutExpired:
                pass  # The service may still start after sc.exe times out.
            else:
                if started.returncode not in (0, 1056):  # already running
                    LOGGER.warning("Could not start Apple Mobile Device Service (exit %s)", started.returncode)
                    return "running" if state(self._quiet(query)) == 4 else "stopped"
        deadline = time.monotonic()+8.0
        while not self._stop.is_set():
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            try:
                if state(self._quiet(query, timeout=min(remaining, 1.0))) == 4:
                    return "running"
            except subprocess.TimeoutExpired:
                pass
            self._stop.wait(min(.25, max(0.0, deadline-time.monotonic())))
        LOGGER.warning("Apple Mobile Device Service did not reach RUNNING within 8 seconds")
        return "stopped"

    def _run(self, prepare_driver: bool = True) -> None:
        kernel = None
        try:
            kernel = ctypes.windll.kernel32
            kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
            kernel.SetThreadExecutionState.restype = ctypes.c_uint32
            self._set(awake="enabled" if kernel.SetThreadExecutionState(0x80000003) else "failed")
        except (AttributeError, OSError):
            self._set(awake="failed")
        try:
            if prepare_driver:
                self._set(driver=self._prepare_apple_service())
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("Apple Mobile Device Service could not be prepared", exc_info=True)
            self._set(driver="stopped")
        finally:
            self._driver_checked.set()
        self._stop.wait()
        if kernel is not None:
            try:
                kernel.SetThreadExecutionState(0x80000000)
            except (AttributeError, OSError):
                LOGGER.warning("execution-state restoration failed", exc_info=True)

    def snapshot(self) -> dict:
        with self._lock:
            if self._caffeinate is not None and self._caffeinate.poll() is not None:
                self.status["awake"] = "failed"
            return dict(self.status)

    def close(self) -> None:
        self._stop.set()
        self._driver_checked.set()
        with self._lock:
            child, self._caffeinate = self._caffeinate, None
        if child is not None:
            try:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                LOGGER.warning("macOS sleep protection cleanup failed", exc_info=True)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=14)


# Campus centres, GCJ-02; these are search shortcuts, never fabricated running templates.
CAMPUSES = [
    ("清华大学", "Tsinghua University", "北京", 40.0031, 116.3268, "qinghua tsinghua"),
    ("北京大学", "Peking University", "北京", 39.9929, 116.3109, "beida peking"),
    ("复旦大学邯郸校区", "Fudan University · Handan", "上海", 31.2990, 121.5010, "fudan"),
    ("上海交通大学闵行校区", "Shanghai Jiao Tong University · Minhang", "上海", 31.0250, 121.4340, "sjtu jiaoda"),
    ("浙江大学紫金港校区", "Zhejiang University · Zijingang", "杭州", 30.3089, 120.0858, "zju zheda"),
    ("武汉大学", "Wuhan University", "武汉", 30.5390, 114.3640, "whu wuda"),
    ("中山大学广州校区南校园", "Sun Yat-sen University · South campus", "广州", 23.0964, 113.2988, "sysu zhongshan"),
    ("厦门大学思明校区", "Xiamen University · Siming", "厦门", 24.4384, 118.0985, "xmu xiada"),
    ("南京大学仙林校区", "Nanjing University · Xianlin", "南京", 32.1199, 118.9584, "nju nanda"),
    ("四川大学望江校区", "Sichuan University · Wangjiang", "成都", 30.6290, 104.0838, "scu chuanda"),
    ("西安交通大学兴庆校区", "Xi'an Jiaotong University · Xingqing", "西安", 34.2474, 108.9831, "xjtu"),
    ("电子科技大学清水河校区", "UESTC · Qingshuihe", "成都", 30.7560, 103.9282, "uestc chengdian"),
]


def rpc(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        try:
            return {"ok": True, "data": function(self, *args, **kwargs)}
        except AppError as exc:
            return {"ok": False, "error": {"code": exc.code}}
        except Exception:
            LOGGER.exception("API method %s failed", function.__name__)
            return {"ok": False, "error": {"code": "operation_failed"}}
    return wrapped


class ShutdownHooks:
    """Unwind SIGTERM through main's finally; handle Windows console teardown."""

    def __init__(self, api):
        self.api = api
        self._previous_sigterm = None
        self._signal_installed = False
        self._console_callback = None
        self._console_api = None
        self._terminating = False

    def install(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._sigterm)
            self._signal_installed = True
        if sys.platform == "win32":
            try:
                callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint32)
                self._console_callback = callback_type(self._console_event)
                self._console_api = ctypes.windll.kernel32.SetConsoleCtrlHandler
                self._console_api.argtypes = [callback_type, ctypes.c_int]
                self._console_api.restype = ctypes.c_int
                if not self._console_api(self._console_callback, True):
                    raise OSError("SetConsoleCtrlHandler failed")
            except (AttributeError, OSError):
                LOGGER.exception("Windows console shutdown handler could not be registered")
                self._console_api = None

    def _sigterm(self, signum, frame) -> None:
        if self._terminating or self.api._closed:
            return
        self._terminating = True
        # Do not acquire locks inside a Python signal handler. SystemExit first
        # unwinds the interrupted stack, then main's finally performs cleanup.
        raise SystemExit(128+signum)

    def _console_event(self, event: int) -> bool:
        if event not in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN; Ctrl+C keeps Python's handler.
            return False
        try:
            self.api._shutdown(timeout=3.5)
        except Exception:
            LOGGER.exception("Windows console shutdown cleanup failed")
        return True  # Windows terminates the process after this callback returns.

    def close(self) -> None:
        if self._console_api is not None:
            try:
                self._console_api(self._console_callback, False)
            except (AttributeError, OSError):
                LOGGER.warning("Windows console handler removal failed", exc_info=True)
            self._console_api = None
        if self._signal_installed and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._previous_sigterm)
            self._signal_installed = False


class DesktopAPI:
    """All exposed mutators validate state, types, countdowns and confirmations."""

    METHODS = {"bootstrap", "get_state", "get_trace", "save_preferences", "save_track", "preview_track", "search_places",
               "recheck_time", "lock_start", "start_cruise", "pause", "resume", "submit_detour", "safe_settle",
               "hold_position", "recover_previous_location"}

    def __init__(self, config: Optional[Path] = None, *, sandbox: bool = False):
        self._lock = threading.RLock()
        self._config = ConfigStore(config)
        self._fault_log = BoundedFaultLog(self._config.path.parent/"Chill"/"last_session.log")
        logging.getLogger().addHandler(self._fault_log)
        self._sandbox = sandbox
        marker_path = self._config.path.with_name(self._config.path.stem+".location-session.json")
        self._dirty_marker = engine.DirtySessionMarker(marker_path)
        self._recovery_required = self._dirty_marker.pending
        self._recovering = False
        self._protection = WindowsProtection()
        self._bridge = self._new_bridge()
        self._runner = engine.BiomechanicalRunner(self._bridge)
        self._runner_retired = False
        self._window = None
        self._closed = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_result = None
        self._session_active = False
        self._preparing = False
        self._state_seq = 0
        self._settling = False
        self._restore_failed = False
        self._restore_dispatched = False
        self._countdown_deadline = None
        self._start_connection_attempt = None
        self._start_interrupted = False
        self._clock = {"status": "checking", "offset_s": None, "checked_at": None}
        self._clock_checked_mono = None
        self._clock_worker = None
        self._search_cache = {}
        self._active_detour = []
        atexit.register(self._shutdown)

    def _new_bridge(self):
        return engine.IOSDeviceBridge(sandbox=self._sandbox,
                                      before_discovery=self._protection.wait_for_driver if sys.platform == "win32" else None,
                                      dirty_marker=None if self._sandbox else self._dirty_marker)

    def _start_services(self) -> None:
        self._protection.start(prepare_driver=not self._sandbox)
        if not self._recovery_required:
            self._bridge.connect()
        elif not self._sandbox and self._dirty_marker.udid is not None:
            try:
                self._bridge._claim_device(self._dirty_marker.udid)
            except InstanceRunningError as exc:
                self._bridge.last_error = self._bridge.reason = str(exc)
                self._bridge._device_error_code = exc.code
                LOGGER.error("%s", exc)
        self._begin_clock_check()

    def _begin_clock_check(self) -> None:
        with self._lock:
            if self._closed or self._clock_worker is not None and self._clock_worker.is_alive():
                return
            self._clock["status"] = "checking"
            self._clock_worker = threading.Thread(target=self._check_clock, name="PiggyClockCheck", daemon=True)
            self._clock_worker.start()

    def _check_clock(self) -> None:
        try:
            offset = engine.check_ntp_offset()
            result = {"status": "synced" if abs(offset) <= 30 else "skew", "offset_s": offset, "checked_at": time.time()}
        except engine.TimeSyncError:
            result = {"status": "offline", "offset_s": None, "checked_at": time.time()}
        except Exception:
            LOGGER.exception("clock check failed")
            result = {"status": "offline", "offset_s": None, "checked_at": time.time()}
        with self._lock:
            self._clock = result
            self._clock_checked_mono = time.monotonic()

    def _idle_required(self) -> None:
        if self._closed:
            raise AppError("app_closed")
        if self._recovering:
            raise AppError("recovery_busy")
        if self._preparing:
            raise AppError("operation_in_progress")
        if self._session_active or self._settling or self._runner.state.value in ACTIVE_STATES:
            raise AppError("track_locked")

    def _active_path(self) -> list:
        with self._lock:
            pending = self._runner._pending_detour
            route = pending["route"] if pending is not None else self._runner._detour
            if route is None:
                return []
            result = []
            for lat, lon in route.points[::2]:
                map_lon, map_lat = engine.wgs84_to_gcj02(lon, lat)
                result.append([map_lat, map_lon])
            return result

    def _start_status(self, device: dict) -> tuple[str, float]:
        if not self._sandbox:
            if (not device["device_ready"]
                    or device["reconnect_attempts"] != self._start_connection_attempt):
                self._start_interrupted = True
            if self._start_interrupted:
                self._countdown_deadline = None
                return "interrupted", 0.0
            if device["delivered_sequence"] < self._runner.start_sequence:
                return "injecting", 0.0
        now = time.monotonic()
        if self._countdown_deadline is None:
            self._countdown_deadline = now+5.0
        remaining = max(0.0, self._countdown_deadline-now)
        return ("countdown" if remaining else "ready"), remaining

    def _state(self) -> dict:
        with self._lock:
            state = self._runner.snapshot()
            self._state_seq += 1
            position = state.get("position")
            if position is not None:
                lng, lat = engine.wgs84_to_gcj02(position["lon"], position["lat"])
                state["gcj_position"] = {"lat": lat, "lon": lng}
            else:
                state["gcj_position"] = None
            clock = dict(self._clock)
            if clock["status"] == "synced" and self._clock_checked_mono is not None and time.monotonic()-self._clock_checked_mono > 600:
                clock["status"] = "stale"
            device = self._bridge.snapshot() if self._runner_retired else state["device"]
            state["device"] = device
            start_phase, countdown = ("idle", 0.0)
            if state["state"] == "LOCKED_START" and self._session_active and not self._settling:
                start_phase, countdown = self._start_status(device)
            device.pop("udid", None)
            for field in ("reason", "last_error", "battery_error"):
                if device.get(field):
                    device[field] = engine._diagnostic_text(device[field])
            if state.get("last_error"):
                state["last_error"] = engine._diagnostic_text(state["last_error"])
            state.update(seq=self._state_seq, preparing=self._preparing,
                         session_active=self._session_active, settling=self._settling,
                         recovery_required=self._recovery_required, recovering=self._recovering,
                         recovery_invalid=self._dirty_marker.invalid, recovery_sandbox=self._sandbox,
                         restore_failed=self._restore_failed, restore_dispatched=self._restore_dispatched,
                         start_phase=start_phase, countdown_s=countdown,
                         clock=clock, system=self._protection.snapshot(),
                         detour_pending=self._runner._pending_detour is not None,
                         active_detour=list(self._active_detour), config_issue=self._config.issue,
                         server_time=time.time())
            state["diagnostic_log"] = str(self._fault_log.path)
            # Diagnostics are bounded plain text; the UI uses textContent.
            state["device_issue"] = bool(state["device"].get("last_error"))
            state["device_error"] = engine._diagnostic_text(state["device"].get("last_error") or "")
            state["engine_issue"] = state.get("stop_reason") == "engine_error"
            state["stride_estimate_m"] = state["speed_mps"]/(192/60) if state["speed_mps"] > 0 else 0.0
            return state

    @rpc
    def bootstrap(self) -> dict:
        return {"config": self._config.read(), "state": self._state(), "preview": self._sandbox,
                "active_path": self._active_path()}

    @rpc
    def get_state(self) -> dict:
        return self._state()

    @rpc
    def get_trace(self, after_sequence: int = 0, session_id: Optional[str] = None) -> dict:
        try:
            result = self._runner.trace_since(after_sequence, session_id)
        except ValueError:
            raise AppError("invalid_request") from None
        for point in result["points"]:
            point["lon"], point["lat"] = engine.wgs84_to_gcj02(point["lon"], point["lat"])
        return result

    @rpc
    def save_preferences(self, preferences: dict) -> dict:
        if not isinstance(preferences, dict):
            raise AppError("invalid_preferences")
        allowed = {"language", "profile", "target_km", "track_width_m", "extra_distance", "map_center", "map_zoom", "layer"}
        if set(preferences)-allowed:
            raise AppError("invalid_preferences")
        with self._lock:
            if {"profile", "target_km", "track_width_m", "extra_distance"}.intersection(preferences):
                self._idle_required()
            return self._config.update(preferences)

    @staticmethod
    def _geometry(points: list[dict]) -> tuple[list[tuple], engine.TrackGeometry]:
        try:
            wgs = []
            for p in points:
                lon, lat = engine.gcj02_to_wgs84(p["lon"], p["lat"])
                wgs.append((lat, lon))
            geometry = engine.TrackGeometry(wgs)
            if geometry.length_m < 10:
                raise ValueError("track too short")
            return wgs, geometry
        except engine.GeometryBudgetError:
            raise AppError("route_too_complex") from None
        except (ValueError, ArithmeticError, OverflowError):
            raise AppError("invalid_track") from None

    @rpc
    def preview_track(self, points: list) -> dict:
        points = _points(points, minimum=3)
        _, geometry = self._geometry(points)
        # A 1 m render path is plenty; engine retains its 0.5 m samples.
        rendered = []
        for node in geometry.points[::2]:
            lon, lat = engine.wgs84_to_gcj02(node["lon"], node["lat"])
            rendered.append([lat, lon])
        return {"points": rendered, "length_m": geometry.length_m, "direction": "ccw"}

    @rpc
    def save_track(self, points: list) -> dict:
        points = _points(points)
        if points:
            if len(points) < 3:
                raise AppError("invalid_track")
            self._geometry(points)
        with self._lock:
            self._idle_required()
            return self._config.update({"track_points": points})

    @rpc
    def recheck_time(self) -> dict:
        self._begin_clock_check()
        return self._state()["clock"]

    def _device_for_lock(self) -> dict:
        device = self._bridge.snapshot()
        if self._sandbox or device["device_ready"]:
            return device
        if device["speed_fuse_tripped"]:
            raise AppError("fuseBlocked")
        code = device["error_code"]
        if code in {"multiple_devices", "instance_running", "device_trust_required",
                    "device_developer_mode_required", "tunnel_admin_required"}:
            raise AppError(code)
        stage = device["connection_stage"]
        if stage == "usb":
            checking = device["mode"] in ("CONNECTING", "RECONNECTING") or device["reconnect_attempts"] == 0
            raise AppError("device_usb_checking" if checking and code is None else "device_usb_not_detected")
        raise AppError({"lockdown": "device_pairing_unready", "rsd": "device_rsd_unready",
                        "ddi": "device_ddi_unready", "dvt": "device_dvt_unready",
                        "ready": "device_dvt_unready"}[stage])

    @rpc
    def lock_start(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise AppError("checklist_required")
        checks = payload.get("checks")
        if not isinstance(checks, list) or len(checks) != PREFLIGHT_CHECK_COUNT or any(check is not True for check in checks):
            raise AppError("checklist_required")
        points = _points(payload.get("points"), minimum=3)
        profile, target = _profile(payload.get("profile")), _target(payload.get("target_km"))
        width = _track_width(payload.get("track_width_m", self._config.read()["track_width_m"]))
        extra = _extra_distance(payload.get("extra_distance", self._config.read()["extra_distance"]))
        wgs, geometry = self._geometry(points)
        with self._lock:
            self._idle_required()
            if self._recovery_required:
                raise AppError("recovery_required")
            clock_status = self._clock["status"]
            if (self._clock_checked_mono is None or clock_status not in ("synced", "offline")
                    or (clock_status == "synced" and time.monotonic()-self._clock_checked_mono > 600)):
                raise AppError("clock_required")
            self._device_for_lock()
            # Persistence must succeed BEFORE taking control of device location.
            self._config.update({"track_points": points, "profile": profile, "target_km": target, "track_width_m": width, "extra_distance": extra})
            if self._runner_retired:
                self._runner = engine.BiomechanicalRunner(self._bridge)
                self._runner_retired = False
            self._preparing = True
            runner = self._runner
        try:
            runner.load_track(wgs, profile=profile, target_km=target, track_width_m=width,
                              extra_distance=extra, _geometry=geometry)
            with self._lock, runner._lock, runner.bridge._lock:
                if self._closed or self._runner is not runner:
                    raise AppError("app_closed")
                self._start_connection_attempt = self._device_for_lock()["reconnect_attempts"]
                self._session_active = True
                runner.lock_start()
                self._restore_failed = False
                self._restore_dispatched = False
                self._countdown_deadline = None
                self._start_interrupted = False
                self._active_detour = []
        except engine.GeometryBudgetError:
            raise AppError("route_too_complex") from None
        except AppError:
            raise
        except (ValueError, RuntimeError):
            with self._lock:
                self._session_active = runner.state.value in ACTIVE_STATES
            raise AppError("lock_failed") from None
        finally:
            with self._lock:
                self._preparing = False
        return self._state()

    @rpc
    def start_cruise(self) -> dict:
        with self._lock, self._runner._lock, self._bridge._lock:
            if self._settling or not self._session_active or self._runner.state != engine.RunnerState.LOCKED_START:
                raise AppError("lock_first")
            phase, _ = self._start_status(self._bridge.snapshot())
            if phase != "ready":
                raise AppError({"injecting": "start_injecting", "interrupted": "start_interrupted",
                                "countdown": "countdown_active"}[phase])
            self._runner.start_cruising()
            return self._state()

    @rpc
    def hold_position(self) -> dict:
        with self._lock:
            if self._settling or not self._session_active:
                raise AppError("hold_unavailable")
            try:
                self._runner.hold_position()
            except RuntimeError:
                raise AppError("hold_unavailable") from None
            return self._state()

    @rpc
    def pause(self) -> dict:
        with self._lock:
            if self._settling or not self._session_active:
                raise AppError("rest_unavailable")
            try:
                self._runner.pause()
            except engine.ControlCooldownError:
                raise AppError("control_cooldown") from None
            except RuntimeError:
                raise AppError("rest_unavailable") from None
            return self._state()

    @rpc
    def resume(self) -> dict:
        with self._lock:
            if self._settling or not self._session_active:
                raise AppError("resume_unavailable")
            try:
                self._runner.resume()
            except engine.ControlCooldownError:
                raise AppError("control_cooldown") from None
            except RuntimeError:
                raise AppError("resume_unavailable") from None
            return self._state()

    @rpc
    def submit_detour(self, points: list) -> dict:
        draft = _points(points, minimum=1, targets=True)
        if not any(p["is_target"] for p in draft):
            raise AppError("target_required")
        converted = []
        for p in draft:
            lon, lat = engine.gcj02_to_wgs84(p["lon"], p["lat"])
            converted.append({**p, "lat": lat, "lon": lon})
        with self._lock:
            if self._settling or not self._session_active or self._runner.state != engine.RunnerState.CRUISING_LOOP:
                raise AppError("cruise_first")
            runner = self._runner
        try:
            result = runner.submit_detour(converted)
        except engine.GeometryBudgetError:
            raise AppError("route_too_complex") from None
        except RuntimeError:
            raise AppError("detour_busy") from None
        except (ValueError, ArithmeticError):
            raise AppError("invalid_detour") from None
        with self._lock:
            if self._closed or self._settling or self._runner is not runner or not self._session_active:
                raise AppError("detour_busy")
            self._active_detour = draft
            return {**result, "path": self._active_path(), "state": self._state()}

    @rpc
    def safe_settle(self, confirmation: dict) -> dict:
        if (not isinstance(confirmation, dict) or confirmation.get("app_closed") is not True
                or not (confirmation.get("discarded") is True
                        or (confirmation.get("submitted") is True and confirmation.get("success") is True))):
            raise AppError("settlement_required")
        with self._lock:
            if self._settling:
                raise AppError("settlement_busy")
            if not self._session_active:
                raise AppError("no_session")
            self._settling = True
        try:
            # Confirmation says the phone app has already ended. Freeze motion
            # immediately, even on a branch; continuing it would be incorrect.
            self._runner.close()
            self._runner_retired = True
            cleared = self._bridge.clear_location(timeout=5.0)
            if not cleared:
                with self._lock:
                    self._restore_failed = True
                raise AppError("restore_failed")
            self._bridge.close()
            with self._shutdown_lock, self._lock:
                if not self._closed:
                    self._bridge = self._new_bridge()
                    self._bridge.connect()
                self._session_active = False
                self._restore_failed = False
                self._restore_dispatched = not self._sandbox
                self._countdown_deadline = None
                self._active_detour = []
        finally:
            with self._lock:
                self._settling = False
        return self._state()

    @rpc
    def recover_previous_location(self) -> dict:
        with self._lock:
            self._idle_required()
            if self._sandbox:
                raise AppError("recovery_sandbox")
            if self._dirty_marker.invalid:
                raise AppError("recovery_invalid")
            if not self._recovery_required:
                return self._state()
            self._recovering = True
        try:
            if not self._bridge.clear_location(timeout=5.0):
                raise AppError("restore_failed")
            with self._lock:
                self._recovery_required = False
                self._restore_dispatched = True
        finally:
            with self._lock:
                self._recovering = False
        return self._state()

    @rpc
    def search_places(self, query: str, language: str = "zh") -> dict:
        if not isinstance(query, str) or len(query) > 120 or language not in ("zh", "en"):
            raise AppError("invalid_search")
        query = query.strip()
        coordinate = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*[,，\s]\s*([+-]?\d+(?:\.\d+)?)\s*", query)
        if coordinate:
            lat, lon = map(float, coordinate.groups())
            if abs(lat) > 90:
                lat, lon = lon, lat
            point = _points([[lat, lon]], minimum=1)[0]
            return {"results": [{**point, "name": f"{lat:.6f}, {lon:.6f}", "source": "coordinate"}], "remote": "not_needed"}
        lowered = query.casefold()
        local = []
        for zh, en, city, lat, lon, aliases in CAMPUSES:
            if not query or lowered in (zh+" "+en+" "+city+" "+aliases).casefold():
                local.append({"name": zh if language == "zh" else en, "lat": lat, "lon": lon, "source": "campus"})
        if len(query) < 2 or local:
            return {"results": local[:12], "remote": "not_needed"}
        key = (query, language)
        with self._lock:
            cached = self._search_cache.get(key)
            if cached is not None and time.monotonic()-cached[0] < 120:
                return cached[1]
        remote, status = [], "unavailable"
        # Public website endpoint is best-effort, not a contracted keyless API.
        # No borrowed key, CAPTCHA bypass, scraping session or token extraction.
        url = "https://www.amap.com/service/poiTips?"+urllib.parse.urlencode({"words": query, "city": "", "datatype": "poi", "lang": language})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(url, headers={"User-Agent": "Piggy/1.0", "Accept": "application/json"})
            with opener.open(request, timeout=3) as response:
                raw = response.read(1024*1024+1)
            if len(raw) > 1024*1024:
                raise ValueError("oversized response")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("invalid place suggestion envelope")
            data = data.get("data", data)
            tips = data.get("tip_list", data.get("tips", [])) if isinstance(data, dict) else []
            if not isinstance(tips, list):
                raise ValueError("invalid place suggestion list")
            for item in tips[:20]:
                try:
                    tip = item.get("tip", item)
                    location = tip.get("location", item.get("location"))
                    if isinstance(location, str) and "," in location:
                        lon, lat = map(float, location.split(",")[:2])
                    else:
                        pos = tip.get("pos", tip)
                        lat, lon = float(pos["y"]), float(pos["x"])
                    p = _points([[lat, lon]], minimum=1)[0]
                    name = tip.get("name", item.get("name", ""))
                except (ValueError, TypeError, KeyError, IndexError, AttributeError, AppError):
                    continue
                if not isinstance(name, str) or not name:
                    continue
                # AMap's Chinese-only POI names cannot meet an English-only UI.
                if language == "en" and re.search(r"[\u3400-\u9fff]", name):
                    name = f"{lat:.6f}, {lon:.6f}"
                remote.append({**p, "name": name[:120], "source": "amap"})
            status = "available" if remote else "empty"
        except (OSError, ValueError, TypeError, KeyError, IndexError, AppError):
            LOGGER.debug("public place suggestions unavailable", exc_info=True)
        result = {"results": remote, "remote": status}
        with self._lock:
            if len(self._search_cache) >= 64:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[key] = (time.monotonic(), result)
        return result

    def _attach_window(self, window: Any) -> None:
        self._window = window

    def _on_closing(self, *args: Any) -> bool:
        with self._lock:
            if self._closed:
                return True
            blocked = self._preparing or self._session_active or self._settling or self._runner.state.value in ACTIVE_STATES
        if blocked:
            if self._window is not None:
                def notify():
                    try:
                        self._window.evaluate_js("window.Piggy && window.Piggy.requestCloseWarning()")
                    except Exception:
                        LOGGER.debug("close warning could not be delivered", exc_info=True)
                threading.Thread(target=notify, name="PiggyCloseWarning", daemon=True).start()
            return False
        return True

    def _shutdown(self, timeout: float = 6.0) -> bool:
        if not self._shutdown_lock.acquire(timeout=timeout):
            return False
        try:
            if self._closed:
                return bool(self._shutdown_result)
            self._closed = True
            # Stop production first, but give clear priority over joining other
            # threads. A failed exit leaves the durable marker for next launch.
            self._runner._shutdown.set()
            try:
                self._shutdown_result = self._bridge.close(timeout=timeout)
            except Exception:
                self._shutdown_result = False
                LOGGER.exception("shutdown location restoration failed; dirty marker retained")
            finally:
                for name, close in (("runner", self._runner.close), ("sleep protection", self._protection.close)):
                    try:
                        close()
                    except Exception:
                        self._shutdown_result = False
                        LOGGER.exception("shutdown %s cleanup failed", name)
                atexit.unregister(self._shutdown)
            level = logging.ERROR if not self._shutdown_result else (
                logging.INFO if self._bridge._ever_connected else logging.DEBUG)
            LOGGER.log(level,
                       "shutdown location clear confirmed: %s", self._shutdown_result)
            return bool(self._shutdown_result)
        finally:
            if self._closed:
                logging.getLogger().removeHandler(self._fault_log)
                self._fault_log.close()
            self._shutdown_lock.release()


class LocalServer:
    """Loopback-only static UI and token-protected, allowlisted JSON RPC."""

    def __init__(self, api: DesktopAPI, port: int = 0):
        self.api = api
        self.token = secrets.token_urlsafe(32)
        self.root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))/"ui"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                LOGGER.debug(fmt, *args)

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("preview client disconnected")

            def _host_ok(self) -> bool:
                return self.headers.get("Host") == owner.address

            def do_GET(self):
                if not self._host_ok():
                    self._reply(403, b"Forbidden", "text/plain")
                    return
                path = urllib.parse.urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    try:
                        body = (owner.root/"index.html").read_text(encoding="utf-8").replace("__PIGGY_SESSION_TOKEN__", owner.token).encode("utf-8")
                        self._reply(200, body, "text/html; charset=utf-8")
                    except OSError:
                        self._reply(500, b"Chill UI is missing", "text/plain")
                elif path in ("/favicon.ico", "/assets/chill-32.png", "/assets/chill-180.png", "/assets/chill-192.png",
                              "/assets/chill-512.png", "/assets/chill.png", "/assets/chill.ico",
                              "/vendor/leaflet.js", "/vendor/leaflet.css", "/vendor/LEAFLET-LICENSE.txt"):
                    try:
                        file = owner.root/("assets/chill.ico" if path == "/favicon.ico" else path.lstrip("/"))
                        content_type = {".js": "application/javascript", ".css": "text/css", ".txt": "text/plain", ".png": "image/png", ".ico": "image/x-icon"}[file.suffix]
                        self._reply(200, file.read_bytes(), content_type+"; charset=utf-8")
                    except OSError:
                        self._reply(404, b"Not found", "text/plain")
                else:
                    self._reply(404, b"Not found", "text/plain")

            def do_POST(self):
                origin = self.headers.get("Origin")
                if (not self._host_ok() or self.headers.get("Authorization") != "Bearer "+owner.token or
                        origin is not None and origin != owner.url or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                    self._reply(403, b"Forbidden", "text/plain")
                    return
                method = urllib.parse.urlsplit(self.path).path.removeprefix("/api/")
                if not self.path.startswith("/api/") or method not in DesktopAPI.METHODS:
                    self._reply(404, b"Not found", "text/plain")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2*1024*1024:
                        raise ValueError("body length")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not isinstance(payload.get("args", []), list):
                        raise ValueError("RPC arguments")
                    result = getattr(owner.api, method)(*payload.get("args", []))
                    body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    self._reply(200, body, "application/json; charset=utf-8")
                except (ValueError, TypeError, OSError):
                    self._reply(400, b'{"ok":false,"error":{"code":"invalid_request"}}', "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.address = "127.0.0.1:"+str(self.server.server_port)
        self.url = "http://"+self.address
        self._thread = threading.Thread(target=self.server.serve_forever, name="PiggyLoopbackUI", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._thread.is_alive():
            self.server.shutdown()
            self._thread.join(timeout=3)
        self.server.server_close()


def _prepare_windowed_streams(*, child_mode: bool = False) -> None:
    """PyInstaller --noconsole supplies None streams; CLI libraries need files."""
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            if child_mode and name != "stdin":
                fd = 1 if name == "stdout" else 2
                if os.name == "nt":
                    import msvcrt
                    from ctypes import wintypes as w
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.GetStdHandle.argtypes = [w.DWORD]
                    kernel.GetStdHandle.restype = w.HANDLE
                    kernel.GetCurrentProcess.restype = w.HANDLE
                    kernel.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE,
                                                       ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
                    kernel.DuplicateHandle.restype = w.BOOL
                    kernel.CloseHandle.argtypes = [w.HANDLE]
                    source = kernel.GetStdHandle(-11 if fd == 1 else -12)
                    duplicate = w.HANDLE()
                    owner = kernel.GetCurrentProcess()
                    if not kernel.DuplicateHandle(owner, source, owner, ctypes.byref(duplicate), 0, False, 2):
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        output_fd = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
                    except BaseException:
                        kernel.CloseHandle(duplicate)
                        raise
                else:
                    output_fd = os.dup(fd)
                stream = os.fdopen(output_fd, "w", encoding="utf-8", errors="replace", buffering=1)
            else:
                stream = open(os.devnull, mode, encoding="utf-8")
            setattr(sys, name, stream)
            atexit.register(stream.close)


def _run_tunneld(arguments: list[str]) -> int:
    """Child-process-only entry: fixed loopback daemon, never another GUI."""
    expected = ["--host", "127.0.0.1", "--port", "49151", "--protocol", "tcp"]
    if arguments != expected and arguments != ["--help"]:
        LOGGER.error("Invalid private tunneld arguments")
        return 2
    original = sys.argv
    try:
        from pymobiledevice3.__main__ import main as device_cli
        sys.argv = [sys.executable, "remote", "tunneld", *arguments]
        try:
            code = device_cli()
        except SystemExit as stopped:
            code = stopped.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except Exception:
        LOGGER.exception("Packaged tunneld could not start")
        return 1
    finally:
        sys.argv = original


def _run_tunnel_command(arguments: list[str]) -> int:
    expected = ["--host", "127.0.0.1", "--port", "49151", "--protocol", "tcp"]
    if len(arguments) <= len(expected) or arguments[-6:] != expected:
        LOGGER.error("Invalid managed tunnel command")
        return 2
    child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, shell=False)
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1)


def _check_device_driver_imports() -> int:
    """Exercise the frozen userspace, DDI and DVT imports without opening USB."""
    try:
        from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.services.cryptexd import CryptexdService, load_cryptex1_assets
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import PersonalizedImageMounter, uses_cryptex_image

        if not all(callable(entry) for entry in (PreferredRsdTunnel, UserspaceRsdTunnel, CryptexdService.auto_install_ddi,
                                                  DvtProvider, LocationSimulation.set,
                                                  PersonalizedImageMounter.mount, uses_cryptex_image)):
            raise RuntimeError("incomplete device driver API")
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))/ddi_support.DDI_ROOT
        for variant in ddi_support.DDI_ASSETS:
            ddi_support.image_files(root/variant, variant)
        load_cryptex1_assets(root/"Cryptex")
    except Exception:
        LOGGER.exception("Packaged device driver import check failed")
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    child_mode = any(flag in arguments for flag in ("--piggy-tunneld", "--start-tunneld", "--piggy-tunnel-command"))
    _prepare_windowed_streams(child_mode=child_mode or "--piggy-check-device-driver" in arguments)
    parser = argparse.ArgumentParser(description="Chill desktop route workbench")
    parser.add_argument("--preview", action="store_true", help="serve a local browser preview (always Sandbox)")
    parser.add_argument("--sandbox", action="store_true", help="never connect to an iPhone")
    parser.add_argument("--config", type=Path, help="override ~/.pace_config.json")
    parser.add_argument("--port", type=int, default=0, help="loopback preview port (0 selects a free port)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--piggy-tunneld", "--start-tunneld", nargs=argparse.REMAINDER, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--piggy-tunnel-command", nargs=argparse.REMAINDER, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--piggy-check-device-driver", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if child_mode:
        watchdog = None
        try:
            if args.parent_pid is not None:
                watchdog = ParentWatchdog(args.parent_pid).start()
            if args.piggy_tunnel_command is not None:
                return _run_tunnel_command(args.piggy_tunnel_command)
            return _run_tunneld(args.piggy_tunneld)
        except Exception:
            LOGGER.exception("Managed tunnel could not start")
            return 1
        finally:
            if watchdog is not None:
                watchdog.close()
    if args.parent_pid is not None:
        parser.error("--parent-pid requires a private tunnel entry")
    if args.piggy_check_device_driver:
        return _check_device_driver_imports()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    webview = None
    if not args.preview:
        try:
            import webview
        except ImportError:
            print("请先安装 pywebview：python -m pip install pywebview\nInstall pywebview, or use --preview --sandbox.", file=sys.stderr)
            return 2
    api = DesktopAPI(args.config, sandbox=args.sandbox or args.preview)
    server = None
    hooks = ShutdownHooks(api)
    fault_hooks = FaultHooks()
    shutdown_result = None
    try:
        fault_hooks.install()
        hooks.install()
        server = LocalServer(api, args.port)
        server.start()
        api._start_services()
        if args.preview:
            print("Chill preview: "+server.url, flush=True)
            threading.Event().wait()
        else:
            window = webview.create_window("Chill", server.url, js_api=api, width=1380, height=860,
                                           min_size=(980, 680), background_color="#181818", text_select=False)
            api._attach_window(window)
            window.events.closing += api._on_closing
            window.events.closed += api._shutdown
            webview.start(debug=args.debug)
    except KeyboardInterrupt:
        LOGGER.info("Chill preview stopped")
    except Exception:
        LOGGER.exception("Chill could not start")
        return 1
    finally:
        try:
            shutdown_result = api._shutdown()
        finally:
            hooks.close()
            fault_hooks.close()
            if server is not None:
                server.close()
    return 1 if shutdown_result is False else 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    code = main()
    if "--parent-pid" in sys.argv:
        # Vendor non-daemon threads must not hold a finished private CLI alive.
        LOGGER.log(logging.INFO if code == 0 else logging.ERROR, "Managed tunnel exited with status %s", code)
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
        if os.name != "nt" and os.getpgrp() == os.getpid():
            # A CLI can return while a vendor worker still holds the inherited
            # pipe. End the complete owned group; the original status is above.
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(code)
    raise SystemExit(code)
