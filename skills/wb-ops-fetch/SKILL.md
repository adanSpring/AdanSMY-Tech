---
name: wb-ops-fetch
description: 通过 WorkBuddy 企业 OpenAPI（pt_ Key，纯后台 HTTP）抓取企业运维数据——席位/积分/用量趋势/成员/超限与未启用名单，并刷新「个人日常工作台」资料库在线页。当用户要求「刷新 WorkBuddy 运维数据」「抓取企业用量」「更新运维看板」「立即抓取运维数据」时使用；也可由资料库页面的按钮触发。
---

# wb-ops-fetch —— WorkBuddy 企业运维数据抓取（纯后台 HTTP）

## 何时用
- 用户要求刷新/抓取 WorkBuddy 企业控制台运维数据（席位、积分、每日用量、超限/未启用名单）。
- 用户点「个人日常工作台」在线页的「▶ 立即抓取运维数据」按钮（由技能触发，无需本机服务）。
- 定时/自动化场景：每天 9:00 自动出数（见下「定时」）。

## 关键事实（避免踩坑）
- 企业 `fzrv66luc6ww`。OpenAPI Base：`https://api.copilot.tencent.com/api/v1/enterprises/{ent}`。
- 鉴权 `Authorization: Bearer pt_xxx`，**企业级应用身份**（管理员在【开放与集成】→【开放平台】创建应用生成的 `pt_` Key），个人 Key 一律 401。
- **Key 不进代码**：运行时从 `~/.workbuddy/wb_openapi.key` 读取（可用 `WB_OPENAPI_KEY` 环境变量覆盖路径）。本技能仓库**不含任何密钥**。
- 沙箱内 `urllib` 走代理返回 400 → 统一用 `curl` 子进程调用（scripts 已处理）。
- 核心接口：
  - `GET /license`（席位）
  - `GET /openapi/resources/overview`（积分池）
  - `GET /openapi/usage/default-quota`（每人 2000）
  - `GET /openapi/members`（pageSize≤200）
  - `POST /openapi/usage/members/query`（**必传 userIds**，totalUsed **忽略 timeRange=周期总量**）
  - `POST /openapi/usage/members/detail`（**version:2**，timeRange≤31 天，nextPageToken 翻页，含 departmentInfo.fullPath）
- ⚠️ 每日用量**不能**靠 members/query 切片（它永远返回周期总量）；必须靠 members/detail 按 usageTime 聚合。
- ⚠️ 成员含系统账号 `service-account-*`，需剔除。

## 用法
技能脚本在 `scripts/`：
- `python3 scripts/wb_ops_daily.py` —— 抓数并更新资料库页（默认动作）。
  - `--dry-run`：只落盘 `wb_ops_latest.json`，不改页面。
  - `--backfill`：强制重抓整个计费周期每日用量。
- `python3 scripts/wb_openapi_fetch.py --compare` —— 与旧 bsk 数据对比验证（落 `wb_openapi_compare.txt`）。

## 安装
本技能位于仓库 `adanSpring/AdanSMY-Tech` 的 **`skills/wb-ops-fetch/`** 子目录：

```bash
# 1) 取技能包：稀疏检出，只拉该子目录（推荐）
git clone --depth 1 --filter=blob:none --sparse https://github.com/adanSpring/AdanSMY-Tech.git /tmp/adansmy-tech
cd /tmp/adansmy-tech && git sparse-checkout set skills/wb-ops-fetch
cp -R skills/wb-ops-fetch ~/.workbuddy/skills/wb-ops-fetch

# 若不想用 sparse：整仓 clone 后拷目录亦可
#   git clone https://github.com/adanSpring/AdanSMY-Tech.git /tmp/adansmy-tech
#   cp -R /tmp/adansmy-tech/skills/wb-ops-fetch ~/.workbuddy/skills/

# 2) 放好企业 Key（600 权限；运行时从此处读取，不入库）
mkdir -p ~/.workbuddy && install -m 600 <你的 pt_ Key 文件> ~/.workbuddy/wb_openapi.key
```

## 数据目录（可移植）
- 数据文件默认在 `WB_OPS_BASE`（环境变量，缺省 `/Users/ajiang/Documents/AIWork/WorkBuddy/SM工作日常`）：
  - `outputs/个人日常工作台.html` —— 本地页源
  - `outputs/wb_ops_latest.json` —— 最近一次数据
  - `outputs/wb_ops_history.json` —— 每日用量历史
- 在线页发布：`lint_page_quality.py --html` → `import_html.py --node-block-id YDka4TZI3oYhn9yuvxoQJg`（返回 `KS_IMPORT_OK`）。

## 定时
WorkBuddy automation（每天 9:00）调用 `scripts/wb_ops_daily.py` 即可，无需本机常驻服务。
