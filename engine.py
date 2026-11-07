import os

# Must precede networking imports, including optional third-party drivers.
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ[_proxy_key] = ""

"""Piggy motion engine (Python 3.9+, standard library only in Sandbox).

Public coordinates are WGS-84 (latitude, longitude), except the two conversion
functions, whose arguments AND return values are (longitude, latitude).
Distances are metres, bearings clockwise degrees from north, curvature rad/m.
No network, device discovery, or thread is started merely by importing this file.

Integration: bridge = IOSDeviceBridge(); runner = BiomechanicalRunner(bridge)
runner.load_track(points); runner.lock_start(); runner.start_cruising()
Poll runner.snapshot() / runner.events from the UI; close both objects on exit
(a bridge supplied by the caller remains owned by that caller).
The nominal tick period is 0.95 s (1.0526 Hz), not exactly 1.0 Hz. Real-device
motion waits for outstanding sends; latency delays production instead of
building a backlog. Desktop OS scheduling cannot promise hard real-time
deadlines. Suspend gaps are discarded.
"""

import atexit
import asyncio
import bisect
import contextlib
import email.utils
import functools
import importlib
import inspect
import json
import logging
import math
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from runtime_support import DaemonDriverExecutor, InstanceRunningError, ProcessFileLock, device_lock, redact_diagnostics

__all__ = ["check_ntp_offset", "TimeSyncError", "wgs84_to_gcj02", "gcj02_to_wgs84",
           "haversine_distance", "calculate_bearing", "TrackGeometry", "RunnerState",
           "BiomechanicalRunner", "BridgeMode", "IOSDeviceBridge", "DirtySessionMarker"]
LOGGER = logging.getLogger(__name__)
EARTH_RADIUS_M = 6371008.8
GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323
EPS_M = 1e-7
# One physical sample may represent at most one second of motion. A delayed
# delivery never earns extra distance, and the 0.95 s rate slot must not shrink
# a legally generated one-second step to 0.95 seconds after a backlog drains.
MOTION_INTERVAL_S = 0.95
MAX_MOTION_STEP_S = 1.0
MAX_MOTION_SPEED_MPS = 3.8
MAX_SAMPLE_DISTANCE_M = MAX_MOTION_SPEED_MPS * MAX_MOTION_STEP_S
DISTANCE_TOLERANCE_M = 1e-5


