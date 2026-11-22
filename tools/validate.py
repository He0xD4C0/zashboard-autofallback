#!/usr/bin/env python3
"""校验运行期数据文件是否符合 schemas/ 下的数据契约。

    validate.py scores PATH      # score 快照（JSON）
    validate.py subs   PATH      # 订阅池清单（YAML）
    validate.py tree   DATA_DIR  # 目录视图：artifacts/scores.json + secrets/subscriptions.yaml

设计约束：
  * 只读校验，绝不修改文件、绝不打印令牌内容（订阅 URL 报错时只给条目序号）。
  * schema 只覆盖我们自己产出的数据。生成的 config.yaml 的唯一权威校验是内核自身
    的 `-t` 检查，这里不复制一份规则，避免两处真源。
  * schema 之外的语义约束（例如订阅名唯一）在代码里补，因为 JSON Schema 表达不了。
"""

import json
import os
import sys

import yaml
from jsonschema import Draft7Validator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 容器里 tools 挂在 /opt/tools，父目录不是仓库根，所以 schema 目录允许显式指定。
SCHEMAS = os.environ.get("SCHEMAS_DIR") or os.path.join(ROOT, "schemas")


def _load_schema(name: str) -> dict:
    path = os.path.join(SCHEMAS, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"schema not found: {path} (set SCHEMAS_DIR if tools are mounted elsewhere)")


def _report(errors, limit: int = 10) -> int:
    for error in errors[:limit]:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        print(f"  invalid at {where}: {error.message}", file=sys.stderr)
    if len(errors) > limit:
        print(f"  ... {len(errors) - limit} more", file=sys.stderr)
    return 1


def validate_scores(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    validator = Draft7Validator(_load_schema("scores.schema.json"))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        print(f"{path}: FAILED", file=sys.stderr)
        return _report(errors)

    weights = sum(float(v) for v in (doc.get("platforms") or {}).values())
    purity_weight = float(doc.get("purity_weight") or 0.0)
    if abs(weights + purity_weight - 1.0) > 0.01:
        print(
            f"{path}: FAILED\n  platform weights + purity_weight = {weights + purity_weight:.3f},"
            " expected 1.0",
            file=sys.stderr,
        )
        return 1
    nodes = doc["nodes"]
    stale = [n for n, info in nodes.items() if not info.get("platforms")]
    if stale:
        print(f"{path}: FAILED\n  {len(stale)} node(s) without platform metrics", file=sys.stderr)
        return 1

    print(f"{path}: ok ({len(nodes)} nodes, {len(doc['platforms'])} platforms, {doc['generated']})")
    return 0


def validate_subs(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    validator = Draft7Validator(_load_schema("subscriptions.schema.json"))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        print(f"{path}: FAILED", file=sys.stderr)
        return _report(errors)

    # 订阅名进文件名也进节点名前缀，重复会让两池节点互相覆盖。
    seen = {}
    for index, entry in enumerate(doc["subscriptions"], 1):
        name = entry["name"]
        if name in seen:
            print(
                f"{path}: FAILED\n  duplicate subscription name {name!r} "
                f"(entries {seen[name]} and {index})",
                file=sys.stderr,
            )
            return 1
        seen[name] = index

    print(f"{path}: ok ({len(doc['subscriptions'])} subscriptions: {', '.join(seen)})")
    return 0


def validate_tree(data_dir: str) -> int:
    targets = [
        (validate_scores, os.path.join(data_dir, "artifacts", "scores.json")),
        (validate_subs, os.path.join(data_dir, "secrets", "subscriptions.yaml")),
    ]
    status = 0
    for fn, path in targets:
        if not os.path.exists(path):
            print(f"{path}: missing (skipped)", file=sys.stderr)
            continue
        status |= fn(path)
    return status


def main(argv) -> int:
    if len(argv) != 3 or argv[1] not in ("scores", "subs", "tree"):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: validate.py {scores|subs|tree} PATH", file=sys.stderr)
        return 2
    command, path = argv[1], argv[2]
    try:
        if command == "scores":
            return validate_scores(path)
        if command == "subs":
            return validate_subs(path)
        return validate_tree(path)
    except FileNotFoundError:
        print(f"{path}: not found", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"{path}: unparseable ({exc.__class__.__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
