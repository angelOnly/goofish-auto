from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .goofish_tasks import (
        DEFAULT_BASE_URL,
        GoofishAPIError,
        GoofishClient,
        create_from_specs,
        load_specs,
    )
    from .pipeline import OUTPUT_DIR, load_tasks, run_named_task
except ImportError:  # direct invocation from the project root
    from goofish_tasks import DEFAULT_BASE_URL, GoofishAPIError, GoofishClient, create_from_specs, load_specs
    from pipeline import OUTPUT_DIR, load_tasks, run_named_task


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Goofish Auto 控制台</title>
<style>
:root{color-scheme:light;--bg:#f6f8fb;--panel:#fff;--line:#e6eaf0;--muted:#64748b;--text:#172033;--blue:#2563eb;--green:#16a34a;--red:#dc2626;--amber:#d97706}
*{box-sizing:border-box}body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;color:var(--text);background:var(--bg)}
header{padding:28px 32px 10px;display:flex;align-items:flex-end;justify-content:space-between;gap:16px}
h1{margin:0;font-size:28px}h2{margin:0 0 16px;font-size:20px}h3{margin:18px 0 10px;font-size:16px}
main{padding:16px 32px 32px;display:grid;gap:16px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.panel,.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 28px #1720330d}
.panel{padding:18px}.card{padding:14px}.card small,.muted{color:var(--muted)}.card strong{display:block;margin-top:8px;font-size:18px}
button,select,input{font:inherit}button,select{height:38px;border-radius:8px;border:1px solid #cbd5e1;background:white;padding:0 12px}
button{background:var(--blue);border-color:var(--blue);color:white;cursor:pointer}button.secondary{background:white;color:var(--text);border-color:#cbd5e1}button.danger{background:var(--red);border-color:var(--red)}
button:disabled{opacity:.55;cursor:not-allowed}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.stack{display:grid;gap:12px}
.status{min-height:22px;color:var(--muted)}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #eef1f5;padding:10px 8px;vertical-align:top}th{color:var(--muted);font-weight:600}
pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;max-height:360px;overflow:auto}
a{color:var(--blue);text-decoration:none}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:3px 8px;background:#eef2ff;color:#3730a3;font-size:12px}
iframe{width:100%;height:520px;border:1px solid var(--line);border-radius:8px;background:white}
@media (max-width:1000px){.grid,.cards{grid-template-columns:1fr}header{display:block}main,header{padding-left:16px;padding-right:16px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>Goofish Auto 控制台</h1>
    <p class="muted">本地资源任务、闲鱼热点监控、文案结果集中管理。</p>
  </div>
  <div class="row">
    <a id="goofishOpen" target="_blank" rel="noreferrer"><button class="secondary">打开闲鱼后台</button></a>
    <button class="secondary" onclick="refreshAll()">刷新</button>
  </div>
</header>
<main>
  <section class="cards">
    <div class="card"><small>本地服务</small><strong id="healthText">检查中</strong></div>
    <div class="card"><small>闲鱼 API</small><strong id="goofishHealthText">未检查</strong></div>
    <div class="card"><small>最近运行</small><strong id="latestRunText">暂无</strong></div>
    <div class="card"><small>远程任务</small><strong id="remoteCountText">-</strong></div>
  </section>

  <section class="grid">
    <div class="panel stack">
      <h2>本地资源任务</h2>
      <div class="row">
        <select id="localTask"></select>
        <label class="row"><input id="includeSeen" type="checkbox"> 包含已处理</label>
        <button id="runBtn" onclick="runLocalTask()">运行一次</button>
      </div>
      <div id="localStatus" class="status"></div>
      <div id="runs"></div>
    </div>

    <div class="panel stack">
      <h2>闲鱼热点监控</h2>
      <div class="row">
        <button onclick="loadGoofishTasks()">测试连接/刷新任务</button>
        <button class="secondary" onclick="previewBootstrap()">预览创建任务</button>
        <button onclick="startBootstrap()">创建并启动监控</button>
      </div>
      <div id="goofishStatus" class="status"></div>
      <div id="goofishTasks"></div>
    </div>
  </section>

  <section class="grid">
    <div class="panel">
      <h2>结果详情</h2>
      <div id="runDetail" class="muted">点击最近运行里的记录查看生成条目。</div>
      <div id="itemDetail"></div>
    </div>
    <div class="panel">
      <h2>闲鱼原始后台</h2>
      <p class="muted">如果下方无法显示，说明远程页面禁止 iframe 嵌入，点右上角“打开闲鱼后台”。</p>
      <iframe id="goofishFrame"></iframe>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let lastRuns = [];

function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function arg(value){
  return esc(JSON.stringify(String(value ?? '')));
}
async function api(path, options={}){
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = {error:text}; }
  if(!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function setStatus(id, text, cls=''){
  const el=$(id); el.className = 'status ' + cls; el.textContent = text;
}

async function loadConfig(){
  const cfg = await api('/api/goofish/config');
  $('goofishOpen').href = cfg.page_url;
  $('goofishFrame').src = cfg.page_url;
}
async function loadHealth(){
  try { await api('/api/health'); $('healthText').textContent='正常'; $('healthText').className='ok'; }
  catch(e){ $('healthText').textContent='异常'; $('healthText').className='bad'; }
}
async function loadLocalTasks(){
  const tasks = await api('/api/local/tasks');
  $('localTask').innerHTML = tasks.map(t => `<option>${esc(t.name)}</option>`).join('');
}
async function loadRuns(){
  lastRuns = await api('/api/local/runs');
  $('latestRunText').textContent = lastRuns[0]?.run_id || '暂无';
  if(!lastRuns.length){ $('runs').innerHTML = '<p class="muted">还没有运行记录。</p>'; return; }
  $('runs').innerHTML = '<h3>最近运行</h3><table><thead><tr><th>时间</th><th>任务</th><th>数量</th><th></th></tr></thead><tbody>' +
    lastRuns.map(r => `<tr><td>${esc(r.run_id)}</td><td>${esc(r.task_name)}</td><td>${esc(r.count)}</td><td><button class="secondary" onclick="showRun(${arg(r.run_id)})">查看</button></td></tr>`).join('') +
    '</tbody></table>';
}
async function runLocalTask(){
  const btn=$('runBtn'); btn.disabled=true; setStatus('localStatus','运行中，请稍等...');
  try{
    const data = await api('/api/local/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:$('localTask').value,include_seen:$('includeSeen').checked})});
    setStatus('localStatus',`完成：${data.count || 0} 条，输出目录 ${data.run_id}`,'ok');
    await loadRuns();
    await showRun(data.run_id);
  }catch(e){ setStatus('localStatus', e.message, 'bad'); }
  finally{ btn.disabled=false; }
}
async function showRun(runId){
  const data = await api(`/api/local/runs/${encodeURIComponent(runId)}`);
  const rows = (data.items || []).map(item => `<tr><td>${esc(item.title)}</td><td>${esc(item.hotness_score)}</td><td><a target="_blank" href="${esc(item.page_url)}">来源</a></td><td><button class="secondary" onclick="showItem(${arg(item.folder)})">文案</button></td></tr>`).join('');
  $('runDetail').innerHTML = `<p><span class="pill">${esc(data.task_name)}</span> ${esc(data.count)} 条</p>` +
    `<table><thead><tr><th>标题</th><th>热度</th><th>来源</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  $('itemDetail').innerHTML = '';
}
async function showItem(folder){
  const data = await api(`/api/local/item?folder=${encodeURIComponent(folder)}`);
  $('itemDetail').innerHTML = `<h3>闲鱼文案 copy.md</h3><pre>${esc(data.copy)}</pre><h3>发货信息 delivery.md</h3><pre>${esc(data.delivery)}</pre>`;
}

async function loadGoofishTasks(){
  setStatus('goofishStatus','正在连接闲鱼监控 API...');
  try{
    const tasks = await api('/api/goofish/tasks');
    $('goofishHealthText').textContent='正常'; $('goofishHealthText').className='ok';
    $('remoteCountText').textContent=String(tasks.length);
    if(!tasks.length){ $('goofishTasks').innerHTML='<p class="muted">远程暂无任务。</p>'; return; }
    $('goofishTasks').innerHTML = '<h3>远程任务</h3><table><thead><tr><th>ID</th><th>任务名</th><th>状态</th><th></th></tr></thead><tbody>' +
      tasks.map(t => {
        const id = t.id ?? t.task_id ?? t.taskId ?? '';
        const name = t.task_name ?? t.name ?? t.title ?? '';
        const status = t.status ?? t.state ?? '';
        const action = id ? `<button class="secondary" onclick="startRemoteTask(${arg(id)})">启动</button>` : '';
        return `<tr><td>${esc(id)}</td><td>${esc(name)}</td><td>${esc(status)}</td><td>${action}</td></tr>`;
      }).join('') + '</tbody></table>';
    setStatus('goofishStatus','连接成功','ok');
  }catch(e){
    $('goofishHealthText').textContent='异常'; $('goofishHealthText').className='bad';
    setStatus('goofishStatus', e.message, 'bad');
  }
}
async function previewBootstrap(){
  setStatus('goofishStatus','正在生成预览...');
  try{
    const data = await api('/api/goofish/bootstrap/dry-run',{method:'POST'});
    $('goofishTasks').innerHTML = '<h3>将创建的任务</h3><pre>'+esc(JSON.stringify(data,null,2))+'</pre>';
    setStatus('goofishStatus',`预览完成：${data.length} 个任务`,'ok');
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
}
async function startBootstrap(){
  if(!confirm('确认创建并启动 goofish_tasks.json 里的热点监控任务？')) return;
  setStatus('goofishStatus','正在创建并启动，AI 任务可能需要等待一会儿...');
  try{
    const data = await api('/api/goofish/bootstrap/start',{method:'POST'});
    $('goofishTasks').innerHTML = '<h3>执行结果</h3><pre>'+esc(JSON.stringify(data,null,2))+'</pre>';
    setStatus('goofishStatus','创建/启动完成','ok');
    await loadGoofishTasks();
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
}
async function startRemoteTask(taskId){
  setStatus('goofishStatus',`正在启动任务 ${taskId}...`);
  try{
    await api('/api/goofish/tasks/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})});
    setStatus('goofishStatus',`任务 ${taskId} 已请求启动`,'ok');
    await loadGoofishTasks();
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
}
async function refreshAll(){
  await Promise.allSettled([loadHealth(), loadConfig(), loadLocalTasks(), loadRuns(), loadGoofishTasks()]);
}
refreshAll();
</script>
</body>
</html>"""


def _goofish_base_url() -> str:
    return os.getenv("GOOFISH_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _goofish_client() -> GoofishClient:
    return GoofishClient(
        _goofish_base_url(),
        timeout=int(os.getenv("GOOFISH_API_TIMEOUT", "30")),
        poll_seconds=float(os.getenv("GOOFISH_POLL_SECONDS", "2")),
        max_wait_seconds=int(os.getenv("GOOFISH_MAX_WAIT_SECONDS", "1800")),
    )


def _safe_output_path(relative_path: str) -> Path:
    root = OUTPUT_DIR.resolve()
    candidate = (OUTPUT_DIR / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("输出路径非法")
    return candidate


def _read_output_item(folder: str) -> dict[str, object]:
    item_dir = _safe_output_path(folder)
    if not item_dir.is_dir():
        raise FileNotFoundError("结果目录不存在")
    copy_path = item_dir / "copy.md"
    delivery_path = item_dir / "delivery.md"
    item_path = item_dir / "item.json"
    return {
        "folder": folder,
        "copy": copy_path.read_text(encoding="utf-8") if copy_path.exists() else "",
        "delivery": delivery_path.read_text(encoding="utf-8") if delivery_path.exists() else "",
        "item": json.loads(item_path.read_text(encoding="utf-8")) if item_path.exists() else {},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, data: object, content_type: str = "application/json; charset=utf-8") -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _send_error(self, status: int, exc: Exception) -> None:
        self._send(status, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send(200, HTML, "text/html; charset=utf-8")
            elif path == "/api/health":
                self._send(200, {"ok": True, "output_dir": str(OUTPUT_DIR)})
            elif path in {"/api/tasks", "/api/local/tasks"}:
                self._send(200, load_tasks())
            elif path in {"/api/runs", "/api/local/runs"}:
                summaries = []
                for summary_path in sorted(OUTPUT_DIR.glob("*/summary.json"), reverse=True)[:30]:
                    try:
                        summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))
                    except json.JSONDecodeError:
                        pass
                self._send(200, summaries)
            elif path.startswith("/api/runs/") or path.startswith("/api/local/runs/"):
                run_id = path.rsplit("/", 1)[-1]
                summary_path = OUTPUT_DIR / run_id / "summary.json"
                if not summary_path.exists():
                    self._send(404, {"error": "运行记录不存在"})
                else:
                    self._send(200, json.loads(summary_path.read_text(encoding="utf-8")))
            elif path == "/api/local/item":
                folder = parse_qs(parsed.query).get("folder", [""])[0]
                self._send(200, _read_output_item(folder))
            elif path == "/api/goofish/config":
                base_url = _goofish_base_url()
                self._send(200, {"base_url": base_url, "page_url": f"{base_url}/tasks?create=1"})
            elif path == "/api/goofish/tasks":
                self._send(200, _goofish_client().list_tasks())
            elif path == "/api/goofish/specs":
                self._send(200, load_specs())
            else:
                self._send(404, {"error": "not found"})
        except (OSError, ValueError, GoofishAPIError) as exc:
            self._send_error(400, exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            body = self._json_body()
            if path in {"/api/run", "/api/local/run"}:
                result = run_named_task(
                    str(body["task"]),
                    include_seen=bool(body.get("include_seen", False)),
                )
                self._send(200, result)
            elif path == "/api/goofish/bootstrap/dry-run":
                result = create_from_specs(_goofish_client(), load_specs(), dry_run=True)
                self._send(200, result)
            elif path == "/api/goofish/bootstrap/start":
                result = create_from_specs(_goofish_client(), load_specs(), start_created=True)
                self._send(200, result)
            elif path == "/api/goofish/tasks/start":
                task_id = body.get("task_id")
                if task_id in {None, ""}:
                    raise ValueError("缺少 task_id")
                self._send(200, _goofish_client().start(task_id))
            else:
                self._send(404, {"error": "not found"})
        except (KeyError, OSError, ValueError, GoofishAPIError, json.JSONDecodeError) as exc:
            self._send_error(400, exc)

    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"控制台：http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
