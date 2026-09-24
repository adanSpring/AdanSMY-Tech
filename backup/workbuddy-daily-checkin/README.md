# WorkBuddy 每日自动签到 · 本地文件备份

> 同步自本机 `~/.workbuddy/`，对应 WorkBuddy 自动化任务「WorkBuddy签到助手 · 每日自动签到」（每日 15:00）。
> 最近同步：2026-09-24

## 这是什么

WorkBuddy「Buddy 加油站」每日签到的完整本地实现备份：包装脚本 + Python 脚本 + 原始技能目录 + 自动化配置记录。
用途：换机 / 重装 / 误删后的恢复，以及版本留痕。

## 文件清单

| 路径 | 本机来源 | 说明 |
|---|---|---|
| `scripts/workbuddy-daily-checkin.sh` | `~/.workbuddy/scripts/` | 包装脚本（已加入 settings.json 沙箱白名单） |
| `scripts/workbuddy_checkin.py` | `~/.workbuddy/scripts/` | 签到 + 派猫猫旅行主脚本（v3.0.0，纯标准库） |
| `scripts/push_message.py` | `~/.workbuddy/scripts/` | 12 渠道消息推送模块（与主脚本同目录被导入） |
| `docs/automation.md` | 自动化配置导出 | 自动化 prompt、rrule、沙箱配置等恢复所需信息 |
| `skill/totorosir-workbuddy-checkin/` | `~/.workbuddy/skills/@user_3a370331/totorosir-workbuddy-checkin/` | 原始技能目录完整副本（其 scripts/ 与本目录 scripts/ 内容一致） |

## 恢复步骤（换机 / 重装）

1. 把 `scripts/` 下 3 个文件复制到 `~/.workbuddy/scripts/`，并 `chmod +x workbuddy-daily-checkin.sh`。
2. 把 `skill/totorosir-workbuddy-checkin/` 整个目录复制到 `~/.workbuddy/skills/` 下。
3. 在 `~/.workbuddy/settings.json` 的 `sandbox.excludedCommands` 数组中加入 `"workbuddy-daily-checkin.sh"`（缺了它自动化读不到登录态）。
4. 按 `docs/automation.md` 里的 prompt 与 rrule 重建 WorkBuddy 自动化任务；模型按「免积分优先、其次最低倍率」手动选定（代理设不了该字段）。
5. 验证：交互式对话里跑一次 `~/.workbuddy/scripts/workbuddy-daily-checkin.sh`，返回 JSON `status=ok` 即恢复完成。

## 安全说明

- 本备份**不含任何凭据**：登录态文件 `workbuddy-desktop.info`、推送密钥配置 `notify_config.json`、运行日志 `checkin.log` 均不同步（见 `.gitignore`）。
- 脚本内所有 token 输出均已脱敏；推送密钥只存在于本机 `notify_config.json`，换机需重新配置。
