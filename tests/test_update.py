"""Regression checks for the migrated refresh transaction and managed core."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from panel import jobs, manager, releases


class RefreshTests(unittest.TestCase):
    def test_purity_failure_still_scores_and_reloads(self):
        self.assertTrue(self.refresh(purity_fails=True))

    def test_successful_probe_runs_before_scoring(self):
        self.assertFalse(self.refresh(purity_fails=False))

    def refresh(self, purity_fails):
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root)
            candidate = scratch / jobs.CONFIG.name
            probe = scratch / jobs.PROBE_CONFIG.name
            steps = Mock()
            if purity_fails:
                steps.purity.side_effect = RuntimeError("probe unavailable")
            steps.stage_sources.return_value = (["source.yaml"], False)
            with patch.dict(os.environ, {"PURITY_ENABLED": "1"}), \
                 patch.multiple(jobs, stage_sources=steps.stage_sources, prepare_main=steps.prepare_main,
                                build_probe=steps.build_probe, reload_config=steps.reload_config,
                                atomic_copy=steps.atomic_copy, save_subscriptions=steps.save_subscriptions,
                                warm=steps.warm, purity=steps.purity, score=steps.score), \
                 patch.object(jobs.rulesets, "pin_installed", steps.pin_installed), \
                 patch.object(jobs.merge, "main", steps.merge):
                degraded = jobs.refresh(scratch, skip_score=False)
            self.assertEqual(
                steps.mock_calls,
                [call.stage_sources(scratch), call.merge(str(candidate), ["source.yaml"]),
                 call.prepare_main(candidate), call.build_probe(candidate, probe),
                 call.reload_config(candidate), call.atomic_copy(probe, jobs.PROBE_CONFIG),
                 call.pin_installed(candidate), call.save_subscriptions(scratch), call.warm(), call.purity(), call.score(),
                 call.merge(str(candidate), ["source.yaml"]),
                 call.prepare_main(candidate), call.reload_config(candidate), call.warm()],
            )
            return degraded


class CoreSwitchTests(unittest.TestCase):
    @patch.object(manager, "CONFIG", Path("/data/artifacts/config.yaml"))
    @patch.object(manager, "PROBE_CONFIG", Path("/data/artifacts/probe-config.yaml"))
    def test_readiness_failure_restores_committed_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(manager, "RUNTIME", Path(tmp)), \
             patch.object(manager, "CACHE", Path(tmp)), \
             patch.dict(os.environ, {"MIHOMO_SECRET": "fixture"}):
            core = manager.CoreManager()
            core.store(current="v1.19.32")
            new = Path(tmp) / "new"
            old = Path(tmp) / "old"
            started = Mock(side_effect=[RuntimeError("candidate failed readiness"), None])
            with patch.object(releases, "install", return_value=new), \
                 patch.object(releases, "executable", return_value=old), \
                 patch.object(core, "wait_task", return_value=0), \
                 patch.object(core, "start_core", started), patch.object(core, "stop_children"):
                with self.assertRaisesRegex(RuntimeError, "restored v1.19.32"):
                    core.switch("v1.19.33")
            self.assertEqual(started.call_args_list, [call(new, "v1.19.33"), call(old, "v1.19.32")])
            self.assertEqual(manager.CoreManager().state["current"], "v1.19.32")

    def test_stopped_desired_state_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(manager, "RUNTIME", Path(tmp)), \
             patch.dict(os.environ, {"MIHOMO_SECRET": "fixture"}):
            core = manager.CoreManager()
            core.lifecycle(manager.Job("stop"))
            fresh = manager.CoreManager()
            self.assertEqual(fresh.snapshot()["desired"], "stopped")
            self.assertEqual(fresh.snapshot()["status"], "stopped")


class OfficialReleaseTests(unittest.TestCase):
    def test_fallback_digest_is_bound_to_exact_asset_row(self):
        digest = "sha256:" + "0" * 64
        url = "/MetaCubeX/mihomo/releases/download/v1.19.33/mihomo-linux-arm64-v1.19.33.gz"
        parser = releases.AssetDigests()
        parser.feed(f'''<li><a href="{url}">asset</a>
          <clipboard-copy value="{digest}"></clipboard-copy></li>
          <li><a href="{url}.rpm">different asset</a>
          <clipboard-copy value="sha256:{'1' * 64}"></clipboard-copy></li>''')
        self.assertEqual(parser.assets[url], {digest})
