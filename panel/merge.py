#!/usr/bin/env python3
"""Merge local overrides into a pool of downloaded Clash subscriptions.

Every local policy decision lives in this file, so both a subscription
refresh (jobs.py) and a container restart reproduce the same result, and
the GUI/panel can only change what is declared here:

  * the pool is declared in `subscriptions.yaml` (file order = preference
    order); `jobs.py` fetches every entry and hands the files here in that
    same order. The first entry is the skeleton: its proxy-groups and rules
    are kept, so only it decides which extra groups the panel shows. Later
    entries contribute nodes only, which is why two pools cannot fight over
    a group name. A node name collision gets its pool tag prefixed
    (`[tag] name`), so the first pool keeps its names and the GUI's stored
    selections survive adding another subscription

  * TUN / inbound surfaces coming from the subscription are stripped
  * transparent proxy pinned on TPROXY_PORT (consumed by the egress container in a
    separate stack,
    which lives in the kernel and is therefore untouched by GUI edits)
  * DNS pinned to domestic DoH only (223.5.5.5 + doh.pub), except the AI
    domain set, whose poisoned answers from domestic resolvers would break the
    AI group health check. No global overseas fallback: it is only reachable
    through `出口`, and its answers for CN CDN names are foreign edges, which
    then miss GEOIP,CN and get proxied back
  * sniffer enabled: intercepted flows are IP-only otherwise, which would
    make the subscription's DOMAIN rules inert. `override-destination` is on
    because peer-supplied IPs can be DNS-poisoned; the sniffed SNI is then the
    only trustworthy destination.
  * `出口` is the single fallback group for proxied traffic. AI and optional
    site health are still measured when ranking nodes by their overall score.
  * Baidu / Bilibili domain families are pinned DIRECT ahead of the
    subscription's rules, so a sniffed flow that no rule covers cannot fall
    through to MATCH and come back from abroad
  * Steam is split by endpoint instead of proxied wholesale (see STEAM_RULES):
    depot CDN, CM, client-update and static hosts leave directly, the blocked
    handful (api/store/community) via `出口`. Proxying all of it made Steam
    report `"countrycode":"US"` and hand the client overseas download edges
  * banned regions (`BAN_NODE`) and pseudo-nodes (`INFO_NODE`) are deleted from
    the node table and from every group, so no rule, fallback or GUI pick can
    reach them
  * `local-nodes.yaml` holds nodes no subscription publishes (hand-converted
    share URIs); they are merged ahead of the subscription nodes and into
    `出口`, because a refresh rebuilds both from scratch
  * everything above is re-asserted on the finished config (`_assert_policy`),
    so a subscription refresh can never install a file that quietly lost the
    AI routing, the SNI override or the final MATCH rule
"""

import copy
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath

import yaml

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.rules import RULE_SET_REFERENCE, required_rules

PLACEHOLDER_MARKERS = ("未授权", "已过期", "需要续费", "请先关闭代理")
MIN_PROXIES = 20

# 运行期参数一律由环境变量给出，仓库里不出现任何密钥或固定端口。
# MIHOMO_SECRET 必填：它是面板与 shell 访问内核 API 的唯一凭据。
API_SECRET = os.environ.get("MIHOMO_SECRET", "")
API_LISTEN = os.environ.get("MIHOMO_API_LISTEN", "127.0.0.1:9090")
MIXED_PORT = int(os.environ.get("MIHOMO_MIXED_PORT", "7897"))
# 透明代理端口由出口容器消费；开发栈没有出口容器时留空即可不生成该键。
TPROXY_PORT = os.environ.get("MIHOMO_TPROXY_PORT", "7898")

# Nodes that no subscription publishes (hand-converted share URIs). Read on
# every merge so a subscription refresh cannot drop them.
LOCAL_NODES = os.environ.get(
    "MIHOMO_LOCAL_NODES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "local-nodes.yaml"),
)

EXIT_GROUP = "出口"
INFO_NODE = re.compile(r"剩余流量|距离下次重置|套餐到期|官网：")
# Nodes whose name matches this are removed from the node table and every group,
# so neither rules, the fallback group nor the GUI can ever select them.
BAN_NODE = re.compile(r"香港|🇭🇰")


