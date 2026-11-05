"""Real-process lifecycle and offline ownership/geometry fault probes."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import engine as e
import main
from runtime_support import BoundedFaultLog, DaemonDriverExecutor, InstanceRunningError, ProcessFileLock


ROOT = Path(__file__).resolve().parent
ORIGIN = (31.2304, 121.4737)


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan zombie has exited; a platform reaper may acknowledge it later.
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                            text=True, timeout=.5)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


class HardeningTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="piggy-hardening-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.udid = "probe-"+uuid.uuid4().hex

    def process(self, program, *arguments):
        return subprocess.Popen([sys.executable, "-B", "-c", program, *map(str, arguments)], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_file(self, path):
        deadline = time.monotonic()+6
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), "managed process did not become ready")
        return int(path.read_text())

    def kill_tree(self, pid):
        if not alive(pid):
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=2)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_parent_sigkill_reaps_managed_wrapper_and_tunnel_within_two_seconds(self):
        wrapper_file, worker_file = self.root/"wrapper", self.root/"worker"
        program = """
import engine,os,sys,time
from pathlib import Path
worker = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)'
bridge=engine.IOSDeviceBridge(tunneld_command=[sys.executable,'-B','-c',worker,sys.argv[2]])
bridge._spawn_tunneld()
Path(sys.argv[1]).write_text(str(bridge._children[0].pid))
time.sleep(60)
"""
        parent = self.process(program, wrapper_file, worker_file)
        wrapper = worker = None
        try:
            wrapper = self.wait_file(wrapper_file)
            worker = self.wait_file(worker_file)
            self.assertTrue(alive(wrapper))
            self.assertTrue(alive(worker))
            started = time.monotonic()
            parent.kill()  # Only the parent, never its child's separate group.
            parent.wait(timeout=1)
            while (alive(wrapper) or alive(worker)) and time.monotonic()-started < 2:
                time.sleep(.03)
            self.assertFalse(alive(wrapper), "managed tunnel wrapper survived parent death")
            self.assertFalse(alive(worker), "tunnel worker survived parent death")
            self.assertLess(time.monotonic()-started, 2)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=2)
            if wrapper is not None:
                self.kill_tree(wrapper)

    def test_normal_bridge_close_reaps_managed_tunnel_tree(self):
        ready = self.root/"worker"
        worker = "import os,sys,time,signal;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
        bridge = e.IOSDeviceBridge(tunneld_command=[sys.executable, "-B", "-c", worker, str(ready)])
        self.addCleanup(bridge.close)
        bridge._spawn_tunneld()
        child = bridge._children[0]
        try:
            pid = self.wait_file(ready)
            self.assertTrue(bridge.close(timeout=.3))
            self.assertIsNotNone(child.poll())
            deadline = time.monotonic()+1
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(alive(pid))
        finally:
            self.kill_tree(child.pid)

    def test_finished_private_cli_cannot_leave_vendor_worker_behind(self):
        ready = self.root/"vendor-worker"
        program = """
import subprocess,sys,time
from pathlib import Path
worker='import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)'
subprocess.Popen([sys.executable,'-B','-c',worker,sys.argv[1]])
while not Path(sys.argv[1]).exists(): time.sleep(.01)
"""
        bridge = e.IOSDeviceBridge(tunneld_command=[sys.executable, "-B", "-c", program, str(ready)])
        self.addCleanup(bridge.close)
        bridge._spawn_tunneld()
        child = bridge._children[0]
        try:
            worker = self.wait_file(ready)
            child.wait(timeout=3)
            for reader in bridge._tunnel_readers:
                reader.join(timeout=.3)
            deadline = time.monotonic()+1
            while alive(worker) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(alive(worker))
            self.assertIn("status 0", "\n".join(bridge._tunnel_log))
        finally:
            self.kill_tree(child.pid)

    def test_permanently_blocked_sync_set_does_not_hold_process_exit_or_confirm_clear(self):
        marker = self.root/"session.json"
        program = """
import asyncio,engine,sys,threading
from pathlib import Path
marker=engine.DirtySessionMarker(Path(sys.argv[1]))
bridge=engine.IOSDeviceBridge(udid=sys.argv[2],dirty_marker=marker,send_timeout=.03)
bridge.connect=lambda: {}
class Simulator:
 def set(self,*args): threading.Event().wait(60)
 def clear(self): raise AssertionError('clear must not overtake a late set')
bridge._simulator=Simulator()
async def run():
 try: await bridge._send((1,31.2304,121.4737))
 except asyncio.TimeoutError: print('set_timed_out',flush=True)
