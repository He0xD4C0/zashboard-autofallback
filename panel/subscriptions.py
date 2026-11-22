"""Stage subscriptions in pool order and reuse a validated source during an outage."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from panel.merge import _load_source


def valid(path, name):
    try:
        _load_source(str(path), name)
        return True
    except (OSError, SystemExit, TypeError, yaml.YAMLError):
        return False


def stage(pool, cache, target, pending, max_age):
    doc = yaml.safe_load(pool.read_text(encoding="utf-8"))
    entries = doc.get("subscriptions") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("subscription pool is empty")
    target.mkdir()
    pending.mkdir()
    seen = set()
    keys = []
    degraded = False
    for entry in entries:
        name, url = entry["name"], entry["url"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) or name in seen:
            raise ValueError("invalid or duplicate subscription name")
        if any(ch.isspace() for ch in url) or urlsplit(url).scheme not in {"https", "http", "file"}:
            raise ValueError("unsupported subscription URL")
        seen.add(name)
        key = name + "-" + hashlib.sha256(url.encode()).hexdigest() + ".yaml"
        keys.append(key)
        fresh = pending / key
        saved = cache / key
        output = target / f"{name}.yaml"
        command = ["curl", "-fsSL", "--proto", "=http,https,file", "--proto-redir", "=http,https",
                   "-m", "60", "-A", "clash-verge/v2.0.0", "-o", str(fresh), "--", url]
        for options in ([], ["--noproxy", "*"]):
            result = subprocess.run([command[0], *options, *command[1:]], stderr=subprocess.DEVNULL)
            if result.returncode == 0 and valid(fresh, name):
                shutil.copyfile(fresh, output)
                print(f"fetched {name}", file=sys.stderr)
                break
        else:
            fresh.unlink(missing_ok=True)
            if not saved.exists() or time.time() - saved.stat().st_mtime > max_age or not valid(saved, name):
                raise ValueError(f"{name}: unavailable and no fresh cache; keeping installed configuration")
            degraded = True
            shutil.copyfile(saved, output)
            print(f"degraded: {name}: reusing cached subscription", file=sys.stderr)
        print(output)
    (pending / "active").write_text("\n".join(keys) + "\n", encoding="utf-8")
    return degraded


def main():
    pool, cache, target, pending = map(Path, sys.argv[1:])
    try:
        stage(pool, cache, target, pending, int(os.environ.get("SUBSCRIPTION_CACHE_MAX_AGE", "86400")))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        message = str(exc) if type(exc) is ValueError and "keeping installed configuration" in str(exc) else "could not stage subscription pool"
        print(f"failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
