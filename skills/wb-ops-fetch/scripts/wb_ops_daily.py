#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日从 WorkBuddy 企业控制台抓取运维数据，并更新「个人日常工作台」页面。

抓六项：席位剩余 / 总积分使用 / 积分超限人员 / 未启用人员 / 成员角色配置 / 每日用量统计。

「未启用人员」= 已分配席位、本周期 totalUsed = 0 且无任何请求记录的成员
（与「席位剩余」= 未分配的空闲席位不重叠）。注意 hy3 / step-5-preview 等免费模型不扣积分，
只用免费模型的人有请求但积分为 0，不算未启用。
依赖：企业 OpenAPI（pt_ Key，见 ~/.workbuddy/wb_openapi.key），纯后台 HTTP，无需浏览器登录态。

每日用量：usage/member/list 支持任意 timeRange，按天查询即得当日消耗（实测与 UI 一致，
且后端按日期字符串过滤，+08:00 与 UTC 结果相同）。首次运行自动回溯本计费周期每一天建基线，
之后每次只补「缺失日期 + 今天 + 昨天」，落盘 wb_ops_history.json。

用法：
    python3 wb_ops_daily.py            # 抓取并更新页面、回传资料库
    python3 wb_ops_daily.py --dry-run  # 只抓数据落盘，不改页面
    python3 wb_ops_daily.py --backfill # 强制重抓整个计费周期的每日用量
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

# ⭐ 2026-10-26 起：数据抓取改为纯后台 HTTP（企业 OpenAPI · pt_ Key），不再依赖 bsk 浏览器。
# 抓取逻辑在 wb_openapi_fetch.py 的 build_data()（已与旧 bsk 数据逐项对比验证等价）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wb_openapi_fetch import build_data as _openapi_build_data

ENT = "fzrv66luc6ww"
BASE = "/Users/ajiang/Documents/AIWork/WorkBuddy/SM工作日常"
HTML = os.path.join(BASE, "outputs", "个人日常工作台.html")
LATEST = os.path.join(BASE, "outputs", "wb_ops_latest.json")
HIST = os.path.join(BASE, "outputs", "wb_ops_history.json")
DAYS_SHOW = 14          # 页面展示最近多少天
BATCH = 10               # 每日用量每批查几天（一次 evaluate 里串行 fetch）
NODE = "YDka4TZI3oYhn9yuvxoQJg"
LIB = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/plugins/workbuddy-builtin/skills/library"
DEP_ROOT = "1496676308661323604"

ROLES = [
    ("2000000", "平台超级管理员"),
    ("1501864238347243053", "需求技术使用"),
    ("1501502964593625554", "业务使用者"),
    ("1500335435560258081", "子管理员/负责人"),
]

DRY = "--dry-run" in sys.argv
BACKFILL = "--backfill" in sys.argv


def run(args, timeout=180):
    r = subprocess.run([BSK] + args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def ev(expr, sid, timeout=180):
    """在页面里执行 JS，尽量解析成 JSON；失败返回带 _raw 的字典。"""
    out = run(["evaluate", expr, "--session", sid], timeout)
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(out[i:j + 1])
        except Exception:
            pass
    return {"_raw": out[:400]}


def js_get(path):
    return ("fetch('%s',{credentials:'include'}).then(function(r){return r.json()})"
            ".then(function(r){return JSON.stringify(r)})" % path)


SCROLL_COLLECT = (
    "(async()=>{var seen={};var el=document.querySelector('.t-table__content');"
    "for(var i=0;i<40;i++){var trs=document.querySelectorAll('tbody tr');"
    "for(var j=0;j<trs.length;j++){var t=(trs[j].textContent||'').trim();"
    "if(t&&t.indexOf('姓名')!==0&&t.indexOf('批量删除')<0)seen[t]=1}"
    "if(el){el.scrollTop+=400}"
    "await new Promise(function(r){setTimeout(r,150)})}"
    "return JSON.stringify({n:Object.keys(seen).length,first:Object.keys(seen)[0]||''})})()"
)

CLICK_ROLE = (
    "(function(){var bs=document.querySelectorAll('button');var hit=null;"
    "for(var i=0;i<bs.length;i++){if((bs[i].textContent||'').trim()==='%s'){hit=bs[i]}}"
    "if(!hit)return 'notfound';if(hit.scrollIntoView)hit.scrollIntoView();hit.click();return 'ok'})()"
)

# ⚠️ 成员用量接口必须带 timeRange（计费周期），否则 totalUsed 恒为 0 —— 这是之前「超限 0 人」的根因
# all[] 为逐成员明细（姓名 / 一级部门 / 末级部门 / 已用 / 个人限量 / 加速包 / 授权状态），供详情页表格使用
MEMBER_USAGE = (
    "fetch('/console/enterprises/%s/usage/member/list',{method:'POST',credentials:'include',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({timeRange:%s,pageNum:1,pageSize:500,mode:'now-main'})})"
    ".then(function(r){return r.json()}).then(function(r){"
    "var it=(r.data&&r.data.items)||[];var mx=0,who='',over=[],unauth=[],sum=0,cnt=0,arr=[],all=[];"
    "it.forEach(function(x){cnt++;var u=x.totalUsed||0;sum+=u;"
    "var fp=(x.departmentInfo&&x.departmentInfo.fullPath)||'';var sg=fp.split('/');"
    "var rec={n:x.userName||'',d:sg.length>1?sg[1]:'',s:sg.length>1?sg[sg.length-1]:'',"
    "u:Math.round(u*100)/100,l:x.cycleLimit||0,b:Math.round((x.boostPackageUsed||0)*100)/100,a:x.authStatus||''};"
    "all.push(rec);arr.push({name:rec.n,used:rec.u,limit:rec.l});"
    "if(u>mx){mx=u;who=x.userName}"
    "if(u>=%d){over.push({name:x.userName,used:Math.round(u*100)/100,limit:%d,ownLimit:x.cycleLimit||0})}"
    "if(x.authStatus&&x.authStatus!=='authorized'){unauth.push(x.userName)}});"
    "arr.sort(function(a,b){return b.used-a.used});"
    "all.sort(function(a,b){return b.u-a.u});"
    "var top=arr.slice(0,3).map(function(x){return {name:x.name,used:x.used,limit:x.limit}});"
    "return JSON.stringify({count:cnt,totalCount:r.data&&r.data.totalCount,"
    "max:Math.round(mx*100)/100,who:who,top:top,over:over,unauth:unauth,sum:Math.round(sum*100)/100,all:all})})"
)

# ⭐ 每日用量：按天切片查同一接口（后端按日期字符串过滤，时区不影响结果）
#    入参是日期字符串数组，返回每天 {d:日期, sum:当日总消耗, act:当日有用量的人数, top:当日 Top3}
DAILY_USAGE = (
    "(async()=>{var days=%s;var out=[];"
    "for(var i=0;i<days.length;i++){"
    "var s=days[i]+'T00:00:00+08:00',e=days[i]+'T23:59:59+08:00';"
    "var r=await fetch('/console/enterprises/%s/usage/member/list',{method:'POST',credentials:'include',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({timeRange:{startTime:s,endTime:e},pageNum:1,pageSize:500,mode:'now-main'})})"
    ".then(function(x){return x.json()});"
    "var it=(r.data&&r.data.items)||[];var sum=0,act=0,tp=[],mm={};"
    "it.forEach(function(x){var u=x.totalUsed||0;sum+=u;"
    "if(u>0){act++;mm[x.userName||'']=Math.round(u*100)/100;"
    "tp.push({n:x.userName||'',u:Math.round(u*100)/100})}});"
    "tp.sort(function(a,b){return b.u-a.u});"
    "out.push({d:days[i],sum:Math.round(sum*100)/100,act:act,top:tp.slice(0,3),m:mm})}"  # m=逐成员当日用量
    "return JSON.stringify({days:out})})()"          # ⚠️ 必须包一层对象：ev() 只解析单个 {...}
)


def load_hist():
    if os.path.exists(HIST):
        try:
            with open(HIST, encoding="utf-8") as f:
                h = json.load(f)
            if isinstance(h.get("days"), dict):
                return h
        except Exception:
            pass
    return {"days": {}}


def save_hist(h):
    # 存了逐成员每日用量后文件变大 → 用紧凑格式，别 indent
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, separators=(",", ":"))


