# 规则层来源与更新

## 来源

* 上游仓库：`HenryChiao/MIHOMO_YAMLS`（AGPL-3.0），文件 `THEYAMLS/General_Config/HenryChiao/MihomoProMax.yaml`
* 本仓库只取其中的 **`rule-providers`（43 个）与 `rules`（45 条）**，其余一律不用：
  上游的 54 个策略组、DNS、端口、TUN、hosts、profile 等由本项目自己的 `panel/merge.py` 决定。
* 当前快照对应的上游提交：`39fc598d3ed20530da711f13ab6a7a2e98aa6cb7`（2026-11-20 抓取）
* 快照文件：`rules/henrychiao.yaml`（规则里的目标组名保持上游原样）

## 预置与更新

首次启动将清单复制到数据卷。被引用的规则文件由独立 Mihomo 进程校验后固定到卷中。部署后通过「内核管理」更新公开 HTTPS 规则清单。Panel 重建时复用已经固定的规则。

上游规则目标由 `panel/merge.py` 映射。国内及局域网类使用 `DIRECT`，拦截使用 `REJECT`，其他需要代理的流量统一走 `出口`。

## 许可

上游清单来自 AGPL-3.0 仓库。分发预置清单时保留本文件中的来源及许可说明。
