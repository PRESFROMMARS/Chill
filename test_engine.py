"""Offline numerical, state-machine and mocked device-transport regression tests."""
import asyncio
import hashlib
import importlib
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import engine as e


ORIGIN = (31.2304, 121.4737)


def circle(radius=60.0, count=16, clockwise=False):
    bearings = [i*360/count for i in range(count)]
    if not clockwise:
        bearings = list(reversed(bearings))
    return [e._destination(ORIGIN, bearing, radius) for bearing in bearings]


class EngineCase(unittest.TestCase):
    def runner(self, **kwargs):
        bridge = e.IOSDeviceBridge(sandbox=True)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(41), clock=lambda: runner.elapsed_s)
        self.addCleanup(bridge.close)
        self.addCleanup(runner.close)
        runner.load_track(circle(), **kwargs)
        runner.lock_start(background=False)
        runner.start_cruising(background=False)
        return runner


class RestTests(EngineCase):
    def test_walk_break_continues_motion_and_resumes_without_a_step_change(self):
        for dt in (0.2, .95, 1.9):
            with self.subTest(dt=dt):
                r=self.runner(target_km=.2)
                while r.distance_m<30:r.step(dt)
                before=r.snapshot();r.pause()
                self.assertEqual(r.position,before["position"])
                prior_speed=r.speed_mps
                for _ in range(80):
                    old=e._point(r.position);distance=r.distance_m;state=r.step(dt)
                    self.assertAlmostEqual(e.haversine_distance(*old,*e._point(r.position)),r.distance_m-distance,delta=.04)
                    self.assertLess(abs(r.speed_mps-prior_speed)/dt,.6)
                    prior_speed=r.speed_mps
                    if state["pause_phase"]=="WALKING_REST":break
                self.assertEqual(state["pause_phase"],"WALKING_REST")
                distance=r.distance_m;elapsed=r.elapsed_s
                for _ in range(5):state=r.step(dt)
                self.assertGreater(state["distance_m"],distance)
                self.assertGreater(state["elapsed_s"],elapsed)
                self.assertEqual(state["session_elapsed_s"],state["elapsed_s"])
                self.assertEqual(r.resume()["pause_phase"],"resuming")
                for _ in range(80):
                    old=r.speed_mps;state=r.step(dt)
                    if state["finishing"]:break
                    self.assertLess(abs(r.speed_mps-old)/dt,.6)
                    if state["pause_phase"]=="running":break
                for _ in range(1800):
                    state=r.step(dt)
                    if state["target_reached"]:break
                self.assertEqual(state["state"],"CRUISING_LOOP")
                self.assertTrue(state["target_walking"])
                self.assertGreaterEqual(state["distance_m"],200)
                r.close()
                self.assertEqual(r.state,e.RunnerState.SAFE_STOPPED)

    def test_committed_detour_survives_a_break_and_can_settle_while_resting(self):
        r = self.runner()
        for _ in range(20):
            r.step()
        target = e._destination(ORIGIN, 120, 100)
        r.submit_detour([{"lat":target[0],"lon":target[1],"is_target":True,"id":"visit"}])
        for _ in range(600):
            if r.state == e.RunnerState.CRUISING_DETOUR:
                break
            r.step()
        self.assertEqual(r.state, e.RunnerState.CRUISING_DETOUR)
        r.pause()
        for _ in range(30):
            if r.step()["pause_phase"] == "WALKING_REST":
                break
        self.assertEqual(r.snapshot()["pause_phase"], "WALKING_REST")
        r.resume()
        for _ in range(700):
            r.step()
            if "visit" in r.targets_visited:
                break
        self.assertIn("visit", r.targets_visited)
        while r.snapshot()["pause_phase"] != "running":
            r.step()
        r.pause()
        for _ in range(30):
            if r.step()["pause_phase"] == "WALKING_REST":
                break
        r.close()
        self.assertEqual(r.state, e.RunnerState.SAFE_STOPPED)
        with self.assertRaises(RuntimeError):
            r.resume()

    def test_invalid_phase_commands_do_not_restart_a_transition(self):
        r = self.runner(target_km=0.1)
        with self.assertRaises(RuntimeError):
            r.resume()
        for _ in range(10):
            r.step()
        r.pause()
        with self.assertRaises(RuntimeError):
            r.pause()
        with self.assertRaises(RuntimeError):
            r.resume()
        for _ in range(20):
            if r.step()["pause_phase"] == "WALKING_REST":
                break
        r.resume()
        with self.assertRaises(RuntimeError):
            r.resume()
        for _ in range(100):
            state = r.step()
            if state["finishing"]:
                break
        self.assertTrue(state["finishing"])
        with self.assertRaises(RuntimeError):
            r.pause()

    def test_rest_on_a_narrow_track_preserves_corridor_and_lap_clock(self):
        r = self.runner(track_width_m=1)
        while r.distance_m < 30:
            r.step()
        first_start = r._lap_start_time
        for cycle in range(2):
            r.pause()
            for _ in range(20):
                state = r.step()
                self.assertTrue(r._corridor.within(e._point(r.position), 0.5))
                if state["pause_phase"] == "WALKING_REST":
                    break
            for _ in range(10):
                r.step()
            r.resume()
            for _ in range(30):
                state = r.step()
                self.assertTrue(r._corridor.within(e._point(r.position), 0.5))
                if state["pause_phase"] == "running":
                    break
        while r.laps < 1:
            r.step()
        self.assertAlmostEqual(r.lap_splits[0], r._lap_start_time-first_start)
        # Crossing time is on the session clock, including stationary rest.
        self.assertGreater(r._lap_start_time, r.elapsed_s-1.9)
        self.assertGreater(r._rest_total_s, 18)

    def test_scheduler_never_catches_up_a_walking_gap(self):
        for phase, expected in (("running", .95), ("WALKING_REST", .95)):
            r = self.runner()
            r._pause_phase = phase
            r._active_since = None
            with patch.object(r._shutdown, "wait", side_effect=[False, True]), \
                 patch.object(e.time, "monotonic", side_effect=[0,0,5,5,5,5,5,5]), \
                 patch.object(r, "_tick") as tick:
                r.run()
            tick.assert_called_once_with(expected)

    def test_extra_distance_is_opt_in_bounded_and_validated_before_mutation(self):
        for target in (0.1, 2, 100):
            r = self.runner(target_km=target, extra_distance=True)
            self.assertGreaterEqual(r.target_m, target*1000)
            self.assertLessEqual(r.target_m-target*1000, min(60,target*30))
        r = self.runner()
        before = r.snapshot()
        with self.assertRaises(ValueError):
            r.load_track(circle(),extra_distance="yes")
        self.assertEqual(r.snapshot()["session_id"], before["session_id"])
        self.assertEqual(r.target_m, 2000)


