import unittest

try:
    from resource_pipeline.server import _delivery_payload
except ImportError:
    from server import _delivery_payload


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


if __name__ == "__main__":
    unittest.main()
