"""Own the managed core process and serialize mutations of its persistent state."""

import fcntl
import hmac
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from panel import releases, rulesets
from panel.runtime import (
    ARTIFACTS, CACHE, CONFIG, DIAGNOSTICS, MANAGER_PORT, PROBE_CONFIG, PROBE_DIR,
    RUNTIME, atomic_copy, kernel, terminate, wait_ready,
)
from tools.rules import atomic_text, read_config, required_rules

LOG = logging.getLogger("panel")
PUBLIC = {"start", "stop", "restart", "upgrade", "rollback", "rules"}
TASKS = {"refresh", "score", "purity", "validate", "watchdog"}


@dataclass
class Job:
    action: str
    version: str = ""
    skip_score: bool = False
    url: str = ""
    status: str = "running"
    message: str = ""


class CoreManager:
    def __init__(self):
        self.state_file = RUNTIME / "state.json"
        self.state = {"desired": "running", "current": None, "previous": None}
        if self.state_file.is_file():
            self.state.update(json.loads(self.state_file.read_text()))
        self.cache_backup = RUNTIME / "switch-cache.db"
        self.switch_marker = RUNTIME / "switch.json"
        self.recover_switch()
        self.secret = os.environ["MIHOMO_SECRET"]
        if not self.secret:
            raise ValueError("MIHOMO_SECRET must be set")
        self.initial = os.environ.get("MIHOMO_INITIAL_VERSION", "v1.19.33")
        self.core = None
        self.phase = "stopped" if self.state["desired"] == "stopped" else "starting"
        self.message = ""
        self.lock = threading.RLock()
        self.shutting_down = threading.Event()
        self.jobs = {}
        self.active = None
        self.pending_lifecycle = None
        self.worker = None
        self.child = None
        self.watchdog = None
        self.retry = 0
        self.next_start = 0
        self.next_refresh = float("inf")
        self.next_watchdog = float("inf")
        self.refresh_interval = max(1, int(os.environ.get("REFRESH_INTERVAL", "21600")))
        self.watchdog_interval = max(1, int(os.environ.get("WATCHDOG_INTERVAL", "300")))

    def store(self, **changes):
        with self.lock:
            state = {**self.state, **changes}
            atomic_text(self.state_file, json.dumps(state))
            self.state = state

    def recover_switch(self):
        if self.switch_marker.is_file():
            switch = json.loads(self.switch_marker.read_text())
            if self.state["current"] != switch["target"]:
                if switch["hasCache"]:
                    atomic_copy(self.cache_backup, CACHE / "cache.db")
                else:
                    (CACHE / "cache.db").unlink(missing_ok=True)
            self.switch_marker.unlink()
        self.cache_backup.unlink(missing_ok=True)

    def backup_cache(self, target):
        cache = CACHE / "cache.db"
        exists = cache.is_file()
        if exists:
            atomic_copy(cache, self.cache_backup)
        atomic_text(self.switch_marker, json.dumps({"target": target, "hasCache": exists}))

    def snapshot(self, identifier=None):
        with self.lock:
            result = {**self.state, "status": self.phase, "message": self.message, "busy": self.active is not None}
            if identifier is not None:
                job = self.jobs.get(identifier)
                if job is None:
                    raise KeyError(identifier)
                result["job"] = {"id": identifier, "action": job.action, "status": job.status,
                                 "message": job.message}
            return result

    def submit(self, action, version="", skip_score=False, url=""):
        if action == "upgrade" and (not isinstance(version, str) or not releases.TAG.fullmatch(version)):
            raise ValueError("enter an official release tag such as v1.19.33")
        if action == "rules":
            rulesets.https_source(url)
        with self.lock:
            if self.shutting_down.is_set():
                raise RuntimeError("Panel is shutting down")
            if self.active is not None and not (
                action in PUBLIC and self.jobs[self.active].action in TASKS and self.pending_lifecycle is None
            ):
                raise RuntimeError("another Panel operation is running")
            if action in ((TASKS - {"validate"}) | {"rules"}) and self.phase != "running":
                raise RuntimeError("Mihomo must be running for this task")
            identifier = secrets.token_hex(8)
            self.jobs[identifier] = Job(action, version, skip_score, url)
            if len(self.jobs) > 64:
                self.jobs.pop(next(iter(self.jobs)))
            if self.active is not None:
                self.pending_lifecycle = identifier
                if self.child is not None:
                    self.interrupt_job()
            else:
                self.launch(identifier)
            return identifier

    def interrupt_job(self):
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def launch(self, identifier):
        self.active = identifier
        self.worker = threading.Thread(target=self.run_job, args=(identifier,), daemon=True)
        self.worker.start()

    def run_job(self, identifier):
        job = self.jobs[identifier]
        try:
            if job.action in TASKS or job.action == "rules":
                self.execute_task(job)
            else:
                self.lifecycle(job)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
            LOG.warning("%s failed: %s", job.action, message)
            with self.lock:
                job.status, job.message = "failed", message
                if job.action in PUBLIC:
                    self.message = message
                    if self.core is None and self.state["desired"] == "running":
                        self.phase = "failed"
                        self.retry = min(self.retry + 1, 6)
                        self.next_start = time.monotonic() + min(60, 2**self.retry)
        else:
            with self.lock:
                job.status = "ok"
                if job.action in PUBLIC:
                    self.message = ""
        finally:
            with self.lock:
                if job.action == "refresh":
                    self.next_refresh = time.monotonic() + self.refresh_interval
                if job.action == "watchdog":
                    self.next_watchdog = time.monotonic() + self.watchdog_interval
                self.active = None
                if self.pending_lifecycle is not None and not self.shutting_down.is_set():
                    identifier, self.pending_lifecycle = self.pending_lifecycle, None
                    self.launch(identifier)

    def spawn(self, command, **kwargs):
        with self.lock:
            if self.shutting_down.is_set():
                raise RuntimeError("Panel is shutting down")
            return subprocess.Popen(command, **kwargs)

    def task_env(self, binary):
        return {
            **os.environ, "MIHOMO_BINARY": str(binary), "DIAGNOSTICS_DIR": str(DIAGNOSTICS),
            "MIHOMO_RULESET": str(rulesets.ACTIVE),
        }

    def wait_task(self, name, binary, *flags, url=""):
        with self.lock:
            if self.pending_lifecycle is not None:
                raise RuntimeError("task interrupted by a lifecycle operation")
            env = self.task_env(binary)
            if name == "rules":
                env["PANEL_RULESET_SOURCE"] = url
            self.child = self.spawn([sys.executable, "-m", "panel.jobs", name, *flags],
                                    env=env, start_new_session=True)
            child = self.child
        try:
            while True:
                try:
                    return child.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    if self.shutting_down.is_set() or self.pending_lifecycle is not None:
                        terminate(child, group=True, timeout=3)
                        raise RuntimeError("task interrupted by a lifecycle operation")
        finally:
            if child.returncode:
                terminate(child, group=True)
            with self.lock:
                self.child = None

    def execute_task(self, job):
        version = self.state["current"]
        if not version:
            raise RuntimeError("Mihomo has no installed version")
        flags = []
        if job.action == "refresh" and job.skip_score:
            flags.append("--skip-score")
        if job.action == "validate" and self.phase == "running":
            flags.append("--live")
        if self.wait_task(job.action, releases.executable(version), *flags, url=job.url):
            raise RuntimeError(f"{job.action} failed; see Panel logs")

    def stop_children(self):
        with self.lock:
            core, watchdog = self.core, self.watchdog
            self.core = self.watchdog = None
        terminate(watchdog, group=True)
        terminate(core)

    def start_core(self, binary, version):
        expected = required_rules(read_config(CONFIG))
        core = self.spawn(kernel(binary, CACHE, CONFIG))
        with self.lock:
            self.core = core
        try:
            wait_ready(core, expected_rules=expected)
            if self.shutting_down.is_set():
                raise RuntimeError("Panel is shutting down")
        except Exception:
            terminate(core)
            with self.lock:
                self.core = None
            raise
        with self.lock:
            self.phase = "running"
            self.retry = 0
            self.next_refresh = time.monotonic() + min(30, self.refresh_interval)
            self.next_watchdog = time.monotonic() + min(30, self.watchdog_interval)
        LOG.info("Mihomo %s is ready", version)

    def start(self):
        self.store(desired="running")
        with self.lock:
            if self.core is not None and self.core.poll() is None:
                return
            self.phase = "starting"
        tag = self.state["current"] or self.initial
        binary = releases.install(tag)
        if self.wait_task("bootstrap", binary):
            raise RuntimeError("could not prepare Mihomo configuration; see Panel logs")
        self.start_core(binary, tag)
        if self.state["current"] is None:
            self.store(current=tag)

    def switch(self, tag):
        current = self.state["current"]
        if current == tag:
            return
        binary = releases.install(tag)
        if current is None:
            if self.wait_task("bootstrap", binary):
                raise RuntimeError("could not prepare Mihomo configuration; see Panel logs")
        elif self.wait_task("preflight", binary):
            raise RuntimeError(f"{tag} rejected the installed configuration; see Panel logs")
        if self.state["desired"] == "stopped":
            self.store(current=tag, previous=current)
            return

        with self.lock:
            self.phase = "starting"
        self.stop_children()
        try:
            self.backup_cache(tag)
            self.start_core(binary, tag)
            self.store(current=tag, previous=current)
        except Exception as exc:
            self.stop_children()
            self.recover_switch()
            if current and not self.shutting_down.is_set():
                self.start_core(releases.executable(current), current)
                raise RuntimeError(f"{tag} failed readiness; restored {current}") from exc
            raise
        self.recover_switch()

    def lifecycle(self, job):
        if job.action == "stop":
            self.store(desired="stopped")
            with self.lock:
                self.phase = "stopping"
            self.stop_children()
            with self.lock:
                self.phase = "stopped"
        elif job.action == "start":
            self.start()
        elif job.action == "restart":
            with self.lock:
                self.phase = "starting"
            self.stop_children()
            self.start()
        elif job.action == "upgrade":
            self.switch(job.version)
        elif job.action == "rollback":
            previous = self.state["previous"]
            if not previous:
                raise RuntimeError("there is no previous Mihomo version")
            self.switch(previous)

    def tick(self):
        with self.lock:
            now = time.monotonic()
            if self.watchdog is not None and self.watchdog.poll() is not None:
                self.watchdog = None
            if self.active and self.jobs[self.active].action in PUBLIC:
                return
            if self.core and self.core.poll() is not None:
                self.core = None
                self.phase = "failed"
                self.message = "Mihomo exited unexpectedly"
                LOG.warning(self.message)
                if self.child is not None:
                    self.interrupt_job()
                if self.watchdog is not None:
                    try:
                        os.killpg(self.watchdog.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                self.next_start = now + 2
            if self.phase != "running":
                if self.active is None and self.state["desired"] == "running" and now >= self.next_start:
                    self.submit("start")
                return
            if self.watchdog is None and now >= self.next_watchdog and (
                self.active is None or self.jobs[self.active].action != "watchdog"
            ):
                binary = releases.executable(self.state["current"])
                self.watchdog = self.spawn([sys.executable, "-m", "panel.jobs", "watchdog"],
                                           env=self.task_env(binary), start_new_session=True)
                self.next_watchdog = now + self.watchdog_interval
            if self.active is None and now >= self.next_refresh:
                self.submit("refresh")

    def close(self):
        self.shutting_down.set()
        with self.lock:
            child = self.child
        terminate(child, group=True, timeout=3)
        self.stop_children()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def send_json(self, status, content):
        self.send_bytes(status, json.dumps(content).encode())

    def send_bytes(self, status, content):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def authenticated(self):
        header = self.headers.get("Authorization", "").encode()
        expected = f"Bearer {self.server.core.secret}".encode()
        if hmac.compare_digest(header, expected):
            return True
        self.send_json(401, {"error": "invalid Panel credential"})
        return False

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        if url.path not in {"/autofallback/core", "/autofallback/state.json", "/autofallback/rules"}:
            self.send_json(404, {"error": "not found"})
            return
        if not self.authenticated():
            return
        if url.path == "/autofallback/core":
            identifier = parse_qs(url.query).get("job", [None])[0]
            try:
                state = self.server.core.snapshot(identifier)
            except KeyError:
                self.send_json(404, {"error": "job not found"})
            else:
                self.send_json(200, state)
        elif url.path == "/autofallback/rules":
            try:
                source = read_config(rulesets.ACTIVE).get("source")
            except FileNotFoundError:
                source = None
            self.send_json(200, {"source": source})
        else:
            try:
                snapshot = (DIAGNOSTICS / "state.json").read_bytes()
            except FileNotFoundError:
                self.send_json(404, {"error": "diagnostics not available yet"})
            else:
                self.send_bytes(200, snapshot)

    def do_POST(self):
        path = urlsplit(self.path).path
        actions = {"/autofallback/core": PUBLIC, "/internal/tasks": TASKS}.get(path)
        if actions is None:
            self.send_json(404, {"error": "not found"})
            return
        if not self.authenticated():
            return
        origin = self.headers.get("Origin")
        if origin:
            source = urlsplit(origin)
            if source.scheme not in {"http", "https"} or source.netloc != self.headers.get("Host"):
                self.send_json(403, {"error": "request must come from this Panel origin"})
                return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 1 <= size <= 2048 or self.headers.get_content_type() != "application/json":
                raise ValueError("expected a small JSON request")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict) or not isinstance(body.get("action"), str) or body["action"] not in actions:
                raise ValueError("unknown Panel action")
            identifier = self.server.core.submit(
                body["action"], body.get("version", ""), body.get("skipScore") is True, body.get("url", "")
            )
        except (ValueError, UnicodeError) as exc:
            self.send_json(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "invalid JSON"})
        except RuntimeError as exc:
            self.send_json(409, {"error": str(exc)})
        else:
            self.send_json(202, {"job": identifier})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RUNTIME / ".manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager = CoreManager()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: manager.shutting_down.set())
        with ThreadingHTTPServer(("127.0.0.1", MANAGER_PORT), Handler) as server:
            server.core = manager
            threading.Thread(target=server.serve_forever, daemon=True, kwargs={"poll_interval": 0.2}).start()
            caddy = subprocess.Popen(["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
            try:
                while not manager.shutting_down.wait(0.3):
                    if caddy.poll() is not None:
                        raise RuntimeError(f"Caddy stopped with status {caddy.returncode}")
                    manager.tick()
            finally:
                server.shutdown()
                manager.close()
                terminate(caddy, timeout=3)


if __name__ == "__main__":
    main()