class CoordinateTests(EngineCase):
    def test_proxy_immunity(self):
        self.assertEqual(os.environ["NO_PROXY"], "*")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.assertEqual(os.environ[key], "")

    def test_known_beijing_conversion_and_inverse_grid(self):
        lng, lat = e.wgs84_to_gcj02(116.404, 39.915)
        self.assertAlmostEqual(lng, 116.41024449916938, places=10)
        self.assertAlmostEqual(lat, 39.91640428150164, places=10)
        rng = random.Random(7)
        worst = 0.0
        for _ in range(1000):
            original = (rng.uniform(73, 136), rng.uniform(2, 54))
            converted = e.wgs84_to_gcj02(*original)
            inverse = e.gcj02_to_wgs84(*converted)
            worst = max(worst, *(abs(a-b) for a, b in zip(original, inverse)))
        self.assertLess(worst, 1e-7)
        self.assertEqual(e.wgs84_to_gcj02(-0.1, 51.5), (-0.1, 51.5))
        self.assertEqual(e.gcj02_to_wgs84(-0.1, 51.5), (-0.1, 51.5))

    def test_haversine_bearings_and_antipodes(self):
        self.assertEqual(e.haversine_distance(0, 0, 0, 0), 0)
        self.assertAlmostEqual(e.haversine_distance(0, 0, 0, 1), 111195.0802335329, places=5)
        self.assertAlmostEqual(e.haversine_distance(0, 0, 0, 180), math.pi*e.EARTH_RADIUS_M)
        self.assertAlmostEqual(e.calculate_bearing(0, 0, 0, 1), 90)
        self.assertAlmostEqual(e.calculate_bearing(0, 0, 1, 0), 0)
        self.assertAlmostEqual(e.haversine_distance(*ORIGIN, *e._destination(ORIGIN, 123, 0.5)), 0.5, places=7)

    def test_invalid_coordinate_inputs(self):
        for point in ((math.nan, 120), (31, math.inf), (91, 0), (0, 181), (True, 0)):
            with self.assertRaises(ValueError):
                e.haversine_distance(*point, 0, 0)


