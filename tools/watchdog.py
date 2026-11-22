#!/usr/bin/env python3
"""对被选中的节点做短周期连通性巡检，失败就立刻触发组内重新选路。

内核自身的 fallback 健康检查是 300s 一次、且要连续失败 3 次才切换，最坏 15 分钟才
换节点。这个巡检每 5 分钟检查当前节点的三个不同目标，过半不通过时用同一批目标检查
候选节点，并切换到首个健康的成员。

    watchdog.py [--targets URL,URL] [--timeout 5000]

所有结果追加到 $DATA_DIR/run.log（一行 JSON），退出码恒为 0：它由调度器反复调用，
不该因为某个节点坏掉就把自己弄死。
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

CONFIG = os.environ.get(
    "MIHOMO_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "artifacts", "config.yaml"),
)
API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")
RUN_LOG = os.environ.get("RUN_LOG", "/data/run.log")
DEFAULT_HEALTH_URL = "http://www.gstatic.com/generate_204"
DEFAULT_TARGETS = {
    DEFAULT_HEALTH_URL: "204",
    "https://chatgpt.com": "200",
    "https://cp.cloudflare.com/generate_204": "204",
}
BUILTIN_PROXIES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE"}


def secret() -> str:
    if os.environ.get("MIHOMO_SECRET"):
        return os.environ["MIHOMO_SECRET"]
    with open(CONFIG, encoding="utf-8") as fh:
        return str((yaml.safe_load(fh) or {}).get("secret") or "")


def api(path: str, timeout: float, token: str, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def journal(entry: dict) -> None:
    entry.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    entry.setdefault("phase", "watchdog")
    line = json.dumps(entry, ensure_ascii=False)
    print("  " + line, flush=True)
    try:
        with open(RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def is_healthy(ok: int, total: int) -> bool:
    """严格过半通过才算健康：单站点被墙不足以换节点，整体不可用才换。"""
    return total > 0 and ok * 2 > total


def probe_node(node: str, targets, timeout_ms: int, token: str):
    """返回 (成功数, 明细)。"""
    node_path = "/proxies/" + urllib.parse.quote(node, safe="")
    detail = []
    for url, expected in targets.items():
        query = urllib.parse.urlencode({"url": url, "timeout": timeout_ms, "expected": expected})
        try:
            delay = api(node_path + "/delay?" + query, timeout_ms / 1000 + 10, token).get("delay")
            if not isinstance(delay, int) or delay <= 0:
                raise ValueError("missing delay in controller response")
            detail.append({"url": url, "delay": delay})
        except Exception as exc:
            detail.append({"url": url, "error": str(exc)[:80]})

    # Mihomo can return a delay even when the HTTP status fails `expected`.
    # The per-URL health flag is the result of that status check.
    measured = [result for result in detail if "delay" in result]
    if measured:
        error = "unexpected HTTP status or missing probe result"
        try:
            extra = api(node_path, 10, token).get("extra") or {}
        except Exception as exc:
            extra = {}
            error = str(exc)[:80]
        for result in measured:
            if not extra.get(result["url"], {}).get("alive"):
                del result["delay"]
                result["error"] = error
    return sum("delay" in result for result in detail), detail


def reselect(path, state, targets, group_target, timeout_ms, token) -> dict:
    previous = state["now"]
    group_url, group_expected = group_target
    candidate_targets = {**targets, group_url: group_expected}
    for candidate in state.get("all") or []:
        if candidate == previous or candidate in BUILTIN_PROXIES:
            continue
        _, detail = probe_node(candidate, candidate_targets, timeout_ms, token)
        passed = {result["url"] for result in detail if "delay" in result}
        if group_url not in passed or not is_healthy(len(passed.intersection(targets)), len(targets)):
            continue

        # Probing can change Mihomo's automatic selection, and a user can switch
        # nodes while the probes run. Check the current state before changing it.
        current = api(path, 10, token)
        selected = current.get("now")
        if selected == candidate:
            return {"action": "switched", "selected": selected}
        if current.get("fixed") != state.get("fixed") or current.get("testUrl") != state.get("testUrl"):
            return {"action": "selection-changed", "selected": selected}
        if candidate not in current.get("all", []):
            continue
        api(path, 10, token, method="PUT", body={"name": candidate})
        selected = api(path, 10, token).get("now")
        action = "switched" if selected == candidate else "reselect-unconfirmed"
        return {"action": action, "selected": selected}
    return {"action": "no-healthy-alternative"}


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="fast connectivity watchdog for selected nodes")
    ap.add_argument("--targets", default="", help="额外的探测地址，逗号分隔")
    ap.add_argument(
        "--force-targets",
        default="",
        help="替换巡检目标列表（演练用）",
    )
    ap.add_argument("--timeout", type=int, default=5000)
    args = ap.parse_args(argv[1:])

    token = secret()
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    groups = [
        g for g in (cfg.get("proxy-groups") or [])
        if g.get("type") in ("fallback", "url-test")
    ]
    extra = [t.strip() for t in args.targets.split(",") if t.strip()]
    forced = [t.strip() for t in args.force_targets.split(",") if t.strip()]

    problems = 0
    for group in groups:
        name = str(group.get("name"))
        path = "/proxies/" + urllib.parse.quote(name, safe="")
        try:
            state = api(path, 10, token)
            now = state.get("now")
        except Exception as exc:
            journal({"group": name, "status": "group-unreadable", "detail": str(exc)[:80]})
            problems += 1
            continue
        if not now or now in BUILTIN_PROXIES:
            continue
        group_url = state.get("testUrl") or group.get("url") or DEFAULT_HEALTH_URL
        group_expected = str(state.get("expectedStatus") or group.get("expected-status") or "*")
        statuses = {**DEFAULT_TARGETS, group_url: group_expected}
        urls = forced or [*list(dict.fromkeys((group_url, *DEFAULT_TARGETS)))[:3], *extra]
        targets = {url: statuses.get(url, "200-399") for url in urls}
        ok, detail = probe_node(now, targets, args.timeout, token)
        healthy = is_healthy(ok, len(targets))
        entry = {
            "group": name,
            "node": now,
            "ok": ok,
            "targets": len(targets),
            "status": "healthy" if healthy else "degraded",
        }
        if not healthy:
            problems += 1
            entry.update(previous=now, detail=detail)
            try:
                entry.update(reselect(
                    path, state, targets, (group_url, group_expected), args.timeout, token,
                ))
            except Exception as exc:
                entry["action"] = f"reselect-failed: {str(exc)[:60]}"
        journal(entry)
    journal({"status": "summary", "groups": len(groups), "problems": problems})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
