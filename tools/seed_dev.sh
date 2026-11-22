#!/usr/bin/env bash
# 把开发栈需要的最小数据播种进命名卷（一次性，可重复执行）。
#
#   bash tools/seed_dev.sh                 # 离线：geo 文件取自 GEO_SRC，订阅池指向本地 fixture
#   GEO_SRC=/path bash tools/seed_dev.sh   # 指定 geo 文件来源目录
#   SUBS_SRC=/path/subscriptions.yaml bash tools/seed_dev.sh   # 用真实订阅池（含令牌）播种
#
# 只写数据卷，不碰生产目录；重复执行会覆盖 secrets 与 assets，artifacts 保留。
set -euo pipefail

VOLUME=${MIHOMO_DATA_VOLUME:-${VOLUME:-mihomo-stack_data}}
GEO_SRC=${GEO_SRC:-/opt/mihomo}
SUBS_SRC=${SUBS_SRC:-}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Compose 声明使用已有卷。只在初始部署创建它。
docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null

MOUNTS=(-v "$VOLUME:/data" -v "$ROOT/tests/fixtures:/fixtures:ro")
[ -d "$GEO_SRC" ] && MOUNTS+=(-v "$GEO_SRC:/geo:ro")
[ -n "$SUBS_SRC" ] && [ -f "$SUBS_SRC" ] && MOUNTS+=(-v "$SUBS_SRC:/subs.yaml:ro")

docker run --rm "${MOUNTS[@]}" alpine:3.24 sh -c '
set -eu
mkdir -p /data/secrets /data/assets /data/artifacts /data/cache /data/archive
if [ -f /geo/geoip.metadb ] && [ -f /geo/geosite.dat ]; then
  cp -f /geo/geoip.metadb /geo/geosite.dat /data/assets/
  echo "  assets: copied from /geo"
else
  echo "  assets: MISSING (pass GEO_SRC=<dir with geoip.metadb + geosite.dat>)" >&2
fi
if [ -f /subs.yaml ]; then
  cp -f /subs.yaml /data/secrets/subscriptions.yaml
  echo "  secrets: copied real subscription pool"
else
  cp -f /fixtures/dev-subscription.yaml /data/dev-subscription.yaml
  cat > /data/secrets/subscriptions.yaml <<YAML
# 开发栈离线池：节点全部指向本机不可达端口，只用于验证数据流与配置生成
subscriptions:
  - name: offline
    url: file:///data/dev-subscription.yaml
YAML
  echo "  secrets: offline pool (file:///data/dev-subscription.yaml)"
fi
chmod 600 /data/secrets/subscriptions.yaml
[ -f /data/secrets/local-nodes.yaml ] || printf "# 手工节点（订阅里没有的），结构同订阅的 proxies 列表\nproxies: []\n" > /data/secrets/local-nodes.yaml
chmod 600 /data/secrets/local-nodes.yaml
'