class ClockTests(EngineCase):
    def test_signed_offset_and_frontend_warning(self):
        class Response:
            headers = {"Date": formatdate(1100, usegmt=True)}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
        warning = []
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
        with patch.object(e.urllib.request, "build_opener", return_value=opener), \
             patch.object(e.time, "time", side_effect=[1000.0, 1000.2]), \
             patch.object(e.time, "monotonic", side_effect=[50.0, 50.2]), \
             patch.object(e.LOGGER, "warning"):
            offset = e.check_ntp_offset(["https://example.test"], on_warning=warning.append)
        self.assertAlmostEqual(offset, 100.4)
        self.assertEqual(len(warning), 1)

    def test_offline_is_not_zero_offset(self):
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
        with patch.object(e.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(e.TimeSyncError):
                e.check_ntp_offset(["https://example.test"])

    def test_stale_and_mid_request_clock_changes_rejected(self):
        class Response:
            headers = {"Date": formatdate(1000, usegmt=True), "Age": "3600"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
        with patch.object(e.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(e.TimeSyncError):
                e.check_ntp_offset(["https://example.test"])
        Response.headers["Age"] = "0"
        with patch.object(e.urllib.request, "build_opener", return_value=opener), \
             patch.object(e.time, "time", side_effect=[1000, 2000]), \
             patch.object(e.time, "monotonic", side_effect=[50, 50.1]):
            with self.assertRaises(e.TimeSyncError):
                e.check_ntp_offset(["https://example.test"])


class GeometryTests(EngineCase):
    def test_ccw_correction_closure_and_start_preservation(self):
        points = circle(clockwise=True)
        fixed = e.TrackGeometry.ensure_counter_clockwise(points+[points[0]])
        self.assertEqual(fixed[0], points[0])
        self.assertEqual(fixed[-1], fixed[0])
        projection = e._Projection(ORIGIN)
        xy = [projection.xy(p) for p in fixed]
        area = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(xy, xy[1:]))
        self.assertGreater(area, 0)

    def test_ring_spacing_curvature_and_perimeter(self):
        geometry = e.TrackGeometry(circle())
        points = geometry.points
        self.assertAlmostEqual(geometry.length_m, 2*math.pi*60, delta=1.0)
        distances = [e.haversine_distance(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(points, points[1:])]
        self.assertTrue(all(abs(d-0.5) < 0.0001 for d in distances))
        self.assertTrue(all(abs(b["s_m"]-a["s_m"]-0.5) < 1e-8 for a, b in zip(points, points[1:])))
        self.assertGreater(e.haversine_distance(points[-1]["lat"], points[-1]["lon"], points[0]["lat"], points[0]["lon"]), 0)
        average_curvature = sum(p["curvature"] for p in points)/len(points)
        self.assertAlmostEqual(average_curvature, 1/60, delta=0.001)

    def test_open_spline_retains_control_points_and_endpoints(self):
        points = [ORIGIN, e._destination(ORIGIN, 40, 30), e._destination(ORIGIN, 110, 60)]
        dense = e.TrackGeometry.catmull_rom(points, False)
        for point in points:
            self.assertLess(min(e.haversine_distance(*p, *point) for p in dense), 1e-5)
        sampled = e.TrackGeometry.build(points, False)
        self.assertLess(e.haversine_distance(*e._point(sampled[0]), *points[0]), 1e-6)
        self.assertEqual(e._point(sampled[-1]), points[-1])

    def test_degenerate_and_budget_validation(self):
        for points in ([], [ORIGIN], [ORIGIN]*4, [(0, 0), (0, 0.001), (0, 0.002)]):
            with self.assertRaises(ValueError):
                e.TrackGeometry(points)
        with self.assertRaises(ValueError):
            e.TrackGeometry(circle(), spacing_m=0)


class RunnerTests(EngineCase):
    def test_randomization_and_stride(self):
        runner = self.runner(profile="high_cadence")
        self.assertGreaterEqual(runner.base_speed_mps, 3.1)
        self.assertLessEqual(runner.base_speed_mps, 3.3)
        self.assertEqual(runner.target_m, 2000)
        self.assertEqual(runner.target_m, runner.requested_target_m)
        radius = e.haversine_distance(*runner._center.points[0], *runner.start_point)
        self.assertTrue(4 <= radius <= 7)
        runner.step()
        self.assertGreater(runner.speed_mps, 0)
        self.assertAlmostEqual(runner.stride_m, runner.speed_mps/(192/60))

    def test_target_transitions_to_indefinite_walk_until_manual_settlement(self):
        for dt in (0.2, 0.95, 1.9):
            with self.subTest(dt=dt):
                runner = self.runner(target_km=0.1)
                last_position = e._point(runner.position)
                physical_distance = 0.0
                prior_speed = None
                reached_at = None
                for _ in range(2000):
                    state = runner.step(dt)
                    position = e._point(state["position"])
                    physical_distance += e.haversine_distance(*last_position, *position)
                    last_position = position
                    self.assertGreaterEqual(state["speed_mps"],1.2-1e-6)
                    if prior_speed is not None:
                        self.assertLess(abs(state["speed_mps"]-prior_speed)/dt,.6)
                    prior_speed = state["speed_mps"]
                    if state["target_reached"] and reached_at is None:
                        reached_at=state["elapsed_s"]
                    if reached_at is not None and state["elapsed_s"]-reached_at>65:
                        break
                self.assertIsNotNone(reached_at)
                self.assertEqual(state["state"], "CRUISING_LOOP")
                self.assertTrue(state["target_walking"])
                self.assertEqual(state["pause_phase"],"WALKING_REST")
                self.assertGreater(state["distance_m"],100)
                self.assertLess(abs(physical_distance-state["distance_m"]),.5)
                self.assertAlmostEqual(state["offset_m"],1.8,delta=.03)
                with self.assertRaises(RuntimeError):runner.resume()
                with self.assertRaises(RuntimeError):runner.pause()
                with self.assertRaises(RuntimeError):runner.submit_detour([{"lat":ORIGIN[0],"lon":ORIGIN[1],"is_target":True}])
                runner.close()
                self.assertEqual(runner.state,e.RunnerState.SAFE_STOPPED)
                self.assertEqual(runner.speed_mps,0)

    def test_full_session_lanes_and_settlement(self):
        runner = self.runner()
        positions = []
        for _ in range(1500):
            old_distance = runner.distance_m
            state = runner.step()
            self.assertGreaterEqual(runner.distance_m, old_distance)
            if state["state"] == "SAFE_STOPPED":
                break
            self.assertGreater(state["speed_mps"], 0)
            self.assertLessEqual(state["speed_mps"], 4.2)
            positions.append(e._point(state["position"]))
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)
        self.assertTrue(runner.snapshot()["target_walking"])
        self.assertGreater(runner.distance_m, runner.target_m)
        self.assertGreater(runner.laps, 4)
        self.assertTrue(-0.4-1e-6 <= runner.offset_m <= 2.2+1e-6)
        self.assertTrue(all(e.haversine_distance(*a, *b) <= 4.2*0.95+0.01 for a, b in zip(positions, positions[1:])))

    def test_detour_waits_for_exit_hits_targets_and_merges(self):
        runner = self.runner()
        for _ in range(10):
            runner.step()
        target1 = e._destination(ORIGIN, 110, 100)
        target2 = e._destination(ORIGIN, 150, 110)
        runner.submit_detour([{"lat": target1[0], "lon": target1[1], "is_target": True, "id": "one"},
                              {"lat": target2[0], "lon": target2[1], "is_target": True, "id": "two"}])
        original_distance = runner.distance_m
        remaining = runner._exit_remaining
        was_branch, hits = False, []
        prior = e._point(runner.position)
        for _ in range(800):
            previous_hits = len(runner.targets_visited)
            runner.step()
            current = e._point(runner.position)
            self.assertLessEqual(e.haversine_distance(*prior, *current), 4.2*0.95+0.02)
            prior = current
            if len(runner.targets_visited) > previous_hits:
                crossed=[]
                while not runner.events.empty():
                    event=runner.events.get_nowait()
                    if event['type']=='target_reached':crossed.append((event['lat'],event['lon']))
                hits.extend(crossed)
                for point in crossed:
                    self.assertLessEqual(e.haversine_distance(*point,*current),3.8*.95+1e-4)
            if runner.state == e.RunnerState.CRUISING_DETOUR:
                if not was_branch:
                    self.assertGreaterEqual(runner.distance_m-original_distance, remaining-1e-4)
                was_branch = True
            elif was_branch:
                break
        self.assertTrue(was_branch)
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)
        self.assertEqual(runner.targets_visited, ["one", "two"])
        self.assertEqual(hits, [target1, target2])

    def test_behind_exit_waits_until_next_lap(self):
        runner = self.runner()
        for _ in range(100):
            runner.step()
        old_laps = runner.laps
        behind = runner._lap.points[10]
        runner.submit_detour([{"lat": behind[0], "lon": behind[1], "is_target": True}])
        self.assertGreater(runner._exit_remaining, runner._lap.length/3)
        for _ in range(300):
            runner.step()
            if runner.state == e.RunnerState.CRUISING_DETOUR:
                break
        self.assertGreater(runner.laps, old_laps)

    def test_fatigue_and_curve_slowdown(self):
        self.assertEqual(e.BiomechanicalRunner.fatigue_factor(0), 1.02)
        self.assertEqual(e.BiomechanicalRunner.fatigue_factor(600), 1.02)
        self.assertAlmostEqual(e.BiomechanicalRunner.fatigue_factor(1800), 0.93)
        self.assertGreater(e.BiomechanicalRunner.fatigue_factor(2400), 0.93)
        runner = self.runner()
        runner.elapsed_s = 20
        with patch.object(runner._rng, "gauss", return_value=0):
            runner.position["curvature"] = 0
            straight = runner._speed(0.95)
            runner.position["curvature"] = 0.02
            curved = runner._speed(0.95)
        self.assertAlmostEqual(curved/straight, 0.92)

    def test_background_cadence_and_shutdown(self):
        bridge = e.IOSDeviceBridge(sandbox=True)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(1))
        self.addCleanup(bridge.close)
        self.addCleanup(runner.close)
        runner.load_track(circle())
        runner.lock_start()
        runner.start_cruising()
        time.sleep(2.05)
        self.assertTrue(runner.daemon)
        self.assertAlmostEqual(runner.elapsed_s, 1.9, delta=0.2)
        self.assertGreater(runner.distance_m, 0)
        runner.request_safe_stop()
        runner.close()
        self.assertFalse(runner.is_alive())
        self.assertEqual(runner.state, e.RunnerState.SAFE_STOPPED)

    def test_goal_waits_for_committed_branch_targets(self):
        runner = self.runner()
        for _ in range(10):
            runner.step()
        runner.target_m = runner.distance_m+1
        target = e._destination(ORIGIN, 120, 100)
        runner.submit_detour([{"lat": target[0], "lon": target[1], "is_target": True, "id": "required"}])
        for _ in range(500):
            runner.step()
            if runner.state == e.RunnerState.SAFE_STOPPED:
                break
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)
        self.assertEqual(runner.targets_visited, ["required"])
        self.assertGreater(runner.distance_m, runner.target_m)
        self.assertEqual(runner.snapshot()["stop_reason"], "target")
        self.assertTrue(runner.snapshot()["target_walking"])

    def test_invalid_detour_does_not_mutate_session(self):
        runner = self.runner()
        before = (runner.exit_idx, runner.entry_idx, runner._pending_detour)
        with self.assertRaises(ValueError):
            runner.submit_detour([{"lat": 80, "lon": 10, "is_target": True}])
        self.assertEqual(before, (runner.exit_idx, runner.entry_idx, runner._pending_detour))
        runner.step()
        self.assertGreater(runner.speed_mps, 0)

    def test_target_already_on_loop_gets_one_critical_tick_before_motion_resumes(self):
        runner = self.runner()
        runner._connector = None
        runner._loop_s = runner._lap.cumulative[5]
        runner.position = runner._lap.sample(runner._loop_s)
        target = runner._lap.points[5]
        runner.submit_detour([{"lat": target[0], "lon": target[1], "is_target": True}])
        with patch.object(runner.bridge, "set_location", wraps=runner.bridge.set_location) as send:
            runner.step()
        self.assertEqual(runner.speed_mps, 0)
        send.assert_called_once_with(*target, critical=True)
        self.assertEqual(runner.targets_visited, [0])
        self.assertEqual(runner.entry_idx, 5)
        runner.step()
        self.assertGreater(runner.speed_mps, 0)
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)

    def test_repeated_target_controls_and_order(self):
        runner = self.runner()
        target = e._destination(ORIGIN, 120, 90)
        runner.submit_detour([{"lat": target[0], "lon": target[1], "is_target": True, "id": 1},
                              {"lat": target[0], "lon": target[1], "is_target": True, "id": 2}])
        for _ in range(500):
            runner.step()
            self.assertGreater(runner.speed_mps, 0)
            if len(runner.targets_visited) == 2:
                break
        self.assertEqual(runner.targets_visited, [1, 2])


