"""Desktop boundary tests: no real phone, firewall changes, or home-directory writes."""
import json
import contextlib
import io
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import engine
import main


TRACK = [{"lat": 31.2300, "lon": 121.4730}, {"lat": 31.2300, "lon": 121.4740},
         {"lat": 31.2310, "lon": 121.4740}, {"lat": 31.2310, "lon": 121.4730}]


class APICase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="piggy-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"config.json"
        self.api = main.DesktopAPI(self.path, sandbox=True)
        self.addCleanup(self.api._shutdown)
        self.api._runner._control_clock = lambda: self.api._runner.elapsed_s
        self.api._clock = {"status": "synced", "offset_s": 0.5, "checked_at": time.time()}
        self.api._clock_checked_mono = time.monotonic()

    def lock(self):
        response = self.api.lock_start({"points": TRACK, "profile": "standard", "target_km": 2,
                                        "checks": [True]*7})
        self.assertTrue(response["ok"], response)
        return response["data"]

    def cruise(self):
        self.lock()
        self.api._countdown_deadline = time.monotonic()-1
        response = self.api.start_cruise()
        self.assertTrue(response["ok"], response)
        return response["data"]


class ConfigTests(APICase):
    def test_width_persists_validates_and_is_locked_during_a_session(self):
        self.assertTrue(self.api.save_preferences({'track_width_m': 2})['ok'])
        self.assertEqual(main.ConfigStore(self.path).read()['track_width_m'], 2)
        for value in (0, 8.1, True, float('nan')):
            self.assertEqual(self.api.save_preferences({'track_width_m': value})['error']['code'], 'invalid_track_width')
        self.lock()
        self.assertEqual(self.api._runner.track_width_m, 2)
        self.assertEqual(self.api.save_preferences({'track_width_m': 8})['error']['code'], 'track_locked')

    def test_calibrated_fixture_round_trip_matches_map_and_trace(self):
        from scripts.preview_calibrated import load_fixture
        spec, anchors = load_fixture()
        response = self.api.preview_track(anchors)['data']
        wgs, geometry = self.api._geometry(anchors)
        self.assertAlmostEqual(response['length_m'], 2*spec['straight_m']+2*3.141592653589793*spec['radius_m'], delta=0.5)
        # A double conversion would move this public stadium by hundreds of m.
        for node, rendered in zip(geometry.points[::2], response['points']):
            lon, lat = engine.wgs84_to_gcj02(node['lon'], node['lat'])
            self.assertLess(engine.haversine_distance(lat, lon, *rendered), 0.0001)
        for original, converted in zip(anchors, wgs):
            lon, lat = engine.wgs84_to_gcj02(converted[1], converted[0])
            self.assertLess(engine.haversine_distance(original['lat'], original['lon'], lat, lon), 0.001)
        self.api._runner.load_track(wgs, track_width_m=spec['track_width_m'])
        self.api._runner.lock_start(background=False)
        self.api._runner.start_cruising(background=False)
        self.api._runner.step()
        last = self.api.get_trace()['data']['points'][-1]
        position = self.api._state()['gcj_position']
        self.assertLess(engine.haversine_distance(last['lat'], last['lon'], position['lat'], position['lon']), 0.0001)

    def test_preferences_and_gcj_track_round_trip(self):
        self.assertTrue(self.api.save_preferences({"language": "en", "profile": "high_cadence"})["ok"])
        self.assertTrue(self.api.save_track(TRACK)["ok"])
        saved = main.ConfigStore(self.path).read()
        self.assertEqual(saved["track_points"], TRACK)
        self.assertEqual(saved["language"], "en")
        self.assertEqual(saved["profile"], "high_cadence")
        self.assertEqual(len(list(self.path.parent.glob(".piggy-*.tmp"))), 0)

    def test_failed_atomic_replace_preserves_both_disk_and_memory(self):
        self.api.save_track(TRACK)
        before = self.path.read_bytes()
        with patch.object(main.os, "replace", side_effect=OSError("read only")):
            result = self.api.save_preferences({"language": "en"})
        self.assertEqual(result["error"]["code"], "config_save_failed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.api._config.read()["language"], "zh")

    def test_corrupt_file_is_preserved_until_explicit_save(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertLogs(main.LOGGER, level="WARNING"):
            store = main.ConfigStore(self.path)
        self.assertEqual(store.issue, "config_load_failed")
        self.assertEqual(self.path.read_text(), "broken")

    def test_clear_track_is_persisted(self):
        self.api.save_track(TRACK)
        self.assertTrue(self.api.save_track([])["ok"])
        self.assertEqual(main.ConfigStore(self.path).read()["track_points"], [])

    def test_invalid_numeric_and_preferences_rejected(self):
        for target in (True, float("nan"), float("inf"), 0, 101):
            self.assertFalse(self.api.save_preferences({"target_km": target})["ok"])
        self.assertFalse(self.api.save_preferences({"unexpected": 1})["ok"])
        self.assertFalse(self.api.save_preferences({"language": "fr"})["ok"])


class SafetyTests(APICase):
    def test_trace_feed_is_gcj_delta_only_and_rejects_invalid_cursors(self):
        state = self.lock()
        trace = self.api.get_trace()["data"]
        self.assertEqual(len(trace["points"]), 1)
        self.assertAlmostEqual(trace["points"][0]["lat"], state["gcj_position"]["lat"])
        self.assertAlmostEqual(trace["points"][0]["lon"], state["gcj_position"]["lon"])
        delta = self.api.get_trace(trace["sequence"], trace["session_id"])["data"]
        self.assertEqual(delta["points"], [])
        self.assertFalse(delta["reset"])
        self.assertEqual(self.api.get_trace(True)["error"]["code"], "invalid_request")
        self.assertNotIn("points", self.api.get_state()["data"])

    def test_engine_failures_are_distinct_from_recoverable_route_errors(self):
        self.lock()
        self.api._runner.last_error = "detour_crosses_infield"
        self.assertFalse(self.api.get_state()["data"]["engine_issue"])
        self.api._runner._stop_reason = "engine_error"
        self.api._runner.last_error = "ArithmeticError: internal diagnostic"
        self.api._runner._finish_stop()
        state = self.api.get_state()["data"]
        self.assertTrue(state["engine_issue"])
        self.assertTrue(state["session_active"])
        self.assertEqual(state["stop_reason"], "engine_error")
        self.assertFalse(self.api._on_closing())

    def test_rest_api_validates_phase_and_keeps_close_protection(self):
        self.assertEqual(self.api.pause()["error"]["code"], "rest_unavailable")
        self.lock()
        self.assertEqual(self.api.pause()["error"]["code"], "rest_unavailable")
        self.api._countdown_deadline = time.monotonic()-1
        self.assertTrue(self.api.start_cruise()["ok"])
        with self.api._runner._lock:
            self.api._runner._tick(.95)
            self.api._runner._tick(.95)
            self.assertEqual(self.api.pause()["data"]["pause_phase"], "slowing")
            self.assertEqual(self.api.resume()["error"]["code"], "control_cooldown")
            for _ in range(10):
                self.api._runner._tick(.95)
                if self.api._runner.snapshot()["pause_phase"] == "WALKING_REST":
                    break
            state = self.api.get_state()["data"]
            self.assertEqual(state["pause_phase"], "WALKING_REST")
            self.assertTrue(state["session_active"])
            self.assertFalse(self.api._on_closing())
            self.assertTrue(self.api.resume()["ok"])
            held = self.api.hold_position()
            self.assertTrue(held["ok"], held)
            self.assertEqual(held["data"]["state"], "SAFE_STOPPED")
            self.assertFalse(self.api._on_closing())
        self.assertTrue(self.api.safe_settle({"submitted":True,"success":True,"app_closed":True})["ok"])
        self.assertEqual(self.api.resume()["error"]["code"], "resume_unavailable")

    def test_target_walk_remains_active_until_manual_settlement(self):
        self.lock()
        self.api._countdown_deadline=time.monotonic()-1
        self.assertTrue(self.api.start_cruise()["ok"])
        with self.api._runner._lock:
            self.api._runner.target_m=100
            for _ in range(150):self.api._runner._tick(.95)
            state=self.api.get_state()["data"]
            self.assertTrue(state["target_walking"])
            self.assertTrue(state["target_reached"])
            self.assertEqual(state["pause_phase"],"WALKING_REST")
            self.assertGreaterEqual(state["speed_mps"],1.2)
            self.assertTrue(state["session_active"])
            self.assertFalse(self.api._on_closing())
            self.assertEqual(self.api.resume()["error"]["code"],"resume_unavailable")
        self.assertTrue(self.api.safe_settle({"submitted":True,"success":True,"app_closed":True})["ok"])
        self.assertFalse(self.api.get_state()["data"]["session_active"])
        self.assertEqual(self.api._runner.speed_mps,0)

    def test_extra_distance_preferences_persist_and_lock_with_the_session(self):
        self.assertTrue(self.api.save_preferences({"extra_distance":True})["ok"])
        self.assertTrue(main.ConfigStore(self.path).read()["extra_distance"])
        self.assertFalse(self.api.save_preferences({"extra_distance":"yes"})["ok"])
        state = self.lock()
        self.assertGreaterEqual(state["target_m"], state["requested_target_m"])
        self.assertLessEqual(state["target_m"]-state["requested_target_m"], 60)
        self.assertEqual(self.api.save_preferences({"extra_distance":False})["error"]["code"], "track_locked")

    def test_device_mode_requires_connection_before_locking(self):
        self.api._sandbox=False
        response=self.api.lock_start({"points":TRACK,"profile":"standard","target_km":2,"checks":[True]*7})
        self.assertEqual(response["error"]["code"],"device_required")
        self.assertFalse(self.api._session_active)

    def test_all_seven_checks_are_required_at_backend(self):
        incomplete = [[True]*6, [1]*7]
        for index in range(7):
            checks = [True]*7
            checks[index] = False
            incomplete.append(checks)
        for checks in incomplete:
            response = self.api.lock_start({"points": TRACK, "profile": "standard", "target_km": 2, "checks": checks})
            self.assertEqual(response["error"]["code"], "checklist_required")
        self.assertFalse(self.api._session_active)

    def test_clock_skew_and_stale_cannot_be_overridden_by_checkbox(self):
        for status in ("skew", "checking"):
            self.api._clock["status"] = status
            result = self.api.lock_start({"points": TRACK, "profile": "standard", "target_km": 2, "checks": [True]*7})
            self.assertEqual(result["error"]["code"], "clock_required")
        self.api._clock["status"] = "synced"
        self.api._clock_checked_mono = time.monotonic()-601
        result = self.api.lock_start({"points": TRACK, "profile": "standard", "target_km": 2, "checks": [True]*7})
        self.assertEqual(result["error"]["code"], "clock_required")

    def test_offline_clock_allows_lock_after_manual_time_check(self):
        with patch.object(engine, "check_ntp_offset", side_effect=engine.TimeSyncError("unreachable")):
            self.api._check_clock()
        self.api._clock_checked_mono -= 601
        self.assertEqual(self.lock()["clock"]["status"], "offline")

    def test_five_second_hold_is_enforced_by_backend(self):
        state = self.lock()
        self.assertGreater(state["countdown_s"], 4.5)
        self.assertEqual(self.api.start_cruise()["error"]["code"], "countdown_active")
        self.api._countdown_deadline = time.monotonic()-1
        self.assertEqual(self.api.start_cruise()["data"]["state"], "CRUISING_LOOP")

    def test_first_device_sample_must_be_sent_before_countdown(self):
        self.api._sandbox = False  # Enforce device gating using the sandbox sender.
        bridge = self.api._bridge
        bridge.mode = engine.BridgeMode.CONNECTED
        bridge.udid = "offline-test-device"
        state = self.lock()
        self.assertEqual(state["start_phase"], "injecting")
        self.assertEqual(state["countdown_s"], 0)
        self.assertIsNone(self.api._countdown_deadline)
        self.assertNotIn("udid", state["device"])
        self.assertEqual(self.api.start_cruise()["error"]["code"], "start_injecting")
        with bridge._lock:
            bridge._sent_sequence = self.api._runner.start_sequence
        state = self.api.get_state()["data"]
        self.assertEqual(state["start_phase"], "countdown")
        self.assertGreater(state["countdown_s"], 4.5)
        self.assertEqual(self.api.start_cruise()["error"]["code"], "countdown_active")

    def test_countdown_disconnect_latches_even_if_bridge_already_reconnected(self):
        self.api._sandbox = False
        bridge = self.api._bridge
        bridge.mode = engine.BridgeMode.CONNECTED
        self.lock()
        with bridge._lock:
            bridge._sent_sequence = self.api._runner.start_sequence
        self.assertEqual(self.api.get_state()["data"]["start_phase"], "countdown")
        # Simulate a disconnect and successful reconnection between UI polls.
        with bridge._lock:
            bridge.reconnect_attempts += 1
        state = self.api.get_state()["data"]
        self.assertEqual((state["start_phase"], state["countdown_s"]), ("interrupted", 0))
        self.assertEqual(self.api.start_cruise()["error"]["code"], "start_interrupted")
        self.assertTrue(state["session_active"])

    def test_discard_locked_session_does_not_require_successful_submission(self):
        self.lock()
        self.assertEqual(self.api.safe_settle({"discarded": True})["error"]["code"], "settlement_required")
        with patch.object(self.api._bridge, "clear_location", wraps=self.api._bridge.clear_location) as clear:
            result = self.api.safe_settle({"discarded": True, "app_closed": True})
        clear.assert_called_once()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["session_active"])
        self.assertFalse(result["data"]["restore_dispatched"], "sandbox does not send a phone command")
        self.assertTrue(self.api._on_closing())

    def test_wgs_injection_and_gcj_display_are_consistent(self):
        state = self.lock()
        wgs, gcj = state["position"], state["gcj_position"]
        lon, lat = engine.wgs84_to_gcj02(wgs["lon"], wgs["lat"])
        self.assertAlmostEqual(gcj["lat"], lat, places=10)
        self.assertAlmostEqual(gcj["lon"], lon, places=10)
        self.assertNotAlmostEqual(wgs["lon"], gcj["lon"], places=4)

    def test_active_track_and_profile_are_immutable_but_language_is_not(self):
        self.lock()
        self.assertEqual(self.api.save_track(TRACK)["error"]["code"], "track_locked")
        self.assertEqual(self.api.save_preferences({"profile": "high_cadence"})["error"]["code"], "track_locked")
        self.assertTrue(self.api.save_preferences({"language": "en"})["ok"])

    def test_close_guard_notifies_without_deadlocking_ui(self):
        self.lock()
        notified = threading.Event()
        self.api._attach_window(SimpleNamespace(evaluate_js=lambda script: notified.set()))
        self.assertFalse(self.api._on_closing())
        self.assertTrue(notified.wait(1))
        self.api._runner._finish_stop()
        self.assertFalse(self.api._on_closing(), "automatic stop still holds simulated location")

    def test_settlement_confirmation_and_restore(self):
        self.cruise()
        self.assertEqual(self.api.safe_settle({"submitted": True})["error"]["code"], "settlement_required")
        result = self.api.safe_settle({"submitted": True, "success": True, "app_closed": True})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["session_active"])
        self.assertTrue(self.api._on_closing())
        self.assertEqual(self.api._bridge.mode, engine.BridgeMode.CLOSED)
        self.lock()  # A fresh session after retirement is supported.

    def test_failed_restore_keeps_close_guard_and_can_retry(self):
        self.lock()
        confirmation = {"submitted": True, "success": True, "app_closed": True}
        with patch.object(self.api._bridge, "clear_location", return_value=False):
            result = self.api.safe_settle(confirmation)
        self.assertEqual(result["error"]["code"], "restore_failed")
        self.assertFalse(self.api._on_closing())
        self.assertTrue(self.api._runner_retired)
        self.assertTrue(self.api.safe_settle(confirmation)["ok"])
        self.assertTrue(self.api._on_closing())

    def test_draft_is_not_submitted_until_explicit_request(self):
        self.cruise()
        self.assertIsNone(self.api._runner._pending_detour)
        target = {"lat": 31.2312, "lon": 121.4745, "is_target": True}
        response = self.api.submit_detour([target])
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["data"]["state"]["detour_pending"])
        self.assertGreater(len(response["data"]["path"]), 2)
        self.assertEqual(self.api.submit_detour([target])["error"]["code"], "detour_busy")

    def test_no_target_detour_rejected(self):
        self.cruise()
        response = self.api.submit_detour([{"lat": 31.2312, "lon": 121.4745}])
        self.assertEqual(response["error"]["code"], "target_required")


