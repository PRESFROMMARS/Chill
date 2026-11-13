"""Offline multi-device, diagnostics, state ordering and geometry lock checks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
import io
import json
import os
from pathlib import Path
import random
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import engine as e
import main
import test_engine
import test_audit


ORIGIN = (31.2304, 121.4737)
TRACK = [{"lat": 31.23, "lon": 121.47}, {"lat": 31.23, "lon": 121.471},
         {"lat": 31.231, "lon": 121.471}]


class DeviceSelectionTests(unittest.TestCase):
    def bridge(self, **kwargs):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False, **kwargs)
        def close_fixture():
            bridge.close(timeout=.02)
            if bridge._thread is not None:
                bridge._thread.join(timeout=.5)
        self.addCleanup(close_fixture)
        return bridge

    def driver(self, serials):
        importer, calls = test_engine.BridgeTests().fake_driver()
        usbmux = SimpleNamespace(list_devices=lambda: [SimpleNamespace(serial=serial) for serial in serials])
        return lambda name: usbmux if name == "pymobiledevice3.usbmux" else importer(name), calls

    def test_multiple_devices_are_rejected_before_any_lockdown_connection(self):
        bridge = self.bridge()
        importer, calls = self.driver(["test-udid", "another-device"])
        with patch.object(e.importlib, "import_module", side_effect=importer):
            with self.assertRaises(e.DeviceConnectionError) as error:
                asyncio.run(bridge._open_session())
        self.assertEqual(error.exception.code, "multiple_devices")
        self.assertNotIn("lockdown", calls)
        self.assertIsNone(bridge.udid)

    def test_explicit_udid_can_select_one_device_without_guessing(self):
        bridge = self.bridge(udid="test-udid")
        importer, calls = self.driver(["another-device", "test-udid"])

        async def exercise():
            await bridge._open_session()
            self.assertEqual(bridge.udid, "test-udid")
            await bridge._disconnect()

        with patch.object(e.importlib, "import_module", side_effect=importer):
            asyncio.run(exercise())
        self.assertIn("lockdown", calls)

    def test_duplicate_usb_network_records_are_one_physical_device(self):
        bridge = self.bridge()
        importer, _ = self.driver(["test-udid", "TESTUDID"])
        with patch.object(e.importlib, "import_module", side_effect=importer):
            self.assertEqual(asyncio.run(bridge._select_device()).replace("-", "").lower(), "testudid")

    def test_clean_automatic_cache_is_discarded_when_a_different_phone_is_present(self):
        bridge = self.bridge()
        bridge.udid, bridge.ios_version = "old-phone", "27.0"
        importer, _ = self.driver(["new-phone"])
        with patch.object(e.importlib, "import_module", side_effect=importer):
            self.assertEqual(asyncio.run(bridge._select_device()), "new-phone")
        self.assertIsNone(bridge.udid)
        self.assertIsNone(bridge.ios_version)

    def test_unplugged_automatic_cache_is_cleared_and_reenumerates(self):
        bridge = self.bridge()
        bridge.udid = "old-phone"
        devices = []
        importer, _ = self.driver(devices)
        with patch.object(e.importlib, "import_module", side_effect=importer):
            with self.assertRaises(e.DeviceConnectionError):
                asyncio.run(bridge._select_device())
            self.assertIsNone(bridge.udid)
            devices.append("new-phone")
            self.assertEqual(asyncio.run(bridge._select_device()), "new-phone")

    def test_explicit_device_is_not_silently_replaced(self):
        bridge = self.bridge(udid="original")
        importer, calls = self.driver(["new-phone"])
        with patch.object(e.importlib, "import_module", side_effect=importer):
            with self.assertRaises(e.DeviceConnectionError):
                asyncio.run(bridge._open_session())
        self.assertEqual(bridge.udid, "original")
        self.assertNotIn("lockdown", calls)

    def test_dirty_session_keeps_original_device_even_when_another_phone_is_present(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = e.DirtySessionMarker(Path(folder)/"session.json")
            marker.mark("original")
            bridge = self.bridge(sandbox=True, dirty_marker=marker)
            importer, calls = self.driver(["new-phone"])
            with patch.object(e.importlib, "import_module", side_effect=importer):
                with self.assertRaises(e.DeviceConnectionError):
                    asyncio.run(bridge._open_session())
            self.assertEqual(bridge.udid, "original")
            self.assertTrue(marker.path.exists())
            self.assertNotIn("lockdown", calls)
            with self.assertLogs(e.LOGGER, level="ERROR"):
                bridge.close(timeout=.02)

    def test_failed_clean_connection_does_not_latch_cached_udid(self):
        bridge = self.bridge()
        bridge.udid = "test-udid"
        importer, _ = self.driver(["test-udid"])

        def broken(**kwargs):
            raise OSError("device vanished before lockdown")

        with patch.object(e.importlib, "import_module", side_effect=lambda name:
                          SimpleNamespace(create_using_usbmux=broken) if name == "pymobiledevice3.lockdown" else importer(name)):
            with self.assertRaises(OSError):
                asyncio.run(bridge._open_session())
        self.assertIsNone(bridge.udid)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="piggy-boundary-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def api(self):
        api = main.DesktopAPI(self.root/"config.json", sandbox=True)
        self.addCleanup(lambda: api._shutdown(timeout=.05))
        api._clock = {"status": "synced", "offset_s": 0, "checked_at": time.time()}
        api._clock_checked_mono = time.monotonic()
        return api

    def runner(self):
        bridge = e.IOSDeviceBridge(sandbox=True)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(41))
        self.addCleanup(bridge.close)
        self.addCleanup(runner.close)
        runner.load_track([e._destination(ORIGIN, -i*360/16, 60) for i in range(16)])
        runner.lock_start(background=False)
        runner.start_cruising(background=False)
        return runner

    def test_snapshots_have_unique_monotonic_sequences_across_threads_and_sessions(self):
        api = self.api()
        with ThreadPoolExecutor(max_workers=8) as pool:
            sequences = list(pool.map(lambda _: api.get_state()["data"]["seq"], range(80)))
        self.assertEqual(sorted(sequences), list(range(1, 81)))
        api._runner.session_id = "new-session"
        self.assertEqual(api.get_state()["data"]["seq"], 81)

    def test_device_failure_details_survive_idle_api_state_and_are_bounded(self):
        api = self.api()
        api._bridge.last_error = "\x1b[31mPermission denied\x1b[0m\x00"+"x"*2000
        api._bridge._device_error_code = "tunnel_unavailable"
        state = api.get_state()["data"]
        self.assertFalse(state["session_active"])
        self.assertTrue(state["device_issue"])
        self.assertLessEqual(len(state["device_error"]), 1600)
        self.assertNotIn("\x1b", state["device_error"])
        self.assertNotIn("\x00", state["device_error"])
        self.assertEqual(state["device"]["error_code"], "tunnel_unavailable")

    def test_tunnel_output_is_bounded_and_exit_error_reaches_fallback(self):
        bridge = e.IOSDeviceBridge(auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        stream = io.BytesIO(b"noise\n"*1000+b"\x1b[31mPermission denied: administrator required\x1b[0m\n")
        bridge._capture_tunnel_output(stream, bridge._tunnel_log)
        self.assertTrue(stream.closed)
        self.assertEqual(len(bridge._tunnel_log), 20)
        bridge._children.append(SimpleNamespace(poll=lambda: 5))
        with patch.object(bridge, "_read_tunneld", side_effect=OSError("connection refused")):
            with self.assertRaises(e.DeviceConnectionError) as error:
                asyncio.run(bridge._tunneld_provider(AsyncExitStack()))
        self.assertIn("Permission denied", str(error.exception))
        self.assertIn("exit=5", str(error.exception))
        self.assertNotIn("\x1b", str(error.exception))
        self.assertLessEqual(len(str(error.exception)), 1600)

    def test_tunneld_startup_waits_for_the_selected_phone_and_reuses_transport(self):
        bridge = e.IOSDeviceBridge(connect_timeout=1)
        self.addCleanup(bridge.close)
        bridge.udid = "test-udid"
        bridge.TUNNEL_POLL_SECONDS = .01
        entries = {"TESTUDID": [{"tunnel-address": "::1", "tunnel-port": 1234}]}
        importer, calls = test_engine.BridgeTests().fake_driver()
        addresses = []

        class RSD:
            udid = "test-udid"

            def __init__(self, address):
                addresses.append(address)

            async def connect(self):
                pass

            async def close(self):
                pass

        def modules(name):
            if name.endswith(".rsd_tunnel"):
                calls.append("preferred")
                raise ImportError("preferred tunnel unavailable")
            if name.endswith(".remote_service_discovery"):
                return SimpleNamespace(RemoteServiceDiscoveryService=RSD)
            return importer(name)

        async def exercise():
            for _ in range(2):
                async with AsyncExitStack() as stack:
                    self.assertIsInstance(await bridge._open_rsd(stack, bridge.udid), RSD)

        with patch.object(e.importlib, "import_module", side_effect=modules), \
             patch.object(bridge, "_read_tunneld", side_effect=[OSError("starting"), {}, entries, entries]) as read, \
             patch.object(bridge, "_spawn_tunneld") as spawn, \
             patch.object(bridge, "_owned_tunneld_running", side_effect=[False, True]):
            asyncio.run(exercise())
        spawn.assert_called_once()
        self.assertEqual(read.call_count, 4)
        self.assertEqual(calls, ["preferred"])
        self.assertEqual(addresses, [("::1", 1234)]*2)

    def test_tunnel_pipe_reader_is_drained_and_closed_after_child_exit(self):
        bridge = e.IOSDeviceBridge()
        self.addCleanup(bridge.close)
        child = SimpleNamespace(poll=lambda: 2, stdout=io.BytesIO(b"missing driver\n"))
        with patch.object(e.subprocess, "Popen", return_value=child) as spawn:
            bridge._spawn_tunneld()
        bridge._tunnel_readers[0].join(timeout=.5)
        self.assertFalse(bridge._tunnel_readers[0].is_alive())
        self.assertTrue(child.stdout.closed)
        self.assertEqual(spawn.call_args.kwargs["stderr"], e.subprocess.STDOUT)
        self.assertIn("missing driver", bridge._tunnel_failure("startup failed"))

    def test_route_loading_does_not_hold_api_lock_and_blocks_a_second_start(self):
        api = self.api()
        entered, release = threading.Event(), threading.Event()
        original = api._runner.load_track
        result = []

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test release missing")
            return original(*args, **kwargs)

        payload = {"points": TRACK, "profile": "standard", "target_km": 2, "checks": [True]*7}
        with patch.object(api._runner, "load_track", side_effect=slow):
            thread = threading.Thread(target=lambda: result.append(api.lock_start(payload)))
            thread.start()
            try:
                self.assertTrue(entered.wait(.5))
                before = time.monotonic()
                self.assertTrue(api.get_state()["data"]["preparing"])
                self.assertLess(time.monotonic()-before, .3)
                self.assertEqual(api.lock_start(payload)["error"]["code"], "operation_in_progress")
                self.assertFalse(api._on_closing())
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result[0]["ok"], result)
        self.assertFalse(result[0]["data"]["preparing"])

    def test_next_lap_build_allows_snapshots_and_discards_work_after_close(self):
        runner = self.runner()
        runner._loop_s = runner._lap.length-10
        entered, release = threading.Event(), threading.Event()
        original = runner._make_lap
        state = runner._geometry_rng.getstate()

        def slow(**kwargs):
            entered.set()
            release.wait(2)
            return original(**kwargs)

        with patch.object(runner, "_make_lap", side_effect=slow):
            thread = threading.Thread(target=runner._prepare_next_lap, args=(.95,))
            thread.start()
            try:
                self.assertTrue(entered.wait(.5))
                before = time.monotonic()
                runner.snapshot()
                self.assertLess(time.monotonic()-before, .3)
                runner.close()
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(runner._prepared_lap)
        self.assertEqual(runner._geometry_rng.getstate(), state)

    def test_detour_build_does_not_hold_api_lock_or_commit_after_shutdown(self):
        api = self.api()
        entered, release = threading.Event(), threading.Event()
        result = []
        api._session_active = True
        api._runner.state = e.RunnerState.CRUISING_LOOP

        def slow(points):
            entered.set()
            release.wait(2)
            return {"exit_idx": 0, "entry_idx": 0}

        with patch.object(api._runner, "submit_detour", side_effect=slow):
            thread = threading.Thread(target=lambda: result.append(api.submit_detour([
                {"lat": ORIGIN[0], "lon": ORIGIN[1], "is_target": True}])))
            thread.start()
            try:
                self.assertTrue(entered.wait(.5))
                before = time.monotonic()
                self.assertTrue(api.get_state()["ok"])
                self.assertLess(time.monotonic()-before, .3)
                api._shutdown(timeout=.05)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["error"]["code"], "detour_busy")
        self.assertEqual(api._active_detour, [])

    def test_lap_histories_are_bounded_but_total_laps_and_latest_split_survive(self):
        runner = self.runner()
        runner._lap_start_time = runner._lap_start_distance = 0
        total = runner.LAP_HISTORY_LIMIT+5
        for index in range(1, total+1):
            runner._prepared_lap = runner._lap
            runner._next_lap(index*10, index*100)
        state = runner.snapshot()
        self.assertEqual(state["laps"], total)
        self.assertEqual(len(state["lap_splits"]), runner.LAP_HISTORY_LIMIT)
        self.assertEqual(len(runner._lap_distances), runner.LAP_HISTORY_LIMIT)
        self.assertEqual(state["lap_splits_start"], 5)
        self.assertEqual(state["last_lap"]["pace_s_per_km"], 100)

    def test_target_history_rolls_while_total_visited_is_preserved(self):
        runner = self.runner()
        runner.state = e.RunnerState.CRUISING_DETOUR
        runner._detour = e._Route([ORIGIN, ORIGIN], allow_zero=True)
        total = runner.TARGET_HISTORY_LIMIT+5
        runner._detour_targets.extend({"s_m": 0, "id": index, "lat": ORIGIN[0], "lon": ORIGIN[1]}
                                      for index in range(total))
        runner._advance(1, 1)
        state = runner.snapshot()
        self.assertEqual(state["targets_visited_total"], total)
        self.assertEqual(len(state["targets_visited"]), runner.TARGET_HISTORY_LIMIT)
        self.assertEqual(state["targets_visited"][0], 5)

    def test_node_missing_skips_with_an_actionable_message(self):
        case = test_audit.AuditTests("test_08_frontend_languages_javascript_and_control_states")
        with patch.dict(os.environ, {"NODE_BINARY": "explicit-node"}), \
             patch.object(test_audit.shutil, "which", return_value=None) as locate:
            with self.assertRaisesRegex(unittest.SkipTest, "NODE_BINARY"):
                case.test_08_frontend_languages_javascript_and_control_states()
        locate.assert_called_once_with("explicit-node")


if __name__ == "__main__":
    unittest.main()