def need_days(hist, cyc_start, today):
    """要抓的日期：缺失的日期 + 今天 + 昨天（今天会持续增长，昨天可能抓时未满一天）。"""
    try:
        d0 = datetime.strptime(cyc_start, "%Y-%m-%d")
    except Exception:
        d0 = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=29)
    end = datetime.strptime(today, "%Y-%m-%d")
    out, d = [], d0
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        # 老版本只存了 sum/act/top，没有逐成员明细 m → 视为缺失，重抓一次补齐
        if BACKFILL or ds not in hist["days"] or "m" not in (hist["days"][ds] or {}):
            out.append(ds)
        d += timedelta(days=1)
    for extra in (today, (end - timedelta(days=1)).strftime("%Y-%m-%d")):
        if extra not in out and extra >= d0.strftime("%Y-%m-%d"):
            out.append(extra)
    return sorted(out)


def fetch_daily(sid, days):
    """按批抓取每日用量，返回 {date: {sum, act, top}}。"""
    got = {}
    for i in range(0, len(days), BATCH):
        part = days[i:i + BATCH]
        out = ev(DAILY_USAGE % (json.dumps(part), ENT), sid, timeout=300)
        items = (out or {}).get("days") if isinstance(out, dict) else None
        if items:
            for it in items:
                if isinstance(it, dict) and it.get("d"):
                    got[it["d"]] = {"sum": it.get("sum") or 0,
                                    "act": it.get("act") or 0,
                                    "top": it.get("top") or [],
                                    "m": it.get("m") or {}}
        else:
            print("  ! 每日用量批次失败", str(out)[:150])
    return got


def daily_summary(hist, today):
    """把历史整理成页面要用的结构：最近 N 天明细 + 7 日均 / 峰值 / 环比 / 可用天数。"""
    keys = sorted(hist["days"])
    if not keys:
        return None
    show = keys[-DAYS_SHOW:]
    # 开头的空白日（周期刚开始、系统尚未启用）不展示，避免图表左侧一排 0 柱；至少保留 7 天
    cut = 0
    while cut < len(show) - 7 and (hist["days"][show[cut]].get("sum") or 0) == 0:
        cut += 1
    show = show[cut:]
    rows = []
    for k in show:
        v = hist["days"][k]
        rows.append({"d": k, "sum": v.get("sum") or 0, "act": v.get("act") or 0, "top": v.get("top") or []})
    # 今天未满一天 → 均线只用「今天之前」的完整天
    full = [r for r in rows if r["d"] < today]
    last7 = full[-7:]
    avg7 = sum(r["sum"] for r in last7) / float(len(last7)) if last7 else 0
    tot = sum(r["sum"] for r in rows)
    peak = max(rows, key=lambda r: r["sum"]) if rows else None
    prev = None
    if len(full) >= 2:
        prev = {"sum": full[-1]["sum"], "before": full[-2]["sum"]}
    return {"days": rows, "avg7": round(avg7, 1), "sum": round(tot, 1),
            "peak": peak, "lastFull": prev, "have": len(keys), "today": today}


