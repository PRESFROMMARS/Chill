"""Offline restoration, shutdown hooks and durable dirty-session regressions."""
import asyncio
import json
import logging
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import engine as e
import main
from runtime_support import BoundedFaultLog


UDID = "offline-test-device"
ORIGIN = (31.2304, 121.4737)
TRACK = [{"lat": 31.23, "lon": 121.47}, {"lat": 31.23, "lon": 121.471},
         {"lat": 31.231, "lon": 121.471}]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="piggy-recovery-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def fake_session(self, bridge):
        state = {"opens": 0, "clears": 0, "sets": 0, "fail_clear": 0, "fail_open": False,
                 "fuse_at_clear": []}

        class Simulator:
            async def set(inner, lat, lon):
                if bridge._dirty_marker is not None:
                    record = json.loads(bridge._dirty_marker.path.read_text())
                    self.assertTrue(record["active"])
                    self.assertEqual(record["udid"], UDID)
                state["sets"] += 1

            async def clear(inner):
                state["clears"] += 1
                state["fuse_at_clear"].append(bridge._fuse_tripped)
                if state["fail_clear"]:
                    state["fail_clear"] -= 1
                    raise OSError("temporary clear failure")

        async def opened():
            state["opens"] += 1
            if state["fail_open"]:
                raise OSError("offline device")
            bridge._simulator = Simulator()

        connection = patch.object(bridge, "_open_session", side_effect=opened)
        connection.start()
        self.addCleanup(connection.stop)
        def close_fixture():
            bridge.close(timeout=.08)
            # An intentionally exhausted production deadline need not wait for
            # final worker teardown. This fixture reuses one fake device ID.
            if bridge._thread is not None:
                bridge._thread.join(timeout=.5)
        self.addCleanup(close_fixture)
        bridge._simulator = Simulator()
        return state

    def bridge(self, name="session.json"):
        marker = e.DirtySessionMarker(self.root/name)
        bridge = e.IOSDeviceBridge(udid=UDID, dirty_marker=marker, auto_start_tunneld=False)
        return bridge, marker, self.fake_session(bridge)

    def injected(self, bridge):
        asyncio.run(bridge._send((1, *ORIGIN)))
        bridge._ever_connected = True

    def test_marker_exists_before_first_set_and_is_written_once(self):
        bridge, marker, state = self.bridge()
        self.assertFalse(marker.path.exists())
        self.injected(bridge)
        first = marker.path.read_bytes()
        bridge._last_send_at = None
        asyncio.run(bridge._send((2, *ORIGIN)))
        self.assertEqual(marker.path.read_bytes(), first)
        self.assertEqual(state["sets"], 2)
        self.assertEqual(list(self.root.glob(".piggy-location-*.tmp")), [])
        self.assertTrue(bridge.clear_location(timeout=.5))
        self.assertFalse(marker.path.exists())
        self.assertFalse(bridge.snapshot()["location_dirty"])

    def test_marker_write_failure_prevents_injection(self):
        bridge, marker, state = self.bridge()
        with patch.object(e.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                asyncio.run(bridge._send((1, *ORIGIN)))
        self.assertEqual(state["sets"], 0)
        self.assertFalse(marker.path.exists())
        self.assertEqual(list(self.root.glob(".piggy-location-*.tmp")), [])

    def test_close_clears_ambiguous_first_send_without_persistence(self):
        bridge = e.IOSDeviceBridge(udid=UDID, auto_start_tunneld=False)
        state = self.fake_session(bridge)

        async def failed_set(*_):
            raise OSError("first send outcome unknown")

        bridge._simulator.set = failed_set
        with self.assertRaises(OSError):
            asyncio.run(bridge._send((1, *ORIGIN)))
        self.assertFalse(bridge._ever_connected)
        self.assertTrue(bridge.close(timeout=.5))
        self.assertEqual(state["clears"], 1)

    def test_shutdown_during_first_marker_write_still_confirms_clear(self):
        bridge, marker, state = self.bridge()
        writing, release = threading.Event(), threading.Event()
        original_mark = marker.mark
        result = []

        def slow_mark(udid):
            writing.set()
            if not release.wait(1):
                raise TimeoutError("test marker write was not released")
            original_mark(udid)

        with patch.object(marker, "mark", side_effect=slow_mark):
            bridge.set_location(*ORIGIN)
            self.assertTrue(writing.wait(.5))
            closer = threading.Thread(target=lambda: result.append(bridge.close(timeout=.8)))
            closer.start()
            release.set()
            closer.join(timeout=1.5)
            self.assertFalse(closer.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(state["clears"], 1)
        self.assertFalse(marker.path.exists())

    def test_failed_send_still_leaves_a_dirty_marker(self):
        bridge, marker, state = self.bridge()

        async def failed_set(*_):
            raise OSError("USB failed after possible delivery")

        bridge._simulator.set = failed_set
        with self.assertRaises(OSError):
            asyncio.run(bridge._send((1, *ORIGIN)))
        self.assertTrue(e.DirtySessionMarker(marker.path).pending)
        self.assertTrue(bridge.clear_location(timeout=.5))
        self.assertFalse(marker.path.exists())

    def test_send_timeout_preserves_marker_until_explicit_clear(self):
        bridge, marker, state = self.bridge()
        bridge.send_timeout = .02

        async def stalled_set(*_):
            await asyncio.sleep(60)

        bridge._simulator.set = stalled_set
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(bridge._send((1, *ORIGIN)))
        self.assertTrue(marker.path.exists())
        self.assertEqual(bridge.physical_sent, 0)
        self.assertTrue(bridge.clear_location(timeout=.5))
        self.assertFalse(marker.path.exists())

    def test_speed_fuse_and_overflow_can_reconnect_and_clear_without_sending(self):
        for trigger in ("speed", "overflow"):
            with self.subTest(trigger=trigger):
                bridge, marker, state = self.bridge(trigger+".json")
                self.injected(bridge)
                if trigger == "speed":
                    bridge._last_send_at = None
                    # Establish a baseline whose age needs no rate-limit sleep.
                    bridge._last_send_at = time.monotonic()-1
                    with self.assertRaises(e.SpeedFuseError):
                        asyncio.run(bridge._send((2, *e._destination(ORIGIN, 0, 100))))
                else:
                    with patch.object(bridge, "connect", return_value={}):
                        for _ in range(129):
                            bridge.set_location(*ORIGIN)
                self.assertTrue(bridge._fuse_tripped)
                asyncio.run(bridge._disconnect())
                self.assertIsNone(bridge._simulator)
                self.assertTrue(bridge.clear_location(timeout=.7))
                self.assertGreaterEqual(state["opens"], 1)
                self.assertTrue(state["fuse_at_clear"][0])
                self.assertEqual(state["sets"], 1)
                self.assertEqual(state["clears"], 1)
                self.assertFalse(bridge._fuse_tripped)
                self.assertFalse(marker.path.exists())
                self.assertEqual(bridge.snapshot()["pending_samples"], 0)

    def test_clear_retries_a_transient_failure_before_success(self):
        bridge, marker, state = self.bridge()
        self.injected(bridge)
        state["fail_clear"] = 1
        with self.assertLogs(e.LOGGER, level="WARNING"):
            self.assertTrue(bridge.clear_location(timeout=.8))
        self.assertEqual(state["clears"], 2)
        self.assertFalse(marker.path.exists())

    def test_offline_restore_is_bounded_and_keeps_fuse_and_marker_for_retry(self):
        bridge, marker, state = self.bridge()
        self.injected(bridge)
        asyncio.run(bridge._disconnect())
        bridge._fuse_tripped = True
        state["fail_open"] = True
        before = time.monotonic()
        with self.assertLogs(e.LOGGER, level="WARNING"):
            self.assertFalse(bridge.clear_location(timeout=.08))
            self.assertTrue(bridge._clear_ack.wait(.3))
        self.assertLess(time.monotonic()-before, .5)
        self.assertTrue(bridge._fuse_tripped)
        self.assertTrue(e.DirtySessionMarker(marker.path).pending)
        self.assertIs(json.loads(marker.path.read_text())["last_clear_succeeded"], False)
        state["fail_open"] = False
        self.assertTrue(bridge.clear_location(timeout=.5))
        self.assertFalse(marker.path.exists())

    def test_close_waits_for_clear_confirmation_and_returns_the_result(self):
        bridge, marker, state = self.bridge()
        self.injected(bridge)
        bridge._simulator = None
        bridge._fuse_tripped = True
        state["fail_clear"] = 1
        with self.assertLogs(e.LOGGER, level="WARNING"):
            self.assertTrue(bridge.close(timeout=.8))
        self.assertEqual(state["clears"], 2)
        self.assertFalse(marker.path.exists())
        self.assertTrue(bridge.close(timeout=.1))

    def test_stalled_clear_is_bounded_and_can_be_retried(self):
        bridge, marker, state = self.bridge()
        self.injected(bridge)
        bridge.send_timeout = .02

        async def stalled_clear():
            await asyncio.sleep(60)

        bridge._simulator.clear = stalled_clear
        before = time.monotonic()
        with self.assertLogs(e.LOGGER, level="WARNING"):
            self.assertFalse(bridge.clear_location(timeout=.08))
            self.assertTrue(bridge._clear_ack.wait(.3))
        self.assertLess(time.monotonic()-before, .5)
        self.assertTrue(marker.path.exists())
        self.assertTrue(bridge.clear_location(timeout=.5))
        self.assertFalse(marker.path.exists())

    def test_failed_shutdown_retains_marker_and_records_failure(self):
        bridge, marker, state = self.bridge()
        self.injected(bridge)
        bridge._simulator = None
        state["fail_open"] = True
        with self.assertLogs(e.LOGGER, level="WARNING"):
            self.assertFalse(bridge.close(timeout=.1))
            bridge._close_ack.wait(.1)
        self.assertTrue(marker.path.exists())
        self.assertIs(json.loads(marker.path.read_text())["last_clear_succeeded"], False)

    def test_abrupt_process_exit_leaves_a_marker_for_next_launch(self):
        path = self.root/"crashed.json"
        program = "import os,sys; from pathlib import Path; import engine; engine.DirtySessionMarker(Path(sys.argv[1])).mark(sys.argv[2]); os._exit(17)"
        result = subprocess.run([sys.executable, "-B", "-c", program, str(path), UDID],
                                cwd=Path(__file__).resolve().parent, timeout=5)
        self.assertEqual(result.returncode, 17)
        restored = e.DirtySessionMarker(path)
        self.assertTrue(restored.pending)
        self.assertEqual(restored.udid, UDID)

    def test_stale_confirmation_never_deletes_or_overwrites_a_newer_marker(self):
        marker = e.DirtySessionMarker(self.root/"session.json")
        marker.mark(UDID)
        newer = {**marker.record, "session_id": "newer-session"}
        marker.path.write_text(json.dumps(newer))
        with self.assertRaises(RuntimeError):
            marker.cleared()
        with self.assertLogs(e.LOGGER, level="ERROR"):
            marker.failed("old clear failed")
        self.assertEqual(json.loads(marker.path.read_text()), newer)

    def application_with_marker(self, *, sandbox=False):
        config = self.root/"config.json"
        marker = e.DirtySessionMarker(self.root/"config.location-session.json")
        marker.mark(UDID)
        api = main.DesktopAPI(config, sandbox=sandbox)
        self.addCleanup(lambda: api._shutdown(timeout=.1))
        state = self.fake_session(api._bridge)
        api._bridge._simulator = None
        return api, marker, state

    def test_startup_warns_and_blocks_new_session_until_matching_device_is_cleared(self):
        api, marker, state = self.application_with_marker()
        with patch.object(api._bridge, "connect") as connect, \
             patch.object(api._protection, "start"), patch.object(api, "_begin_clock_check"):
            api._start_services()
        connect.assert_not_called()
        self.assertTrue(api.bootstrap()["data"]["state"]["recovery_required"])
        self.assertEqual(api._bridge.udid, UDID)
        result = api.lock_start({"points": TRACK, "profile": "standard", "target_km": 2, "checks": [True]*7})
        self.assertEqual(result["error"]["code"], "recovery_required")
        result = api.recover_previous_location()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["recovery_required"])
        self.assertFalse(result["data"]["recovering"])
        self.assertTrue(result["data"]["restore_dispatched"])
        self.assertNotIn("udid", result["data"]["device"])
        self.assertFalse(marker.path.exists())
        self.assertEqual(state["sets"], 0)

    def test_shared_fault_log_redacts_device_ids_and_user_paths(self):
        log = BoundedFaultLog(self.root/"Chill"/"last_session.log")
        self.addCleanup(log.close)
        message = r"00008110-001234ABCD56789E: C:\Users\Local User\driver.log and /Users/localname/session"
        record = logging.LogRecord("engine", logging.ERROR, __file__, 1, message, (), None)
        log.handle(record)
        sanitized = e._diagnostic_text(message)
        self.assertIn(r"C:\Users\<REDACTED>\driver.log", sanitized)
        self.assertIn("/Users/<REDACTED>/session", sanitized)
        written = log.path.read_text(encoding="utf-8")
        self.assertIn(sanitized, written)
        self.assertNotIn("00008110-001234ABCD56789E", written)

    def test_device_diagnostics_distinguish_setup_failures(self):
        for error, code in (
            (type("NotPairedError", (Exception,), {})(), "device_trust_required"),
            (type("DeveloperModeIsNotEnabledError", (Exception,), {})(), "device_developer_mode_required"),
            (e.DeviceConnectionError("tunnel_unavailable", "AccessDeniedError: requires root/admin"),
             "tunnel_admin_required"),
            (ConnectionError("USB disconnected"), "device_connection_failed"),
        ):
            with self.subTest(code=code):
                self.assertEqual(e._device_error_code(error), code)

    def test_sandbox_detects_residue_but_never_connects_or_deletes_it(self):
        api, marker, state = self.application_with_marker(sandbox=True)
        self.assertTrue(api.get_state()["data"]["recovery_required"])
        result = api.recover_previous_location()
        self.assertEqual(result["error"]["code"], "recovery_sandbox")
        self.assertTrue(marker.path.exists())
        self.assertEqual(state["opens"], 0)

    def test_corrupt_marker_blocks_automatic_device_selection(self):
        config = self.root/"config.json"
        path = self.root/"config.location-session.json"
        path.write_text("broken")
        with self.assertLogs(e.LOGGER, level="ERROR"):
            api = main.DesktopAPI(config, sandbox=False)
        self.addCleanup(lambda: api._shutdown(timeout=.02))
        self.assertTrue(api.get_state()["data"]["recovery_invalid"])
        self.assertEqual(api.recover_previous_location()["error"]["code"], "recovery_invalid")
        self.assertIsNone(api._bridge._thread)
        self.assertEqual(path.read_text(), "broken")

    def test_recovery_rejects_a_device_different_from_the_marker(self):
        marker = e.DirtySessionMarker(self.root/"session.json")
        marker.mark(UDID)
        with self.assertRaises(ValueError):
            e.IOSDeviceBridge(udid="another-device", dirty_marker=marker)

    def test_shutdown_exception_still_closes_other_resources_and_keeps_marker(self):
        api, marker, state = self.application_with_marker()
        with patch.object(api._bridge, "close", side_effect=OSError("worker unavailable")), \
             patch.object(api._runner, "close") as runner_close, \
             patch.object(api._protection, "close") as protection_close, \
             self.assertLogs(main.LOGGER, level="ERROR"):
            self.assertFalse(api._shutdown(timeout=.1))
        runner_close.assert_called_once()
        protection_close.assert_called_once()
        self.assertTrue(marker.path.exists())


class ShutdownHookTests(unittest.TestCase):
    def test_sigterm_unwinds_main_and_runs_cleanup_before_restoring_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        api, server = MagicMock(), MagicMock()
        api._closed = False
        server.url = "http://127.0.0.1:8765"
        window = SimpleNamespace(events=SimpleNamespace(closing=MagicMock(), closed=MagicMock()))
        webview = SimpleNamespace(create_window=MagicMock(return_value=window),
                                  start=lambda **_: signal.raise_signal(signal.SIGTERM))
        with patch.object(main, "DesktopAPI", return_value=api), \
             patch.object(main, "LocalServer", return_value=server), \
             patch.dict(sys.modules, {"webview": webview}):
            with self.assertRaises(SystemExit) as stopped:
                main.main(["--sandbox"])
        self.assertEqual(stopped.exception.code, 128+signal.SIGTERM)
        api._shutdown.assert_called_once()
        server.close.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_console_callback_is_retained_handles_close_and_is_unregistered(self):
        api = MagicMock()
        handler_api = MagicMock(return_value=1)
        kernel = SimpleNamespace(SetConsoleCtrlHandler=handler_api)
        hooks = main.ShutdownHooks(api)
        with patch.object(main.sys, "platform", "win32"), \
             patch.object(main.ctypes, "WINFUNCTYPE", return_value=lambda fn: fn, create=True), \
             patch.object(main.ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
            hooks.install()
            try:
                callback = hooks._console_callback
                self.assertIsNotNone(callback)
                self.assertFalse(callback(0))
                api._shutdown.assert_not_called()
                for event in (2, 5, 6):
                    self.assertTrue(callback(event))
                self.assertEqual(api._shutdown.call_count, 3)
                api._shutdown.assert_called_with(timeout=3.5)
            finally:
                hooks.close()
        handler_api.assert_any_call(callback, True)
        handler_api.assert_called_with(callback, False)

    def test_console_cleanup_failure_does_not_escape_the_native_callback(self):
        api = MagicMock()
        api._shutdown.side_effect = OSError("cleanup unavailable")
        with self.assertLogs(main.LOGGER, level="ERROR"):
            self.assertTrue(main.ShutdownHooks(api)._console_event(2))


if __name__ == "__main__":
    unittest.main()
