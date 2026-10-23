# src/

`entry.py` 是 Worker 的部署入口，由仓库根目录的

```bash
python3 tools/port_from_stdio.py
```

自动拼装而成：`yzj_core.py`（移植后的 async 工具逻辑）+ `tools/_worker_glue.py`（HTTP/MCP 粘合层）。

它是**生成产物，不入库**（见 .gitignore），原因：

1. 避免与 stdio 版 `server.py` 两份代码长期漂移；
2. 生成物约 870 行，入库会让 diff 噪音很大。

部署或本地冒烟前跑一次上面的命令即可重新生成。`wrangler.jsonc` 的 `main` 指向 `src/entry.py`，
所以**生成这一步不能跳过**。
