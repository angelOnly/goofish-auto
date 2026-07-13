from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


LOGGER = logging.getLogger("goofish_auto.server")

try:
    from .goofish_tasks import (
        DEFAULT_BASE_URL,
        GoofishAPIError,
        GoofishClient,
        create_from_specs,
        load_specs,
    )
    from .pipeline import OUTPUT_DIR, load_tasks, run_named_task, template_copy
except ImportError:  # direct invocation from the project root
    from goofish_tasks import DEFAULT_BASE_URL, GoofishAPIError, GoofishClient, create_from_specs, load_specs
    from pipeline import OUTPUT_DIR, load_tasks, run_named_task, template_copy


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
a{color:var(--blue);text-decoration:none}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:3px 8px;background:#eef2ff;color:#3730a3;font-size:12px}.pill.ok{background:#ecfdf5;color:#166534}.pill.warn{background:#fff7ed;color:#9a3412}
.help{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:8px;padding:12px;color:#475569;line-height:1.7}
.copy-actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.cover-preview{max-width:360px;width:100%;border:1px solid var(--line);border-radius:8px;margin-top:8px;background:white}
.image-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin:12px 0}.tiny{font-size:12px;color:var(--muted)}
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
    <button class="secondary" id="openTasksBtn">新标签打开任务管理</button>
    <button class="secondary" id="openResultsBtn">新标签打开监控结果</button>
    <button class="secondary" id="refreshBtn">刷新</button>
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
      <h2>本地资源整理</h2>
      <div class="row">
        <select id="localTask"></select>
        <label class="row"><input id="includeSeen" type="checkbox"> 包含已处理</label>
        <button id="runBtn">运行一次</button>
      </div>
      <div class="help">这里是本地 TheItzy 元数据整理，只保留一个“AI虚拟课程选品整理”任务；默认只看近 15 天、课程/教程/资料/项目实战类内容，并排除远程安装、账号卡密和实体商品。下面的“闲鱼热点监控”才是远程闲鱼监控任务。</div>
      <div id="localStatus" class="status"></div>
      <div id="runs"></div>
    </div>

    <div class="panel stack">
      <h2>闲鱼热点监控</h2>
      <div class="row">
        <button id="loadGoofishBtn">测试连接/刷新任务</button>
        <button class="secondary" id="previewBootstrapBtn">预览本地配置</button>
        <button id="startBootstrapBtn">创建缺失任务到闲鱼监控</button>
      </div>
      <div class="help">“预览本地配置”只查看将要提交的 2 个任务，不会创建。“创建缺失任务到闲鱼监控”会调用远程 API：远程没有同名任务就创建并启动，已有同名任务就跳过，不会更新或覆盖旧任务。当前监控只看虚拟课程/资料/项目实战类商品，排除远程安装、账号卡密和实体商品；上新范围使用 `14天内`，cron 为 `0 */12 * * *`。</div>
      <div id="goofishStatus" class="status"></div>
      <div id="goofishTasks"></div>
    </div>
  </section>

  <section class="panel">
    <h2>结果详情</h2>
    <div id="runDetail" class="muted">点击最近运行里的“查看”查看生成条目。</div>
    <div id="itemDetail"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let goofishUrls = {tasks_url:'https://goofish.xiaolicloud.cn:18443/tasks?create=1', results_url:'https://goofish.xiaolicloud.cn:18443/results'};
let currentItem = null;