class SearchTests(APICase):
    def test_campus_search_is_keyless_and_localized(self):
        zh = self.api.search_places("清华", "zh")["data"]
        en = self.api.search_places("tsinghua", "en")["data"]
        self.assertEqual(zh["results"][0]["name"], "清华大学")
        self.assertEqual(en["results"][0]["name"], "Tsinghua University")
        self.assertEqual(zh["remote"], "not_needed")

    def test_coordinate_order_fallback(self):
        for query in ("31.23, 121.47", "121.47,31.23", "31.23，121.47"):
            point = self.api.search_places(query)["data"]["results"][0]
            self.assertEqual((point["lat"], point["lon"]), (31.23, 121.47))
        self.assertFalse(self.api.search_places("91,200")["ok"])

    def test_public_search_failure_falls_back_without_crash(self):
        opener = SimpleNamespace(open=MagicMock(side_effect=OSError("offline")))
        with patch.object(main.urllib.request, "build_opener", return_value=opener):
            response = self.api.search_places("a place outside built-in campuses", "en")
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"], {"results": [], "remote": "unavailable"})

    def test_malformed_remote_search_envelopes_fall_back(self):
        for index, payload in enumerate([None, [], {"data": {"tips": {"unexpected": "object"}}}]):
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            with patch.object(main.urllib.request, "build_opener", return_value=opener):
                response = self.api.search_places(f"uncatalogued place {index}", "en")
            self.assertTrue(response["ok"])
            self.assertEqual(response["data"]["remote"], "unavailable")

    def test_malformed_tip_does_not_discard_valid_suggestions(self):
        payload = {"data": {"tips": [None, {"name": "Test stadium", "location": "121.45,31.02"}]}}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(main.urllib.request, "build_opener", return_value=opener):
            response = self.api.search_places("uncatalogued stadium", "en")
        self.assertEqual(response["data"]["remote"], "available")
        self.assertEqual(response["data"]["results"][0]["name"], "Test stadium")


