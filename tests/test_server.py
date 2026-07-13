import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    from resource_pipeline import server
except ImportError:
    import server

_delivery_payload = server._delivery_payload


class ServerTests(unittest.TestCase):
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
