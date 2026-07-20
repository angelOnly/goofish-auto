import unittest
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

try:
    from resource_pipeline import server
except ImportError:
    import server

_delivery_payload = server._delivery_payload


class ServerTests(unittest.TestCase):
    def test_dashboard_script_is_served_as_a_same_origin_resource(self):
        self.assertIn('<script src="/static/dashboard.js" defer></script>', server.HTML)
        self.assertNotIn("refreshAll();", server.HTML)
        self.assertIn("refreshAll();", server.DASHBOARD_JS)
        self.assertIn("deliveryScreenshotHtml", server.DASHBOARD_JS)
        self.assertIn("memberRequestBreakdown", server.DASHBOARD_JS)
        self.assertIn("memberUnique", server.DASHBOARD_JS)
        self.assertIn("sourceDuplicates", server.DASHBOARD_JS)
        self.assertIn('id="memberCookie" type="password"', server.HTML)
        self.assertIn("saveMemberCookieFromPage", server.DASHBOARD_JS)
        self.assertIn("/api/local/member-cookie", server.DASHBOARD_JS)
        self.assertIn("join('\\n');", server.DASHBOARD_JS)
        self.assertNotIn("join('\n');", server.DASHBOARD_JS)

    def test_clean_error_message_summarizes_gateway_html(self):
        message = server._clean_error_message("<html><title>504 Gateway Time-out</title><body>openresty</body></html>")
        self.assertIn("504", message)
        self.assertNotIn("<html>", message)

    def test_member_cookie_update_validates_persists_and_activates(self):
        cookie = "PHPSESSID=session; wordpress_logged_in_test=jiangzb%7Ctoken"
        task = {
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / ".theitzy_cookie"
            with patch.dict(os.environ, {"THEITZY_COOKIE": "old-cookie"}):
                with patch.object(server, "load_tasks", return_value=[task]), patch.object(
                    server,
                    "validate_member_cookie",
                    return_value={"username": "jiangzb"},
                ) as validate:
                    result = server._save_member_cookie(cookie, cookie_path)
                self.assertEqual(os.environ["THEITZY_COOKIE"], cookie)
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), cookie)
        validate.assert_called_once_with(task, cookie_override=cookie)
        self.assertEqual(result, {"configured": True, "username": "jiangzb"})
        self.assertNotIn(cookie, json.dumps(result))

    def test_member_cookie_update_rejects_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "换行"):
                server._save_member_cookie(
                    "wordpress_logged_in_test=jiangzb%7Ctoken\nextra=value",
                    Path(tmp) / ".theitzy_cookie",
                )

    def test_local_run_job_finishes_in_background(self):
        with patch.object(server, "run_named_task", return_value={"run_id": "run-1", "count": 2, "diagnostics": {}}):
            job = server._start_local_run_job("测试任务", False)
            job_id = str(job["job_id"])
            for _ in range(20):
                snapshot = server._job_snapshot(job_id)
                if snapshot.get("status") == "done":
                    break
                time.sleep(0.05)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["result"]["run_id"], "run-1")

    def test_local_run_rejects_overlapping_job(self):
        marker = "test-busy-job"
        with server.LOCAL_RUN_LOCK:
            server.LOCAL_RUN_JOBS[marker] = {"status": "running"}
        try:
            with self.assertRaisesRegex(RuntimeError, "已有本地整理任务运行中"):
                server._start_local_run_job("另一个任务", False)
        finally:
            with server.LOCAL_RUN_LOCK:
                server.LOCAL_RUN_JOBS.pop(marker, None)

    def test_local_run_request_id_is_idempotent(self):
        request_id = "same-browser-request"
        with patch.object(server, "run_named_task", return_value={"run_id": "run-1", "count": 1, "diagnostics": {}}) as run:
            first = server._start_local_run_job("测试任务", False, request_id)
            second = server._start_local_run_job("测试任务", False, request_id)
            job_id = str(first["job_id"])
            for _ in range(20):
                snapshot = server._job_snapshot(job_id)
                if snapshot.get("status") == "done":
                    break
                time.sleep(0.05)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(run.call_count, 1)
        with server.LOCAL_RUN_LOCK:
            server.LOCAL_RUN_REQUESTS.pop(request_id, None)

    def test_list_run_summaries_paginates_three_per_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            for index in range(7):
                run_id = f"20260714-00000{index}"
                run_dir = output_dir / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "summary.json").write_text(
                    json.dumps({"run_id": run_id, "task_name": "任务", "count": index}, ensure_ascii=False),
                    encoding="utf-8",
                )
            with patch.object(server, "OUTPUT_DIR", output_dir):
                page_two = server._list_run_summaries(page=2, page_size=3)
        self.assertEqual(page_two["total"], 7)
        self.assertEqual(page_two["total_pages"], 3)
        self.assertEqual(page_two["page"], 2)
        self.assertEqual(page_two["count"], 3)
        self.assertEqual(page_two["items"][0]["run_id"], "20260714-000003")

    def test_list_published_items_includes_delivery_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            db_path = output_dir / "selection.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE course_selections (
                            course_id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            source TEXT,
                            page_url TEXT,
                            first_selected_at TEXT NOT NULL,
                            last_selected_at TEXT NOT NULL,
                            last_run_id TEXT,
                            last_task_name TEXT,
                            selection_count INTEGER NOT NULL DEFAULT 0,
                            published INTEGER NOT NULL DEFAULT 0,
                            published_at TEXT,
                            updated_at TEXT NOT NULL,
                            last_hotness_score REAL,
                            last_market_match_score REAL,
                            raw_json TEXT
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO course_selections (
                            course_id, title, source, page_url, first_selected_at, last_selected_at,
                            selection_count, published, published_at, updated_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "theitzy:published",
                            "AI 发布课程",
                            "theitzy",
                            "https://theitzy.net/published/",
                            "2026-07-14T00:00:00+08:00",
                            "2026-07-14T00:00:00+08:00",
                            1,
                            1,
                            "2026-07-14T00:10:00+08:00",
                            "2026-07-14T00:10:00+08:00",
                            json.dumps(
                                {
                                    "id": "theitzy:published",
                                    "title": "AI 发布课程",
                                    "rights_review": "confirmed",
                                    "copy": "已保存文案",
                                    "member_delivery": {
                                        "links": ["https://pan.baidu.com/s/1abc"],
                                        "passwords": ["p123"],
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
            finally:
                conn.close()
            with patch.object(server, "OUTPUT_DIR", output_dir):
                data = server._list_published_items("发布")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["copy_display"], "已保存文案")
        self.assertIn("https://pan.baidu.com/s/1abc", data["items"][0]["delivery_payload"])
        self.assertIn("p123", data["items"][0]["delivery_payload"])

    def test_list_published_items_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            db_path = output_dir / "selection.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE course_selections (
                            course_id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            source TEXT,
                            page_url TEXT,
                            first_selected_at TEXT NOT NULL,
                            last_selected_at TEXT NOT NULL,
                            last_run_id TEXT,
                            last_task_name TEXT,
                            selection_count INTEGER NOT NULL DEFAULT 0,
                            published INTEGER NOT NULL DEFAULT 0,
                            published_at TEXT,
                            updated_at TEXT NOT NULL,
                            last_hotness_score REAL,
                            last_market_match_score REAL,
                            raw_json TEXT
                        )
                        """
                    )
                    for index in range(25):
                        conn.execute(
                            """
                            INSERT INTO course_selections (
                                course_id, title, source, page_url, first_selected_at, last_selected_at,
                                selection_count, published, published_at, updated_at, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"theitzy:{index:02d}",
                                f"课程 {index:02d}",
                                "theitzy",
                                f"https://theitzy.net/{index:02d}/",
                                "2026-07-14T00:00:00+08:00",
                                "2026-07-14T00:00:00+08:00",
                                1,
                                1,
                                f"2026-07-14T00:{index:02d}:00+08:00",
                                f"2026-07-14T00:{index:02d}:00+08:00",
                                json.dumps({"id": f"theitzy:{index:02d}", "title": f"课程 {index:02d}"}, ensure_ascii=False),
                            ),
                        )
            finally:
                conn.close()
            with patch.object(server, "OUTPUT_DIR", output_dir):
                page_two = server._list_published_items("", 10, 2)
        self.assertEqual(page_two["total"], 25)
        self.assertEqual(page_two["total_pages"], 3)
        self.assertEqual(page_two["page"], 2)
        self.assertEqual(page_two["count"], 10)

    def test_delivery_payload_contains_only_link_and_password(self):
        payload, status = _delivery_payload(
            {
                "rights_review": "confirmed",
                "member_delivery": {
                    "links": ["https://pan.baidu.com/s/1abc"],
                    "passwords": ["exn7"],
                },
            }
        )
        self.assertEqual(payload, "百度网盘链接：https://pan.baidu.com/s/1abc\n提取码/文件密码：exn7")
        self.assertIn("已获取", status)

    def test_delivery_payload_prefers_manual_payload(self):
        payload, status = _delivery_payload(
            {
                "rights_review": "confirmed",
                "manual_delivery_payload": "百度网盘链接：https://pan.baidu.com/s/manual\n提取码：m123",
                "member_delivery": {
                    "links": ["https://pan.baidu.com/s/auto"],
                    "passwords": ["auto"],
                },
            }
        )
        self.assertIn("/manual", payload)
        self.assertIn("手动补充", status)

    def test_save_manual_delivery_updates_published_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            db_path = output_dir / "selection.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE course_selections (
                            course_id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            source TEXT,
                            page_url TEXT,
                            first_selected_at TEXT NOT NULL,
                            last_selected_at TEXT NOT NULL,
                            last_run_id TEXT,
                            last_task_name TEXT,
                            selection_count INTEGER NOT NULL DEFAULT 0,
                            published INTEGER NOT NULL DEFAULT 0,
                            published_at TEXT,
                            updated_at TEXT NOT NULL,
                            last_hotness_score REAL,
                            last_market_match_score REAL,
                            raw_json TEXT
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO course_selections (
                            course_id, title, source, page_url, first_selected_at, last_selected_at,
                            selection_count, published, published_at, updated_at, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "theitzy:manual",
                            "手动发货课程",
                            "theitzy",
                            "https://theitzy.net/manual/",
                            "2026-07-14T00:00:00+08:00",
                            "2026-07-14T00:00:00+08:00",
                            1,
                            1,
                            "2026-07-14T00:10:00+08:00",
                            "2026-07-14T00:10:00+08:00",
                            json.dumps(
                                {
                                    "id": "theitzy:manual",
                                    "title": "手动发货课程",
                                    "rights_review": "confirmed",
                                    "copy": "文案",
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
            finally:
                conn.close()
            manual = "百度网盘链接：https://pan.baidu.com/s/manual\n提取码：m123"
            with patch.object(server, "OUTPUT_DIR", output_dir):
                saved = server._save_manual_delivery("theitzy:manual", manual)
                listed = server._list_published_items("手动")
            self.assertEqual(saved["delivery_payload"], manual)
            self.assertEqual(listed["items"][0]["delivery_payload"], manual)

    def test_delivery_payload_empty_when_rights_unconfirmed(self):
        payload, status = _delivery_payload(
            {
                "rights_review": "required",
                "member_delivery": {
                    "status": "skipped_rights_unconfirmed",
                    "links": [],
                    "passwords": [],
                },
            }
        )
        self.assertEqual(payload, "")
        self.assertIn("未确认分发权", status)

    def test_delivery_payload_reports_quota_exhaustion(self):
        payload, status = _delivery_payload(
            {
                "rights_review": "confirmed",
                "member_delivery": {
                    "status": "quota_exhausted",
                    "message": "今日免费下载次数已用【15】，剩余【0】",
                },
            }
        )
        self.assertEqual(payload, "")
        self.assertIn("剩余【0】", status)

    def test_delivery_payload_reports_request_budget_exhaustion(self):
        payload, status = _delivery_payload(
            {
                "rights_review": "confirmed",
                "member_delivery": {
                    "status": "request_budget_exhausted",
                    "message": "本次运行已达到会员请求预算（10 条）",
                },
            }
        )
        self.assertEqual(payload, "")
        self.assertIn("预算（10 条）", status)

    def test_read_output_item_prefers_saved_ai_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            item_dir = output_dir / "run" / "01-item"
            item_dir.mkdir(parents=True)
            (item_dir / "copy.md").write_text("AI 文案正文", encoding="utf-8")
            (item_dir / "delivery.md").write_text("delivery", encoding="utf-8")
            (item_dir / "item.json").write_text(
                json.dumps(
                    {
                        "title": "测试课程",
                        "rights_review": "confirmed",
                        "copy_source": "ai",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original_filter = server.apply_content_rules
            fixed_filter = lambda value: original_filter(value, {"forbidden_words": ["chatgpt", "gpt"], "replacement": "AI工具"})
            with patch.object(server, "OUTPUT_DIR", output_dir), patch.object(server, "apply_content_rules", fixed_filter):
                data = server._read_output_item("run/01-item")
            self.assertEqual(data["copy_display"], "AI 文案正文")
            self.assertEqual(data["copy_source"], "ai")

    def test_read_output_item_applies_content_rules_to_old_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            item_dir = output_dir / "run" / "01-item"
            item_dir.mkdir(parents=True)
            (item_dir / "copy.md").write_text("ChatGPT 和 GPT 课程文案", encoding="utf-8")
            (item_dir / "delivery.md").write_text("delivery", encoding="utf-8")
            (item_dir / "item.json").write_text(
                json.dumps({"title": "测试课程", "rights_review": "confirmed"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(server, "OUTPUT_DIR", output_dir):
                data = server._read_output_item("run/01-item")
            self.assertNotRegex(data["copy_display"].lower(), r"chatgpt|gpt")


if __name__ == "__main__":
    unittest.main()
