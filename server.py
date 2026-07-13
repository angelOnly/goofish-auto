from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .pipeline import OUTPUT_DIR, load_tasks, run_named_task
except ImportError:  # direct invocation from the resource_pipeline directory
    from pipeline import OUTPUT_DIR, load_tasks, run_named_task


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地资源热点审核</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:1000px;margin:32px auto;padding:0 16px;color:#172033;background:#f6f8fb}
main{background:white;border:1px solid #e6eaf0;border-radius:14px;padding:24px;box-shadow:0 8px 30px #17203312}
button,select{padding:10px 14px;border:1px solid #cbd5e1;border-radius:8px;background:white}button{background:#2563eb;color:white;border:0;cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:18px}th,td{text-align:left;border-bottom:1px solid #eef1f5;padding:10px 6px}small{color:#64748b}.pill{padding:3px 8px;border-radius:999px;background:#fff7ed;color:#9a3412}.ok{background:#ecfdf5;color:#166534}
pre{white-space:pre-wrap;background:#f8fafc;border-radius:8px;padding:14px}
</style></head><body><main>
<h1>本地资源热点审核</h1><p><small>只显示公开元数据；发布前请完成权利、内容和网盘链接审核。</small></p>
<label>监控任务：<select id="task"></select></label> <button onclick="runTask()">运行一次</button>
<p id="status"></p><div id="runList"></div>
<script>
const taskEl=document.getElementById('task'), statusEl=document.getElementById('status'), runListEl=document.getElementById('runList');
async function load(){const ts=await fetch('/api/tasks').then(r=>r.json());taskEl.innerHTML=ts.map(x=>`<option>${x.name}</option>`).join('');await runs();}
async function runTask(){statusEl.textContent='运行中（按站点 robots.txt 频率执行）...';const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:taskEl.value})});const j=await r.json();statusEl.textContent=j.error||`完成：${j.count||0} 条，输出目录 ${j.run_id||''}`;await runs();}
async function runs(){const j=await fetch('/api/runs').then(r=>r.json());runListEl.innerHTML='<h2>最近运行</h2>'+j.map(x=>`<p><a href="/api/runs/${x.run_id}">${x.run_id}</a> ${x.task_name} · ${x.count} 条 · ${x.rights_review==='confirmed'?'<span class="pill ok">已确认权利</span>':'<span class="pill">待确认权利</span>'}</p>`).join('');}
load();
</script></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, data: object, content_type: str = "application/json; charset=utf-8") -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif parsed.path == "/api/health":
            self._send(200, {"ok": True})
        elif parsed.path == "/api/tasks":
            self._send(200, load_tasks())
        elif parsed.path == "/api/runs":
            summaries = []
            for path in sorted(OUTPUT_DIR.glob("*/summary.json"), reverse=True)[:30]:
                try:
                    summaries.append(json.loads(path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
            self._send(200, summaries)
        elif parsed.path.startswith("/api/runs/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            path = OUTPUT_DIR / run_id / "summary.json"
            if not path.exists():
                self._send(404, {"error": "运行记录不存在"})
            else:
                self._send(200, json.loads(path.read_text(encoding="utf-8")))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/run":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            result = run_named_task(str(body["task"]))
            self._send(200, result)
        except Exception as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"审核页面：http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
