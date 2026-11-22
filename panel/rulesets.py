"""Manage a validated, volume-backed rule source and pinned rule provider files."""

import fcntl
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
from urllib.parse import quote, urlsplit

import yaml

from panel import merge
from panel.runtime import API, CACHE, CONFIG, DATA, ROOT, RUNTIME
from tools import reload, rules

DIRECTORY = DATA / "rules"
ACTIVE = DIRECTORY / "active.yaml"
JOURNAL = DIRECTORY / ".update.json"
SEED = ROOT / "rules/henrychiao.yaml"
MANIFEST_LIMIT = 512 * 1024
PROVIDER_LIMIT = 16 * 1024**2


def https_source(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 1024 or any(
        char.isspace() or ord(char) < 32 for char in url
    ):
        raise ValueError("use a public HTTPS ruleset URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.port not in {443, None}
            or parsed.username is not None or parsed.fragment
            or not parsed.hostname or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)):
        raise ValueError("use a public HTTPS ruleset URL without credentials or fragments")
    return parsed


def public_addresses(host):
    addresses = {record[4][0] for record in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(
        not address.is_global or address.is_multicast for address in map(ipaddress.ip_address, addresses)
    ):
        raise ValueError("ruleset sources must resolve to public addresses")
    return sorted(addresses, key=lambda address: (":" in address, address))


def github_raw(url):
    parsed = https_source(url)
    if parsed.hostname == "github.com":
        path = re.fullmatch(r"/([^/]+/[^/]+)/(?:raw|blob)/(.+)", parsed.path)
        if path:
            url = f"https://raw.githubusercontent.com/{path[1]}/{path[2]}"
            if parsed.query:
                url += "?" + parsed.query
    return url


def get(url, target, limit):
    url = github_raw(url)
    parsed = https_source(url)
    try:
        fetch(url, target, limit)
    except (OSError, RuntimeError):
        if parsed.hostname != "raw.githubusercontent.com":
            raise
        path = re.fullmatch(r"/([\w.-]+/[\w.-]+)/(?:refs/(?:heads|tags)/)?([^/]+)/(.+)", parsed.path)
        if not path:
            raise
        repository, ref, filename = path.groups()
        ref = quote(ref, safe="")
        try:
            fetch(f"https://api.github.com/repos/{repository}/contents/{filename}?ref={ref}", target, limit)
        except (OSError, RuntimeError):
            fetch(f"https://cdn.jsdelivr.net/gh/{repository}@{ref}/{filename}", target, limit)


def fetch(url, target, limit):
    for _ in range(5):
        parsed = https_source(url)
        host = parsed.hostname
        for address in public_addresses(host)[:2]:
            resolved = f"[{address}]" if ":" in address else address
            headers = ["--header", "Accept: application/vnd.github.raw+json"] if host == "api.github.com" else []
            timeout = "8" if host == "raw.githubusercontent.com" else "20"
            command = [
                "curl", "-q", "--silent", "--noproxy", "*", "--proto", "=https",
                "--user-agent", "zashboard-autofallback", *headers,
                "--resolve", f"{host}:443:{resolved}", "--globoff",
                "--connect-timeout", "4", "--max-time", timeout, "--max-filesize", str(limit),
                "--output", str(target), "--write-out", "%{response_code}\n%{redirect_url}\n", "--", url,
            ]
            response = subprocess.run(command, capture_output=True, text=True, timeout=25)
            if response.returncode == 0:
                break
        else:
            raise RuntimeError("could not download rules from the configured HTTPS source")
        code, redirect = response.stdout.split("\n", 1)
        if code == "200" and 0 < target.stat().st_size <= limit:
            return
        if code in {"301", "302", "303", "307", "308"} and redirect.strip():
            url = github_raw(redirect.strip())
            continue
        raise RuntimeError(f"ruleset download failed (HTTP {code})")
    raise RuntimeError("too many ruleset redirects")


def sanitize_document(path, url):
    try:
        doc = rules.read_config(path)
    except yaml.YAMLError:
        raise ValueError("ruleset is not valid YAML") from None
    if not isinstance(doc, dict):
        raise ValueError("ruleset must be a YAML mapping")
    entries = doc.get("rules")
    providers = doc.get("rule-providers", {})
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 5000
            or not all(isinstance(item, str) and 1 <= len(item) <= 2048 for item in entries)
            or not isinstance(providers, dict) or len(providers) > 100):
        raise ValueError("ruleset must contain a rules list and optional rule-providers")
    if not merge._map_rules(entries):
        raise ValueError("ruleset must have entries besides MATCH or FINAL")
    required = rules.required_rules({"rules": entries})
    if required - providers.keys():
        raise ValueError("ruleset references undefined rule-providers")
    output = {}
    for name in sorted(required):
        spec = providers[name]
        if not re.fullmatch(r"[\w.-]{1,80}", name) or name in {".", ".."} or not isinstance(spec, dict):
            raise ValueError("invalid rule provider definition")
        content_format = spec.get("format", "yaml")
        behavior = spec.get("behavior")
        source = spec.get("url")
        if (spec.get("type") != "http" or behavior not in ("domain", "ipcidr", "classical")
                or content_format not in ("yaml", "text", "mrs")):
            raise ValueError(f"unsupported rule provider: {name}")
        https_source(source)
        output[name] = {"type": "http", "behavior": behavior, "format": content_format, "url": source}
    return {"source": url, "rules": entries, "rule-providers": output}