class MultiLapTests(EngineCase):
    def test_one_to_eight_metre_corridors_contain_start_and_eight_laps(self):
        for width in (1.0, 2.0, 8.0):
            with self.subTest(width=width):
                runner = self.runner(target_km=10, track_width_m=width)
                self.assertTrue(runner._corridor.within(runner.start_point, width/2-0.08))
                previous = e._point(runner.position)
                while runner.laps < 8:
                    runner.step()
                    point = e._point(runner.position)
                    self.assertTrue(runner._corridor.within(point, width/2-0.08))
                    # Check rendered chords too, rather than only their endpoints.
                    for t in (0.25, 0.5, 0.75):
                        self.assertTrue(runner._corridor.within(e._interpolate(previous, point, t), width/2))
                    self.assertLessEqual(e.haversine_distance(*previous, *point), runner.speed_mps*0.95+1e-4)
                    previous = point

    def test_stadium_dimensions_rotation_and_deformation_are_bounded(self):
        for heading in (0, 37, 135):
            anchors = e.TrackGeometry.stadium_anchors(ORIGIN, 82, 44.4, heading)
            geometry = e.TrackGeometry(anchors)
            self.assertAlmostEqual(geometry.length_m, 2*82+2*math.pi*44.4, delta=0.5)
            frame = e._Projection(ORIGIN)
            for point in anchors:
                x, y = frame.xy(point)
                theta = math.radians(heading)
                across = x*math.cos(theta)-y*math.sin(theta)
                along = x*math.sin(theta)+y*math.cos(theta)
                self.assertAlmostEqual(math.hypot(across, max(0, abs(along)-41)), 44.4, delta=1e-5)
        runner = self.runner()
        with patch.object(runner._geometry_rng, 'gauss', side_effect=[100, -100]*len(runner._anchors)):
            lap = runner._make_lap()
        self.assertTrue(all(runner._corridor.within(p, 0.501) for p in lap.points))

    def test_infield_shortcuts_rejected_but_perimeter_detours_work(self):
        runner = self.runner(target_km=5)
        for _ in range(40):
            runner.step()
        with self.assertRaisesRegex(ValueError, 'infield'):
            runner.submit_detour([{'lat': ORIGIN[0], 'lon': ORIGIN[1], 'is_target': True}])
        self.assertIsNone(runner._pending_detour)
        # Both endpoints are outside, but the straight road through the field
        # is unsafe too: inspecting only control-point validity would miss it.
        west, east = e._destination(ORIGIN, 270, 90), e._destination(ORIGIN, 90, 90)
        with self.assertRaisesRegex(ValueError, 'infield'):
            runner.submit_detour([{'lat': p[0], 'lon': p[1], 'is_target': True} for p in (west, east)])
        north = e._destination(ORIGIN, 0, 100)
        runner.submit_detour([{'lat': north[0], 'lon': north[1], 'is_target': True}])
        self.assertTrue(all(not runner._corridor.crosses_infield(p) for p in runner._pending_detour['route'].points))

    def test_invalid_width_cannot_replace_an_existing_session(self):
        runner = self.runner()
        old_session = runner.session_id
        for width in (0.9, 8.1, True, float('nan')):
            with self.assertRaises(ValueError):
                runner.load_track(circle(), track_width_m=width)
            self.assertEqual(runner.session_id, old_session)

    def test_live_merge_rejection_keeps_loop_motion_running(self):
        runner = self.runner()
        while runner._connector is not None:
            runner.step()
        target = e._destination(ORIGIN, 110, 100)
        runner.submit_detour([{'lat': target[0], 'lon': target[1], 'is_target': True}])
        runner._exit_remaining = 0
        distance = runner.distance_m
        with patch.object(runner, '_build_detour', side_effect=ValueError('infield')):
            runner.step()
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)
        self.assertGreater(runner.distance_m, distance)
        self.assertIsNone(runner._pending_detour)
        self.assertEqual(runner.last_error, 'detour_crosses_infield')

    def test_rebuilt_lap_rejects_unsafe_pending_merge_without_stopping(self):
        runner = self.runner()
        for _ in range(50):
            runner.step()
        target = e._destination(ORIGIN, 110, 100)
        runner.submit_detour([{'lat': target[0], 'lon': target[1], 'is_target': True}])
        with patch.object(runner, '_build_detour', side_effect=ValueError('infield')):
            runner._next_lap(runner.elapsed_s+1, runner.distance_m+1)
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)
        self.assertEqual(runner.current_lap, 2)
        self.assertIsNone(runner._pending_detour)
        self.assertIsNone(runner.exit_idx)
        self.assertEqual(runner.last_error, 'detour_crosses_infield')

    def test_overtaking_cosine_endpoints_hold_and_return(self):
        shape = e._LateralMotion.envelope
        self.assertEqual(shape(0, 12), 0)
        self.assertAlmostEqual(shape(2.5, 12), 0.5)
        self.assertEqual(shape(5, 12), 1)
        self.assertEqual(shape(17, 12), 1)
        self.assertAlmostEqual(shape(19.5, 12), 0.5)
        self.assertEqual(shape(22, 12), 0)
        for t in (0, 5, 17, 22):
            slope = (shape(t+1e-5, 12)-shape(t-1e-5, 12))/2e-5
            self.assertAlmostEqual(slope, 0, delta=1e-5)

    def test_ou_distance_sampling_is_independent_of_step_partition(self):
        def simulate(step):
            motion = e._LateralMotion(random.Random(3))
            values, starts = [], []
            for stop in range(1, 601):
                while motion.distance_m < stop-1e-7:
                    part = min(step, stop-motion.distance_m, motion.boundary_m())
                    changes = motion.advance(part, part/2.8)
                    if "overtake_started" in changes:
                        starts.append(dict(motion.event))
                value = motion.sample()
                self.assertTrue(-0.4-1e-6 <= value <= 2.2+1e-6)
                values.append(value)
            return values, starts
        a, starts = simulate(0.25)
        b, other_starts = simulate(0.125)
        self.assertEqual(len(starts), len(other_starts))
        self.assertLess(max(abs(x-y) for x, y in zip(a, b)), 1e-6)
        self.assertGreater(len(starts), 3)
        previous = 0
        for event in starts:
            self.assertTrue(90-1e-6 <= event["start_m"]-previous <= 180+1e-6)
            self.assertTrue(1.2 <= event["amplitude_m"] <= 2.2)
            self.assertTrue(10 <= event["hold_s"] <= 15)
            previous = event["start_m"]

    def test_eight_laps_have_distinct_shapes_continuous_seams_and_exact_splits(self):
        runner = self.runner(target_km=10)
        controls = list(runner._anchors)
        shapes, factors, seam_errors = [], [runner.lap_pace_factor], []
        first_crossing = None
        original = runner._next_lap
        def next_lap(crossing_time, crossing_distance):
            point, offset = e._point(runner.position), runner.offset_m
            old_lap = runner._lap
            s, t = runner._lateral.distance_m, runner._lateral.time_s
            original(crossing_time, crossing_distance)
            node, value = runner._loop_position(0, 0, 2.8)
            seam_errors.append(e.haversine_distance(*point, *e._point(node)))
            self.assertEqual(offset, value)
            self.assertEqual((s, t), (runner._lateral.distance_m, runner._lateral.time_s))
            def shifted(lap, distance):
                sample = lap.sample(distance)
                return e._destination(e._point(sample), sample["bearing"]+90, offset)
            incoming = e.calculate_bearing(*shifted(old_lap, old_lap.length-0.1), *point)
            outgoing = e.calculate_bearing(*point, *shifted(runner._lap, 0.1))
            self.assertLess(abs(e._angle_delta(incoming, outgoing)), 3.0)
            factors.append(runner.lap_pace_factor)
            shapes.append(tuple(runner._lap.control_points))
            for a, b in zip(controls, runner._lap.control_points):
                self.assertLessEqual(e.haversine_distance(*a, *b), 0.50001)
            gaps = [b-a for a, b in zip(runner._lap.cumulative, runner._lap.cumulative[1:])]
            self.assertLess(max(abs(gap-0.5) for gap in gaps[:-1]), 0.0005)
            self.assertLessEqual(gaps[-1], 0.50001)
        with patch.object(runner, "_next_lap", side_effect=next_lap):
            for _ in range(2200):
                old = e._point(runner.position)
                before = runner.distance_m
                state = runner.step()
                if first_crossing is None and runner._lap_start_time is not None:
                    first_crossing = runner._lap_start_time
                self.assertLessEqual(e.haversine_distance(*old, *e._point(runner.position)), runner.distance_m-before+1e-5)
                if runner.laps == 8:
                    break
        self.assertEqual(runner.laps, 8)
        self.assertEqual(runner.current_lap, 9)
        self.assertEqual(len(runner.lap_splits), 8)
        self.assertEqual(len(set(shapes)), 8)
        self.assertLess(max(seam_errors), 0.0001)
        self.assertGreater(max(runner.lap_splits)-min(runner.lap_splits), 1)
        self.assertTrue(all(0.96 <= factor <= 1.04 for factor in factors))
        self.assertEqual(controls, runner._anchors)
        self.assertEqual(runner.lateral_offset, state["lateral_offset"])
        self.assertAlmostEqual(sum(runner.lap_splits), runner._lap_start_time-first_crossing)
        self.assertAlmostEqual(state["last_lap"]["pace_s_per_km"], runner.lap_splits[-1]*1000/runner._lap_distances[-1])

    def test_detour_during_overtaking_has_no_exit_or_entry_teleport(self):
        runner = self.runner(target_km=5)
        for _ in range(200):
            runner.step()
            if runner.offset_m > 1.2:
                break
        self.assertGreater(runner.offset_m, 1.2)
        target = e._destination(ORIGIN, 130, 95)
        runner.submit_detour([{"lat": target[0], "lon": target[1], "is_target": True}])
        was_branch = False
        for _ in range(600):
            old, distance = e._point(runner.position), runner.distance_m
            runner.step()
            self.assertLessEqual(e.haversine_distance(*old, *e._point(runner.position)), runner.distance_m-distance+1e-5)
            was_branch |= runner.state == e.RunnerState.CRUISING_DETOUR
            if was_branch and runner.state == e.RunnerState.CRUISING_LOOP:
                break
        self.assertTrue(was_branch)
        self.assertEqual(runner.targets_visited, [0])
        self.assertEqual(runner.state, e.RunnerState.CRUISING_LOOP)

    def test_trace_is_bounded_incremental_copied_and_session_scoped(self):
        runner = self.runner()
        for i in range(3610):
            runner.elapsed_s = i
            runner._record_trace()
        full = runner.trace_since()
        self.assertEqual(len(full["points"]), 3600)
        self.assertTrue(full["reset"])
        cursor = full["sequence"]
        full["points"][-1]["lat"] = 0
        self.assertNotEqual(runner.trace_since(cursor-1, runner.session_id)["points"][0]["lat"], 0)
        self.assertFalse(runner.trace_since(cursor, runner.session_id)["reset"])
        self.assertEqual(runner.trace_since(cursor, runner.session_id)["points"], [])
        self.assertTrue(runner.trace_since(1, runner.session_id)["reset"])
        old_session = runner.session_id
        runner.request_safe_stop()
        runner.step()
        runner.load_track(circle())
        self.assertNotEqual(old_session, runner.session_id)
        self.assertEqual(runner.lap_splits, [])
        self.assertEqual(runner.trace_since(cursor, old_session)["points"], [])
        for value in (True, -1, 0.5, "0"):
            with self.assertRaises(ValueError):
                runner.trace_since(value)


