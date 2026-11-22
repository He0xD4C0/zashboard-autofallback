"""数据契约的单元测试：好样本通过、坏样本必须被挡下。

测试只碰 tests/fixtures 下的合成数据，不读运行期目录，所以不需要 docker / 内核在跑。
"""

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate", os.path.join(ROOT, "tools", "validate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = _load_validator()


class ScoresTests(unittest.TestCase):
    def _run(self, fixture):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validate.validate_scores(os.path.join(FIXTURES, fixture))
        return code, out.getvalue(), err.getvalue()

    def test_good_snapshot_passes(self):
        code, out, _ = self._run("scores.good.json")
        self.assertEqual(code, 0)
        self.assertIn("2 nodes", out)

    def test_bad_snapshot_is_rejected(self):
        code, _, err = self._run("scores.bad.json")
        self.assertEqual(code, 1)
        self.assertIn("invalid at", err)

    def test_bad_snapshot_reports_every_violation_shape(self):
        _, _, err = self._run("scores.bad.json")
        # 缺 generated、overall 越界、availability 越界、samples 类型错，至少 4 条
        self.assertGreaterEqual(err.count("invalid at"), 4)


class SubscriptionTests(unittest.TestCase):
    def _run(self, fixture):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validate.validate_subs(os.path.join(FIXTURES, fixture))
        return code, out.getvalue(), err.getvalue()

    def test_good_pool_passes_and_lists_names(self):
        code, out, _ = self._run("subscriptions.good.yaml")
        self.assertEqual(code, 0)
        self.assertIn("primary", out)
        self.assertIn("backup", out)

    def test_bad_pool_is_rejected(self):
        code, _, _ = self._run("subscriptions.bad.yaml")
        self.assertEqual(code, 1)

    def test_file_url_is_allowed_for_offline_dev(self):
        code, _, err = self._run("subscriptions.file.yaml")
        self.assertEqual(code, 0, err)

    def test_duplicate_name_is_rejected_even_when_fields_are_valid(self):
        # 两个条目字段都合法，只有标签重复：schema 层看不出来，必须由代码兜住。
        code, _, err = self._run("subscriptions.dup.yaml")
        self.assertEqual(code, 1)
        self.assertIn("duplicate subscription name", err)


class CliTests(unittest.TestCase):
    def test_usage_error_exit_code(self):
        self.assertEqual(validate.main(["validate.py"]), 2)
        self.assertEqual(validate.main(["validate.py", "bogus", "x"]), 2)

    def test_missing_file_exit_code(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = validate.main(["validate.py", "scores", os.path.join(FIXTURES, "nope.json")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