def fetch_provider(name, spec, directory, _cache, _use_existing):
    try:
        get(spec["url"], directory / name, PROVIDER_LIMIT)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ValueError(f"rule provider download failed: {name}") from exc


def encode(doc):
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def seed():
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not ACTIVE.exists():
        rules.atomic_text(ACTIVE, SEED.read_text(encoding="utf-8"))


def pin_installed(config):
    doc = rules.read_config(ACTIVE)
    providers = rules.read_config(config).get("rule-providers") or {}
    pinned = {name: spec for name, spec in providers.items() if spec.get("type") == "file"}
    if pinned != doc.get("pinned-providers", {}):
        doc["pinned-providers"] = pinned
        rules.atomic_text(ACTIVE, encode(doc))


def recover():
    if JOURNAL.exists():
        record = json.loads(JOURNAL.read_text())
        committed = ACTIVE.exists() and hashlib.sha256(ACTIVE.read_bytes()).hexdigest() == record["next"]
        if not committed:
            rules.atomic_text(CONFIG, record["config"])
        JOURNAL.unlink()


def stage_update(scratch, url):
    remote = scratch / "download.yaml"
    staged = scratch / "ruleset.yaml"
    candidate = scratch / CONFIG.name
    get(url, remote, MANIFEST_LIMIT)
    doc = sanitize_document(remote, url)
    rules.atomic_text(staged, encode(doc))
    config = rules.read_config(CONFIG)
    subscription_rules = merge.installed_subscription_rules(config, ACTIVE)
    merge.apply_rules(config, subscription_rules, staged)
    rules.atomic_text(candidate, encode(config))
    rules.prepare(candidate, CACHE, fetch=fetch_provider, force=True)
    doc["pinned-providers"] = rules.read_config(candidate).get("rule-providers") or {}
    return candidate, encode(doc)


def commit(candidate, new_rules, secret):
    with (RUNTIME / ".controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = CONFIG.read_text(encoding="utf-8")
        record = {"config": previous, "next": hashlib.sha256(new_rules.encode()).hexdigest()}
        rules.atomic_text(JOURNAL, json.dumps(record))
        try:
            if reload.reload(API, secret, candidate, retries=1, delay=0):
                raise RuntimeError("Mihomo rejected the ruleset")
            if rules.live(API, secret, rules.required_rules(rules.read_config(candidate))):
                raise RuntimeError("updated rule providers are unavailable")
            rules.atomic_text(CONFIG, candidate.read_text(encoding="utf-8"))
            rules.atomic_text(ACTIVE, new_rules)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            recover()
            if reload.reload(API, secret, CONFIG, retries=3, delay=1):
                raise RuntimeError("ruleset rollback needs Mihomo restart") from None
            raise
        JOURNAL.unlink()