class TimeSyncError(RuntimeError):
    """No usable reference clock was reachable; never interpret this as offset 0."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _latlon(lat: Any, lon: Any) -> tuple[float, float]:
    lat, lon = _finite(lat, "latitude"), _finite(lon, "longitude")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("latitude/longitude outside [-90,90]/[-180,180]")
    return lat, lon


def _point(point: Any) -> tuple[float, float]:
    if isinstance(point, Mapping):
        return _latlon(point["lat"], point["lon"])
    if len(point) != 2:
        raise ValueError("points must contain (lat, lon)")
    return _latlon(point[0], point[1])


def check_ntp_offset(
    urls: Sequence[str] = ("https://www.baidu.com", "https://www.aliyun.com", "http://www.baidu.com"),
    timeout: float = 3.0,
    on_warning: Optional[Callable[[str], None]] = None,
) -> float:
    """Estimate reference-minus-local seconds from fresh HTTP Date headers.

    This compatibility name does NOT imply authenticated NTP. HTTP Date has
    second resolution and proxy/cache/server uncertainty; use it for the 30 s
    UI warning, never for security decisions or setting the system clock.
    Uses the lowest-RTT successful source, local monotonic RTT and a wall-clock
    midpoint. A changing local wall clock, stale cache or absent Date is rejected.
    Total worst-case HTTP wait is approximately len(urls)*timeout.
    Raises TimeSyncError if every source fails, rather than faking synchrony.
    """
    timeout = _finite(timeout, "timeout")
    if timeout <= 0 or not urls:
        raise ValueError("positive timeout and at least one URL are required")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    samples, failures = [], []
    for url in urls:
        if not url.startswith(("https://", "http://")):
            raise ValueError("clock sources must be HTTP(S) URLs")
        request = urllib.request.Request(url, method="HEAD", headers={
            "Cache-Control": "no-cache, no-store", "Pragma": "no-cache", "User-Agent": "Piggy/1.0"})
        wall_before, mono_before = time.time(), time.monotonic()
        try:
            with opener.open(request, timeout=timeout) as response:
                header = response.headers.get("Date")
                age = float(response.headers.get("Age", "0"))
                if not header or not math.isfinite(age) or age < 0 or age > 5:
                    raise ValueError("missing Date or stale cached response")
                standard = email.utils.parsedate_to_datetime(header)
                if standard.tzinfo is None:
                    raise ValueError("Date must include a timezone")
                timestamp = standard.timestamp() + age + 0.5
            elapsed, wall_after = time.monotonic() - mono_before, time.time()
            if abs((wall_after - wall_before) - elapsed) > 0.25:
                raise ValueError("system clock changed during request")
            samples.append((elapsed, timestamp - (wall_before + elapsed / 2)))
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            failures.append(f"{url}: {exc}")
    if not samples:
        raise TimeSyncError("; ".join(failures))
    offset = min(samples, key=lambda sample: sample[0])[1]
    if abs(offset) > 30:
        warning = f"系统时间与 HTTP 参考时间相差 {offset:+.1f} 秒（阈值 30 秒）"
        LOGGER.warning(warning)
        if on_warning is not None:
            on_warning(warning)
    return offset


def _outside_china(lng: float, lat: float) -> bool:
    # Conventional GCJ applicability rectangle, not a political boundary mask.
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _gcj_delta(lng: float, lat: float) -> tuple[float, float]:
    x, y = lng - 105.0, lat - 35.0
    dlat = (-100.0 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x)))
    dlat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    dlat += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    dlng = (300.0 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x)))
    dlng += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlng += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    dlng += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    radlat = math.radians(lat)
    magic = 1.0 - GCJ_EE * math.sin(radlat)**2
    root = math.sqrt(magic)
    return (dlng * 180 / (GCJ_A/root*math.cos(radlat)*math.pi),
            dlat * 180 / ((GCJ_A*(1-GCJ_EE))/(magic*root)*math.pi))


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    lat, lng = _latlon(lat, lng)
    if _outside_china(lng, lat):
        return lng, lat
    dlng, dlat = _gcj_delta(lng, lat)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """Inverse by fixed-point correction, with forward residual <= 1e-10 deg.

    The conventional rectangle introduces a discontinuity at its boundary:
    for ambiguous boundary coordinates the outside-China identity convention
    applies. No inverse can disambiguate overlapping coordinate images there.
    """
    lat, lng = _latlon(lat, lng)
    if _outside_china(lng, lat):
        return lng, lat
    guess_lng, guess_lat = lng, lat
    for _ in range(64):
        # Unmasked delta avoids iteration oscillation at the applicability edge.
        dlng, dlat = _gcj_delta(guess_lng, guess_lat)
        error_lng = guess_lng + dlng - lng
        error_lat = guess_lat + dlat - lat
        if max(abs(error_lng), abs(error_lat)) <= 1e-10:
            return guess_lng, guess_lat
        guess_lng -= error_lng
        guess_lat -= error_lat
    raise ArithmeticError("GCJ-02 inverse did not converge")


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1 = _latlon(lat1, lon1)
    lat2, lon2 = _latlon(lat2, lon2)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2-p1, math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*EARTH_RADIUS_M*math.atan2(math.sqrt(max(0, min(1, a))), math.sqrt(max(0, 1-a)))


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1 = _latlon(lat1, lon1)
    lat2, lon2 = _latlon(lat2, lon2)
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2-lon1)
    y = math.sin(dl)*math.cos(p2)
    x = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def _destination(point: tuple[float, float], bearing: float, metres: float) -> tuple[float, float]:
    p, l, b, d = math.radians(point[0]), math.radians(point[1]), math.radians(bearing), metres/EARTH_RADIUS_M
    p2 = math.asin(max(-1, min(1, math.sin(p)*math.cos(d)+math.cos(p)*math.sin(d)*math.cos(b))))
    l2 = l + math.atan2(math.sin(b)*math.sin(d)*math.cos(p), math.cos(d)-math.sin(p)*math.sin(p2))
    return math.degrees(p2), (math.degrees(l2)+180) % 360 - 180


def _interpolate(a: tuple[float, float], b: tuple[float, float], fraction: float) -> tuple[float, float]:
    if fraction <= 0:
        return a
    if fraction >= 1:
        return b
    return _destination(a, calculate_bearing(*a, *b), haversine_distance(*a, *b)*fraction)


def _angle_delta(a: float, b: float) -> float:
    return (b-a+180) % 360 - 180


class _Projection:
    """Local equirectangular metric frame for small, nonpolar running routes."""

    def __init__(self, origin: tuple[float, float]):
        self.lat, self.lon = origin
        if abs(self.lat) >= 85:
            raise ValueError("running routes must stay between 85°S and 85°N")
        self.cos_lat = math.cos(math.radians(self.lat))

    def xy(self, point: tuple[float, float]) -> tuple[float, float]:
        return (EARTH_RADIUS_M*math.radians((point[1]-self.lon+180) % 360-180)*self.cos_lat,
                EARTH_RADIUS_M*math.radians(point[0]-self.lat))

    def ll(self, xy: tuple[float, float]) -> tuple[float, float]:
        return (self.lat+math.degrees(xy[1]/EARTH_RADIUS_M),
                (self.lon+math.degrees(xy[0]/(EARTH_RADIUS_M*self.cos_lat))+180) % 360-180)


class GeometryBudgetError(ValueError):
    """A route exceeded bounded points or cooperative computation time."""


_geometry_local = threading.local()
GEOMETRY_SECONDS = 2.0
LIVE_GEOMETRY_SECONDS = 0.25


def _geometry_check() -> None:
    deadline = getattr(_geometry_local, "deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        raise GeometryBudgetError("route calculation timed out; shorten the route or reduce anchors")


@contextlib.contextmanager
def _geometry_window(seconds: float):
    previous = getattr(_geometry_local, "deadline", None)
    _geometry_local.deadline = min(previous if previous is not None else math.inf, time.monotonic()+seconds)
    try:
        _geometry_check()
        yield
        _geometry_check()
    finally:
        _geometry_local.deadline = previous


def _bounded_geometry(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        with _geometry_window(GEOMETRY_SECONDS):
            return function(*args, **kwargs)
    return guarded


def _nearest_checked(points, point, start=0):
    best, distance = start, math.inf
    for index in range(start, len(points)):
        if index % 64 == 0:
            _geometry_check()
        candidate = haversine_distance(*points[index], *point)
        if candidate < distance:
            best, distance = index, candidate
    return best


class TrackGeometry:
    """Interpolating centripetal Catmull-Rom and spherical arc-length sampling.

    All full intervals are spacing_m along the flattened spline. A closed ring
    has one residual seam < spacing_m unless its perimeter is an exact multiple;
    an open route retains its exact last endpoint with a residual final interval.
    Equal geodesic CHORDS are not equal curved arc lengths. Dense flattening uses
    <=0.1 m edges and <=0.2 mm local chord error, not fixed parameter increments.
    """

    MAX_POINTS = 25000

    @_bounded_geometry
    def __init__(self, points: Optional[Iterable[Any]] = None, closed: bool = True, spacing_m: float = 0.5,
                 *, preprocess: bool = True):
        self.closed = bool(closed)
        self.spacing_m = _finite(spacing_m, "spacing_m")
        if self.spacing_m <= 0:
            raise ValueError("spacing_m must be positive")
        self.anchors = [] if points is None else (
            self.prepare_anchors(points, self.closed) if preprocess else self._collect(points))
        self.points = [] if points is None else self.build(self.anchors, self.closed, self.spacing_m, preprocess=False)
        self.length_m = 0.0
        if self.points:
            self.length_m = _Route(self.points, self.closed).length

    @staticmethod
    def _collect(points: Iterable[Any], limit: Optional[int] = None) -> list[Any]:
        limit = TrackGeometry.MAX_POINTS if limit is None else limit
        result = []
        for index, point in enumerate(points):
            if index % 64 == 0:
                _geometry_check()
            if index >= limit:
                raise GeometryBudgetError("too many input points")
            result.append(point)
        return result

    @staticmethod
    def _clean(points: Iterable[Any], closed: bool, max_points: int = 20000) -> list[tuple[float, float]]:
        result = []
        for index, item in enumerate(points):
            if index % 64 == 0:
                _geometry_check()
            if index > max_points:
                raise GeometryBudgetError("too many input points")
            point = _point(item)
            if not result or haversine_distance(*result[-1], *point) > 1e-5:
                result.append(point)
            if len(result) > max_points:
                raise GeometryBudgetError("too many control points")
        if closed and len(result) > 1 and haversine_distance(*result[0], *result[-1]) < 1e-5:
            result.pop()
        if len(result) < (3 if closed else 2):
            raise ValueError("a ring needs 3 distinct points; an open route needs 2")
        frame = _Projection(result[0])
        if any(math.hypot(*frame.xy(p)) > 100000 for p in result):
            raise ValueError("local running routes must fit within 100 km of their origin")
        return result

    @staticmethod
    def ensure_counter_clockwise(points: Iterable[Any]) -> list[tuple[float, float]]:
        original = [_point(p) for p in TrackGeometry._collect(points)]
        explicitly_closed = len(original) > 1 and original[0] == original[-1]
        clean = TrackGeometry._clean(original, True)
        projection = _Projection(clean[0])
        xy = [projection.xy(p) for p in clean]
        area2 = math.fsum(xy[i][0]*xy[(i+1) % len(xy)][1] -
                          xy[(i+1) % len(xy)][0]*xy[i][1] for i in range(len(xy)))
        if abs(area2) < 1e-4:
            raise ValueError("ring has zero signed area (collinear or self-cancelling)")
        if area2 < 0:
            # Keep the template's named starting point while reversing direction.
            clean = [clean[0]] + list(reversed(clean[1:]))
        return clean + [clean[0]] if explicitly_closed else clean

    @staticmethod
    def _flatten(evaluate: Callable[[float], tuple[float, float]], max_edge: float = 0.1) -> list[tuple[float, float]]:
        output = [evaluate(0.0)]

        def split(t0: float, p0: tuple, t1: float, p1: tuple, depth: int) -> None:
            if len(output) % 64 == 0:
                _geometry_check()
            if len(output) >= TrackGeometry.MAX_POINTS:
                raise GeometryBudgetError("spline exceeds point budget")
            tm = (t0+t1)/2
            pm = evaluate(tm)
            chord = math.hypot(p1[0]-p0[0], p1[1]-p0[1])
            half = math.hypot(pm[0]-p0[0], pm[1]-p0[1]) + math.hypot(p1[0]-pm[0], p1[1]-pm[1])
            if chord <= max_edge and half-chord <= 0.0002:
                output.append(p1)
            elif depth >= 24 or len(output) >= TrackGeometry.MAX_POINTS:
                raise GeometryBudgetError("spline exceeds adaptive subdivision budget")
            else:
                split(t0, p0, tm, pm, depth+1)
                split(tm, pm, t1, p1, depth+1)

        # Seed quarters to detect S-curves whose midpoint lies on their chord.
        for index in range(4):
            t0, t1 = index/4, (index+1)/4
            split(t0, evaluate(t0), t1, evaluate(t1), 0)
        return output

    @staticmethod
    def catmull_rom(points: Iterable[Any], closed: bool = True) -> list[tuple[float, float]]:
        clean = TrackGeometry._clean(points, closed)
        if closed:
            clean = TrackGeometry._clean(TrackGeometry.ensure_counter_clockwise(clean), True)
        frame = _Projection(clean[0])
        xy = [frame.xy(p) for p in clean]
        output = []
        n = len(xy)
        for i in range(n if closed else n-1):
            _geometry_check()
            p1, p2 = xy[i], xy[(i+1) % n]
            p0 = xy[(i-1) % n] if closed or i > 0 else (2*p1[0]-p2[0], 2*p1[1]-p2[1])
            p3 = xy[(i+2) % n] if closed or i+2 < n else (2*p2[0]-p1[0], 2*p2[1]-p1[1])
            # Centripetal knot spacing t[i+1]-t[i] = ||P[i+1]-P[i]||^0.5.
            d01 = max(1e-8, math.hypot(p1[0]-p0[0], p1[1]-p0[1])**0.5)
            d12 = max(1e-8, math.hypot(p2[0]-p1[0], p2[1]-p1[1])**0.5)
            d23 = max(1e-8, math.hypot(p3[0]-p2[0], p3[1]-p2[1])**0.5)
            m1 = tuple(d12*((p1[k]-p0[k])/d01-(p2[k]-p0[k])/(d01+d12)+(p2[k]-p1[k])/d12) for k in (0, 1))
            m2 = tuple(d12*((p2[k]-p1[k])/d12-(p3[k]-p1[k])/(d12+d23)+(p3[k]-p2[k])/d23) for k in (0, 1))

            def evaluate(t: float) -> tuple[float, float]:
                return tuple((2*t**3-3*t**2+1)*p1[k]+(t**3-2*t**2+t)*m1[k]+
                             (-2*t**3+3*t**2)*p2[k]+(t**3-t**2)*m2[k] for k in (0, 1))

            segment = TrackGeometry._flatten(evaluate)
            # Retain the exact control coordinate (especially marked targets),
            # without a projection round-trip at the segment boundary.
            output.append(clean[i])
            output.extend(frame.ll(p) for p in segment[1:-1])
            if len(output) > TrackGeometry.MAX_POINTS:
                raise GeometryBudgetError("spline is too long")
        if len(output) >= TrackGeometry.MAX_POINTS:
            raise GeometryBudgetError("spline is too long")
        output.append(clean[0] if closed else clean[-1])
        return output

    @staticmethod
    def _decorate(points: list[tuple[float, float]], closed: bool) -> list[dict[str, float]]:
        n = len(points)
        output, cumulative = [], 0.0
        for i, point in enumerate(points):
            if i % 64 == 0:
                _geometry_check()
            prev, nxt = points[(i-1) % n], points[(i+1) % n]
            if not closed and i == 0:
                prev = point
            if not closed and i == n-1:
                nxt = point
            before, after = haversine_distance(*prev, *point), haversine_distance(*point, *nxt)
            incoming = calculate_bearing(*prev, *point) if before > EPS_M else calculate_bearing(*point, *nxt)
            outgoing = calculate_bearing(*point, *nxt) if after > EPS_M else incoming
            turn = math.radians(_angle_delta(incoming, outgoing))
            curvature = abs(turn)/max(EPS_M, (before+after)/2) if before > EPS_M and after > EPS_M else 0.0
            if i:
                cumulative += haversine_distance(*points[i-1], *point)
            output.append({"lat": point[0], "lon": point[1], "bearing": (incoming+math.degrees(turn)/2) % 360,
                           "curvature": curvature, "s_m": cumulative})
        if not closed and n > 2:
            output[0]["curvature"], output[-1]["curvature"] = output[1]["curvature"], output[-2]["curvature"]
        return output

    @staticmethod
    def resample_arc_length(points: Iterable[Any], spacing_m: float = 0.5, closed: bool = True,
                            *, preserve_points: Iterable[Any] = ()) -> list[dict[str, float]]:
        spacing_m = _finite(spacing_m, "spacing_m")
        if spacing_m <= 0:
            raise ValueError("spacing_m must be positive")
        clean = TrackGeometry._clean(points, closed, TrackGeometry.MAX_POINTS)
        dense = clean + [clean[0]] if closed else clean
        cumulative = [0.0]
        for index, (a, b) in enumerate(zip(dense, dense[1:])):
            if index % 64 == 0:
                _geometry_check()
            cumulative.append(cumulative[-1]+haversine_distance(*a, *b))
        total = cumulative[-1]
        count = int(math.floor(total/spacing_m))
        if count+2 > TrackGeometry.MAX_POINTS:
            raise GeometryBudgetError("resampling exceeds point budget")
        distances = [i*spacing_m for i in range(count+1) if i*spacing_m < total-1e-8]
        protected = {_point(p) for p in TrackGeometry._collect(preserve_points)}
        if protected:
            distances = sorted(set(distances + [s for p, s in zip(dense, cumulative)
                                                if p in protected and s < total-1e-8]))
        if not closed:
            distances.append(total)
        if len(distances) > TrackGeometry.MAX_POINTS:
            raise GeometryBudgetError("protected anchors exceed resampling point budget")
        sampled = []
        for index, distance in enumerate(distances):
            if index % 64 == 0:
                _geometry_check()
            idx = min(len(dense)-2, max(0, bisect.bisect_right(cumulative, distance)-1))
            fraction = (distance-cumulative[idx])/(cumulative[idx+1]-cumulative[idx])
            sampled.append(_interpolate(dense[idx], dense[idx+1], fraction))
        if len(sampled) < (3 if closed else 2):
            raise ValueError("route too short for the requested sample spacing")
        result = TrackGeometry._decorate(sampled, closed)
        for node, distance in zip(result, distances):
            node["s_m"] = distance
        return result

    @staticmethod
    def prepare_anchors(points: Iterable[Any], closed: bool = True) -> list[dict[str, Any]]:
        """Denoise user controls once; never move targets or open endpoints.

        A simultaneous 25% Laplacian pass is bounded to 0.75 m at noisy
        corners and 0.05 m on regular gentle bends, preserving calibrated ovals.
        Rebuilt laps must bypass this input-only pass to avoid shrinkage.
        """
        metadata, raw = {}, []
        for item in points:
            point = _point(item)
            if len(raw) >= 20000:
                raise GeometryBudgetError("too many control points")
            marked = isinstance(item, Mapping) and bool(item.get("is_target", False))
            previous = metadata.get(point, {})
            metadata[point] = {**previous, **(dict(item) if isinstance(item, Mapping) else {}),
                               "lat": point[0], "lon": point[1],
                               "is_target": marked or previous.get("is_target", False)}
            raw.append(point)
        clean = TrackGeometry._clean(raw, closed)
        nodes = [metadata[p] for p in clean]
        minimum = 3 if closed else 2
        filtered = []
        for index, node in enumerate(nodes):
            close = (filtered and not node["is_target"] and not filtered[-1]["is_target"]
                     and haversine_distance(*_point(filtered[-1]), *_point(node)) < 3.0)
            if close and not closed and index == len(nodes)-1:
                # Keep both ends of an open route, removing an interior duplicate.
                if len(filtered) > 1:
                    filtered.pop()
            elif close and len(filtered)+len(nodes)-index-1 >= minimum:
                continue
            filtered.append(node)
        while (closed and len(filtered) > minimum and not filtered[0]["is_target"]
               and not filtered[-1]["is_target"]
               and haversine_distance(*_point(filtered[-1]), *_point(filtered[0])) < 3.0):
            filtered.pop()
        frame = _Projection(_point(filtered[0]))
        xy = [frame.xy(_point(p)) for p in filtered]
        turns = []
        for index, here in enumerate(xy):
            if not closed and index in (0, len(xy)-1):
                turns.append(0.0)
                continue
            before, after = xy[index-1], xy[(index+1) % len(xy)]
            incoming = math.atan2(here[1]-before[1], here[0]-before[0])
            outgoing = math.atan2(after[1]-here[1], after[0]-here[0])
            turns.append(_angle_delta(math.degrees(incoming), math.degrees(outgoing)))
        result = []
        for index, node in enumerate(filtered):
            node = dict(node)
            if not node["is_target"] and (closed or 0 < index < len(filtered)-1):
                before, here, after = xy[index-1], xy[index], xy[(index+1) % len(xy)]
                dx = 0.25*((before[0]+after[0])/2-here[0])
                dy = 0.25*((before[1]+after[1])/2-here[1])
                # A zigzag can be shallow yet noisy. Give sign-reversing
                # corners the full pass while limiting normal oval shrinkage.
                zigzag = abs(turns[index]) >= 1.0 and any(
                    turns[index]*neighbor < 0 and abs(neighbor) >= 1.0
                    for neighbor in (turns[index-1], turns[(index+1) % len(turns)]))
                limit = 0.75 if not closed or abs(turns[index]) >= 30.0 or zigzag else 0.05
                scale = min(1.0, limit/max(1e-12, math.hypot(dx, dy)))
                node["lat"], node["lon"] = frame.ll((here[0]+dx*scale, here[1]+dy*scale))
            result.append(node)
        if closed:
            ordered = TrackGeometry.ensure_counter_clockwise(result)
            if _point(result[1]) != ordered[1]:
                result = [result[0]]+list(reversed(result[1:]))
        return result

    @staticmethod
    @_bounded_geometry
    def build(points: Iterable[Any], closed: bool = True, spacing_m: float = 0.5,
              *, preprocess: bool = True) -> list[dict[str, float]]:
        controls = TrackGeometry.prepare_anchors(points, closed) if preprocess else TrackGeometry._collect(points)
        protected = [p for p in controls if isinstance(p, Mapping) and p.get("is_target", False)]
        return TrackGeometry.resample_arc_length(TrackGeometry.catmull_rom(controls, closed), spacing_m, closed,
                                                preserve_points=protected)

    @staticmethod
    def stadium_anchors(center: Any, straight_m: float, radius_m: float,
                        bearing_deg: float = 0.0) -> list[tuple[float, float]]:
        """CCW metric stadium anchors; centre is WGS-84, axis bearing is north=0.

        straight_m is the length of EACH straight, radius_m the baseline bend
        radius. Dense anchors avoid the corner overshoot of a four-point oval.
        The template remains editable and goes through the ordinary spline path.
        """
        frame = _Projection(_point(center))
        straight = _finite(straight_m, "straight_m")
        radius = _finite(radius_m, "radius_m")
        angle = math.radians(_finite(bearing_deg, "bearing_deg"))
        if not 0 <= straight <= 10000 or not 2 <= radius <= 1000:
            raise ValueError("invalid stadium dimensions")
        points = []
        def append(x: float, y: float) -> None:
            # Rotate clockwise from north (east x, north y).
            points.append(frame.ll((x*math.cos(angle)+y*math.sin(angle),
                                    -x*math.sin(angle)+y*math.cos(angle))))
        segments = max(1, math.ceil(straight/10))
        for side in (1, -1):
            begin = 0 if side == 1 else math.pi
            for i in range(13):
                theta = begin+i*math.pi/12
                append(radius*math.cos(theta), side*straight/2+radius*math.sin(theta))
            if straight:
                for i in range(1, segments):
                    append(-side*radius, side*straight/2-side*straight*i/segments)
        return TrackGeometry._clean(points, True)

    @staticmethod
    def tangent_connector(start: Any, end: Any, start_bearing: float, end_bearing: float) -> list[tuple[float, float]]:
        """Cubic Hermite connector with prescribed endpoint headings (C1 joins)."""
        a, b = _point(start), _point(end)
        distance = haversine_distance(*a, *b)
        if distance <= 1e-5:
            return [a]
        frame = _Projection(a)
        p0, p1 = frame.xy(a), frame.xy(b)
        scale = max(0.1, distance*0.65)
        m0 = (scale*math.sin(math.radians(start_bearing)), scale*math.cos(math.radians(start_bearing)))
        m1 = (scale*math.sin(math.radians(end_bearing)), scale*math.cos(math.radians(end_bearing)))

        def evaluate(t: float) -> tuple[float, float]:
            return tuple((2*t**3-3*t*t+1)*p0[k]+(t**3-2*t*t+t)*m0[k]+
                         (-2*t**3+3*t*t)*p1[k]+(t**3-t*t)*m1[k] for k in (0, 1))

        result = [frame.ll(p) for p in TrackGeometry._flatten(evaluate)]
        result[0], result[-1] = a, b
        return result


class _Route:
    """Immutable metric polyline; closed rings include the last-to-first edge."""

    def __init__(self, nodes: Iterable[Any], closed: bool = False, *, allow_zero: bool = False):
        self.points = [_point(p) for p in TrackGeometry._collect(nodes, TrackGeometry.MAX_POINTS+1)]
        if len(self.points) < 2:
            raise ValueError("route needs at least two points")
        self.closed = closed
        self.nodes = TrackGeometry._decorate(self.points, closed)
        self.cumulative = [0.0]
        edges = self.points+[self.points[0]] if closed else self.points
        for a, b in zip(edges, edges[1:]):
            self.cumulative.append(self.cumulative[-1]+haversine_distance(*a, *b))
        self.length = self.cumulative[-1]
        if self.length < 1e-5 and not allow_zero:
            raise ValueError("route has no length")

    def sample(self, distance: float) -> dict[str, float]:
        s = distance % self.length if self.closed else max(0, min(self.length, distance))
        idx = min(len(self.cumulative)-2, max(0, bisect.bisect_right(self.cumulative, s)-1))
        nxt = (idx+1) % len(self.points)
        length = self.cumulative[idx+1]-self.cumulative[idx]
        fraction = (s-self.cumulative[idx])/length if length > EPS_M else 0.0
        lat, lon = _interpolate(self.points[idx], self.points[nxt], fraction)
        a, b = self.nodes[idx], self.nodes[nxt]
        return {"lat": lat, "lon": lon, "bearing": (a["bearing"]+fraction*_angle_delta(a["bearing"], b["bearing"])) % 360,
                "curvature": a["curvature"]*(1-fraction)+b["curvature"]*fraction, "s_m": s}

    def nearest_index(self, point: Any) -> int:
        point = _point(point)
        return _nearest_checked(self.points, point)


class _TrackCorridor:
    """Metric strip around the saved centreline, plus its excluded infield.

    Satellite pixels are not survey data: callers must place that centreline
    on the actual running surface. The grid bounds distance queries to nearby
    segments; the ray test is only needed for points outside the strip.
    """

    def __init__(self, route: _Route, width_m: float):
        self.frame = _Projection(route.points[0])
        self.xy = [self.frame.xy(p) for p in route.points]
        self.half_width = width_m/2
        self.cell = max(2.0, self.half_width+1)
        self.segments, self.grid = [], {}
        for a, b in zip(self.xy, self.xy[1:]+self.xy[:1]):
            _geometry_check()
            index = len(self.segments)
            self.segments.append((a, b))
            for x in range(math.floor(min(a[0], b[0])/self.cell), math.floor(max(a[0], b[0])/self.cell)+1):
                for y in range(math.floor(min(a[1], b[1])/self.cell), math.floor(max(a[1], b[1])/self.cell)+1):
                    self.grid.setdefault((x, y), []).append(index)

    def within(self, point: Any, radius: Optional[float] = None) -> bool:
        _geometry_check()
        x, y = self.frame.xy(_point(point))
        radius = self.half_width if radius is None else radius
        cx, cy = math.floor(x/self.cell), math.floor(y/self.cell)
        reach = math.ceil(radius/self.cell)
        for gx in range(cx-reach, cx+reach+1):
            for gy in range(cy-reach, cy+reach+1):
                for index in self.grid.get((gx, gy), ()):
                    a, b = self.segments[index]
                    dx, dy = b[0]-a[0], b[1]-a[1]
                    t = max(0.0, min(1.0, ((x-a[0])*dx+(y-a[1])*dy)/max(1e-20, dx*dx+dy*dy)))
                    if (x-a[0]-t*dx)**2+(y-a[1]-t*dy)**2 <= radius*radius:
                        return True
        return False

    def crosses_infield(self, point: Any) -> bool:
        if self.within(point, self.half_width-0.05):
            return False
        x, y = self.frame.xy(_point(point))
        inside = False
        for a, b in self.segments:
            _geometry_check()
            if (a[1] > y) != (b[1] > y) and x < a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1]):
                inside = not inside
        return inside


class ControlCooldownError(RuntimeError):
    """A motion control was repeated before its monotonic cooldown elapsed."""


class SpeedFuseError(RuntimeError):
    """A coordinate cannot be delivered within the configured speed bound."""


class RunnerState(str, Enum):
    IDLE = "IDLE"
    LOCKED_START = "LOCKED_START"
    CRUISING_LOOP = "CRUISING_LOOP"
    CRUISING_DETOUR = "CRUISING_DETOUR"
    SAFE_STOPPED = "SAFE_STOPPED"


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value*value*(3-2*value)


def _smootherstep(value: float) -> float:
    """Quintic blend with zero first AND second derivatives at both ends."""
    value = max(0.0, min(1.0, value))
    return value**3*(10.0-15.0*value+6.0*value**2)


class _LateralMotion:
    """Distance-domain OU knots with C2 interpolation and timed overtaking.

    X(s+h) = mu + exp(-h/L)*(X(s)-mu)
             + sigma*sqrt(1-exp(-2*h/L))*N(0,1).
    sigma is the stationary standard deviation, not diffusion per sqrt(m).
    Clipped OU observations stay in [-0.4,0.8]; interpolation is smooth and
    bounded. This state is NEVER reset at a lap seam. Branch travel pauses it.
    """
    KNOT_M = 12.0
    MEAN_M = 0.2
    CORRELATION_M = 32.0
    SIGMA_M = 0.28
    RAMP_S = 5.0

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.distance_m = self.time_s = self.left_s = self.left = 0.0
        self.right = self._draw(self.left)
        self.next_event_m = rng.uniform(90.0, 180.0)
        self.event = None
        self.events_started = 0

    def _draw(self, previous: float) -> float:
        phi = math.exp(-self.KNOT_M/self.CORRELATION_M)
        value = self.MEAN_M+phi*(previous-self.MEAN_M)+self.SIGMA_M*math.sqrt(1-phi*phi)*self.rng.gauss(0, 1)
        return max(-0.4, min(0.8, value))

    @classmethod
    def envelope(cls, seconds: float, hold_s: float) -> float:
        if seconds <= 0 or seconds >= 2*cls.RAMP_S+hold_s:
            return 0.0
        if seconds < cls.RAMP_S:
            return (1-math.cos(math.pi*seconds/cls.RAMP_S))/2
        if seconds <= cls.RAMP_S+hold_s:
            return 1.0
        return (1+math.cos(math.pi*(seconds-cls.RAMP_S-hold_s)/cls.RAMP_S))/2

    def sample(self, distance_m: float = 0.0, seconds: float = 0.0) -> float:
        fraction = (self.distance_m+distance_m-self.left_s)/self.KNOT_M
        base = self.left+(self.right-self.left)*_smootherstep(fraction)
        if self.event is None:
            return base
        blend = self.envelope(self.time_s+seconds-self.event["start_s"], self.event["hold_s"])
        return base*(1-blend)+self.event["amplitude_m"]*blend

    def boundary_m(self) -> float:
        knot = self.left_s+self.KNOT_M-self.distance_m
        event = self.next_event_m-self.distance_m if self.event is None else math.inf
        return max(0.0, min(knot, event))

    def advance(self, distance_m: float, seconds: float) -> list[str]:
        self.distance_m += distance_m
        self.time_s += seconds
        transitions = []
        if self.distance_m >= self.left_s+self.KNOT_M-1e-6:
            self.left_s += self.KNOT_M
            self.left, self.right = self.right, self._draw(self.right)
        if self.event is not None and self.time_s >= self.event["start_s"]+2*self.RAMP_S+self.event["hold_s"]:
            self.event = None
            transitions.append("overtake_completed")
        if self.event is None and self.distance_m >= self.next_event_m-1e-6:
            self.event = {"start_s": self.time_s, "start_m": self.distance_m,
                          "amplitude_m": self.rng.uniform(1.2, 2.2), "hold_s": self.rng.uniform(10.0, 15.0)}
            self.next_event_m = self.distance_m+self.rng.uniform(90.0, 180.0)
            self.events_started += 1
            transitions.append("overtake_started")
        return transitions


class BiomechanicalRunner(threading.Thread):
    """Single owner of motion; a bounded event queue is the UI integration API.

    Device I/O uses a nonblocking FIFO owned by IOSDeviceBridge.
    UI callbacks are deliberately not executed on this timing thread. Call
    lock_start(), then start_cruising(); manual step(dt) is available for tests
    only while this thread is not running. State changes and snapshots are locked.
    Loading a template uses WGS-84; convert GCJ map points before calling it.
    """

    TICK_SECONDS = MOTION_INTERVAL_S
    CURVE_THRESHOLD = 0.012  # rad/m; roughly radius <83 m
    MAX_CATCHUP_SECONDS = MAX_MOTION_STEP_S
    MAX_MANUAL_STEP_SECONDS = 1.9  # Numerical test hook, never the live scheduler.
    TRACE_LIMIT = 3600
    LAP_HISTORY_LIMIT = 256
    TARGET_HISTORY_LIMIT = 2048

    def __init__(self, bridge: Optional["IOSDeviceBridge"] = None, *, rng: Optional[random.Random] = None,
                 clock: Optional[Callable[[], float]] = None):
        super().__init__(name="PiggyBiomechanicalRunner", daemon=True)
        self.bridge = bridge if bridge is not None else IOSDeviceBridge()
        self._owns_bridge = bridge is None
        self._rng = rng if rng is not None else random.Random()
        self._control_clock = clock or time.monotonic
        self._control_deadline = -math.inf
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._closed = False
        self.events = queue.Queue(maxsize=2048)
        self.dropped_events = 0
        self.state = RunnerState.IDLE
        self.start_sequence = 0
        self._center = None
        self._corridor = None
        self.track_width_m = 8.0
        self._geometry_limit_m = 0.5
        self._lateral_scale = 1.0
        self._lap = None
        self._connector = None
        self._connector_s = 0.0
        self._loop_s = 0.0
        self._detour = None
        self._detour_s = 0.0
        self._detour_targets = deque()
        self._pending_detour = None
        self.exit_idx = None
        self.entry_idx = None
        self._exit_remaining = None
        self._anchors = []
        self._prepared_lap = None
        self._lateral = None
        self._lane_resume_m = 0.0
        self._geometry_rng = random.Random(self._rng.getrandbits(128))
        self._lap_rng = random.Random(self._rng.getrandbits(128))
        self._checkpoint_rng = random.Random()
        self._checkpoint_rng.setstate(self._rng.getstate())
        self.offset_m = 0.0
        self.laps = 0
        self.current_lap = 0
        self.lap_splits = []
        self._lap_distances = []
        self._lap_start_time = self._lap_start_distance = None
        self.lap_pace_factor = self._pace_factor = 1.0
        self.session_id = uuid.uuid4().hex
        self._trace = deque(maxlen=self.TRACE_LIMIT)
        self._trace_sequence = 0
        self.distance_m = 0.0
        self.elapsed_s = 0.0
        self.speed_mps = 0.0
        self.stride_m = 0.0
        self.profile = "standard"
        self.base_speed_mps = 0.0
        self.requested_target_m = 2000.0
        self.target_m = 2000.0
        self.start_point = None
        self.position = None
        self._noise = 0.0
        self._stop_pending = False
        self._stop_reason = None
        self._target_walking = False
        self._stopped_at = None
        self._hold_elapsed_s = 0.0
        self._pause_phase = "running"
        self._pause_speed = 0.0
        self._walk_center = 1.45
        self._walk_duration_s = 50.0
        self._phase_elapsed_s = 0.0
        self._lane_start_blend = 0.0
        self._tick_lateral_time = 0.0
        self._rest_total_s = 0.0
        self._current_rest_s = 0.0
        self._active_since = None
        self.last_error = None
        self.targets_visited = []
        self.targets_visited_total = 0
        atexit.register(self.close)

    def _event(self, kind: str, **data: Any) -> None:
        item = {"type": kind, "timestamp": time.time(), **data}
        try:
            self.events.put_nowait(item)
        except queue.Full:
            self.dropped_events += 1
            with contextlib.suppress(queue.Empty):
                self.events.get_nowait()
            try:
                self.events.put_nowait(item)
            except queue.Full:
                self.dropped_events += 1

    def _set_state(self, state: RunnerState) -> None:
        previous = self.state
        self.state = state
        self._event("state", previous=previous.value, state=state.value)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("runner is closed")

    @property
    def lateral_offset(self) -> float:
        """Current applied loop offset in metres; offset_m remains compatible."""
        with self._lock:
            return self.offset_m

    def load_track(self, points: Iterable[Any], profile: str = "standard", target_km: float = 2.0,
                   *, track_width_m: float = 8.0, extra_distance: bool = False,
                   _geometry: Optional[TrackGeometry] = None) -> dict[str, Any]:
        if not isinstance(extra_distance, bool):
            raise ValueError("extra_distance must be a boolean")
        if profile not in ("standard", "high_cadence"):
            raise ValueError("profile must be standard or high_cadence")
        target_km = _finite(target_km, "target_km")
        if not 0 < target_km <= 1000:
            raise ValueError("target_km must be in (0,1000]")
        track_width_m = _finite(track_width_m, "track_width_m")
        if not 1 <= track_width_m <= 8:
            raise ValueError("track_width_m must be between 1 and 8 metres")
        # Build before touching live state, so invalid inputs are transactional.
        geometry = _geometry if _geometry is not None else TrackGeometry(points, closed=True)
        anchors = [_point(p) for p in geometry.anchors]
        center = _Route(geometry.points, True)
        if center.length < 10:
            raise ValueError("track perimeter must be at least 10 m")
        corridor = _TrackCorridor(center, track_width_m)
        initial_lap = self._make_lap(perturb=False, center=center, anchors=anchors)
        with self._lock:
            self._require_open()
            if self.state in (RunnerState.CRUISING_LOOP, RunnerState.CRUISING_DETOUR):
                raise RuntimeError("stop cruising before replacing the track")
            self._center = center
            self.track_width_m = track_width_m
            self._corridor = corridor
            safe_half_width = track_width_m/2-0.1
            self._geometry_limit_m = min(0.5, safe_half_width/3)
            self._lateral_scale = min(1.0, (safe_half_width-self._geometry_limit_m)/2.2)
            self._anchors = anchors
            self._lateral = _LateralMotion(random.Random(self._rng.getrandbits(128)))
            self._lane_resume_m = 0.0
            self._prepared_lap = None
            self._lap = initial_lap
            self.profile = profile
            self.base_speed_mps = self._rng.uniform(*( (2.75, 2.95) if profile == "standard" else (3.10, 3.30) ))
            self.requested_target_m = target_km*1000
            self.target_m = self.requested_target_m + (self._rng.uniform(0, min(60.0, self.requested_target_m*0.03))
                                                       if extra_distance else 0.0)
            # Randomise ALONG the track, never on an unconstrained 7 m disk.
            # Follow its final arc to the named seam instead of a grass-cutting
            # Hermite chord. This also works on a one-metre-wide running strip.
            start_s = self._lap.length-self._rng.uniform(4.0, min(7.0, self._lap.length/2))
            self.position = self._lap.sample(start_s)
            self.start_point = _point(self.position)
            connector = [self.start_point]+[p for p, s in zip(self._lap.points, self._lap.cumulative)
                                            if s > start_s]
            self._connector = _Route(connector)
            self._connector_s = self._loop_s = self._detour_s = 0.0
            self._detour = self._pending_detour = self._exit_remaining = None
            self.exit_idx = self.entry_idx = None
            self._detour_targets.clear()
            self.targets_visited.clear()
            self.targets_visited_total = 0
            self.distance_m = self.elapsed_s = self.speed_mps = self.stride_m = self._noise = 0.0
            self.offset_m = 0.0
            self.laps = 0
            self.current_lap = 1
            self.lap_splits.clear()
            self._lap_distances.clear()
            self._lap_start_time = self._lap_start_distance = None
            self.lap_pace_factor = self._pace_factor = self._lap_rng.uniform(0.96, 1.04)
            self.session_id = uuid.uuid4().hex
            self._trace.clear()
            self._trace_sequence = 0
            self.last_error = self._stop_reason = None
            self._active_since = None
            self._stop_pending = False
            self._target_walking = False
            self._stopped_at = None
            self._hold_elapsed_s = 0.0
            self._pause_phase = "running"
            self._pause_speed = self._rest_total_s = self._current_rest_s = 0.0
            self._phase_elapsed_s = self._lane_start_blend = 0.0
            self._control_deadline = -math.inf
            self._set_state(RunnerState.IDLE)
            self._event("track_loaded", base_speed_mps=self.base_speed_mps, target_m=self.target_m,
                        start_point=self.start_point, perimeter_m=center.length)
            return self.snapshot()

    @_bounded_geometry
    def _make_lap(self, *, perturb: bool = True, center: Optional[_Route] = None,
                  anchors: Optional[Sequence] = None, rng: Optional[random.Random] = None,
                  limit_m: Optional[float] = None) -> _Route:
        center = self._center if center is None else center
        if center is None:
            raise RuntimeError("load a track first")
        controls = list(self._anchors if anchors is None else anchors)
        rng = self._geometry_rng if rng is None else rng
        limit_m = self._geometry_limit_m if limit_m is None else limit_m
        if perturb:
            frame = _Projection(controls[0])
            shifted = []
            for point in controls:
                dx, dy = rng.gauss(0, 0.22), rng.gauss(0, 0.22)
                scale = min(1.0, limit_m/max(1e-12, math.hypot(dx, dy)))
                x, y = frame.xy(point)
                shifted.append(frame.ll((x+dx*scale, y+dy*scale)))
            controls = shifted
            candidate = _Route(TrackGeometry(controls, closed=True, preprocess=False).points, True)
            points = []
            # Match canonical position and tangent in a small seam guard band.
            # All controls are perturbed, but deformation fades to zero here.
            guard = min(2.0, center.length/20)
            fade = min(18.0, center.length/5)
            for i, point in enumerate(candidate.points):
                if i % 64 == 0:
                    _geometry_check()
                phase = candidate.cumulative[i]/candidate.length
                canonical = _point(center.sample(phase*center.length))
                seam_distance = min(phase, 1-phase)*center.length
                weight = _smootherstep((seam_distance-guard)/max(1e-6, fade-guard))
                # Bounding controls alone does NOT bound a Catmull-Rom curve.
                # Limit the entire reconstructed deformation relative to the
                # canonical sample, reserving the rest for lateral motion.
                deformation = haversine_distance(*canonical, *point)
                weight *= limit_m/math.hypot(limit_m, deformation)
                points.append(_interpolate(canonical, point, weight))
            points.append(center.points[0])
            nodes = TrackGeometry.resample_arc_length(points, 0.5, False)
            lap = _Route(nodes)
        else:
            lap = _Route(center.points+[center.points[0]])
        lap.control_points = controls
        # The normal at the exact seam must agree, too: otherwise a 2 m offset
        # multiplied by mismatched endpoint headings creates a lateral jump.
        for index in (0, -1):
            lap.nodes[index]["bearing"] = center.nodes[0]["bearing"]
            lap.nodes[index]["curvature"] = center.nodes[0]["curvature"]
        return lap

    def _prepare_next_lap(self, dt: float) -> None:
        with self._lock:
            if (self._closed or self.state != RunnerState.CRUISING_LOOP or self._prepared_lap is not None
                    or self._lap.length-self._loop_s > 15+MAX_MOTION_SPEED_MPS*dt
                    or self.bridge.snapshot()["speed_fuse_tripped"]):
                return
            lap, center, anchors = self._lap, self._center, tuple(self._anchors)
            limit = self._geometry_limit_m
            rng = random.Random()
            rng.setstate(self._geometry_rng.getstate())
        try:
            prepared = self._make_lap(center=center, anchors=anchors, rng=rng, limit_m=limit)
        except GeometryBudgetError:
            with self._lock:
                if not self._closed and self._lap is lap:
                    self._prepared_lap = lap
                    self.last_error = "route_too_complex"
                    self._event("geometry_degraded", reason=self.last_error)
            return
        with self._lock:
            if not self._closed and self.state == RunnerState.CRUISING_LOOP and self._lap is lap and self._prepared_lap is None:
                self._prepared_lap = prepared
                self._geometry_rng.setstate(rng.getstate())

    def lock_start(self, *, background: bool = True) -> dict[str, Any]:
        with self._lock:
            self._require_open()
            if self.state != RunnerState.IDLE or self.position is None:
                raise RuntimeError("load a track in IDLE before locking the start")
            self._set_state(RunnerState.LOCKED_START)
            self._record_trace()
            self.start_sequence = self.bridge.set_location(self.position["lat"], self.position["lon"])
            if background and not self.is_alive():
                super().start()
            return self.snapshot()

    def start_cruising(self, *, background: bool = True) -> dict[str, Any]:
        with self._lock:
            self._require_open()
            if self.state != RunnerState.LOCKED_START:
                raise RuntimeError("lock_start() is required before cruising")
            self._active_since = time.monotonic()
            self._control_deadline = self._control_clock()+1.5
            self._set_state(RunnerState.CRUISING_LOOP)
            if background and not self.is_alive():
                super().start()
            return self.snapshot()

    def submit_detour(self, detour_points: Iterable[Any]) -> dict[str, int]:
        draft = []
        for index, point in enumerate(detour_points):
            if index >= 2000:
                raise ValueError("too many detour control points")
            lat, lon = _point(point)
            draft.append({"lat": lat, "lon": lon, "is_target": bool(point.get("is_target", False))
                          if isinstance(point, Mapping) else False, "id": point.get("id", index)
                          if isinstance(point, Mapping) else index})
        if not draft:
            raise ValueError("detour must contain at least one point")
        for _ in range(3):
            with self._lock:
                self._require_open()
                if self.state != RunnerState.CRUISING_LOOP or self._pending_detour is not None:
                    raise RuntimeError("one detour can be submitted while cruising the loop")
                if self._stop_pending or self._pause_phase != "running":
                    raise RuntimeError("safe stop already pending")
                lap = self._lap
                center = self._center
                phase = self._rng.uniform(0, 2*math.pi)
            for node in draft:
                if haversine_distance(*center.points[0], node["lat"], node["lon"]) > 100000:
                    raise ValueError("detour must remain within 100 km of the track")
            # Index excludes the duplicated seam. It is an actual lane node.
            exit_idx = min(range(len(lap.points)-1), key=lambda i:
                           haversine_distance(*lap.points[i], draft[0]["lat"], draft[0]["lon"]))
            entry_idx = min(range(len(lap.points)-1), key=lambda i:
                            haversine_distance(*lap.points[i], draft[-1]["lat"], draft[-1]["lon"]))
            # Heavy geometry runs outside the timing lock, against an immutable
            # lap snapshot. A lap change retries; a state change rejects cleanly.
            route, targets = self._build_detour(draft, exit_idx, entry_idx, lap=lap, phase=phase)
            with self._lock:
                self._require_open()
                if self.state != RunnerState.CRUISING_LOOP or self._pending_detour is not None or self._stop_pending or self._pause_phase != "running":
                    raise RuntimeError("runner state changed while preparing detour")
                if self._lap is not lap:
                    continue
                self.exit_idx, self.entry_idx = exit_idx, entry_idx
                exit_s = lap.cumulative[exit_idx]
                self._exit_remaining = (exit_s-self._loop_s) % lap.length
                self._pending_detour = {"draft": draft, "route": route, "targets": targets}
                self.last_error = None
                self._event("detour_queued", exit_idx=exit_idx, entry_idx=entry_idx,
                            distance_to_exit_m=self._exit_remaining)
                return {"exit_idx": exit_idx, "entry_idx": entry_idx}
        raise RuntimeError("track changed repeatedly while preparing detour; resubmit")

    @_bounded_geometry
    def _build_detour(self, draft: list[dict[str, Any]], exit_idx: int, entry_idx: int, *,
                      lap: Optional[_Route] = None, phase: Optional[float] = None,
                      exit_position: Optional[dict[str, float]] = None) -> tuple[_Route, list[dict[str, Any]]]:
        lap = self._lap if lap is None else lap
        exit_point, entry_point = lap.points[exit_idx], lap.points[entry_idx]
        exit_heading = lap.nodes[exit_idx]["bearing"]
        if exit_position is not None:
            exit_point, exit_heading = _point(exit_position), exit_position["bearing"]
        clean = []
        for p in draft:
            ll = _point(p)
            if not clean or haversine_distance(*clean[-1], *ll) > 1e-5:
                clean.append(ll)
        body = TrackGeometry.catmull_rom(clean, False) if len(clean) > 1 else clean
        first_heading = calculate_bearing(*body[0], *body[1]) if len(body) > 1 else calculate_bearing(*exit_point, *body[0])
        last_heading = calculate_bearing(*body[-2], *body[-1]) if len(body) > 1 else calculate_bearing(*body[0], *entry_point)
        if len(body) == 1:
            # A one-point out-and-back needs a shared tangent at the checkpoint.
            # Independent outbound/inbound headings make a 180-degree cusp,
            # whose sampled displacement collapses even at constant path speed.
            delta = _angle_delta(first_heading, last_heading)
            first_heading = last_heading = (first_heading+delta/2) % 360
        lead = TrackGeometry.tangent_connector(exit_point, body[0], exit_heading, first_heading)
        tail = TrackGeometry.tangent_connector(body[-1], entry_point, last_heading, lap.nodes[entry_idx]["bearing"])
        dense = lead[:-1]+body+tail[1:]
        # A target exactly ON the exit has no branch length. It still gets a
        # dedicated critical sample before loop motion resumes on the next tick.
        if len(dense) == 1:
            result = _Route([dense[0], dense[0]], allow_zero=True)
            result.entry_idx = entry_idx
            targets = [{"index": 0, "id": p["id"], "lat": p["lat"], "lon": p["lon"], "s_m": 0.0}
                       for p in draft if p["is_target"]]
            return result, targets
        dense = TrackGeometry._clean(dense, False, TrackGeometry.MAX_POINTS)
        route = _Route(dense)
        # Preserve exact target controls. Their ordered nearest matches cannot
        # jump backwards on a self-crossing branch.
        targets, cursor = [], 0
        for p in draft:
            _geometry_check()
            idx = _nearest_checked(dense, (p["lat"], p["lon"]), cursor)
            if p["is_target"]:
                dense[idx] = (p["lat"], p["lon"])
                targets.append({"index": idx, "id": p["id"], "lat": p["lat"], "lon": p["lon"]})
            cursor = idx
        route = _Route(dense)
        # Static smooth normal wander. Inside every target's 15 m disk it is
        # identically zero, so convergence is complete BEFORE the target centre.
        phase = self._rng.uniform(0, 2*math.pi) if phase is None else phase
        target_ll = [(p["lat"], p["lon"]) for p in targets]
        drifted = []
        for i, p in enumerate(dense):
            if i % 64 == 0:
                _geometry_check()
            distance_to_target = min((haversine_distance(*p, *t) for t in target_ll), default=math.inf)
            gate = _smoothstep((distance_to_target-15)/10)
            gate *= _smoothstep(min(route.cumulative[i], route.length-route.cumulative[i])/15)
            offset = 0.6*math.sin(2*math.pi*route.cumulative[i]/80+phase)*gate
            drifted.append(_destination(p, route.nodes[i]["bearing"]+90, offset) if offset else p)
        # Sample each span between targets separately to retain the exact anchor.
        # The shorter anchor intervals are deliberate, unlike loop samples.
        anchors = sorted(set([0, len(drifted)-1]+[t["index"] for t in targets]))
        sampled, mapped = [], {0: 0}
        for begin, end in zip(anchors, anchors[1:]):
            span = drifted[begin:end+1]
            if haversine_distance(*span[0], *span[-1]) < 1e-5 and len(span) == 2:
                continue
            part = TrackGeometry.resample_arc_length(span, 0.5, False)
            sampled.extend(_point(p) for p in (part if not sampled else part[1:]))
            if len(sampled) > TrackGeometry.MAX_POINTS:
                raise GeometryBudgetError("combined detour exceeds point budget")
            mapped[end] = len(sampled)-1
        result = _Route(sampled)
        if any(self._corridor.crosses_infield(p) for p in result.points):
            raise ValueError("detour crosses the excluded infield; add perimeter road anchors")
        result.entry_idx = entry_idx
        for target in targets:
            target["s_m"] = result.cumulative[mapped[target["index"]]]
        return result, targets

    @staticmethod
    def fatigue_factor(distance_m: float) -> float:
        distance_m = max(0.0, _finite(distance_m, "distance_m"))
        if distance_m <= 600:
            return 1.02
        if distance_m <= 1800:
            return 1.02-(distance_m-600)/1200*0.09
        # Smooth asymptotic recovery above 1800 m, continuous at the join.
        return 0.93+0.04*(1-math.exp(-(distance_m-1800)/600))

    def _speed(self, dt: float) -> float:
        warm = 1.5+(self.base_speed_mps-1.5)*min(1, self.elapsed_s/12)
        fatigue = self.fatigue_factor(self.distance_m)
        curve = 0.92 if self.position["curvature"] >= self.CURVE_THRESHOLD else 1.0
        alpha = 1-math.exp(-dt/3.0)
        self._noise += alpha*(self._rng.gauss(0, 0.18)-self._noise)
        self._pace_factor += (self.lap_pace_factor-self._pace_factor)*(1-math.exp(-dt/5.0))
        return max(1.20, min(MAX_MOTION_SPEED_MPS, warm*fatigue*curve*self._pace_factor+self._noise))

    def _checkpoint_speed_factor(self) -> float:
        """Blend to 73% within the next unvisited checkpoint's 10 m disk."""
        target = self._detour_targets[0] if self._detour_targets else None
        if target is None and self._pending_detour is not None:
            targets = self._pending_detour["targets"]
            target = targets[0] if targets else None
        if target is None:
            return 1.0
        distance = haversine_distance(*_point(self.position), target["lat"], target["lon"])
        return 0.73+0.27*_smoothstep(distance/10.0)

    @staticmethod
    def _cosine(seconds: float, duration: float) -> float:
        return (1-math.cos(math.pi*max(0.0, min(1.0, seconds/duration))))/2

    def _lane_blend(self, seconds: float) -> float:
        if self._pause_phase == "slowing":
            return self._lane_start_blend+(1-self._lane_start_blend)*self._cosine(seconds, 5.5)
        if self._pause_phase == "WALKING_REST":
            return 1.0
        if self._pause_phase == "resuming":
            return self._lane_start_blend*(1-self._cosine(seconds, 8.0))
        return 0.0

    def _check_control_cooldown(self) -> None:
        if self._control_clock() < self._control_deadline:
            raise ControlCooldownError("wait for the 1.5 second control cooldown")

    def pause(self) -> dict[str, Any]:
        with self._lock:
            self._require_open()
            self._check_control_cooldown()
            if self.state not in (RunnerState.CRUISING_LOOP, RunnerState.CRUISING_DETOUR) or self._stop_pending or self._pause_phase != "running" or self.elapsed_s < self.TICK_SECONDS:
                raise RuntimeError("walking break is not available in the current phase")
            self._control_deadline = self._control_clock()+1.5
            self._pause_phase = "slowing"
            self._lane_start_blend = 0.0
            self._pause_speed = max(1.2, self.speed_mps)
            self._walk_center = self._rng.uniform(1.35, 1.55)
            self._walk_duration_s = self._rng.uniform(40.0, 59.0)
            self._phase_elapsed_s = self._current_rest_s = 0.0
            self._lateral.event = None
            self._lateral.next_event_m = self._lateral.distance_m+120.0
            self._event("walk_break_requested", duration_s=self._walk_duration_s)
            return self.snapshot()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            self._require_open()
            self._check_control_cooldown()
            if self.state not in (RunnerState.CRUISING_LOOP, RunnerState.CRUISING_DETOUR) or self._pause_phase != "WALKING_REST" or self._target_walking:
                raise RuntimeError("resume requires a temporary walking break")
            self._control_deadline = self._control_clock()+1.5
            self._pause_phase = "resuming"
            self._pause_speed = self.speed_mps
            self._phase_elapsed_s = 0.0
            self._lane_start_blend = 1.0
            self._event("walk_break_ended", duration_s=self._current_rest_s)
            return self.snapshot()

    def _begin_target_walk(self) -> None:
        """Latch a moving finish. Only explicit settlement ends this walk."""
        blend = self._lane_blend(self._phase_elapsed_s)
        self._target_walking = True
        self._stop_pending = True
        self._stop_reason = "target"
        self._pending_detour = self._exit_remaining = None
        self._lateral.event = None
        if self._pause_phase != "WALKING_REST":
            self._pause_phase = "slowing"
            self._pause_speed = max(1.2, self.speed_mps)
            self._lane_start_blend = blend
            self._phase_elapsed_s = 0.0
            self._walk_center = self._rng.uniform(1.35, 1.55)
        self._event("target_walk_started", distance_m=self.distance_m)

    def request_safe_stop(self, reason: str = "user") -> None:
        with self._lock:
            self._require_open()
            if self.state in (RunnerState.IDLE, RunnerState.SAFE_STOPPED):
                return
            self._stop_pending = True
            self._stop_reason = str(reason)
            if self.state == RunnerState.CRUISING_LOOP:
                self._pending_detour = self._exit_remaining = None
            if self.state == RunnerState.LOCKED_START:
                self._finish_stop()

    def hold_position(self) -> None:
        with self._lock:
            self._require_open()
            if (self.state not in (RunnerState.CRUISING_LOOP, RunnerState.CRUISING_DETOUR)
                    or not (self._target_walking or self._pause_phase != "running")):
                raise RuntimeError("stationary hold requires a walking break or a cool-down")
            self._stop_reason = "user_hold"
            self._finish_stop()

    def _finish_stop(self) -> None:
        self._stopped_at = time.monotonic()
        self._target_walking = False
        self._pause_phase = "running"
        self._pause_speed = 0.0
        self.speed_mps = self.stride_m = 0.0
        self._stop_pending = False
        self._pending_detour = None
        self._exit_remaining = None
        self._set_state(RunnerState.SAFE_STOPPED)
        self._event("settled", reason=self._stop_reason or "target", distance_m=self.distance_m,
                    elapsed_s=self.elapsed_s, targets_visited=list(self.targets_visited))
        # Hold the final simulated location until explicit release/close. Clearing
        # it here would restore real GPS before the UI can settle its session.

    def _draw_lap_factor(self, crossing_distance: float, previous_length: float) -> float:
        """Sample a lap pace factor without forcing a gap from the prior split."""
        return self._lap_rng.uniform(0.96, 1.04)

    def _next_lap(self, crossing_time: float, crossing_distance: float) -> None:
        next_lap = self._prepared_lap
        if next_lap is None:
            rng_state = self._geometry_rng.getstate()
            try:
                with _geometry_window(LIVE_GEOMETRY_SECONDS):
                    next_lap = self._make_lap()
            except GeometryBudgetError:
                self._geometry_rng.setstate(rng_state)
                next_lap = self._lap
                self.last_error = "route_too_complex"
                self._event("geometry_degraded", reason=self.last_error)
        duration = crossing_time-self._lap_start_time
        distance = crossing_distance-self._lap_start_distance
        if duration <= 0 or distance <= 0:
            raise ArithmeticError("lap statistics must describe forward motion")
        self.lap_splits.append(duration)
        self._lap_distances.append(distance)
        del self.lap_splits[:-self.LAP_HISTORY_LIMIT]
        del self._lap_distances[:-self.LAP_HISTORY_LIMIT]
        self.laps += 1
        self.current_lap = self.laps+1
        previous_length = self._lap.length
        previous_endpoint = self._lap.points[-1]
        self._lap = next_lap
        self._prepared_lap = None
        if haversine_distance(*previous_endpoint, *self._lap.points[0]) > 0.001:
            raise ArithmeticError("reconstructed lap seam is discontinuous")
        self._loop_s = 0.0
        self._lap_start_time, self._lap_start_distance = crossing_time, crossing_distance
        self.lap_pace_factor = self._draw_lap_factor(crossing_distance, previous_length)
        self._event("lap", laps=self.laps, current_lap=self.current_lap, lap_time_s=duration,
                    lap_distance_m=distance, offset_m=self.offset_m, pace_factor=self.lap_pace_factor)
        if self._pending_detour is not None:
            # Perturbation changes both node count and the closest exit/entry.
            draft = self._pending_detour["draft"]
            self.exit_idx = self._lap.nearest_index(draft[0]) % (len(self._lap.points)-1)
            self.entry_idx = self._lap.nearest_index(draft[-1]) % (len(self._lap.points)-1)
            self._exit_remaining = self._lap.cumulative[self.exit_idx]
            try:
                with _geometry_window(LIVE_GEOMETRY_SECONDS):
                    route, targets = self._build_detour(draft, self.exit_idx, self.entry_idx)
            except ValueError as exc:
                self._pending_detour = self._exit_remaining = None
                self.exit_idx = self.entry_idx = None
                self.last_error = "route_too_complex" if isinstance(exc, GeometryBudgetError) else "detour_crosses_infield"
                self._event("detour_rejected", reason=self.last_error)
                return
            self._pending_detour.update(route=route, targets=targets)

    def _loop_position(self, advance_m: float, physical_m: float, speed: float) -> tuple[dict, float]:
        node = self._lap.sample(self._loop_s+advance_m)
        gate = _smootherstep((self._lateral.distance_m+physical_m-self._lane_resume_m)/25.0)
        offset = self._lateral.sample(physical_m, physical_m/speed)*gate*self._lateral_scale
        seconds = self._phase_elapsed_s+(self._lateral.time_s-self._tick_lateral_time)+physical_m/speed
        blend = self._lane_blend(seconds)
        # +1.8 m is only feasible on a sufficiently wide calibrated corridor.
        # A narrow track must never be forced outside its usable boundary.
        outer = max(0.0, min(1.8, self.track_width_m/2-0.1-self._geometry_limit_m-0.05))
        offset = offset*(1-blend)+outer*blend
        # Keep forward motion feasible even for pathological low-speed samples.
        # Ordinary OU/overtaking motion remains below this kinematic bound.
        limit = physical_m*0.75
        offset = max(self.offset_m-limit, min(self.offset_m+limit, offset))
        node["lat"], node["lon"] = _destination(_point(node), node["bearing"]+90, offset)
        return node, offset

    def _move_loop(self, budget: float, speed: float) -> tuple[float, float, dict, float]:
        """Invert the offset curve in <=0.25 m physical chords, not index steps.

        At a fixed physical distance q, dt=q/v is known. Bisect centre-line
        advance u until Haversine(P(s+u,OU(q,dt)),P_previous)=q. If the lap/exit
        boundary comes first, bisect q instead. Reported mileage is thus the
        actual emitted path's length, including lateral movement.
        """
        q = min(budget, 0.25, self._lateral.boundary_m())
        upper = min(0.5, self._lap.length-self._loop_s)
        if self._pending_detour is not None:
            upper = min(upper, self._exit_remaining)
        origin = _point(self.position)
        node, offset = self._loop_position(upper, q, speed)
        if haversine_distance(*origin, *_point(node)) >= q:
            low, high = 0.0, upper
            for _ in range(26):
                middle = (low+high)/2
                trial, _ = self._loop_position(middle, q, speed)
                if haversine_distance(*origin, *_point(trial)) < q:
                    low = middle
                else:
                    high = middle
            advance = (low+high)/2
        else:
            advance = upper
            low, high = 0.0, q
            for _ in range(26):
                middle = (low+high)/2
                trial, _ = self._loop_position(advance, middle, speed)
                if haversine_distance(*origin, *_point(trial)) > middle:
                    low = middle
                else:
                    high = middle
            q = (low+high)/2
        node, offset = self._loop_position(advance, q, speed)
        moved = haversine_distance(*origin, *_point(node))
        if moved > budget+1e-5 or not math.isfinite(moved):
            raise ArithmeticError("offset-curve integration exceeded its physical budget")
        if moved > EPS_M:
            node["bearing"] = calculate_bearing(*origin, *_point(node))
        return min(moved, budget), advance, node, offset

    def _advance(self, budget: float, speed: Optional[float] = None) -> tuple[float, bool]:
        travelled, critical = 0.0, False
        speed = max(0.8, self.speed_mps or self.base_speed_mps) if speed is None else speed
        for _ in range(4096):
            if budget <= EPS_M:
                break
            if self._connector is not None:
                move = min(budget, self._connector.length-self._connector_s)
                self._connector_s += move
                self.position = self._connector.sample(self._connector_s)
                if self._connector_s >= self._connector.length-EPS_M:
                    self._connector = None
                    self._lap_start_time = self.elapsed_s+(travelled+move)/speed
                    self._lap_start_distance = self.distance_m+travelled+move
            elif self.state == RunnerState.CRUISING_LOOP:
                if self._pending_detour is not None and self._exit_remaining <= EPS_M:
                    pending = self._pending_detour
                    # The exit is approached on the loop. Only now is its exact
                    # moving lane position known; rebuild the tangent lead here.
                    try:
                        with _geometry_window(LIVE_GEOMETRY_SECONDS):
                            route, targets = self._build_detour(pending["draft"], self.exit_idx, self.entry_idx,
                                                                exit_position=self.position)
                    except ValueError as exc:
                        # A live lane/tangent may make a previously valid lead
                        # unsafe. Keep following the loop and reject the branch.
                        self._pending_detour = self._exit_remaining = None
                        self.exit_idx = self.entry_idx = None
                        self.last_error = "route_too_complex" if isinstance(exc, GeometryBudgetError) else "detour_crosses_infield"
                        self._event("detour_rejected", reason=self.last_error)
                        continue
                    self._detour, self._detour_targets = route, deque(targets)
                    self.entry_idx = self._detour.entry_idx
                    self._detour_s = 0.0
                    self._pending_detour = None
                    self._exit_remaining = None
                    self._set_state(RunnerState.CRUISING_DETOUR)
                    continue
                if self._lap_start_time is None:
                    self._lap_start_time = self.elapsed_s+travelled/speed
                    self._lap_start_distance = self.distance_m+travelled
                if self._lateral.boundary_m() <= EPS_M:
                    self._lateral.advance(0.0, 0.0)
                    continue
                move, advance, node, self.offset_m = self._move_loop(budget, speed)
                self._loop_s += advance
                if self._pending_detour is not None:
                    self._exit_remaining = max(0.0, self._exit_remaining-advance)
                self.position = node
                for transition in self._lateral.advance(move, move/speed):
                    self._event(transition, loop_distance_m=self._lateral.distance_m)
                if self._loop_s >= self._lap.length-EPS_M:
                    self._next_lap(self.elapsed_s+(travelled+move)/speed, self.distance_m+travelled+move)
            else:
                next_target = self._detour_targets[0] if self._detour_targets else None
                limit = next_target["s_m"] if next_target is not None else self._detour.length
                move = min(budget, max(0.0, limit-self._detour_s))
                reached = next_target is not None and self._detour_s+move >= limit-EPS_M
                if reached:
                    # Stay on the approach curve, 2–8 cm before the exact
                    # anchor. Bound by remaining travel so jitter never backs
                    # up or creates a displacement outside this tick's budget.
                    jitter = min(self._checkpoint_rng.uniform(0.02, 0.08), move*0.25)
                    move = max(0.0, move-jitter)
                self._detour_s += move
                self.position = self._detour.sample(self._detour_s)
                if reached:
                    # Co-located target IDs share one physical delivery; do not
                    # generate repeated stationary ticks for duplicate anchors.
                    while self._detour_targets and self._detour_targets[0]["s_m"] <= limit+EPS_M:
                        target = self._detour_targets.popleft()
                        self.targets_visited.append(target["id"])
                        self.targets_visited_total += 1
                        del self.targets_visited[:-self.TARGET_HISTORY_LIMIT]
                        self._event("target_reached", target_id=target["id"], lat=target["lat"], lon=target["lon"],
                                    sample_lat=self.position["lat"], sample_lon=self.position["lon"])
                    # Send this checkpoint as the tick endpoint, even for a
                    # zero-length branch. Discard leftover travel, never catch
                    # it up on the next tick or merge before delivery is queued.
                    travelled += move
                    critical = True
                    break
                if self._detour_s >= self._detour.length-EPS_M and not self._detour_targets:
                    self._loop_s = self._lap.cumulative[self.entry_idx]
                    self.position = self._lap.sample(self._loop_s)
                    self.offset_m = 0.0
                    self._lane_resume_m = self._lateral.distance_m
                    self._detour = None
                    self._set_state(RunnerState.CRUISING_LOOP)
                    self._event("detour_completed", entry_idx=self.entry_idx)
            travelled += move
            budget -= move
        else:
            raise ArithmeticError("motion exceeded transition budget")
        return travelled, critical

    def _tick(self, dt: float) -> None:
        if not self.bridge.force_sandbox:
            dt = min(dt, MAX_MOTION_STEP_S)
        self._prepare_next_lap(dt)
        with self._lock:
            if self.state in (RunnerState.IDLE, RunnerState.SAFE_STOPPED):
                return
            if self.state == RunnerState.LOCKED_START:
                if not self.bridge.snapshot()["speed_fuse_tripped"] and self.bridge.motion_delay() <= 1e-6:
                    self.bridge.set_location(self.position["lat"], self.position["lon"])
                return
            on_loop = self.state == RunnerState.CRUISING_LOOP and self._pending_detour is None
            if self._stop_pending and on_loop and self._stop_reason != "target":
                self._finish_stop()
                return
            if self.bridge.snapshot()["speed_fuse_tripped"]:
                # Preserve position, effective time, mileage and model state
                # until the bridge's explicit restoration releases the latch.
                return
            if self.bridge.motion_delay() > 1e-6:
                # No speculative motion while the preceding point is pending
                # (including checkpoints, reconnects and synchronous timeouts).
                # Do not advance mileage, phase timers, trace or RNG state.
                return
            if on_loop and not self._target_walking and self.target_m-self.distance_m <= 12.0:
                self._begin_target_walk()
            if not self._target_walking and self._pause_phase == "WALKING_REST" and self._phase_elapsed_s >= 40.0 and self._phase_elapsed_s+dt/2 >= self._walk_duration_s:
                self.resume()
            self._tick_lateral_time = self._lateral.time_s
            normal_speed = self._speed(dt)
            requested_speed = normal_speed
            phase = self._pause_phase
            midpoint = self._phase_elapsed_s+dt/2
            walking_speed = self._walk_center+0.12*math.sin(2*math.pi*(self.elapsed_s+dt/2)/6.0)
            if phase == "slowing":
                blend = self._cosine(midpoint, 5.5)
                requested_speed = self._pause_speed*(1-blend)+walking_speed*blend
            elif phase == "WALKING_REST":
                requested_speed = walking_speed
            elif phase == "resuming":
                blend = self._cosine(midpoint, 8.0)
                requested_speed = self._pause_speed*(1-blend)+normal_speed*blend
            checkpoint_factor = self._checkpoint_speed_factor()
            requested_speed *= checkpoint_factor
            floor = 0.8 if checkpoint_factor < 1.0 else 1.20
            requested_speed = max(floor, min(MAX_MOTION_SPEED_MPS, requested_speed))
            # Slew limit all cruising commands, including button transitions.
            # Checkpoint arrival can clip the actual distance below the command;
            # recovery starts from that actual speed, never the discarded budget.
            # The first moving sample establishes the initial cruise baseline;
            # the separate locked-start and terminal states may have speed 0.
            previous = self.speed_mps if self.elapsed_s > 0 else requested_speed
            speed = max(previous-0.55*dt, min(previous+0.55*dt, requested_speed))
            active_dt = dt
            if phase != "running":
                self._lateral.event = None
                self._lateral.next_event_m = self._lateral.distance_m+120.0
            # The mileage goal is a notification threshold, not an endpoint.
            # Every subsequent sample keeps following the route until settlement.
            budget = speed*dt
            moved, critical = self._advance(budget, speed)
            self.distance_m += moved
            self.elapsed_s += active_dt
            self.speed_mps = moved/active_dt
            self.stride_m = self.speed_mps/(192/60) if self.profile == "high_cadence" else 0.0
            self.bridge.set_location(self.position["lat"], self.position["lon"], critical=critical)
            self._phase_elapsed_s += active_dt
            if phase == "WALKING_REST":
                self._current_rest_s += active_dt
                self._rest_total_s += active_dt
            if phase == "slowing" and self._phase_elapsed_s >= 5.5:
                self._pause_phase = "WALKING_REST"
                self._phase_elapsed_s = 0.0
                self._event("walking_rest_started")
            elif phase == "resuming" and self._phase_elapsed_s >= 8.0:
                self._pause_phase = "running"
                self._phase_elapsed_s = 0.0
            self._record_trace()
            if self.distance_m >= self.target_m-EPS_M:
                self._stop_pending = True
                self._stop_reason = self._stop_reason or "target"
            self._event("position", **self.snapshot())

    def step(self, dt: float = MOTION_INTERVAL_S) -> dict[str, Any]:
        """Deterministic simulation hook; disallowed alongside the worker thread."""
        dt = _finite(dt, "dt")
        if not 0 < dt <= self.MAX_MANUAL_STEP_SECONDS:
            raise ValueError("manual dt must be in (0,1.9]")
        if self.is_alive():
            raise RuntimeError("manual stepping is disabled while background thread runs")
        self._require_open()
        self._tick(dt)
        return self.snapshot()

    def run(self) -> None:
        previous = time.monotonic()
        deadline = previous+self.TICK_SECONDS
        while not self._shutdown.wait(max(0, deadline-time.monotonic())):
            now = time.monotonic()
            delay = self.bridge.motion_delay()
            if (delay > 1e-6 and not self.bridge.snapshot()["speed_fuse_tripped"] and
                    not (self._stop_pending and self._stop_reason != "target")):
                # Follow delivery completion rather than losing every other
                # fixed-period tick to backpressure. The next step is still
                # capped; elapsed wall time is never replayed as a large jump.
                deadline = now+delay
                continue
            elapsed = now-previous
            previous = now
            if elapsed > self.MAX_CATCHUP_SECONDS:
                self._event("timing_gap", missed_seconds=elapsed)
                # Discard excess wall time; do not generate a two-tick jump.
                dt = self.TICK_SECONDS if elapsed > 2*self.TICK_SECONDS+1e-6 else self.MAX_CATCHUP_SECONDS
            else:
                dt = max(1e-4, elapsed)
            with self._lock:
                if self._active_since is not None:
                    dt = min(dt, max(1e-4, now-self._active_since))
            try:
                self._tick(dt)
            except Exception as exc:
                LOGGER.exception("motion tick failed")
                with self._lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self._stop_reason = "engine_error"
                    self._finish_stop()
                    self._event("error", message=self.last_error)
            if not self.bridge.force_sandbox and not self.bridge.snapshot()["speed_fuse_tripped"]:
                # Queue the next point ahead of its wire rate slot when the
                # previous send has completed. This also prevents an unchanged
                # heartbeat from repeatedly winning that slot over motion.
                deadline = max(previous+self.TICK_SECONDS, time.monotonic()+self.bridge.motion_delay())
            else:
                deadline += self.TICK_SECONDS
                if deadline <= time.monotonic():
                    deadline = time.monotonic()+self.TICK_SECONDS

    def _record_trace(self) -> None:
        if self.position is None:
            return
        self._trace_sequence += 1
        self._trace.append({"sequence": self._trace_sequence, "lat": self.position["lat"],
                            "lon": self.position["lon"], "elapsed_s": self.elapsed_s,
                            "distance_m": self.distance_m, "lap": self.current_lap,
                            "session_elapsed_s": self.elapsed_s, "pause_phase": self._pause_phase})

    def trace_since(self, after_sequence: int = 0, session_id: Optional[str] = None) -> dict[str, Any]:
        """Bounded delta feed; stale cursors and new sessions receive a reset."""
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("trace cursor must be a nonnegative integer")
        if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 64):
            raise ValueError("invalid trace session")
        with self._lock:
            oldest = self._trace[0]["sequence"] if self._trace else self._trace_sequence+1
            reset = session_id != self.session_id or after_sequence < oldest-1 or after_sequence > self._trace_sequence
            points = [dict(point) for point in self._trace if reset or point["sequence"] > after_sequence]
            return {"session_id": self.session_id, "sequence": self._trace_sequence, "reset": reset,
                    "points": points, "limit": self.TRACE_LIMIT}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            last_lap = None if not self.lap_splits else {
                "duration_s": self.lap_splits[-1], "distance_m": self._lap_distances[-1],
                "pace_s_per_km": self.lap_splits[-1]*1000/self._lap_distances[-1]}
            return {"state": self.state.value, "position": dict(self.position) if self.position else None,
                    "distance_m": self.distance_m, "elapsed_s": self.elapsed_s, "speed_mps": self.speed_mps,
                    "stride_m": self.stride_m, "base_speed_mps": self.base_speed_mps, "profile": self.profile,
                    "requested_target_m": self.requested_target_m, "target_m": self.target_m,
                    "laps": self.laps, "current_lap": self.current_lap, "lap_splits": list(self.lap_splits),
                    "lap_splits_start": max(0, self.laps-len(self.lap_splits)),
                    "last_lap": last_lap, "lap_pace_factor": self.lap_pace_factor,
                    "offset_m": self.offset_m, "lateral_offset": self.offset_m,
                    "track_width_m": self.track_width_m,
                    "overtaking": self._lateral.event is not None if self._lateral else False,
                    "session_id": self.session_id, "trace_sequence": self._trace_sequence,
                    "trace_count": len(self._trace), "exit_idx": self.exit_idx,
                    "entry_idx": self.entry_idx, "stop_pending": self._stop_pending,
                    "finishing": self._target_walking, "target_walking": self._target_walking,
                    "target_reached": self.distance_m >= self.target_m-EPS_M, "stop_reason": self._stop_reason,
                    "pause_phase": self._pause_phase, "rest_elapsed_s": self._current_rest_s,
                    "control_cooldown_s": max(0.0, self._control_deadline-self._control_clock()),
                    "walk_duration_s": self._walk_duration_s, "walk_center_mps": self._walk_center,
                    "rest_total_s": self._rest_total_s, "session_elapsed_s": self.elapsed_s,
                    "hold_elapsed_s": (self._hold_elapsed_s if self._closed else
                                       max(0.0, time.monotonic()-self._stopped_at) if self._stopped_at is not None else 0.0),
                    "targets_visited": list(self.targets_visited), "targets_visited_total": self.targets_visited_total,
                    "last_error": self.last_error,
                    "dropped_events": self.dropped_events, "device": self.bridge.snapshot()}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._stopped_at is not None:
                self._hold_elapsed_s = max(0.0, time.monotonic()-self._stopped_at)
            self._closed = True
            self._shutdown.set()
            if self.state not in (RunnerState.IDLE, RunnerState.SAFE_STOPPED):
                self._stop_reason = "shutdown"
                self._finish_stop()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3.0)
        if self._owns_bridge:
            self.bridge.close()
        atexit.unregister(self.close)


