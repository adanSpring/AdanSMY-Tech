# ---------------------------------------------------------------------------
# Worker glue —— 由 tools/port_from_stdio.py 拼接到 src/yzj_core.py 之后，
# 组成自包含的 src/entry.py。不要单独运行本文件。
# ---------------------------------------------------------------------------
from workers import WorkerEntrypoint, Response

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Mcp-Token, Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
}

JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# 部分客户端（含企业网关）会先探测 resources / prompts，返回空列表避免报 -32601
EMPTY_LISTS = {
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}


def _json(obj, status=200):
    headers = dict(JSON_HEADERS)
    headers.update(CORS_HEADERS)
    return Response(json.dumps(obj, ensure_ascii=False), status=status, headers=headers)


def _sse(payload, status=200):
    """按 SSE 帧格式回一条 JSON-RPC 响应（客户端 Accept 含 text/event-stream 时走这条）。"""
    headers = dict(SSE_HEADERS)
    headers.update(CORS_HEADERS)
    body = "event: message\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
    return Response(body, status=status, headers=headers)


def _query_token(url):
    """从 URL 里取 ?token=（不依赖 Request.query，兼容性更稳）。"""
    if "?" not in url:
        return ""
    qs = url.split("?", 1)[1].split("#", 1)[0]
    for part in qs.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k == "token":
                return urllib.parse.unquote(v)
    return ""


def _authorized(request):
    """未配置 MCP_TOKEN 时不鉴权（便于自测），配置后必须 Bearer / X-Mcp-Token / ?token= 三选一。"""
    token = _env("MCP_TOKEN", "")
    if not token:
        return True
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        auth = auth[7:]
    if auth.strip() == token:
        return True
    if (request.headers.get("X-Mcp-Token") or "").strip() == token:
        return True
    return _query_token(request.url).strip() == token


async def dispatch(req):
    """MCP 协议分发：补齐空列表探测 → 交给 yzj_core.handle_request。"""
    method = req.get("method")
    if method in EMPTY_LISTS and req.get("id") is not None:
        return {"jsonrpc": "2.0", "id": req["id"], "result": {EMPTY_LISTS[method]: []}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    return await handle_request(req)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url
        path = url.split("?", 1)[0]
        method = request.method.upper()

        if method == "OPTIONS":
            return Response("", status=204, headers=CORS_HEADERS)

        # 健康检查：不鉴权，方便部署后 curl 冒烟
        if method == "GET" and path.rstrip("/").endswith("/health"):
            configure(self.env)
            return _json({
                "ok": True,
                "service": "shoumaoyun-mcp",
                "transport": "streamable-http",
                "eid_configured": bool(EID),
                "auth_required": bool(_env("MCP_TOKEN", "")),
                "tools": len(build_tools()),
            })

        if not _authorized(request):
            return _json({"error": "unauthorized"}, status=401)

        if method != "POST":
            return _json({"error": "use POST for MCP (streamable-http)"}, status=405)

        configure(self.env)

        try:
            req = await request.json()
        except Exception:
            return _json({"jsonrpc": "2.0", "error": {"code": -32700, "message": "invalid JSON"}},
                         status=400)

        batch = isinstance(req, list)
        payloads = req if batch else [req]

        try:
            results = []
            for item in payloads:
                resp = await dispatch(item)
                if resp is not None:
                    results.append(resp)
        except Exception as exc:  # noqa: BLE001
            return _json({"jsonrpc": "2.0", "id": (payloads[0] or {}).get("id"),
                          "error": {"code": -32603, "message": str(exc)}}, status=500)

        if not results:
            return Response("", status=202, headers=CORS_HEADERS)

        out = results if batch else results[0]
        accept = (request.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept:
            return _sse(out)
        return _json(out)
