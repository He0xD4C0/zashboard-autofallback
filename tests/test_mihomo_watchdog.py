"""使用 MIHOMO_TEST_BIN 指定的内核运行回环网络集成测试。"""

import contextlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from panel import jobs

WATCHDOG = Path(__file__).resolve().parents[1] / "tools" / "watchdog.py"
MIHOMO_TEST_BIN = os.environ.get("MIHOMO_TEST_BIN")
TARGET = "http://127.0.0.1:9"


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_CONNECT(self):
        self.send_response_only(200, "Connection established")
        self.end_headers()
        request = self.rfile.readline().decode()
        if not request:
            return
        while self.rfile.readline() not in (b"\r\n", b""):
            pass
        path = request.split()[1]
        status, body = "204 No Content", b""
        if path == "/service":
            status, body = "200 OK", self.server.node.encode()
        elif path == "/health" and self.server.fail_health:
            status = "403 Forbidden"
        elif self.server.partial:
            if path == "/third":
                return
            if path == "/second":
                status = "403 Forbidden"
        self.wfile.write(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    def log_message(self, *_):
        pass


@unittest.skipUnless(MIHOMO_TEST_BIN, "set MIHOMO_TEST_BIN to run against a real Mihomo instance")
class MihomoWatchdogTests(unittest.TestCase):
    def test_repeated_failover_and_refresh_preserve_healthy_proxy_traffic(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
            root = Path(tmp)
            proxies = []
            servers = {}
            for node, partial in (
                ("current-A", True), ("single-site-B", True), ("healthy-C", False), ("healthy-D", False),
            ):
                server = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler))
                server.node, server.partial = node, partial
                server.fail_health = False
                servers[node] = server
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                cleanup.callback(worker.join)
                cleanup.callback(server.shutdown)
                proxies.append({
                    "name": node, "type": "http", "server": "127.0.0.1", "port": server.server_port,
                })
            with socket.socket() as controller_socket, socket.socket() as mixed_socket:
                for sock in (controller_socket, mixed_socket):
                    sock.bind(("127.0.0.1", 0))
                api_port = controller_socket.getsockname()[1]
                mixed_port = mixed_socket.getsockname()[1]
            api_url = f"http://127.0.0.1:{api_port}"
            token = "isolated-test"
            config, journal = root / "config.yaml", root / "run.log"
            config.write_text(yaml.safe_dump({
                "external-controller": f"127.0.0.1:{api_port}", "mixed-port": mixed_port,
                "secret": token, "proxies": proxies, "rules": ["MATCH,Exit"],
                "profile": {"store-selected": True},
                "proxy-groups": [{
                    "name": "Exit", "type": "fallback", "proxies": [p["name"] for p in proxies],
                    "url": TARGET + "/health", "expected-status": 204,
                }],
            }))
            kernel_log = cleanup.enter_context((root / "kernel.log").open("w+"))
            kernel = cleanup.enter_context(subprocess.Popen(
                [MIHOMO_TEST_BIN, "-d", root, "-f", config], stdout=kernel_log, stderr=kernel_log,
            ))

            def stop_kernel():
                if kernel.poll() is None:
                    kernel.terminate()
                    try:
                        kernel.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        kernel.kill()

            cleanup.callback(stop_kernel)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def controller(path, timeout=1):
                request = urllib.request.Request(
                    api_url + path, headers={"Authorization": "Bearer " + token},
                )
                with opener.open(request, timeout=timeout) as response:
                    return json.load(response)

            def selected_node():
                return controller("/proxies/Exit")["now"]

            def proxy_request():
                connection = http.client.HTTPConnection("127.0.0.1", mixed_port, timeout=2)
                try:
                    connection.request("GET", TARGET + "/service")
                    response = connection.getresponse()
                    return response.status, response.read().decode()
                finally:
                    connection.close()

            initial = None
            for _ in range(80):
                try:
                    initial = selected_node()
                    break
                except OSError:
                    if kernel.poll() is not None:
                        break
                    time.sleep(0.05)
            if initial is None:
                kernel_log.seek(0)
                self.fail("Mihomo failed to start\n" + kernel_log.read())
            self.assertEqual(initial, "current-A")
            for _ in range(20):
                traffic = proxy_request()
                if traffic == (200, initial):
                    break
                time.sleep(0.05)
            self.assertEqual(traffic, (200, initial))
            env = {
                **os.environ, "MIHOMO_CONFIG": str(config), "MIHOMO_SECRET": token,
                "MIHOMO_API": api_url, "RUN_LOG": str(journal),
                "MIHOMO_CONTROLLER_LOCK": str(root / ".controller.lock"),
                "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
            }
            targets = ",".join(TARGET + path for path in ("/health", "/second", "/third"))
            command = [sys.executable, str(WATCHDOG), "--force-targets", targets, "--timeout", "1000"]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            event = json.loads(journal.read_text().splitlines()[0])
            self.assertEqual((event["ok"], event["targets"]), (1, 3), event)
            self.assertEqual((event["action"], event["previous"], event["selected"]),
                             ("switched", initial, "healthy-C"), event)
            self.assertEqual(selected_node(), "healthy-C")
            self.assertEqual(proxy_request(), (200, "healthy-C"))
            with patch.multiple(jobs, CONFIG=config, controller=controller):
                jobs.warm()
            self.assertEqual(controller("/proxies/Exit")["fixed"], "healthy-C")
            self.assertEqual(proxy_request(), (200, "healthy-C"))

            servers["healthy-C"].partial = True
            servers["healthy-C"].fail_health = True
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            event = json.loads(journal.read_text().splitlines()[-2])
            self.assertEqual((event["action"], event["previous"], event["selected"]),
                             ("switched", "healthy-C", "healthy-D"), event)
            current = controller("/proxies/Exit")
            self.assertEqual((current["now"], current["fixed"]), ("healthy-D", "healthy-D"))
            self.assertEqual(proxy_request(), (200, "healthy-D"))

            scratch = root / "refresh"
            scratch.mkdir()
            with (
                patch.dict(os.environ, {"MIHOMO_SECRET": token}),
                patch.multiple(
                    jobs, CONFIG=config, RUNTIME=root, ARTIFACTS=root, API=api_url,
                    PROBE_CONFIG=root / "probe-config.yaml", controller=controller,
                    stage_sources=lambda _: ([], False), prepare_main=lambda _: None,
                    build_probe=shutil.copyfile, save_subscriptions=lambda _: None,
                ),
                patch.object(jobs.merge, "main", side_effect=lambda path, _: shutil.copyfile(config, path)),
                patch.object(jobs.rulesets, "pin_installed"),
            ):
                self.assertFalse(jobs.refresh(scratch, skip_score=True))
            current = controller("/proxies/Exit")
            self.assertEqual((current["now"], current["fixed"]), ("healthy-D", "healthy-D"))
            self.assertEqual(proxy_request(), (200, "healthy-D"))
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            event = json.loads(journal.read_text().splitlines()[-2])
            self.assertEqual((event["node"], event["status"]), ("healthy-D", "healthy"), event)