def unused_summary(hist, data, today):
    """近三天未使用：靠 history 里的逐成员每日明细 m 算，不额外请求接口。

    分三档：never（整个计费周期零用量）/ stalled（周期用过、近三天停用）/ todayOnly（今天还没用，不算未使用）
    """
    allm = (data.get("usage") or {}).get("all") or []
    if not allm or not hist.get("days"):
        return None
    t = datetime.strptime(today, "%Y-%m-%d")
    last3 = [(t - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]   # [今天, 昨天, 前天]
    lastuse = {}
    for k in sorted(hist["days"]):
        for n, u in (hist["days"][k].get("m") or {}).items():
            if u:
                lastuse[n] = k

    never, stalled, today_only = [], [], []
    for r in allm:
        n = r.get("n") or ""
        if not n:
            continue
        used = [k for k in last3 if (hist["days"].get(k, {}).get("m") or {}).get(n)]
        lu = lastuse.get(n, "")
        rec = {"n": n, "d": r.get("d") or "", "s": r.get("s") or "",
               "u": r.get("u") or 0, "l": r.get("l") or 0, "last": lu,
               "gap": (t - datetime.strptime(lu, "%Y-%m-%d")).days if lu else None}
        if used:
            # 今天为 0、但前两天有用过 → 只是今天还没开始用
            if last3[0] not in used:
                today_only.append(n)
            continue
        (never if not lu else stalled).append(rec)

    never.sort(key=lambda x: (x["d"], x["s"]))
    stalled.sort(key=lambda x: (-x["u"], x["last"]))
    return {"last3": last3, "never": never, "stalled": stalled,
            "todayOnly": today_only, "zero": len(never) + len(stalled),
            "total": len([r for r in allm if r.get("n")]),
            "days": len(hist["days"])}


def main():
    """抓取 WorkBuddy 企业运维数据（OpenAPI 纯后台 HTTP）并更新「个人日常工作台」页面。

    数据来源：wb_openapi_fetch.build_data()（企业 OpenAPI pt_ Key，纯后台，已与旧 bsk 抓取逐项对比验证等价）。
    用法：python3 wb_ops_daily.py [--dry-run]  （--dry-run 仅落盘 json 不改页面）
    """
    print("WorkBuddy 运维数据抓取（OpenAPI 纯后台 HTTP）…")
    data = _openapi_build_data()
    with open(LATEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("已写入", LATEST)

    if DRY:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    return update_page(data)




def set_kpi(html, label, value, small, foot):
    """同一指标可能同时出现在「首页」与「WorkBuddy 运维」两个页签 → count=0 全量替换。"""
    pat = re.compile(r'(<div class="lb">' + re.escape(label) + r'</div>\s*<div class="v[^"]*">)'
                     r'(.*?)(<small>.*?</small></div>\s*<div class="ft">)(.*?)(</div>)', re.S)
    return pat.sub(lambda m: m.group(1) + value + m.group(3) + foot + m.group(5), html, count=0)


def set_kpi_class(html, label, cls):
    """给某指标的卡片换左侧色条：kpi ok / warn / bad / na（两处副本都改）。"""
    # 卡片上可能带 data-detail 等属性 → 用 [^>]* 吃掉到 '>' 为止，否则加了属性就匹配不上
    pat = re.compile(r'(<div class="kpi)(\s+[a-z]+)?("[^>]*>\s*<div class="lb">' + re.escape(label) + r'</div>)')
    return pat.sub(lambda m: m.group(1) + " " + cls + m.group(3), html, count=0)


def set_role_bar(html, name, count):
    pat = re.compile(r'(<div class="lb">' + re.escape(name) + r'</div><div class="tr"><div class="fl[^"]*" '
                     r'style="width:)([\d.]+)(%"></div></div><div class="vl">)(\d+)(</div>)')
    m = pat.search(html)
    if not m:
        return html
    others = []
    for mn in re.finditer(r'<div class="vl">(\d+)</div>', html):
        others.append(int(mn.group(1)))
    top = max(others) if others else 1

    def rep(mm):
        pct = round(count * 100.0 / top, 1) if top else 0
        return mm.group(1) + str(pct) + mm.group(3) + str(count) + mm.group(5)
    return pat.sub(rep, html, count=1)


# ======================== 明细页渲染（KPI 点击后进入的四张表） ========================

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def num(v, nd=0):
    try:
        x = float(v)
    except Exception:
        return "—"
    return ("{:,.2f}".format(x)) if nd else "{:,}".format(int(round(x)))


def pbar(p):
    p = max(0.0, min(100.0, p or 0.0))
    cls = "bad" if p >= 100 else ("warn" if p >= 80 else "")
    return ('<div style="display:flex;align-items:center;gap:8px">'
            '<div class="barline" style="flex:1"><i class="%s" style="width:%.0f%%"></i></div>'
            '<span class="mono" style="width:40px;text-align:right;font-size:12px;color:var(--tx2)">%.0f%%</span>'
            '</div>' % (cls, p, p))


def card(title, sub, inner, span=False):
    st = ' style="grid-column:1/-1"' if span else ""
    return ('<div class="card"%s><div class="card-h"><h3>%s</h3></div>'
            '<p class="sub" style="padding-left:0">%s</p>%s</div>' % (st, title, sub, inner))


def table(heads, body, tid="", fold=False):
    ths = []
    for i, hd in enumerate(heads):
        if isinstance(hd, tuple):
            ths.append('<th class="sortable" data-col="%d" data-type="%s">%s<span class="ar">↕</span></th>'
                       % (i, hd[1], hd[0]))
        else:
            ths.append("<th>%s</th>" % hd)
    cls = "tbl fold" if fold else "tbl"
    ida = ' id="%s"' % tid if tid else ""      # id 必须落在 <table> 上：JS 用 tBodies[0].rows 取行
    if fold and body:
        # 折叠 = 第 13 行起加 class="more"（CSS 隐藏），配合「显示全部」按钮 + 页面 JS
        segs = [s.strip() for s in body.split("</tr>") if s.strip()]
        body = ("</tr>".join(segs[:12]) + "</tr>"
                + "".join(s.replace("<tr", '<tr class="more"', 1) + "</tr>" for s in segs[12:]))
    return ('<div class="%s"><div class="tbl-scroll"><table%s><thead><tr>%s</tr></thead>'
            '<tbody>%s</tbody></table></div></div>' % (cls, ida, "".join(ths), body))


def toolbar(tid, n, fold=False):
    btn = ('<button type="button" class="more" data-expand="%s">显示全部</button>' % tid) if fold else ""
    return ('<div class="tool"><input type="text" placeholder="搜索姓名 / 部门…" data-filter="%s" data-count="c-%s">'
            '<span class="cnt" id="c-%s">共 %d 条</span><span class="sp"></span>%s</div>'
            % (tid, tid, tid, n, btn))


def dep_of(allm, name):
    for m in allm:
        if m.get("n") == name:
            return m.get("d") or "—"
    return "—"


def detail_seat(data):
    lic = data.get("license") or {}
    mb = data.get("members") or {}
    us = data.get("usage") or {}
    roles = data.get("roles") or {}
    allm = us.get("all") or []
    total = lic.get("total") or 0
    used = lic.get("used") or 0
    left = lic.get("remaining")
    auth = mb.get("authorized")
    un = mb.get("unauthorized")
    # 已分配但零用量 —— 口径与「未启用」KPI 统一：unused3.never
    # （积分 0 **且**无请求记录；只用免费模型的人有请求，不算未启用）
    _never = {x.get("n") for x in ((data.get("unused3") or {}).get("never") or [])}
    idle = [m for m in allm if m.get("n") in _never]
    active = len(allm) - len(idle)                          # 已分配且用过
    pct = used * 100.0 / total if total else 0

    dep = {}
    for m in allm:
        k = m.get("d") or "未分配"
        dep[k] = dep.get(k, 0) + 1
    deps = sorted(dep.items(), key=lambda x: (-x[1], x[0]))
    top = deps[0][1] if deps else 1

    rows = ""
    for k, c in deps[:15]:
        rows += ('<tr><td><b>%s</b></td><td class="mono" data-v="%d">%d</td>'
                 '<td data-v="%.1f">%s</td></tr>'
                 % (esc(k), c, c, c * 100.0 / used if used else 0, pbar(c * 100.0 / top)))

    role_rows = ""
    for name, info in roles.items():
        c = info.get("count")
        if c is None:
            role_rows += '<tr><td>%s</td><td class="ph">未取到</td><td class="ph">—</td></tr>' % esc(name)
        else:
            role_rows += ('<tr><td>%s</td><td class="mono" data-v="%d">%d</td>'
                          '<td><span class="tag %s">%s</span></td></tr>'
                          % (esc(name), c, c, "ok" if info.get("source") == "api" else "warn",
                             "接口" if info.get("source") == "api" else "页面采集"))

    left_cls = "var(--bad)" if (left or 0) <= 0 else ("var(--warn)" if (left or 0) <= 5 else "var(--tx)")
    head = ('<div class="dsum">'
            '<div class="it"><div class="k">席位总数</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">已分配</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">剩余可用</div><div class="v" style="color:%s">%s</div></div>'
            '<div class="it"><div class="k">已启用</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">未启用</div><div class="v" style="color:%s">%s</div></div>'
            '<div class="it"><div class="k">到期日</div><div class="v" style="font-size:14px">%s</div></div>'
            '</div>' % (num(total), num(used), left_cls, num(left), num(active),
                        ("var(--warn)" if idle else "var(--tx)"), num(len(idle)),
                        esc(lic.get("expire") or "—")))

    occ = ('<div style="font-size:12.5px;color:var(--tx2);margin:2px 0 6px">已分配 %s / %s（%.1f%%）</div>%s'
           % (num(used), num(total), pct, pbar(pct)))

    idle_tip = ("其中 %d 人已分配席位却从未使用，是回收席位的优先名单。"
                % len(idle)) if idle else "已分配席位全部在用，无闲置可收。"
    note = ('<div class="note %s"><b>%s</b>%s%s</div>'
            % ("r" if (left or 0) <= 0 else ("w" if (left or 0) <= 5 else "b"),
               "席位已用尽：" if (left or 0) <= 0 else ("席位告急：" if (left or 0) <= 5 else "席位充足："),
               "新增成员前必须先回收闲置席位，否则无法分配。" if (left or 0) <= 5
               else "当前可自由分配，新增成员无需先回收。", idle_tip))

    return (head
            + '<div class="dgrid">'
            + card("席位占用", "按订阅口径统计", occ + note)
            + card("席位构成", "来源：订阅管理 / 席位管理 / 成员用量",
                   table(["项目", ("数量", "num"), "说明"],
                         '<tr><td>席位总数</td><td class="mono" data-v="%d">%s</td><td class="ph">订阅购买总量</td></tr>'
                         '<tr><td>已分配</td><td class="mono" data-v="%d">%s</td><td class="ph">已占用，共 %d 人</td></tr>'
                         '<tr><td>空闲未分配</td><td class="mono" data-v="%d">%s</td><td class="ph">即「席位剩余」，可直接新增</td></tr>'
                         '<tr><td>已启用</td><td class="mono" data-v="%d">%s</td><td class="ph">已分配且本周期有用量</td></tr>'
                         '<tr><td>未启用</td><td class="mono" data-v="%d">%s</td><td class="ph">已分配但零用量，可回收</td></tr>'
                         % (total or 0, num(total), used or 0, num(used), len(allm),
                            left or 0, num(left), active, num(active),
                            len(idle), num(len(idle)))))
            + card("角色占用", "角色可重叠，人数加总大于成员数",
                   table(["角色", ("人数", "num"), "取数方式"], role_rows))
            + card("部门占用 Top 15", "按成员所属一级部门聚合（共 %d 人）" % len(allm),
                   table([("部门", "text"), ("占用席位", "num"), ("占比", "num")], rows), span=True)
            + "</div>")


def detail_quota(data):
    qt = data.get("quota") or {}
    bp = data.get("boost") or {}
    us = data.get("usage") or {}
    allm = us.get("all") or []
    total = qt.get("total") or 0
    used = qt.get("used") or 0
    rem = qt.get("remaining") or 0
    pct = used * 100.0 / total if total else 0

    dsu = {}
    for m in allm:
        k = m.get("d") or "未分配"
        v = dsu.get(k) or {"u": 0.0, "n": 0}
        v["u"] += m.get("u") or 0
        v["n"] += 1
        dsu[k] = v
    dlist = sorted(dsu.items(), key=lambda x: (-x[1]["u"], x[0]))

    dep_rows = ""
    for k, v in dlist[:12]:
        dep_rows += ('<tr><td><b>%s</b></td><td class="mono" data-v="%.2f">%s</td>'
                     '<td class="mono" data-v="%d">%d</td><td class="mono" data-v="%.1f">%s</td></tr>'
                     % (esc(k), v["u"], num(v["u"]), v["n"], v["n"],
                        v["u"] / v["n"] if v["n"] else 0, num(v["u"] / v["n"] if v["n"] else 0)))

    rows = ""
    for i, m in enumerate(allm, 1):
        u = m.get("u") or 0
        l = m.get("l") or 0
        p = u * 100.0 / l if l else 0
        more = ' class="more"' if i > 50 else ""
        rows += ('<tr%s><td class="mono" data-v="%d">%d</td><td><b>%s</b></td><td>%s</td><td>%s</td>'
                 '<td class="mono" data-v="%.2f">%s</td><td class="mono" data-v="%d">%s</td>'
                 '<td data-v="%.1f">%s</td><td class="mono" data-v="%.2f">%s</td></tr>'
                 % (more, i, i, esc(m.get("n") or "—"), esc(m.get("d") or "—"), esc(m.get("s") or "—"),
                    u, num(u), l, num(l), p, pbar(p), m.get("b") or 0, num(m.get("b") or 0)))

    head = ('<div class="dsum">'
            '<div class="it"><div class="k">组织已用</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">总额度</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">剩余</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">用量占比</div><div class="v">%.0f<small>%%</small></div></div>'
            '<div class="it"><div class="k">人均限量</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">周期至</div><div class="v" style="font-size:14px">%s</div></div>'
            '</div>' % (num(used), num(total), num(rem), pct, num(qt.get("perLicense")),
                        esc((qt.get("cycleEnd") or "—")[5:] or "—")))

    occ = ('<div style="font-size:12.5px;color:var(--tx2);margin:2px 0 6px">组织已用 %s / %s（%.1f%%）</div>%s'
           '<div class="note b" style="margin-top:10px">加速包 %s / %s 已用，'
           '待发放 %s 笔。额度 %s 重置，未用完清零。</div>'
           % (num(used), num(total), pct, pbar(pct), num(bp.get("used")), num(bp.get("total")),
              num(bp.get("pendingCount")), esc((qt.get("reset") or "—")[5:] or "—")))

    return (head
            + '<div class="dgrid">'
            + card("额度使用", "周期 %s ~ %s" % (esc((qt.get("cycleStart") or "—")[5:] or "—"),
                                                esc((qt.get("cycleEnd") or "—")[5:] or "—")), occ)
            + card("部门用量 Top 12", "按成员所属一级部门聚合",
                   table([("部门", "text"), ("已用积分", "num"), ("人数", "num"), ("人均", "num")], dep_rows))
            + "</div>"
            + card("逐成员用量明细", "默认按已用降序，前 50 条；支持搜索与点击表头排序",
                   toolbar("t-quota", len(allm), fold=len(allm) > 50)
                   + table([("#", "num"), ("姓名", "text"), ("一级部门", "text"), ("所在部门", "text"),
                            ("已用", "num"), ("个人限量", "num"), ("占个人限量", "num"), ("加速包", "num")],
                           rows, tid="t-quota", fold=len(allm) > 50), span=True))


def detail_over(data):
    """积分超限：口径为「默认每人 2,000」，用量 ≥ 2,000 即计入（不看个人是否被单独调高限量）。"""
    us = data.get("usage") or {}
    qt = data.get("quota") or {}
    allm = us.get("all") or []
    over = sorted(us.get("overLimit") or [], key=lambda x: -(x.get("used") or 0))
    per = int(qt.get("perLicense") or 2000)

    # 临限预警：用量接近默认额度但尚未超过（≥ 2,000 的 80%）
    near = [m for m in allm if per * 0.8 <= (m.get("u") or 0) < per]
    near.sort(key=lambda m: -(m.get("u") or 0))

    over_rows = ""
    for m in over:
        u = m.get("used") or 0
        l = per
        own = m.get("ownLimit") or 0
        over_rows += ('<tr><td><b>%s</b></td><td>%s</td><td class="mono" data-v="%.2f">%s</td>'
                      '<td class="mono" style="color:var(--bad)" data-v="%.2f">+%s</td>'
                      '<td class="mono" data-v="%d">%s</td>'
                      '<td data-v="%.1f">%s</td></tr>'
                      % (esc(m.get("name") or "—"), esc(dep_of(allm, m.get("name"))), u, num(u),
                         u - l, num(u - l), own, (num(own) + (" ★" if own != l else "")) if own else "—",
                         u * 100.0 / l if l else 0,
                         pbar(u * 100.0 / l if l else 0)))

    near_rows = ""
    for m in near[:20]:
        u = m.get("u") or 0
        l = per
        near_rows += ('<tr><td><b>%s</b></td><td>%s</td><td class="mono" data-v="%.2f">%s</td>'
                      '<td class="mono" data-v="%.2f">剩 %s</td>'
                      '<td data-v="%.1f">%s</td></tr>'
                      % (esc(m.get("n") or "—"), esc(m.get("d") or "—"), u, num(u),
                         l - u, num(l - u),
                         u * 100.0 / l if l else 0, pbar(u * 100.0 / l if l else 0)))

    head = ('<div class="dsum">'
            '<div class="it"><div class="k">已超人员</div><div class="v" style="color:%s">%d</div></div>'
            '<div class="it"><div class="k">临限人员</div><div class="v">%d</div></div>'
            '<div class="it"><div class="k">默认额度</div><div class="v">%s</div></div>'
            '<div class="it"><div class="k">组织已用</div><div class="v">%s</div></div>'
            '</div>' % ("var(--bad)" if over else "var(--ok)", len(over), len(near),
                        num(per), num(qt.get("used"))))

    if over:
        body = table([("姓名", "text"), ("部门", "text"), ("已用", "num"),
                      ("超出额度", "num"), ("个人限量", "num"), ("占比", "num")], over_rows)
    else:
        body = ('<div class="empty"><b>本周期无人超过默认额度 %s</b>'
                '下方列出最接近 %s 的成员，提前留意即可</div>' % (num(per), num(per)))

    return (head
            + '<div class="dgrid">'
            + card("超限名单", "判定：本周期用量 ≥ 默认额度 %s（周期 %s ~ %s），不区分个人限量是否被单独调高"
                   % (num(per),
                      esc((qt.get("cycleStart") or "—")[5:] or "—"), esc((qt.get("cycleEnd") or "—")[5:] or "—")),
                   body)
            + card("临限预警（≥80%）", "用量达到默认额度 %s 的 80%% 但尚未超过，%d 人" % (num(per), len(near)),
                   table([("姓名", "text"), ("部门", "text"), ("已用", "num"), ("剩余额度", "num"),
                          ("占比", "num")], near_rows) if near
                   else '<div class="empty"><b>无人接近额度</b>所有成员用量均低于默认额度的 80%</div>')
            + "</div>"
            + '<div class="note %s"><b>处置建议：</b>%s</div>'
            % ("w" if over else "b",
               "默认额度 %s / 人，对确需高频使用的成员可在控制台「成员用量」页单独调高个人限量（表中标 ★），"
               "或等 %s 周期重置；超过额度期间该成员可能无法继续使用。"
               % (num(per), esc((qt.get("reset") or "—")[5:] or "下个")) if over
               else "当前无人超额度，无需处置；保持每周看一次临限名单即可。"))


def detail_unused(data):
    """未启用人员：已分配席位、本周期从未使用（unused3.never）的成员（席位回收优先名单）。"""
    lic = data.get("license") or {}
    qt = data.get("quota") or {}
    us = data.get("usage") or {}
    allm = us.get("all") or []
    # 口径与 unused_notice.py / 页面「一键提醒」一致：周期累计为 0
    idle = list((data.get("unused3") or {}).get("never") or [])
    active = max(len(allm) - len(idle), 0)
    left = lic.get("remaining")
    cycle = "%s ~ %s" % (esc((qt.get("cycleStart") or "—")[5:] or "—"),
                         esc((qt.get("cycleEnd") or "—")[5:] or "—"))

    dep = {}
    for m in idle:
        k = m.get("d") or "未分配"
        dep[k] = dep.get(k, 0) + 1
    deps = sorted(dep.items(), key=lambda x: (-x[1], x[0]))
    top = deps[0][1] if deps else 1
    dep_rows = ""
    for k, c in deps:
        dep_rows += ('<tr><td><b>%s</b></td><td class="mono" data-v="%d">%d</td>'
                     '<td data-v="%.1f">%s</td></tr>'
                     % (esc(k), c, c, c * 100.0 / top, pbar(c * 100.0 / top)))

    rows = ""
    for i, m in enumerate(idle, 1):
        more = ' class="more"' if i > 50 else ""
        rows += ('<tr%s><td class="mono" data-v="%d">%d</td><td><b>%s</b></td><td>%s</td><td>%s</td>'
                 '<td class="mono" data-v="0">0</td><td class="mono" data-v="%d">%s</td>'
                 '<td><span class="tag warn">未启用</span></td></tr>'
                 % (more, i, i, esc(m.get("n") or "—"), esc(m.get("d") or "—"), esc(m.get("s") or "—"),
                    m.get("l") or 0, num(m.get("l") or 0)))

    head = ('<div class="dsum">'
            '<div class="it"><div class="k">未启用</div><div class="v" style="color:%s">%d</div></div>'
            '<div class="it"><div class="k">占已分配</div><div class="v">%d<small>%%</small></div></div>'
            '<div class="it"><div class="k">已启用</div><div class="v">%d</div></div>'
            '<div class="it"><div class="k">可回收席位</div><div class="v">%d</div></div>'
            '</div>' % (("var(--warn)" if idle else "var(--ok)"), len(idle),
                        round(len(idle) * 100.0 / len(allm)) if allm else 0,
                        active, len(idle)))

    return (head
            + '<div class="dgrid">'
            + card("口径说明", "和「席位剩余」不是一回事",
                   '<ul class="plain">'
                   '<li>未启用 = 已分配席位、本周期 <code>totalUsed = 0</code> '
                   '<b>且无任何请求记录</b> 的成员。</li>'
                   '<li>说明：<code>hy3</code> / <code>step-5-preview</code> 等模型完全免费不扣积分，'
                   '只用免费模型的人有请求但积分为 0，<b>不算未启用</b>（按请求记录甄别）。</li>'
                   '<li>席位剩余 = <b>尚未分配出去的空席位</b>（当前 %s 个）；'
                   '两者不重叠：一个是「占着席位没用」，一个是「空着没人」。</li>'
                   '<li>统计周期 %s，接口 <code>usage/member/list</code>。</li>'
                   '</ul>' % (num(left), cycle))
            + card("未启用部门分布", "共 %d 人，按一级部门聚合" % len(idle),
                   table([("部门", "text"), ("人数", "num"), ("占比", "num")], dep_rows) if idle
                   else '<div class="empty"><b>全员均已启用</b>没有零用量成员</div>')
            + "</div>"
            + card("未启用人员名单", "已分配席位、本周期零用量，按部门排序可直接批量核对",
                   (toolbar("t-unused", len(idle), fold=len(idle) > 50)
                    + table([("#", "num"), ("姓名", "text"), ("一级部门", "text"), ("所在部门", "text"),
                             ("本周期已用", "num"), ("个人限量", "num"), "状态"],
                            rows, tid="t-unused", fold=len(idle) > 50)) if idle
                   else '<div class="empty"><b>全员均已启用</b>已分配席位全部有用量</div>', span=True)
            + '<div class="note %s"><b>要做的事：</b>%s</div>'
            % ("w" if idle else "b",
               ("先和部门确认这些人是否真的不用（含新入职未上手的情况），确认后回收释放给待开通人员；"
                "当前空闲席位只剩 %s 个，回收 %d 个即可立刻满足新增需求。"
                % (num(left), len(idle))) if idle
               else "已分配成员全部在用，无需回收。"))


# ======================== 每日用量统计（02 页签主区块） ========================

WD = ["一", "二", "三", "四", "五", "六", "日"]


def wd(ds):
    try:
        return "周" + WD[datetime.strptime(ds, "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def chart_svg(rows, avg, today):
    """纯 SVG 柱状图：无外部依赖，周末浅色、今日高亮，虚线为近 7 日均。"""
    n = len(rows)
    W, H = 700.0, 196.0
    pl, pr, pt, pb = 4.0, 4.0, 20.0, 28.0
    bw = (W - pl - pr) / n
    mx = max([r["sum"] for r in rows] + [avg, 1])
    base = H - pb
    hmax = base - pt
    # 窄屏时整体等比缩小会让标注不可读 → 给个最小宽度 + 横向滚动
    s = ['<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">'
         '<svg viewBox="0 0 %.0f %.0f" style="width:100%%;min-width:560px;height:auto;display:block" '
         'preserveAspectRatio="xMidYMid meet" role="img">' % (W, H)]
    # 横向网格线
    for g in range(5):
        gy = pt + hmax * g / 4.0
        s.append('<line x1="%.0f" y1="%.1f" x2="%.0f" y2="%.1f" stroke="var(--line2)" stroke-width="1"/>'
                 % (pl, gy, W - pr, gy))
    for i, r in enumerate(rows):
        h = r["sum"] / mx * hmax
        x = pl + i * bw + bw * 0.18
        w = bw * 0.64
        isd = r["d"] == today
        wk = 5 if wd(r["d"]) in ("周六", "周日") else 0
        fill = "var(--warn)" if isd else ("#aabbe6" if wk else "var(--pri)")
        t = "%s %s · %s 积分 · 活跃 %d 人" % (r["d"], wd(r["d"]), num(r["sum"], 2), r["act"])
        if r.get("top"):
            t += " · 最高 %s %s" % (r["top"][0]["n"], num(r["top"][0]["u"], 2))
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="%s" opacity="%.2f">'
                 '<title>%s</title></rect>'
                 % (x, base - h, w, max(h, 1.5), fill, 1 if (isd or not wk) else 0.75, esc(t)))
        if bw >= 38 or isd:
            s.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9.5" fill="%s">%s</text>'
                     % (x + w / 2, base - h - 4,
                        "var(--warn)" if isd else "var(--tx3)", num(round(r["sum"]))))
        step = 2 if n > 10 else 1
        if i % step == 0 or i == n - 1:
            s.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10" fill="var(--tx3)">%s</text>'
                     % (x + w / 2, base + 15, r["d"][5:]))
    ay = base - (avg / mx * hmax)
    s.append('<line x1="%.0f" y1="%.1f" x2="%.0f" y2="%.1f" stroke="var(--bad)" stroke-width="1" '
             'stroke-dasharray="5 4" opacity="0.75"/>' % (pl, ay, W - pr, ay))
    s.append("</svg></div>")
    return "".join(s)


def legend(avg):
    """图例单独放一行：画在 SVG 里会和最右侧柱子打架。"""
    it = ('<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px">'
          '<i style="width:9px;height:9px;border-radius:2px;background:%s;display:inline-block"></i>%s</span>')
    dash = ('<span style="display:inline-flex;align-items:center;gap:6px">'
            '<i style="width:16px;height:0;border-top:2px dashed var(--bad);display:inline-block"></i>'
            '近 7 日均 %s</span>' % num(round(avg)))
    return ('<div style="font-size:11.5px;color:var(--tx2);margin:6px 0 2px">'
            + it % ("var(--pri)", "工作日")
            + it % ("#aabbe6", "周末")
            + it % ("var(--warn)", "今日（未满一天）")
            + dash
            + '</div>')


def detail_daily(data):
    dd = data.get("daily") or {}
    rows = dd.get("days") or []
    if not rows:
        return ('<div class="empty"><b>还没有每日用量数据</b>'
                '首次抓取会自动回溯整个计费周期，之后每天补一天</div>')
    qt = data.get("quota") or {}
    rem = qt.get("remaining") or 0
    cyc_end = qt.get("cycleEnd") or ""
    today = dd.get("today") or ""
    avg7 = dd.get("avg7") or 0
    peak = dd.get("peak") or {}
    lf = dd.get("lastFull") or {}
    yd, yb = lf.get("sum"), lf.get("before")

    delta = None
    if yd is not None and yb:
        delta = (yd - yb) * 100.0 / yb
    dtxt, dcls = "—", "var(--tx3)"
    if delta is not None:
        dtxt = ("▲ %.0f%%" if delta >= 0 else "▼ %.0f%%") % abs(delta)
        dcls = "var(--bad)" if delta >= 0 else "var(--ok)"   # 用量上升=红（A 股色感：涨红）

    run_out = int(rem // avg7) if avg7 else 0
    try:
        left_days = (datetime.strptime(cyc_end, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    except Exception:
        left_days = None
    tight = left_days is not None and run_out and run_out < left_days

    head = ('<div class="dsum">'
            '<div class="it"><div class="k">昨日消耗</div><div class="v">%s</div>'
            '<div class="s" style="font-size:11.5px;color:%s;margin-top:2px">环比 %s</div></div>'
            '<div class="it"><div class="k">近 7 日均</div><div class="v">%s</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">不含今日</div></div>'
            '<div class="it"><div class="k">峰值日</div><div class="v" style="font-size:15px">%s</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">%s 积分</div></div>'
            '<div class="it"><div class="k">剩余够用</div><div class="v" style="color:%s">%s</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">按 7 日均，天</div></div>'
            '<div class="it"><div class="k">近 %d 天累计</div><div class="v">%s</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">本周期共 %s 天</div></div>'
            '</div>'
            % (num(yd) if yd is not None else "—", dcls, dtxt,
               num(round(avg7)),
               esc((peak.get("d") or "—")[5:]), num(peak.get("sum")),
               "var(--warn)" if tight else "var(--ok)", num(run_out) if run_out else "—",
               len(rows), num(dd.get("sum")), dd.get("have") or len(rows)))

    # 明细表：最近的在上方。环比必须先正序算，否则倒序遍历会把「比后一天」当成「比前一天」
    seq = sorted(rows, key=lambda r: r["d"])
    chg = {}
    for i, r in enumerate(seq):
        if i == 0:
            continue
        p = seq[i - 1]["sum"]
        chg[r["d"]] = ((r["sum"] - p) * 100.0 / p) if p else None
    trs = ""
    for r in reversed(seq):
        isd = r["d"] == today
        dv = chg.get(r["d"], "na")
        if dv == "na":
            ch = '<td class="ph">—</td>'
        elif dv is None:
            ch = '<td class="mono" style="color:var(--bad);font-weight:600">新增</td>'
        else:
            ch = ('<td class="mono" style="color:%s;font-weight:600">%s %.0f%%</td>'
                  % ("var(--bad)" if dv >= 0 else "var(--ok)", "▲" if dv >= 0 else "▼", abs(dv)))
        t1 = r["top"][0] if r["top"] else None
        trs += ('<tr><td%s>%s <span style="color:var(--tx3);font-size:11.5px">%s</span>%s</td>'
                '<td class="mono" data-v="%.2f"><b>%s</b></td>%s'
                '<td class="mono" data-v="%d">%d</td>'
                '<td>%s</td></tr>'
                % (' style="white-space:nowrap"' if isd else "", r["d"][5:], wd(r["d"]),
                   ' <span class="tag warn">今日</span>' if isd else "",
                   r["sum"], num(r["sum"], 2), ch, r["act"], r["act"],
                   ('<span class="mono">%s</span> %s' % (num(t1["u"], 2), esc(t1["n"]))) if t1
                   else '<span class="ph">无消耗</span>'))

    # 5 列在窄屏会被压成两行 → 给个最小宽度让它横向滚动，行高保持正常
    tbl = table(["日期", ("当日消耗", "num"), "环比", ("活跃人数", "num"), "当日最高"], trs) \
        .replace("<table", '<table style="min-width:460px"', 1)

    if tight:
        note = ('<div class="note w"><b>额度偏紧：</b>剩余 %s 积分，按近 7 日均 %s / 天只够 %d 天，'
                '而周期还剩 %d 天到 %s 重置 —— 需要限流或申请加速包。</div>'
                % (num(rem), num(round(avg7)), run_out, left_days, cyc_end[5:]))
    else:
        note = ('<div class="note b"><b>额度充裕：</b>剩余 %s 积分，按近 7 日均 %s / 天可用 %d 天，'
                '覆盖到 %s 周期重置没问题。</div>'
                % (num(rem), num(round(avg7)), run_out, cyc_end[5:] or "下次"))

    src = ('<div style="font-size:11.5px;color:var(--tx3);margin-top:8px">'
           '口径：按天切片查询 <code>usage/member/list</code>（timeRange = 当日 00:00~23:59），'
           '单位积分；「活跃人数」= 当日消耗 &gt; 0 的成员数；今日为累计到 %s 的部分量。</div>'
           % esc((data.get("fetched_at") or "")[11:16] or "抓取时刻"))

    return head + chart_svg(rows, avg7, today) + legend(avg7) + tbl + note + src


def detail_unused3(data):
    """近三天未使用：02 页签常驻卡片（三档名单 + 处置建议）。"""
    u3 = data.get("unused3") or {}
    never = u3.get("never") or []
    stalled = u3.get("stalled") or []
    today_only = u3.get("todayOnly") or []
    if not never and not stalled:
        return ('<div class="empty"><b>近三天全员都有用量</b>'
                '下结论前先确认今天是完整一天，抓取时刻过早时今天的数据量偏低</div>')
    zero = u3.get("zero") or (len(never) + len(stalled))
    tot = u3.get("total") or 1
    long_stop = [x for x in stalled if (x.get("gap") or 0) >= 7]
    pct = zero * 100.0 / tot if tot else 0

    head = ('<div class="dsum">'
            '<div class="it"><div class="k">近三天未使用</div><div class="v" style="color:var(--bad)">%d</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">占成员 %s 的 %.0f%%</div></div>'
            '<div class="it"><div class="k">周期从未使用</div><div class="v" style="color:var(--bad)">%d</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">可回收 / 需培训</div></div>'
            '<div class="it"><div class="k">用过但停用</div><div class="v" style="color:var(--warn)">%d</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">周期有量，近三天为 0</div></div>'
            '<div class="it"><div class="k">停用 ≥ 7 天</div><div class="v">%d</div>'
            '<div class="s" style="font-size:11.5px;color:var(--tx3);margin-top:2px">重点关注名单</div></div>'
            '</div>'
            % (zero, num(tot), pct, len(never), len(stalled), len(long_stop)))

    rows = ""
    for x in never:
        rows += ('<tr><td><span class="tag bad">未使用</span></td><td><b>%s</b></td>'
                 '<td>%s</td><td>%s</td><td class="mono" data-v="%.2f">%s</td>'
                 '<td class="ph">从未</td><td class="ph">—</td></tr>'
                 % (esc(x["n"]), esc(x["d"] or "—"), esc(x["s"] or "—"), x["u"], num(x["u"], 2)))
    for x in stalled:
        gap = x.get("gap")
        rows += ('<tr><td><span class="tag warn">停用</span></td><td><b>%s</b></td>'
                 '<td>%s</td><td>%s</td><td class="mono" data-v="%.2f">%s</td>'
                 '<td class="mono">%s</td><td class="mono" data-v="%d">%s 天</td></tr>'
                 % (esc(x["n"]), esc(x["d"] or "—"), esc(x["s"] or "—"), x["u"], num(x["u"], 2),
                    esc((x.get("last") or "")[5:]), gap or 0, gap if gap is not None else "—"))

    n = len(never) + len(stalled)
    tid = "t-idle3"
    tbl = (toolbar(tid, n, fold=n > 50)
           + table(["类别", ("姓名", "text"), ("一级部门", "text"), ("所在部门", "text"),
                    ("周期累计", "num"), "最后使用", ("停用天数", "num")],
                   rows, tid=tid, fold=n > 50))

    tip = []
    if never:
        tip.append("先核实 %d 位从未使用者（新入职未上手 / 岗位确实不需要），确认后可回收席位" % len(never))
    if long_stop:
        tip.append("%d 位已连续停用 7 天以上，优先确认是否休假、出差或已离职" % len(long_stop))
    if today_only:
        tip.append("另有 %d 人今天还没用、前两天用过，属正常，不计入" % len(today_only))
    note = ('<div class="note w"><b>要做的事：</b>%s。</div>'
            % "；".join(tip) if tip else "")

    src = ('<div style="font-size:11.5px;color:var(--tx3);margin-top:8px">'
           '口径：取 %s ~ %s 三天的逐成员用量，三天合计为 0 即计入；「周期累计」为本计费周期全部用量；'
           '「最后使用」由每日明细回溯得出，可查范围为本周期已记录的 %d 天。</div>'
           % (esc((u3.get("last3") or ["", "", ""])[2]), esc((u3.get("last3") or ["", "", ""])[0]),
              u3.get("days") or 0))

    return head + tbl + note + src


DETAILS = [("seat", detail_seat), ("quota", detail_quota),
           ("over", detail_over), ("unused", detail_unused)]


def set_unused3(html, body):
    pat = re.compile(r'(<div id="idle3-body">).*?(</div><!--/idle3-->)', re.S)
    if not pat.search(html):
        return html
    return pat.sub(lambda m: m.group(1) + body + m.group(2), html, count=1)


def set_notice_data(html, data):
    """把「未启用提醒」用的名单写成页面内嵌 JSON，供 02 页签卡片与一键按钮读取。"""
    never = list((data.get("unused3") or {}).get("never") or [])
    payload = {
        "day": (data.get("fetched_at") or "")[:10],
        "exclude": ["沈建洪", "沈嘉凯"],
        "members": [{"n": x.get("n") or "", "d": x.get("d") or ""} for x in never],
    }
    js = json.dumps(payload, ensure_ascii=False)
    pat = re.compile(r'(<script type="application/json" id="notice-data">).*?(</script>)', re.S)
    if not pat.search(html):
        return html
    return pat.sub(lambda m: m.group(1) + js + m.group(2), html, count=1)


def set_daily(html, body):
    pat = re.compile(r'(<div id="daily-body">).*?(</div><!--/daily-->)', re.S)
    if not pat.search(html):
        return html
    return pat.sub(lambda m: m.group(1) + body + m.group(2), html, count=1)


def set_detail(html, key, body):
    pat = re.compile(r'(<div id="d-%s-body">).*?(</div><!--/d-%s-->)' % (key, key), re.S)
    if not pat.search(html):
        return html
    return pat.sub(lambda m: m.group(1) + body + m.group(2), html, count=1)


def update_page(data):
    with open(HTML, encoding="utf-8") as f:
        html = f.read()

    lic = data["license"]
    qt = data["quota"]
    mb = data["members"]
    us = data["usage"]
    roles = data["roles"]
    ts = data["fetched_at"]

    html = set_kpi(html, "席位剩余", str(lic.get("remaining")), " / %s" % lic.get("total"),
                   "已用 %s · 到期 %s" % (lic.get("used"), lic.get("expire")))
    # 首页 Hero 胶囊同步席位与更新时间
    html = re.sub(r'(<span class="pill b"><i></i>)席位仅剩 [^<]*(</span>)',
                  lambda m: m.group(1) + "席位仅剩 %s / %s" % (lic.get("remaining"), lic.get("total")) + m.group(2),
                  html, count=0)
    html = set_kpi(html, "总积分使用", "{:,}".format(qt.get("used") or 0),
                   " / %s" % ("%.1f 万" % ((qt.get("total") or 0) / 10000.0)),
                   "剩余 %s · 周期至 %s" % ("{:,}".format(qt.get("remaining") or 0),
                                            (qt.get("cycleEnd") or "")[5:]))
    # ⭐ 口径：统一按「默认每人 2,000」判定，超过即算超限（不看个人是否被单独调高限量）
    over = sorted(us.get("overLimit") or [], key=lambda x: -(x.get("used") or 0))
    over_n = len(over)
    per = int(qt.get("perLicense") or 2000)
    perf = "{:,}".format(per)
    if over_n == 0:
        over_foot = "默认 %s / 人 · 无人超过" % perf
    elif over_n == 1:
        over_foot = "%s %s / %s" % (over[0]["name"], "{:,}".format(int(round(over[0]["used"]))), perf)
    else:
        over_foot = "%s 等 %d 人超过 %s" % (over[0]["name"], over_n, perf)
    html = set_kpi(html, "积分已超人员", str(over_n), " 人", over_foot)

    # 未启用人员 = 已分配席位但本周期从未使用（与「席位剩余」= 空席位互不重叠）
    # 口径与明细页、群通知模板统一：unused3.never（周期累计为 0）
    allm = us.get("all") or []
    idle_n = len((data.get("unused3") or {}).get("never") or [])
    if not allm:
        nu_txt, nu_foot, nu_cls = "—", "逐成员数据未取到", "na"
    elif idle_n == 0:
        nu_txt, nu_foot, nu_cls = "0", "已分配成员全部在用", "ok"
    else:
        nu_txt = str(idle_n)
        nu_foot = "占已分配 %d%% · 可回收" % round(idle_n * 100.0 / len(allm))
        nu_cls = "warn"
    html = set_kpi(html, "未启用人员", nu_txt, " 人", nu_foot)

    # KPI 左侧色条：按健康度着色
    seat_left = lic.get("remaining")
    seat_cls = "na" if seat_left is None else ("bad" if seat_left <= 0 else ("warn" if seat_left <= 5 else "ok"))
    html = set_kpi_class(html, "席位剩余", seat_cls)
    html = set_kpi_class(html, "未启用人员", nu_cls)
    html = set_kpi_class(html, "积分已超人员", "warn" if over_n else "ok")
    used_pct = (qt.get("used") or 0) * 100.0 / (qt.get("total") or 1)
    html = set_kpi_class(html, "总积分使用", "warn" if used_pct >= 85 else "ok")

    for name, info in roles.items():
        if info.get("count") is not None:
            html = set_role_bar(html, name, info["count"])

    html = re.sub(r'每日自动抓取 · 更新于 [\d\-: ]+', '每日自动抓取 · 更新于 %s' % ts, html, count=0)
    # 首页 Hero 第三颗胶囊：显示数据新鲜度
    html = re.sub(r'(<span class="pill c"><i></i>)运维数据[^<]*(</span>)',
                  lambda m: m.group(1) + "运维数据 · %s 更新" % ts[5:16] + m.group(2),
                  html, count=0)
    html = re.sub(r'组织已用 <b>[\d,]+</b> / [\d,]+，占 \d+%。',
                  '组织已用 <b>%s</b> / %s，占 %d%%。' % (
                      "{:,}".format(qt.get("used") or 0),
                      "{:,}".format(qt.get("total") or 0),
                      round((qt.get("used") or 0) * 100.0 / (qt.get("total") or 1))),
                  html, count=1)

    # 用量卡：超限明细 + 成员数
    if over_n == 0:
        detail = "成员级用量：本周期<b>无人超过</b>默认额度 %s。" % perf
    else:
        items = "、".join("%s %s" % (o["name"], "{:,}".format(int(round(o["used"])))) for o in over[:4])
        more = " 等 %d 人" % over_n if over_n > 4 else ""
        detail = "成员级用量：<b>%d 人</b>超过默认额度 %s —— %s%s；额度每月 10-31 重置。" % (
            over_n, perf, items, more)
    html = re.sub(r'(<li id="usage-detail">).*?(</li>)',
                  lambda m: m.group(1) + detail + m.group(2), html, count=1, flags=re.S)
    top = us.get("top") or []
    if top:
        t = top[0]
        top_txt = "用量最高：%s %s，默认额度 %s。" % (t["name"], "{:,}".format(int(round(t["used"]))), perf)
        html = re.sub(r'(<li id="usage-top">).*?(</li>)',
                      lambda m: m.group(1) + top_txt + m.group(2), html, count=1, flags=re.S)
    if mb.get("total") is not None:
        html = re.sub(r'(<span id="mem-n">).*?(</span>)',
                      lambda m: m.group(1) + str(mb["total"]) + m.group(2), html, count=1)
        html = re.sub(r'(<span id="mem-n2">).*?(</span>)',
                      lambda m: m.group(1) + str(mb["total"]) + m.group(2), html, count=1)
        # 静态文案里的「席位 x / y」同步
        html = re.sub(r'席位 \d+ / \d+',
                      '席位 %s / %s' % (mb["total"], lic.get("total") or 0), html, count=0)

    # 首页「AI 工具运营」卡：昨日消耗一目了然
    yd = ((data.get("daily") or {}).get("lastFull") or {}).get("sum")
    if yd is not None:
        html = re.sub(r'(<span[^>]*id="daily-y"[^>]*>).*?(</span>)',
                      lambda m: m.group(1) + "{:,}".format(int(round(yd))) + m.group(2),
                      html, count=0)

    # 每日用量统计（02 页签主区块）：抓不到时保留上一次内容
    if (data.get("daily") or {}).get("days"):
        try:
            html = set_daily(html, detail_daily(data))
        except Exception as e:
            print("  ! 每日用量渲染失败：%s" % e)
    else:
        print("  ! 未取到每日用量，区块保留上次内容")

    # 近三天未使用（02 页签常驻卡）：判定失败时保留上一次内容
    if data.get("unused3"):
        try:
            html = set_unused3(html, detail_unused3(data))
        except Exception as e:
            print("  ! 近三天未使用渲染失败：%s" % e)

    # 未启用提醒名单（02 页签群通知卡 + 一键按钮的数据源）
    try:
        html = set_notice_data(html, data)
    except Exception as e:
        print("  ! 未启用提醒名单写入失败：%s" % e)

    # 明细页：KPI 卡片点击后进入的四张表；抓不到逐成员数据时不覆盖，保留上一次内容
    if us.get("all"):
        for key, fn in DETAILS:
            try:
                html = set_detail(html, key, fn(data))
            except Exception as e:
                print("  ! 明细 %s 渲染失败：%s" % (key, e))
    else:
        print("  ! 未取到逐成员数据，明细页保留上次内容")

    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("页面已更新:", HTML)

    out = subprocess.run(
        ["python3", os.path.join(LIB, "page", "import_html.py"), HTML, "--node-block-id", NODE],
        capture_output=True, text=True, timeout=300)
    print((out.stdout or "").strip()[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
