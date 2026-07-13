"""Create and start Xianyu/Goofish hotspot monitoring tasks.

The service exposes task creation as an HTTP API.  AI tasks are asynchronous:
create a generation job, poll it until it contains a task, then start that
task.  Keyword tasks are created directly by the same endpoint.

This module is intentionally idempotent by task name.  It does not publish
items, send messages, or log in to Xianyu.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = ROOT / "goofish_tasks.json"
DEFAULT_BASE_URL = "https://goofish.xiaolicloud.cn:18443"


class GoofishAPIError(RuntimeError):
    """Raised when the Goofish task API cannot complete an operation."""


def _load_env_file(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=value pairs without overwriting real environment vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class GoofishClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: int = 30,
        poll_seconds: float = 2.0,
        max_wait_seconds: int = 1800,
        token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.poll_seconds = max(0.2, poll_seconds)
        self.max_wait_seconds = max(1, max_wait_seconds)
        self.token = token or os.getenv("GOOFISH_API_TOKEN", "").strip()

    def request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "User-Agent": "GoofishHotspotBootstrap/1.0",
        }
        data: Optional[bytes] = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GoofishAPIError(
                f"闲鱼任务 API 返回 HTTP {exc.code}\nURL: {url}\n响应: {body[:2000]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GoofishAPIError(f"无法请求闲鱼任务 API：{url}\n{exc}") from exc
        except json.JSONDecodeError as exc:
            raise GoofishAPIError(f"闲鱼任务 API 未返回 JSON：{url}\n响应: {raw[:1000]}") from exc

    def list_tasks(self) -> List[Dict[str, Any]]:
        data = self.request_json("GET", "/api/tasks")
        return _as_task_list(data)

    def generate(self, payload: Dict[str, Any]) -> Any:
        return self.request_json("POST", "/api/tasks/generate", payload)

    def generation_job(self, job_id: str) -> Dict[str, Any]:
        data = self.request_json("GET", f"/api/tasks/generate-jobs/{job_id}")
        if isinstance(data, dict) and isinstance(data.get("job"), dict):
            return data["job"]
        if isinstance(data, dict):
            return data
        raise GoofishAPIError(f"生成作业响应格式异常：{_json_text(data)}")

    def wait_for_generation(self, job_id: str) -> Dict[str, Any]:
        started = time.monotonic()
        last_status = ""
        while True:
            job = self.generation_job(job_id)
            status = str(job.get("status") or "").lower()
            if status != last_status:
                print(f"AI 任务生成：job_id={job_id} status={status or 'unknown'}")
                last_status = status

            if status in {"completed", "complete", "success", "succeeded", "done"}:
                return job
            if status in {"failed", "error", "cancelled", "canceled"}:
                detail = job.get("error") or job.get("message") or _json_text(job)
                raise GoofishAPIError(f"AI 任务生成失败：{detail}")
            if time.monotonic() - started >= self.max_wait_seconds:
                raise GoofishAPIError(
                    f"AI 任务生成超时（{self.max_wait_seconds} 秒）：job_id={job_id}"
                )
            time.sleep(self.poll_seconds)

    def start(self, task_id: Any) -> Any:
        return self.request_json("POST", f"/api/tasks/start/{task_id}")

    def stop(self, task_id: Any) -> Any:
        return self.request_json("POST", f"/api/tasks/stop/{task_id}")


def _as_task_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("tasks", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get("data")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return _as_task_list(nested)
    return []


def _task_name(task: Dict[str, Any]) -> str:
    return str(task.get("task_name") or task.get("name") or task.get("title") or "").strip()


def _find_task_id(value: Any) -> Optional[Any]:
    """Find task.id in the response shapes used by different service versions."""
    if isinstance(value, dict):
        for key in ("task_id", "taskId"):
            if value.get(key) is not None:
                return value[key]
        task = value.get("task")
        if isinstance(task, dict) and task.get("id") is not None:
            return task["id"]
        if value.get("id") is not None and any(key in value for key in ("task_name", "name", "keyword", "status")):
            return value["id"]
        for key in ("data", "result"):
            found = _find_task_id(value.get(key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_task_id(item)
            if found is not None:
                return found
    return None


def _find_job_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("job_id", "jobId"):
            if value.get(key):
                return str(value[key])
        for key in ("job", "data", "result"):
            found = _find_job_id(value.get(key))
            if found:
                return found
    return None


def _payload_from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a local config entry into the service's documented payload."""
    required = ("task_name", "keyword", "decision_mode")
    missing = [key for key in required if not str(spec.get(key, "")).strip()]
    if missing:
        raise ValueError(f"热点任务缺少字段：{', '.join(missing)}")
    mode = str(spec["decision_mode"]).lower().strip()
    if mode not in {"ai", "keyword"}:
        raise ValueError(f"decision_mode 必须是 ai 或 keyword：{spec['task_name']}")

    payload: Dict[str, Any] = {
        "task_name": str(spec["task_name"]),
        "keyword": str(spec["keyword"]),
        "decision_mode": mode,
        "description": str(spec.get("description", "")),
        "analyze_images": bool(spec.get("analyze_images", mode == "ai")),
        "personal_only": bool(spec.get("personal_only", False)),
        "min_price": spec.get("min_price"),
        "max_price": spec.get("max_price"),
        "max_pages": int(spec.get("max_pages", 3)),
        "cron": spec.get("cron"),
        "account_strategy": spec.get("account_strategy", "auto"),
        "account_state_file": spec.get("account_state_file"),
        "free_shipping": bool(spec.get("free_shipping", False)),
        "new_publish_option": spec.get("new_publish_option"),
        "region": spec.get("region"),
        "keyword_rules": list(spec.get("keyword_rules", [])),
    }
    return payload