asyncio.run(run())
assert not bridge.close(timeout=.12)
assert marker.pending
print('process_can_exit',flush=True)
"""
        child = self.process(program, marker, self.udid)
        try:
            output, errors = child.communicate(timeout=2)
        except BaseException:
            child.kill()
            child.communicate(timeout=2)
            raise
        self.assertEqual(child.returncode, 0, errors)
        self.assertIn("set_timed_out", output)
        self.assertIn("process_can_exit", output)
        self.assertTrue(e.DirtySessionMarker(marker).pending)

    def test_standalone_invoke_timeout_also_allows_interpreter_exit(self):
        child = self.process("""
import asyncio,engine,threading
async def run():
 try: await asyncio.wait_for(engine._invoke(lambda:threading.Event().wait(60)),.02)
 except asyncio.TimeoutError: print('deadline',flush=True)
asyncio.run(run())
print('exited',flush=True)
""")
        try:
            output, errors = child.communicate(timeout=2)
        except BaseException:
            child.kill(); child.communicate(timeout=2)
            raise
        self.assertEqual(child.returncode, 0, errors)
        self.assertIn("exited", output)

    def test_daemon_executor_has_bounded_capacity_and_nonwaiting_shutdown(self):
        executor = DaemonDriverExecutor(max_workers=2)
        release = threading.Event()
        work = [executor.submit(release.wait, 2) for _ in range(2)]
        try:
            self.assertTrue(all(thread.daemon for thread in executor._work.values()))
            with self.assertRaisesRegex(RuntimeError, "too many"):
                executor.submit(lambda: None)
            before = time.monotonic()
            executor.shutdown(wait=False, cancel_futures=True)
            self.assertLess(time.monotonic()-before, .1)
            self.assertTrue(all(not future.done() for future in work))
            with self.assertRaisesRegex(RuntimeError, "closed"):
                executor.submit(lambda: None)
        finally:
            release.set()
            for future in work:
                future.result(timeout=1)

    def test_device_lock_conflicts_across_processes_and_recovers_after_kill(self):
        ready = self.root/"holder"
        program = """
import os,sys,time
from pathlib import Path
from runtime_support import device_lock
lease=device_lock(sys.argv[1]).acquire()
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(60)
"""
        holder = self.process(program, self.udid, ready)
        bridge = e.IOSDeviceBridge(udid=self.udid, auto_start_tunneld=False)
        self.addCleanup(bridge.close)
        try:
            self.wait_file(ready)
            with self.assertRaises(InstanceRunningError):
                bridge._claim_device(self.udid)
            # The losing instance cannot clear a device it does not own.
            cleared = []
            async def clear(): cleared.append(True)
            bridge._simulator = SimpleNamespace(clear=clear)
            self.assertFalse(asyncio.run(bridge._disconnect(clear=True)))
            self.assertEqual(cleared, [])
            holder.kill(); holder.communicate(timeout=2)
            bridge._claim_device(self.udid)
            self.assertIsNotNone(bridge._device_lease)
        finally:
            if holder.poll() is None: holder.kill()
            holder.communicate(timeout=2)

    def test_normalized_udid_is_exclusive_even_with_different_config_paths(self):
        first, second = e.IOSDeviceBridge(), e.IOSDeviceBridge()
        self.addCleanup(first.close); self.addCleanup(second.close)
        first._claim_device(self.udid)
        with self.assertRaises(InstanceRunningError):
            second._claim_device(self.udid.replace("-", "").upper())
        first.close()
        second._claim_device(self.udid)

    def test_closed_bridge_keeps_lease_while_an_async_worker_has_not_exited(self):
        first, second = e.IOSDeviceBridge(), e.IOSDeviceBridge()
        self.addCleanup(first.close); self.addCleanup(second.close)
        first._claim_device(self.udid)
        lease = first._device_lease
        worker = MagicMock()
        worker.is_alive.return_value = True
        first._thread = worker
        pending = Future()
        pending.set_running_or_notify_cancel()
        first._pending_sync_calls.add(pending)
        try:
            first.close(timeout=.01)
            self.assertIn(lease, e._quarantined_device_leases)
            with self.assertRaises(InstanceRunningError):
                second._claim_device(self.udid)
            first._worker_finished = True
            first._release_retired_device()
            with self.assertRaises(InstanceRunningError):
                second._claim_device(self.udid)
            pending.set_result(None)
            second._claim_device(self.udid)
            self.assertNotIn(lease, e._quarantined_device_leases)
        finally:
            if lease in e._quarantined_device_leases:
                e._quarantined_device_leases.remove(lease)
            lease.close()

    def test_marker_concurrent_claim_has_exactly_one_owner(self):
        path = self.root/"session.json"
        markers = [e.DirtySessionMarker(path), e.DirtySessionMarker(path)]
        barrier = threading.Barrier(2)
        def claim(marker):
            barrier.wait(timeout=1)
            try:
                marker.mark(self.udid)
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, markers))
        self.assertEqual(sum(outcomes), 1)
        winner = markers[outcomes.index(True)]
        self.assertEqual(json.loads(path.read_text())["session_id"], winner.record["session_id"])
        winner.cleared()
        self.assertFalse(path.exists())

    def test_changed_marker_is_rejected_before_physical_clear(self):
        marker = e.DirtySessionMarker(self.root/"session.json")
        marker.mark(self.udid)
        marker.path.write_text(json.dumps({**marker.record, "session_id": "other-owner"}))
        bridge = e.IOSDeviceBridge(udid=self.udid, dirty_marker=marker)
        bridge.connect = lambda: {}
        self.addCleanup(lambda: bridge.close(timeout=.02))
        clear = MagicMock()
        bridge._simulator = SimpleNamespace(clear=clear)
        self.assertFalse(asyncio.run(bridge._disconnect(clear=True)))
        clear.assert_not_called()
        self.assertEqual(json.loads(marker.path.read_text())["session_id"], "other-owner")

    def test_marker_transaction_contention_does_not_abort_bounded_shutdown(self):
        marker = e.DirtySessionMarker(self.root/"session.json")
        marker.mark(self.udid)
        original = marker.path.read_bytes()
        bridge = e.IOSDeviceBridge(udid=self.udid, dirty_marker=marker)
        bridge.connect = lambda: {}
        self.addCleanup(bridge.close)
        with ProcessFileLock(marker.path.with_suffix(".json.lock")), self.assertLogs(e.LOGGER, level="ERROR"):
            self.assertFalse(bridge.close(timeout=.02))
        self.assertTrue(bridge._close_ack.is_set())
        self.assertEqual(marker.path.read_bytes(), original)

    def test_conflicting_connection_fails_before_lockdown_and_reports_to_idle_ui(self):
        owner = e.IOSDeviceBridge()
        owner._claim_device(self.udid)
        self.addCleanup(owner.close)
        api = main.DesktopAPI(self.root/"config.json")
        self.addCleanup(lambda: api._shutdown(timeout=.1))
        with patch.object(api._bridge, "_select_device", new=AsyncMock(return_value=self.udid)), \
             patch.object(e.importlib, "import_module", side_effect=AssertionError("must not open lockdown")):
            api._bridge.connect()
            deadline = time.monotonic()+1
            while api._bridge._device_error_code is None and time.monotonic() < deadline:
                time.sleep(.01)
            state = api.get_state()["data"]
            self.assertEqual(state["device"]["error_code"], "instance_running")
            self.assertIn("请勿多开", state["device_error"])
            self.assertFalse(state["session_active"])
            api._shutdown(timeout=.1)

    def test_windowed_private_cli_preserves_inherited_error_pipe(self):
        child = self.process("""