class WindowsTests(unittest.TestCase):
    def test_windows_startup_queries_driver_without_changing_firewall(self):
        protection = main.WindowsProtection()
        protection._stop.set()
        kernel = SimpleNamespace(SetThreadExecutionState=MagicMock(return_value=1))
        with patch.object(main.ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True), \
             patch.object(protection, "_quiet", return_value=SimpleNamespace(returncode=0, stdout="STATE: 4 RUNNING")) as command:
            protection._run()
        command.assert_called_once_with(["sc.exe", "query", "Apple Mobile Device Service"])
        self.assertEqual(protection.snapshot()["driver"], "running")

    def test_windows_console_encoding_preserves_non_ascii_program_paths(self):
        kernel = SimpleNamespace(GetOEMCP=MagicMock(return_value=936))
        with patch.object(main.sys, "platform", "win32"), \
             patch.object(main.ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True), \
             patch.object(main.subprocess, "run") as command:
            main.WindowsProtection._quiet(["sc.exe", "query", "Apple Mobile Device Service"])
        self.assertEqual(command.call_args.kwargs["encoding"], "cp936")

    def test_guard_and_restore_with_missing_driver(self):
        protection = main.WindowsProtection()
        protection._stop.set()
        kernel = SimpleNamespace(SetThreadExecutionState=MagicMock(return_value=1))
        with patch.object(main.ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True), \
             patch.object(protection, "_quiet", return_value=SimpleNamespace(returncode=1060, stdout="")):
            protection._run()
        self.assertEqual(kernel.SetThreadExecutionState.call_args_list[0].args, (0x80000003,))
        self.assertEqual(kernel.SetThreadExecutionState.call_args_list[-1].args, (0x80000000,))
        self.assertEqual(protection.snapshot()["driver"], "missing")


