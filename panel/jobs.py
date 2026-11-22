"""Prepare, validate and refresh the managed Mihomo configuration."""

import fcntl
import gzip
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import yaml

from panel import make_probe_config, merge, rulesets, subscriptions
from panel.runtime import (
    API, ARTIFACTS, CACHE, CONFIG, DATA, PROBE_API, PROBE_CONFIG, PROBE_DIR, ROOT, RUNTIME,
    atomic_copy, controller, kernel, terminate, wait_ready,
)
from tools import reload, rules, validate


def binary():
    return Path(os.environ["MIHOMO_BINARY"])


def prepare_dirs():
    for path in (ARTIFACTS, CACHE, PROBE_DIR, DATA / "archive", DATA / "subscription-cache"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("geoip.metadb", "geosite.dat"):
        source = DATA / "assets" / name
        target = CACHE / name
        if source.is_file() and source.stat().st_size:
            if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
                atomic_copy(source, target, 0o644)
        if not target.is_file() or not target.stat().st_size:
            raise RuntimeError(f"missing {name}; run tools/seed_dev.sh")


def check(config, home):
    result = subprocess.run(kernel(binary(), home, config, validate=True), capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f"Mihomo rejected {config.name}; check rule providers and configuration")


def prepare_main(path):
    rules.prepare(path, CACHE, use_existing=True)
    check(path, CACHE)


def build_probe(src, dst):
    doc = make_probe_config.build(rules.read_config(src), 7899, os.environ["MIHOMO_SECRET"], "127.0.0.1:9190")
    rules.atomic_text(dst, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
    check(dst, PROBE_DIR)


def stage_sources(scratch):
    pending = scratch / "pending"
    captured = io.StringIO()
    with redirect_stdout(captured):
        degraded = subscriptions.stage(
            DATA / "secrets/subscriptions.yaml", DATA / "subscription-cache", scratch / "sources",
            pending, int(os.environ.get("SUBSCRIPTION_CACHE_MAX_AGE", "86400")),
        )
    return captured.getvalue().splitlines(), bool(degraded)


def save_subscriptions(scratch):
    pending = scratch / "pending"
    cache = DATA / "subscription-cache"
    active = set((pending / "active").read_text().splitlines())
    for source in pending.glob("*.yaml"):
        source.chmod(0o600)
        os.replace(source, cache / source.name)
    for saved in cache.glob("*.yaml"):
        if saved.name not in active:
            saved.unlink()


def install_config(candidate):
    atomic_copy(candidate, CONFIG)
    (ARTIFACTS / ".config-installed").touch(0o600)


def bootstrap(scratch):
    rulesets.recover()
    rulesets.seed()
    candidate = scratch / CONFIG.name
    probe = scratch / PROBE_CONFIG.name
    if (ARTIFACTS / ".config-installed").is_file() and CONFIG.is_file():
        try:
            atomic_copy(CONFIG, candidate)
            merge.restore(candidate)
            prepare_main(candidate)
            build_probe(candidate, probe)
        except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError, yaml.YAMLError, SystemExit):
            print("Installed configuration failed validation; rebuilding from subscriptions.", file=sys.stderr)
        else:
            atomic_copy(probe, PROBE_CONFIG)
            install_config(candidate)
            rulesets.pin_installed(candidate)
            return
    sources, _ = stage_sources(scratch)
    merge.main(str(candidate), sources)
    prepare_main(candidate)
    build_probe(candidate, probe)
    atomic_copy(probe, PROBE_CONFIG)
    install_config(candidate)
    rulesets.pin_installed(candidate)
    save_subscriptions(scratch)


def reload_config(candidate):
    with (RUNTIME / ".controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if reload.reload(API, os.environ["MIHOMO_SECRET"], candidate, retries=3, delay=1):
            raise RuntimeError("Mihomo rejected reload; keeping the previous configuration")
        install_config(candidate)


def warm():
    for group in rules.read_config(CONFIG).get("proxy-groups") or []:
        if group["type"] not in ("fallback", "url-test", "load-balance"):
            continue
        name = urllib.parse.quote(group["name"], safe="")
        query = urllib.parse.urlencode({"url": group.get("url") or "http://www.gstatic.com/generate_204",
                                       "timeout": 6000})
        try:
            controller(f"/group/{name}/delay?{query}", timeout=180)
        except (OSError, ValueError):
            print(f"Group probe unavailable: {group['name']}", file=sys.stderr)


def archive(path, name):
    directory = DATA / "archive"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = directory / f"{name}-{stamp}.json.gz"
    with tempfile.NamedTemporaryFile(dir=directory) as temporary:
        with gzip.open(temporary.name, "wb") as compressed, path.open("rb") as stream:
            shutil.copyfileobj(stream, compressed)
        atomic_copy(Path(temporary.name), destination)
    for expired in sorted(directory.glob(f"{name}-*.json.gz"))[:-7]:
        expired.unlink()


def script(path, *args):
    return subprocess.run([sys.executable, str(ROOT / path), *map(str, args)], check=False).returncode


def score():
    scores = Path(os.environ.get("MIHOMO_SCORES", ARTIFACTS / "scores.json"))
    result = script("panel/score.py", "--platforms", os.environ.get("SCORE_PLATFORMS", "all"),
                    "--samples", os.environ.get("SCORE_SAMPLES", "2"), "--prefer",
                    os.environ.get("PREFER_REGION", "jp"), "--prefer-bonus",
                    os.environ.get("PREFER_BONUS", "5"), "--json", scores, "--top", "0")
    if result:
        raise RuntimeError("node scoring failed")
    archive(scores, "scores")


def purity():
    path = Path(os.environ.get("MIHOMO_PURITY", ARTIFACTS / "purity.json"))
    process = subprocess.Popen(kernel(binary(), PROBE_DIR, PROBE_CONFIG))
    try:
        wait_ready(process, PROBE_API)
        result = script("tools/purity.py", "--pace", os.environ.get("PURITY_PACE", "1.5"),
                        "--json", path, "--top", "5")
        if result or not path.is_file():
            raise RuntimeError("exit IP and purity scan failed")
        archive(path, "purity")
    finally:
        terminate(process, timeout=3)


def refresh(scratch, skip_score):
    candidate = scratch / CONFIG.name
    probe = scratch / PROBE_CONFIG.name
    sources, degraded = stage_sources(scratch)
    merge.main(str(candidate), sources)
    prepare_main(candidate)
    build_probe(candidate, probe)
    reload_config(candidate)
    atomic_copy(probe, PROBE_CONFIG)
    rulesets.pin_installed(candidate)
    save_subscriptions(scratch)
    warm()
    if skip_score:
        return degraded
    if os.environ.get("PURITY_ENABLED", "1") == "1":
        try:
            purity()
        except (OSError, RuntimeError):
            print("Purity probe unavailable; scoring continues.", file=sys.stderr)
            degraded = True
    score()
    merge.main(str(candidate), sources)
    prepare_main(candidate)
    reload_config(candidate)
    warm()
    return degraded


def preflight():
    check(CONFIG, CACHE)
    check(PROBE_CONFIG, PROBE_DIR)
    config = rules.read_config(CONFIG)
    providers = config.get("rule-providers") or {}
    rules.validate_bundle(CACHE, providers, rules.required_rules(config))


def validate_configs(check_live):
    check(CONFIG, CACHE)
    check(PROBE_CONFIG, PROBE_DIR)
    config = rules.read_config(CONFIG)
    required = rules.required_rules(config)
    rules.validate_bundle(CACHE, config.get("rule-providers") or {}, required)
    if check_live and rules.live(API, config["secret"], required):
        raise RuntimeError("main Mihomo has unloaded rule sets")
    if validate.validate_tree(str(DATA)):
        raise RuntimeError("data contract validation failed")


def main():
    operation = sys.argv[1]
    result = "ok"
    try:
        if operation in {"bootstrap", "refresh"}:
            prepare_dirs()
        with tempfile.TemporaryDirectory(prefix=".job-", dir=ARTIFACTS) as directory:
            scratch = Path(directory)
            if operation == "bootstrap":
                bootstrap(scratch)
            elif operation == "refresh":
                if refresh(scratch, "--skip-score" in sys.argv[2:]):
                    result = "degraded"
            elif operation == "score":
                score()
            elif operation == "purity":
                purity()
            elif operation == "watchdog":
                with (RUNTIME / ".controller.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if script("tools/watchdog.py"):
                        raise RuntimeError("watchdog failed")
            elif operation == "rules":
                candidate, rule_data = rulesets.stage_update(scratch, os.environ["PANEL_RULESET_SOURCE"])
                check(candidate, CACHE)
                rulesets.commit(candidate, rule_data, os.environ["MIHOMO_SECRET"])
            elif operation == "preflight":
                preflight()
            elif operation == "validate":
                validate_configs("--live" in sys.argv[2:])
            else:
                raise RuntimeError("unknown panel job")
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError, yaml.YAMLError, SystemExit) as exc:
        print(f"{operation} failed: {exc}", file=sys.stderr)
        result = "failed"
        if operation == "refresh" and os.environ.get("KEEP_FAILURE") == "1":
            failure = DATA / "last-failure" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            failure.mkdir(mode=0o700, parents=True)
            rules.atomic_text(failure / "status", "refresh failed\n")
    finally:
        if operation in {"refresh", "bootstrap", "score", "purity", "watchdog"}:
            args = [result] if operation == "refresh" or (operation == "bootstrap" and result == "failed") else []
            script("tools/diagnostics.py", *args)
    return int(result == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
