"""Run the frozen app in Sandbox, with no phone, GUI, or persistent user config."""
import argparse
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def smoke(executable: Path) -> None:
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError("Missing executable: " + str(executable))
    # Imports the bundled CLI and remote command tree without creating a tunnel.
    # A Windows GUI executable has no console handles. Supply explicit pipes,
    # exactly as the production bridge does for its private tunnel subprocess.
    try:
        helper = subprocess.run([str(executable), "--piggy-tunneld", "--help"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", timeout=60, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise RuntimeError("Frozen tunnel CLI timed out: " + output[-4000:]) from error
    if helper.returncode != 0:
        raise RuntimeError("Frozen tunnel CLI import check failed: " + str(helper.returncode)
                           + "\n" + helper.stdout[-4000:])
    with tempfile.TemporaryDirectory(prefix="pace-frozen-smoke-") as directory:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = "http://127.0.0.1:" + str(port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        child = subprocess.Popen([str(executable), "--preview", "--sandbox", "--port", str(port),
                                  "--config", str(Path(directory) / "config.json")])
        try:
            deadline = time.monotonic() + 60
            html = None
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("Frozen app exited before serving its UI: " + str(child.returncode))
                try:
                    with opener.open(url, timeout=1) as response:
                        html = response.read().decode("utf-8")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            if html is None:
                raise RuntimeError("Frozen Sandbox did not start within 60 seconds")
            token = re.search(r'<meta\s+name="piggy-token"\s+content="([^"]+)"', html)
            if token is None or token.group(1) == "__PIGGY_SESSION_TOKEN__":
                raise RuntimeError("Frozen UI did not receive a session token")
            for asset in ("/vendor/leaflet.js", "/vendor/leaflet.css", "/vendor/LEAFLET-LICENSE.txt",
                          "/assets/chill-32.png", "/assets/chill-192.png", "/favicon.ico"):
                with opener.open(url + asset, timeout=3) as response:
                    if not response.read():
                        raise RuntimeError("Empty frozen resource: " + asset)
            request = urllib.request.Request(url + "/api/bootstrap", data=b'{"args":[]}',
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": "Bearer " + token.group(1)})
            with opener.open(request, timeout=5) as response:
                data = json.load(response)
            if not data.get("ok") or data["data"]["state"]["device"]["mode"] != "SANDBOX_MODE":
                raise RuntimeError("Frozen API is not running a healthy Sandbox")
            print("Frozen Sandbox: UI, offline assets, authenticated API, and tunnel CLI verified.")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    smoke(args.executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