def load_specs(path: Path = DEFAULT_CONFIG_FILE) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"热点任务配置必须是 JSON 数组：{path}")
    specs = [item for item in data if isinstance(item, dict)]
    names = [_task_name(item) for item in specs]
    if len(names) != len(set(names)):
        raise ValueError("热点任务配置中存在重复的 task_name")
    return specs


def create_from_specs(
    client: GoofishClient,
    specs: Sequence[Dict[str, Any]],
    *,
    selected_names: Optional[Iterable[str]] = None,
    start_created: bool = False,
    skip_existing: bool = True,
    start_existing: bool = False,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    selected = {name.strip() for name in (selected_names or []) if name.strip()}
    selected_specs = [spec for spec in specs if not selected or _task_name(spec) in selected]
    if selected and len(selected_specs) != len(selected):
        known = {_task_name(spec) for spec in specs}
        unknown = sorted(selected - known)
        raise ValueError(f"找不到配置中的任务：{', '.join(unknown)}")
    if not selected_specs:
        raise ValueError("没有可创建的热点任务")

    existing = [] if dry_run else client.list_tasks()
    existing_by_name = {_task_name(task): task for task in existing if _task_name(task)}
    results: List[Dict[str, Any]] = []

    for spec in selected_specs:
        name = _task_name(spec)
        payload = _payload_from_spec(spec)
        old = existing_by_name.get(name)
        if old and skip_existing:
            task_id = _find_task_id(old)
            result: Dict[str, Any] = {"task_name": name, "status": "skipped_existing", "task_id": task_id}
            if start_existing and task_id is not None and not dry_run:
                result["start_response"] = client.start(task_id)
                result["status"] = "existing_started"
            results.append(result)
            print(f"跳过已存在任务：{name}" + (f" task_id={task_id}" if task_id is not None else ""))
            continue

        if dry_run:
            results.append({"task_name": name, "status": "dry_run", "payload": payload})
            print(f"[dry-run] 将创建：{name}")
            continue

        print(f"创建热点任务：{name} mode={payload['decision_mode']}")
        create_response = client.generate(payload)
        task_id = _find_task_id(create_response)
        job_id = _find_job_id(create_response)
        if payload["decision_mode"] == "ai":
            if not job_id:
                raise GoofishAPIError(f"AI 创建响应没有 job_id：{_json_text(create_response)}")
            job = client.wait_for_generation(job_id)
            task_id = _find_task_id(job) or task_id
        elif task_id is None:
            raise GoofishAPIError(f"关键词任务创建响应没有 task_id：{_json_text(create_response)}")

        result = {"task_name": name, "status": "created", "task_id": task_id}
        if start_created and task_id is not None:
            result["start_response"] = client.start(task_id)
            result["status"] = "created_started"
        results.append(result)
        print(f"任务创建完成：{name} task_id={task_id}")

    return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量创建闲鱼热点监控任务")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("GOOFISH_TASKS_FILE", DEFAULT_CONFIG_FILE)))
    parser.add_argument("--task", action="append", dest="task_names", help="只处理指定任务名，可重复传入")
    parser.add_argument("--base-url", default=os.getenv("GOOFISH_API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("GOOFISH_API_TIMEOUT", "30")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("GOOFISH_POLL_SECONDS", "2")))
    parser.add_argument("--max-wait-seconds", type=int, default=int(os.getenv("GOOFISH_MAX_WAIT_SECONDS", "1800")))
    parser.add_argument("--start", action="store_true", help="创建成功后立即启动新任务")
    parser.add_argument("--start-existing", action="store_true", help="已存在的任务也调用启动接口")
    parser.add_argument("--no-skip-existing", action="store_true", help="允许创建同名任务；通常不建议使用")
    parser.add_argument("--dry-run", action="store_true", help="只打印将提交的 JSON，不请求远端 API")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    _load_env_file()
    args = build_arg_parser().parse_args(argv)
    try:
        specs = load_specs(args.config)
        client = GoofishClient(
            args.base_url,
            timeout=args.timeout,
            poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait_seconds,
        )
        results = create_from_specs(
            client,
            specs,
            selected_names=args.task_names,
            start_created=args.start,
            start_existing=args.start_existing,
            skip_existing=not args.no_skip_existing,
            dry_run=args.dry_run,
        )
        print(_json_text(results))
        return 0
    except (OSError, ValueError, GoofishAPIError) as exc:
        print(f"闲鱼任务同步失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
