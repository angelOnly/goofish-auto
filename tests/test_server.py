import unittest
import json
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
    def test_clean_error_message_summarizes_gateway_html(self):
        message = server._clean_error_message("<html><title>504 Gateway Time-out</title><body>openresty</body></html>")
        self.assertIn("504", message)
        self.assertNotIn("<html>", message)

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
            with patch.object(server, "OUTPUT_DIR", output_dir):
                data = server._read_output_item("run/01-item")
            self.assertEqual(data["copy_display"], "AI 文案正文")
            self.assertEqual(data["copy_source"], "ai")


if __name__ == "__main__":
    unittest.main()
