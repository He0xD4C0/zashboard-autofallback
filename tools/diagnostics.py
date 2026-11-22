"""Publish the panel's read-only snapshot without exposing configuration or raw logs."""

import fcntl
import ipaddress
import json
import math
import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from rules import atomic_text

STATUSES = {
    "healthy", "degraded", "group-unreadable", "summary",
}
ACTIONS = {
    "switched", "selection-changed", "reselect-unconfirmed", "no-healthy-alternative",
}


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def safe_label(value):
    if not isinstance(value, str) or "://" in value:
        return ""
    return " ".join(value.split())[:160]


def timestamp(value):
    try:
        return datetime.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def read_events(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            recent = deque(stream, maxlen=32)
    except OSError:
        return []
    events = []
    for line in recent:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("phase") != "watchdog" or entry.get("status") not in STATUSES:
            continue
        events.append({
            "at": timestamp(entry.get("ts")), "status": entry["status"],
            "action": entry["action"] if entry.get("action") in ACTIONS else None,
            **{key: safe_label(entry.get(key)) for key in ("group", "node", "previous", "selected")},
            **{key: value for key in ("ok", "targets", "groups", "problems")
               if isinstance(value := entry.get(key), int)},
        })
    return events[-14:][::-1]


def safe_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def make_snapshot(scores, purity, events, refresh):
    table = scores.get("nodes") or {}
    measured = purity.get("nodes") or {}
    nodes = []
    for name in dict.fromkeys((*table, *measured)):
        info = table.get(name, {})
        sample = measured.get(name, {})
        if not isinstance(info, dict) or not isinstance(sample, dict):
            continue
        scored = info.get("purity") or {}
        classification = sample or scored
        nodes.append({
            "name": safe_label(name), "score": number(info.get("final")), "base": number(info.get("overall")),
            "region": safe_label(info.get("region") or sample.get("claimed")),
            "country": safe_label(classification.get("country_code")),
            "ip": safe_ip(classification.get("ip")),
            "purity": number(sample.get("purity", scored.get("score"))),
            "regionMatch": classification.get("region_match") if isinstance(classification.get("region_match"), bool) else None,
            "platforms": {
                safe_label(name): {key: number(cell.get(key)) for key in ("score", "availability", "median_ms", "jitter_ms")}
                for name, cell in (info.get("platforms") or {}).items() if isinstance(cell, dict)
            },
        })
    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(),
        "scoresAt": timestamp(scores.get("generated")),
        "purityAt": timestamp(purity.get("generated")),
        "scoresMaxAge": int(os.environ.get("MIHOMO_SCORES_MAX_AGE", "43200")),
        "purityMaxAge": int(os.environ.get("MIHOMO_PURITY_MAX_AGE", "43200")),
        "nodes": sorted(nodes, key=lambda n: n["score"] or 0, reverse=True),
        "events": events,
        "refresh": refresh,
    }


def main():
    directory = Path(os.environ["DIAGNOSTICS_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    data = Path(os.environ.get("DATA_DIR", "/data"))
    artifacts = data / "artifacts"
    path = directory / "state.json"
    with (directory / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        refresh = read_json(path).get("refresh")
        if len(sys.argv) > 1:
            refresh = {"status": sys.argv[1], "at": datetime.now(timezone.utc).isoformat()}
        snapshot = make_snapshot(
            read_json(Path(os.environ.get("MIHOMO_SCORES", artifacts / "scores.json"))),
            read_json(Path(os.environ.get("MIHOMO_PURITY", artifacts / "purity.json"))),
            read_events(Path(os.environ.get("RUN_LOG", data / "run.log"))),
            refresh,
        )
        atomic_text(path, json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
        path.chmod(0o644)


if __name__ == "__main__":
    main()
