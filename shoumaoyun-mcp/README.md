# shoumaoyun-mcp（Cloudflare Workers 版）

把「首茂云（云之家）」的 14 个能力以 **远程 MCP（Streamable HTTP）** 暴露出来，供 WorkBuddy
企业控制台登记为自定义连接器后全员使用。

- **无需自有服务器**：跑在 Cloudflare 全球边缘，按请求计费，免费额度足够内部使用。
- **零第三方依赖**：只用 Python 标准库 + Workers 内置 `fetch`。
- **代码可审计**：全部逻辑在本仓库，改完 `git push` + 一条命令即可上线。

> 迁移自本地 stdio 版 `~/.workbuddy/mcp-servers/shoumaoyun/server.py`。
> 逻辑不复制粘贴，而是由 `tools/port_from_stdio.py` 自动移植，见下「代码是怎么来的」。

---

## 1. 目录结构

```
.
├── src/
│   ├── README.md          # 说明 src/entry.py 为什么不入库
│   └── entry.py           # ⭐ Worker 唯一入口（自动拼装产物，别手改，已 gitignore）
├── tools/
│   ├── port_from_stdio.py # stdio 版 → Workers 版的移植器
│   ├── _worker_glue.py    # HTTP/MCP 粘合层源码（被拼进 entry.py）
│   └── smoke_local.py     # 本地冒烟测试，不需要 Cloudflare 账号
├── wrangler.jsonc         # Worker 配置（name / main / python_workers flag）
├── pyproject.toml
└── .dev.vars.example      # 本地凭证模板（复制为 .dev.vars，已 gitignore）
```

> ⚠️ `src/yzj_core.py` 和 `src/entry.py` 都是**生成产物，不入库**（已 gitignore）。
> 克隆后必须先跑一次 `python3 tools/port_from_stdio.py` 生成它们，否则 `pywrangler deploy`
> 会报找不到 `src/entry.py`。详见 `src/README.md`。

## 2. 代码是怎么来的（重要）

`src/entry.py` **不要手改**。Worker 跑在 Pyodide（WebAssembly）里，和本机 CPython 有三处硬差异，
全部由 `tools/port_from_stdio.py` 自动处理：

| 差异 | stdio 版 | Workers 版 |
| --- | --- | --- |
| 出网 | `urllib.request` | `await fetch(...)`（Pyodide 没有 socket） |
| 调用链 | 同步 | `http_json` / `gw_post` / 所有 `tool_*` 级联 `async` |
| 环境变量 | 导入期读 `os.environ` | 模块导入早于 env 绑定 → 改由 `configure(env)` 在每个请求入口**重绑模块级全局** |

改工具逻辑的正确姿势：改 stdio 版那份 `server.py`（默认读
`~/.workbuddy/mcp-servers/shoumaoyun/server.py`，也可用 `--src` 指定），然后

```bash
python3 tools/port_from_stdio.py     # 重新生成 src/yzj_core.py 与 src/entry.py
python3 tools/smoke_local.py         # 冒烟
```

## 3. 部署前：本地冒烟（不需要 Cloudflare 账号）

脚本会伪造一个 `workers` 模块、把 `fetch` 落到本机真实网络，然后真跑一遍 `src/entry.py`
（会真实请求云之家网关）。

```bash
python3 tools/port_from_stdio.py            # 先生成 src/entry.py
cp .dev.vars.example .dev.vars              # 填好 YZJ_EID / YZJ_RES_SECRET
python3 tools/smoke_local.py --env-file .dev.vars
# 或直接用本机 ~/.workbuddy/mcp.json 里的凭证：
python3 tools/smoke_local.py
```

预期输出 `PASS 9 / FAIL 0`。

## 4. 部署

