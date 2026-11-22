"""Prepare local rule bundles and verify the rule sets loaded by Mihomo."""

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml


def required_rules(config):
    fields = [*(config.get("rules") or [])]
    for rules in (config.get("sub-rules") or {}).values():
        fields.extend(rules)
    names = {name.strip() for rule in fields for name in re.findall(r"(?:^|\()RULE-SET,([^,)]+)", rule)}
    dns = config.get("dns") or {}
    for item in dns.get("fake-ip-filter") or []:
        if item.startswith("rule-set:"):
            names.update(name.strip() for name in item.removeprefix("rule-set:").split(","))
    return names


def read_config(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def live(api, token, expected):
    request = urllib.request.Request(api.rstrip("/") + "/providers/rules",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        providers = json.load(response).get("providers") or {}
    return sorted(name for name in expected if (providers.get(name) or {}).get("ruleCount", 0) <= 0)


def validate_bundle(cache, specs, expected):
    if not expected:
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_hex(24)
    probe = {
        "external-controller": f"127.0.0.1:{port}", "secret": token,
        "rule-providers": {name: specs[name] for name in sorted(expected)},
        "rules": [*(f"RULE-SET,{name},DIRECT" for name in sorted(expected)), "MATCH,DIRECT"],
        "dns": {"enable": False},
    }
    with tempfile.TemporaryDirectory(dir=cache) as temp:
        home = Path(temp)
        (home / "rule-providers").symlink_to(cache / "rule-providers", target_is_directory=True)
        path = home / "rules.yaml"
        path.write_text(yaml.safe_dump(probe), encoding="utf-8")
        with subprocess.Popen([os.environ.get("MIHOMO_BINARY", "mihomo"), "-d", str(home), "-f", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            missing = sorted(expected)
            try:
                for _ in range(40):
                    if process.poll() is not None:
                        break
                    try:
                        missing = live(f"http://127.0.0.1:{port}", token, expected)
                        if not missing:
                            return
                    except (OSError, KeyError, ValueError):
                        pass
                    time.sleep(0.15)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            raise ValueError("could not load rule sets: " + ", ".join(missing))


def atomic_text(path, content):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download(name, spec, directory, cache, use_existing):
    source = cache / spec.get("path", f"rule-providers/{name}.mrs")
    output = directory / name
    if use_existing and source.is_file():
        output.write_bytes(source.read_bytes())
        return
    url = re.sub(r"^https://github\.com/([^/]+/[^/]+)/raw/", r"https://raw.githubusercontent.com/\1/", spec["url"])
    command = ["curl", "-fsSL", "--proto", "=https,http", "--proto-redir", "=https,http", "--max-time", "20",
               "-o", str(output), "--", url]
    for options in ([], ["--noproxy", "*"]):
        if subprocess.run([command[0], *options, *command[1:]], stderr=subprocess.DEVNULL).returncode == 0:
            return
    raise ValueError(f"rule provider download failed: {name}")


def prepare(path, cache, use_existing=False, *, fetch=None, force=False):
    fetch = fetch or download
    config = read_config(path)
    required = required_rules(config)
    providers = config.get("rule-providers") or {}
    missing = required - providers.keys()
    if missing:
        raise ValueError("rule definitions missing: " + ", ".join(sorted(missing)))
    remote = {name: providers[name] for name in sorted(required) if providers[name].get("type") == "http"}
    if not remote:
        pinned = {name: providers[name] for name in sorted(required)}
        validate_bundle(cache, pinned, required)
        if pinned != providers:
            config["rule-providers"] = pinned
            atomic_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        return
    if any(not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."} for name in remote):
        raise ValueError("invalid rule provider name")
    base = cache / "rule-providers"
    base.mkdir(parents=True, exist_ok=True)
    sources = {name: {"url": spec["url"], "behavior": spec["behavior"], "format": spec.get("format", "yaml")}
               for name, spec in remote.items()}
    identity = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    manifest = base / f"{identity}.json"
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}

    local = saved.get("providers") or {}
    if local:
        try:
            validate_bundle(cache, {**providers, **local}, required)
        except (OSError, KeyError, ValueError):
            local = {}
    if force or not local or (not use_existing and time.time() - saved.get("checked", 0) >= 86400):
        try:
            with tempfile.TemporaryDirectory(dir=base, prefix=".stage-") as staging:
                stage = Path(staging)
                with ThreadPoolExecutor(max_workers=8) as workers:
                    list(workers.map(lambda item: fetch(*item, stage, cache, use_existing), remote.items()))
                digest = hashlib.sha256()
                for name in remote:
                    digest.update(name.encode())
                    digest.update(hashlib.sha256((stage / name).read_bytes()).digest())
                directory = base / digest.hexdigest()
                directory.mkdir(exist_ok=True)
                candidate = {}
                for name, spec in remote.items():
                    destination = directory / name
                    source = stage / name
                    if not destination.exists() or destination.read_bytes() != source.read_bytes():
                        os.replace(source, destination)
                    candidate[name] = {"type": "file", "path": str(destination.relative_to(cache)),
                                       "behavior": spec["behavior"], "format": spec.get("format", "yaml")}
                validate_bundle(cache, {**providers, **candidate}, required)
                atomic_text(manifest, json.dumps({"checked": time.time(), "providers": candidate}))
                local = candidate
        except (OSError, KeyError, ValueError) as exc:
            if force or not local:
                raise
            print(f"degraded: {exc}; using last verified rules", file=sys.stderr)
    providers.update(local)
    # The unused upstream declarations would otherwise be fetched by the active kernel.
    config["rule-providers"] = {name: spec for name, spec in providers.items() if name in required}
    atomic_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def main():
    mode, config_path, *args = sys.argv[1:]
    try:
        path = Path(config_path)
        if mode == "prepare":
            prepare(path, Path(args[0]).resolve(), len(args) > 1 and args[1] == "--use-existing")
        elif mode == "seed":
            cache = Path(args[0]).resolve()
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                candidate = Path(temporary) / "rules.yaml"
                candidate.write_bytes(path.read_bytes())
                prepare(candidate, cache, len(args) > 1 and args[1] == "--use-existing")
                print(f"verified {len(required_rules(read_config(candidate)))} required rule sets")
        elif mode == "check":
            config = read_config(path)
            validate_bundle(Path(args[0]).resolve(), config.get("rule-providers") or {}, required_rules(config))
        elif mode == "live":
            config = read_config(path)
            missing = live(os.environ.get("MIHOMO_API", "http://127.0.0.1:9090"), config["secret"], required_rules(config))
            if missing:
                raise ValueError("empty or missing rule sets: " + ", ".join(missing))
        else:
            raise ValueError("unknown rules command")
    except (OSError, KeyError, ValueError, yaml.YAMLError) as exc:
        print(f"rules {mode}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