import main,sys,types
def cli():
 print('PermissionDenied: offline driver probe',file=sys.stderr,flush=True)
 raise SystemExit(7)
sys.modules['pymobiledevice3.__main__']=types.SimpleNamespace(main=cli)
sys.stdout=sys.stderr=None
raise SystemExit(main.main(['--piggy-tunneld','--help']))
""")
        output, errors = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 7, output+errors)
        self.assertIn("PermissionDenied", errors)

    def test_fault_log_is_one_bounded_utf8_file_and_filters_normal_activity(self):
        handler = BoundedFaultLog(self.root/"last_session.log")
        def record(level, message, fault=False):
            entry = logging.LogRecord("engine", level, "probe", 1, message, (), None)
            entry.fault = fault
            handler.handle(entry)
        record(logging.INFO, "normal tick")
        record(logging.WARNING, "ordinary validation warning")
        self.assertFalse(handler.path.exists())
        record(logging.WARNING, "connection failed", fault=True)
        first = handler.path.read_bytes()
        record(logging.WARNING, "connection failed", fault=True)
        self.assertEqual(first, handler.path.read_bytes())
        for i in range(100):
            record(logging.ERROR, str(i)+": "+"定位故障"*1000)
            self.assertLessEqual(handler.path.stat().st_size, 256*1024)
        text = handler.path.read_text(encoding="utf-8")
        self.assertIn("99:", text)
        self.assertEqual(list(self.root.glob("*.log")), [handler.path])
        self.assertEqual(list(self.root.glob("*.log.*")), [handler.path.with_suffix(".log.lock")])
        handler.close()

    def test_fault_log_io_failure_never_blocks_cleanup(self):
        handler = BoundedFaultLog(self.root/"last_session.log")
        with patch.object(ProcessFileLock, "acquire", side_effect=PermissionError("read-only")):
            handler.handle(logging.LogRecord("engine", logging.ERROR, "probe", 1, "shutdown failed", (), None))
        handler.close()

    def test_uncaught_exception_hooks_persist_summaries_and_restore_previous_hooks(self):
        api = main.DesktopAPI(self.root/"config.json", sandbox=True)
        self.addCleanup(api._shutdown)
        system, worker = MagicMock(), MagicMock()
        with patch.object(sys, "excepthook", system), patch.object(threading, "excepthook", worker):
            hooks = main.FaultHooks()
            hooks.install()
            try:
                sys.excepthook(RuntimeError, RuntimeError("process-boom"), None)
                threading.excepthook(SimpleNamespace(exc_type=OSError, exc_value=OSError("worker-boom"), exc_traceback=None))
                text = api._fault_log.path.read_text()
                self.assertIn("process-boom", text)
                self.assertIn("worker-boom", text)
                system.assert_called_once()
                worker.assert_called_once()
            finally:
                hooks.close()
            self.assertIs(sys.excepthook, system)
            self.assertIs(threading.excepthook, worker)

    def test_shutdown_resource_exceptions_are_logged_and_other_cleanup_still_runs(self):
        api = main.DesktopAPI(self.root/"config.json", sandbox=True)
        runner_close = api._runner.close
        self.addCleanup(runner_close)
        with patch.object(api._runner, "close", side_effect=OSError("runner-close-boom")), \
             patch.object(api._protection, "close", side_effect=OSError("sleep-close-boom")) as close:
            self.assertFalse(api._shutdown(timeout=.1))
            close.assert_called_once()
        text = api._fault_log.path.read_text()
        self.assertIn("runner-close-boom", text)
        self.assertIn("sleep-close-boom", text)
        self.assertNotIn(api._fault_log, logging.getLogger().handlers)

    def runner(self):
        bridge = e.IOSDeviceBridge(sandbox=True)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(41))
        self.addCleanup(bridge.close); self.addCleanup(runner.close)
        runner.load_track([e._destination(ORIGIN, -i*360/16, 60) for i in range(16)])
        runner.lock_start(background=False); runner.start_cruising(background=False)
        return runner

    def test_route_point_budget_and_duplicate_input_bound(self):
        self.assertEqual(e.TrackGeometry.MAX_POINTS, 25000)
        with self.assertRaises(e.GeometryBudgetError):
            e.TrackGeometry([ORIGIN, e._destination(ORIGIN, 0, 5000)], closed=False)
        with self.assertRaises(e.GeometryBudgetError):
            e.TrackGeometry._clean((ORIGIN for _ in range(26000)), False, 25000)
        with self.assertRaises(e.GeometryBudgetError):
            e.TrackGeometry((ORIGIN for _ in range(26000)), closed=False, preprocess=False)

    def test_cooperative_geometry_deadline_interrupts_subdivision(self):
        clock = [0.0]
        def now():
            clock[0] += .001
            return clock[0]
        with patch.object(e.time, "monotonic", side_effect=now):
            with self.assertRaises(e.GeometryBudgetError):
                with e._geometry_window(.01):
                    e.TrackGeometry([ORIGIN, e._destination(ORIGIN, 0, 100)], closed=False)

    def test_next_lap_timeout_uses_safe_existing_geometry_without_rng_or_seam_damage(self):
        runner = self.runner()
        runner._connector = None
        runner._loop_s = runner._lap.length-1
        lap, rng = runner._lap, runner._geometry_rng.getstate()
        with patch.object(runner, "_make_lap", side_effect=e.GeometryBudgetError("timeout")):
            runner._prepare_next_lap(.95)
        self.assertIs(runner._prepared_lap, lap)
        self.assertEqual(runner._geometry_rng.getstate(), rng)
        runner._lap_start_time = runner._lap_start_distance = 0
        runner._next_lap(150, lap.length)
        self.assertIs(runner._lap, lap)
        self.assertEqual(runner.current_lap, 2)
        self.assertEqual(runner.last_error, "route_too_complex")

    def test_live_detour_timeout_rejects_branch_and_keeps_loop_motion(self):
        runner = self.runner()
        target = e._destination(ORIGIN, 120, 100)
        runner.submit_detour([{"lat": target[0], "lon": target[1], "is_target": True}])
        runner._connector = None
        runner._loop_s = runner._lap.cumulative[runner.exit_idx]
        runner.position = runner._lap.sample(runner._loop_s)
        runner._exit_remaining = 0
        with patch.object(runner, "_build_detour", side_effect=e.GeometryBudgetError("timeout")):
            state = runner.step()
        self.assertEqual(state["state"], "CRUISING_LOOP")
        self.assertIsNone(runner._pending_detour)
        self.assertEqual(state["last_error"], "route_too_complex")
        self.assertGreater(state["distance_m"], 0)


if __name__ == "__main__":
    unittest.main()
