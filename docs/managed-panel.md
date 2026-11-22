# Zashboard 托管 Mihomo

`compose.example.yaml` 运行一个 Panel 容器。Zashboard 提供管理页和诊断，Panel 管理主内核、订阅刷新、评分和巡检。配置只生成一个 `出口` 自动故障切换组。直连和拦截规则各自生效，AI 可用性仍参与组内节点评分。

## 首次部署

```bash
cp .env.example .env
# 在 .env 设置 DEV_API_SECRET
GEO_SRC=/path/to/geo-data bash tools/seed_dev.sh
docker compose -f compose.example.yaml up -d --build
```

Geo 数据目录应包含 `geoip.metadb` 和 `geosite.dat`。播种时可通过 `SUBS_SRC` 导入真实订阅清单。默认面板发布端口为 `19091`，宿主 API `19090` 和 mixed 代理 `17897` 仅绑定 loopback。后端路径为面板同源的 `/mihomo-api`，密码为 `DEV_API_SECRET`。内核离线时仍可打开 `/#/core` 管理内核。

首启按 `MIHOMO_INITIAL_VERSION` 从官方 Release 安装内核。从管理页或 CLI 指定版本即可独立升级或回滚。已有数据卷保留内核版本和手动停止状态。

```bash
docker compose -f compose.example.yaml exec panel panelctl status
docker compose -f compose.example.yaml exec panel panelctl refresh --skip-score
docker compose -f compose.example.yaml exec panel panelctl validate
```

## 规则集

首次运行会将预置的 HenryChiao 规则清单写入卷内的 `rules/active.yaml`，通过验证的规则文件存放在 `cache/rule-providers/`。第一次生成配置需要规则文件可达，也可以预置已有的同版本缓存。后续启动、订阅刷新和镜像重建直接复用卷中固定的规则。

在「内核管理」的「规则集」输入公开 HTTPS 链接并点击「更新规则」，或执行 `panelctl rules <https-url>`。链接应指向包含 `rules` 和可选 `rule-providers` 的 Clash YAML。Panel 下载新清单及其引用的文件，校验后热重载并保存到卷。失败时继续使用原规则。使用同一链接再次更新可获取该来源的新内容。

## 迁移现有开发栈

1. 备份数据卷和旧部署，把现有 `.env` 复制到新的部署目录。默认项目名为 `mihomo-stack`，数据卷为 `mihomo-stack_data`。可用 `MIHOMO_DATA_VOLUME` 选择其他已存在的卷。
2. 停止原项目后，在本仓库执行 `docker compose -f compose.example.yaml up -d --build`。首次迁移会复用旧配置的已验证规则文件，并将代理组收敛到 `出口`。
3. 验证状态、规则和实际代理流量。Compose 将数据卷声明为外部卷，重建容器时继续使用同一卷。

参阅 [卷内数据说明](panel-data.md) 和 [预置规则来源](../rules/SOURCE.md)。
