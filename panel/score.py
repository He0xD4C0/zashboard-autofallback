#!/usr/bin/env python3
"""Score every node in the pool on the platforms we cross the wall for.

mihomo's own health check is one boolean against one URL, so it cannot express
"fast, but the site resets this exit", "passes gstatic yet flaps", or "fine for
youtube, blocked by chatgpt". This runs repeated probes through the controller
against a set of commonly blocked platforms and turns them into 0-100 scores:

    availability  成功探测比例                      weight 0.50
    latency       中位 RTT，300ms 满分、2000ms 归零    weight 0.35
    stability     1 - 标准差/中位（抖动惩罚）          weight 0.15

Per node: overall is the weighted sum of platform scores and, when available,
IP purity (0-100). A preferred region adds a small bonus (`--prefer`, default 日本) so an equally good
domestic-side exit wins a tie without overriding quality. `final` is that
ranking index and may exceed 100 - it is a sort key, not a percentage.

merge.py reads scores.json (while it is younger than 12h) and orders 出口 by
`final`, AI出口 by the `ai` platform score, and the optional site group by `site`.

Usage:
    score.py [--platforms all|google,ai,...] [--samples 2] [--threads 24]
             [--timeout 6000] [--prefer jp] [--prefer-bonus 5]
             [--json <config dir>/scores.json] [--top 20]

Platforms with weights (sum 1.0):
    gstatic .14  google .12  youtube .12  x .10  wikipedia .08  github .08
    reddit .08  telegram .07  instagram .07  discord .06  ai .04
    site .04（可选：仅当 MIHOMO_SITE_HEALTH_URL 给出时才参与评分）
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yaml

CONFIG = os.environ.get(
    "MIHOMO_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
)
CONTROLLER = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")

PLATFORMS = {
    "gstatic": ("https://www.gstatic.com/generate_204", 0.14),
    "google": ("https://www.google.com/generate_204", 0.12),
    "youtube": ("https://www.youtube.com/generate_204", 0.12),
    "x": ("https://x.com/", 0.10),
    "wikipedia": ("https://www.wikipedia.org/", 0.08),
    "github": ("https://github.com/", 0.08),
    "reddit": ("https://www.reddit.com/", 0.08),
    "telegram": ("https://web.telegram.org/", 0.07),
    "instagram": ("https://www.instagram.com/", 0.07),
    "discord": ("https://discord.com/", 0.06),
    "ai": ("https://chatgpt.com", 0.04),
}

# 定向站点平台：仓库不内置供应商地址，由 MIHOMO_SITE_HEALTH_URL 提供；
# 未提供时该平台不参与评分（写入快照时按当次平台集合重新归一权重）。
SITE_PROBE_URL = os.environ.get("MIHOMO_SITE_HEALTH_URL", "").strip()
if SITE_PROBE_URL:
    PLATFORMS["site"] = (SITE_PROBE_URL, 0.04)

EXPECTED_STATUS = {"gstatic": "204", "google": "204", "ai": "200",
                   "site": os.environ.get("MIHOMO_SITE_HEALTH_STATUS", "200")}

# IP 纯净度（tools/purity.py 产出）：住宅/移动 100、机房 55、被判定代理 20，
# 地区虚标再扣 25。它作为一个独立维度参与总分，权重默认 0.10，其余平台按比例让出。
PURITY_FILE = os.environ.get(
    "MIHOMO_PURITY", os.path.join(os.path.dirname(CONFIG), "purity.json")
)
PURITY_WEIGHT = float(os.environ.get("MIHOMO_PURITY_WEIGHT", "0.10"))
PURITY_MAX_AGE = int(os.environ.get("MIHOMO_PURITY_MAX_AGE", str(12 * 3600)))
# 没测到纯净度的节点按"未知偏保守"计分：不测不等于干净，避免未测节点靠缺数据占便宜
PURITY_MISSING = float(os.environ.get("MIHOMO_PURITY_MISSING", "40"))
# `ai` and `site` are also the per-group keys used by merge.py; the rest only
# contribute to the overall score.

# region code -> name fragments. Node names come from several subscriptions, so
# both the flag emoji and the Chinese/English wording are matched.
REGIONS = {
    "jp": ("🇯🇵", "日本", "japan"),
    "sg": ("🇸🇬", "新加坡", "singapore"),
    "us": ("🇺🇸", "美国", "united states", "洛杉矶", "los angeles"),
    "hk": ("🇭🇰", "香港", "hong kong"),
    "tw": ("🇹🇼", "台湾", "taiwan"),
    "gb": ("🇬🇧", "英国", "united kingdom", "伦敦"),
    "de": ("🇩🇪", "德国", "germany"),
    "nl": ("🇳🇱", "荷兰", "netherlands"),
    "my": ("🇲🇾", "马来西亚", "malaysia"),
    "kr": ("🇰🇷", "韩国", "korea"),
    "cn": ("🇨🇳", "中国"),
}
DEFAULT_PREFER = ("jp",)

WEIGHT_AVAIL, WEIGHT_LAT, WEIGHT_STAB = 0.5, 0.35, 0.15
LAT_BEST, LAT_WORST = 300.0, 2000.0

GROUP_TYPES = {"Selector", "Fallback", "URLTest", "LoadBalance", "Relay"}
BUILTIN = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE", "GLOBAL"}
BUILTIN_TYPES = {"Direct", "Reject", "RejectDrop", "Pass", "Compatible"}


def _secret() -> str:
    with open(CONFIG, encoding="utf-8") as fh:
        return str((yaml.safe_load(fh) or {}).get("secret") or "")


def _api(path: str, timeout: float, secret: str):
    req = urllib.request.Request(CONTROLLER + path, headers={"Authorization": "Bearer " + secret})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def nodes(secret: str) -> list:
    data = _api("/proxies", 10, secret)["proxies"]
    return [
        name
        for name, info in data.items()
        if info.get("type") not in GROUP_TYPES
        and info.get("type") not in BUILTIN_TYPES
        and name not in BUILTIN
    ]


def region_of(name: str) -> str:
    lowered = name.lower()
    for code, fragments in REGIONS.items():
        if any(f.lower() in lowered for f in fragments):
            return code
    return "unknown"


def probe(name: str, url: str, timeout_ms: int, secret: str, expected: str = "200-399"):
    """One real HTTPS request through the node; returns milliseconds or None."""
    query = urllib.parse.urlencode({"timeout": timeout_ms, "url": url, "expected": expected})
    path = "/proxies/" + urllib.parse.quote(name, safe="")
    try:
        delay = float(_api(path + "/delay?" + query, timeout_ms / 1000 + 10, secret)["delay"])
        status = (_api(path, 10, secret).get("extra") or {}).get(url, {})
        return delay if delay > 0 and status.get("alive") is True else None
    except (OSError, TypeError, ValueError, KeyError):
        return None


def latency_score(ms):
    if ms is None:
        return 0.0
    if ms <= LAT_BEST:
        return 1.0
    if ms >= LAT_WORST:
        return 0.0
    return (LAT_WORST - ms) / (LAT_WORST - LAT_BEST)


def stability_score(latencies):
    if not latencies:
        return 0.0
    if len(latencies) == 1:
        # 单采样无法衡量抖动：按中性处理，避免把"测得少"当成"不稳定"
        return 1.0
    median = statistics.median(latencies)
    if median <= 0:
        return 0.0
    return max(0.0, 1.0 - min(statistics.pstdev(latencies) / median, 1.0))


def score_platform(samples):
    """samples: list of latency-or-None in probe order."""
    ok = [s for s in samples if s is not None]
    availability = len(ok) / len(samples) if samples else 0.0
    median = statistics.median(ok) if ok else None
    jitter = statistics.pstdev(ok) if len(ok) > 1 else None
    score = 100.0 * (
        WEIGHT_AVAIL * availability
        + WEIGHT_LAT * latency_score(median)
        + WEIGHT_STAB * stability_score(ok)
    )
    return {
        "score": round(score, 1),
        "availability": round(availability, 3),
        "median_ms": round(median, 1) if median is not None else None,
        "jitter_ms": round(jitter, 1) if jitter is not None else None,
        "attempts": len(samples),
        "ok": len(ok),
        "samples": [round(s, 1) if s is not None else None for s in samples],
    }


def load_purity() -> dict:
    """name -> 纯净度记录；文件缺失或过期返回空字典（评分退化为纯连通性）。"""
    try:
        if time.time() - os.path.getmtime(PURITY_FILE) > PURITY_MAX_AGE:
            print(
                f"note: purity snapshot older than {PURITY_MAX_AGE // 3600}h - ignored",
                file=sys.stderr,
            )
            return {}
        with open(PURITY_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for name, info in (doc.get("nodes") or {}).items():
        try:
            out[str(name)] = {"purity": float(info.get("purity") or 0.0), **info}
        except (TypeError, ValueError):
            continue
    return out


def write_json_atomic(path: str, doc) -> None:
    """同目录 tmp + rename + fsync：读方永远看不到半份快照。"""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def main() -> int:
    ap = argparse.ArgumentParser(description="score mihomo nodes per platform")
    ap.add_argument("--platforms", default="all", help="all, or comma separated keys")
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--threads", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=6000, help="per-probe timeout, ms")
    ap.add_argument("--prefer", default=",".join(DEFAULT_PREFER), help="preferred region codes")
    ap.add_argument("--prefer-bonus", type=float, default=5.0, help="points added for a preferred region")
    ap.add_argument(
        "--json",
        default=os.environ.get(
            "MIHOMO_SCORES", os.path.join(os.path.dirname(CONFIG), "scores.json")
        ),
    )
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if args.platforms.strip().lower() == "all":
        chosen = list(PLATFORMS)
    else:
        chosen = list(dict.fromkeys(p.strip() for p in args.platforms.split(",") if p.strip()))
        unknown = [p for p in chosen if p not in PLATFORMS]
        if unknown:
            print(f"unknown platform(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
    prefer = tuple(p.strip() for p in args.prefer.split(",") if p.strip())

    secret = _secret()
    pool = nodes(secret)
    if not pool:
        print("no nodes in the controller", file=sys.stderr)
        return 1

    # (node, platform) -> latencies in probe order; probes of one pair stay
    # sequential so a sample can catch a cold/flapping node, while different
    # pairs run in parallel.
    def worker(task):
        name, platform = task
        url = PLATFORMS[platform][0]
        expected = EXPECTED_STATUS.get(platform, "200-399")
        return task, [probe(name, url, args.timeout, secret, expected) for _ in range(args.samples)]

    tasks = [(n, p) for n in pool for p in chosen]
    raw = {}
    with ThreadPoolExecutor(max_workers=args.threads) as pool_exec:
        for (name, platform), samples in pool_exec.map(worker, tasks):
            raw.setdefault(name, {})[platform] = score_platform(samples)

    weight_sum = sum(PLATFORMS[p][1] for p in chosen)
    purity_table = load_purity()
    purity_weight = PURITY_WEIGHT if purity_table else 0.0
    platform_weights = {
        p: PLATFORMS[p][1] / weight_sum * (1.0 - purity_weight) for p in chosen
    }
    if purity_table:
        print(
            f"note: purity data for {len(purity_table)} node(s), weight={purity_weight:g}",
            file=sys.stderr,
        )
    result = {}
    for name, per_platform in raw.items():
        overall = sum(per_platform[p]["score"] * platform_weights[p] for p in chosen)
        purity = purity_table.get(name)
        if purity_weight:
            purity_score = purity["purity"] if purity is not None else PURITY_MISSING
            overall += purity_weight * purity_score
        region = region_of(name)
        bonus = args.prefer_bonus if region in prefer else 0.0
        entry = {
            "overall": round(overall, 1),
            "final": round(overall + bonus, 1),
            "region": region,
            "preferred": region in prefer,
            "bonus": round(bonus, 1),
            "platforms": per_platform,
        }
        if purity is not None:
            entry["purity"] = {
                "score": round(purity["purity"], 1),
                "ip": purity.get("ip"),
                "country_code": purity.get("country_code"),
                "claimed": purity.get("claimed"),
                "region_match": purity.get("region_match"),
                "hosting": purity.get("hosting"),
                "proxy": purity.get("proxy"),
                "reason": purity.get("reason"),
            }
        result[name] = entry

    ranked = sorted(result.items(), key=lambda kv: kv[1]["final"], reverse=True)
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    head = f"{'#':>3}  {'final':>5}  {'base':>5}  {'reg':<4}  {'node':<32}"
    for p in chosen:
        head += f" {p[:6]:>6}"
    print(head)
    print("-" * len(head))
    for rank, (name, info) in enumerate(ranked[: args.top], 1):
        line = (
            f"{rank:>3}  {info['final']:>5.1f}  {info['overall']:>5.1f}"
            f"  {info['region']:<4}  {name[:32]:<32}"
        )
        for p in chosen:
            line += f" {info['platforms'][p]['score']:>6.1f}"
        print(line)
    print(
        f"\n{len(ranked)} nodes, {args.samples} samples/platform,"
        f" prefer={','.join(prefer) or '-'} (+{args.prefer_bonus:g}), {stamp}"
    )

    write_json_atomic(
        args.json,
        {
            "generated": stamp,
            "samples": args.samples,
            # 平台权重与 purity_weight 之和恒为 1.0（契约要求），子集运行时按子集归一
            "platforms": {p: round(weight, 6) for p, weight in platform_weights.items()},
            "purity_weight": round(purity_weight, 6),
            "prefer": list(prefer),
            "prefer_bonus": args.prefer_bonus,
            "nodes": result,
        },
    )
    print(f"written {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
