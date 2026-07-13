import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from resource_pipeline import pipeline
except ImportError:
    import pipeline

parse_metric = pipeline.parse_metric
parse_wp_post = pipeline.parse_wp_post
run_task = pipeline.run_task
score_item = pipeline.score_item
strip_html = pipeline.strip_html


class PipelineTests(unittest.TestCase):
    def test_strip_html_and_metric(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")
        self.assertEqual(parse_metric("3.13K"), 3130)
        self.assertEqual(parse_metric("1.2M"), 1200000)

    def test_parse_post_extracts_cover_and_categories(self):
        raw = {
            "id": 1,
            "date": "2026-07-13T10:00:00",
            "link": "https://example.test/post/",
            "title": {"rendered": "AI 工程 | English"},
            "content": {"rendered": '<p><img data-src="/cover.jpg" /></p><p>摘要</p>'},
            "excerpt": {"rendered": ""},
            "categories": [7],
            "tags": []
        }
        item = parse_wp_post(raw, {7: "人工智能"})
        self.assertEqual(item["title"], "AI 工程")
        self.assertEqual(item["categories"], ["人工智能"])
        self.assertEqual(item["cover_url"], "https://example.test/cover.jpg")

    def test_score(self):
        item = score_item({"title": "Python AI Agent", "categories": ["人工智能"], "published_at": "2026-07-13T00:00:00+00:00", "cover_url": "x"}, ["Python", "Agent"])
        self.assertEqual(item["matched_keywords"], ["python", "agent"])
        self.assertGreater(item["hotness_score"], 30)

    def test_run_is_review_first(self):
        task = {
            "name": "测试",
            "source": "theitzy",
            "keywords": ["AI"],
            "rights_confirmed": False,
            "authorized_assets": False,
            "source_config": {"base_url": "https://theitzy.net"}
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pipeline, "fetch_source", return_value=[{
                "id": "theitzy:1", "title": "AI 课程", "page_url": "https://theitzy.net/a/", "published_at": "2026-07-13T00:00:00+00:00", "categories": ["AI"], "summary": "摘要", "cover_url": ""
            }]):
                summary = run_task(task, output_dir=Path(tmp))
            self.assertEqual(summary["count"], 1)
            item_dir = next(Path(tmp).glob("*/01-*"))
            self.assertIn("禁止发布", (item_dir / "copy.md").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((item_dir / "item.json").read_text(encoding="utf-8"))["rights_review"], "required")


if __name__ == "__main__":
    unittest.main()
