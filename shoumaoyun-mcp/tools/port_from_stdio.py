#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从本地 stdio 版 MCP Server 自动移植出 Cloudflare Python Workers 版。

用法:
    python3 tools/port_from_stdio.py [--src ~/.workbuddy/mcp-servers/shoumaoyun/server.py]

产出:
    src/yzj_core.py   核心逻辑（async 版，供阅读/diff）
    src/entry.py      Worker 单文件入口 = yzj_core + tools/_worker_glue.py（直接部署这个）

做了三处必要改造（Pyodide 环境限制）:
  1. urllib.request 不可用（无 socket）→ http_json 改 async + await fetch(...)
  2. 级联：所有网关调用 / tool_* / 内部辅助函数一并 async
  3. 模块导入早于 env 绑定 → 环境变量不再在导入期读，改由 configure(env) 重绑模块级全局
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CORE_HEADER = '''# -*- coding: utf-8 -*-
"""
首茂云（云之家）MCP 工具核心 —— Cloudflare Python Workers 版。

本文件由 tools/port_from_stdio.py 从 stdio 版 server.py 自动移植，**不要手改**；
改逻辑请改 stdio 版后重新执行移植脚本。

与 stdio 版的差异（Pyodide 运行环境限制）:
  * urllib.request 不可用（WebAssembly 里没有 socket）→ 出网一律 `await fetch(...)`
  * 因此所有 tool_* 及其内部辅助函数都变成 async
  * 环境变量不在导入期读取，改由 configure(env) 在每个请求入口重绑下面的模块级全局

需要的 Workers bindings（Secret 或 Var，见 README）:
  YZJ_EID / YZJ_RES_SECRET            必填
  YZJ_IM_SECRET                       可选，缺省回退 YZJ_RES_SECRET
  YZJ_IM_CURRENT_UID                  可选，群组接口操作者 openId
  YZJ_ROBOT_WEBHOOKS                  可选，格式 名称=URL|名称=URL
  YZJ_APPROVAL_APP_ID / _EID / _SECRET 可选，智能审批（team 级）
  YZJ_APP_ID / YZJ_APP_SECRET         可选，待办（app 级）
  YZJ_PUB_ID / YZJ_PUB_SECRET         可选，公共号单聊
  MCP_TOKEN                           可选，不填则不鉴权
"""

import json
import os
import time
import random
import string
import hashlib
import urllib.parse

from workers import fetch

'''

CONFIG_BLOCK = '''# ------------------------------------------------------------------ 配置
# Workers 里模块导入早于 env 绑定，所以不在导入期读环境变量；
# 由 glue 在每个请求入口调用 configure(self.env) 重绑下面的模块级全局。
ENV = None

EID = ""
RES_SECRET = ""
IM_SECRET = ""
APPROVAL_APP_ID = ""
APPROVAL_EID = ""
APPROVAL_SECRET = ""
APP_ID = ""
APP_SECRET = ""
PUB_ID = ""
PUB_SECRET = ""
ROBOT_WEBHOOKS = {}


def _env(name, default=""):
    """从 Workers bindings 取值，取不到再退回进程环境变量。"""
    val = None
    try:
        val = getattr(ENV, name, None)
    except Exception:
        val = None
    if val is None and ENV is not None:
        try:
            val = ENV[name]
        except Exception:
            val = None
    if val is None:
        try:
            val = os.environ.get(name)
        except Exception:
            val = None
    return default if val in (None, "") else val


def configure(env):
    """在每个请求入口调用一次：把 Workers 的 Secret/Var 写回模块级全局。"""
    global ENV, EID, RES_SECRET, IM_SECRET, APPROVAL_APP_ID, APPROVAL_EID
    global APPROVAL_SECRET, APP_ID, APP_SECRET, PUB_ID, PUB_SECRET, ROBOT_WEBHOOKS
    ENV = env
    EID = _env("YZJ_EID")
    RES_SECRET = _env("YZJ_RES_SECRET")
    IM_SECRET = _env("YZJ_IM_SECRET") or RES_SECRET
    APPROVAL_APP_ID = _env("YZJ_APPROVAL_APP_ID")
    APPROVAL_EID = _env("YZJ_APPROVAL_EID") or EID
    APPROVAL_SECRET = _env("YZJ_APPROVAL_SECRET")
    APP_ID = _env("YZJ_APP_ID")
    APP_SECRET = _env("YZJ_APP_SECRET")
    PUB_ID = _env("YZJ_PUB_ID")
    PUB_SECRET = _env("YZJ_PUB_SECRET")
    hooks = {}
    for item in _env("YZJ_ROBOT_WEBHOOKS").split("|"):
        item = item.strip()
        if "=" in item:
            name, url = item.split("=", 1)
            hooks[name.strip()] = url.strip()
    ROBOT_WEBHOOKS = hooks


'''

