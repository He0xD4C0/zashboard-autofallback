"""纯净度、探测配置与面板测试，不依赖运行期数据。"""

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


purity = load("purity_mod", os.path.join(ROOT, "tools", "purity.py"))


class RegionTest(unittest.TestCase):
    def test_flags_and_names(self):
        self.assertEqual(purity.region_of("🇯🇵日本高速01|BGP|CUCM"), "jp")
        self.assertEqual(purity.region_of("Singapore-02"), "sg")
        self.assertEqual(purity.region_of("🇺🇸洛杉矶 LOS ANGELES"), "us")
        self.assertEqual(purity.region_of("🇭🇰香港IPLC"), "hk")
        self.assertEqual(purity.region_of("未知"), "unknown")


class PurityScoreTest(unittest.TestCase):
    def info(self, **kw):
        base = {"country_code": "JP", "hosting": False, "proxy": False, "mobile": False}
        base.update(kw)
        return base

    def test_residential(self):
        score, reason = purity.score_of(self.info(), "jp")
        self.assertEqual(score, 100.0)
        self.assertEqual(reason, "residential")

    def test_mobile_counts_as_residential(self):
        score, _ = purity.score_of(self.info(mobile=True), "jp")
        self.assertEqual(score, 100.0)

    def test_datacenter_is_penalised(self):
        score, reason = purity.score_of(self.info(hosting=True), "jp")
        self.assertEqual(score, 55.0)
        self.assertEqual(reason, "datacenter")

    def test_flagged_proxy_is_lowest(self):
        score, reason = purity.score_of(self.info(proxy=True), "jp")
        self.assertEqual(score, 20.0)
        self.assertEqual(reason, "flagged-proxy")

    def test_region_mismatch_subtracts(self):
        score, reason = purity.score_of(self.info(hosting=True, country_code="FI"), "de")
        self.assertEqual(score, 30.0)  # 55 - 25
        self.assertIn("region-mismatch", reason)

    def test_mismatch_never_goes_negative(self):
        score, _ = purity.score_of(self.info(proxy=True, country_code="SG"), "us")
        self.assertEqual(score, 0.0)

    def test_unknown_claimed_region_does_not_penalise(self):
        score, reason = purity.score_of(self.info(), "unknown")
        self.assertEqual(score, 100.0)
        self.assertNotIn("mismatch", reason)


class TraceParseTest(unittest.TestCase):
    def test_parses_ip_and_loc(self):
        raw = "fl=123f\nh=www.cloudflare.com\nip=203.0.113.7\nts=1700000000\nloc=JP\n"
        doc = purity.parse_trace(raw)
        self.assertEqual(doc["ip"], "203.0.113.7")
        self.assertEqual(doc["country_code"], "JP")
        self.assertEqual(doc["source"], "cdn-cgi/trace")

    def test_missing_ip_raises(self):
        with self.assertRaises(RuntimeError):
            purity.parse_trace("loc=JP\n")