def _blocked(name: str) -> bool:
    """Pseudo-nodes (流量/到期提示) and banned regions are never selectable."""
    return bool(INFO_NODE.search(name) or BAN_NODE.search(name))


def _local_nodes() -> list:
    """Host-only nodes, in file order (they are tried first, see `main`)."""
    if not os.path.exists(LOCAL_NODES):
        return []
    with open(LOCAL_NODES, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or []
    entries = doc.get("proxies") if isinstance(doc, dict) else doc
    if not isinstance(entries, list):
        raise SystemExit(f"rejected: {LOCAL_NODES} is not a proxy list")
    return [p for p in entries if isinstance(p, dict) and p.get("name")]


def _load_source(path: str, tag: str) -> dict:
    """Read one pool entry and reject the shapes a refresh can produce.

    A dead subscription still answers 200 with a single "expired" pseudo-node
    (or with no node at all), so both cases fail here instead of installing a
    pool entry that silently contributes nothing.
    """
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise SystemExit(f"rejected: {tag} subscription is not a YAML mapping")
    entries = [p for p in (doc.get("proxies") or []) if isinstance(p, dict) and p.get("name")]
    if not entries:
        raise SystemExit(f"rejected: {tag} subscription published no proxies")
    bad = [str(p["name"]) for p in entries if any(m in str(p["name"]) for m in PLACEHOLDER_MARKERS)]
    if bad:
        raise SystemExit(f"rejected: {tag} subscription has a placeholder node: {bad[0]}")
    return doc


def _unique(base: str, seen: set) -> str:
    candidate, index = base, 2
    while candidate in seen:
        candidate = f"{base} #{index}"
        index += 1
    return candidate


def _merge(tagged: list) -> list:
    """Flatten the pool in declaration order.

    Order is the preference order of `出口`, so the first pool that
    has a healthy node serves the traffic. Only colliding names are rewritten:
    renaming every node would drop the GUI's stored selections on the day a
    second subscription is added.
    """
    seen = set()
    merged = []
    for tag, entries in tagged:
        for proxy in entries:
            name = str(proxy["name"])
            if name in seen:
                name = _unique(f"[{tag}] {name}", seen)
            seen.add(name)
            merged.append({**proxy, "name": name})
    return merged


HEALTH_URL = "http://www.gstatic.com/generate_204"
HEALTH_INTERVAL = 300

AI_DOMAINS = (
    "chatgpt.com",
    "chat.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "sora.com",
)
AI_RULES = [f"DOMAIN-SUFFIX,{d},{EXIT_GROUP}" for d in AI_DOMAINS]

# Optional site rules pin the domains to the shared exit. The configured
# health URL still contributes to the node score.
def _site_config():
    """(enabled, health_url, rules) - 域名与探针必须成对出现。"""
    domains = tuple(
        d.strip().lower()
        for d in os.environ.get("MIHOMO_SITE_DOMAINS", "").split(",")
        if d.strip()
    )
    url = os.environ.get("MIHOMO_SITE_HEALTH_URL", "").strip()
    if domains and not url:
        raise SystemExit(
            "rejected: MIHOMO_SITE_DOMAINS is set but MIHOMO_SITE_HEALTH_URL is empty - "
            "site scoring requires a health URL"
        )
    return bool(domains), url, [f"DOMAIN-SUFFIX,{d},{EXIT_GROUP}" for d in domains]


SITE_ENABLED, SITE_HEALTH_URL, SITE_RULES = _site_config()

# 规则层：上游仓库的精简快照（只含 rule-providers 与 rules）。规则里的目标组名保持
# 上游原样，由 TARGET_MAP 落到本项目的组；缺文件时跳过（测试与最小部署也能跑）。
_HERE = os.path.dirname(os.path.abspath(__file__))
RULESET = os.environ.get(
    "MIHOMO_RULESET", os.path.join(_HERE, os.pardir, "rules", "henrychiao.yaml")
)
# 是否保留订阅自带的规则。默认丢弃：订阅规则与 curated 规则集常打架，且会引用
# 订阅自己的组（U3 之后那些组会被隐藏）。
SUB_RULES = os.environ.get("MIHOMO_SUB_RULES", "0") not in ("", "0", "false", "no")
# 规则集下载走哪条路：默认 DIRECT。内核默认按规则路由，而规则里 github 往往指向
# "国外流量"——出口组还没选好节点时会把规则集拉空（实测 EOF / connection refused）。
# 本机直连 raw.githubusercontent 可用，所以默认 DIRECT；直连不通的环境改这里。
RULESET_PROXY = os.environ.get("MIHOMO_RULESET_PROXY", "DIRECT")

# 上游目标组 -> 本项目落点。默认一律 `出口`；表里只列需要区别对待的。
TARGET_MAP = {
    "直接连接": "DIRECT",
    "国内流量": "DIRECT",
    "抖快书定位": "DIRECT",  # 抖音/快手地区服务，直连保持国内口径
    "DIRECT": "DIRECT",
    "REJECT": "REJECT",
    "REJECT-DROP": "REJECT-DROP",
    "PASS": "PASS",
}
TARGET_DEFAULT = EXIT_GROUP

# score.py measures every node per platform (and adds a small bonus for the
# preferred region). A fallback group serves its first healthy member, so
# ordering by that score instead of subscription order is what makes selection
# quality-driven. A stale file is ignored rather than trusted.
# 评分快照：默认落在数据根的 artifacts/ 下（脚本目录永远不会有运行期数据）
SCORES = os.environ.get(
    "MIHOMO_SCORES",
    os.path.join(os.environ.get("DATA_DIR", "/data"), "artifacts", "scores.json"),
)
# 数据契约：阈值 = 2 × 刷新周期（docs/DATA.md）。默认刷新 6h -> 12h。
SCORES_MAX_AGE = int(os.environ.get("MIHOMO_SCORES_MAX_AGE", str(12 * 3600)))

# Domains that must always leave from this node's own network. GEOIP,CN cannot
# cover them on its own: a sniffed flow has no destination IP, so a domain that
# the subscription has no rule for falls through to MATCH and gets proxied
# (observed on i1/i3.hdslb.com). bilibili.tv is deliberately absent - that one
# is the overseas site and belongs on the exit group.
CN_DIRECT_DOMAINS = (
    "baidu.com",
    "bdurl.net",
    "bdurl.cn",
    "bdstatic.com",
    "bdimg.com",
    "bilibili.com",
    "bilivideo.com",
    "hdslb.com",
    "hdslb.org",
    "biliapi.net",
    "biliimg.com",
    "biligame.com",
    "bili2233.cn",
    "b23.tv",
)
CN_DIRECT_RULES = [f"DOMAIN-SUFFIX,{d},DIRECT" for d in CN_DIRECT_DOMAINS]

# --- Steam ---------------------------------------------------------------
# Measured from this node: only a few Steam endpoints are blocked
# (api.steampowered.com 0/8 attempts, steamcommunity.com connect timeout,
# store.steampowered.com 1/3), while depot CDN, client-update, CM
# (steamserver.net) and the static hosts answer directly. Proxying the whole
# client - which is what MATCH does - also made Steam answer with
# `"countrycode":"US"` and hand out overseas edges (Fastly Tokyo, Alibaba HK),
# so every game download crossed the exit node. Split it by endpoint instead.
#
# CM servers are also dialled by cached IP without a DNS lookup, so Valve's own
# prefixes stay pinned DIRECT too; a CM reached abroad is what moves the login
# cell - and with it the download region - out of CN.
STEAM_DIRECT_DOMAINS = (
    "steamcontent.com",      # steampipe depot CDN: all game downloads
    "steamserver.net",       # CM / cell servers
    "steamstatic.com",       # client-update, store and community statics
    "steamusercontent.com",  # screenshots, workshop images
    "steamgames.com",        # partner assets the store pulls
)
STEAM_DIRECT_HOSTS = (
    "steamcdn-a.akamaihd.net",
    "media.steampowered.com",
    "login.steampowered.com",  # answers 302 directly
)
STEAM_PROXY_DOMAINS = (
    "steamcommunity.com",  # connect timeout direct
    "steampowered.com",    # api / store / checkout / help time out or flap
    "steam-chat.com",      # friend chat + voice
)
# AS32590 (Valve), IPv4 only, live BGP data collapsed to containing prefixes.
STEAM_DIRECT_CIDRS = (
    "45.121.184.0/24",
    "103.10.124.0/23",
    "103.28.54.0/24",
    "146.66.152.0/24",
    "146.66.155.0/24",
    "155.133.224.0/21",
    "155.133.236.0/22",
    "155.133.240.0/23",
    "155.133.244.0/24",
    "155.133.246.0/24",
    "155.133.248.0/22",
    "155.133.252.0/24",
    "155.133.254.0/23",
    "162.254.192.0/21",
    "185.25.180.0/24",
    "185.25.182.0/23",
    "192.69.96.0/22",
    "205.196.6.0/24",
    "208.64.200.0/22",
    "208.78.164.0/22",
)
# Domain rules before the CIDR block: the proxy entries must be able to win
# over a broad Valve prefix, and both must precede the subscription's
# DOMAIN-KEYWORD rules (google2.cdn.steampipe... matched `google` before).
STEAM_RULES = (
    [f"DOMAIN,{h},DIRECT" for h in STEAM_DIRECT_HOSTS]
    + [f"DOMAIN-SUFFIX,{d},DIRECT" for d in STEAM_DIRECT_DOMAINS]
    + [f"DOMAIN-SUFFIX,{d},{EXIT_GROUP}" for d in STEAM_PROXY_DOMAINS]
    + [f"IP-CIDR,{c},DIRECT,no-resolve" for c in STEAM_DIRECT_CIDRS]
)
PINNED_RULES = (SITE_RULES if SITE_ENABLED else []) + AI_RULES + CN_DIRECT_RULES + STEAM_RULES


def _assert_policy(cfg: dict) -> None:
    """Refuse to write a config that lost a pinned policy.

    A subscription refresh rebuilds this file from scratch, so a regression in
    the code above would silently hand peers a working-looking config without
    the AI routing or the SNI destination override. Fail before install
    instead, leaving the previous config.yaml in place.
    """
    groups = {
        g.get("name"): g for g in cfg.get("proxy-groups") or [] if isinstance(g, dict)
    }
    rules = [r for r in cfg.get("rules") or [] if isinstance(r, str)]
    lost = []
    if (groups.get(EXIT_GROUP) or {}).get("url") != HEALTH_URL:
        lost.append(f"{EXIT_GROUP} health check")
    if set(groups) != {EXIT_GROUP} or groups[EXIT_GROUP].get("type") != "fallback":
        lost.append("one fallback exit")
    if (cfg.get("sniffer") or {}).get("override-destination") is not True:
        lost.append("sniffer.override-destination")
    dns = cfg.get("dns") or {}
    resolvers = [
        *(dns.get("nameserver") or []),
        *(dns.get("direct-nameserver") or []),
    ]
    if "fallback" in dns or any(
        marker in str(u) for u in resolvers for marker in DNS_OVERSEAS
    ):
        lost.append("domestic-only DNS resolvers")
    if rules[: len(PINNED_RULES)] != PINNED_RULES:
        lost.append("AI / CN-direct / Steam rules not first")
    if not rules or rules[-1] != f"MATCH,{EXIT_GROUP}":
        lost.append("final MATCH rule")
    if lost:
        raise SystemExit("rejected: generated config lost pinned policy: " + ", ".join(lost))


DNS_RESOLVERS = [
    "https://223.5.5.5/dns-query",
    "https://doh.pub/dns-query",
]
# A resolver outside CN can only be reached through `出口`, so its answers look
# like overseas traffic: i1.hdslb.com resolved to cdn20.com / 14.128.1.28 (JP)
# instead of Tencent's 43.171.80.28, which then missed GEOIP,CN and got proxied.
DNS_OVERSEAS = ("1.1.1.1", "8.8.8.8", "8.8.4.4", "cloudflare-dns.com", "dns.google")

DNS_OVERRIDE = {
    "enable": True,
    "listen": "0.0.0.0:5353",
    "ipv6": False,
    "enhanced-mode": "normal",
    "respect-rules": True,
    "use-hosts": True,
    "use-system-hosts": True,
    "default-nameserver": ["223.5.5.5", "119.29.29.29"],
    "proxy-server-nameserver": ["https://223.5.5.5/dns-query"],
    "nameserver": list(DNS_RESOLVERS),
    "direct-nameserver": list(DNS_RESOLVERS),
    # Domestic resolvers answer blocked names with sinkhole IPs (223.5.5.5
    # returns 31.13.73.9 for chatgpt.com), which breaks the AI group's health
    # check. Those names only get a trustworthy answer from a resolver reached
    # through `出口`, so scope the overseas resolver to them instead of
    # falling back to it globally.
    "nameserver-policy": {
        f"+.{d}": ["https://1.1.1.1/dns-query"] for d in AI_DOMAINS
    },
}

SNIFFER = {
    "enable": True,
    "force-dns-mapping": True,
    "parse-pure-ip": True,
    # Peers resolve names themselves and hand us whatever IP they got; a
    # poisoned answer (chatgpt.com -> 104.244.46.21, 69.171.224.36) makes the
    # exit node dial the wrong host and the TLS handshake is closed
    # (schannel: "failed to receive handshake"). Overriding the destination
    # with the sniffed SNI makes the exit node dial the real domain instead.
    "override-destination": True,
    "sniff": {
        "TLS": {"ports": [443, 8443]},
        "HTTP": {"ports": [80, "8080-8880"]},
        "QUIC": {"ports": [443, 8443]},
    },
}


def _load_ruleset(path=None):
    """(providers, rules) from the curated ruleset; ({}, []) when the file is absent."""
    path = RULESET if path is None else path
    if not path or not os.path.exists(path):
        if path and os.environ.get("MIHOMO_RULESET"):
            raise SystemExit(f"rejected: configured ruleset is missing: {path}")
        print(f"note: no ruleset at {path} - subscription rules stay in use", file=sys.stderr)
        return {}, []
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    providers = doc.get("rule-providers") or {}
    rules = doc.get("rules") or []
    if not isinstance(providers, dict) or not isinstance(rules, list) or not rules:
        raise SystemExit(f"rejected: ruleset {path} needs rule-providers and rules")
    pinned = doc.get("pinned-providers") or {}
    return {name: pinned.get(name, spec) for name, spec in providers.items()}, [str(r) for r in rules]


def _map_rule(rule: str) -> str:
    """上游规则的目标组换成本项目的组；MATCH 由调用方统一追加。"""
    parts = [p.strip() for p in rule.split(",")]
    keyword = parts[0].upper()
    if keyword in ("MATCH", "FINAL") and len(parts) >= 2:
        parts[1] = TARGET_MAP.get(parts[1], TARGET_DEFAULT)
        return ",".join(parts)
    if keyword == "SUB-RULE":
        return ",".join(parts)
    if keyword in {"AND", "OR", "NOT"}:
        # AND,((DST-PORT,443),(NETWORK,UDP)),REJECT -> 目标在最后一段
        target = -2 if parts[-1] == "no-resolve" else -1
        parts[target] = TARGET_MAP.get(parts[target], TARGET_DEFAULT)
        return ",".join(parts)
    if len(parts) >= 3:
        parts[2] = TARGET_MAP.get(parts[2], TARGET_DEFAULT)
    return ",".join(parts)


def _map_rules(rules) -> list:
    return [
        _map_rule(rule)
        for rule in rules
        if rule.split(",", 1)[0].strip().upper() not in ("MATCH", "FINAL")
    ]


def merge_subscription_providers(config, providers, subscription_rules):
    sub_rules = {name: [_map_rule(rule) for rule in entries]
                 for name, entries in (config.get("sub-rules") or {}).items()}
    definitions = config.get("rule-providers") or {}
    required = required_rules({"rules": subscription_rules, "sub-rules": sub_rules})
    names = required & definitions.keys()
    reserved = providers.keys() | names
    renamed = {}
    for name in sorted(names):
        spec = definitions[name]
        if not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."} or not isinstance(spec, dict):
            raise ValueError("invalid subscription rule provider")
        target = name
        if name in providers:
            base = f"subscription-{name}"
            target, index = base, 2
            while target in reserved:
                target = f"{base}-{index}"
                index += 1
            reserved.add(target)
            renamed[name] = target

        spec = dict(spec)
        if spec.get("type") == "http":
            spec["path"] = f"./rule-providers/subscription/{target}.mrs"
            spec["proxy"] = RULESET_PROXY
        elif spec.get("type") == "file":
            parts = PurePosixPath(spec.get("path") or "").parts
            if len(parts) < 2 or parts[0] != "rule-providers" or ".." in parts:
                raise ValueError(f"subscription rule provider {name} must use a file in rule-providers/")
        elif spec.get("type") != "inline":
            raise ValueError(f"unsupported subscription rule provider: {name}")
        providers[target] = spec

    def rewrite(rule):
        return RULE_SET_REFERENCE.sub(
            lambda match: match[1] + renamed.get(match[2].strip(), match[2]), rule,
        )

    if sub_rules:
        config["sub-rules"] = {
            name: [rewrite(rule) for rule in entries] for name, entries in sub_rules.items()
        }
    return [rewrite(rule) for rule in subscription_rules] if renamed else subscription_rules


def apply_rules(config, subscription_rules, path=None):
    providers, selected = _load_ruleset(path)
    rules = _map_rules(selected)
    for name, spec in providers.items():
        if isinstance(spec, dict) and spec.get("type") == "http":
            spec.setdefault("path", f"./rule-providers/{name}.mrs")
            spec.setdefault("proxy", RULESET_PROXY)
    if SUB_RULES or not rules:
        rules.extend(merge_subscription_providers(config, providers, subscription_rules))
    else:
        config.pop("sub-rules", None)
        if subscription_rules:
            print(f"note: dropping {len(subscription_rules)} subscription rule(s)", file=sys.stderr)
    config["rules"] = [*PINNED_RULES, *rules, f"MATCH,{EXIT_GROUP}"]
    if providers:
        config["rule-providers"] = providers
    else:
        config.pop("rule-providers", None)


def installed_subscription_rules(config, active):
    _, selected = _load_ruleset(active)
    prefix = [*PINNED_RULES, *_map_rules(selected)]
    rules = config["rules"]
    if rules[:len(prefix)] != prefix or rules[-1] != f"MATCH,{EXIT_GROUP}":
        raise ValueError("installed rules differ from the active ruleset; refresh configuration first")
    return rules[len(prefix):-1]


def _score_table() -> dict:
    """name -> (final, {platform: score + region bonus}); {} when missing/stale."""
    try:
        if time.time() - os.path.getmtime(SCORES) > SCORES_MAX_AGE:
            return {}
        with open(SCORES, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    table = {}
    for name, info in (doc.get("nodes") or {}).items():
        try:
            bonus = float(info.get("bonus") or 0.0)
            platforms = {
                key: float(cell.get("score") or 0.0) + bonus
                for key, cell in (info.get("platforms") or {}).items()
            }
            final = float(info.get("final") or 0.0)
        except (AttributeError, TypeError, ValueError):
            # 快照被外部改坏时只丢这一条，不让整轮刷新失败
            print(f"note: ignoring malformed score entry {name!r}", file=sys.stderr)
            continue
        table[str(name)] = (final, platforms)
    return table


def _rank(nodes: list, scores: dict) -> list:
    """Best score first; ties and unscored nodes keep the subscription order."""
    if not scores:
        return list(nodes)

    def key(name: str):
        entry = scores.get(name)
        if entry is None:
            return (1, 0.0)
        final, _ = entry
        return (0, -final)

    return sorted(nodes, key=key)


def configure_inbound(cfg):
    cfg["mixed-port"] = MIXED_PORT
    cfg["allow-lan"] = True
    cfg["bind-address"] = "*"
    cidrs = os.environ.get("MIHOMO_LAN_ALLOWED_IPS", "127.0.0.0/8,10.0.0.0/8")
    cfg["lan-allowed-ips"] = [str(ipaddress.ip_network(cidr.strip())) for cidr in cidrs.split(",")]
    cfg["external-controller"] = API_LISTEN
    cfg["external-doh-server"] = "/dns-query"
    if not API_SECRET:
        raise SystemExit("rejected: MIHOMO_SECRET is empty - the API would be unauthenticated")
    cfg["secret"] = API_SECRET
    cfg["external-controller-cors"] = {"allow-origins": ["*"], "allow-private-network": True}
    if TPROXY_PORT:
        cfg["tproxy-port"] = int(TPROXY_PORT)
    else:
        cfg.pop("tproxy-port", None)


def restore(path):
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict) or not cfg.get("proxies"):
        raise SystemExit("installed configuration has no nodes")
    configure_inbound(cfg)
    cfg["proxy-groups"] = [group for group in cfg.get("proxy-groups", []) if group.get("name") == EXIT_GROUP]
    cfg["rules"] = [*_map_rules(cfg.get("rules", [])), f"MATCH,{EXIT_GROUP}"]
    _assert_policy(cfg)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)


