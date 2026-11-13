"""Offline engineering audit. No phone or third-party account is contacted."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

import engine as e

ROOT = Path(__file__).resolve().parent
ORIGIN = (31.2304, 121.4737)
REPORT = {}

class Clock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t

class Recorder:
    def __init__(self, clock): self.clock, self.calls = clock, []
    async def set(self, lat, lon): self.calls.append((self.clock.now(), lat, lon))
    async def clear(self): pass


def track():
    return [e._destination(ORIGIN, -i*360/16, 60) for i in range(16)]

class AuditTests(unittest.TestCase):
    def make(self, width=8, seed=41):
        clock = Clock()
        bridge = e.IOSDeviceBridge(sandbox=True, clock=clock.now)
        bridge._simulator = recorder = Recorder(clock)
        runner = e.BiomechanicalRunner(bridge, rng=random.Random(seed), clock=clock.now)
        self.addCleanup(bridge.close); self.addCleanup(runner.close)
        runner.load_track(track(), target_km=20, track_width_m=width)
        runner.lock_start(background=False)
        runner.start_cruising(background=False)
        return clock, bridge, runner, recorder

    def test_01_2500_tick_walking_break_stress(self):
        clock,b,r,rec = self.make()
        rows=[]; triggers=[]; walks=[]; resume_times=[]; outer_offsets=[]; inner_offsets=[]; phase='running'; rest_start=None
        manual_done=False; accepted=0; rejected=0
        threads = {t.ident for t in threading.enumerate()}
        async def exercise():
            nonlocal phase,rest_start,manual_done,accepted,rejected
            await b._send(b._take_sample())  # Initial locked coordinate, outside cruise samples.
            for tick in range(2500):
                clock.t += .95
                if len(triggers)<2 and clock.t >= (400,1100)[len(triggers)]:
                    r.pause(); accepted+=1; triggers.append(clock.t)
                    for _ in range(5):
                        for method in (r.pause,r.resume):
                            try: method()
                            except RuntimeError: rejected+=1
                            else: self.fail('Repeated click was accepted')
                if len(triggers)==2 and not manual_done and r._pause_phase=='WALKING_REST' and r._phase_elapsed_s>=20:
                    r.resume();accepted+=1;manual_done=True
                    for _ in range(5):
                        try:r.resume()
                        except RuntimeError:rejected+=1
                        else:self.fail('Repeated resume was accepted')
                before=r.distance_m
                state=r.step(.95)
                await b._send(b._take_sample())
                speed=(r.distance_m-before)/.95
                self.assertGreaterEqual(speed,1.20-1e-6)
                self.assertLessEqual(speed,3.80+1e-6)
                if rows:
                    acceleration=abs(speed-rows[-1]['speed_mps'])/.95
                    self.assertLessEqual(acceleration,.8+1e-6)
                    if state['pause_phase']!='running' or rows[-1]['phase']!='running':
                        self.assertLess(acceleration,.6)
                if state['pause_phase']!=phase:
                    if state['pause_phase']=='WALKING_REST':rest_start=clock.t
                    if phase=='WALKING_REST':walks.append(clock.t-rest_start);resume_times.append(clock.t)
                    phase=state['pause_phase']
                if phase=='WALKING_REST' and r._phase_elapsed_s>5:
                    self.assertAlmostEqual(state['offset_m'],1.8,delta=.03)
                    outer_offsets.append(state['offset_m'])
                if phase=='running' and resume_times and 2<clock.t-resume_times[-1]<15:
                    self.assertLessEqual(abs(state['offset_m']),.85)
                    inner_offsets.append(abs(state['offset_m']))
                rows.append({'tick':tick+1,'time_s':clock.t,'speed_mps':speed,'offset_m':state['offset_m'],'phase':phase})
        asyncio.run(exercise())
        self.assertEqual(len(rows),2500)
        self.assertEqual(len(triggers),2);self.assertEqual(len(walks),2)
        self.assertTrue(manual_done)
        self.assertGreaterEqual(walks[0],40);self.assertLessEqual(walks[0],60)
        self.assertGreaterEqual(rejected,25)
        self.assertEqual(threads,{t.ident for t in threading.enumerate()})
        gaps=[b[0]-a[0] for a,b in zip(rec.calls,rec.calls[1:])]
        self.assertTrue(all(.9-1e-6<=gap<=1+1e-6 for gap in gaps))
        wire_speeds=[e.haversine_distance(a[1],a[2],c[1],c[2])/gap
                     for a,c,gap in zip(rec.calls,rec.calls[1:],gaps)]
        self.assertEqual(len(wire_speeds),2500)
        self.assertGreaterEqual(min(wire_speeds),1.2)
        self.assertLessEqual(max(wire_speeds),3.8)
        wire_acceleration=max(abs(c-a)/gap for a,c,gap in zip(wire_speeds,wire_speeds[1:],gaps[1:]))
        self.assertLessEqual(wire_acceleration,.8)
        self.assertFalse(b.snapshot()['speed_fuse_tripped'])
        REPORT['stress']={'ticks':2500,'trigger_times_s':triggers,'walk_durations_s':walks,'accepted_manual_commands':accepted,'rejected_spam_commands':rejected,'outer_lane_min_m':min(outer_offsets),'outer_lane_max_m':max(outer_offsets),'inner_return_max_abs_m':max(inner_offsets),'min_speed_mps':min(v['speed_mps'] for v in rows),'max_speed_mps':max(v['speed_mps'] for v in rows),'max_acceleration_mps2':max(abs(c['speed_mps']-a['speed_mps'])/.95 for a,c in zip(rows,rows[1:])),'wire_calls_including_initial_lock':len(rec.calls),'min_wire_interval_s':min(gaps),'max_wire_interval_s':max(gaps)}
        REPORT['stress'].update(min_wire_speed_mps=min(wire_speeds),max_wire_speed_mps=max(wire_speeds),max_wire_acceleration_mps2=wire_acceleration)
        for row,wire_speed in zip(rows,wire_speeds):row['wire_speed_mps']=wire_speed
        (ROOT/'artifacts/chill-audit-motion.json').write_text(json.dumps(rows,indent=2)+'\n')

    def test_02_bridge_fuse_rejects_teleport_and_stays_latched(self):
        clock,b,r,rec=self.make()
        async def exercise():
            await b._send((1,*ORIGIN));clock.t+=.95
            far=e._destination(ORIGIN,0,100)
            with self.assertRaises(e.SpeedFuseError):await b._send((2,*far))
            clock.t+=30
            with self.assertRaises(e.SpeedFuseError):await b._send((3,*ORIGIN))
        asyncio.run(exercise())
        self.assertEqual(len(rec.calls),1)
        self.assertTrue(b.snapshot()['speed_fuse_tripped'])

    def test_03_rate_limit_serializes_concurrent_sends_and_heartbeat(self):
        clock,b,r,rec=self.make()
        sleep=asyncio.sleep
        async def advance(seconds):
            clock.t+=seconds
            await sleep(0)
        async def exercise():
            with patch.object(e.asyncio,'sleep',side_effect=advance):
                await asyncio.gather(*(b._send((n,*ORIGIN)) for n in range(5)))
            b._latest=(5,*ORIGIN);clock.t+=.8
            self.assertIsNotNone(b._take_sample())
            await b._send(b._take_sample())
        asyncio.run(exercise())
        self.assertEqual(len(rec.calls),6)
        for a,c in zip(rec.calls,rec.calls[1:]):
            self.assertAlmostEqual(c[0]-a[0], .8)

    def test_04_outage_is_reported_and_does_not_allow_catch_up_jump(self):
        clock,b,r,rec=self.make()
        async def exercise():
            await b._send((1,*ORIGIN));clock.t+=30
            with self.assertRaises(e.SpeedFuseError):await b._send((2,*e._destination(ORIGIN,0,20)))
        asyncio.run(exercise())
        self.assertEqual(b.snapshot()['send_late_count'],1)
        self.assertEqual(b.snapshot()['max_send_gap_s'],30)
        self.assertEqual(len(rec.calls),1)

    def test_05_narrow_corridor_never_forces_an_unsafe_outer_lane(self):
        clock,b,r,_=self.make(width=1)
        for _ in range(20):clock.t+=.95;r.step()
        r.pause()
        for _ in range(90):
            clock.t+=.95;r.step()
            self.assertTrue(r._corridor.within(e._point(r.position),.5))
            if r._pause_phase=='WALKING_REST':self.assertLessEqual(r.offset_m,.3)

    def test_06_gateway_has_one_injection_site_and_no_control_timers(self):
        tree=ast.parse((ROOT/'engine.py').read_text())
        sites=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.AsyncFunctionDef) or isinstance(node,ast.FunctionDef):
                if node.name in ('pause','resume'):
                    for call in ast.walk(node):
                        if isinstance(call,ast.Call):
                            name=ast.unparse(call.func)
                            self.assertFalse(any(v in name for v in ('Thread','Timer','create_task')))
                if node.name=='_send':sites.append(ast.get_source_segment((ROOT/'engine.py').read_text(),node))
        self.assertEqual((ROOT/'engine.py').read_text().count('_invoke(self._simulator.set,'),1)
        self.assertIn('_invoke(self._simulator.set,',sites[0])

    def test_07_sender_keeps_adjacent_points_when_updates_arrive_in_a_burst(self):
        clock=Clock();bridge=e.IOSDeviceBridge(clock=clock.now,auto_start_tunneld=False)
        self.addCleanup(bridge.close);rec=Recorder(clock);bridge._simulator=rec
        bridge.mode=e.BridgeMode.CONNECTED
        async def exercise():
            await bridge._send((0,*ORIGIN))
            with patch.object(bridge,'connect',return_value={}):
                for n in range(1,6):bridge.set_location(*e._destination(ORIGIN,0,n))
            for _ in range(5):
                clock.t+=.95
                await bridge._send(bridge._take_sample())
        asyncio.run(exercise())
        self.assertEqual(len(rec.calls),6)
        self.assertFalse(bridge.snapshot()['speed_fuse_tripped'])
        for a,b in zip(rec.calls,rec.calls[1:]):self.assertAlmostEqual(e.haversine_distance(a[1],a[2],b[1],b[2]),1,places=4)

    def test_09_goal_keeps_walking_until_explicit_settlement(self):
        clock,b,r,rec=self.make()
        r.target_m=100
        prior=None;goal_tick=None;post=[]
        async def exercise():
            nonlocal prior,goal_tick
            await b._send(b._take_sample())
            for tick in range(400):
                clock.t+=.95
                state=r.step(.95)
                await b._send(b._take_sample())
                if prior is not None:self.assertLess(abs(r.speed_mps-prior)/.95,.6)
                prior=r.speed_mps
                self.assertGreaterEqual(r.speed_mps,1.2-1e-6)
                if state['target_reached']:
                    if goal_tick is None:goal_tick=tick
                    post.append(r.speed_mps)
                    self.assertEqual(state['state'],'CRUISING_LOOP')
            self.assertGreater(len(post),250)
            self.assertTrue(state['target_walking'])
            self.assertEqual(state['pause_phase'],'WALKING_REST')
            self.assertAlmostEqual(state['offset_m'],1.8,delta=.03)
            with self.assertRaises(RuntimeError):r.resume()
            with self.assertRaises(RuntimeError):r.pause()
        asyncio.run(exercise())
        REPORT['goal_walk']={'ticks_after_target':len(post),'min_speed_mps':min(post),'distance_at_manual_end_m':r.distance_m,'auto_resume':False,'wire_calls_including_initial_lock':len(rec.calls)}
        r.hold_position()
        held = r.snapshot()
        anchor = e._point(held["position"])
        hold_started = len(rec.calls)
        async def hold():
            for _ in range(8):
                clock.t += 1.0
                r.step(.95)
                await b._send(b._take_sample())
        asyncio.run(hold())
        for prior, current in zip(rec.calls[hold_started-1:], rec.calls[hold_started:]):
            self.assertNotEqual(prior[1:], current[1:])
            self.assertAlmostEqual(current[0]-prior[0], 1.0)
            self.assertGreaterEqual(e.haversine_distance(*anchor, *current[1:]), .3-1e-6)
            self.assertLessEqual(e.haversine_distance(*anchor, *current[1:]), .8+1e-6)
        self.assertEqual(r.snapshot()["position"], held["position"])
        self.assertEqual(r.snapshot()["distance_m"], held["distance_m"])
        self.assertEqual(r.snapshot()["elapsed_s"], held["elapsed_s"])
        self.assertEqual(r.state, e.RunnerState.SAFE_STOPPED)
        r.close()
        self.assertEqual(r.state,e.RunnerState.SAFE_STOPPED)
        self.assertEqual(r.speed_mps,0)

    def test_08_frontend_languages_javascript_and_control_states(self):
        node=shutil.which(os.environ.get('NODE_BINARY') or 'node')
        if node is None:
            self.skipTest('Node.js is unavailable; install node or set NODE_BINARY to its executable')
        result=subprocess.run([node,'test_ui.js'],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        REPORT['frontend']=result.stdout.strip().splitlines()

    def test_10_fuse_latched_during_rate_wait_prevents_the_pending_send(self):
        clock,b,_,rec=self.make()
        async def latch_during_wait(seconds):
            clock.t+=seconds
            b._fuse_tripped=True
        async def exercise():
            await b._send((1,*ORIGIN))
            with patch.object(e.asyncio,'sleep',side_effect=latch_during_wait):
                with self.assertRaises(e.SpeedFuseError):await b._send((2,*ORIGIN))
        asyncio.run(exercise())
        self.assertEqual(len(rec.calls),1)

    def test_11_five_concurrent_control_requests_accept_exactly_one(self):
        clock,_,r,_=self.make()
        for _ in range(20):clock.t+=.95;r.step()
        barrier=threading.Barrier(5)
        def click(_):
            barrier.wait(timeout=5)
            try:r.pause()
            except e.ControlCooldownError:return 'cooldown'
            return 'accepted'
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(click,range(5)))
        self.assertEqual(results.count('accepted'),1)
        self.assertEqual(results.count('cooldown'),4)
        self.assertEqual(r.snapshot()['pause_phase'],'slowing')

    def test_12_backlog_overflow_latches_the_same_gateway(self):
        clock=Clock();b=e.IOSDeviceBridge(clock=clock.now,auto_start_tunneld=False)
        self.addCleanup(b.close);b._simulator=rec=Recorder(clock)
        with patch.object(b,'connect',return_value={}):
            for _ in range(129):b.set_location(*ORIGIN)
        self.assertTrue(b.snapshot()['speed_fuse_tripped'])
        self.assertEqual(b.snapshot()['pending_samples'],128)
        self.assertEqual(b.snapshot()['dropped_device_samples'],1)
        async def exercise():
            with self.assertRaises(e.SpeedFuseError):await b._send(b._take_sample())
        asyncio.run(exercise())
        self.assertEqual(rec.calls,[])

    def test_13_target_walk_takes_over_each_temporary_break_phase(self):
        for requested_phase in ('slowing','WALKING_REST','resuming'):
            with self.subTest(phase=requested_phase):
                clock,b,r,rec=self.make()
                for _ in range(20):clock.t+=.95;r.step()
                r.pause()
                while r.snapshot()['pause_phase']!=requested_phase:
                    clock.t+=.95;r.step()
                    if requested_phase=='resuming' and r.snapshot()['pause_phase']=='WALKING_REST':r.resume()
                r.target_m=r.distance_m+6
                previous=r.speed_mps
                for _ in range(100):
                    clock.t+=.95;state=r.step()
                    self.assertGreaterEqual(r.speed_mps,1.2-1e-6)
                    self.assertLess(abs(r.speed_mps-previous)/.95,.6)
                    previous=r.speed_mps
                self.assertTrue(state['target_walking'])
                self.assertTrue(state['target_reached'])
                self.assertEqual(state['pause_phase'],'WALKING_REST')
                self.assertAlmostEqual(state['offset_m'],1.8,delta=.03)

    def test_14_walking_detour_delivers_repeated_checkpoints_with_a_buffer(self):
        clock,b,r,rec=self.make()
        interval=e.MOTION_INTERVAL_S
        target=e._destination(ORIGIN,120,100)
        speeds=[];paused=False;crossings=[];arrival_ticks=set()
        async def exercise():
            nonlocal paused
            await b._send(b._take_sample())
            for tick in range(600):
                clock.t+=interval
                if tick==20:r.submit_detour([{'lat':target[0],'lon':target[1],'is_target':True,'id':n} for n in (1,2)])
                if r.state==e.RunnerState.CRUISING_DETOUR and not paused:r.pause();paused=True
                before=r.distance_m;state=r.step()
                await b._send(b._take_sample())
                speed=(r.distance_m-before)/interval
                self.assertGreaterEqual(speed,0)
                self.assertLessEqual(speed,3.8+1e-6)
                reached=False
                while not r.events.empty():
                    event=r.events.get_nowait()
                    if event['type']=='target_reached':
                        crossings.append((event['lat'],event['lon']))
                        reached=True
                        arrival_ticks.add(tick)
                        self.assertLessEqual(e.haversine_distance(*target,*e._point(state['position'])),.081)
                        self.assertEqual((event['sample_lat'],event['sample_lon']),rec.calls[-1][1:])
                # The dedicated arrival sample truncates travel once. Recovery
                # and every other motion command still obey the slew limit.
                if speeds and not reached:self.assertLess(abs(speed-speeds[-1])/interval,.6)
                if e.haversine_distance(*target,*e._point(state['position']))>10:
                    self.assertGreaterEqual(speed,1.2-1e-6)
                speeds.append(speed)
        asyncio.run(exercise())
        self.assertTrue(paused)
        self.assertEqual(r.targets_visited,[1,2])
        self.assertEqual(crossings,[target,target])
        self.assertEqual(r.state,e.RunnerState.CRUISING_LOOP)
        wire=[e.haversine_distance(a[1],a[2],c[1],c[2])/(c[0]-a[0]) for a,c in zip(rec.calls,rec.calls[1:])]
        self.assertGreaterEqual(min(wire),0)
        self.assertLessEqual(max(wire),3.8)
        self.assertEqual(len(arrival_ticks),1)  # Co-located IDs share one sample.
        maximum_acceleration=max(abs(c-a)/interval for i,(a,c) in enumerate(zip(wire,wire[1:]),1)
                                 if i not in arrival_ticks)
        self.assertLess(maximum_acceleration,.6)
        REPORT['detour']={'ticks':600,'checkpoints_crossed':len(crossings),'min_wire_speed_mps':min(wire),'max_wire_acceleration_mps2':maximum_acceleration}

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests))
    REPORT['result']={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'passed':result.wasSuccessful()}
    print('\nENGINEERING AUDIT REPORT')
    print('Scope: 2500 cruise samples, 600 detour samples and goal walking; initial lock and explicit settlement may be stationary.')
    print(f'audit_passed: {result.wasSuccessful()}')
    stats=REPORT.get('stress',{})
    for key,value in stats.items():
        if isinstance(value,float):value=f'{value:.6f}'
        elif isinstance(value,list):value=', '.join(f'{v:.2f}' for v in value)
        print(f'{key}: {value}')
    for key,value in REPORT.get('goal_walk',{}).items():print(f'goal_walk_{key}: {value}')
    for key,value in REPORT.get('detour',{}).items():print(f'detour_{key}: {value}')
    for line in REPORT.get('frontend',[]):print(line)
    print('Transport timing is deterministic mock timing, not a guarantee during USB/network/OS failures.')
    print('No iPhone, third-party account, anti-cheat policy, or account-ban outcome was tested.')
    (ROOT/'artifacts/chill-audit-report.json').write_text(json.dumps(REPORT,ensure_ascii=False,indent=2)+'\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
