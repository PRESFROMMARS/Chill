"""Offline scheduler, critical queue, driver deadline and sleep-guard regressions."""
import asyncio
import random
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import engine as e
import main


ORIGIN = (31.2304, 121.4737)


class SchedulingTests(unittest.TestCase):
    def bridge(self, **kwargs):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False, **kwargs)
        connection = patch.object(bridge, "connect", return_value={})
        connection.start()
        self.addCleanup(connection.stop)
        self.addCleanup(bridge.close)
        return bridge

    def runner(self):
        bridge = self.bridge(sandbox=True)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(41), clock=lambda: runner.elapsed_s)
        self.addCleanup(runner.close)
        runner.load_track([e._destination(ORIGIN, -i*360/16, 60) for i in range(16)])
        runner.lock_start(background=False)
        runner.start_cruising(background=False)
        return runner, bridge

    def simulator(self, bridge, clock, latency=0.0):
        starts, ends = [], []

        class Simulator:
            async def set(inner, lat, lon):
                starts.append(clock[0])
                clock[0] += latency
                ends.append(clock[0])

            async def clear(inner):
                pass

        bridge._simulator = Simulator()
        self.addCleanup(lambda: asyncio.run(bridge._disconnect(clear=True)))
        return starts, ends

    def test_live_scheduler_discards_1_to_1_9_second_jitter_without_wire_fuse(self):
        runner, bridge = self.runner()
        runner._active_since = 0.0
        wall = [0.0]
        bridge._wire_clock = lambda: wall[0]
        starts, _ = self.simulator(bridge, wall)
        asyncio.run(bridge._send(bridge._take_sample()))
        gaps = iter((1.01, 1.5, 1.9, 60.0, .95))
        advances = []
        original_tick = runner._tick

        class Scheduler:
            def wait(inner, delay):
                try:
                    wall[0] += next(gaps)
                    return False
                except StopIteration:
                    return True

        def tick(dt):
            advances.append(dt)
            old = e._point(runner.position)
            original_tick(dt)
            self.assertLessEqual(e.haversine_distance(*old, *e._point(runner.position)), 3.8+1e-5)
            asyncio.run(bridge._send(bridge._take_sample()))

        with patch.object(runner, "_shutdown", Scheduler()), \
             patch.object(e.time, "monotonic", side_effect=lambda: wall[0]), \
             patch.object(runner, "_tick", side_effect=tick):
            runner.run()
        for actual, expected in zip(advances, [1.0, 1.0, 1.0, .95, .95]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(runner.elapsed_s, 4.9)
        self.assertEqual(len(starts), 6)
        self.assertFalse(bridge.snapshot()["speed_fuse_tripped"])

    def test_latched_fuse_freezes_mileage_time_trace_and_pace_model_until_restored(self):
        runner, bridge = self.runner()
        runner.step()
        before = runner.snapshot()
        rng_state = runner._rng.getstate()
        bridge._fuse_tripped = True
        for _ in range(10):
            state = runner.step()
        for key in ("position", "distance_m", "elapsed_s", "trace_sequence", "rest_total_s", "current_lap"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(runner._rng.getstate(), rng_state)
        self.assertTrue(bridge.clear_location())
        self.assertGreater(runner.step()["distance_m"], before["distance_m"])

    def test_fuse_does_not_prevent_manual_safe_stop(self):
        runner, bridge = self.runner()
        runner.step()
        bridge._fuse_tripped = True
        distance = runner.distance_m
        runner.request_safe_stop()
        self.assertEqual(runner.step()["state"], "SAFE_STOPPED")
        self.assertEqual(runner.distance_m, distance)

    def test_locked_start_does_not_keep_queueing_when_fused(self):
        runner, bridge = self.runner()
        runner.close()
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(4))
        self.addCleanup(runner.close)
        runner.load_track([e._destination(ORIGIN, -i*90, 60) for i in range(4)])
        runner.lock_start(background=False)
        sequence = bridge.snapshot()["queued_sequence"]
        bridge._fuse_tripped = True
        runner.step()
        self.assertEqual(bridge.snapshot()["queued_sequence"], sequence)

    def test_full_queue_accepts_checkpoint_by_compacting_safe_interior_ordinary_sample(self):
        bridge = self.bridge()
        for index in range(128):
            bridge.set_location(*ORIGIN, critical=index in (0, 2))
        newest = bridge.set_location(*ORIGIN, critical=True)
        sequences = [sample[0] for sample in bridge._critical]
        self.assertEqual(len(sequences), 128)
        self.assertIn(1, sequences)
        self.assertNotIn(2, sequences)
        self.assertEqual(sequences[-1], newest)
        self.assertEqual(bridge._critical_sequences, {1, 3, newest})
        self.assertFalse(bridge.snapshot()["speed_fuse_tripped"])
        self.assertEqual(bridge.dropped_device_samples, 1)

    def test_full_critical_queue_latches_without_discarding_existing_checkpoints(self):
        bridge = self.bridge()
        for _ in range(128):
            bridge.set_location(*ORIGIN, critical=True)
        pending = list(bridge._critical)
        bridge.set_location(*ORIGIN, critical=True)
        self.assertEqual(list(bridge._critical), pending)
        self.assertEqual(len(bridge._critical_sequences), 128)
        self.assertTrue(bridge.snapshot()["speed_fuse_tripped"])

    def test_ordinary_overflow_keeps_existing_fail_closed_behavior(self):
        bridge = self.bridge()
        for _ in range(129):
            bridge.set_location(*ORIGIN)
        self.assertEqual(bridge.snapshot()["pending_samples"], 128)
        self.assertTrue(bridge.snapshot()["speed_fuse_tripped"])

    def test_evicted_ordinary_sample_does_not_lose_checkpoint_fifo_delivery(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        starts, _ = self.simulator(bridge, clock)
        for _ in range(128):
            bridge.set_location(*ORIGIN)
        checkpoint = e._destination(ORIGIN, 0, 1)
        sequence = bridge.set_location(*checkpoint, critical=True)

        async def exercise():
            for _ in range(128):
                clock[0] += .95
                await bridge._send(bridge._take_sample())

        asyncio.run(exercise())
        self.assertEqual(len(starts), 128)
        self.assertEqual(bridge.snapshot()["delivered_sequence"], sequence)
        self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)
        self.assertEqual(e._point(bridge.last_delivered_position), checkpoint)

    def test_rate_limit_measures_previous_completion_to_next_start(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        starts, ends = self.simulator(bridge, clock, latency=.8)
        bridge.set_location(*ORIGIN)

        async def sleep(seconds):
            clock[0] += seconds

        async def exercise():
            await bridge._send(bridge._take_sample())
            with patch.object(e.asyncio, "sleep", side_effect=sleep):
                await asyncio.gather(bridge._send((2, *ORIGIN)), bridge._send((3, *ORIGIN)))
            for end, start in zip(ends, starts[1:]):
                self.assertGreaterEqual(start-end, .95-1e-8)
            self.assertAlmostEqual(bridge._last_send_completed_at, ends[-1])
            self.assertAlmostEqual(bridge._last_send_at, starts[-1])

        asyncio.run(exercise())

    def test_heartbeat_waits_for_completion_and_clear_resets_both_clocks(self):
        clock = [0.0]
        bridge = self.bridge(sandbox=True, clock=lambda: clock[0])
        self.simulator(bridge, clock, latency=.8)
        bridge.set_location(*ORIGIN)
        asyncio.run(bridge._send(bridge._take_sample()))
        clock[0] = 1.74
        self.assertIsNone(bridge._take_sample())
        clock[0] = 1.76
        self.assertIsNotNone(bridge._take_sample())
        self.assertTrue(bridge.clear_location())
        self.assertIsNone(bridge._last_send_completed_at)
        self.assertIsNone(bridge._last_send_at)

    def test_locked_start_keeps_sending_distinct_positions_without_advancing_route(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(41))
        self.addCleanup(runner.close)
        runner.load_track([e._destination(ORIGIN, -i*360/16, 60) for i in range(16)])
        locked = runner.lock_start(background=False)
        anchor = e._point(locked["position"])
        starts, _ = self.simulator(bridge, clock, latency=.35)
        positions = []

        async def send_updates():
            await bridge._send(bridge._take_sample())
            positions.append(e._point(bridge.last_delivered_position))
            for _ in range(12):
                clock[0] = starts[-1]+.94
                self.assertIsNone(bridge._take_sample())
                clock[0] = starts[-1]+.96
                runner.step()
                await bridge._send(bridge._take_sample())
                positions.append(e._point(bridge.last_delivered_position))

        asyncio.run(send_updates())
        self.assertEqual(positions[0], anchor)
        for previous, current in zip(positions, positions[1:]):
            self.assertNotEqual(previous, current)
            self.assertGreaterEqual(e.haversine_distance(*anchor, *current), .3-1e-6)
            self.assertLessEqual(e.haversine_distance(*anchor, *current), .8+1e-6)
        self.assertTrue(all(.8 <= b-a <= 1 for a, b in zip(starts, starts[1:])))
        held = runner.snapshot()
        for field in ("position", "distance_m", "elapsed_s", "trace_sequence"):
            self.assertEqual(held[field], locked[field])
        self.assertEqual(bridge.snapshot()["queued_sequence"], runner.start_sequence)
        self.assertEqual(bridge.snapshot()["delivered_sequence"], runner.start_sequence)
        offset = e.haversine_distance(*anchor, *positions[-1])
        runner.start_cruising(background=False)
        with patch.object(runner, "_speed", return_value=e.MAX_MOTION_SPEED_MPS):
            moving = runner.step(1.0)
        self.assertLessEqual(moving["distance_m"], e.MAX_SAMPLE_DISTANCE_M-offset+1e-6)
        clock[0] = bridge._last_send_completed_at+.96
        asyncio.run(bridge._send(bridge._take_sample()))
        self.assertEqual(e._point(bridge.last_delivered_position), e._point(moving["position"]))

    def test_live_runner_45_minutes_with_100_to_150ms_sends_has_no_backlog(self):
        # Virtual wall time drives the real run(), FIFO and _send(). Driver
        # latency also attempts a producer tick while the send is in flight.
        # No real sleeping, phone, networking or alternate pacing algorithm.
        for latency in (.10, .15):
            with self.subTest(latency=latency):
                runner, bridge = self.runner()
                bridge.force_sandbox = False
                clock = [0.0]
                bridge._wire_clock = lambda: clock[0]
                runner._active_since = 0.0
                runner.target_m = 100000
                starts, ends, positions = [], [], []
                high_water = [0]
                blocked_ticks = [0]
                bridge.set_location(*e._point(runner.position))

                class Simulator:
                    async def set(inner, lat, lon):
                        starts.append(clock[0])
                        positions.append((lat, lon))
                        before = runner.snapshot()
                        rng = runner._rng.getstate()
                        runner.step()
                        self.assertEqual(runner.snapshot(), before)
                        self.assertEqual(runner._rng.getstate(), rng)
                        blocked_ticks[0] += 1
                        clock[0] += latency
                        ends.append(clock[0])

                    async def clear(inner):
                        pass

                bridge._simulator = Simulator()
                stop_at = 2701.0

                class Scheduler:
                    def wait(inner, delay):
                        target = min(stop_at, clock[0]+delay)
                        high_water[0] = max(high_water[0], bridge.snapshot()["pending_samples"])
                        # Include idle heartbeats, which compete for the same
                        # slot in _serve(). They must not starve moving samples.
                        while bridge._latest is not None:
                            completed = bridge._last_send_completed_at
                            due = clock[0] if completed is None else max(clock[0], completed+e.MOTION_INTERVAL_S)
                            if due > target+1e-8:
                                break
                            clock[0] = due
                            sample = bridge._take_sample()
                            if sample is None:
                                break
                            asyncio.run(bridge._send(sample))
                        clock[0] = max(clock[0], target)
                        return clock[0] >= stop_at

                try:
                    with patch.object(runner, "_shutdown", Scheduler()), \
                         patch.object(e.time, "monotonic", side_effect=lambda: clock[0]):
                        runner.run()
                    state = bridge.snapshot()
                    self.assertGreaterEqual(clock[0], 2700)
                    self.assertLessEqual(high_water[0], 2)
                    self.assertFalse(state["speed_fuse_tripped"])
                    self.assertNotEqual(state["last_error"], "coordinate_backlog_overflow")
                    self.assertGreater(len(starts), 2400)
                    self.assertEqual(blocked_ticks[0], len(starts))
                    # Prevent a "fix" that merely halves the simulated pace.
                    self.assertGreater(runner.elapsed_s, 2400)
                    self.assertLessEqual(runner.elapsed_s, clock[0])
                    for end, start in zip(ends, starts[1:]):
                        self.assertGreaterEqual(start-end, e.MOTION_INTERVAL_S-1e-7)
                    for a, b in zip(positions, positions[1:]):
                        self.assertLessEqual(e.haversine_distance(*a, *b), e.MAX_SAMPLE_DISTANCE_M+1e-5)
                finally:
                    asyncio.run(bridge._disconnect(clear=True))

    def test_full_moving_queue_keeps_entire_approach_and_delivers_reserved_checkpoint(self):
        for step_m in (2.5, 3.5):
            with self.subTest(step_m=step_m):
                clock = [0.0]
                bridge = self.bridge(clock=lambda: clock[0])
                self.simulator(bridge, clock)

                async def exercise():
                    await bridge._send((0, *ORIGIN))
                    approach = [e._destination(ORIGIN, 0, i*step_m) for i in range(1, 129)]
                    for point in approach:
                        bridge.set_location(*point)
                    queued = list(bridge._critical)
                    target = e._destination(ORIGIN, 0, 129*step_m)
                    critical = bridge.set_location(*target, critical=True)
                    self.assertEqual(list(bridge._critical), queued)
                    self.assertEqual(bridge.snapshot()["pending_samples"], 129)
                    self.assertEqual(bridge.snapshot()["pending_critical_samples"], 1)
                    self.assertEqual(bridge.dropped_device_samples, 0)
                    self.assertGreater(bridge.motion_delay(), 0)
                    # USB stalls, then reconnects repeatedly. No extra distance
                    # is granted, and no predecessor may disappear on recovery.
                    clock[0] += 10
                    for point in approach+[target]:
                        sample = bridge._take_sample(reconnect=True)
                        self.assertEqual(sample[1:], point)
                        await bridge._send(sample)
                        clock[0] += e.MOTION_INTERVAL_S
                    self.assertEqual(bridge.snapshot()["delivered_sequence"], critical)
                    self.assertEqual(bridge.snapshot()["pending_samples"], 0)
                    self.assertEqual(bridge.snapshot()["pending_critical_samples"], 0)
                    self.assertFalse(bridge.snapshot()["speed_fuse_tripped"])
                    self.assertEqual(e._point(bridge.last_delivered_position), target)

                asyncio.run(exercise())

    def test_compaction_of_moving_interior_sample_checks_new_edge_before_deletion(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        self.simulator(bridge, clock)

        async def exercise():
            await bridge._send((0, *ORIGIN))
            for index in range(1, 129):
                bridge.set_location(*e._destination(ORIGIN, 0, index*1.5))
            head = bridge._take_sample()
            target = e._destination(ORIGIN, 0, 129*1.5)
            critical = bridge.set_location(*target, critical=True)
            self.assertEqual(bridge._take_sample(), head)
            self.assertIsNone(bridge._deferred_critical)
            self.assertEqual(bridge.dropped_device_samples, 1)
            while bridge._critical:
                clock[0] += e.MOTION_INTERVAL_S
                await bridge._send(bridge._take_sample())
            self.assertEqual(bridge._sent_sequence, critical)
            self.assertFalse(bridge._fuse_tripped)

        asyncio.run(exercise())

    def test_one_second_3_8m_step_survives_point95_drain_but_outage_cannot_teleport(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        self.simulator(bridge, clock)

        async def exercise():
            await bridge._send((0, *ORIGIN))
            # Reproduce the audit's variable-latency/queued-recovery sequence.
            bridge._last_send_completed_at = .8
            first = e._destination(ORIGIN, 0, 3.61)
            clock[0] = 1.75
            await bridge._send((1, *first))
            second = e._destination(first, 0, 3.8)
            clock[0] = 2.70
            await bridge._send((2, *second))
            self.assertFalse(bridge._fuse_tripped)
            clock[0] += 60
            with self.assertRaises(e.SpeedFuseError):
                await bridge._send((3, *e._destination(second, 0, 5)))
            self.assertTrue(bridge._fuse_tripped)
            self.assertEqual(bridge.physical_sent, 3)

        asyncio.run(exercise())

    def test_backpressure_freezes_pending_checkpoint_and_resumes_after_delivery(self):
        runner, bridge = self.runner()
        bridge.force_sandbox = False
        clock = [0.0]
        bridge._wire_clock = lambda: clock[0]
        self.simulator(bridge, clock)
        bridge.set_location(*e._point(runner.position), critical=True)
        before, rng = runner.snapshot(), runner._rng.getstate()
        for _ in range(20):
            runner.step()
        self.assertEqual(runner.snapshot(), before)
        self.assertEqual(runner._rng.getstate(), rng)
        asyncio.run(bridge._send(bridge._take_sample()))
        clock[0] = .95
        after = runner.step(1.9)
        self.assertGreater(after["distance_m"], before["distance_m"])
        self.assertEqual(after["elapsed_s"]-before["elapsed_s"], e.MAX_MOTION_STEP_S)
        self.assertEqual(bridge.snapshot()["pending_samples"], 1)
        asyncio.run(bridge._send(bridge._take_sample()))

    def test_reconnect_without_checkpoint_also_keeps_necessary_ordinary_chain(self):
        clock = [0.0]
        bridge = self.bridge(clock=lambda: clock[0])
        self.simulator(bridge, clock)

        async def exercise():
            await bridge._send((0, *ORIGIN))
            points = [e._destination(ORIGIN, 0, i*3.5) for i in range(1, 5)]
            for point in points:
                bridge.set_location(*point)
            clock[0] += 30
            for point in points:
                sample = bridge._take_sample(reconnect=True)
                self.assertEqual(sample[1:], point)
                await bridge._send(sample)
                clock[0] += e.MOTION_INTERVAL_S
            self.assertFalse(bridge._fuse_tripped)
            self.assertEqual(bridge.dropped_device_samples, 0)

        asyncio.run(exercise())

    def test_reserved_checkpoint_is_bounded_and_explicit_clear_removes_it(self):
        bridge = self.bridge()
        for index in range(128):
            bridge.set_location(*e._destination(ORIGIN, 0, index*3.5))
        checkpoint = bridge.set_location(*e._destination(ORIGIN, 0, 128*3.5), critical=True)
        self.assertEqual(bridge._deferred_critical[0], checkpoint)
        self.assertFalse(bridge._fuse_tripped)
        bridge.set_location(*e._destination(ORIGIN, 0, 129*3.5), critical=True)
        self.assertTrue(bridge._fuse_tripped)
        self.assertEqual(bridge.snapshot()["pending_samples"], 129)
        self.assertEqual(bridge._deferred_critical[0], checkpoint)
        self.assertTrue(bridge.clear_location())
        self.assertIsNone(bridge._deferred_critical)
        self.assertEqual(bridge.snapshot()["pending_samples"], 0)
        self.assertFalse(bridge._fuse_tripped)


class DriverTimeoutTests(unittest.TestCase):
    def test_blocking_sync_invoke_times_out_while_event_loop_keeps_running(self):
        release, entered = threading.Event(), threading.Event()
        caller = threading.get_ident()
        driver_threads = []

        def blocking(*, value):
            driver_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return value

        async def exercise():
            ticks = []

            async def heartbeat():
                for _ in range(4):
                    await asyncio.sleep(.005)
                    ticks.append(1)

            pulse = asyncio.create_task(heartbeat())
            before = time.monotonic()
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(e._invoke(blocking, value=42), timeout=.04)
                self.assertLess(time.monotonic()-before, .5)
                self.assertTrue(entered.is_set())
                self.assertGreater(len(ticks), 0)
                self.assertNotEqual(driver_threads, [caller])
            finally:
                release.set()
                await pulse

        asyncio.run(exercise())

    def test_sync_returning_awaitable_runs_coroutine_on_original_loop(self):
        async def exercise():
            owner = asyncio.get_running_loop()

            def adapter(value):
                async def result():
                    self.assertIs(asyncio.get_running_loop(), owner)
                    return value
                return result()

            self.assertEqual(await e._invoke(adapter, 42), 42)

            def broken():
                raise OSError("driver failure")

            with self.assertRaisesRegex(OSError, "driver failure"):
                await e._invoke(broken)

        asyncio.run(exercise())

    def test_late_sync_set_must_finish_before_clear_can_remove_dirty_marker(self):
        self.check_late_call(clear_stalls=False)

    def test_timed_out_sync_clear_is_not_overlapped_or_reported_as_success(self):
        self.check_late_call(clear_stalls=True)

    def check_late_call(self, *, clear_stalls):
        release, finished = threading.Event(), threading.Event()
        order = []
        with tempfile.TemporaryDirectory(prefix="piggy-driver-deadline-") as folder:
            marker = e.DirtySessionMarker(Path(folder)/"session.json")
            bridge = e.IOSDeviceBridge(udid="offline-device", dirty_marker=marker,
                                       send_timeout=.03, auto_start_tunneld=False)
            simulator = MagicMock()

            def send(lat, lon):
                if not clear_stalls:
                    release.wait(2)
                    order.append("set_finished")
                    finished.set()

            def clear():
                order.append("clear")
                if clear_stalls and order.count("clear") == 1:
                    release.wait(2)
                    finished.set()

            simulator.set = send
            simulator.clear = clear
            bridge._simulator = simulator

            async def exercise():
                try:
                    if clear_stalls:
                        await bridge._send((1, *ORIGIN))
                        self.assertFalse(await bridge._disconnect(clear=True))
                    else:
                        with self.assertRaises(asyncio.TimeoutError):
                            await bridge._send((1, *ORIGIN))
                        self.assertFalse(await bridge._disconnect())
                    before = time.monotonic()
                    self.assertFalse(await bridge._disconnect(clear=True))
                    self.assertLess(time.monotonic()-before, .5)
                    self.assertIs(bridge._simulator, simulator)
                    self.assertTrue(marker.path.exists())
                    self.assertEqual(order.count("clear"), int(clear_stalls))
                    release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait, .5))
                    self.assertTrue(await bridge._disconnect(clear=True))
                    self.assertFalse(marker.path.exists())
                    if not clear_stalls:
                        self.assertEqual(order, ["set_finished", "clear"])
                    else:
                        self.assertEqual(order, ["clear", "clear"])
                finally:
                    release.set()

            try:
                asyncio.run(exercise())
            finally:
                bridge.close(timeout=.1)


class MacSleepTests(unittest.TestCase):
    def test_caffeinate_is_pid_bound_idempotent_and_reaped_on_close(self):
        child = MagicMock()
        child.poll.return_value = None
        protection = main.WindowsProtection()
        with patch.object(main.sys, "platform", "darwin"), \
             patch.object(main.os, "getpid", return_value=12345), \
             patch.object(main.subprocess, "Popen", return_value=child) as spawn:
            protection.start()
            protection.start()
            self.assertEqual(protection.snapshot()["awake"], "enabled")
            spawn.assert_called_once_with(["/usr/bin/caffeinate", "-di", "-w", "12345"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
            protection.close()
            protection.close()
            protection.start()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=1)
        child.kill.assert_not_called()
        self.assertIsNone(protection._caffeinate)

    def test_missing_caffeinate_reports_failure(self):
        protection = main.WindowsProtection()
        with patch.object(main.sys, "platform", "darwin"), \
             patch.object(main.subprocess, "Popen", side_effect=FileNotFoundError("missing")), \
             self.assertLogs(main.LOGGER, level="WARNING"):
            protection.start()
        self.assertEqual(protection.snapshot()["awake"], "failed")
        protection.close()

    def test_unexpected_caffeinate_exit_is_visible(self):
        child = MagicMock()
        child.poll.return_value = None
        protection = main.WindowsProtection()
        with patch.object(main.sys, "platform", "darwin"), \
             patch.object(main.subprocess, "Popen", return_value=child):
            protection.start()
        child.poll.return_value = 1
        self.assertEqual(protection.snapshot()["awake"], "failed")
        protection.close()
        child.terminate.assert_not_called()
        child.wait.assert_called_once()

    def test_caffeinate_termination_timeout_kills_and_reaps_only_owned_child(self):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 1), 0]
        protection = main.WindowsProtection()
        with patch.object(main.sys, "platform", "darwin"), \
             patch.object(main.subprocess, "Popen", return_value=child):
            protection.start()
            protection.close()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_other_platforms_do_not_start_caffeinate(self):
        protection = main.WindowsProtection()
        with patch.object(main.sys, "platform", "linux"), \
             patch.object(main.subprocess, "Popen") as spawn:
            protection.start()
            protection.close()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
