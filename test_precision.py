"""Checkpoint delivery and input-denoising regressions; no device is contacted."""
import asyncio
from collections import deque
import copy
import math
import random
import unittest
from unittest.mock import patch

import engine as e


ORIGIN = (31.2304, 121.4737)


class PrecisionTests(unittest.TestCase):
    def runner(self, *, physical=False, seed=41):
        clock = [0.0]
        bridge = e.IOSDeviceBridge(sandbox=not physical, clock=lambda: clock[0])
        connection = patch.object(bridge, "connect", return_value={})
        connection.start()
        self.addCleanup(connection.stop)
        self.addCleanup(bridge.close)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(seed), clock=lambda: clock[0])
        self.addCleanup(runner.close)
        controls = [e._destination(ORIGIN, -i*360/16, 60) for i in range(16)]
        runner.load_track(controls, target_km=10)
        runner.lock_start(background=False)
        runner.start_cruising(background=False)
        return runner, bridge, clock

    def test_close_controls_are_filtered_without_mutating_input(self):
        frame = e._Projection(ORIGIN)
        raw = [dict(zip(("lat", "lon"), frame.ll(p)))
               for p in ((0, 0), (1, .2), (20, 0), (20, 20), (0, 20), (.5, 1))]
        original = copy.deepcopy(raw)
        geometry = e.TrackGeometry(raw)
        self.assertEqual(raw, original)
        self.assertEqual(len(geometry.anchors), 4)
        gaps = [e.haversine_distance(*e._point(a), *e._point(b))
                for a, b in zip(geometry.anchors, geometry.anchors[1:]+geometry.anchors[:1])]
        self.assertGreater(min(gaps), 3)

    def test_laplacian_reduces_an_outlier_and_keeps_open_endpoints(self):
        frame = e._Projection(ORIGIN)
        controls = [frame.ll(p) for p in ((0, 0), (10, 2), (20, 0))]
        prepared = e.TrackGeometry.prepare_anchors(controls, closed=False)
        self.assertEqual(e._point(prepared[0]), controls[0])
        self.assertEqual(e._point(prepared[-1]), controls[-1])
        _, corrected_y = frame.xy(e._point(prepared[1]))
        self.assertAlmostEqual(corrected_y, 1.5, places=5)

    def test_shallow_zigzag_on_a_closed_straight_gets_the_laplacian_weight(self):
        frame = e._Projection(ORIGIN)
        controls = [frame.ll(p) for p in ((0, 0), (10, 2), (20, 0), (20, 20), (0, 20))]
        prepared = e.TrackGeometry.prepare_anchors(controls)
        # Orientation is canonicalized, so find the control nearest the outlier.
        corrected = min(prepared, key=lambda p: e.haversine_distance(*controls[1], *e._point(p)))
        self.assertAlmostEqual(frame.xy(e._point(corrected))[1], 1.5, places=5)

    def test_protected_close_targets_survive_filter_smoothing_and_resampling(self):
        frame = e._Projection(ORIGIN)
        raw = [dict(zip(("lat", "lon"), frame.ll(p)))
               for p in ((0, 0), (10, 3), (10.8, 3.2), (20, 0), (20, 20), (0, 20))]
        raw[1].update(is_target=True, id="one")
        raw[2].update(is_target=True, id="two")
        original = copy.deepcopy(raw)
        for closed in (False, True):
            with self.subTest(closed=closed):
                geometry = e.TrackGeometry(raw, closed=closed)
                marked = [p for p in geometry.anchors if p["is_target"]]
                self.assertEqual({e._point(p) for p in marked}, {e._point(raw[1]), e._point(raw[2])})
                samples = {e._point(p) for p in geometry.points}
                self.assertIn(e._point(raw[1]), samples)
                self.assertIn(e._point(raw[2]), samples)
        self.assertEqual(raw, original)

    def test_coincident_normal_and_target_controls_do_not_lose_target_metadata(self):
        frame = e._Projection(ORIGIN)
        target = dict(zip(("lat", "lon"), frame.ll((10, 1))))
        target.update(is_target=True, id="fixed")
        controls = [frame.ll((0, 0)), target, e._point(target), frame.ll((20, 0))]
        geometry = e.TrackGeometry(controls, closed=False)
        marked = [p for p in geometry.anchors if p["is_target"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual(e._point(marked[0]), e._point(target))
        self.assertEqual(marked[0]["id"], "fixed")
        self.assertIn(e._point(target), {e._point(p) for p in geometry.points})

    def test_preview_and_runner_share_one_preprocessing_pass(self):
        runner, _, _ = self.runner()
        raw = [e._destination(ORIGIN, -i*360/16, 60) for i in range(16)]
        preview = e.TrackGeometry(raw)
        self.assertEqual(runner._center.points, [e._point(p) for p in preview.points])
        controls = list(runner._anchors)
        with patch.object(e.TrackGeometry, "prepare_anchors", wraps=e.TrackGeometry.prepare_anchors) as prepare:
            for _ in range(3):
                runner._make_lap()
        prepare.assert_not_called()
        self.assertEqual(runner._anchors, controls)

    def test_checkpoint_factor_is_smooth_and_does_not_change_the_base_model(self):
        runner, _, _ = self.runner()
        runner._detour_targets = deque([dict(lat=ORIGIN[0], lon=ORIGIN[1], s_m=20, id=0)])
        factors = []
        for distance in (12, 10, 9, 5, 1, 0):
            runner.position.update(zip(("lat", "lon"), e._destination(ORIGIN, 0, distance)))
            factors.append(runner._checkpoint_speed_factor())
        self.assertAlmostEqual(factors[0], 1)
        self.assertAlmostEqual(factors[1], 1)
        self.assertAlmostEqual(factors[-1], .73)
        self.assertEqual(factors, sorted(factors, reverse=True))
        runner._detour_targets.clear()
        self.assertEqual(runner._checkpoint_speed_factor(), 1)

    def test_tick_sends_checkpoint_sample_and_resumes_without_catch_up(self):
        for dt in (.2, .95, 1.9):
            with self.subTest(dt=dt):
                runner, bridge, _ = self.runner()
                target = e._destination(ORIGIN, 120, 100)
                runner.submit_detour([dict(lat=target[0], lon=target[1], is_target=True, id=n) for n in (1, 2)])
                with patch.object(bridge, "set_location", wraps=bridge.set_location) as send:
                    for _ in range(math.ceil(600/dt)):
                        old_distance = runner.distance_m
                        runner.step(dt)
                        if runner.targets_visited:
                            break
                    else:
                        self.fail("checkpoint was not reached")
                    delivered = bridge._latest[1:]
                    error = e.haversine_distance(*target, *delivered)
                    self.assertGreater(error, 0)
                    self.assertLessEqual(error, .080001)
                    self.assertEqual(runner.targets_visited, [1, 2])
                    self.assertTrue(send.call_args.kwargs["critical"])
                    self.assertEqual(delivered, e._point(runner.position))
                    self.assertAlmostEqual(runner.speed_mps, (runner.distance_m-old_distance)/dt)
                    arrival_speed = runner.speed_mps
                    distance = runner.distance_m
                    runner.step(dt)
                    self.assertGreater(runner.distance_m, distance)
                    self.assertLessEqual(runner.speed_mps, arrival_speed+.55*dt+1e-5)
                    self.assertFalse(send.call_args.kwargs["critical"])

    def test_real_queue_and_mock_device_receive_the_same_centimetre_sample(self):
        runner, bridge, clock = self.runner(physical=True)
        wire = []

        class Simulator:
            async def set(self, lat, lon):
                wire.append((clock[0], lat, lon))

        bridge._simulator = Simulator()
        target = e._destination(ORIGIN, 120, 100)
        runner.submit_detour([dict(lat=target[0], lon=target[1], is_target=True)])

        async def exercise():
            await bridge._send(bridge._take_sample())
            for _ in range(600):
                clock[0] += .95
                runner.step()
                queued_critical = bridge.snapshot()["pending_critical_samples"]
                await bridge._send(bridge._take_sample())
                if runner.targets_visited:
                    self.assertEqual(queued_critical, 1)
                    self.assertLessEqual(e.haversine_distance(*target, *wire[-1][1:]), .080001)
                    self.assertEqual(wire[-1][1:], e._point(runner.position))
                    self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)
                    return
            self.fail("no physical checkpoint call")

        asyncio.run(exercise())
        self.assertFalse(bridge.snapshot()["speed_fuse_tripped"])
        self.assertTrue(all(b[0]-a[0] >= .95-1e-6 for a, b in zip(wire, wire[1:])))

    def test_critical_survives_reconnect_with_its_approach_and_following_samples(self):
        _, bridge, clock = self.runner(physical=True)
        wire = []

        class Simulator:
            async def set(self, lat, lon):
                wire.append((lat, lon))

        bridge._simulator = Simulator()

        async def exercise():
            await bridge._send(bridge._take_sample())
            origin = wire[-1]
            samples = [e._destination(origin, 0, n) for n in range(1, 6)]
            for n, point in enumerate(samples, 1):
                bridge.set_location(*point, critical=n == 3)
            self.assertEqual(bridge.snapshot()["pending_critical_samples"], 1)
            for point in samples:
                # Simulate repeated reconnects until the critical sample is
                # acknowledged, rather than jumping to the newest ordinary one.
                sample = bridge._take_sample(reconnect=bool(bridge._critical_sequences))
                self.assertEqual(sample[1:], point)
                self.assertEqual(bridge._take_sample(), sample)
                clock[0] += .95
                await bridge._send(sample)
            self.assertEqual(wire[1:], samples)

        asyncio.run(exercise())
        self.assertEqual(bridge.snapshot()["pending_samples"], 0)
        self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)
        self.assertEqual(bridge.dropped_device_samples, 0)

    def test_failed_in_flight_checkpoint_is_retried_before_later_samples(self):
        _, bridge, clock = self.runner(physical=True)
        wire = []
        fail = [False]

        class Simulator:
            async def set(self, lat, lon):
                if fail[0]:
                    raise OSError("USB disconnected")
                wire.append((lat, lon))

        bridge._simulator = Simulator()

        async def exercise():
            await bridge._send(bridge._take_sample())
            target = e._destination(wire[-1], 0, 1)
            bridge.set_location(*target, critical=True)
            sample = bridge._take_sample()
            fail[0] = True
            clock[0] += .95
            with self.assertRaises(OSError):
                await bridge._send(sample)
            bridge.set_location(*e._destination(target, 0, 1))
            self.assertEqual(bridge.snapshot()["pending_critical_samples"], 1)
            self.assertEqual(bridge._take_sample(reconnect=True), sample)
            fail[0] = False
            clock[0] += .95
            await bridge._send(bridge._take_sample(reconnect=True))
            self.assertEqual(wire[-1], target)
            self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)
            self.assertEqual(bridge.snapshot()["pending_samples"], 1)

        asyncio.run(exercise())

    def test_rate_wait_does_not_overwrite_checkpoint_or_bypass_the_fuse(self):
        _, bridge, clock = self.runner(physical=True)
        wire = []

        class Simulator:
            async def set(self, lat, lon):
                wire.append((clock[0], lat, lon))

        bridge._simulator = Simulator()

        async def exercise():
            await bridge._send(bridge._take_sample())
            target = e._destination(wire[-1][1:], 0, 1)
            bridge.set_location(*target, critical=True)
            sample = bridge._take_sample()

            async def produce_while_waiting(seconds):
                bridge.set_location(*e._destination(target, 0, 1))
                clock[0] += seconds

            with patch.object(e.asyncio, "sleep", side_effect=produce_while_waiting):
                await bridge._send(sample)
            self.assertEqual(wire[-1][1:], target)
            self.assertAlmostEqual(wire[-1][0]-wire[-2][0], .8)
            self.assertEqual(bridge.snapshot()["pending_samples"], 1)
            bridge.set_location(*e._destination(target, 0, 100), critical=True)
            clock[0] += .95
            await bridge._send(bridge._take_sample())
            clock[0] += .95
            with self.assertRaises(e.SpeedFuseError):
                await bridge._send(bridge._take_sample())
            self.assertEqual(bridge.snapshot()["pending_critical_samples"], 1)

        asyncio.run(exercise())

    def test_explicit_restore_clears_pending_checkpoint(self):
        _, bridge, _ = self.runner(physical=True)
        bridge.set_location(*ORIGIN, critical=True)
        self.assertEqual(bridge.snapshot()["pending_critical_samples"], 1)
        self.assertTrue(bridge.clear_location())
        self.assertEqual(bridge.snapshot()["pending_samples"], 0)
        self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)


if __name__ == "__main__":
    unittest.main()
