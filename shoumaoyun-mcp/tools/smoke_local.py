#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地冒烟测试：**不需要 Cloudflare 账号**。

思路：用一个「假的 workers 模块」把 `fetch` 落到本机真实网络上，然后直接执行
src/entry.py —— 这样在部署前就能验证工具逻辑、鉴权、SSE 响应都是好的。

用法:
    python3 tools/smoke_local.py                       # 凭证从 ~/.workbuddy/mcp.json 读
    python3 tools/smoke_local.py --env-file .dev.vars  # 或从一个 wrangler 风格的 env 文件读

依赖: Python 3.10+，无需任何第三方包。
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import types
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 假的 workers 模块
class _FetchResp:
    def __init__(self, text):
        self._text = text

    async def text(self):
        return self._text


async def _fetch(url, method="GET", headers=None, body=None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return _FetchResp(r.read().decode("utf-8"))


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}


class WorkerEntrypoint:
    def __init__(self, ctx=None, env=None):
        self.ctx, self.env = ctx, env


class Headers(dict):
    """模拟 Workers Headers.get 的大小写不敏感行为。"""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeRequest:
    def __init__(self, url, method="POST", headers=None, payload=None):
        self.url, self.method = url, method
        self.headers = Headers(headers or {})
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def install_fake_workers():
    mod = types.ModuleType("workers")
    mod.fetch, mod.Response, mod.WorkerEntrypoint = _fetch, Response, WorkerEntrypoint
    sys.modules["workers"] = mod


# ---------------------------------------------------------------- 凭证
def load_env(path=None):
    values = {}
    if path:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
        return values
    cfg_path = os.path.expanduser("~/.workbuddy/mcp.json")
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
            values = dict(cfg["mcpServers"]["shoumaoyun"].get("env") or {})
        except Exception:
            pass
    for key in os.environ:
        if key.startswith("YZJ_"):
            values[key] = os.environ[key]
    return values


# ---------------------------------------------------------------- 断言
OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print("%s %s %s" % ("  PASS" if cond else "! FAIL", name, detail))


async def run(env_values):
    spec = importlib.util.spec_from_file_location("entry", os.path.join(ROOT, "src", "entry.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    env = types.SimpleNamespace(**env_values)
    env.MCP_TOKEN = env_values.get("MCP_TOKEN", "smoke-token")
    worker = entry.Default(None, env)

    print("\n=== 1. 协议层（直连真实云之家 API） ===")
    r = await worker.fetch(FakeRequest("https://local/health", "GET"))
    body = json.loads(r.body)
    check("GET /health 返回 200", r.status == 200, json.dumps(body, ensure_ascii=False))
    check("工具数量为 14", body.get("tools") == 14, "实际 %s" % body.get("tools"))
    check("EID 已注入", body.get("eid_configured") is True)

    good = {"Authorization": "Bearer smoke-token"}
    r = await worker.fetch(FakeRequest("https://local/mcp", "POST", {},
                                       {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    check("无鉴权 POST -> 401", r.status == 401)

    r = await worker.fetch(FakeRequest("https://local/mcp", "POST", good,
                                       {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    names = [t["name"] for t in json.loads(r.body).get("result", {}).get("tools", [])]
    check("Bearer 鉴权 POST -> 200", r.status == 200)
    check("tools/list 返回 yzj_* 工具", len(names) >= 7, ",".join(names[:3]) + " ...")

    print("\n=== 2. 真实调用（会真的请求云之家网关） ===")
    r = await worker.fetch(FakeRequest("https://local/mcp", "POST", good, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "yzj_list_persons", "arguments": {"begin": 0, "count": 2}}}))
    payload = json.loads(r.body)
    text = payload.get("result", {}).get("content", [{}])[0].get("text", "")
    check("yzj_list_persons 调用成功", '"success": true' in text, text[:80].replace("\n", " "))

    r = await worker.fetch(FakeRequest("https://local/mcp", "POST",
                                       {"Authorization": "Bearer smoke-token",
                                        "Accept": "text/event-stream"},
                                       {"jsonrpc": "2.0", "id": 3, "method": "ping"}))
    check("Accept: text/event-stream -> SSE 帧",
          r.headers.get("Content-Type", "").startswith("text/event-stream")
          and r.body.startswith("event: message"),
          r.headers.get("Content-Type", ""))

    r = await worker.fetch(FakeRequest("https://local/mcp", "OPTIONS"))
    check("OPTIONS 预检 -> 204 + CORS", r.status == 204 and
          r.headers.get("Access-Control-Allow-Origin") == "*")

    print("\n=== 结果 ===")
    print("PASS %d / FAIL %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项: %s" % ", ".join(FAIL))
    return 1 if FAIL else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None, help="wrangler 风格的 .dev.vars 文件")
    args = ap.parse_args()

    env_values = load_env(args.env_file)
    if not env_values.get("YZJ_EID") or not env_values.get("YZJ_RES_SECRET"):
        sys.stderr.write("缺少 YZJ_EID / YZJ_RES_SECRET。用 --env-file .dev.vars 指定，"
                         "或确保 ~/.workbuddy/mcp.json 里有 shoumaoyun 配置。\n")
        return 1

    install_fake_workers()
    return asyncio.run(run(env_values))


if __name__ == "__main__":
    sys.exit(main())