HTTP_JSON = '''async def http_json(url, payload, form=False, timeout=30):
    """发起请求并返回解析后的 JSON（Workers 里用 fetch，没有 urllib.request）。"""
    if form:
        body = urllib.parse.urlencode(payload)
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    else:
        body = json.dumps(payload, ensure_ascii=False)
        headers = {"Content-Type": "application/json; charset=utf-8"}
    resp = await fetch(url, method="POST", headers=headers, body=body)
    return json.loads(await resp.text())

'''

WEBHOOK_POST = '''    resp = await fetch(webhook, method="POST",
                       headers={"Content-Type": "application/json;charset=utf-8"},
                       body=json.dumps(payload, ensure_ascii=False))
    return json.loads(await resp.text())
'''


def slice_body(src):
    start = src.index('EID = os.environ.get("YZJ_EID", "")')
    end = src.index("\ndef main():")
    return src[start:end]


def replace_config(body):
    end = body.index('BASE = "https://www.yunzhijia.com"')
    return CONFIG_BLOCK + body[end:]


def replace_http_json(body):
    start = body.index("def http_json(url, payload, form=False, timeout=30):")
    end = body.index("\ndef get_access_token(")
    return body[:start] + HTTP_JSON + body[end + 1:]


def replace_webhook_post(body):
    start = body.index("    req = urllib.request.Request(webhook, method=\"POST\")")
    end = body.index("    return res\n", start)
    return body[:start] + WEBHOOK_POST + body[end + len("    return res\n"):]


def to_async(body):
    names = ["http_json", "get_access_token", "gw_post", "_current_uid",
             "_all_persons", "_resolve_open_ids", "_group_call", "handle_request"]
    names += re.findall(r"^def (tool_\w+)\(", body, flags=re.M)

    # 1) def -> async def
    body = re.sub(r"^def (%s)\(" % "|".join(names), r"async def \1(", body, flags=re.M)

    # 2) 调用点补 await（跳过 def 声明与已有 await）
    for name in names:
        body = re.sub(r"(?<!def )(?<!await )\b%s\(" % name, "await %s(" % name, body)

    # 3) handle_request 里通过变量调用工具实现
    body = body.replace("result = impl(args)", "result = await impl(args)")

    # 4) 群组接口操作者 openId 也走 bindings
    body = body.replace('os.environ.get("YZJ_IM_CURRENT_UID", "").strip()',
                        '_env("YZJ_IM_CURRENT_UID").strip()')
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=None,
                    help="stdio 版 server.py；默认依次找本机的"
                         " ~/.workbuddy/mcp-servers/shoumaoyun/server.py 和仓库里的"
                         " reference/server_stdio.py")
    args = ap.parse_args()

    candidates = [args.src] if args.src else [
        os.path.expanduser("~/.workbuddy/mcp-servers/shoumaoyun/server.py"),
        os.path.join(ROOT, "reference", "server_stdio.py"),
    ]
    args.src = next((c for c in candidates if c and os.path.exists(c)), "")
    if not args.src:
        sys.stderr.write("找不到源 server.py，用 --src 指定\n")
        return 1
    print("源: %s" % args.src)

    src = open(args.src, encoding="utf-8").read()
    body = slice_body(src)
    body = replace_config(body)
    body = replace_http_json(body)
    body = replace_webhook_post(body)
    body = to_async(body)

    leftover = [ln for ln in body.splitlines()
                if "urllib.request.Request" in ln or "urllib.request.urlopen" in ln]
    if leftover:
        sys.stderr.write("移植失败：仍残留 urllib.request 调用 -> %s\n" % leftover)
        return 1

    core = CORE_HEADER + body.rstrip() + "\n"
    with open(os.path.join(ROOT, "src", "yzj_core.py"), "w", encoding="utf-8") as fh:
        fh.write(core)

    glue = open(os.path.join(HERE, "_worker_glue.py"), encoding="utf-8").read()
    banner = ("# 自动生成，请勿手改：src/yzj_core.py + tools/_worker_glue.py\n"
              "# 重新生成： python3 tools/port_from_stdio.py\n")
    with open(os.path.join(ROOT, "src", "entry.py"), "w", encoding="utf-8") as fh:
        fh.write(banner + core + "\n\n" + glue)

    print("生成完成: src/yzj_core.py (%d 行) / src/entry.py (%d 行)"
          % (core.count("\n") + 1, (banner + core + glue).count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
