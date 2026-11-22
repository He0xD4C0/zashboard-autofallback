"""合并模块的纯函数测试：评分快照的容错、组排序。

merge.py 在 import 时会读环境变量（secret、站点组、路径），所以每个用例
用独立的模块加载，避免互相污染。
"""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "panel", "merge.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScoreTableTests(unittest.TestCase):
    def test_malformed_entry_is_skipped(self):
        merge = load("merge_bad")
        merge.SCORES = os.path.join(ROOT, "tests", "fixtures", "scores.malformed.json")
        err = io.StringIO()
        with (
            patch.object(merge.time, "time", return_value=os.path.getmtime(merge.SCORES)),
            redirect_stderr(err),
        ):
            table = merge._score_table()
        self.assertIn("good", table)
        self.assertNotIn("broken", table)
        self.assertIn("ignoring malformed score entry", err.getvalue())

    def test_rank_orders_by_score_then_keeps_unscored_last(self):
        merge = load("merge_rank")
        scores = {"a": (10.0, {"gstatic": 10.0}), "b": (99.0, {"gstatic": 99.0})}
        self.assertEqual(merge._rank(["a", "x", "b"], scores), ["b", "a", "x"])


class SiteGroupTests(unittest.TestCase):
    def test_absent_without_env(self):
        os.environ.pop("MIHOMO_SITE_DOMAINS", None)
        os.environ.pop("MIHOMO_SITE_HEALTH_URL", None)
        merge = load("merge_no_site")
        self.assertFalse(merge.SITE_ENABLED)
        self.assertEqual(merge.SITE_RULES, [])

    def test_present_with_env(self):
        os.environ["MIHOMO_SITE_DOMAINS"] = "panel.example.com"
        os.environ["MIHOMO_SITE_HEALTH_URL"] = "https://panel.example.com/"
        try:
            merge = load("merge_site")
            self.assertTrue(merge.SITE_ENABLED)
            self.assertEqual(
                merge.SITE_RULES, ["DOMAIN-SUFFIX,panel.example.com,出口"]
            )
        finally:
            os.environ.pop("MIHOMO_SITE_DOMAINS", None)
            os.environ.pop("MIHOMO_SITE_HEALTH_URL", None)

    def test_partial_config_is_rejected(self):
        os.environ["MIHOMO_SITE_DOMAINS"] = "panel.example.com"
        os.environ.pop("MIHOMO_SITE_HEALTH_URL", None)
        try:
            with self.assertRaises(SystemExit):
                load("merge_partial")
        finally:
            os.environ.pop("MIHOMO_SITE_DOMAINS", None)


class RulesetTests(unittest.TestCase):
    def setUp(self):
        os.environ["MIHOMO_RULESET"] = os.path.join(
            ROOT, "tests", "fixtures", "ruleset.tiny.yaml"
        )
        self.merge = load("merge_ruleset")

    def tearDown(self):
        os.environ.pop("MIHOMO_RULESET", None)

    def test_targets_are_mapped_to_local_groups(self):
        self.assertEqual(self.merge._map_rule("RULE-SET,Ads,REJECT"), "RULE-SET,Ads,REJECT")
        self.assertEqual(self.merge._map_rule("RULE-SET,CN,国内流量"), "RULE-SET,CN,DIRECT")
        self.assertEqual(self.merge._map_rule("RULE-SET,AI,人工智能"), "RULE-SET,AI,出口")
        self.assertEqual(
            self.merge._map_rule("DOMAIN-SUFFIX,example.com,国外流量"),
            "DOMAIN-SUFFIX,example.com,出口",
        )

    def test_and_rule_maps_last_segment(self):
        self.assertEqual(
            self.merge._map_rule("AND,((DST-PORT,443),(NETWORK,UDP)),REJECT"),
            "AND,((DST-PORT,443),(NETWORK,UDP)),REJECT",
        )

    def test_no_resolve_suffix_survives(self):
        self.assertEqual(
            self.merge._map_rule("RULE-SET,ProxyIP,国外流量,no-resolve"),
            "RULE-SET,ProxyIP,出口,no-resolve",
        )

    def test_ruleset_loads_providers_and_drops_match(self):
        providers, rules = self.merge._load_ruleset()
        self.assertEqual(sorted(providers), ["AI", "Ads", "CN"])
        self.assertEqual(len(rules), 5)
        self.assertEqual(len(self.merge._map_rules(rules)), 5)


if __name__ == "__main__":
    unittest.main()