class BridgeMode(str, Enum):
    SANDBOX_MODE = "SANDBOX_MODE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    CLOSED = "CLOSED"


class DeviceConnectionError(ConnectionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _diagnostic_text(value: Any, limit: int = 1600) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return redact_diagnostics("".join(char for char in text if char in "\n\t" or char.isprintable()))[-limit:]


def _device_error_code(error: Exception, default: str = "device_connection_failed") -> str:
    code = getattr(error, "code", None)
    if code and code != "tunnel_unavailable":
        return code
    details = (type(error).__name__ + " " + str(error)).casefold()
    if any(part in details for part in ("notpairederror", "not paired", "userdeniedpairing",
                                       "pairingdialogresponsepending", "invalidhostid", "trust this computer")):
        return "device_trust_required"
    if "developermodeisnotenabled" in details or "developer mode" in details and any(
            part in details for part in ("disabled", "not enabled", "enable developer mode", "must be enabled")):
        return "device_developer_mode_required"
    if any(part in details for part in ("accessdeniederror", "access is denied", "requires root/admin",
                                       "requires administrator", "winerror 5]", "winerror 740]")):
        return "tunnel_admin_required"
    return code or default


_standalone_driver = DaemonDriverExecutor()
# A closed bridge retains its device until all actual work ends. A permanently
# blocked native call keeps the lease until the OS releases it at process exit.
_quarantined_device_leases = []


async def _invoke(function: Callable, *args: Any, _sync_calls: Optional[set] = None,
                  _executor: Optional[DaemonDriverExecutor] = None, **kwargs: Any) -> Any:
    """Accommodate both the current async and older synchronous PMD APIs."""
    if inspect.iscoroutinefunction(function):
        result = function(*args, **kwargs)
    else:
        # Keep the REAL concurrent Future, not a cancelable asyncio Task: loop
        # cancellation must never turn an unfinished native write into "done".
        work = (_executor or _standalone_driver).submit(function, *args, **kwargs)
        if _sync_calls is not None:
            _sync_calls.add(work)
        waiting = asyncio.wrap_future(work)
        waiting.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            result = await asyncio.shield(waiting)
        finally:
            if _sync_calls is not None and work.done():
                _sync_calls.discard(work)
    return await result if inspect.isawaitable(result) else result


class DirtySessionMarker:
    """Durable intent written BEFORE injection; only a confirmed clear removes it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.record = None
        self.invalid = False
        try:
            if self.path.stat().st_size > 4096:
                raise ValueError("oversized location-session marker")
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("active") is not True
                    or not isinstance(record.get("udid"), str) or not 1 <= len(record["udid"]) <= 128
                    or not isinstance(record.get("session_id"), str) or not record["session_id"]):
                raise ValueError("invalid location-session marker")
            self.record = record
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.invalid = True
            LOGGER.error("location-session marker is unreadable; recovery requires attention", exc_info=True)

    @property
    def pending(self) -> bool:
        with self._lock:
            return self.invalid or self.record is not None

    @property
    def udid(self) -> Optional[str]:
        with self._lock:
            return self.record["udid"] if self.record is not None else None

    def _write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent),
                                             prefix=".piggy-location-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(record, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def mark(self, udid: str) -> None:
        with self._lock, ProcessFileLock(self.path.with_suffix(".json.lock")):
            if self.pending or self.path.exists():
                raise RuntimeError("an earlier location session requires recovery")
            if not isinstance(udid, str) or not 1 <= len(udid) <= 128:
                raise ValueError("a device UDID is required before location injection")
            record = {"version": 1, "active": True, "udid": udid,
                      "session_id": uuid.uuid4().hex, "started_at": time.time()}
            self._write(record)
            self.record = record

    def cleared(self) -> None:
        with self._lock, ProcessFileLock(self.path.with_suffix(".json.lock")):
            self._assert_current()
            if self.invalid:
                raise RuntimeError("cannot clear an unidentified location session")
            if self.record is not None:
                # Do not delete a newer session written by another app instance.
                current = json.loads(self.path.read_text(encoding="utf-8"))
                if current.get("session_id") != self.record["session_id"]:
                    raise RuntimeError("location-session marker changed during recovery")
                self.path.unlink()
                self.record = None

    def _assert_current(self) -> None:
        if self.invalid:
            raise RuntimeError("cannot clear an unidentified location session")
        if self.path.exists():
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if self.record is None or current.get("session_id") != self.record["session_id"]:
                raise RuntimeError("location-session marker changed during recovery")
        elif self.record is not None:
            raise RuntimeError("location-session marker disappeared during recovery")

    def assert_current(self) -> None:
        """Validate ownership BEFORE the device-side clear, not just deletion."""
        with self._lock, ProcessFileLock(self.path.with_suffix(".json.lock")):
            self._assert_current()

    def failed(self, reason: str) -> None:
        with self._lock, ProcessFileLock(self.path.with_suffix(".json.lock")):
            if self.record is not None:
                current = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(current, dict) or current.get("session_id") != self.record["session_id"]:
                    LOGGER.error("location-session marker changed; failure record was not overwritten")
                    return
                record = {**self.record, "last_clear_succeeded": False,
                          "last_clear_attempt_at": time.time(), "last_clear_error": reason[:512]}
                self._write(record)
                self.record = record


class IOSDeviceBridge:
    """Owns one device, async loop, optional tunneld child and bounded mailbox.

    PreferredRsdTunnel maintains an RSD channel in this daemon thread. If that
    API is absent/unavailable, a loopback tunneld is discovered or started without
    interactive elevation. Lack of permissions/drivers/trust/DDI becomes Sandbox
    with an explicit reason. Never kills a tunneld that this instance did not own.

    A dropped session gets one quick reconnect. Failed connection attempts
    back off to avoid repeatedly opening USB and RSD services. Motion never
    blocks on these operations.

    Adjacent samples are queued while connected. Critical samples and the
    approach samples needed to reach them survive USB loss until acknowledged.
    Recovery retains the FIFO: even ordinary points may be necessary to reach
    the current point without skipping a legal physical step.
    Every recovery point must pass the speed fuse against the last delivery.
    Lost physical deliveries are counted separately from simulated crossings.
    Test/sandbox movement and device delivery are intentionally observable apart.
    """

    RETRY_SECONDS = 0.2
    DISCOVERY_SECONDS = 2.0
    MAX_DISCOVERY_SECONDS = 8.0
    TUNNEL_POLL_SECONDS = 0.5

    def __init__(self, udid: Optional[str] = None, *, sandbox: bool = False,
                 auto_start_tunneld: bool = True, connect_timeout: Optional[float] = None,
                 send_timeout: Optional[float] = None, mount_timeout: float = 180.0,
                 low_battery_percent: float = 20.0,
                 tunneld_command: Optional[Sequence[str]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 dirty_marker: Optional[DirtySessionMarker] = None):
        self._dirty_marker = dirty_marker
        self._requested_udid = udid
        if dirty_marker is not None and dirty_marker.pending and udid not in (None, dirty_marker.udid):
            raise ValueError("recovery must target the device in the dirty-session marker")
        self.udid = dirty_marker.udid if dirty_marker is not None and dirty_marker.pending else udid
        self._recovery_required = bool(dirty_marker is not None and dirty_marker.pending)
        self._location_dirty = self._recovery_required
        self._wire_clock = clock or time.monotonic
        self._last_send_at = None
        self._last_send_completed_at = None
        self._last_send_position = None
        self._send_lock = None
        self._sending = False
        self._pending_sync_calls = set()
        self._driver_executor = DaemonDriverExecutor()
        self._device_lease = None
        self._leased_udid = None
        self._retired_device_lease = None
        self._worker_finished = False
        self._fuse_tripped = False
        self._send_late_count = 0
        self._max_send_gap_s = 0.0
        self._battery_task = None
        self._dvt_disconnect_task = None
        self.force_sandbox = bool(sandbox)
        self.auto_start_tunneld = bool(auto_start_tunneld)
        if tunneld_command is not None and (
                isinstance(tunneld_command, (str, bytes)) or not tunneld_command or
                any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in tunneld_command)):
            raise ValueError("tunneld_command must be a nonempty sequence of argument strings")
        self._tunneld_command = list(tunneld_command) if tunneld_command is not None else None
        windows = sys.platform == "win32"
        self.connect_timeout = _finite(connect_timeout if connect_timeout is not None else (30.0 if windows else 12.0),
                                       "connect_timeout")
        self.send_timeout = _finite(send_timeout if send_timeout is not None else (5.0 if windows else 1.5),
                                    "send_timeout")
        self.mount_timeout = _finite(mount_timeout, "mount_timeout")
        self.low_battery_percent = _finite(low_battery_percent, "low_battery_percent")
        if min(self.connect_timeout, self.send_timeout, self.mount_timeout) <= 0 or not 0 <= self.low_battery_percent <= 100:
            raise ValueError("timeouts must be positive and battery threshold in [0,100]")
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None
        self._open_task = None
        self._retry_wakeup = None
        self._stack = None
        self._simulator = None
        self._provider = None
        self._latest = None
        self._sequence = 0
        self._sent_sequence = 0
        self._critical = deque(maxlen=128)
        self._critical_sequences = set()
        # One emergency checkpoint slot preserves a full, indispensable
        # approach chain without silently evicting its head. Motion is blocked
        # until this slot and the FIFO have drained; it cannot grow unbounded.
        self._deferred_critical = None
        self._children = []
        self._tunnel_log = deque(maxlen=20)
        self._tunnel_readers = []
        self._last_spawn = -math.inf
        self._tunneld_in_use = False
        self._ever_connected = False
        self._closed = False
        self._closing = False
        self._close_deadline = None
        self._close_ack = threading.Event()
        self.mode = BridgeMode.SANDBOX_MODE
        self.reason = "explicit sandbox" if sandbox else "device not yet probed"
        self.last_error = None
        self._device_error_code = None
        self.ios_version = None
        self.battery_percent = None
        self.battery_charging = None
        self.low_battery = None
        self.battery_error = None
        self.reconnect_attempts = 0
        self.physical_sent = 0
        self.sandbox_updates = 0
        self.dropped_device_samples = 0
        self.last_delivered_position = None
        self._clear_requested = False
        self._clear_future = None
        self._clear_deadline = 0.0
        self._clear_ack = threading.Event()
        self._last_clear_succeeded = False
        atexit.register(self.close)

    def connect(self) -> dict[str, Any]:
        """Start discovery asynchronously; return immediately for frontend use."""
        with self._lock:
            if self._closed or (self._closing and not self._clear_requested):
                raise RuntimeError("bridge is closed")
            if self._recovery_required and not self._clear_requested:
                raise RuntimeError("the previous location session must be cleared first")
            if not self.force_sandbox and (self._thread is None or not self._thread.is_alive()):
                self._worker_finished = False
                self._thread = threading.Thread(target=self._worker, name="PiggyIOSBridge", daemon=True)
                self._thread.start()
            return self.snapshot()

    def set_location(self, lat: float, lon: float, *, critical: bool = False) -> int:
        lat, lon = _latlon(lat, lon)
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("bridge is closed")
            if self._recovery_required:
                raise RuntimeError("the previous location session must be cleared first")
            if self._clear_requested:
                raise RuntimeError("cannot inject while a location reset is pending")
            self._sequence += 1
            sample = (self._sequence, lat, lon)
            self._latest = sample
            if self.force_sandbox:
                self.sandbox_updates += 1
                return self._sequence
            # Retain ordinary samples as well as checkpoints. A latest-only
            # mailbox can skip a moving point when sender/producer clocks drift,
            # turning healthy jitter into a two-tick displacement.
            reserved = False
            if critical and not self._fuse_tripped and self._deferred_critical is None and len(self._critical) == self._critical.maxlen:
                if not self._compact_ordinary(sample):
                    # An all-critical queue is still a hard overflow. Otherwise
                    # retain every indispensable predecessor and the checkpoint
                    # in a single reserved tail slot, rather than skipping one.
                    if any(item[0] not in self._critical_sequences for item in self._critical):
                        self._deferred_critical = sample
                        self._critical_sequences.add(sample[0])
                        reserved = True
            if not reserved:
                if self._fuse_tripped or self._deferred_critical is not None or len(self._critical) == self._critical.maxlen:
                    self.dropped_device_samples += 1
                    if not self._fuse_tripped:
                        self._fuse_tripped = True
                        self.last_error = "coordinate_backlog_overflow"
                        self.reason = "coordinate backlog exceeded its bound"
                        LOGGER.error("coordinate_backlog_overflow: location injection latched")
                else:
                    self._critical.append(sample)
                    if critical:
                        self._critical_sequences.add(sample[0])
            if self.mode == BridgeMode.SANDBOX_MODE:
                self.sandbox_updates += 1
            self.connect()
            return self._sequence

    def _compact_ordinary(self, following: tuple[int, float, float]) -> bool:
        """Remove one redundant interior point only if its new edge is legal.

        Caller holds _lock. Never touch the queue head: the sender may already
        have peeked it. Never touch a checkpoint, including one in flight.
        """
        samples = list(self._critical)
        for index in range(1, len(samples)):
            current = samples[index]
            if current[0] in self._critical_sequences:
                continue
            previous = samples[index-1]
            successor = samples[index+1] if index+1 < len(samples) else following
            distance = haversine_distance(*previous[1:], *successor[1:])
            if distance <= MAX_SAMPLE_DISTANCE_M+DISTANCE_TOLERANCE_M:
                del self._critical[index]
                self.dropped_device_samples += 1
                return True
        return False

    def motion_delay(self) -> float:
        """Nonblocking producer backpressure; sandbox numerical tests stay free.

        A real-device producer gets at most one outstanding moving sample.
        One successor may wait in _send's rate slot after delivery completes.
        Waiting for that slot *before* queueing would let idle heartbeats race
        and starve the producer. Only the sender enforces wire-time pacing.
        """
        with self._lock:
            if self.force_sandbox:
                return 0.0
            if (self._critical or self._deferred_critical is not None or self._sending or
                    self._pending_sync_calls or self._clear_requested or self._recovery_required or
                    self._closed or self._closing):
                return 0.02
            return 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"mode": self.mode.value, "reason": self.reason, "last_error": self.last_error,
                    "error_code": self._device_error_code,
                    "udid": self.udid, "ios_version": self.ios_version, "battery_percent": self.battery_percent,
                    "battery_charging": self.battery_charging, "low_battery": self.low_battery,
                    "battery_error": self.battery_error, "reconnect_attempts": self.reconnect_attempts,
                    "physical_sent": self.physical_sent, "sandbox_updates": self.sandbox_updates,
                    "dropped_device_samples": self.dropped_device_samples,
                    "speed_fuse_tripped": self._fuse_tripped, "send_late_count": self._send_late_count,
                    "max_send_gap_s": self._max_send_gap_s,
                    "send_age_s": None if self._last_send_at is None else max(0.0, self._wire_clock()-self._last_send_at),
                    "queued_sequence": self._sequence, "delivered_sequence": self._sent_sequence,
                    "pending_samples": len(self._critical)+int(self._deferred_critical is not None),
                    "pending_critical_samples": len(self._critical_sequences),
                    "send_in_progress": self._sending,
                    "motion_backpressured": self.motion_delay() > 1e-6,
                    "location_dirty": self._location_dirty, "last_clear_succeeded": self._last_clear_succeeded,
                    "last_delivered_position": dict(self.last_delivered_position) if self.last_delivered_position else None}

    async def _close_resource(self, resource: Any) -> None:
        if resource is None:
            return
        try:
            if hasattr(resource, "__aexit__"):
                await _invoke(resource.__aexit__, None, None, None, _executor=self._driver_executor)
            elif hasattr(resource, "__exit__"):
                await _invoke(resource.__exit__, None, None, None, _executor=self._driver_executor)
            else:
                closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
                if closer is not None:
                    await _invoke(closer, _executor=self._driver_executor)
        except Exception:
            LOGGER.debug("device resource cleanup failed", exc_info=True)

    async def _enter_resource(self, stack: AsyncExitStack, resource: Any) -> Any:
        # Register BEFORE opening so cancellation/partial initialization is cleaned.
        stack.push_async_callback(self._close_resource, resource)
        if hasattr(resource, "__aenter__"):
            return await resource.__aenter__()
        if hasattr(resource, "__enter__"):
            return await _invoke(resource.__enter__, _executor=self._driver_executor)
        return resource

    @staticmethod
    def _read_tunneld() -> dict[str, Any]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:49151/", timeout=0.4) as response:
            payload = response.read(1024*1024+1)
        if len(payload) > 1024*1024:
            raise ValueError("oversized tunneld discovery response")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError("invalid tunneld discovery response")
        return result

    def _spawn_tunneld(self) -> None:
        """Only launch our own daemon; never request sudo or alter driver state."""
        if not self.auto_start_tunneld or self._shutdown.is_set():
            return
        now = time.monotonic()
        if now-self._last_spawn < 15:
            return
        with self._lock:
            if self._closed or self._shutdown.is_set():
                return
            for child in self._children:
                if child.poll() is not None:
                    self._terminate_child(child)
            self._children = [child for child in self._children if child.poll() is None]
            if self._children:
                return
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(Path(__file__).with_name("main.py")))
            command += ["--parent-pid", str(os.getpid())]
            if self._tunneld_command is not None:
                command += ["--piggy-tunnel-command", *self._tunneld_command]
            else:
                command += ["--piggy-tunneld"]
            self._last_spawn = now
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                      "stderr": subprocess.STDOUT, "shell": False}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            command += ["--host", "127.0.0.1", "--port", "49151", "--protocol", "tcp"]
            child = subprocess.Popen(command, **kwargs)
            if isinstance(getattr(child, "pid", None), int):
                child._piggy_managed = True
            self._children.append(child)
            self._tunnel_log = output = deque(maxlen=20)
            self._tunnel_readers = [thread for thread in self._tunnel_readers if thread.is_alive()]
            if getattr(child, "stdout", None) is not None:
                reader = threading.Thread(target=self._capture_tunnel_output, args=(child.stdout, output),
                                          name="PiggyTunnelOutput", daemon=True)
                self._tunnel_readers.append(reader)
                reader.start()

    def _capture_tunnel_output(self, stream: Any, output: deque) -> None:
        try:
            while True:
                chunk = stream.readline(512)
                if not chunk:
                    break
                text = _diagnostic_text(chunk.decode("utf-8", errors="replace"), 512).strip()
                if text:
                    with self._lock:
                        output.append(text)
        except (OSError, ValueError):
            LOGGER.debug("tunnel output reader stopped", exc_info=True)
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def _tunnel_failure(self, cause: Any) -> str:
        with self._lock:
            statuses = [child.poll() for child in self._children]
            output = "\n".join(self._tunnel_log)
        exits = [str(code) for code in statuses if code is not None]
        return _diagnostic_text(f"{cause}"+(f"; tunneld exit={','.join(exits)}" if exits else "")+
                                (f"\n{output}" if output else ""))

    def _device_is_pinned(self) -> bool:
        return bool(self._requested_udid is not None or self._location_dirty or
                    self._dirty_marker is not None and self._dirty_marker.pending)

    def _claim_device(self, udid: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("bridge is closed")
            key = udid.replace("-", "").casefold()
            if self._device_lease is not None and self._leased_udid == key:
                return
            if self._device_lease is not None and self._location_dirty:
                raise RuntimeError("cannot change device while location restoration is unconfirmed")
            lease = device_lock(udid).acquire()
            self._release_device()
            self._device_lease, self._leased_udid = lease, key

    def _release_device(self) -> None:
        with self._lock:
            lease, self._device_lease = self._device_lease, None
            self._leased_udid = None
            if lease is not None:
                lease.close()

    def _release_retired_device(self) -> None:
        with self._lock:
            lease = self._retired_device_lease
            if (lease is None or self._sending or any(not work.done() for work in tuple(self._pending_sync_calls))
                    or self._thread is not None and not self._worker_finished):
                return
            self._retired_device_lease = None
            if lease in _quarantined_device_leases:
                _quarantined_device_leases.remove(lease)
            lease.close()

    async def _select_device(self) -> str:
        usbmux = importlib.import_module("pymobiledevice3.usbmux")
        devices = await asyncio.wait_for(_invoke(usbmux.list_devices, _executor=self._driver_executor), timeout=self.connect_timeout)
        serials = {device.serial.replace("-", "").casefold(): device.serial for device in devices}
        if self._device_is_pinned():
            selected = serials.get(self.udid.replace("-", "").casefold()) if self.udid else None
            if selected is None:
                raise DeviceConnectionError("device_unavailable", "The selected iPhone is not connected; reconnect the original device")
            return selected
        previous = self.udid
        if len(serials) != 1:
            with self._lock:
                self.udid = self.ios_version = None
            if serials:
                raise DeviceConnectionError("multiple_devices", "Multiple devices detected; keep only one connected or specify a device UDID")
            raise DeviceConnectionError("device_unavailable", "No iPhone is connected")
        selected = next(iter(serials.values()))
        if previous != selected:
            with self._lock:
                self.udid = self.ios_version = None
        return selected

    def _owned_tunneld_running(self) -> bool:
        with self._lock:
            return any(child.poll() is None for child in self._children)

    async def _tunneld_provider(self, stack: AsyncExitStack) -> Any:
        with self._lock:
            self.reason = "Waiting for local TCP tunneld"
        deadline = time.monotonic()+self.connect_timeout
        started = False
        cause = "no RSD tunnel for the selected iPhone"
        while time.monotonic() < deadline:
            try:
                devices = await asyncio.wait_for(_invoke(self._read_tunneld, _executor=self._driver_executor),
                                                 timeout=max(.001, deadline-time.monotonic()))
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                cause = f"tunneld discovery failed: {type(exc).__name__}: {exc}"
                if not started:
                    started = True
                    try:
                        self._spawn_tunneld()
                    except OSError as spawn_error:
                        raise DeviceConnectionError("tunnel_unavailable", self._tunnel_failure(spawn_error)) from spawn_error
                if not self._owned_tunneld_running():
                    raise DeviceConnectionError("tunnel_unavailable", self._tunnel_failure(cause)) from exc
            else:
                if self.udid is None and len(devices) > 1:
                    raise DeviceConnectionError("multiple_devices", "Multiple devices detected; specify a device UDID")
                selected = self.udid or next(iter(devices), "")
                key = selected.replace("-", "").casefold()
                details = [detail for serial, entries in devices.items()
                           if serial.replace("-", "").casefold() == key and isinstance(entries, list)
                           for detail in entries]
                if details:
                    break
                cause = "no RSD tunnel for the selected iPhone"
            await asyncio.sleep(min(self.TUNNEL_POLL_SECONDS, max(0.0, deadline-time.monotonic())))
        else:
            raise DeviceConnectionError("tunnel_unavailable", self._tunnel_failure(
                f"tunneld did not publish the iPhone within {self.connect_timeout:g}s: {cause}"))

        rsd_class = importlib.import_module("pymobiledevice3.remote.remote_service_discovery").RemoteServiceDiscoveryService
        errors = []
        for detail in details:
            attempt = AsyncExitStack()
            try:
                host, port = str(detail["tunnel-address"]), int(detail["tunnel-port"])
                if not 1 <= port <= 65535:
                    raise ValueError("invalid RSD port")
                rsd = rsd_class((host, port))
                attempt.push_async_callback(self._close_resource, rsd)
                await asyncio.wait_for(_invoke(rsd.connect, _executor=self._driver_executor),
                                       timeout=max(.001, deadline-time.monotonic()))
                self._verify_rsd_device(rsd, selected)
                stack.push_async_callback(attempt.aclose)
                self._tunneld_in_use = True
                return rsd
            except BaseException as exc:
                await attempt.aclose()
                if not isinstance(exc, Exception):
                    raise
                errors.append(f"{type(exc).__name__}: {exc}")
                if time.monotonic() >= deadline:
                    break
        raise DeviceConnectionError("tunnel_unavailable", self._tunnel_failure(
            "tunneld RSD handshake failed: "+"; ".join(errors)))

    @staticmethod
    def _verify_rsd_device(provider: Any, selected: str) -> None:
        actual = getattr(provider, "udid", None)
        if not actual:
            raise DeviceConnectionError("device_unavailable", "RSD did not identify the selected iPhone")
        if actual.replace("-", "").casefold() != selected.replace("-", "").casefold():
            raise DeviceConnectionError("device_unavailable", "RSD identified a different iPhone; refusing control")

    @contextlib.contextmanager
    def _connection_stage(self, stage: str, timeout: float):
        with self._lock:
            self.reason = stage
        try:
            yield
        except asyncio.TimeoutError as exc:
            raise DeviceConnectionError("device_connection_failed", f"{stage} timed out after {timeout:g}s") from exc
        except Exception as exc:
            raise DeviceConnectionError(_device_error_code(exc), f"{stage}: {type(exc).__name__}: {exc}") from exc

    async def _open_rsd(self, stack: AsyncExitStack, selected: str) -> Any:
        # An already running owned daemon may still hold the device tunnel.
        if self._tunneld_in_use or self._owned_tunneld_running():
            try:
                return await self._tunneld_provider(stack)
            except Exception:
                self._tunneld_in_use = False
                raise

        preferred_stack = AsyncExitStack()
        try:
            with self._connection_stage("Preferred RSD tunnel", self.connect_timeout):
                preferred = importlib.import_module("pymobiledevice3.remote.rsd_tunnel").PreferredRsdTunnel
                provider = await asyncio.wait_for(
                    self._enter_resource(preferred_stack, preferred(serial=selected, autopair=False)), timeout=self.connect_timeout)
                self._verify_rsd_device(provider, selected)
        except BaseException as exc:
            await preferred_stack.aclose()
            if not isinstance(exc, Exception):
                raise
            preferred_code = _device_error_code(exc)
            if preferred_code in {"device_trust_required", "device_developer_mode_required", "device_unavailable"}:
                raise
            try:
                return await self._tunneld_provider(stack)
            except Exception as fallback:
                fallback_code = _device_error_code(fallback, "tunnel_unavailable")
                if fallback_code == "tunnel_unavailable" and preferred_code == "tunnel_admin_required":
                    fallback_code = preferred_code
                raise DeviceConnectionError(fallback_code,
                    _diagnostic_text(f"{exc}; Tunneld fallback: {fallback}")) from fallback
        stack.push_async_callback(preferred_stack.aclose)
        return provider

    async def _open_session(self) -> None:
        stack = AsyncExitStack()
        try:
            with self._connection_stage("usbmux device discovery", self.connect_timeout):
                requested = await self._select_device()
            self._claim_device(requested)
            with self._connection_stage("USB Lockdown handshake", self.connect_timeout):
                async with AsyncExitStack() as probe:
                    lockdown_module = importlib.import_module("pymobiledevice3.lockdown")
                    lockdown = await asyncio.wait_for(
                        _invoke(lockdown_module.create_using_usbmux, serial=requested, autopair=False,
                                _executor=self._driver_executor), timeout=self.connect_timeout)
                    probe.push_async_callback(self._close_resource, lockdown)
                    if getattr(lockdown, "paired", None) is False:
                        raise DeviceConnectionError("device_trust_required", "The iPhone has not trusted this computer")
                    version = str(getattr(lockdown, "product_version", ""))
                    major = int(version.split(".")[0])
                    if not 17 <= major <= 27:
                        raise RuntimeError(f"iOS {version} is outside configured 17–27 range")
                    selected = getattr(lockdown, "udid", None) or getattr(lockdown, "identifier", None)
                    if not selected:
                        raise RuntimeError("lockdown did not identify the device")
                    if selected.replace("-", "").casefold() != requested.replace("-", "").casefold():
                        raise DeviceConnectionError("device_unavailable", "Device identity changed during connection; refusing control")
            with self._lock:
                self.udid, self.ios_version = selected, version
            provider = await self._open_rsd(stack, selected)
            with self._connection_stage(f"iOS {version} DDI auto-mount over RSD", self.mount_timeout):
                mounter = importlib.import_module("pymobiledevice3.services.mobile_image_mounter")
                # Let the driver check the phone again on every new session.
                # A cached UDID cannot tell us whether the phone rebooted.
                try:
                    await asyncio.wait_for(_invoke(mounter.auto_mount, provider, _executor=self._driver_executor),
                                           timeout=self.mount_timeout)
                except Exception as exc:
                    try:
                        exceptions = importlib.import_module("pymobiledevice3.exceptions")
                    except ImportError:
                        already_mounted = ()
                    else:
                        already_mounted = getattr(exceptions, "AlreadyMountedError", ())
                    if not isinstance(exc, already_mounted):
                        raise
            with self._connection_stage("DVT handshake", self.connect_timeout):
                try:
                    dvt_class = importlib.import_module("pymobiledevice3.services.dvt.instruments.dvt_provider").DvtProvider
                except (ImportError, AttributeError):
                    dvt_class = importlib.import_module("pymobiledevice3.services.dvt.dvt_secure_socket_proxy").DvtSecureSocketProxyService
                dvt = await asyncio.wait_for(self._enter_resource(stack, dvt_class(provider)), timeout=self.connect_timeout)
            with self._connection_stage("DVT LocationSimulation channel", self.connect_timeout):
                sim_class = importlib.import_module("pymobiledevice3.services.dvt.instruments.location_simulation").LocationSimulation
                simulator = await asyncio.wait_for(self._enter_resource(stack, sim_class(dvt)), timeout=self.connect_timeout)
            wait_disconnected = getattr(getattr(dvt, "dtx", None), "wait_disconnected", None)
            if wait_disconnected is not None:
                self._dvt_disconnect_task = asyncio.create_task(wait_disconnected())
            self._stack, self._simulator, self._provider = stack, simulator, provider
        except BaseException:
            if not self._device_is_pinned():
                with self._lock:
                    self.udid = self.ios_version = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stack.aclose(), timeout=2.0)
            raise

    async def _wait_sync_calls(self) -> None:
        pending = tuple(self._pending_sync_calls)
        if not pending:
            return
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(
                *(asyncio.wrap_future(work) for work in pending), return_exceptions=True)),
                                   timeout=self.send_timeout)
        finally:
            finished = {work for work in pending if work.done()}
            self._pending_sync_calls.difference_update(finished)
            if finished:
                # Conservative completion baseline, including timed-out sends.
                self._last_send_completed_at = self._wire_clock()

    async def _disconnect(self, *, clear: bool = False) -> bool:
        if self._pending_sync_calls:
            if not clear and any(not work.done() for work in tuple(self._pending_sync_calls)):
                return False
            try:
                await self._wait_sync_calls()
            except asyncio.TimeoutError:
                self.last_error = "location reset waiting for an unfinished synchronous driver call"
                return False
        tasks = (self._battery_task, self._dvt_disconnect_task)
        self._battery_task = None
        self._dvt_disconnect_task = None
        for task in tasks:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        simulator, stack = self._simulator, self._stack
        cleared = self._last_clear_succeeded or not (self._ever_connected or self._location_dirty)
        if clear and simulator is not None:
            try:
                if self._closed:
                    raise RuntimeError("bridge is closed; no new clear may start after the recovery deadline")
                if not self.force_sandbox and self.udid is not None:
                    self._claim_device(self.udid)
                if self._dirty_marker is not None and self._dirty_marker.pending and self.udid != self._dirty_marker.udid:
                    raise RuntimeError("location clear refused for a device different from the marker")
                if self._dirty_marker is not None:
                    self._dirty_marker.assert_current()
                await asyncio.wait_for(_invoke(simulator.clear, _sync_calls=self._pending_sync_calls,
                                              _executor=self._driver_executor),
                                       timeout=self.send_timeout)
                if self._dirty_marker is not None:
                    self._dirty_marker.cleared()
                self._location_dirty = self._recovery_required = False
                self._last_clear_succeeded = True
                cleared = True
            except Exception as exc:
                cleared = False
                with self._lock:
                    self.last_error = f"location reset failed: {exc}"
        if any(not work.done() for work in tuple(self._pending_sync_calls)):
            return False
        self._simulator = self._stack = self._provider = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stack.aclose(), timeout=2.0)
        if cleared and clear:
            self._release_device()
        return cleared

    async def _restore_session(self, deadline: float) -> bool:
        """Clear takes priority over the fuse; never flush coordinates here."""
        if self._last_clear_succeeded and not self._location_dirty:
            await self._disconnect(clear=False)
            return True
        if self._dirty_marker is not None and self._dirty_marker.pending:
            if self._dirty_marker.invalid or self.udid != self._dirty_marker.udid:
                self.last_error = "location recovery requires an identifiable matching device"
                return False
        while time.monotonic() < deadline:
            try:
                if self._simulator is None:
                    await asyncio.wait_for(self._open_session(), timeout=max(.001, deadline-time.monotonic()))
                cleared = await asyncio.wait_for(self._disconnect(clear=True),
                                                timeout=max(.001, deadline-time.monotonic()))
                if cleared:
                    return True
                LOGGER.warning("location clear was not confirmed; retrying within the recovery window")
            except Exception as exc:
                self.last_error = f"location recovery failed: {exc}"
                LOGGER.warning("location recovery attempt failed: %s", exc)
            remaining = deadline-time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(self.RETRY_SECONDS, remaining))
        if self._dirty_marker is not None:
            try:
                self._dirty_marker.failed(self.last_error or "location recovery timed out")
            except (OSError, ValueError, InstanceRunningError):
                LOGGER.exception("could not record failed location recovery")
        return False

    def _complete_clear(self, cleared: bool) -> None:
        with self._lock:
            future = self._clear_future
            self._clear_future = None
            self._clear_requested = False
            self._last_clear_succeeded = cleared
            self.mode = BridgeMode.SANDBOX_MODE
            if cleared:
                self._fuse_tripped = False
                self._last_send_at = self._last_send_position = None
                self._last_send_completed_at = None
            self.reason = "location simulation released" if cleared else "location reset unconfirmed"
            self._clear_ack.set()
            if future is not None and not future.done():
                future.set_result(cleared)

    async def _read_battery(self) -> None:
        try:
            diagnostics_class = importlib.import_module("pymobiledevice3.services.diagnostics").DiagnosticsService
            async with AsyncExitStack() as stack:
                service = await self._enter_resource(stack, diagnostics_class(self._provider))
                battery = await asyncio.wait_for(_invoke(service.get_battery, _executor=self._driver_executor), timeout=1.0)
            if not isinstance(battery, Mapping):
                raise ValueError("device did not expose battery information")
            current = battery.get("CurrentCapacity", battery.get("AppleRawCurrentCapacity"))
            maximum = battery.get("MaxCapacity", battery.get("AppleRawMaxCapacity"))
            if current is None:
                raise ValueError("battery capacity missing")
            current = _finite(current, "battery capacity")
            if maximum is not None:
                maximum = _finite(maximum, "battery maximum")
                if maximum <= 0:
                    raise ValueError("invalid maximum battery capacity")
                percent = current/maximum*100
            elif 0 <= current <= 100:
                percent = current
            else:
                raise ValueError("raw battery capacity without maximum")
            percent = max(0, min(100, percent))
            with self._lock:
                self.battery_percent = percent
                self.battery_charging = bool(battery.get("IsCharging", battery.get("ExternalConnected", False)))
                self.low_battery = percent < self.low_battery_percent
                self.battery_error = None
        except Exception as exc:
            with self._lock:
                self.battery_error = f"{type(exc).__name__}: {exc}"
                self.battery_percent = self.low_battery = self.battery_charging = None

    def _prune_delivered(self) -> None:
        """Caller holds _lock; move a reserved checkpoint only after a slot frees."""
        while self._critical and self._critical[0][0] <= self._sent_sequence:
            self._critical_sequences.discard(self._critical.popleft()[0])
        if self._deferred_critical is not None and len(self._critical) < self._critical.maxlen:
            self._critical.append(self._deferred_critical)
            self._deferred_critical = None

    def _take_sample(self, reconnect: bool = False) -> Optional[tuple[int, float, float]]:
        with self._lock:
            self._prune_delivered()
            if self._critical:
                # Peek until successful delivery. A timeout or disconnect must
                # not lose an in-flight checkpoint. FIFO keeps its approach
                # intact; jumping a checkpoint ahead could trip the speed fuse.
                return self._critical[0]
            heartbeat_due = self._last_send_completed_at is not None and self._wire_clock()-self._last_send_completed_at >= MOTION_INTERVAL_S
            return self._latest if self._latest is not None and (reconnect or self._latest[0] > self._sent_sequence or heartbeat_due) else None

    @contextlib.asynccontextmanager
    async def _sending_sample(self):
        with self._lock:
            self._sending = True
        try:
            yield
        finally:
            with self._lock:
                self._sending = False

    async def _send(self, sample: tuple[int, float, float]) -> None:
        # This is the sole location injection gateway, including reconnection,
        # critical waypoints and unchanged-position heartbeats.
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock, self._sending_sample():
            if self._fuse_tripped:
                raise SpeedFuseError("speed fuse is latched; explicit restoration required")
            await self._wait_sync_calls()
            completed_at = self._last_send_completed_at if self._last_send_completed_at is not None else self._last_send_at
            if completed_at is not None:
                wait = MOTION_INTERVAL_S-(self._wire_clock()-completed_at)
                if wait > 1e-6:
                    await asyncio.sleep(wait)
            if self._closed or self._closing or self._clear_requested:
                return
            if self._recovery_required:
                raise RuntimeError("the previous location session must be cleared first")
            # A producer can latch the fuse while this coroutine is waiting
            # for its rate-limit slot. Revalidate before touching the device.
            if self._fuse_tripped:
                raise SpeedFuseError("speed fuse latched while waiting to transmit")
            sent_at = self._wire_clock()
            if self._last_send_at is not None and self._last_send_position is not None:
                gap = max(1e-6, sent_at-self._last_send_at)
                self._max_send_gap_s = max(self._max_send_gap_s, gap)
                if gap > 1.0+1e-6:
                    self._send_late_count += 1
                # A long outage must not grant permission to jump to a distant
                # simulated position. Preserve this baseline across reconnects.
                distance = haversine_distance(*self._last_send_position, sample[1], sample[2])
                if distance > MAX_SAMPLE_DISTANCE_M+DISTANCE_TOLERANCE_M:
                    with self._lock:
                        self._fuse_tripped = True
                        self.last_error = "speed_fuse_tripped"
                        self.reason = "coordinate transmission blocked by speed fuse"
                        LOGGER.error("speed_fuse_tripped: step %.3f m exceeds %.3f m", distance, MAX_SAMPLE_DISTANCE_M)
                    raise SpeedFuseError("coordinate exceeds the one-second 3.8 m wire step bound")
            with self._lock:
                if self._closed or self._closing or self._clear_requested:
                    return
                if not self._location_dirty:
                    if not self.force_sandbox and self.udid is not None:
                        self._claim_device(self.udid)
                    if self._dirty_marker is not None and not self.force_sandbox:
                        self._dirty_marker.mark(self.udid)
                    self._location_dirty = True
                    self._last_clear_succeeded = False
            try:
                await asyncio.wait_for(_invoke(self._simulator.set, sample[1], sample[2],
                                               _sync_calls=self._pending_sync_calls,
                                               _executor=self._driver_executor), timeout=self.send_timeout)
            except asyncio.TimeoutError as exc:
                raise asyncio.TimeoutError(f"DVT location send timed out after {self.send_timeout:g}s") from exc
            with self._lock:
                self._last_send_at = sent_at
                self._last_send_completed_at = self._wire_clock()
                self._last_send_position = (sample[1], sample[2])
                self._sent_sequence = sample[0]
                self._prune_delivered()
                self.physical_sent += 1
                self.last_delivered_position = {"lat": sample[1], "lon": sample[2]}

    def _check_dvt_connection(self) -> None:
        task = self._dvt_disconnect_task
        if task is not None and task.done():
            task.result()
            raise ConnectionError("DVT transport disconnected")

    async def _wait_to_retry(self, delay: float) -> None:
        if self._clear_requested:
            return
        try:
            await asyncio.wait_for(self._retry_wakeup.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        finally:
            self._retry_wakeup.clear()

    def _wake_for_clear(self) -> None:
        if self._clear_requested:
            self._retry_wakeup.set()
            if self._open_task is not None:
                self._open_task.cancel()

    async def _serve(self) -> None:
        next_battery = math.inf
        retry_delay = self.DISCOVERY_SECONDS
        self._retry_wakeup = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            while not self._shutdown.is_set():
                if self._clear_requested:
                    self._retry_wakeup.clear()
                    cleared = await self._restore_session(self._clear_deadline)
                    self._complete_clear(cleared)
                    continue
                if self._fuse_tripped or self._closing or self._recovery_required:
                    await asyncio.sleep(0.05)
                    continue
                if self.mode == BridgeMode.RECONNECTING and self._simulator is not None:
                    # A timed-out synchronous write can outlive the failed
                    # connection. Wait for it before closing and reopening.
                    await self._disconnect()
                    if self._simulator is not None:
                        await self._wait_to_retry(self.RETRY_SECONDS)
                        continue
                if self._simulator is None:
                    with self._lock:
                        self.mode = BridgeMode.RECONNECTING if self._ever_connected else BridgeMode.CONNECTING
                        self.reconnect_attempts += 1
                    try:
                        self._open_task = asyncio.create_task(self._open_session())
                        try:
                            await self._open_task
                        except asyncio.CancelledError:
                            if not self._clear_requested or self._shutdown.is_set():
                                raise
                            continue
                        finally:
                            self._open_task = None
                        if self._clear_requested:
                            continue
                        # Deliver the FIFO head before announcing connection;
                        # later points still need their physical predecessors.
                        self._check_dvt_connection()
                        sample = self._take_sample(reconnect=True)
                        if sample is not None:
                            await self._send(sample)
                        self._check_dvt_connection()
                        with self._lock:
                            self.mode = BridgeMode.CONNECTED
                            self.reason = "RSD + DVT location simulation ready"
                            self.last_error = None
                            self._device_error_code = None
                            self._ever_connected = True
                        retry_delay = self.DISCOVERY_SECONDS
                        next_battery = time.monotonic()+5.0
                    except SpeedFuseError:
                        continue
                    except Exception as exc:
                        await self._disconnect()
                        with self._lock:
                            self.mode = BridgeMode.RECONNECTING if self._simulator is not None else BridgeMode.SANDBOX_MODE
                            self.last_error = _diagnostic_text(f"{type(exc).__name__}: {exc}")
                            self._device_error_code = _device_error_code(exc)
                            self.reason = self.last_error
                        LOGGER.warning("Device connection failed: %s", self.last_error, extra={"fault": True})
                        await self._wait_to_retry(retry_delay)
                        retry_delay = min(self.MAX_DISCOVERY_SECONDS, retry_delay*2)
                        continue
                sample = None
                try:
                    self._check_dvt_connection()
                    sample = self._take_sample()
                    if sample is not None:
                        await self._send(sample)
                    if time.monotonic() >= next_battery:
                        if self._battery_task is None or self._battery_task.done():
                            self._battery_task = asyncio.create_task(self._read_battery())
                        next_battery = time.monotonic()+60
                    await asyncio.sleep(0.02)
                except SpeedFuseError:
                    continue
                except Exception as exc:
                    with self._lock:
                        self.mode = BridgeMode.RECONNECTING
                        self._device_error_code = _device_error_code(exc)
                        self.reason = self.last_error = _diagnostic_text(f"{type(exc).__name__}: {exc}")
                        failure = "Location transmission failed" if sample is not None else "DVT connection lost"
                        LOGGER.warning("%s: %s", failure, self.last_error, extra={"fault": True})
                        if sample is not None and sample[0] not in self._critical_sequences:
                            self.dropped_device_samples += 1
                    await self._disconnect()
                    await self._wait_to_retry(self.RETRY_SECONDS)
        finally:
            needs_clear = self._location_dirty or (self._ever_connected and not self._last_clear_succeeded)
            deadline = self._close_deadline if self._close_deadline is not None else time.monotonic()+3.0
            if needs_clear and time.monotonic() < deadline:
                cleared = await self._restore_session(deadline)
            else:
                cleared = await self._disconnect(clear=False)
            self._complete_clear(cleared)

    def _worker(self) -> None:
        try:
            if sys.platform == "win32" and hasattr(asyncio, "Runner"):
                # Match pymobiledevice3's Windows CLI for Bonjour/RSD while
                # keeping the selector loop local to this worker thread.
                with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                    runner.run(self._serve())
            else:
                asyncio.run(self._serve())
        except asyncio.CancelledError:
            LOGGER.debug("device worker cancelled")
        except Exception as exc:
            LOGGER.exception("device worker stopped; Sandbox remains available")
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.reason = self.last_error
                self.mode = BridgeMode.SANDBOX_MODE
        finally:
            with self._lock:
                self._worker_finished = True
                self._loop = self._task = None
                if self._closed:
                    self.mode = BridgeMode.CLOSED
                self._release_retired_device()

    def clear_location(self, timeout: float = 3.0) -> bool:
        """Explicitly restore real GPS. Stop the runner before calling this.

        Returns False if acknowledgement cannot be obtained before timeout.
        Never blocks the motion thread; this is a UI shutdown/reset operation.
        """
        timeout = _finite(timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._lock:
            if self._closed:
                return False
            self._latest = None
            self._critical.clear()
            self._critical_sequences.clear()
            self._deferred_critical = None
            if self.force_sandbox or not (self._ever_connected or self._location_dirty):
                self._fuse_tripped = False
                self._last_send_at = self._last_send_position = None
                self._last_send_completed_at = None
                self._last_clear_succeeded = True
                return True
            if self._thread is threading.current_thread():
                raise RuntimeError("clear_location cannot wait on its own worker")
            if not self._clear_requested:
                self._clear_ack.clear()
                self._last_clear_succeeded = False
                self._clear_future = Future()
                self._clear_deadline = time.monotonic()+timeout
                self._clear_requested = True
            future = self._clear_future
            self.connect()
            if self._loop is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._wake_for_clear)
        try:
            return bool(future.result(timeout=timeout))
        except FutureTimeoutError:
            return False

    @staticmethod
    def _terminate_child(child: subprocess.Popen) -> None:
        if child.poll() is not None:
            # A dead group leader may still have workers holding the log pipe.
            if os.name != "nt" and getattr(child, "_piggy_managed", False):
                IOSDeviceBridge._kill_remaining_group(child.pid)
            return
        try:
            if os.name == "nt":
                # /T covers children of ONLY this owned PID; no image-name kill.
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3, check=False)
            else:
                import signal
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    child.kill()
                else:
                    import signal
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            if os.name != "nt":
                IOSDeviceBridge._kill_remaining_group(child.pid)
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("owned tunneld cleanup failed", exc_info=True, extra={"fault": True})

    @staticmethod
    def _kill_remaining_group(pid: int) -> None:
        # After wait() reaps the owned leader, do not target a recycled leader
        # PID. A still-existing old group with no leader consists of our workers.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            with contextlib.suppress(ProcessLookupError):
                import signal
                os.killpg(pid, signal.SIGKILL)

    def close(self, timeout: float = 6.0) -> bool:
        timeout = _finite(timeout, "timeout")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic()+timeout
        with self._lock:
            if self._closed:
                return self._last_clear_succeeded
            already_closing = self._closing
            self._closing = True
            if not already_closing:
                self._close_deadline = deadline
        if already_closing:
            self._close_ack.wait(timeout)
            return self._last_clear_succeeded
        # Keep the worker/tunnel alive until the bounded clear attempts finish.
        # A timed-out first send can have reached the phone before the session
        # was announced connected, including when persistence is not enabled.
        needs_clear = (self._ever_connected or self._location_dirty) and not self._last_clear_succeeded
        if needs_clear and not self.force_sandbox and threading.current_thread() is not self._thread:
            try:
                for _ in range(2):
                    if time.monotonic() >= deadline:
                        break
                    if self.clear_location(timeout=max(.001, deadline-time.monotonic())):
                        break
                    if self._clear_requested:
                        break
            except Exception:
                LOGGER.exception("location clear could not finish during shutdown")
        with self._lock:
            self._closed = True
            self._shutdown.set()
            loop, task, thread = self._loop, self._task, self._thread
            children = list(self._children)
            self._children.clear()
            self.mode = BridgeMode.CLOSED
        if loop is not None and task is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline-time.monotonic()))
        for child in children:
            self._terminate_child(child)
        for reader in self._tunnel_readers:
            reader.join(timeout=.2)
        self._driver_executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            unfinished = (self._sending or any(not work.done() for work in tuple(self._pending_sync_calls))
                          or thread is not None and not self._worker_finished)
            if self._device_lease is not None and unfinished:
                self._retired_device_lease = self._device_lease
                _quarantined_device_leases.append(self._retired_device_lease)
                self._device_lease = None
                for work in tuple(self._pending_sync_calls):
                    work.add_done_callback(lambda done: self._release_retired_device())
                self._release_retired_device()
            else:
                self._release_device()
        atexit.unregister(self.close)
        if not needs_clear:
            self._last_clear_succeeded = True
        if needs_clear and not self._last_clear_succeeded:
            # The caller's deadline can expire just before the worker records
            # its timeout. Persist the final result before close() returns too.
            if self._dirty_marker is not None:
                try:
                    self._dirty_marker.failed(self.last_error or "shutdown location clear timed out")
                except (OSError, ValueError, InstanceRunningError):
                    LOGGER.exception("could not record failed shutdown location recovery")
            LOGGER.error("shutdown finished without confirmed location restoration; dirty marker retained")
        else:
            LOGGER.log(logging.INFO if needs_clear else logging.DEBUG,
                       "shutdown location restoration confirmed or no injection occurred")
        self._close_ack.set()
        return self._last_clear_succeeded or not needs_clear

    def __enter__(self) -> "IOSDeviceBridge":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
