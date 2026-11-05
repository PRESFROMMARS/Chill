"""Reproducible satellite-aligned Sandbox fixture; never opens device drivers.

Run: python scripts/preview_calibrated.py --port 8770
All configuration is isolated in a temporary directory, never ~/.pace_config.json.
"""
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine
import main


def load_fixture(path=ROOT/"examples/calibrated-track.json"):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if spec["coordinate_system"] != "GCJ-02":
        raise ValueError("fixture map coordinates must explicitly be GCJ-02")
    lat, lon = spec["center"]
    wgs_lon, wgs_lat = engine.gcj02_to_wgs84(lon, lat)
    anchors = engine.TrackGeometry.stadium_anchors((wgs_lat, wgs_lon), spec["straight_m"],
                                                  spec["radius_m"], spec["bearing_deg"])
    gcj = []
    for lat, lon in anchors:
        map_lon, map_lat = engine.wgs84_to_gcj02(lon, lat)
        gcj.append({"lat": map_lat, "lon": map_lon})
    return spec, gcj


def main_preview():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--laps", type=int, default=8)
    args = parser.parse_args()
    if not 0 <= args.laps <= 10:
        parser.error("--laps must be between 0 and 10")
    spec, gcj = load_fixture()
    with tempfile.TemporaryDirectory(prefix="piggy-calibrated-") as folder:
        api = main.DesktopAPI(Path(folder)/"config.json", sandbox=True)
        server = main.LocalServer(api, args.port)
        try:
            api._config.update({"track_points": gcj, "track_width_m": spec["track_width_m"],
                                "map_center": spec["center"], "map_zoom": 18, "target_km": 4.0})
            if args.laps:
                api._runner.close()
                api._runner = engine.BiomechanicalRunner(api._bridge, rng=random.Random(41))
                # Use the production boundary, including its single GCJ inverse.
                wgs, _ = api._geometry(gcj)
                api._runner.load_track(wgs, target_km=4.0, track_width_m=spec["track_width_m"])
                api._runner.lock_start(background=False)
                api._runner.start_cruising(background=False)
                while api._runner.laps < args.laps:
                    api._runner.step()
                api._session_active = True
                api._runner.start()
            server.start()
            api._start_services()
            print("Calibrated Sandbox preview: "+server.url, flush=True)
            threading.Event().wait()
        except KeyboardInterrupt:
            print("Preview stopped", flush=True)
        finally:
            api._shutdown()
            server.close()


if __name__ == "__main__":
    main_preview()
