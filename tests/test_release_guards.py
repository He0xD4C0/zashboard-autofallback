"""Regression checks for subscription caching and rule readiness."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "panel"), str(ROOT / "tools"), *sys.path]):
    import rules
    import subscriptions


class SubscriptionCacheTests(unittest.TestCase):
    def test_unavailable_source_uses_only_fresh_cache_for_its_url(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            cache = root / "cache"
            cache.mkdir()
            source = root / "source.yaml"
            source.write_text("proxies: [{name: lab-node, type: http, server: 127.0.0.1, port: 9}]")
            pool = root / "pool.yaml"
            pool.write_text(yaml.safe_dump({
                "subscriptions": [{"name": "primary", "url": source.as_uri()}],
            }))
            first = root / "pending"
            subscriptions.stage(pool, cache, root / "first", first, 86400)
            saved, = first.glob("*.yaml")
            saved = Path(shutil.move(saved, cache))

            source.unlink()
            subscriptions.stage(pool, cache, root / "second", root / "retry", 86400)
            self.assertEqual((root / "first/primary.yaml").read_bytes(),
                             (root / "second/primary.yaml").read_bytes())

            pool.write_text(pool.read_text().replace("source.yaml", "new-url.yaml"))
            with self.assertRaisesRegex(ValueError, "no fresh cache"):
                subscriptions.stage(pool, cache, root / "rotated", root / "rotate-retry", 86400)

            pool.write_text(pool.read_text().replace("new-url.yaml", "source.yaml"))
            os.utime(saved, (1, 1))
            with self.assertRaisesRegex(ValueError, "no fresh cache"):
                subscriptions.stage(pool, cache, root / "expired", root / "expired-retry", 86400)


class RuleReadinessTests(unittest.TestCase):
    def test_bootstrap_reuses_bundle_across_generated_path_and_proxy_fields(self):
        with tempfile.TemporaryDirectory() as scratch:
            cache = Path(scratch)
            original = {
                "rules": ["RULE-SET,Local,DIRECT"],
                "rule-providers": {"Local": {"type": "http", "behavior": "domain", "format": "text",
                                             "url": "https://rules.example.invalid/Local.txt"}},
            }
            seed, generated = cache / "seed.yaml", cache / "candidate.yaml"
            seed.write_text(yaml.safe_dump(original))
            original["rule-providers"]["Local"].update(path="./rule-providers/Local.mrs", proxy="DIRECT")
            generated.write_text(yaml.safe_dump(original))

            with patch.object(rules, "validate_bundle"), patch.object(rules, "download") as download:
                download.side_effect = lambda name, spec, directory, *_: (directory / name).write_text("+.example.net\n")
                rules.prepare(seed, cache)
                rules.prepare(generated, cache, use_existing=True)
                self.assertEqual(download.call_count, 1)
            self.assertEqual(rules.read_config(seed), rules.read_config(generated))

    def test_required_rules_include_nested_and_dns_references(self):
        config = {
            "rules": ["SUB-RULE,(RULE-SET,Main),sub"],
            "sub-rules": {"sub": ["RULE-SET,Secondary,DIRECT"]},
            "dns": {"fake-ip-filter": ["rule-set:DNS1,DNS2"]},
        }
        self.assertEqual(rules.required_rules(config), {"Main", "Secondary", "DNS1", "DNS2"})

    def test_readiness_rejects_empty_and_missing_sets(self):
        class Response:
            def __enter__(self):
                from io import StringIO
                return StringIO(json.dumps({
                    "providers": {"ready": {"ruleCount": 1}, "empty": {"ruleCount": 0}, "null": None},
                }))

            def __exit__(self, *_):
                return False

        with patch("rules.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            result = rules.live("http://127.0.0.1", "fixture", {"ready", "empty", "null", "missing"})
        self.assertEqual(result, ["empty", "missing", "null"])
