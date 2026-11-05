"""Offline physical-width and CRS regression for the calibrated public fixture."""
import json
import math
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.preview_calibrated import load_fixture
import engine
import main


def verify():
    spec, gcj = load_fixture()
    wgs, geometry = main.DesktopAPI._geometry(gcj)
    lon, lat = engine.gcj02_to_wgs84(spec['center'][1], spec['center'][0])
    frame = engine._Projection((lat, lon))
    angle = math.radians(spec['bearing_deg'])
    def deviation(point):
        x, y = frame.xy(point)
        across = x*math.cos(angle)-y*math.sin(angle)
        along = x*math.sin(angle)+y*math.cos(angle)
        return math.hypot(across, max(0, abs(along)-spec['straight_m']/2))-spec['radius_m']
    round_trip = []
    for source, converted in zip(gcj, wgs):
        map_lon, map_lat = engine.wgs84_to_gcj02(converted[1], converted[0])
        round_trip.append(engine.haversine_distance(source['lat'], source['lon'], map_lat, map_lon))
    runs = []
    for seed, width in ((1, 8), (41, 8), (2026, 8), (41, 1)):
        bridge = engine.IOSDeviceBridge(sandbox=True)
        runner = engine.BiomechanicalRunner(bridge, rng=random.Random(seed))
        try:
            runner.load_track(wgs, target_km=10, track_width_m=width)
            runner.lock_start(background=False)
            runner.start_cruising(background=False)
            deviations = [deviation(runner.start_point)]
            maximum_tick = 0
            while runner.laps < 8:
                previous = engine._point(runner.position)
                started = time.perf_counter()
                runner.step()
                maximum_tick = max(maximum_tick, time.perf_counter()-started)
                point = engine._point(runner.position)
                for fraction in (0.25, 0.5, 0.75, 1.0):
                    sample = engine._interpolate(previous, point, fraction)
                    value = deviation(sample)
                    if abs(value) > width/2-0.05:
                        raise AssertionError(f'Emitted/rendered sample left {width} m stadium strip: {value} m')
                    deviations.append(value)
            runs.append({'seed': seed, 'width_m': width, 'completed_laps': runner.laps,
                         'samples_and_chord_probes': len(deviations),
                         'min_signed_deviation_m': min(deviations), 'max_signed_deviation_m': max(deviations),
                         'max_tick_wall_s': maximum_tick, 'lap_splits_s': runner.lap_splits})
        finally:
            runner.close()
            bridge.close()
    result = {'fixture': 'examples/calibrated-track.json', 'map_crs': 'GCJ-02', 'engine_crs': 'WGS-84',
              'geometry_length_m': geometry.length_m, 'max_coordinate_round_trip_error_m': max(round_trip),
              'runs': runs, 'limits': ['Sandbox only; no device location changed',
              'Satellite alignment is a manual visual fit to this public example, not survey accuracy',
              'Other saved tracks must be placed on their own running surface; no universal map translation is applied',
              'Confirmed detours may leave the strip along user-planned roads; infield shortcuts are rejected']}
    output = ROOT/'artifacts/alignment-verification.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    verify()
