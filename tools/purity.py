#!/usr/bin/env python3
"""测每个节点的"真实出口 IP"与纯净度，并把地区虚标扣进分数。

两步走，比"在节点后面直接查情报接口"稳得多：

1. **取 IP**（经节点）：`https://www.cloudflare.com/cdn-cgi/trace` 返回 `ip=`/`loc=`，
   失败再退 `https://ipwho.is/`、`http://ip-api.com/json/?fields=query,countryCode`。
   很多机场节点会拦明文 HTTP 或拦情报站，所以这一步只要求"能拿到自己的出口 IP"。
2. **判属性**（直连）：拿到的 IP 交给 `ip-api.com/json/<ip>` 查 hosting/proxy/mobile/ASN，
   失败退 `ipwho.is/<ip>`。直连查询不经过节点，也不会因为节点封锁而失败。

纯净度评分（100 分制，允许依赖外部 API 是既定策略）：

    住宅 / 移动网络（hosting=false, proxy=false）   100
    数据中心（hosting=true, proxy=false）           55
    被判定为代理 / VPN（proxy=true）                20
    地区虚标（节点名声明地区 ≠ 实际国家）           -25（下限 0）
    IP 拿不到                                       0（reason=unreachable）

用法：
    purity.py [--nodes REGEX] [--limit N] [--pace 1.2] [--json OUT]

环境变量：MIHOMO_CONFIG（取 secret）、PROBE_API、PROBE_PROXY。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

CONFIG = os.environ.get(
    "MIHOMO_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "artifacts", "config.yaml"),
)
PROBE_API = os.environ.get("PROBE_API", "http://127.0.0.1:9190").rstrip("/")
PROBE_PROXY = os.environ.get("PROBE_PROXY", "http://127.0.0.1:7899")
PURITY_MISSING = float(os.environ.get("MIHOMO_PURITY_MISSING", "40"))

FIELDS = "status,message,country,countryCode,regionName,city,as,asname,hosting,proxy,mobile"

REGION_COUNTRY = {
    "jp": "JP", "sg": "SG", "us": "US", "hk": "HK", "tw": "TW", "gb": "GB",
    "de": "DE", "nl": "NL", "my": "MY", "kr": "KR", "cn": "CN",
}
REGION_HINTS = {
    "jp": ("🇯🇵", "日本", "japan"),
    "sg": ("🇸🇬", "新加坡", "singapore"),
    "us": ("🇺🇸", "美国", "united states", "洛杉矶"),
    "hk": ("🇭🇰", "香港", "hong kong"),
    "tw": ("🇹🇼", "台湾", "taiwan"),
    "gb": ("🇬🇧", "英国", "united kingdom", "伦敦"),
    "de": ("🇩🇪", "德国", "germany"),
    "nl": ("🇳🇱", "荷兰", "netherlands"),
    "my": ("🇲🇾", "马来西亚", "malaysia"),
    "kr": ("🇰🇷", "韩国", "korea"),
    "cn": ("🇨🇳", "中国"),
}
HOSTING_WORDS = (
    "cloud", "hosting", "server", "data", "vps", "colo", "amazon", "google",
    "microsoft", "digitalocean", "linode", "ovh", "hetzner", "leaseweb",
    "contabo", "vultr", "choopa", "m247", "zenlayer", "alibaba", "tencent",
)


def region_of(name: str) -> str:
    lowered = name.lower()
    for code, fragments in REGION_HINTS.items():
        if any(f.lower() in lowered for f in fragments):
            return code
    return "unknown"


def secret() -> str:
    if os.environ.get("MIHOMO_SECRET"):
        return os.environ["MIHOMO_SECRET"]
    with open(CONFIG, encoding="utf-8") as fh:
        return str((yaml.safe_load(fh) or {}).get("secret") or "")


def _request(url, timeout, method="GET", body=None, headers=None, proxy=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _api(path, timeout, token):
    raw = _request(
        PROBE_API + path, timeout, headers={"Authorization": "Bearer " + token}
    )
    return json.loads(raw)


def nodes(token):
    data = _api("/proxies", 15, token)["proxies"]
    skip = {"GLOBAL", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE"}
    return [
        name
        for name, info in data.items()
        if name not in skip
        and info.get("type") not in (
            "Selector", "Fallback", "URLTest", "LoadBalance", "Relay",
            "Direct", "Reject", "Pass", "Compatible",
        )
    ]


def select(node, token) -> bool:
    try:
        _request(
            PROBE_API + "/proxies/GLOBAL",
            15,
            method="PUT",
            body={"name": node},
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        return _api("/proxies/GLOBAL", 10, token).get("now") == node
    except Exception:
        return False


def reachable(node, timeout, token) -> bool:
    """节点自身都不通时，不必浪费一次外部查询。"""
    query = urllib.parse.urlencode(
        {"url": "http://www.gstatic.com/generate_204", "timeout": int(timeout * 1000)}
    )
    try:
        _request(
            PROBE_API + "/proxies/" + urllib.parse.quote(node, safe="") + "/delay?" + query,
            timeout + 10,
            headers={"Authorization": "Bearer " + token},
        )
        return True
    except Exception:
        return False


def parse_trace(raw: str) -> dict:
    """cdn-cgi/trace 是 key=value 行文本，取 ip 与 loc。"""
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if not fields.get("ip"):
        raise RuntimeError("trace has no ip field")
    return {"ip": fields["ip"], "country_code": (fields.get("loc") or "").upper(),
            "source": "cdn-cgi/trace"}


def exit_ip_via_node(timeout) -> dict:
    """经节点取自己的出口 IP + 国家（只要这一步成功，剩下的都能直连补全）。"""
    errors = []
    try:
        raw = _request(
            "https://www.cloudflare.com/cdn-cgi/trace",
            timeout,
            proxy=PROBE_PROXY,
            headers={"User-Agent": "curl/8"},
        ).decode("utf-8", "replace")
        return parse_trace(raw)
    except Exception as exc:
        errors.append(f"trace: {str(exc)[:60]}")

    for url, name in (("https://ipwho.is/", "ipwho"), ("https://api.myip.com", "myip")):
        try:
            doc = json.loads(_request(url, timeout, proxy=PROBE_PROXY))
            ip = doc.get("ip")
            if ip:
                return {
                    "ip": ip,
                    "country_code": (doc.get("country_code") or doc.get("cc") or "").upper(),
                    "source": name,
                }
            errors.append(f"{name}: no ip field")
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:60]}")
    raise RuntimeError(" | ".join(errors))


def classify(ip, timeout, pace_state) -> dict:
    """直连查询该 IP 的属性：机房/代理/移动/ASN。"""
    wait = pace_state["pace"] - (time.time() - pace_state["last"])
    if wait > 0:
        time.sleep(wait)
    try:
        doc = json.loads(
            _request(f"http://ip-api.com/json/{ip}?fields={FIELDS}", timeout)
        )
        pace_state["last"] = time.time()
        if doc.get("status") == "success":
            return {
                "country": doc.get("country"),
                "country_code": (doc.get("countryCode") or "").upper() or None,
                "region": doc.get("regionName"),
                "city": doc.get("city"),
                "asn": doc.get("as"),
                "asname": doc.get("asname") or doc.get("as"),
                "hosting": doc.get("hosting"),
                "proxy": doc.get("proxy"),
                "mobile": bool(doc.get("mobile")),
                "source": "ip-api",
            }
    except Exception:
        pass

    doc = json.loads(_request(f"https://ipwho.is/{ip}", timeout))
    pace_state["last"] = time.time()
    if not doc.get("success"):
        raise RuntimeError(doc.get("message") or "ipwho status != success")
    conn = doc.get("connection") or {}
    org = str(conn.get("org") or conn.get("isp") or "")
    return {
        "country": doc.get("country"),
        "country_code": (doc.get("country_code") or "").upper() or None,
        "region": doc.get("region"),
        "city": doc.get("city"),
        "asn": f"AS{conn.get('asn')}" if conn.get("asn") else None,
        "asname": org or None,
        # ipwho 不给 hosting/proxy 标记，只能按 ASN 归属粗判
        "hosting": any(w in org.lower() for w in HOSTING_WORDS),
        "proxy": False,
        "mobile": "mobile" in str(conn.get("type") or "").lower(),
        "source": "ipwho.is",
    }


def score_of(info: dict, claimed: str):
    """(purity, reason)"""
    if info.get("proxy"):
        base, reason = 20.0, "flagged-proxy"
    elif info.get("hosting"):
        base, reason = 55.0, "datacenter"
    elif info.get("proxy") is None or info.get("hosting") is None:
        base, reason = PURITY_MISSING, "unclassified"
    else:
        base, reason = 100.0, "residential"
    expected = REGION_COUNTRY.get(claimed)
    if expected and info.get("country_code") and expected != info["country_code"]:
        base -= 25.0
        reason += "+region-mismatch"
    return max(0.0, base), reason


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="probe real exit IP and score purity")
    ap.add_argument("--nodes", default=".*", help="只有匹配该正则的节点参与")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pace", type=float, default=1.2, help="直连情报查询的最小间隔（秒）")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument(
        "--json",
        default=os.environ.get(
            "MIHOMO_PURITY", os.path.join(os.path.dirname(CONFIG), "purity.json")
        ),
    )
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv[1:])

    token = secret()
    pool = [n for n in nodes(token) if re.search(args.nodes, n)]
    if args.limit:
        pool = pool[: args.limit]
    if not pool:
        print("no nodes to probe", file=sys.stderr)
        return 1

    pace_state = {"pace": args.pace, "last": 0.0}
    result = {}
    for index, node in enumerate(pool, 1):
        started = time.time()
        claimed = region_of(node)
        if not select(node, token) or not reachable(node, args.timeout, token):
            result[node] = {"purity": 0.0, "reason": "unreachable", "claimed": claimed}
            print(f"  [{index}/{len(pool)}] {node}: unreachable", flush=True)
            continue
        try:
            found = exit_ip_via_node(args.timeout)
        except Exception as exc:
            result[node] = {
                "purity": 0.0,
                "reason": f"no-exit-ip: {str(exc)[:120]}",
                "claimed": claimed,
            }
            print(f"  [{index}/{len(pool)}] {node}: 取不到出口 IP ({str(exc)[:60]})", flush=True)
            continue
        try:
            info = classify(found["ip"], args.timeout, pace_state)
        except Exception as exc:
            # 属性查不到但 IP 是真的：按"未知"处理，给中间分而不是 0
            info = {
                "country": None, "asn": None, "asname": None,
                "hosting": None, "proxy": None, "mobile": None,
                "classify_error": str(exc)[:80],
            }
        country_code = info.get("country_code") or found.get("country_code") or None
        info["country_code"] = country_code
        purity, reason = score_of(info, claimed)
        expected = REGION_COUNTRY.get(claimed)
        result[node] = {
            **info,
            **found,
            "country_code": country_code,
            "claimed": claimed,
            "region_match": expected == country_code if expected and country_code else None,
            "purity": purity,
            "reason": reason,
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
        print(
            f"  [{index}/{len(pool)}] {node}: {found['ip']} {country_code}"
            f" {info.get('asname') or '?'} purity={purity:.0f} ({reason})",
            flush=True,
        )

    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    ranked = sorted(result.items(), key=lambda kv: kv[1]["purity"], reverse=True)
    measured = sum(1 for v in result.values() if v.get("ip"))
    print(f"\n{len(result)} nodes probed, {measured} with exit IP, {stamp}")
    for name, info in ranked[: args.top]:
        print(
            f"  {info['purity']:>5.0f}  {name[:34]:<34} {info.get('country_code') or '--'}"
            f" {info.get('reason', '')}"
        )

    doc = {
        "generated": stamp,
        "probe_api": PROBE_API,
        "pace_seconds": args.pace,
        "nodes": result,
    }
    tmp = f"{args.json}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, args.json)
    print(f"written {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