class PuritySnapshotTest(unittest.TestCase):
    def probe(self, trace_country, classification):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "purity.json"
            found = {"ip": "203.0.113.7", "country_code": trace_country, "source": "trace"}
            with (
                patch.object(purity, "PURITY_MISSING", 40.0, create=True),
                patch.multiple(
                    purity, secret=lambda: "test",
                    nodes=lambda _: ["日本A"], select=lambda *_: True,
                    reachable=lambda *_: True, exit_ip_via_node=lambda _: found,
                    classify=classification,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(purity.main(["purity.py", "--json", str(output)]), 0)
            return json.loads(output.read_text())["nodes"]["日本A"]

    def test_classification_failure_scores_as_unknown(self):
        result = self.probe("JP", Mock(side_effect=TimeoutError("lookup timed out")))
        self.assertEqual(result["purity"], 40.0)
        self.assertEqual(result["reason"], "unclassified")
        self.assertIsNone(result["hosting"])
        self.assertIsNone(result["proxy"])
        self.assertTrue(result["region_match"])

    def test_country_code_matches_the_scoring_decision(self):
        for observed, expected, matches, score in (
            ("US", "US", False, 30.0),
            (None, "JP", True, 55.0),
        ):
            with self.subTest(observed=observed):
                result = self.probe("JP", Mock(return_value={
                    "country_code": observed, "hosting": True, "proxy": False,
                }))
                self.assertEqual(result["country_code"], expected)
                self.assertIs(result["region_match"], matches)
                self.assertEqual(result["purity"], score)


class WatchdogVerdictTest(unittest.TestCase):
    def test_majority_rule(self):
        wd = load("watchdog_mod", os.path.join(ROOT, "tools", "watchdog.py"))
        self.assertTrue(wd.is_healthy(2, 3))
        self.assertFalse(wd.is_healthy(1, 3))
        self.assertTrue(wd.is_healthy(1, 1))   # 单目标时 1 次通过即健康
        self.assertFalse(wd.is_healthy(0, 1))


class ScanConfigTest(unittest.TestCase):
    """探测进程的 API 和代理只在 Panel 容器的 loopback 上访问。"""

    def test_probe_config_binds_loopback(self):
        make = load("mkprobe", os.path.join(ROOT, "panel", "make_probe_config.py"))
        doc = make.build(
            {"proxies": [{"name": "n1", "type": "ss", "server": "1.2.3.4", "port": 443,
                          "cipher": "aes-128-gcm", "password": "x"}]},
            7899,
            "s3cret",
            "127.0.0.1:9190",
        )
        self.assertEqual(doc["mode"], "global")
        self.assertFalse(doc["allow-lan"])
        self.assertEqual(doc["external-controller"], "127.0.0.1:9190")
        self.assertEqual(doc["mixed-port"], 7899)
        self.assertNotIn("proxy-groups", doc)  # global 模式由内核自动提供 GLOBAL

    def test_rejects_empty_pool(self):
        make = load("mkprobe2", os.path.join(ROOT, "panel", "make_probe_config.py"))
        with self.assertRaises(SystemExit):
            make.build({}, 7899, "s", "127.0.0.1:9190")

    def test_probe_config_is_private_before_and_after_install(self):
        make = load("mkprobe_private", os.path.join(ROOT, "panel", "make_probe_config.py"))
        replace = os.replace

        def check_temporary_mode(src, dst):
            self.assertEqual(stat.S_IMODE(Path(src).stat().st_mode), 0o600)
            replace(src, dst)

        for exists in (False, True):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "config.yaml"
                dst = Path(tmp) / "probe.yaml"
                src.write_text(yaml.safe_dump({"proxies": [{"name": "n1", "password": "test"}]}))
                if exists:
                    dst.write_text("previous config")
                    dst.chmod(0o644)
                with patch.object(make.os, "replace", side_effect=check_temporary_mode):
                    self.assertEqual(make.main(["make", str(src), str(dst), "--secret", "test"]), 0)
                self.assertEqual(stat.S_IMODE(dst.stat().st_mode), 0o600)
                self.assertEqual(yaml.safe_load(dst.read_text())["secret"], "test")
                self.assertEqual(set(Path(tmp).iterdir()), {src, dst})


class DiagnosticsSnapshotTest(unittest.TestCase):
    def test_publish_scores_without_raw_log_details_or_controller_secrets(self):
        with patch.object(sys, "path", [os.path.join(ROOT, "tools"), *sys.path]):
            diagnostic = load("diagnostic_mod", os.path.join(ROOT, "tools", "diagnostics.py"))
        scores = {
            "generated": "2026-11-20T10:00:00+08:00",
            "platforms": {"gstatic": 0.9},
            "purity_weight": 0.1,
            "nodes": {
                "🇯🇵日本A": {
                    "overall": 96.0,
                    "final": 101.0,
                    "region": "jp",
                    "bonus": 5.0,
                    "platforms": {"gstatic": {"score": 98.0, "availability": 1.0}},
                    "purity": {"score": 55.0, "ip": "203.0.113.9", "country_code": "JP",
                               "claimed": "jp", "region_match": True, "hosting": True,
                               "proxy": False, "reason": "datacenter"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_text(json.dumps({
                "phase": "watchdog", "status": "degraded", "group": "出口",
                "detail": "https://example.invalid/?token=secret",
            }), encoding="utf-8")
            state = diagnostic.make_snapshot(scores, {}, diagnostic.read_events(path), None)
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(state["nodes"][0]["purity"], 55.0)
        self.assertEqual(state["nodes"][0]["country"], "JP")
        self.assertEqual(state["events"][0]["status"], "degraded")
        self.assertNotIn("secret", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
