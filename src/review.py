#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地服务：HTML 选文清单页 + 参数设置页 + 本地阅读页。

采用「一个进程、一个端口、多页面」：
- /review   清单只显示元信息（不取正文），点「开始总结」才写放行信号；
- /settings 参数设置，按规格动态生成表单，增量写 config/settings.local.json；
           同一页另有抓取黑名单编辑区，独立写 config/blacklist.json；
- /read     只读 out/archive/ 里发布时留存的简报副本，纯本地、不回拉云端。

服务只绑 127.0.0.1。v1.7 起统一为「一个常驻服务、一个端口（7777）」：
- dynamic 模式（CLI 默认）：候选池每次请求动态读取，phase=scan 且没提交过时
  /review 自动出现；点「开始总结」写放行信号后服务**不退出**，
  /read 与 /settings 继续可用，不用换端口。
- 旧模式（显式传 inbox）：启动时固定候选池，收到提交后自动退出，保留兼容。

没有信号，cmd_hydrate 一律拒绝执行；没有超时兜底、没有静默放行（不会「等太久就自动放行」）。
自动模式（配置 auto_summary=1 或 `scan --auto-summary`）下，信号由 scan 代写、
带 `"mode": "auto"` 指纹——不是这里的第二条放行路径，页面也不因此获得任何特权。
阅读页不写任何文件，也不等于授权总结；在设置页改参数也不等于授权。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import archive, blacklist, selection, settings_ui
from .config import inbox_matches, project_path
from .state import load_json


# 服务启动时默认的候选池："" = 信息流那份，否则是按号抓取那份（号名）。
# 实际每**次请求**的 scope 以 ?scope= 为准（见 _Handler.request_scope），
# 没带参数才退回这里，因为同一个常驻服务会先后服务两份简报。
_ACTIVE_SCOPE = ""


def latest_inbox(scope: str | None = None) -> dict | None:
    """最新候选池（不管有没有提交过）。

    提交后清单会收起，但「这批有没有提交」这个事实还得能被问到，
    否则 /api/status 会一直报 submitted=false，调用方会以为人没点。
    """
    scope = _ACTIVE_SCOPE if scope is None else scope
    files = sorted((p for p in project_path("out").glob("inbox-*.json")
                    if inbox_matches(p.name, scope)), reverse=True)
    if not files:
        return None
    try:
        box = load_json(files[0], {})
    except (OSError, ValueError):
        return None
    if not isinstance(box, dict) or not box.get("articles"):
        return None
    if box.get("phase") not in ("scan", "hydrated"):
        return None
    return box


