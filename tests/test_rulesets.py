"""Checks for HTTPS ruleset sources and durable replacement."""

import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from panel import merge, rulesets


class RuleSourceTests(unittest.TestCase):
    def test_private_addresses_are_rejected_before_download(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(rulesets.socket, "getaddrinfo", return_value=answer), \
             patch.object(rulesets.subprocess, "run") as curl:
            with self.assertRaisesRegex(ValueError, "public addresses"):
                rulesets.get("https://rules.example", Path("/unused"), 1024)
        curl.assert_not_called()

    def test_rejects_credentialed_non_https_and_nonstandard_ports(self):
        for address in ("http://example.com/rules", "file:///data/config.yaml",
                        "https://user:pass@example.com", "https://example.com:8080/rules"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                rulesets.https_source(address)

    def test_public_github_fallback_preserves_explicit_branch(self):
        with patch.object(rulesets, "fetch", side_effect=[OSError(), RuntimeError(), None]) as fetch:
            rulesets.get("https://raw.githubusercontent.com/owner/repo/refs/heads/main/rules.yaml",
                         Path("/unused"), 1024)
        self.assertEqual(fetch.call_args_list[1].args[0],
                         "https://api.github.com/repos/owner/repo/contents/rules.yaml?ref=main")
        self.assertEqual(fetch.call_args_list[2].args[0],
                         "https://cdn.jsdelivr.net/gh/owner/repo@main/rules.yaml")

    def test_manifest_drops_unmanaged_fields(self):
        doc = {
            "rules": ["RULE-SET,example,DIRECT"], "external-controller": "0.0.0.0:9090",
            "rule-providers": {"example": {
                "type": "http", "behavior": "domain", "format": "text",
                "url": "https://example.com/domains", "path": "/data/secrets/node",
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "download"
            source.write_text(yaml.safe_dump(doc))
            parsed = rulesets.sanitize_document(source, "https://example.com/rules")
        self.assertNotIn("external-controller", parsed)
        self.assertNotIn("path", parsed["rule-providers"]["example"])

    def test_legacy_targets_converge_to_single_exit(self):
        config = {
            "proxies": [{"name": "node"}],
            "proxy-groups": [
                {"name": "出口", "type": "fallback", "url": merge.HEALTH_URL, "proxies": ["node"]},
                {"name": "AI出口", "type": "fallback", "proxies": ["node"]},
                {"name": "legacy", "type": "select", "proxies": ["node"]},
            ],
            "sniffer": merge.SNIFFER, "dns": merge.DNS_OVERRIDE,
            "rules": [
                *(rule.replace(",出口", ",AI出口") if rule in merge.AI_RULES else rule
                  for rule in merge.PINNED_RULES),
                "DOMAIN,example.com,legacy", "MATCH,出口",
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(rulesets.encode(config))
            with patch.object(merge, "API_SECRET", "fixture"):
                merge.restore(path)
            restored = yaml.safe_load(path.read_text())
        self.assertEqual([group["name"] for group in restored["proxy-groups"]], ["出口"])
        self.assertIn("DOMAIN,example.com,出口", restored["rules"])


class RuleTransactionTests(unittest.TestCase):
    def test_failed_readiness_restores_previous_config_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active, config, journal, candidate = [root / name for name in ("active", "config", "journal", "candidate")]
            active.write_text("source: https://example.com/previous")
            config.write_text("rules: ['MATCH,DIRECT']")
            candidate.write_text("rules: ['MATCH,REJECT']")
            with patch.multiple(rulesets, ACTIVE=active, CONFIG=config, JOURNAL=journal, RUNTIME=root), \
                 patch.object(rulesets.reload, "reload", return_value=0) as reload, \
                 patch.object(rulesets.rules, "live", return_value=["missing"]):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    rulesets.commit(candidate, "source: https://example.com/candidate", "fixture")
            self.assertIn("previous", active.read_text())
            self.assertIn("DIRECT", config.read_text())
            self.assertFalse(journal.exists())
            self.assertEqual([call.args[2] for call in reload.call_args_list], [candidate, config])

    def test_startup_recovers_interrupted_two_file_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active, config, journal = [root / name for name in ("active", "config", "journal")]
            active.write_text("previous")
            config.write_text("candidate config written before the interruption")
            journal.write_text(json.dumps({
                "config": "previous configuration", "next": hashlib.sha256(b"new source").hexdigest(),
            }))
            with patch.multiple(rulesets, ACTIVE=active, CONFIG=config, JOURNAL=journal):
                rulesets.recover()
            self.assertEqual(config.read_text(), "previous configuration")
            self.assertFalse(journal.exists())