前置：Cloudflare 账号（免费即可）、[uv](https://docs.astral.sh/uv/)、Node 18+。

```bash
# 4.1 登录（会打开浏览器）
npx wrangler login

# 4.2 写入密钥（逐个执行，粘贴值；写入后不可读回）
npx wrangler secret put YZJ_EID
npx wrangler secret put YZJ_RES_SECRET
npx wrangler secret put MCP_TOKEN          # 强烈建议：MCP 访问令牌，自己生成一串随机串
# 可选（不填则对应工具不注册 / 回退）
npx wrangler secret put YZJ_IM_SECRET
npx wrangler secret put YZJ_IM_CURRENT_UID
npx wrangler secret put YZJ_ROBOT_WEBHOOKS
npx wrangler secret put YZJ_APPROVAL_APP_ID
npx wrangler secret put YZJ_APPROVAL_SECRET
npx wrangler secret put YZJ_APP_ID
npx wrangler secret put YZJ_APP_SECRET
npx wrangler secret put YZJ_PUB_ID
npx wrangler secret put YZJ_PUB_SECRET

# 4.3 部署
uvx --from workers-py pywrangler deploy
```

部署成功会给出形如 `https://shoumaoyun-mcp.<你的子域>.workers.dev` 的地址。

想绑自定义域名：Cloudflare 控制台 → Workers → 本服务 → Settings → Domains & Routes → Add
（域名需托管在 Cloudflare）。

## 5. 验证线上

```bash
BASE=https://shoumaoyun-mcp.<你的子域>.workers.dev

# 健康检查（不鉴权）
curl -s $BASE/health

# 列工具（需要令牌）
curl -s -X POST $BASE/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

`/health` 里 `tools` 应为 14（未配审批/待办凭证时是 7），`eid_configured` 应为 `true`。

## 6. 在 WorkBuddy 企业控制台登记

路径：**AI 资源管理 → 连接器管理 → 自定义连接器 → 新建**

| 字段 | 填法 |
| --- | --- |
| 标识 | `enterprise_shoumaoyun`（按平台要求加 `enterprise_` 前缀） |
| 名称 | 首茂云（云之家） |
| 作为 MCP Server | ✅ 勾选 |
| URL | `https://shoumaoyun-mcp.<你的子域>.workers.dev/mcp`（**注意末尾 `/mcp`**） |
| Transport | Streamable HTTP（若只有 SSE 选项，填同一个 URL 通常也可用） |
| API Key | Header 名 `Authorization`，值前缀 `Bearer `，密钥填 `MCP_TOKEN` |
| 适用产品 | WorkBuddy ✅ / 企业智能体 ✅ / CodeBuddy 暂不支持 |
| 下发策略 | allow 交集、deny 并集、**deny 优先** |

保存后让一个同事在会话里问一句「查一下某某的手机号」，能正常回调即生效。

## 7. 更新与回滚

```bash
python3 tools/port_from_stdio.py && python3 tools/smoke_local.py   # 改完先本地验
git push
uvx --from workers-py pywrangler deploy                            # 秒级上线
```

Workers 每次部署都是一个新版本，可在 Cloudflare 控制台 → Deployments 一键回退到上一版本。
`wrangler secret put` 改密钥后需要**重新 deploy** 才会生效。

## 8. 和旧 ECS 版的关系

旧的 HTTP 版跑在阿里云 ECS `123.57.73.153`（`http://123.57.73.153/yzj-mcp/mcp`），
由 systemd + nginx 托管。两边**功能等价、可并存**：

- 建议：先在控制台登记 Cloudflare 版并验证；确认稳定后，再决定要不要停掉 ECS 上的
  `shoumaoyun-mcp.service` 并移除 nginx 的 `yzj-mcp.inc`。
- 停用 ECS 版（在服务器上执行）：
  ```bash
  systemctl stop shoumaoyun-mcp && systemctl disable shoumaoyun-mcp
  # 再从 /usr/local/nginx/conf/conf.d/shoumao-root.conf 里去掉 yzj-mcp.inc 的 include
  /usr/local/nginx/sbin/nginx -t && /usr/local/nginx/sbin/nginx -s reload
  ```

## 9. 已知边界

- **⚠️ 国内访问风险（上线前必须实测）**：调用方是国内的 WorkBuddy 服务，而
  `workers.dev` / Cloudflare 在中国大陆的访问质量不稳定。部署后**务必**在企业网络里实际跑一次
  工具调用再全员开放。若确实不通：① 给 Worker 绑一个自定义域名（通常比 `workers.dev` 稳）；
  ② 或改用国内无服务器（阿里云函数计算 / 腾讯云函数）；③ 或保留 ECS 版。
- **冷启动**：Python Worker 冷启动约 1s 上下，之后同一 isolate 内会复用。
- **Token 缓存**：云之家 `accessToken` 缓存在 isolate 内存里，isolate 被回收会重新换取，
  不影响正确性。
- **并发**：无共享状态，天然并发安全；`_all_persons` 这类翻页操作按 isolate 缓存，不跨请求污染。
- **超时**：Worker 默认的墙钟时间足够；单次云之家调用超时 60s。

## 10. 工具清单

| 工具 | 说明 | 所需凭证 |
| --- | --- | --- |
| `yzj_list_persons` | 分页查全部在职人员 | YZJ_EID + YZJ_RES_SECRET |
| `yzj_get_persons` | 按手机号/openId 查详情（≤100 人） | 同上 |
| `yzj_get_updated_persons` | 增量同步变更人员 | 同上 |
| `yzj_group_info` | 群信息 | + YZJ_IM_SECRET |
| `yzj_group_users` | 群成员列表 | + YZJ_IM_SECRET |
| `yzj_send_group_message` | 群机器人发消息 | YZJ_ROBOT_WEBHOOKS |
| `yzj_send_pub_message` | 公共号单聊 | YZJ_PUB_ID + YZJ_PUB_SECRET |
| `yzj_get_templates` | 审批表单模板 | YZJ_APPROVAL_* |
| `yzj_find_flows` | 查审批流程 | YZJ_APPROVAL_* |
| `yzj_get_flow_status` | 审批状态 | YZJ_APPROVAL_* |
| `yzj_view_form_inst` | 审批单据详情 | YZJ_APPROVAL_* |
| `yzj_create_todo` | 发待办 | YZJ_APP_ID + YZJ_APP_SECRET |
| `yzj_update_todo` | 置已读/已办/删除 | 同上 |
| `yzj_check_todo` | 查待办处理结果 | 同上 |

> 未配置对应凭证时，审批 / 待办类工具不会出现在 `tools/list` 里。
