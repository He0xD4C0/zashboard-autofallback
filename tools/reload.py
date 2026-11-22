#!/usr/bin/env python3
"""把一份配置文件热重载进内核（payload 方式）。

内核只允许 `path` 方式重载它 `-d` 目录内的文件，而我们的配置放在别处；payload 方式
把正文直接发给 API，失败时内存与磁盘都不变。脚本对"内核还没起来"做有限重试。

    reload.py <api-base> <secret> <config.yaml> [--retries 30] [--delay 2]

环境变量：无（全部由参数给出，便于被 Panel 刷新任务共用）。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def reload(api: str, secret: str, path: str, retries: int, delay: float) -> int:
    api = api.rstrip("/")
    with open(path, encoding="utf-8") as fh:
        body = json.dumps({"payload": fh.read()}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(retries):
        req = urllib.request.Request(
            api + "/configs?force=true",
            data=body,
            method="PUT",
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=60):
                pass
            return 0
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500:
                break
        except OSError as exc:
            last = exc
        time.sleep(delay)
    print(f"reload rejected: {last}", file=sys.stderr)
    return 1


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="reload a config via the kernel API")
    ap.add_argument("api")
    ap.add_argument("secret")
    ap.add_argument("path")
    ap.add_argument("--retries", type=int, default=30)
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args(argv[1:])
    return reload(args.api, args.secret, args.path, args.retries, args.delay)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
