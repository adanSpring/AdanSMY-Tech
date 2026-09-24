# 自动化任务配置记录（WorkBuddy签到助手 · 每日自动签到）

> 导出时间：2026-09-24；导出来源：WorkBuddy 自动化面板（本机调度器）

## 基本信息

- 名称：WorkBuddy签到助手 · 每日自动签到
- 任务 ID：0fe78e6b-361e-462e-a6a0-731ad3011f50
- 状态：ACTIVE
- 调度：recurring，`FREQ=DAILY;BYHOUR=15;BYMINUTE=0`（每日 15:00；选 15:00 是因为 12h 补跑窗口覆盖到次日 03:00，当天白天开过机就能补签）
- 工作目录（cwds）：`/Users/ajiang/WorkBuddy/2026-09-17-17-12-46`

## 配套系统配置（缺一不可）

1. 包装脚本：`~/.workbuddy/scripts/workbuddy-daily-checkin.sh`（exec 托管 Python 跑 `workbuddy_checkin.py`）
2. 沙箱白名单：`~/.workbuddy/settings.json` →

   ```json
   { "sandbox": { "excludedCommands": ["workbuddy-daily-checkin.sh"] } }
   ```

   登录态目录在沙箱内置 denyRead 列表中，不加白名单自动化会报「未找到本机登录态文件」。
   设置改动非即时生效，首次执行若仍被拦，重跑一次即可。
3. 模型选择（任务级配置，代理设不了，需手动）：自动化 → 该任务 → 选择模型和技能 → 免积分模型优先（如标注「限时免费」的混元系 Hy3），其次选倍率最低的（如 Deepseek-V4-Flash x0.06，以客户端当期标注为准）；**不要留 Auto**。

## 自动化 prompt（原文）

```
执行 WorkBuddy「Buddy 加油站」每日自动签到。

操作步骤：
1. 用 Bash 工具运行以下命令（这是已配置好的包装脚本，请勿改动脚本或另写代码）：
   ~/.workbuddy/scripts/workbuddy-daily-checkin.sh
2. 脚本会读取本机 WorkBuddy 登录态并直接调用官方接口签到，同时尝试派猫猫旅行，最后输出一段 JSON。
3. 根据 JSON 的 status / action / msg / balance 字段，用一句中文向用户汇报结果：
   - action=clicked：签到成功，说明获得积分、连续天数与当前余额
   - action=skip_already_signed：今日已签到（幂等保护），无需处理
   - status=error：签到失败，原样说明 msg 里的原因，并提示检查 WorkBuddy 是否已登录、电脑是否联网
4. 若命令因沙箱限制报"未找到本机登录态文件"，说明 ~/.workbuddy/settings.json 中 sandbox.excludedCommands 的 workbuddy-daily-checkin.sh 条目丢失，请在汇报中明确告知用户需要恢复该项配置。

严格约束：
- 绝不输出、复述或展示任何 accessToken / refreshToken / 推送凭据；脚本输出里 token 已脱敏（形如 eyJhbG...br7A），直接忽略该字段即可。
- 不要修改登录态文件，不要新增或改动脚本，不要用 crontab / launchd。
- 只需一条简洁的中文结论，不要长篇报告。
```

## 调度行为备忘

- 本机调度器驱动（30s 一次 tick），WorkBuddy 进程退出则不跑；睡眠/合盖不算关机，唤醒会自动补扫。
- 补跑窗口 12h：超过判 skip_missed，只补窗口内最近一次。
- 15:00 属非高峰，执行基本准点（09/10 点高峰最多延迟 10 分钟）。
- 脚本自身有幂等保护（skip_already_signed），补跑不会重复签到。
