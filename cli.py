from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from .pipeline import load_tasks, run_named_task
except ImportError:  # direct invocation: python resource_pipeline/cli.py
    from pipeline import load_tasks, run_named_task


def main() -> int:
    parser = argparse.ArgumentParser(description="本地热点监控与授权资源待审核流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出监控任务")
    run = sub.add_parser("run", help="运行指定任务")
    run.add_argument("--task", required=True, help="tasks.json 中的任务名称")
    run.add_argument("--include-seen", action="store_true", help="包含之前已经发现过的条目")

    serve = sub.add_parser("serve", help="启动本地审核页面")
    serve.add_argument("--host", default=os.getenv("PIPELINE_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("PIPELINE_PORT", "8765")))

    goofish = sub.add_parser("goofish-create", help="批量创建闲鱼热点监控任务")
    # goofish_tasks.py owns its own options. They are forwarded from the
    # command dispatcher below so the two CLIs do not drift apart.

    args, extra = parser.parse_known_args()
    if args.command != "goofish-create" and extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    if args.command == "list":
        for task in load_tasks():
            print(f"- {task.get('name')} [{task.get('source')}] keywords={','.join(task.get('keywords', []))}")
        return 0
    if args.command == "run":
        try:
            print(json.dumps(run_named_task(args.task, include_seen=args.include_seen), ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:  # CLI should print a concise actionable error.
            print(f"运行失败：{exc}", file=sys.stderr)
            return 1
    if args.command == "serve":
        try:
            from .server import serve
        except ImportError:
            from server import serve

        serve(args.host, args.port)
        return 0
    if args.command == "goofish-create":
        try:
            from .goofish_tasks import main as goofish_main
        except ImportError:
            from goofish_tasks import main as goofish_main

        return goofish_main(extra)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