class BridgeTests(EngineCase):
    def test_usb_discovery_waits_for_service_preparation(self):
        preparing, prepared = threading.Event(), threading.Event()

        def prepare():
            preparing.set()
            if not prepared.wait(2):
                raise TimeoutError("test did not release the service check")

        bridge = e.IOSDeviceBridge(sandbox=True, before_discovery=prepare)
        self.addCleanup(bridge.close)
        usbmux = MagicMock()
        usbmux.list_devices.return_value = [SimpleNamespace(serial="test-phone")]

        async def exercise():
            discovery = asyncio.create_task(bridge._select_device())
            try:
                self.assertTrue(await asyncio.to_thread(preparing.wait, 1))
                usbmux.list_devices.assert_not_called()
            finally:
                prepared.set()
            self.assertEqual(await discovery, "test-phone")

        with patch.object(e.importlib, "import_module", return_value=usbmux):
            asyncio.run(exercise())

    def test_frozen_mount_uses_validated_cache_or_bundled_image_without_downloading(self):
        bridge = e.IOSDeviceBridge(sandbox=True)
        self.addCleanup(bridge.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root/"home"
            cached = home/"Xcode_iOS_DDI_Personalized"
            bundled = root/e.ddi_support.DDI_ROOT/"Cryptex"
            assets = {}
            for variant, folder in (("Personalized", cached), ("Cryptex", bundled)):
                folder.mkdir(parents=True)
                assets[variant] = {}
                for name in e.ddi_support.DDI_ASSETS[variant]:
                    data = name.encode()
                    local_name = "Image.trustcache" if variant == "Personalized" and name.endswith(".trustcache") else name
                    (folder/local_name).write_bytes(data)
                    assets[variant][name] = (len(data), hashlib.sha256(data).hexdigest())

            mounter = MagicMock()
            image = mounter.return_value.__aenter__.return_value
            image.mount = AsyncMock()
            image.raise_if_cannot_mount = AsyncMock()
            cryptex = MagicMock()
            cryptex.return_value.auto_install_ddi = AsyncMock()
            modules = {
                "pymobiledevice3.common": SimpleNamespace(get_home_folder=lambda: home),
                "pymobiledevice3.services.mobile_image_mounter": SimpleNamespace(
                    uses_cryptex_image=lambda provider: provider == "ios27", PersonalizedImageMounter=mounter),
                "pymobiledevice3.services.cryptexd": SimpleNamespace(CryptexdService=cryptex),
            }
            with patch.object(e.sys, "_MEIPASS", str(root), create=True), patch.object(e.sys, "frozen", True, create=True), \
                 patch.object(e.importlib, "import_module", side_effect=modules.__getitem__), \
                 patch.object(e.ddi_support, "DDI_ASSETS", assets), \
                 patch.object(e.ddi_support, "prepare_image") as download:
                asyncio.run(bridge._mount_developer_image("ios26"))
                asyncio.run(bridge._mount_developer_image("ios27"))
                (bundled/"Image.dmg.root_hash").unlink()
                with self.assertRaisesRegex(ValueError, "Cryptex/Image.dmg.root_hash"):
                    asyncio.run(bridge._mount_developer_image("ios27"))
            download.assert_not_called()
            image.mount.assert_awaited_once_with(cached/"Image.dmg", cached/"BuildManifest.plist",
                                                 cached/"Image.trustcache")
            cryptex.return_value.auto_install_ddi.assert_awaited_once_with(restore_dir=bundled)

    def fake_driver(self, *, legacy=False, clear_fails=False, fail_dvt=False):
        calls = []
        class Resource:
            def __init__(self, name):
                self.name = name
            async def __aenter__(self):
                calls.append("open:"+self.name)
                if fail_dvt and self.name == "dvt":
                    raise OSError("DVT refused")
                return self
            async def __aexit__(self, *args):
                calls.append("close:"+self.name)
                return None
        class Lockdown:
            product_version = "27.0"
            udid = "test-udid"
            async def close(self):
                calls.append("close:lockdown")
        class Preferred(Resource):
            udid = "test-udid"

            def __init__(self, **kwargs):
                super().__init__("tunnel")
        class DVT(Resource):
            def __init__(self, provider):
                super().__init__("dvt")
        class Simulator(Resource):
            def __init__(self, provider):
                super().__init__("simulator")
            async def set(self, lat, lon):
                calls.append(("set", lat, lon))
            async def clear(self):
                calls.append("clear")
                if clear_fails:
                    raise OSError("reset refused")
        class Diagnostics(Resource):
            def __init__(self, provider):
                super().__init__("diagnostics")
            async def get_battery(self):
                return {"CurrentCapacity": 15, "MaxCapacity": 100, "IsCharging": False}
        class AlreadyMountedError(Exception):
            pass
        async def lockdown(**kwargs):
            calls.append("lockdown")
            return Lockdown()
        class ImageMounter(Resource):
            def __init__(self, provider):
                super().__init__("image")

            async def raise_if_cannot_mount(self):
                calls.append("mount")
                raise AlreadyMountedError()
        modules = {
            "pymobiledevice3.usbmux": SimpleNamespace(list_devices=lambda: [SimpleNamespace(serial="test-udid")]),
            "pymobiledevice3.lockdown": SimpleNamespace(create_using_usbmux=lockdown),
            "pymobiledevice3.remote.rsd_tunnel": SimpleNamespace(PreferredRsdTunnel=Preferred),
            "pymobiledevice3.services.mobile_image_mounter": SimpleNamespace(
                uses_cryptex_image=lambda provider: True, PersonalizedImageMounter=ImageMounter),
            "pymobiledevice3.exceptions": SimpleNamespace(AlreadyMountedError=AlreadyMountedError),
            "pymobiledevice3.services.dvt.instruments.location_simulation": SimpleNamespace(LocationSimulation=Simulator),
            "pymobiledevice3.services.diagnostics": SimpleNamespace(DiagnosticsService=Diagnostics),
        }
        if legacy:
            modules["pymobiledevice3.services.dvt.dvt_secure_socket_proxy"] = SimpleNamespace(DvtSecureSocketProxyService=DVT)
        else:
            modules["pymobiledevice3.services.dvt.instruments.dvt_provider"] = SimpleNamespace(DvtProvider=DVT)
        def importer(name):
            if name not in modules:
                raise ModuleNotFoundError(name)
            return modules[name]
        return importer, calls

    def test_modern_and_legacy_dvt_mount_battery_and_cleanup(self):
        for legacy in (False, True):
            bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
            self.addCleanup(bridge.close)
            importer, calls = self.fake_driver(legacy=legacy)
            async def exercise():
                await bridge._open_session()
                await bridge._read_battery()
                self.assertEqual(bridge.battery_percent, 15)
                self.assertTrue(bridge.low_battery)
                await bridge._send((1, *ORIGIN))
                self.assertNotIn("close:simulator", calls)
                await bridge._disconnect(clear=True)
                self.assertLess(calls.index("close:simulator"), calls.index("close:dvt"))
                self.assertLess(calls.index("close:dvt"), calls.index("close:tunnel"))
                self.assertLess(calls.index("close:lockdown"), calls.index("open:tunnel"))
                self.assertLess(calls.index("mount"), calls.index("open:dvt"))
                await bridge._open_session()
                await bridge._disconnect()
            with patch.object(e.importlib, "import_module", side_effect=importer):
                asyncio.run(exercise())
            bridge.close()  # Release this fixture's exclusive device before the next driver variant.
            self.assertEqual(calls.count("mount"), 2)
            self.assertEqual(bridge.udid, "test-udid")

    def test_partial_dvt_failure_releases_tunnel(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        importer, calls = self.fake_driver(fail_dvt=True)
        with patch.object(e.importlib, "import_module", side_effect=importer):
            with self.assertRaisesRegex(OSError, "DVT handshake.*DVT refused"):
                asyncio.run(bridge._open_session())
        self.assertIn("close:dvt", calls)
        self.assertIn("close:tunnel", calls)
        self.assertIn("close:lockdown", calls)

    def test_ddi_timeout_names_the_stage_and_never_opens_dvt(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False, mount_timeout=.02)
        self.addCleanup(bridge.close)
        importer, calls = self.fake_driver()

        async def slow_mount(provider):
            await asyncio.sleep(60)
        with patch.object(e.importlib, "import_module", side_effect=importer), \
             patch.object(bridge, "_mount_developer_image", side_effect=slow_mount):
            with self.assertRaisesRegex(e.DeviceConnectionError, "iOS 27.0 DDI mount over RSD timed out"):
                asyncio.run(bridge._open_session())
        self.assertNotIn("open:dvt", calls)
        self.assertIn("close:tunnel", calls)

    def test_dvt_disconnect_replays_last_position_before_announcing_reconnect(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        modes = []

        async def exercise():
            disconnect = asyncio.Event()
            sent = asyncio.Event()
            opens = 0

            class Simulator:
                async def set(self, lat, lon):
                    modes.append((bridge.mode, (lat, lon)))
                    sent.set()

                async def clear(self):
                    pass

            async def opened():
                nonlocal opens
                opens += 1
                bridge._simulator = Simulator()
                if opens == 1:
                    bridge._dvt_disconnect_task = asyncio.create_task(disconnect.wait())

            async def wait_connected():
                await sent.wait()
                while bridge.mode != e.BridgeMode.CONNECTED:
                    await asyncio.sleep(.005)

            with patch.object(bridge, "_open_session", side_effect=opened), self.assertLogs(e.LOGGER, level="WARNING"):
                worker = asyncio.create_task(bridge._serve())
                try:
                    bridge._latest = (1, *ORIGIN)
                    await asyncio.wait_for(wait_connected(), 1)
                    sent.clear()
                    self.assertEqual(bridge.mode, e.BridgeMode.CONNECTED)
                    self.assertEqual(bridge.snapshot()["pending_samples"], 0)
                    self.assertIsNone(bridge._take_sample())
                    disconnect.set()
                    await asyncio.wait_for(wait_connected(), 2)
                    self.assertEqual(opens, 2)
                    self.assertEqual(bridge.mode, e.BridgeMode.CONNECTED)
                finally:
                    bridge._shutdown.set()
                    await worker

        asyncio.run(exercise())
        self.assertEqual(modes, [(e.BridgeMode.CONNECTING, ORIGIN), (e.BridgeMode.RECONNECTING, ORIGIN)])

    def test_repeated_connection_failures_back_off_even_after_success(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        bridge._ever_connected = True
        bridge._last_clear_succeeded = True
        delays = []

        async def retry(delay):
            delays.append(delay)
            if len(delays) == 4:
                bridge._shutdown.set()

        with patch.object(bridge, "_open_session", new=AsyncMock(side_effect=OSError("device disconnected"))), \
             patch.object(bridge, "_wait_to_retry", side_effect=retry), self.assertLogs(e.LOGGER, level="WARNING"):
            asyncio.run(bridge._serve())
        self.assertEqual(delays, [2, 4, 8, 8])

    def test_explicit_clear_interrupts_an_in_progress_reconnect(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        bridge._ever_connected = bridge._location_dirty = True
        connecting, cancelled, cleared = threading.Event(), threading.Event(), threading.Event()

        async def clear():
            cleared.set()

        async def opened():
            if not connecting.is_set():
                connecting.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
            bridge._simulator = SimpleNamespace(clear=clear)

        with patch.object(bridge, "_open_session", side_effect=opened):
            bridge.connect()
            self.assertTrue(connecting.wait(1))
            self.assertTrue(bridge.clear_location(timeout=1))
            self.assertTrue(cancelled.is_set())
            self.assertTrue(cleared.is_set())
            bridge.close()

    def test_reset_failure_is_not_reported_as_success(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        importer, _ = self.fake_driver(clear_fails=True)
        async def exercise():
            await bridge._open_session()
            bridge._ever_connected = True
            self.assertFalse(await bridge._disconnect(clear=True))
        with patch.object(e.importlib, "import_module", side_effect=importer):
            asyncio.run(exercise())
        self.assertIn("reset failed", bridge.last_error)

    @unittest.skipIf(os.name == "nt", "POSIX process-group lifecycle test")
    def test_closes_only_owned_child_process(self):
        bridge = e.IOSDeviceBridge(sandbox=True)
        self.addCleanup(bridge.close)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        bridge._children.append(child)
        bridge.close()
        self.assertIsNotNone(child.poll())

    def test_frozen_build_launches_only_the_private_daemon_entry(self):
        bridge = e.IOSDeviceBridge()
        self.addCleanup(bridge.close)
        child = SimpleNamespace(poll=lambda: 0)
        with patch.object(sys, "frozen", True, create=True), patch.object(e.subprocess, "Popen", return_value=child) as spawn:
            bridge._spawn_tunneld()
            command = spawn.call_args.args[0]
            self.assertEqual(command, [sys.executable, "--parent-pid", str(os.getpid()), "--piggy-tunneld", "--host", "127.0.0.1",
                                       "--port", "49151", "--protocol", "tcp"])
            self.assertFalse(spawn.call_args.kwargs["shell"])

    def test_deployed_tunneld_command_uses_argument_list_and_loopback(self):
        bridge = e.IOSDeviceBridge(tunneld_command=["driver-python", "-m", "pymobiledevice3", "remote", "tunneld"])
        self.addCleanup(bridge.close)
        child = SimpleNamespace(poll=lambda: 0)
        with patch.object(e.subprocess, "Popen", return_value=child) as spawn:
            bridge._spawn_tunneld()
            args, kwargs = spawn.call_args
            self.assertEqual(args[0][:5], [sys.executable, str(Path(e.__file__).with_name("main.py")),
                                          "--parent-pid", str(os.getpid()), "--piggy-tunnel-command"])
            self.assertEqual(args[0][5:10], ["driver-python", "-m", "pymobiledevice3", "remote", "tunneld"])
            self.assertEqual(args[0][-6:], ["--host", "127.0.0.1", "--port", "49151", "--protocol", "tcp"])
            self.assertFalse(kwargs["shell"])

    def test_sandbox_no_dependency_and_input_validation(self):
        with e.IOSDeviceBridge(sandbox=True) as bridge:
            bridge.set_location(*ORIGIN)
            self.assertEqual(bridge.snapshot()["mode"], "SANDBOX_MODE")
            self.assertEqual(bridge.snapshot()["sandbox_updates"], 1)
            self.assertIsNone(bridge._thread)
            with self.assertRaises(ValueError):
                bridge.set_location(100, 0)
        self.assertEqual(bridge.mode, e.BridgeMode.CLOSED)

    def test_missing_dependency_automatically_falls_back(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        real_import = importlib.import_module
        def missing(name):
            if name.startswith("pymobiledevice3"):
                raise ModuleNotFoundError("test: no pymobiledevice3")
            return real_import(name)
        with patch.object(e.importlib, "import_module", side_effect=missing):
            bridge.set_location(*ORIGIN)
            deadline = time.monotonic()+1
            while bridge.last_error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(bridge.mode, e.BridgeMode.SANDBOX_MODE)
            self.assertIn("ModuleNotFoundError", bridge.last_error)

    def test_usb_failure_reconnects_and_injects_safe_latest(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        delivered, connection_times = [], []
        class Simulator:
            async def set(self, lat, lon):
                if len(connection_times) == 1 and delivered:
                    raise OSError("USB removed")
                delivered.append((lat, lon))
            async def clear(self):
                return None
        async def opened():
            connection_times.append(time.monotonic())
            bridge._simulator = Simulator()
        with patch.object(bridge, "_open_session", side_effect=opened):
            bridge.set_location(*ORIGIN)
            deadline = time.monotonic()+2
            while not delivered and time.monotonic() < deadline:
                time.sleep(0.01)
            latest = e._destination(ORIGIN, 40, 1.5)
            sequence = bridge.set_location(*latest)
            # Opening the replacement session precedes its asynchronous set()
            # acknowledgement. Wait for delivery and CONNECTED, rather than
            # racing the worker after it merely entered _open_session().
            while (len(connection_times) < 2 or bridge.snapshot()["delivered_sequence"] < sequence
                   or bridge.mode != e.BridgeMode.CONNECTED) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(connection_times), 2)
            self.assertEqual(delivered[-1], latest)
            self.assertGreaterEqual(connection_times[1]-connection_times[0], 0.19)
            self.assertEqual(bridge.mode, e.BridgeMode.CONNECTED)
            bridge.close()


if __name__ == "__main__":
    unittest.main()
