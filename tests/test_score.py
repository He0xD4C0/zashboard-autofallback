"""评分模块的纯函数测试：稳定性、单平台打分、站点平台的可配置性。

测试只加载 shell/score.py 的函数，不连内核、不读运行期目录。
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path=None):
    path = path or os.path.join(ROOT, "panel", "score.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.score = load("score_stab")

    def test_single_sample_is_neutral(self):
        # 一次测量无法判断抖动；按中性（满分）处理，否则单采样运行总分上限只有 85
        self.assertEqual(self.score.stability_score([120.0]), 1.0)

    def test_no_sample_scores_zero(self):
        self.assertEqual(self.score.stability_score([]), 0.0)

    def test_stable_samples_score_one(self):
        self.assertEqual(self.score.stability_score([100.0, 100.0]), 1.0)

    def test_noisy_samples_score_lower(self):
        self.assertLess(self.score.stability_score([50.0, 150.0]), 1.0)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.score = load("score_platform")

    def test_single_sample_can_reach_full_marks(self):
        cell = self.score.score_platform([300.0])
        self.assertEqual(cell["availability"], 1.0)
        self.assertEqual(cell["score"], 100.0)


class SitePlatformTests(unittest.TestCase):
    def test_site_platform_follows_env(self):
        os.environ.pop("MIHOMO_SITE_HEALTH_URL", None)
        self.assertNotIn("site", load("score_no_site").PLATFORMS)
        os.environ["MIHOMO_SITE_HEALTH_URL"] = "https://panel.example.com/"
        self.assertIn("site", load("score_with_site").PLATFORMS)
        os.environ.pop("MIHOMO_SITE_HEALTH_URL", None)


class PurityWeightTests(unittest.TestCase):
    def test_scoring_uses_the_published_weights(self):
        score = load("score_weights")
        cases = (
            ("gstatic", {"node": {"purity": 0.0}}, 0.10, 90.0),
            ("gstatic,ai", {"node": {"purity": 0.0}}, 0.10, 90.0),
            ("all", {"node": {"purity": 0.0}}, 0.10, 90.0),
            ("gstatic", {}, 0.0, 100.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "scores.json"
            for platforms, purity, weight, overall in cases:
                args = ["score.py", "--platforms", platforms, "--json", str(output)]
                with self.subTest(platforms=platforms, purity=purity):
                    with (
                        patch.object(sys, "argv", args),
                        patch.multiple(
                            score, PURITY_WEIGHT=0.10, _secret=lambda: "test",
                            nodes=lambda _: ["node"], probe=lambda *_: 50.0,
                            load_purity=lambda: purity,
                        ),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(score.main(), 0)
                    data = json.loads(output.read_text())
                    self.assertEqual(data["purity_weight"], weight)
                    self.assertAlmostEqual(sum(data["platforms"].values()) + weight, 1.0, places=5)
                    self.assertEqual(data["nodes"]["node"]["overall"], overall)


if __name__ == "__main__":
    unittest.main()