def main(dst: str, srcs: list) -> None:
    docs = []
    for path in srcs:
        tag = os.path.splitext(os.path.basename(path))[0]
        docs.append((tag, _load_source(path, tag)))

    # The first pool entry supplies the template and optional subscription rules.
    # Every pool supplies nodes to the common exit.
    cfg = docs[0][1]
    proxies = _merge([(tag, doc.get("proxies") or []) for tag, doc in docs])
    names = [str(p["name"]) for p in proxies]
    # Per-entry sanity lives in _load_source; this is the pool-level guard: a
    # refresh that lost every node must not install a config whose fallback
    # groups have nothing to dial.
    if len(names) < MIN_PROXIES:
        raise SystemExit(f"rejected: pool has only {len(names)} proxies (< {MIN_PROXIES})")

    for key in (
        "port",
        "socks-port",
        "redir-port",
        "tproxy-port",
        "listeners",
        "tun",
        "secret",
        "authentication",
        "external-controller-tls",
        "external-controller-unix",
        "external-controller-pipe",
        "proxy-providers",
    ):
        cfg.pop(key, None)

    configure_inbound(cfg)
    cfg["ipv6"] = False
    cfg["geo-auto-update"] = False
    cfg["profile"] = {"store-selected": True, "store-fake-ip": True}
    cfg["sniffer"] = copy.deepcopy(SNIFFER)
    cfg["dns"] = copy.deepcopy(DNS_OVERRIDE)

    # --- exit group: first healthy node in subscription order -------------
    # Host-only nodes lead the list: a fallback group serves traffic from its
    # first healthy member, so file order is the actual preference order.
    local = [p for p in _local_nodes() if not _blocked(str(p["name"]))]
    local_names = [str(p["name"]) for p in local]
    nodes = local_names + [
        n for n in names if n and not _blocked(n) and n not in local_names
    ]
    if not nodes:
        raise SystemExit("rejected: no usable nodes for exit group")

    scores = _score_table()
    if not scores:
        print(
            f"note: {os.path.basename(SCORES)} missing or older than "
            f"{SCORES_MAX_AGE // 3600}h - keeping subscription order",
            file=sys.stderr,
        )
    elif not any(n in scores for n in nodes):
        print(
            "note: no node in the pool has a score (names changed?) - "
            "keeping subscription order",
            file=sys.stderr,
        )

    exit_group = {
        "name": EXIT_GROUP,
        "type": "fallback",
        "proxies": _rank(nodes, scores),
        "url": HEALTH_URL,
        "interval": HEALTH_INTERVAL,
        "timeout": 3000,
        "max-failed-times": 3,
        "expected-status": 204,
        "lazy": False,
    }
    cfg["proxy-groups"] = [exit_group]

    subscription_rules = _map_rules(rule for rule in cfg.get("rules", []) if isinstance(rule, str))
    apply_rules(cfg, [rule for rule in subscription_rules if rule not in PINNED_RULES])

    # banned regions and pseudo-nodes never reach the node table at all
    cfg["proxies"] = local + [
        p
        for p in proxies
        if not _blocked(str(p.get("name", ""))) and str(p.get("name", "")) not in local_names
    ]

    _assert_policy(cfg)

    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, dst)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--restore":
        restore(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) < 3:
        raise SystemExit(
            f"usage: {sys.argv[0]} <output.yaml> <subscription.yaml> [more.yaml ...]"
        )
    main(sys.argv[1], sys.argv[2:])
