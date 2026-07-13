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
parse_goofish_result_record = pipeline.parse_goofish_result_record
apply_market_signals = pipeline.apply_market_signals
selection_db_path = pipeline.selection_db_path
set_course_published = pipeline.set_course_published


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

    def test_score_matches_summary_text(self):
        item = score_item({"title": "项目资料", "categories": [], "summary": "包含 Cursor 和 MCP 工作流", "published_at": "2026-07-13T00:00:00+00:00", "cover_url": ""}, ["Cursor", "MCP"])
        self.assertEqual(item["matched_keywords"], ["cursor", "mcp"])

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
            self.assertNotIn("禁止发布", (item_dir / "copy.md").read_text(encoding="utf-8"))
            self.assertIn("禁止发布", (item_dir / "delivery.md").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((item_dir / "item.json").read_text(encoding="utf-8"))["rights_review"], "required")

    def test_only_published_selection_is_filtered_next_time(self):
        raw = [{
            "id": "theitzy:1",
            "title": "AI 课程",
            "page_url": "https://theitzy.net/a/",
            "published_at": "2026-07-13T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "摘要",
            "cover_url": "",
        }]
        task = {"name": "任务A", "source": "theitzy", "keywords": ["AI"], "source_config": {"base_url": "https://theitzy.net"}}
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(pipeline, "fetch_source", return_value=raw):
                self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
                self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
                set_course_published("theitzy:1", True, selection_db_path(output_dir))
                skipped = run_task(task, output_dir=output_dir)
                included = run_task(task, output_dir=output_dir, include_seen=True)
                set_course_published("theitzy:1", False, selection_db_path(output_dir))
                unblocked = run_task(task, output_dir=output_dir)
            self.assertEqual(skipped["count"], 0)
            self.assertEqual(skipped["diagnostics"]["skipped_published_count"], 1)
            self.assertEqual(included["count"], 1)
            self.assertEqual(unblocked["count"], 1)

    def test_run_filters_old_and_excluded_items(self):
        raw = [
            {
                "id": "theitzy:old",
                "title": "AI 课程旧资料",
                "page_url": "https://theitzy.net/old/",
                "published_at": "2000-01-01T00:00:00+00:00",
                "categories": ["AI"],
                "summary": "项目实战",
                "cover_url": "",
            },
            {
                "id": "theitzy:remote",
                "title": "AI 课程远程安装配置",
                "page_url": "https://theitzy.net/remote/",
                "published_at": "2999-01-01T00:00:00+00:00",
                "categories": ["AI"],
                "summary": "项目实战",
                "cover_url": "",
            },
        ]
        task = {
            "name": "过滤测试",
            "source": "theitzy",
            "keywords": ["AI课程", "项目实战"],
            "exclude_keywords": ["远程安装"],
            "max_age_days": 15,
            "source_config": {"base_url": "https://theitzy.net"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pipeline, "fetch_source", return_value=raw):
                summary = run_task(task, output_dir=Path(tmp), include_seen=True)
            self.assertEqual(summary["count"], 0)
            self.assertEqual(summary["diagnostics"]["skipped_old_count"], 1)
            self.assertEqual(summary["diagnostics"]["skipped_excluded_count"], 1)

    def test_goofish_market_signal_boosts_matching_course(self):
        raw_market = {
            "商品信息": {
                "商品标题": "Vercel AI 大师课程 RAG 项目实战",
                "当前售价": "¥9.9",
                "“想要”人数": 8,
                "浏览量": 50,
                "商品链接": "https://goofish.test/item",
            },
            "ai_analysis": {
                "is_recommended": True,
                "matched_keywords": ["Vercel AI", "RAG"],
                "keyword_hit_count": 2,
            },
        }
        signal = parse_goofish_result_record(raw_market, ["Vercel AI", "RAG"])
        self.assertIsNotNone(signal)
        course = score_item(
            {
                "title": "Vercel AI 大师课程: 从零开始构建现代化AI应用",
                "categories": ["AI"],
                "summary": "包含 RAG 项目",
                "published_at": "2999-01-01T00:00:00+00:00",
                "cover_url": "",
            },
            ["Vercel AI", "RAG"],
        )
        boosted = apply_market_signals(course, [signal])
        self.assertGreater(boosted["hotness_score"], course["hotness_score"])
        self.assertIn("vercel ai", boosted["market_matched_terms"])
        self.assertEqual(boosted["market_median_price"], 9.9)
        self.assertEqual(boosted["market_price_min"], 9.9)
        self.assertEqual(boosted["market_price_max"], 9.9)


if __name__ == "__main__":
    unittest.main()
