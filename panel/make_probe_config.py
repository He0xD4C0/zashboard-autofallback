#!/usr/bin/env python3
"""生成一个一次性探测内核的配置：同一批节点、global 模式、无规则、无 DNS。

主内核有规则和 fallback 组，无法表达"这条请求请走某个具体节点"。第二个内核实例在
global 模式下正好提供这个能力：切换 GLOBAL 选中项，再经它的混合端口发请求，就能拿到
该节点的出口 IP 与真实地区——这是"IP 纯净度"和"地区是否虚标"的唯一可靠测法。

    make_probe_config.py <main-config.yaml> <probe-config.yaml> [--mixed-port 7899]
"""

import argparse
import os
import sys
import tempfile

import yaml


def build(main: dict, mixed_port: int, secret: str, api: str) -> dict:
    proxies = main.get("proxies") or []
    if not proxies:
        raise SystemExit("rejected: source config has no proxies")
    return {
        "mixed-port": mixed_port,
        # global 模式：所有流量按 GLOBAL 组的选中项走，规则不参与
        "mode": "global",
        "log-level": "warning",
        "allow-lan": False,
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": api,
        "secret": secret,
        # 探测实例不提供 DNS 服务，避免和主内核抢 5353；域名交给容器 resolver
        "dns": {"enable": False},
        "proxies": proxies,
        "rules": ["MATCH,DIRECT"],
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="build a global-mode probe config")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mixed-port", type=int, default=7899)
    ap.add_argument("--api", default="127.0.0.1:9190")
    ap.add_argument("--secret", default=os.environ.get("MIHOMO_SECRET", ""))
    args = ap.parse_args(argv[1:])

    with open(args.src, encoding="utf-8") as fh:
        source = yaml.safe_load(fh) or {}
    doc = build(source, args.mixed_port, args.secret, args.api)

    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(args.dst) or ".", prefix=f".{os.path.basename(args.dst)}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)
        os.replace(tmp, args.dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"written {args.dst} ({len(doc['proxies'])} nodes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
