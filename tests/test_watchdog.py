"""巡检测试覆盖同组备选、控制器回读和默认目标去重。"""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import yaml

from tools import watchdog


TARGETS = {
    "http://www.gstatic.com/generate_204": "204",
    "https://chatgpt.com": "200",
    "https://cp.cloudflare.com/generate_204": "204",
}


class WatchdogReselectTests(unittest.TestCase):
    def run_watchdog(self, healthy_alternative=True, apply_selection=True, new_selection=None,
                     initial_fixed="", fail_group_url=False, config_changed=False, confirm_fixed=True,
                     transient_initial=False):
        health = next(iter(TARGETS))
        members = ["current", "single-site", "backup"]
        group_path = "/proxies/Exit"
        passed = {node: {health} for node in members}
        if healthy_alternative:
            passed["backup"] = set(TARGETS)
        if fail_group_url:
            passed["current"].clear()
        current = "current"
        fixed = initial_fixed
        selections = []
        probes = []
        group_reads = 0

        def api(path, timeout, token, method="GET", body=None):
            nonlocal current, fixed, group_reads
            if path == group_path and method == "GET":
                group_reads += 1
                if new_selection and probes and not selections:
                    current, fixed = new_selection
                selected = "backup" if transient_initial and group_reads == 1 else current
                return {
                    "now": selected, "fixed": fixed, "all": members, "type": "Fallback",
                    "testUrl": health, "expectedStatus": "204",
                }
            if path == group_path and method == "PUT":
                with lock.open("a") as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                selections.append(body["name"])
                if apply_selection:
                    current = body["name"]
                    if confirm_fixed:
                        fixed = current
                return {}
            if path.rsplit("/", 1)[-1] in members:
                node = path.rsplit("/", 1)[-1]
                return {"extra": {url: {"alive": url in passed[node]} for url in TARGETS}}
            parsed = urlsplit(path)
            _, route, node, action = parsed.path.split("/")
            self.assertEqual((route, action), ("proxies", "delay"))
            query = parse_qs(parsed.query)
            url = query["url"][0]
            probes.append((node, url, query["expected"][0]))
            if config_changed and node == "backup" and url == health:
                replacement = config.with_suffix(".new")
                replacement.write_text(config.read_text())
                replacement.replace(config)
            if url not in passed[node] and url != "https://chatgpt.com":
                raise TimeoutError("probe failed")
            # A HTTP status mismatch still returns a delay in the real controller.
            return {"delay": 5}

        journal = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            lock = Path(tmp) / ".controller.lock"
            config.write_text(yaml.safe_dump({
                "proxy-groups": [{"name": "Exit", "type": "fallback", "url": health}],
            }))
            with (
                patch.dict(os.environ, {"MIHOMO_CONTROLLER_LOCK": str(lock)}),
                patch.multiple(watchdog, CONFIG=config, secret=lambda: "test", api=api, journal=journal),
            ):
                self.assertEqual(watchdog.main(["watchdog.py"]), 0)
        return journal.call_args_list[0].args[0], selections, probes

    def test_switches_only_to_a_candidate_that_passes_the_same_targets(self):
        event, selections, probes = self.run_watchdog()
        self.assertEqual(event["status"], "degraded")
        self.assertEqual(event["action"], "switched")
        self.assertEqual(event["previous"], "current")
        self.assertEqual(event["selected"], "backup")
        self.assertEqual(event["ok"], 1)
        self.assertEqual(selections, ["backup"])
        for node in ("current", "single-site", "backup"):
            self.assertEqual(
                [(url, expected) for name, url, expected in probes if name == node],
                list(TARGETS.items()),
            )

    def test_keeps_current_node_when_no_candidate_is_healthy(self):
        event, selections, _ = self.run_watchdog(healthy_alternative=False)
        self.assertEqual(event["action"], "no-healthy-alternative")
        self.assertEqual(selections, [])

    def test_does_not_record_a_switch_when_the_kernel_keeps_the_old_node(self):
        event, selections, _ = self.run_watchdog(apply_selection=False)
        self.assertEqual(selections, ["backup"])
        self.assertEqual(event["selected"], "current")
        self.assertEqual(event["action"], "reselect-unconfirmed")

    def test_uses_healthy_backup_after_kernel_automatically_selects_degraded_node(self):
        event, selections, _ = self.run_watchdog(new_selection=("single-site", ""))
        self.assertEqual(selections, ["backup"])
        self.assertEqual(event["action"], "switched")

    def test_respects_a_new_manual_selection_during_probes(self):
        event, selections, _ = self.run_watchdog(new_selection=("single-site", "single-site"))
        self.assertEqual(selections, [])
        self.assertEqual(event["action"], "selection-changed")

    def test_fixes_a_transient_automatic_choice_after_a_pinned_node_fails(self):
        event, selections, _ = self.run_watchdog(
            initial_fixed="current", fail_group_url=True, new_selection=("backup", ""),
        )
        self.assertEqual(selections, ["backup"])
        self.assertEqual((event["action"], event["selected"]), ("switched", "backup"))

    def test_selection_readback_must_confirm_the_pin(self):
        event, selections, _ = self.run_watchdog(confirm_fixed=False)
        self.assertEqual(selections, ["backup"])
        self.assertEqual((event["action"], event["selected"]), ("reselect-unconfirmed", "backup"))

    def test_config_replaced_during_probes_invalidates_the_result(self):
        event, selections, _ = self.run_watchdog(config_changed=True)
        self.assertEqual(selections, [])
        self.assertEqual(event["action"], "selection-changed")

    def test_transient_initial_choice_does_not_hide_the_degraded_current_node(self):
        event, selections, _ = self.run_watchdog(transient_initial=True)
        self.assertEqual(event["previous"], "current")
        self.assertEqual(selections, ["backup"])