function esc(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
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
function openUrl(url){
  window.open(url, '_blank', 'noopener,noreferrer');
}
async function copyText(value, label){
  const text = String(value || '');
  if(!text.trim()){
    setStatus('localStatus', `${label}为空，没东西可复制`, 'warn');
    return;
  }
  try{
    await navigator.clipboard.writeText(text);
  }catch(e){
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
  }
  setStatus('localStatus', `${label}已复制`, 'ok');
}
function taskState(task){
  const enabled = Boolean(task.enabled);
  const running = Boolean(task.is_running);
  if(enabled && running) return '<span class="pill ok">运行中</span>';
  if(enabled) return '<span class="pill warn">已启用</span>';
  return '<span class="pill">已停止</span>';
}
function imageAnalysisState(task){
  return task.analyze_images
    ? '<span class="pill warn">图片分析开</span>'
    : '<span class="pill ok">只看文本</span>';
}
function summarizeBootstrapResult(data){
  const list = Array.isArray(data) ? data : [];
  return {
    total: list.length,
    created: list.filter(item => String(item.status || '').startsWith('created')).length,
    skipped: list.filter(item => item.status === 'skipped_existing').length,
    dryRun: list.filter(item => item.status === 'dry_run').length,
    failed: list.filter(item => String(item.status || '').includes('failed')).length,
  };
}
function renderDiagnostics(data){
  const d = data?.diagnostics || {};
  if(!Object.keys(d).length) return '';
  const zero = d.zero_reason ? `<p class="warn">0 条原因：${esc(d.zero_reason)}</p>` : '';
  const fallback = d.fallback_used ? '<p class="warn">没有关键词命中，已启用兜底：从候选里按热度取最新内容，方便确认数据源是否正常。</p>' : '';
  const top = (d.top_candidates || []).map(item => {
    const matched = (item.matched_keywords || []).join(', ') || '无';
    return `<li>${esc(item.title)} <small>热度 ${esc(item.hotness_score)}，命中：${esc(matched)}</small></li>`;
  }).join('');
  const age = d.max_age_days ? `；超过 ${esc(d.max_age_days)} 天过滤 ${esc(d.skipped_old_count ?? 0)} 条` : '';
  const excluded = d.exclude_keywords?.length ? `；排除词过滤 ${esc(d.skipped_excluded_count ?? 0)} 条` : '';
  return `<div class="help"><h3>运行诊断</h3>${zero}${fallback}<p>抓取 ${esc(d.fetched_count ?? 0)} 条；已处理跳过 ${esc(d.skipped_seen_count ?? 0)} 条${age}${excluded}；候选 ${esc(d.candidate_count ?? 0)} 条；关键词命中 ${esc(d.matched_count ?? 0)} 条；最终输出 ${esc(d.selected_count ?? data.count ?? 0)} 条。</p>${top ? `<ul>${top}</ul>` : ''}</div>`;
}

async function loadConfig(){
  const cfg = await api('/api/goofish/config');
  goofishUrls = cfg;
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
  const runs = await api('/api/local/runs');
  $('latestRunText').textContent = runs[0]?.run_id || '暂无';
  if(!runs.length){ $('runs').innerHTML = '<p class="muted">还没有运行记录。</p>'; return; }
  $('runs').innerHTML = '<h3>最近运行</h3><table><thead><tr><th>时间</th><th>任务</th><th>数量</th><th></th></tr></thead><tbody>' +
    runs.map(r => `<tr><td>${esc(r.run_id)}</td><td>${esc(r.task_name)}</td><td>${esc(r.count)}</td><td><button class="secondary js-show-run" data-run-id="${esc(r.run_id)}">查看</button></td></tr>`).join('') +
    '</tbody></table>';
}
async function runLocalTask(){
  const btn=$('runBtn'); btn.disabled=true; setStatus('localStatus','运行中，请稍等...');
  try{
    const data = await api('/api/local/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:$('localTask').value,include_seen:$('includeSeen').checked})});
    const reason = data.diagnostics?.zero_reason;
    if((data.count || 0) === 0 && reason){
      setStatus('localStatus',`完成但没有输出：${reason}`,'warn');
    }else{
      setStatus('localStatus',`完成：${data.count || 0} 条，输出目录 ${data.run_id}`,'ok');
    }
    await loadRuns();
    await showRun(data.run_id);
  }catch(e){ setStatus('localStatus', e.message, 'bad'); }
  finally{ btn.disabled=false; }
}
async function showRun(runId){
  const data = await api(`/api/local/runs/${encodeURIComponent(runId)}`);
  const rows = (data.items || []).map(item => `<tr><td>${esc(item.title)}</td><td>${esc(item.hotness_score)}</td><td><a target="_blank" rel="noreferrer" href="${esc(item.page_url)}">来源</a></td><td><button class="secondary js-show-item" data-folder="${esc(item.folder)}">文案</button></td></tr>`).join('');
  $('runDetail').innerHTML = `<p><span class="pill">${esc(data.task_name)}</span> ${esc(data.count)} 条</p>` +
    renderDiagnostics(data) +
    `<table><thead><tr><th>标题</th><th>热度</th><th>来源</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  $('itemDetail').innerHTML = '';
}
async function showItem(folder){
  const data = await api(`/api/local/item?folder=${encodeURIComponent(folder)}`);
  currentItem = data;
  const copyText = data.copy_suggested || data.copy;
  const imageHtml = data.cover_url
    ? `<div class="image-box"><h3>图片信息</h3><p class="tiny">公开封面仅供人工核验。正式上架建议使用你自己有权使用的封面图、目录长图或重新制作说明图。</p><p><a target="_blank" rel="noreferrer" href="${esc(data.cover_url)}">${esc(data.cover_url)}</a></p><img class="cover-preview" src="${esc(data.cover_url)}" alt="封面预览"></div>`
    : `<div class="image-box"><h3>图片信息</h3><p class="muted">这个条目没有公开封面地址。正式上架时建议补一张自有/授权封面图或目录图。</p></div>`;
  $('itemDetail').innerHTML = imageHtml +
    `<h3>新版闲鱼文案</h3><div class="copy-actions"><button class="secondary js-copy-field" data-field="copy_suggested">复制文案</button><button class="secondary js-copy-field" data-field="cover_url">复制图片链接</button></div><pre>${esc(copyText)}</pre>` +
    `<h3>发货信息 delivery.md</h3><div class="copy-actions"><button class="secondary js-copy-field" data-field="delivery">复制发货信息</button></div><pre>${esc(data.delivery)}</pre>`;
}

async function loadGoofishTasks(){
  setStatus('goofishStatus','正在连接闲鱼监控 API...');
  try{
    const tasks = await api('/api/goofish/tasks');
    $('goofishHealthText').textContent='正常'; $('goofishHealthText').className='ok';
    $('remoteCountText').textContent=String(tasks.length);
    if(!tasks.length){ $('goofishTasks').innerHTML='<p class="muted">远程暂无任务。</p>'; setStatus('goofishStatus','连接成功，但没有任务','warn'); return; }
    const seen = {};
    const duplicateNames = new Set();
    tasks.forEach(t => {
      const name = t.task_name ?? t.name ?? t.title ?? '';
      seen[name] = (seen[name] || 0) + 1;
      if(seen[name] > 1) duplicateNames.add(name);
    });
    Object.keys(seen).forEach(name => seen[name] = 0);
    $('goofishTasks').innerHTML = '<h3>远程任务</h3><table><thead><tr><th>ID</th><th>任务</th><th>关键词</th><th>状态</th><th>图片分析</th><th>计划</th><th>操作</th></tr></thead><tbody>' +
      tasks.map(t => {
        const id = t.id ?? t.task_id ?? t.taskId ?? '';
        const name = t.task_name ?? t.name ?? t.title ?? '';
        seen[name] = (seen[name] || 0) + 1;
        const duplicate = seen[name] > 1 ? ' <span class="pill warn">同名重复</span>' : '';
        const next = t.next_run_at ? `<br><small>下次：${esc(t.next_run_at)}</small>` : '';
        const mode = t.decision_mode ? `<br><small>${esc(t.decision_mode)}</small>` : '';
        const startStop = t.is_running
          ? `<button class="danger js-stop-remote" data-task-id="${esc(id)}">停止</button>`
          : `<button class="secondary js-start-remote" data-task-id="${esc(id)}">启动</button>`;
        return `<tr><td>${esc(id)}</td><td>${esc(name)}${duplicate}${mode}</td><td>${esc(t.keyword || '')}</td><td>${taskState(t)}</td><td>${imageAnalysisState(t)}</td><td>${esc(t.cron || '')}${next}</td><td class="row">${startStop}<button class="secondary js-open-results">看结果</button></td></tr>`;
      }).join('') + '</tbody></table>';
    const duplicateText = duplicateNames.size ? `发现 ${duplicateNames.size} 组同名重复任务；建议停止多余的，只保留一条。` : '没有发现同名重复任务。';
    setStatus('goofishStatus',`连接成功。${duplicateText} 运行中的任务会按“下次”时间采集，结果在闲鱼后台结果页查看。`,'ok');
  }catch(e){
    $('goofishHealthText').textContent='异常'; $('goofishHealthText').className='bad';
    setStatus('goofishStatus', e.message, 'bad');
  }
}
async function previewBootstrap(){
  const btn = $('previewBootstrapBtn');
  btn.disabled = true;
  setStatus('goofishStatus','正在生成预览...');
  try{
    const data = await api('/api/goofish/bootstrap/dry-run',{method:'POST'});
    const summary = summarizeBootstrapResult(data);
    $('goofishTasks').innerHTML = '<h3>本地配置预览（不会创建）</h3><pre>'+esc(JSON.stringify(data,null,2))+'</pre>';
    setStatus('goofishStatus',`预览完成：配置里 ${summary.total} 个任务。这里只是预览，没有调用远程创建。`,'ok');
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
  finally{ btn.disabled = false; }
}
async function startBootstrap(){
  if(!confirm('确认把 goofish_tasks.json 里的 2 个任务创建到闲鱼监控？远程已有同名任务会跳过，只创建缺失任务。')) return;
  const btn = $('startBootstrapBtn');
  btn.disabled = true;
  setStatus('goofishStatus','正在创建远程缺失任务，并启动新创建的任务。当前配置只看文本，不分析图片...');
  try{
    const data = await api('/api/goofish/bootstrap/start',{method:'POST'});
    const summary = summarizeBootstrapResult(data);
    $('goofishTasks').innerHTML = '<h3>执行结果</h3><pre>'+esc(JSON.stringify(data,null,2))+'</pre>';
    setStatus('goofishStatus',`创建完成：实际新建 ${summary.created} 个，已有同名跳过 ${summary.skipped} 个。新任务已按文本分析、每 12 小时一次配置。`,'ok');
    await loadGoofishTasks();
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
  finally{ btn.disabled = false; }
}
async function startRemoteTask(taskId){
  setStatus('goofishStatus',`正在启动任务 ${taskId}...`);
  try{
    await api('/api/goofish/tasks/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})});
    setStatus('goofishStatus',`任务 ${taskId} 已请求启动`,'ok');
    await loadGoofishTasks();
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
}
async function stopRemoteTask(taskId){
  setStatus('goofishStatus',`正在停止任务 ${taskId}...`);
  try{
    await api('/api/goofish/tasks/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})});
    setStatus('goofishStatus',`任务 ${taskId} 已停止`,'ok');
    await loadGoofishTasks();
  }catch(e){ setStatus('goofishStatus', e.message, 'bad'); }
}
async function refreshAll(){
  await Promise.allSettled([loadHealth(), loadConfig(), loadLocalTasks(), loadRuns(), loadGoofishTasks()]);
}

document.addEventListener('click', (event) => {
  const target = event.target.closest('button');
  if(!target) return;
  if(target.id === 'refreshBtn') refreshAll();
  else if(target.id === 'runBtn') runLocalTask();
  else if(target.id === 'loadGoofishBtn') loadGoofishTasks();
  else if(target.id === 'previewBootstrapBtn') previewBootstrap();
  else if(target.id === 'startBootstrapBtn') startBootstrap();
  else if(target.id === 'openTasksBtn') openUrl(goofishUrls.tasks_url);
  else if(target.id === 'openResultsBtn' || target.classList.contains('js-open-results')) openUrl(goofishUrls.results_url);
  else if(target.classList.contains('js-show-run')) showRun(target.dataset.runId);
  else if(target.classList.contains('js-show-item')) showItem(target.dataset.folder);
  else if(target.classList.contains('js-copy-field')) copyText(currentItem?.[target.dataset.field], target.textContent.trim());
  else if(target.classList.contains('js-start-remote')) startRemoteTask(target.dataset.taskId);
  else if(target.classList.contains('js-stop-remote')) stopRemoteTask(target.dataset.taskId);
});
window.addEventListener('error', (event) => setStatus('localStatus', `页面脚本错误：${event.message}`, 'bad'));
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
    item = json.loads(item_path.read_text(encoding="utf-8")) if item_path.exists() else {}
    rights_confirmed = item.get("rights_review") == "confirmed"
    copy_suggested = template_copy(item, {"rights_confirmed": rights_confirmed}) if item else ""
    return {
        "folder": folder,
        "copy": copy_path.read_text(encoding="utf-8") if copy_path.exists() else "",
        "copy_suggested": copy_suggested,
        "delivery": delivery_path.read_text(encoding="utf-8") if delivery_path.exists() else "",
        "cover_url": item.get("cover_url", ""),
        "cover_local_path": item.get("cover_local_path", ""),
        "page_url": item.get("page_url", ""),
        "title": item.get("title", ""),
        "item": item,
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
                self._send(
                    200,
                    {
                        "base_url": base_url,
                        "tasks_url": f"{base_url}/tasks?create=1",
                        "results_url": f"{base_url}/results",
                    },
                )
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
                task_name = str(body["task"])
                include_seen = bool(body.get("include_seen", False))
                LOGGER.info("local_run request task=%s include_seen=%s", task_name, include_seen)
                result = run_named_task(
                    task_name,
                    include_seen=include_seen,
                )
                diagnostics = result.get("diagnostics", {}) if isinstance(result, dict) else {}
                LOGGER.info(
                    "local_run done task=%s run_id=%s count=%s diagnostics=%s",
                    task_name,
                    result.get("run_id") if isinstance(result, dict) else "",
                    result.get("count") if isinstance(result, dict) else "",
                    diagnostics,
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
            elif path == "/api/goofish/tasks/stop":
                task_id = body.get("task_id")
                if task_id in {None, ""}:
                    raise ValueError("缺少 task_id")
                self._send(200, _goofish_client().stop(task_id))
            else:
                self._send(404, {"error": "not found"})
        except (KeyError, OSError, ValueError, GoofishAPIError, json.JSONDecodeError) as exc:
            LOGGER.exception("request failed path=%s", getattr(self, "path", ""))
            self._send_error(400, exc)

    def log_message(self, fmt: str, *args: object) -> None:
        logging.getLogger("goofish_auto.http").info(fmt, *args)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("server_start url=http://%s:%s/ output_dir=%s", host, port, OUTPUT_DIR)
    print(f"控制台：http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