class LocalHTTPTests(APICase):
    def setUp(self):
        super().setUp()
        try:
            self.server = main.LocalServer(self.api)
        except PermissionError:
            self.skipTest("this sandbox prohibits binding a loopback socket")
        self.server.start()
        self.addCleanup(self.server.close)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, *, token=True, origin=None, host=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer "+self.server.token
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        request = urllib.request.Request(self.server.url+path, data=b'{"args":[]}', headers=headers)
        return self.opener.open(request, timeout=2)

    def test_bootstrap_requires_session_token(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/bootstrap", token=False)
        self.assertEqual(raised.exception.code, 403)
        with self.request("/api/bootstrap") as response:
            self.assertTrue(json.load(response)["ok"])

    def test_cross_origin_and_host_spoofing_are_rejected(self):
        for kwargs in ({"origin": "https://example.test"}, {"host": "attacker.example"}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/bootstrap", **kwargs)
            self.assertEqual(raised.exception.code, 403)

    def test_private_methods_and_project_files_are_not_served(self):
        for path in ("/api/_shutdown", "/api/_config"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(path)
            self.assertEqual(raised.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(self.server.url+"/main.py", timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_rest_methods_are_token_protected_and_reach_phase_validation(self):
        for method,code in (("pause","rest_unavailable"),("resume","resume_unavailable")):
            with self.request("/api/"+method) as response:
                self.assertEqual(json.load(response)["error"]["code"], code)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/"+method, token=False)
            self.assertEqual(raised.exception.code, 403)

    def test_ui_and_bundled_fallback_are_served(self):
        with self.opener.open(self.server.url, timeout=2) as response:
            text = response.read().decode()
            self.assertIn(self.server.token, text)
            self.assertNotIn("__PIGGY_SESSION_TOKEN__", text)
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        for path in ("/vendor/leaflet.js", "/vendor/leaflet.css"):
            with self.opener.open(self.server.url+path, timeout=2) as response:
                self.assertGreater(len(response.read()), 10000)

    def test_chill_icons_are_served_without_exposing_reference_photo(self):
        for route, relative, content_type in (
                ('/favicon.ico', 'chill.ico', 'image/x-icon'),
                ('/assets/chill-32.png', 'chill-32.png', 'image/png'),
                ('/assets/chill-180.png', 'chill-180.png', 'image/png'),
                ('/assets/chill-192.png', 'chill-192.png', 'image/png')):
            with self.opener.open(self.server.url+route, timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertEqual(response.read(), (Path(__file__).parent/'ui/assets'/relative).read_bytes())
        for route in ('/IMG_0288.jpg', '/assets/../../IMG_0288.jpg'):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.opener.open(self.server.url+route, timeout=2)
            self.assertEqual(raised.exception.code, 404)



class EntryPointTests(unittest.TestCase):
    def test_private_tunnel_dispatch_never_creates_a_window_or_api(self):
        calls = []
        original = sys.argv
        def fake_cli():
            calls.append(list(sys.argv))
            raise SystemExit(0)
        module = SimpleNamespace(main=fake_cli)
        arguments = ["--host", "127.0.0.1", "--port", "49151", "--protocol", "tcp"]
        with patch.dict(sys.modules, {"pymobiledevice3.__main__": module}), \
             patch.object(main, "DesktopAPI") as factory, patch.object(main, "LocalServer") as server:
            self.assertEqual(main.main(["--piggy-tunneld", *arguments]), 0)
        factory.assert_not_called()
        server.assert_not_called()
        self.assertEqual(calls, [[sys.executable, "remote", "tunneld", *arguments]])
        self.assertIs(sys.argv, original)

    def test_private_tunnel_rejects_non_loopback_or_unrelated_commands(self):
        with patch.object(main, "DesktopAPI") as factory, self.assertLogs(main.LOGGER, level="ERROR"):
            self.assertEqual(main.main(["--piggy-tunneld", "--host", "0.0.0.0"]), 2)
        factory.assert_not_called()

    def test_private_tunnel_preserves_exit_codes_and_restores_argv_on_failure(self):
        original = sys.argv
        for outcome, expected in ((7, 7), (None, 0), ("error", 1)):
            module = SimpleNamespace(main=MagicMock(side_effect=SystemExit(outcome)))
            with patch.dict(sys.modules, {"pymobiledevice3.__main__": module}):
                self.assertEqual(main._run_tunneld(["--help"]), expected)
            self.assertIs(sys.argv, original)
        with patch.dict(sys.modules, {"pymobiledevice3.__main__": SimpleNamespace(main=MagicMock(side_effect=ImportError("missing")))}), \
             self.assertLogs(main.LOGGER, level="ERROR"):
            self.assertEqual(main._run_tunneld(["--help"]), 1)
        self.assertIs(sys.argv, original)

    def test_windowed_streams_are_usable_before_third_party_imports(self):
        files = []
        real_open = open
        def capture(*args, **kwargs):
            stream = real_open(*args, **kwargs)
            files.append(stream)
            return stream
        with patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), \
             patch.object(sys, "stderr", None), patch("builtins.open", side_effect=capture), \
             patch.object(main.atexit, "register") as register:
            try:
                main._prepare_windowed_streams()
                self.assertEqual(sys.stdin.read(), "")
                self.assertEqual(sys.stdout.write("quiet"), 5)
                self.assertEqual(sys.stderr.write("quiet"), 5)
                main._prepare_windowed_streams()
                self.assertEqual(register.call_count, 3)
            finally:
                for stream in files:
                    stream.close()

    def test_browser_preview_always_forces_sandbox(self):
        api, server, stop = MagicMock(), MagicMock(), MagicMock()
        server.url = "http://127.0.0.1:8765"
        stop.wait.side_effect = KeyboardInterrupt
        with patch.object(main, "DesktopAPI", return_value=api) as factory, \
             patch.object(main, "LocalServer", return_value=server), \
             patch.object(main.threading, "Event", return_value=stop), contextlib.redirect_stdout(io.StringIO()):
            result = main.main(["--preview"])
        self.assertEqual(result, 0)
        factory.assert_called_once_with(None, sandbox=True)
        api._shutdown.assert_called_once()
        server.close.assert_called_once()

    def test_native_window_dimensions_and_close_guard_are_wired(self):
        class Event:
            def __init__(self):
                self.handlers = []
            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self
        window = SimpleNamespace(events=SimpleNamespace(closing=Event(), closed=Event()))
        api, server = MagicMock(), MagicMock()
        server.url = "http://127.0.0.1:8765"
        webview = SimpleNamespace(create_window=MagicMock(return_value=window), start=MagicMock())
        with patch.object(main, "DesktopAPI", return_value=api), \
             patch.object(main, "LocalServer", return_value=server), \
             patch.dict(sys.modules, {"webview": webview}):
            result = main.main(["--sandbox"])
        self.assertEqual(result, 0)
        self.assertEqual(webview.create_window.call_args.kwargs["width"], 1380)
        self.assertEqual(webview.create_window.call_args.kwargs["height"], 860)
        self.assertEqual(window.events.closing.handlers, [api._on_closing])
        self.assertEqual(window.events.closed.handlers, [api._shutdown])
        webview.start.assert_called_once_with(debug=False)


if __name__ == "__main__":
    unittest.main()