def current_inbox(scope: str | None = None) -> dict | None:
    """动态读最新候选池；只有 scan 阶段且没提交过才算「有待确认清单」。

    每次请求都重读，所以 scan 重建候选池后清单自动出现，提交 / hydrate 之后
    自动收起，全程不需要重启服务，也不会因为服务常驻而重复放行。
    """
    box = latest_inbox(scope)
    if box is None or box.get("phase") != "scan":
        return None
    if selection.read_request(box) is not None:
        return None
    return box


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号简报 · 选文清单</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 20px 48px; background:#f6f7f9; color:#1f2329;
         font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:980px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#667085; font-size:13px; margin-bottom:16px; }
  .bar { position:sticky; top:0; z-index:2; background:#f6f7f9; padding:12px 0;
         border-bottom:1px solid #e4e7ec; display:flex; gap:9px; align-items:center; flex-wrap:wrap; }
  button { border:1px solid #d0d5dd; background:#fff; color:#1f2329; padding:8px 14px;
           border-radius:8px; cursor:pointer; font-size:13px; }
  button:hover { border-color:#98a2b3; }
  button.primary { background:#2563eb; border-color:#2563eb; color:#fff; font-weight:600; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .count { margin-left:auto; color:#475467; font-size:13px; }
  .list { margin-top:14px; background:#fff; border:1px solid #e4e7ec; border-radius:10px; overflow:hidden; }
  .row { display:flex; gap:12px; padding:13px 16px; border-bottom:1px solid #f0f1f3; align-items:flex-start; }
  .row:last-child { border-bottom:0; }
  .row:hover { background:#fafbfc; }
  .row input { margin-top:4px; width:16px; height:16px; flex:none; }
  .idx { color:#98a2b3; width:26px; flex:none; text-align:right; font-variant-numeric:tabular-nums; }
  .main { flex:1; min-width:0; }
  .title { font-size:15px; font-weight:600; color:#101828; text-decoration:none; }
  .title:hover { color:#2563eb; text-decoration:underline; }
  .meta { color:#667085; font-size:12px; margin-top:4px; }
  .tag { display:inline-block; padding:1px 8px; border-radius:99px; background:#eef2ff; color:#3538cd; }
  .msg { margin-top:14px; padding:10px 14px; border-radius:8px; font-size:13px; display:none; }
  .msg.ok { display:block; background:#ecfdf3; color:#027a48; border:1px solid #abefc6; }
  .msg.err { display:block; background:#fef3f2; color:#b42318; border:1px solid #fecdca; }
  .note { margin-top:18px; color:#667085; font-size:12px; line-height:1.8; }
  .note b { color:#344054; }
</style>
</head>
<body>
<div class="wrap">
  <h1>公众号简报 · 选文清单</h1>
  <div class="sub" id="sub"></div>
  <div class="bar">
    <button id="all">全选</button>
    <button id="none">全不选</button>
    <button id="save">保存选择</button>
    <button id="submit" class="primary">开始总结</button>
    <span class="count" id="count"></span>
  </div>
  <div class="list" id="list"></div>
  <div class="msg" id="msg"></div>
  <div class="note">
    这里只列元信息，未勾选的文章永远不会取正文。<br>
    「保存选择」只记录勾了哪些；<b>只有点「开始总结」才算授权</b>，之后脚本才会取正文并生成简报。<br>
    没有截止时间，不点就一直等着，不会自动放行。
  </div>
</div>
<script>
const DATA = __DATA__;
const list = document.getElementById('list');
// 这份清单属于哪个 scope（""=信息流那份，否则是某个号的精选简报）。
// 保存 / 提交必须带回去，否则常驻服务会当成另一个 scope 处理。
function query() {
  return '?scope=' + encodeURIComponent(DATA.scope || '');
}

DATA.articles.forEach(a => {
  const row = document.createElement('div'); row.className = 'row';
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.dataset.i = a.index; cb.checked = a.selected;
  const idx = document.createElement('span'); idx.className = 'idx'; idx.textContent = a.index;
  const main = document.createElement('div'); main.className = 'main';
  const link = document.createElement('a');
  link.className = 'title'; link.href = a.url; link.target = '_blank'; link.rel = 'noopener';
  link.textContent = a.title;
  const meta = document.createElement('div'); meta.className = 'meta';
  const tag = document.createElement('span'); tag.className = 'tag';
  tag.textContent = a.source || '未知来源';
  meta.append(tag, document.createTextNode(' · ' + (a.published_at || '时间未知')));
  main.append(link, meta);
  row.append(cb, idx, main);
  list.append(row);
});

document.getElementById('sub').textContent =
  DATA.date + ' · 候选 ' + DATA.articles.length + ' 篇 · 生成于 ' + DATA.generated_at;

const boxes = [...document.querySelectorAll('input[data-i]')];
const btnSave = document.getElementById('save');
const btnSubmit = document.getElementById('submit');
const picked = () => boxes.filter(x => x.checked).map(x => Number(x.dataset.i));

function update() {
  document.getElementById('count').textContent =
    '已选 ' + picked().length + ' / ' + DATA.articles.length + ' 篇';
  btnSubmit.disabled = DATA.submitted || picked().length === 0;
  btnSave.disabled = DATA.submitted;
}
function msg(text, ok) {
  const m = document.getElementById('msg');
  m.textContent = text; m.className = 'msg ' + (ok ? 'ok' : 'err');
}
async function post(path, body) {
  // 常驻服务按 ?scope= 决定看哪份候选池，提交 / 保存都要把这个参数带回去
  const r = await fetch(path + query(), {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok || !j.ok) throw new Error(j.error || '请求失败');
  return j;
}

boxes.forEach(b => b.addEventListener('change', update));
document.getElementById('all').onclick = () => { boxes.forEach(b => b.checked = true); update(); };
document.getElementById('none').onclick = () => { boxes.forEach(b => b.checked = false); update(); };
btnSave.onclick = async () => {
  try {
    const j = await post('/api/save', {indices: picked()});
    msg('已保存 ' + j.selected + ' 篇。还没放行总结，要继续勾或点「开始总结」。', true);
  } catch (e) { msg(e.message, false); }
};
btnSubmit.onclick = async () => {
  const n = picked().length;
  if (!confirm('确定开始总结这 ' + n + ' 篇吗？\n提交后脚本会取正文并生成简报，这一批就不能再改选文了。')) return;
  btnSubmit.disabled = true;
  try {
    const j = await post('/api/submit', {indices: picked()});
    msg('已提交 ' + j.selected + ' 篇。服务继续运行，简报生成后会留在「本地阅读」。', true);
    document.querySelectorAll('button').forEach(b => b.disabled = true);
  } catch (e) { msg(e.message, false); btnSubmit.disabled = false; }
};
if (DATA.submitted) {
  msg('这一批已经提交过了，按钮已锁定。', true);
  document.querySelectorAll('button, input').forEach(x => x.disabled = true);
}
update();
</script>
</body>
</html>
"""


_NAV_CSS = """<style>
  .wdnav { background:#2c2c2a; color:#fff; padding:0 20px; display:flex; gap:4px;
           align-items:center; height:44px; position:sticky; top:0; z-index:5; }
  .wdnav .brand { font-size:13px; color:#a9ada6; margin-right:14px; letter-spacing:.3px; }
  .wdnav a { color:#d8dbd4; text-decoration:none; font-size:13px; padding:6px 13px;
             border-radius:7px; }
  .wdnav a:hover { background:#3d3d39; color:#fff; }
  .wdnav a.on { background:#2563eb; color:#fff; font-weight:600; }
  body { padding-top:0 !important; }
  .wrap { padding-top:26px; }
</style>"""


def nav_html(active: str, has_review: bool | None = None,
             scope: str | None = None) -> str:
    """三页共用的顶部导航。**三个入口永远都在**，与候选池状态无关。

    曾把「选文清单」绑在 `has_review` 上：只有 /review 页硬编码 True，
    于是从清单页跳到 /settings（没带 ?scope=，退回启动 scope 后找不到
    按号抓取那份候选池）时，导航就只剩两项，用户以为页面回不去了。
    「一个服务、一个端口、三页同在」是产品约定，显隐不该由数据决定；
    没有待确认清单时 /review 本来就会给提示页。

    `has_review` 只为兼容旧调用保留，不参与渲染。`scope` 不为 None 时，
    每个链接都带 `?scope=`（空串也带），保证从任意一页点回清单不会丢 scope。
    """
    q = "" if scope is None else "?scope=" + quote(scope or "")
    items = [("review", "/review" + q, "选文清单"),
             ("grab", "/grab" + q, "本轮文章"),
             ("read", "/read" + q, "本地阅读"),
             ("settings", "/settings" + q, "参数设置")]
    links = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{text}</a>'
        for key, href, text in items)
    return _NAV_CSS + (
        '<nav class="wdnav"><span class="brand">公众号简报 · 本地控制台</span>'
        + links + '</nav>')


NO_REVIEW_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号简报 · 选文清单</title>
<style>
  :root { color-scheme: light; }
  body { margin:0; padding:28px 20px 48px; background:#f6f7f9; color:#1f2329;
         font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:760px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 8px; }
  p { color:#475467; }
  a { color:#2563eb; }
</style>
</head>
<body>
__NAV__
<div class="wrap">
  <h1>选文清单</h1>
__AUTO__
  <p>当前没有待确认的选文清单：可能本轮已经提交，或者还没跑 scan。</p>
  <p>选文清单、本地阅读、参数设置始终是<b>同一个服务、同一个端口</b>。
     跑完 scan 后刷新本页，清单会自动出现；提交后服务不会关闭，
     简报会留在「本地阅读」里。</p>
</div>
</body>
</html>
"""


def _auto_notice(scope: str | None = None) -> str:
    """自动模式「已放行、还没 hydrate」时在清单页补一句说明。

    正常情况下清单在提交后就收起；自动模式本来没人看页面，这里只是让用户
    自己打开 /review 时不会误以为「扫描没出清单」。
    """
    box = latest_inbox(scope)
    if box is None or box.get("phase") != "scan":
        return ""
    req = selection.read_request(box)
    if not req or req.get("mode") != "auto":
        return ""
    at = escape(str(req.get("requested_at") or ""))
    return ('<p style="color:#b54708;background:#fffaeb;border:1px solid #fedf89;'
            'border-radius:8px;padding:10px 12px;">⚙ 本轮已按<b>自动模式</b>放行 '
            f'{int(req.get("selected") or 0)} 篇（{at}，未经人工确认），'
            '下一步直接跑 <code>hydrate</code>。</p>')


def build_no_review_page(scope: str | None = None) -> str:
    return (NO_REVIEW_PAGE.replace("__NAV__", nav_html("", scope=scope or ""), 1)
            .replace("__AUTO__", _auto_notice(scope), 1))


GRAB_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号简报 · 本轮文章</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 20px 60px; background:#f6f7f9; color:#1f2329;
         font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#667085; font-size:13px; margin-bottom:18px; }
  .count { display:inline-block; background:#fff; border:1px solid #e4e7ec;
           border-radius:8px; padding:8px 14px; margin-bottom:14px; font-size:13px; color:#475467; }
  .count b { color:#101828; font-size:16px; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #e4e7ec;
          border-radius:10px; overflow:hidden; }
  th, td { padding:11px 14px; text-align:left; border-bottom:1px solid #f0f1f3;
           font-size:13px; vertical-align:top; }
  th { background:#f9fafb; color:#475467; font-weight:600; white-space:nowrap; }
  tr:last-child td { border-bottom:0; }
  tr:hover td { background:#fafbfc; }
  .idx { color:#98a2b3; width:40px; font-variant-numeric:tabular-nums; }
  .t { font-weight:600; color:#101828; text-decoration:none; }
  .t:hover { color:#2563eb; text-decoration:underline; }
  .src { color:#667085; font-size:12px; }
  .empty { padding:40px; text-align:center; color:#667085; background:#fff;
           border:1px solid #e4e7ec; border-radius:10px; }
  .note { margin-top:18px; color:#667085; font-size:12px; line-height:1.9; }
  .note b { color:#344054; }
</style>
</head>
<body>
<div class="wrap">
  <h1>公众号简报 · 本轮文章</h1>
  <div class="sub" id="sub"></div>
  <div class="count" id="count"></div>
  <table><thead><tr><th>#</th><th>标题</th><th>公众号</th><th>发布时间</th></tr></thead>
    <tbody id="rows"></tbody></table>
  <div class="note">
    本页<b>只读</b>：只列本轮要总结的新文章，已经总结过或处理过的文章不会出现在这里。<br>
    要开始总结请到 <a href="/review?scope=__SCOPE__">选文清单</a> 勾选后点「开始总结」。
  </div>
</div>
<script>
const D = __DATA__;
const A = D.articles || [];
const $ = id => document.getElementById(id);
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = String(txt);
  return e;
}
$('sub').textContent = D.empty
  ? '这一轮还没有可总结的文章。'
  : '日期 ' + D.date + ' · 生成于 ' + D.generated_at;
$('count').appendChild(el('span', '', '本轮 '));
$('count').appendChild(el('b', '', A.length));
$('count').appendChild(el('span', '', ' 篇'));
const box = $('rows');
A.forEach((r, i) => {
  const tr = el('tr');
  tr.appendChild(el('td', 'idx', i + 1));
  const td = el('td');
  const u = String(r.url || '');
  if (/^https?:\/\//.test(u)) {
    const a = el('a', 't', r.title);
    a.href = u; a.target = '_blank'; a.rel = 'noopener';
    td.appendChild(a);
  } else { td.appendChild(el('span', 't', r.title)); }
  tr.appendChild(td);
  tr.appendChild(el('td', 'src', r.source || '（无公众号名）'));
  tr.appendChild(el('td', 'src', r.published_at || '时间未知'));
  box.appendChild(tr);
});
if (!A.length) {
  const tr = el('tr'), td = el('td', 'empty', '这一轮没有新文章');
  td.colSpan = 4; tr.appendChild(td); box.appendChild(tr);
}
</script>
</body>
</html>
"""


def latest_grab_review(scope: str = "") -> dict | None:
    """当前 scope 下最新一份抓取明细；没有就返回 None（页面给空态）。"""
    files = sorted(project_path("out").glob("_grab_review-*.json"), reverse=True)
    for f in files:
        data = load_json(f, {})
        if isinstance(data, dict) and (data.get("scope") or "") == (scope or ""):
            return data
    return None


def build_grab_page(*, scope: str = "") -> str:
    """`/grab`：只列本轮候选池里的新文章（纯只读）。

    用户 2026-11-09 拍板：页面上**不出现**已总结过 / 已投递 / 被排除的文章，
    也不做折叠区。所以在组装 HTML 数据时就把非 `in_inbox` 的行全部丢掉，
    连内嵌 JSON 里都不带重复条目；明细数据文件本身仍保留全量分类供内部查账。
    """
    data = latest_grab_review(scope) or {}
    rows = [r for r in (data.get("articles") or [])
            if r.get("status") in (None, "in_inbox")]
    payload = {
        "date": data.get("date") or "",
        "generated_at": data.get("generated_at") or "",
        "empty": bool(data.get("empty")) or not rows,
        "articles": [{"title": r.get("title") or "（未取到标题）",
                      "url": r.get("url") or "",
                      "source": r.get("source") or "",
                      "published_at": r.get("published_at") or ""}
                     for r in rows],
    }
    raw = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (GRAB_PAGE.replace(
                "<body>", "<body>" + nav_html("grab", scope=scope), 1)
            .replace("__DATA__", raw)
            .replace("__SCOPE__", quote(scope or "")))


SETTINGS_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号简报 · 参数设置</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 20px 70px; background:#f6f7f9; color:#1f2329;
         font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:900px; margin:0 auto; padding-top:26px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#667085; font-size:13px; margin-bottom:16px; line-height:1.9; }
  .sub b { color:#344054; }
  .state { display:inline-block; padding:2px 9px; border-radius:99px; font-size:12px;
           background:#ecfdf3; color:#027a48; border:1px solid #abefc6; }
  .state.warn { background:#fffaeb; color:#b54708; border-color:#fedf89; }
  .group { margin-top:18px; background:#fff; border:1px solid #e4e7ec; border-radius:10px;
           overflow:hidden; }
  .group h2 { font-size:13px; margin:0; padding:10px 16px; background:#f9fafb;
              border-bottom:1px solid #e4e7ec; color:#475467; letter-spacing:.5px; }
  .field { display:flex; gap:16px; padding:12px 16px; border-bottom:1px solid #f0f1f3;
           align-items:center; }
  .field:last-child { border-bottom:0; }
  .field.dirty { background:#f0f9ff; }
  .lab { width:270px; flex:none; }
  .lab .name { font-weight:600; color:#101828; }
  .lab .key { font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:#98a2b3; }
  .lab .hint { font-size:12px; color:#667085; margin-top:2px; }
  .ctl { flex:1; display:flex; gap:7px; align-items:center; }
  input[type=text], input[type=number] { width:100%; max-width:230px; padding:7px 10px;
         border:1px solid #d0d5dd; border-radius:7px; font-size:13px; background:#fff;
         color:#1f2329; font-family:inherit; }
  input.coord { max-width:100px; }
  input:focus { outline:2px solid #b2ccff; border-color:#2563eb; }
  .unit { font-size:12px; color:#98a2b3; }
  .bar { position:sticky; bottom:0; margin-top:18px; padding:12px 16px; background:#fff;
         border:1px solid #e4e7ec; border-radius:10px; display:flex; gap:9px;
         align-items:center; flex-wrap:wrap; box-shadow:0 -3px 12px rgba(16,24,40,.05); }
  button { border:1px solid #d0d5dd; background:#fff; color:#1f2329; padding:8px 14px;
           border-radius:8px; cursor:pointer; font-size:13px; }
  button:hover { border-color:#98a2b3; }
  button.primary { background:#2563eb; border-color:#2563eb; color:#fff; font-weight:600; }
  button.danger { color:#b42318; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .msg { margin-left:auto; font-size:13px; color:#475467; }
  .msg.ok { color:#027a48; } .msg.err { color:#b42318; }
  .note { margin-top:16px; color:#667085; font-size:12px; line-height:1.9; }
  .note b { color:#344054; }
  textarea { width:100%; min-height:130px; padding:9px 11px; border:1px solid #d0d5dd;
             border-radius:7px; font:13px/1.8 ui-monospace,SFMono-Regular,Menlo,
             "PingFang SC",monospace; background:#fff; color:#1f2329; resize:vertical; }
  textarea:focus { outline:2px solid #b2ccff; border-color:#2563eb; }
  .blbody { padding:14px 16px 16px; }
  .blhint { font-size:12px; color:#667085; line-height:1.9; margin-bottom:10px; }
  .blhint b { color:#344054; }
  .blbar { display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  .blbar .msg { margin-left:0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>参数设置</h1>
  <div class="sub" id="sub"></div>
  <div id="form"></div>
  <div class="group blcard">
    <h2>抓取黑名单 · 整号排除</h2>
    <div class="blbody">
      <div class="blhint">一行一个公众号名。<b>整号排除</b>：命中后不进选文清单、不取正文、不总结、不推送。
        匹配时忽略空格 / 全半角 / 英文大小写，<b>精确相等才命中</b>（不会误伤名字相近的号）。
        保存写 <b>config/blacklist.json</b>，下一次 scan 生效；不影响台账，也不等于授权总结。</div>
      <textarea id="bl-text" spellcheck="false" placeholder="例如：中汇人寿"></textarea>
      <div class="blbar">
        <button id="bl-save" class="primary">保存黑名单</button>
        <span class="unit" id="bl-count"></span>
        <span class="msg" id="bl-msg"></span>
      </div>
    </div>
  </div>
  <div class="bar">
    <button id="undo">撤销改动</button>
    <button id="reset" class="danger">恢复默认</button>
    <button id="save" class="primary">保存设置</button>
    <span class="msg" id="msg"></span>
  </div>
  <div class="note">
    改动只写 <b>config/settings.local.json</b>（已 Git 忽略），主配置 <b>config/settings.json</b> 不动；
    写盘前自动生成 <b>.bak</b> 备份。<b>保存后下一条命令即生效</b>，不用重启服务。<br>
    令牌内容<b>不会</b>出现在这个页面，也不在这里修改；只显示是否已配置。<br>
    命令行参数优先级更高：例如 <b>scan --max-articles 10</b> / <b>scan --days 1</b> 会临时覆盖本页的「抓取篇数上限」/「抓取窗口天数」，只影响那一次运行。<br>
    「抓取范围 = 1（最近 N 天全部）」时，抓取篇数上限只是<b>兜底值</b>：程序会一直往下滚，滚出这个窗口为止（默认 N=2：今天 + 昨天）。
  </div>
</div>
<script>
let DATA = __SETTINGS_DATA__;
const form = document.getElementById('form');
const btnSave = document.getElementById('save');
const msgEl = document.getElementById('msg');

function msg(text, ok) {
  msgEl.textContent = text;
  msgEl.className = 'msg ' + (ok === true ? 'ok' : ok === false ? 'err' : '');
}
function val(k) {
  const f = document.querySelector('[data-k="' + CSS.escape(k) + '"]');
  const s = DATA.specs.find(x => x.key === k);
  if (!f) return DATA.values[k];
  if (s.kind === 'coords') {
    return [Number(f.querySelectorAll('input')[0].value),
            Number(f.querySelectorAll('input')[1].value)];
  }
  const inp = f.querySelector('input');
  if (s.kind === 'int') return Number(inp.value);
  if (s.kind === 'float') return Number(inp.value);
  if (s.kind === 'bool') return inp.checked;
  return inp.value;
}
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function changes() {
  const out = {};
  DATA.specs.forEach(s => {
    if (!same(val(s.key), DATA.values[s.key])) out[s.key] = val(s.key);
  });
  return out;
}
function refreshDirty() {
  const ch = changes();
  form.querySelectorAll('.field').forEach(f => {
    f.classList.toggle('dirty', Object.prototype.hasOwnProperty.call(ch, f.dataset.field));
  });
  btnSave.disabled = Object.keys(ch).length === 0;
  return ch;
}
function renderForm() {
  form.innerHTML = '';
  DATA.groups.forEach(g => {
    const items = DATA.specs.filter(s => s.group === g);
    if (!items.length) return;
    const box = document.createElement('div'); box.className = 'group';
    const h = document.createElement('h2'); h.textContent = g; box.append(h);
    items.forEach(s => {
      const f = document.createElement('div');
      f.className = 'field'; f.dataset.field = s.key;
      const lab = document.createElement('div'); lab.className = 'lab';
      lab.innerHTML = '<div class="name"></div><div class="key"></div><div class="hint"></div>';
      lab.querySelector('.name').textContent = s.label;
      lab.querySelector('.key').textContent = s.key;
      lab.querySelector('.hint').textContent = s.hint;
      const ctl = document.createElement('div'); ctl.className = 'ctl';
      ctl.dataset.k = s.key;
      const v = DATA.values[s.key];
      if (s.kind === 'coords') {
        [0, 1].forEach(i => {
          const inp = document.createElement('input');
          inp.type = 'number'; inp.className = 'coord'; inp.min = s.lo; inp.max = s.hi;
          inp.step = 1; inp.value = Array.isArray(v) ? v[i] : 0;
          inp.addEventListener('input', refreshDirty);
          ctl.append(inp);
        });
        const u = document.createElement('span'); u.className = 'unit'; u.textContent = 'x, y';
        ctl.append(u);
      } else {
        const inp = document.createElement('input');
        if (s.kind === 'int' || s.kind === 'float') {
          inp.type = 'number'; inp.min = s.lo; inp.max = s.hi;
          inp.step = s.kind === 'int' ? 1 : 0.1; inp.value = v;
        } else if (s.kind === 'bool') {
          inp.type = 'checkbox'; inp.checked = !!v;
        } else { inp.type = 'text'; inp.value = v == null ? '' : v; }
        inp.addEventListener('input', refreshDirty);
        inp.addEventListener('change', refreshDirty);
        ctl.append(inp);
      }
      f.append(lab, ctl); box.append(f);
    });
    form.append(box);
  });
  const t = DATA.token || {};
  const st = t.configured ? '已配置（' + t.source + '）' : '未配置';
  document.getElementById('sub').innerHTML =
    '当前生效值来自主配置 + 本地覆盖层。已覆盖 <b>' + (DATA.overrides || []).length +
    '</b> 项 · 令牌状态 <span class="state ' + (t.configured ? '' : 'warn') + '">' +
    st + '</span>';
  refreshDirty();
}
async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok || !j.ok) throw new Error(j.error || '请求失败');
  return j;
}
async function refresh() {
  const r = await fetch('/api/settings/get');
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '读取失败');
  DATA = j; renderForm();
}
btnSave.onclick = async () => {
  const ch = changes(), n = Object.keys(ch).length;
  if (!n) return;
  if (!confirm('确定保存这 ' + n + ' 项改动吗？写回 ' + DATA.local_file + '，下一条命令即生效。')) return;
  btnSave.disabled = true;
  try {
    const j = await post('/api/settings/save', {changes: ch});
    await refresh();
    msg('已保存 ' + j.changed.length + ' 项：' + j.note, true);
  } catch (e) { msg(e.message, false); refreshDirty(); }
};
document.getElementById('undo').onclick = () => { renderForm(); msg('已撤销未保存的改动', true); };
document.getElementById('reset').onclick = async () => {
  if (!confirm('恢复默认会清掉本地覆盖层里所有设置项，回到主配置的值。确定吗？')) return;
  try {
    const j = await post('/api/settings/save', {reset: true});
    await refresh();
    msg('已清除 ' + j.removed.length + ' 项覆盖：' + j.note, true);
  } catch (e) { msg(e.message, false); }
};
const blText = document.getElementById('bl-text');
const blSave = document.getElementById('bl-save');
const blMsg = document.getElementById('bl-msg');
const blCount = document.getElementById('bl-count');
let BL_ORIG = '';
function blMessage(text, ok) {
  blMsg.textContent = text;
  blMsg.className = 'msg ' + (ok === true ? 'ok' : ok === false ? 'err' : '');
}
async function refreshBlacklist() {
  const r = await fetch('/api/blacklist/get');
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '黑名单读取失败');
  BL_ORIG = j.text || '';
  blText.value = BL_ORIG;
  blCount.textContent = '当前 ' + j.sources.length + ' 个公众号 · ' + j.file;
  blSave.disabled = true;
  blMessage('', null);
}
blText.addEventListener('input', () => {
  const dirty = blText.value !== BL_ORIG;
  blSave.disabled = !dirty;
  blMessage(dirty ? '有未保存的改动' : '', null);
});
blSave.onclick = async () => {
  if (!confirm('确定把黑名单改成当前内容吗？写回 config/blacklist.json，下次 scan 生效。')) return;
  blSave.disabled = true;
  try {
    const j = await post('/api/blacklist/save', {text: blText.value});
    await refreshBlacklist();
    blMessage((j.changed ? '已保存' : '内容没变') + '：' + j.sources.length + ' 个公众号', true);
  } catch (e) { blMessage(e.message, false); blSave.disabled = false; }
};
renderForm();
refreshBlacklist().catch(e => blMessage(e.message, false));
</script>
</body>
</html>
"""


READ_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号简报 · 本地阅读</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 20px 48px; background:#f6f7f9; color:#1f2329;
         font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#667085; font-size:13px; margin-bottom:18px; }
  .cols { display:flex; gap:18px; align-items:flex-start; }
  .side { width:270px; flex:none; background:#fff; border:1px solid #e4e7ec;
          border-radius:12px; padding:10px; max-height:72vh; overflow:auto; }
  .side a { display:block; padding:10px 12px; border-radius:8px; color:#1f2329;
            text-decoration:none; margin-bottom:4px; }
  .side a:hover { background:#f2f4f7; }
  .side a.on { background:#e8f1fb; color:#185fa5; }
  .side .t { font-size:13px; font-weight:500; }
  .side .m { color:#667085; font-size:12px; margin-top:2px; }
  .tag { display:inline-block; font-size:11px; padding:1px 6px; border-radius:4px;
         background:#eef2f6; color:#475467; margin-right:5px; }
  .tag.voice { background:#faeeda; color:#854f0b; }
  .tag.failed { background:#fcebeb; color:#a32d2d; }
  .main { flex:1; min-width:0; background:#fff; border:1px solid #e4e7ec;
          border-radius:12px; padding:24px 28px 40px; }
  .meta { color:#667085; font-size:12px; border-bottom:1px solid #e4e7ec;
          padding-bottom:12px; margin-bottom:18px; }
  .body h1,.body h2,.body h3 { line-height:1.4; margin:22px 0 10px; }
  .body h1 { font-size:22px; } .body h2 { font-size:18px; } .body h3 { font-size:15px; }
  .body p { margin:0 0 14px; }
  .body a { color:#185fa5; }
  .body hr { border:0; border-top:1px solid #e4e7ec; margin:24px 0; }
  .body ul { padding-left:22px; }
  .empty { color:#667085; padding:40px 12px; text-align:center; }
  @media (max-width:760px) {
    .cols { flex-direction:column; }
    .side { width:100%; max-height:none; }
    .main { width:100%; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>本地阅读</h1>
  <div class="sub">只显示发布时留存到本地的简报副本，不改动 ZenMark 与台账。历史推送不在此列。</div>
  <div class="cols">
    <div class="side">__LIST__</div>
    <div class="main">__BODY__</div>
  </div>
</div>
</body>
</html>
"""


def _read_item(meta: dict, cur_id: str) -> str:
    mid = str(meta.get("id") or "")
    tags = ""
    if meta.get("kind") == "voice":
        tags += '<span class="tag voice">语音稿</span>'
    if meta.get("status") == "failed":
        tags += '<span class="tag failed">未推送</span>'
    title = escape(str(meta.get("title") or mid))
    when = escape(str(meta.get("created_at") or ""))
    qid = quote(mid, safe="")
    cls = "on" if mid == cur_id else ""
    return (f'<a class="{cls}" href="/read?id={qid}">'
            f'<div class="t">{tags}{title}</div>'
            f'<div class="m">{when}</div></a>')


def build_read_page(selected: str | None = None, *,
                    scope: str | None = None) -> str:
    """只读归档：左侧按期列表，右侧渲染选中那期。无归档时给空态。"""
    items = archive.list_items()
    if not items:
        listing = '<div class="empty">暂无本地归档</div>'
        body = ('<div class="empty">还没有归档。下次发布简报时会自动留一份'
                '到本地，这里就能读了。</div>')
    else:
        cur = next((i for i in items if i["id"] == selected), items[0])
        md = archive.load(cur["id"])
        if md is None:
            body = '<div class="empty">这份归档的正文读不到</div>'
        else:
            bits = []
            if cur.get("kind") == "voice":
                bits.append('<span class="tag voice">语音稿</span>')
            if cur.get("status") == "failed":
                bits.append('<span class="tag failed">未推送</span>')
            if cur.get("articles"):
                bits.append(f'{int(cur["articles"])} 篇')
            bits.append(escape(str(cur.get("created_at") or "")))
            bits.append(f'{cur.get("chars", 0)} 字')
            body = ('<div class="meta">' + " · ".join(bits) + '</div>'
                    '<div class="body">' + archive.render_markdown(md) + '</div>')
        listing = "".join(_read_item(i, cur["id"]) for i in items)
    return (READ_PAGE.replace(
        "<body>", "<body>" + nav_html("read", scope=scope), 1)
        .replace("__LIST__", listing).replace("__BODY__", body))


def build_settings_page(*, scope: str | None = None) -> str:
    """设置页首屏数据内嵌；不含任何令牌值。"""
    payload = json.dumps(settings_ui.current_state(), ensure_ascii=False).replace("</", "<\\/")
    return SETTINGS_PAGE.replace(
        "<body>", "<body>" + nav_html("settings", scope=scope), 1
    ).replace("__SETTINGS_DATA__", payload)


def build_page(inbox: dict) -> str:
    """把候选池元信息内嵌进页面；不读取、不下载任何正文。"""
    saved = selection.read_plan(inbox)
    valid = {a["index"] for a in inbox["articles"]}
    chosen = set(saved) & valid if saved is not None else valid
    articles = [{
        "index": a["index"],
        "title": a.get("title") or "（无标题）",
        "source": a.get("source") or "",
        "published_at": a.get("published_at") or "",
        "url": a.get("url") or "",
        "selected": a["index"] in chosen,
    } for a in inbox["articles"]]
    data = {
        "date": inbox["date"],
        "generated_at": inbox.get("generated_at", ""),
        "phase": inbox.get("phase", ""),
        "run_key": inbox.get("run_key", ""),
        # 页面上的保存 / 提交请求要带回去，常驻服务靠它区分「信息流那份」
        # 与「按号抓取那份」——同一个服务可能先后服务过两个 scope。
        "scope": inbox.get("scope") or "",
        "submitted": selection.read_request(inbox) is not None,
        "articles": articles,
    }
    # 防止标题里的 </script> 提前闭合脚本
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace(
        "<body>", "<body>" + nav_html("review", scope=inbox.get("scope") or ""), 1
    ).replace("__DATA__", payload)


class _Handler(BaseHTTPRequestHandler):
    inbox: dict = {}
    state: dict = {}
    has_review: bool = False
    dynamic: bool = False

    def active_inbox(self) -> dict | None:
        """当前可用于选文的候选池；dynamic 模式每次请求重读磁盘。"""
        if self.dynamic:
            return current_inbox(self.request_scope())
        return self.inbox if self.has_review else None

    def request_scope(self) -> str:
        """这次请求要看哪个 scope。

        优先看请求里的 `?scope=`：常驻服务可能先服务信息流那份、后服务
        按号抓取那份，光靠启动参数（`_ACTIVE_SCOPE`）会看错池子。
        `?scope=` 为空串表示**明确要信息流那份**（与「没带参数」区分开），
        没带参数时才退回启动时的 scope。
        """
        q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        got = q.get("scope")
        return (got[0] if got else _ACTIVE_SCOPE) or ""

    def log_message(self, fmt, *args):
        pass  # 本地服务，不刷访问日志

    def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        box = self.active_inbox()
        review_on = box is not None
        if path == "/settings":
            self._send(200, build_settings_page(scope=self.request_scope()),
                       "text/html; charset=utf-8")
        elif path == "/read":
            picked = (parse_qs(u.query).get("id") or [None])[0]
            self._send(200, build_read_page(picked, scope=self.request_scope()),
                       "text/html; charset=utf-8")
        elif path == "/grab":
            self._send(200, build_grab_page(scope=self.request_scope()),
                       "text/html; charset=utf-8")
        elif path == "/api/settings/get":
            try:
                self._json(200, settings_ui.current_state())
            except RuntimeError as exc:
                self._json(500, {"ok": False, "error": str(exc)})
        elif path == "/api/blacklist/get":
            try:
                names = blacklist.load()
                self._json(200, {"ok": True, "sources": names,
                                 "text": blacklist.as_text(names),
                                 "file": blacklist.PATH.name})
            except RuntimeError as exc:
                self._json(500, {"ok": False, "error": str(exc)})
        elif path == "/api/status":
            scope = self.request_scope()
            info = box if review_on else latest_inbox(scope)
            if info is None:
                self._json(200, {
                    "ok": True, "app": "wechat_digest",
                    "mode": "dynamic" if self.dynamic else "settings",
                    "review": False, "count": 0, "submitted": False,
                    "scope": scope,
                })
            else:
                req = selection.read_request(info)
                self._json(200, {
                    "ok": True, "app": "wechat_digest",
                    "mode": "review" if review_on else "dynamic",
                    "review": review_on,
                    "date": info["date"],
                    "scope": info.get("scope") or scope,
                    "phase": info.get("phase"),
                    "count": len(info["articles"]),
                    "submitted": req is not None,
                    "request_mode": (req or {}).get("mode"),
                })
        elif path in ("/", "/review"):
            if review_on:
                self._send(200, build_page(box), "text/html; charset=utf-8")
            elif path == "/":
                self._send(200, build_settings_page(scope=self.request_scope()),
                           "text/html; charset=utf-8")
            elif self.dynamic:
                self._send(200, build_no_review_page(self.request_scope()),
                           "text/html; charset=utf-8")
            else:
                self._json(404, {"ok": False, "error": "当前没有候选池清单"})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _post_settings(self, length: int):
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"请求格式不对：{exc}"})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "请求体必须是对象"})
            return
        try:
            res = (settings_ui.reset() if body.get("reset")
                   else settings_ui.save(body.get("changes") or {}))
        except RuntimeError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(200, res)

    def _post_blacklist(self, length: int):
        """黑名单独立保存：不进 settings.local.json，恢复默认也清不掉它。"""
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"请求格式不对：{exc}"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            self._json(400, {"ok": False, "error": "text 必须是字符串"})
            return
        try:
            before = blacklist.load()
            names = blacklist.save(blacklist.parse_text(body["text"]))
        except RuntimeError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        changed = names != before
        self._json(200, {
            "ok": True, "sources": names, "text": blacklist.as_text(names),
            "changed": changed,
            "note": "已保存，下次 scan 生效" if changed else "内容没变，没有写盘",
        })

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/api/settings/save":
            self._post_settings(length)
            return
        if path == "/api/blacklist/save":
            self._post_blacklist(length)
            return
        if path not in ("/api/save", "/api/submit"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        box = self.active_inbox()
        if box is None:
            self._json(409, {"ok": False, "error": "当前没有待确认的候选池清单"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            indices = body.get("indices")
            if not isinstance(indices, list):
                raise ValueError("indices 必须是数组")
            indices = [int(i) for i in indices]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"请求格式不对：{exc}"})
            return

        # 常驻服务不能用进程内 state 判重（下一轮 scan 还要能再提交），
        # 改以信号文件 + 候选池时间戳为准；旧模式沿用进程内状态。
        already = (selection.read_request(box) is not None
                   if self.dynamic else bool(self.state.get("result")))
        if already:
            self._json(409, {"ok": False, "error": "这一批已经提交过了"})
            return

        plan = selection.write_plan(box, indices, "review")
        n = len(plan["selected"])

        if path == "/api/save":
            self._json(200, {"ok": True, "selected": n, "submitted": False})
            return

        if not n:
            self._json(400, {"ok": False, "error": "一篇都没勾中，不能提交"})
            return

        req = selection.write_request(box, n)
        self.state["result"] = {
            "submitted": True,
            "selected": n,
            "requested_at": req["requested_at"],
            "run_key": req.get("run_key"),
        }
        self._json(200, {
            "ok": True, "selected": n, "submitted": True,
            "service": "running" if self.dynamic else "stopping",
            "read_url": "/read",
        })
        # 常驻服务提交后继续运行，三个页面始终是同一个端口；
        # 只有旧模式才退出。shutdown() 不能在 serve_forever() 所在线程直接调用。
        if not self.dynamic:
            def _stop():
                time.sleep(0.4)
                self.server.shutdown()
            threading.Thread(target=_stop, daemon=True).start()


def review_url(base: str, scope: str = "") -> str:
    """清单页地址。**总是带 `?scope=`**（空 scope 也要带），

    因为常驻服务靠这个参数决定读哪份候选池：`?scope=` 空串 = 信息流那份，
    不带参数 = 沿用服务启动时的 scope。两份简报同一天都在时，
    少一个参数就会打开错的那份。
    """
    return f"{base}/review?scope={quote(scope or '')}"


def serve(inbox: dict | None, port: int = 7777,
          open_browser: bool = True, *, dynamic: bool = False,
          landing: str | None = None, scope: str = "") -> dict:
    """起本地服务。

    dynamic=True（CLI 默认）：常驻模式，候选池每次请求动态读取，
    phase=scan 且未提交时 /review 自动出现；提交后服务不退出，
    /read 与 /settings 继续在同一端口可用，Ctrl+C 结束。
    dynamic=False 且传 inbox：兼容旧模式，收到提交后自动退出。
    """
    has_review = inbox is not None
    if has_review:
        if inbox.get("phase") != "scan":
            raise RuntimeError(
                f"候选池 phase 是 {inbox.get('phase')}，只有 scan 阶段能出清单；"
                "请重新跑 scan 生成新候选池")
        if not inbox.get("articles"):
            raise RuntimeError("候选池里没有文章，没清单可出")

    state: dict = {"result": None}
    handler = type("BoundHandler", (_Handler,),
                   {"inbox": inbox or {}, "state": state,
                    "has_review": has_review, "dynamic": dynamic})
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise RuntimeError(
            f"端口 {port} 起不来：{exc}；换个端口加 --port <端口>") from exc

    base = f"http://127.0.0.1:{port}"
    global _ACTIVE_SCOPE
    _ACTIVE_SCOPE = scope or ""
    box = current_inbox() if dynamic else None
    landing = landing or ("review" if has_review else "settings")
    url = (review_url(base, _ACTIVE_SCOPE) if landing == "review" and
           (has_review or box is not None) else f"{base}/settings")
    print(f"→ 本地控制台：{base}", flush=True)
    if dynamic:
        if box is not None:
            print(f"  选文清单：{base}/review（候选 {len(box['articles'])} 篇，已就绪）",
                  flush=True)
        else:
            print(f"  选文清单：{base}/review（当前没待确认候选池，跑完 scan 自动出现）",
                  flush=True)
        print(f"  本地阅读：{base}/read", flush=True)
        print(f"  参数设置：{base}/settings", flush=True)
        print("  三页同一个端口；提交后服务不退出，Ctrl+C 结束", flush=True)
    elif has_review:
        print(f"  候选 {len(inbox['articles'])} 篇 | 只显示元信息，不取正文", flush=True)
        print("  勾选后点「开始总结」才算授权；没有提交不会执行任何总结", flush=True)
        print(f"  本地阅读：{base}/read", flush=True)
        print(f"  参数设置：{base}/settings", flush=True)
    else:
        print(f"  本地阅读：{base}/read（只读归档，不发请求、不改台账）", flush=True)
        print("  平时调参数用，不依赖候选池；改动保存即写盘，下一条命令生效", flush=True)
        print("  Ctrl+C 结束服务", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n· 服务已结束" + ("，没有写提交信号" if has_review else ""), flush=True)
    finally:
        httpd.server_close()

    if dynamic:
        res = state["result"]
        print("\n· 常驻服务已结束" +
              ("（本轮已在页面提交过）" if res else "（没有写提交信号）"), flush=True)
        return res or {"submitted": False, "selected": 0, "mode": "dynamic"}

    if not has_review:
        return {"submitted": False, "selected": 0, "mode": "settings"}

    res = state["result"]
    if res:
        print(f"✓ 已收到提交：选中 {res['selected']} 篇，放行后续流程", flush=True)
        print("  下一步：python3 -m src.main hydrate", flush=True)
    else:
        print("· 没有收到提交，脚本什么都没放行", flush=True)
    return res or {"submitted": False, "selected": 0}
