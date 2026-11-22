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
from tools.rules import atomic_text

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MUTATIONS = {"add", "delete", "enable", "disable", "apply"}


def checked_url(value, *, allow_file=False):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("unsupported subscription URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0:
            return parsed
        if allow_file and parsed.scheme == "file" and parsed.path:
            return parsed
    except ValueError:
        pass
    raise ValueError("unsupported subscription URL")


def read_pool(pool, *, missing_ok=False):
    try:
        doc = yaml.safe_load(pool.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
    except yaml.YAMLError:
        raise ValueError("subscription pool is not valid YAML") from None
    entries = doc.get("subscriptions") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise ValueError("invalid subscription pool")
    seen = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
            raise ValueError("invalid or duplicate subscription name")
        checked_url(entry.get("url"), allow_file=True)
        if type(entry.get("enabled", True)) is not bool:
            raise ValueError("subscription enabled must be a boolean")
        seen.add(name)
    return entries


def pending_marker(pool):
    return pool.with_name(".subscriptions-pending")


def cache_key(entry):
    return entry["name"] + "-" + hashlib.sha256(entry["url"].encode()).hexdigest() + ".yaml"


def listing(pool, cache):
    entries = read_pool(pool, missing_ok=True)
    return {
        "subscriptions": [{
            "name": entry["name"],
            "enabled": entry.get("enabled", True),
            "host": checked_url(entry["url"], allow_file=True).hostname or "file",
            "cached": (cache / cache_key(entry)).is_file(),
        } for entry in entries],
        "pending": pending_marker(pool).exists(),
    }


def mutate(pool, cache, body):
    action, name = body["action"], body.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid subscription name")
    entries = read_pool(pool, missing_ok=True)
    before = [cache_key(entry) for entry in entries if entry.get("enabled", True)]
    existing = next((entry for entry in entries if entry["name"] == name), None)
    if action == "add":
        if existing is not None:
            raise ValueError("subscription name already exists")
        url, enabled = body.get("url"), body.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("subscription enabled must be a boolean")
        checked_url(url)
        entries.append({"name": name, "url": url, "enabled": enabled})
    elif existing is None:
        raise ValueError("subscription not found")
    elif action == "delete":
        entries.remove(existing)
    else:
        existing["enabled"] = action == "enable"
    after = [cache_key(entry) for entry in entries if entry.get("enabled", True)]
    pool.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if before != after:
        pending_marker(pool).touch(0o600)
    atomic_text(pool, yaml.safe_dump({"subscriptions": entries}, allow_unicode=True, sort_keys=False))
    if action == "delete":
        (cache / cache_key(existing)).unlink(missing_ok=True)


def valid(path, name):
    try:
        _load_source(str(path), name)
        return True
    except (OSError, SystemExit, TypeError, yaml.YAMLError):
        return False


def stage(pool, cache, target, pending, max_age):
    entries = read_pool(pool)
    target.mkdir()
    pending.mkdir()
    degraded = False
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        name, url = entry["name"], entry["url"]
        key = cache_key(entry)
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
    (pending / "retained").write_text("\n".join(map(cache_key, entries)) + "\n", encoding="utf-8")
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
