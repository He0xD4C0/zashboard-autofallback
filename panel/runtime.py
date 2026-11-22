"""Process and controller access shared by the panel and its background jobs."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
ARTIFACTS = DATA / "artifacts"
CACHE = DATA / "cache"
CONFIG = ARTIFACTS / "config.yaml"
PROBE_DIR = DATA / "probe"
PROBE_CONFIG = ARTIFACTS / "probe-config.yaml"
DIAGNOSTICS = DATA / "diagnostics"
RUNTIME = DATA / "runtime"
ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:9090"
PROBE_API = "http://127.0.0.1:9190"
PROBE_PROXY = "http://127.0.0.1:7899"
MANAGER_PORT = 9081


def atomic_copy(source, destination, mode=0o600):
    fd, path = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(path, destination)
    finally:
        Path(path).unlink(missing_ok=True)


def controller(path, api=API, timeout=3):
    request = urllib.request.Request(
        api + path, headers={"Authorization": "Bearer " + os.environ["MIHOMO_SECRET"]}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def kernel(binary, home, config, *, validate=False):
    command = [str(binary), "-d", str(home), "-f", str(config)]
    return [*command, "-t"] if validate else command


def wait_ready(process, api=API, *, expected_rules=(), timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Mihomo exited with status {process.returncode}")
        try:
            if controller("/version", api, 1).get("version"):
                if not expected_rules:
                    return
                body = controller("/providers/rules", api, 1)["providers"]
                if all((body.get(name) or {}).get("ruleCount", 0) > 0 for name in expected_rules):
                    return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Mihomo controller or rule sets did not become ready")


def terminate(process, *, group=False, timeout=5):
    if process is None:
        return
    # A job can exit while a subprocess in its group is still running.
    if group:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        if group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()
