"""Persistent and authenticated subscription management."""

import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from panel import jobs, manager, merge, subscriptions


class SubscriptionVolumeTests(unittest.TestCase):
    def test_disabled_source_keeps_the_last_download_without_contributing_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "secrets/subscriptions.yaml"
            cache = root / "subscription-cache"
            pool.parent.mkdir()
            cache.mkdir()
            source = root / "node.yaml"
            source.write_text("proxies: [{name: example, type: http, server: 127.0.0.1, port: 9}]")
            entry = {"name": "example", "url": source.as_uri()}
            pool.write_text(yaml.safe_dump({"subscriptions": [entry]}))
            key = subscriptions.cache_key(entry)

            def refresh(name):
                scratch = root / name
                scratch.mkdir()
                with redirect_stdout(io.StringIO()), patch.object(jobs, "DATA", root):
                    sources, degraded = jobs.stage_sources(scratch)
                    jobs.save_subscriptions(scratch)
                return sources, degraded

            sources, degraded = refresh("first")
            self.assertEqual(len(sources), 1)
            self.assertFalse(degraded)
            self.assertEqual(stat.S_IMODE((cache / key).stat().st_mode), 0o600)
            subscriptions.mutate(pool, cache, {"action": "disable", "name": "example"})
            source.unlink()
            self.assertEqual(refresh("disabled"), ([], False))
            self.assertTrue((cache / key).is_file())
            subscriptions.mutate(pool, cache, {"action": "enable", "name": "example"})
            sources, degraded = refresh("reenabled")
            self.assertEqual(len(sources), 1)
            self.assertTrue(degraded)
            subscriptions.mutate(pool, cache, {"action": "delete", "name": "example"})
            self.assertEqual(subscriptions.read_pool(pool), [])
            self.assertFalse((cache / key).exists())

    def test_empty_pool_and_a_small_valid_pool_produce_usable_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, source = root / "config.yaml", root / "source.yaml"
            fixture_rules = root / "rules.yaml"
            fixture_rules.write_text("rules: ['DOMAIN,example.test,DIRECT']")
            source.write_text("proxies: [{name: example, type: http, server: 127.0.0.1, port: 9}]")
            with patch.multiple(merge, RULESET=str(fixture_rules), API_SECRET="test", LOCAL_NODES="",
                                SCORES=str(root / "scores.json")):
                merge.main(str(config), [])
                idle = yaml.safe_load(config.read_text())
                self.assertEqual(idle["proxies"], [])
                self.assertEqual(idle["proxy-groups"][0]["proxies"], ["REJECT"])
                merge.restore(config)
                merge.main(str(config), [str(source)])
                active = yaml.safe_load(config.read_text())
            self.assertEqual(active["proxy-groups"][0]["proxies"], ["example"])

    def test_pending_changes_prevent_bootstrap_from_reusing_old_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "secrets/subscriptions.yaml"
            pool.parent.mkdir()
            pool.write_text("subscriptions: []")
            subscriptions.pending_marker(pool).touch()
            config = root / "config.yaml"
            config.write_text("proxies: [{name: old}]")
            (root / ".config-installed").touch()
            scratch = root / "scratch"
            scratch.mkdir()
            with (
                patch.multiple(jobs, DATA=root, ARTIFACTS=root, CONFIG=config, stage_sources=lambda _: ([], False)),
                patch.multiple(jobs.rulesets, recover=lambda: None, seed=lambda: None, pin_installed=lambda _: None),
                patch.object(jobs.merge, "restore") as restore,
                patch.object(jobs.merge, "main", side_effect=lambda path, _: Path(path).write_text("proxies: []")) as build,
                patch.multiple(jobs, prepare_main=lambda _: None, build_probe=lambda *_: None,
                               atomic_copy=lambda *_: None, save_subscriptions=lambda _: None),
            ):
                jobs.bootstrap(scratch)
            restore.assert_not_called()
            build.assert_called_once_with(str(scratch / "config.yaml"), [])


class SubscriptionApiTests(unittest.TestCase):
    def test_authorized_mutation_persists_secret_without_returning_it(self):
        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), manager.Handler) as server:
            root = Path(tmp)
            with patch.multiple(manager, DATA=root, RUNTIME=root), patch.dict(os.environ, {"MIHOMO_SECRET": "test"}):
                server.core = manager.CoreManager()
            server.core.phase = "running"
            url = f"http://127.0.0.1:{server.server_port}/autofallback/subscriptions"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()

            def request(body=None, *, authorized=True):
                headers = {"Content-Type": "application/json"}
                if authorized:
                    headers["Authorization"] = "Bearer test"
                req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode() if body else None)
                with opener.open(req, timeout=2) as response:
                    return response.status, json.load(response)

            try:
                for body in (None, {"action": "delete", "name": "example"}):
                    with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                        request(body, authorized=False)
                    self.assertEqual(unauthorized.exception.code, 401)
                source = {"action": "add", "name": "example", "url": "https://example.test/sub?token=hidden"}
                with patch.object(server.core, "submit", return_value="refresh-job") as submit:
                    status, result = request(source)
                    self.assertEqual((status, result), (202, {"job": "refresh-job"}))
                    submit.assert_called_once_with("refresh", skip_score=True)
                _, data = request()
                self.assertEqual(data["subscriptions"], [
                    {"name": "example", "enabled": True, "host": "example.test", "cached": False},
                ])
                self.assertTrue(data["pending"])
                self.assertNotIn("hidden", json.dumps(data))
                pool = server.core.subscription_pool
                self.assertIn("token=hidden", pool.read_text())
                self.assertEqual(stat.S_IMODE(pool.stat().st_mode), 0o600)
                with self.assertRaises(urllib.error.HTTPError) as unsupported:
                    request({"action": "add", "name": "file", "url": "file:///data/secrets/private"})
                self.assertEqual(unsupported.exception.code, 400)
            finally:
                server.shutdown()
                thread.join()
