import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
ai_copy_config = pipeline.ai_copy_config
extract_baidu_delivery_from_html = pipeline.extract_baidu_delivery_from_html
extract_member_download_context = pipeline.extract_member_download_context
selection_db_path = pipeline.selection_db_path
set_course_published = pipeline.set_course_published
validate_member_cookie = pipeline.validate_member_cookie
apply_content_rules = pipeline.apply_content_rules
save_content_rules = pipeline.save_content_rules
load_content_rules = pipeline.load_content_rules


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch.object(pipeline, "ai_copy_configured", return_value=False),
            patch.object(pipeline, "maybe_ai_copy", return_value=""),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()

    def test_strip_html_and_metric(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")
        self.assertEqual(parse_metric("3.13K"), 3130)
        self.assertEqual(parse_metric("1.2M"), 1200000)

    def test_content_rules_replace_forbidden_words(self):
        rules = {"forbidden_words": ["chatgpt", "gpt"], "replacement": "AI工具"}
        text = apply_content_rules("ChatGPT 和 GPT-4 实战课", rules)
        self.assertEqual(text, "AI工具 和 AI工具-4 实战课")
        self.assertNotRegex(text.lower(), r"chatgpt|gpt")

    def test_content_rules_save_accepts_textarea_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content_rules.json"
            saved = save_content_rules({"forbidden_words": "chatgpt\ngpt，OpenAI", "replacement": "AI工具"}, path)
            loaded = load_content_rules(path)
        self.assertEqual(saved["forbidden_words"], ["chatgpt", "gpt", "OpenAI"])
        self.assertEqual(loaded["replacement"], "AI工具")

    def test_saved_member_cookie_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            cookie_path = root / "output" / pipeline.MEMBER_COOKIE_FILE.name
            env_path.write_text("THEITZY_COOKIE='old-cookie'\n", encoding="utf-8")
            cookie_path.parent.mkdir()
            cookie_path.write_text("new-cookie", encoding="utf-8")
            with patch.dict(os.environ, {"THEITZY_COOKIE": "process-cookie"}):
                pipeline.load_env_file(env_path)
                self.assertEqual(os.environ["THEITZY_COOKIE"], "new-cookie")

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

    def test_openai_env_aliases_are_supported(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "key",
                "OPENAI_BASE_URL": "https://llm.example/v1",
                "OPENAI_MODEL_NAME": "model-a",
                "AI_API_KEY": "",
                "AI_BASE_URL": "",
                "AI_MODEL": "",
            },
        ):
            self.assertEqual(
                ai_copy_config(),
                {"api_key": "key", "base_url": "https://llm.example/v1", "model": "model-a"},
            )

    def test_extracts_baidu_delivery_from_member_html(self):
        delivery = extract_baidu_delivery_from_html(
            '<a href="https://pan.baidu.com/s/1Ab_cdE">立即下载</a><span>文件密码：exn7</span>',
            "https://theitzy.net/course/",
        )
        self.assertEqual(delivery["status"], "found")
        self.assertEqual(delivery["links"], ["https://pan.baidu.com/s/1Ab_cdE"])
        self.assertEqual(delivery["passwords"], ["exn7"])

    def test_extracts_member_download_context(self):
        context = extract_member_download_context(
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a target="_blank" data-id="25611" class="go-down btn">立即下载</a>',
            "https://theitzy.net/course/",
        )
        self.assertEqual(context["post_id"], "25611")
        self.assertEqual(context["ajax_url"], "https://theitzy.net/wp-admin/admin-ajax.php")

    def test_template_copy_uses_course_outline_and_omits_internal_market_text(self):
        copy = pipeline.template_copy(
            {
                "title": "AI 数据平台实战 | English title",
                "categories": ["人工智能"],
                "summary": "简短摘要",
                "course_outline": "从评估文本转SQL失败原因开始。设计语义层并实施权限治理。构建受保护的MCP服务。",
                "matched_keywords": ["AI"],
            },
            {"rights_confirmed": False},
        )
        self.assertIn("设计语义层并实施权限治理", copy)
        self.assertNotIn("市场参考", copy)
        self.assertNotIn("选品需求判断", copy)

    def test_member_delivery_stops_after_quota_response(self):
        task = {
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        item = {
            "id": "theitzy:quota",
            "source_id": 25611,
            "page_url": "https://theitzy.net/course/",
        }
        page_html = (
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a data-id="25611" class="go-down btn">立即下载</a>'
        )
        quota_message = "今日免费下载次数已用〖15〗，剩余〖0〗"
        state = {}
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(pipeline, "http_text", return_value=page_html), patch.object(
                pipeline,
                "http_form_json",
                return_value={"status": 0, "msg": quota_message},
            ) as ajax:
                first = pipeline.fetch_member_delivery(item, task, [], state)
                second = pipeline.fetch_member_delivery(item, task, [], state)
        self.assertEqual(first["status"], "quota_exhausted")
        self.assertEqual(second["status"], "quota_exhausted")
        ajax.assert_called_once()

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

    def test_delivery_screenshot_config_clamps_count(self):
        self.assertEqual(
            pipeline._delivery_screenshot_config({"source_config": {"capture_delivery_screenshots": True, "delivery_screenshot_count": 9, "delivery_screenshot_timeout": 999}}),
            (True, 3, 120),
        )

    def test_delivery_screenshot_skips_without_rights(self):
        result = pipeline.capture_baidu_delivery_screenshots(
            {"member_delivery": {"links": ["https://pan.baidu.com/s/mock"]}},
            {"rights_confirmed": False, "source_config": {"capture_delivery_screenshots": True}},
            Path("output"),
        )
        self.assertEqual(result["status"], "skipped_rights_unconfirmed")

    def test_delivery_screenshot_does_not_succeed_on_outer_share_page(self):
        submit_seen = {"value": False}

        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector
                self.first = self

            def is_visible(self, timeout=0):
                return "input[" in self.selector or self.selector == "#submitBtn"

            def fill(self, value):
                self.value = value

            def click(self, timeout=0):
                if self.selector == "#submitBtn":
                    submit_seen["value"] = True
                self.clicked = True

        class FakePage:
            url = "https://pan.baidu.com/s/mock"

            def goto(self, *args, **kwargs):
                return None

            def wait_for_load_state(self, *args, **kwargs):
                return None

            def locator(self, selector):
                return FakeLocator(selector)

            def wait_for_timeout(self, *args, **kwargs):
                return None

            def evaluate(self, script, *args):
                if script.lstrip().startswith("() =>"):
                    return {
                        "hasPasswordInput": True,
                        "hasDetailSelector": False,
                        "markerHits": [],
                        "isDetail": False,
                        "url": self.url,
                    }
                return 0

            def screenshot(self, *args, **kwargs):
                raise AssertionError("outer page must never be captured")

        class FakeContext:
            def new_page(self):
                return FakePage()

            def close(self):
                return None

        class FakeBrowser:
            def new_context(self, **kwargs):
                return FakeContext()

            def close(self):
                return None

        fake_playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **kwargs: FakeBrowser())
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("playwright.sync_api.sync_playwright") as sync_playwright:
                sync_playwright.return_value.__enter__.return_value = fake_playwright
                result = pipeline.capture_baidu_delivery_screenshots(
                    {
                        "member_delivery": {
                            "links": ["https://pan.baidu.com/s/mock"],
                            "passwords": ["abcd"],
                        }
                    },
                    {
                        "rights_confirmed": True,
                        "source_config": {
                            "capture_delivery_screenshots": True,
                            "delivery_screenshot_timeout": 5,
                        },
                    },
                    Path(tmp),
                )
        self.assertEqual(result["status"], "not_detail")
        self.assertEqual(result["paths"], [])
        self.assertEqual(result["count"], 0)
        self.assertTrue(submit_seen["value"])

    def test_delivery_screenshot_reports_missing_browser_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("playwright.sync_api.sync_playwright") as sync_playwright:
                sync_playwright.return_value.__enter__.return_value = SimpleNamespace(
                    chromium=SimpleNamespace(
                        launch=lambda **kwargs: (_ for _ in ()).throw(
                            RuntimeError("Executable doesn't exist; run playwright install")
                        )
                    )
                )
                result = pipeline.capture_baidu_delivery_screenshots(
                    {"member_delivery": {"links": ["https://pan.baidu.com/s/mock"]}},
                    {"rights_confirmed": True, "source_config": {"capture_delivery_screenshots": True}},
                    Path(tmp),
                )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("playwright install chromium", result["message"])

    @unittest.skipUnless(os.getenv("RUN_LIVE_THEITZY_TEST") == "1", "live TheItzy test")
    def test_live_end_to_end_ai_engineering_delivery_and_screenshot(self):
        """Spend exactly one member download request, then capture the Baidu detail page."""

        pipeline.load_env_file()
        self.assertTrue(
            os.getenv("THEITZY_COOKIE", "").strip(),
            "THEITZY_COOKIE is required in .env for the live test",
        )
        item = {
            "id": "theitzy:end-to-end-ai-engineering",
            "page_url": "https://theitzy.net/end-to-end-ai-engineering/",
        }
        task = {
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
                "member_timeout": 45,
                "capture_delivery_screenshots": True,
                "delivery_screenshot_count": 1,
                "delivery_screenshot_timeout": 45,
            },
        }
        quota_state = {
            "_requested_post_ids": set(),
            "_delivery_cache": {},
            "_ajax_request_count": 0,
            "_ajax_request_budget": 1,
        }
        asset_dir = pipeline.OUTPUT_DIR / "live-test-end-to-end-ai-engineering" / "assets"

        with pipeline._exclusive_run_lock(pipeline.OUTPUT_DIR):
            pipeline.validate_member_cookie(task)
            with patch.object(
                pipeline, "http_form_json", wraps=pipeline.http_form_json
            ) as ajax_request, patch.object(
                pipeline, "http_text", wraps=pipeline.http_text
            ) as text_request:
                delivery = pipeline.fetch_member_delivery(item, task, [], quota_state)

            self.assertIsInstance(delivery, dict)
            self.assertEqual(ajax_request.call_count, 1)
            self.assertEqual(quota_state["_ajax_request_count"], 1)
            self.assertEqual(delivery.get("ajax_request_count"), 1)
            links = [str(link) for link in delivery.get("links", [])]
            self.assertTrue(
                any("pan.baidu.com" in link.lower() for link in links),
                delivery.get("message") or delivery.get("status"),
            )

            item["member_delivery"] = delivery
            screenshot = pipeline.capture_baidu_delivery_screenshots(item, task, asset_dir)

        go_requests = [call for call in text_request.call_args_list if call.kwargs.get("return_url")]
        self.assertEqual(delivery.get("status"), "found", delivery.get("message"))
        self.assertEqual(delivery.get("go_request_count"), 1)
        self.assertEqual(len(go_requests), 1)
        self.assertEqual(screenshot.get("status"), "found", screenshot.get("message"))
        self.assertGreaterEqual(len(screenshot.get("paths", [])), 1)
        for relative_path in screenshot["paths"]:
            screenshot_path = pipeline.OUTPUT_DIR / relative_path
            self.assertTrue(screenshot_path.is_file(), screenshot_path)
            self.assertGreater(screenshot_path.stat().st_size, 0)

    def test_member_delivery_is_written_when_authorized_and_cookie_is_available(self):
        task = {
            "name": "会员链接测试",
            "source": "theitzy",
            "keywords": ["AI"],
            "rights_confirmed": True,
            "authorized_assets": False,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        raw = [{
            "id": "theitzy:member",
            "title": "AI 会员课程",
            "page_url": "https://theitzy.net/member/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "摘要",
            "cover_url": "",
        }]
        html = 'jiangzb 退出 <a href="https://pan.baidu.com/s/1Member">下载</a><div>提取码：m123</div>'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken", "AI_API_KEY": "", "AI_BASE_URL": "", "AI_MODEL": ""}):
                with patch.object(pipeline, "fetch_source", return_value=raw), patch.object(pipeline, "http_text", return_value=html):
                    summary = run_task(task, output_dir=Path(tmp))
            self.assertEqual(summary["diagnostics"]["member_delivery_found_count"], 1)
            self.assertTrue(summary["diagnostics"]["member_cookie_validated"])
            item_dir = next(Path(tmp).glob("*/01-*"))
            delivery_text = (item_dir / "delivery.md").read_text(encoding="utf-8")
            self.assertIn("https://pan.baidu.com/s/1Member", delivery_text)
            self.assertIn("m123", delivery_text)

    def test_member_delivery_follows_ajax_go_redirect(self):
        task = {
            "name": "会员链接 AJAX 测试",
            "source": "theitzy",
            "keywords": ["AI"],
            "rights_confirmed": True,
            "authorized_assets": False,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        item = {
            "id": "theitzy:25611",
            "source_id": 25611,
            "title": "测试课程",
            "page_url": "https://theitzy.net/course/",
        }
        page_html = (
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a target="_blank" data-id="25611" class="go-down btn">立即下载</a>'
            '<span class="pwd">文件密码：<span>qa8i</span></span>'
        )
        go_html = "<html>redirected</html>"
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(
                pipeline,
                "http_text",
                side_effect=[page_html, (go_html, "https://pan.baidu.com/s/1RealLink")],
            ):
                with patch.object(pipeline, "http_form_json", return_value={"status": "1", "msg": "https://theitzy.net/go?post_id=25611"}):
                    delivery = pipeline.fetch_member_delivery(item, task, [])
        self.assertEqual(delivery["status"], "found")
        self.assertEqual(delivery["links"], ["https://pan.baidu.com/s/1RealLink"])
        self.assertIn("qa8i", delivery["passwords"])
        self.assertEqual(delivery["ajax_request_count"], 1)
        self.assertEqual(delivery["go_request_count"], 1)

    def test_member_delivery_skips_duplicate_post_id_in_one_run(self):
        task = {
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        item = {
            "id": "theitzy:duplicate",
            "source_id": 25611,
            "page_url": "https://theitzy.net/course/",
        }
        page_html = (
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a data-id="25611" class="go-down btn">立即下载</a>'
        )
        go_html = "<html>redirected</html>"
        state = {}
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(
                pipeline,
                "http_text",
                side_effect=[page_html, (go_html, "https://pan.baidu.com/s/1RealLink"), page_html],
            ) as text:
                with patch.object(
                    pipeline,
                    "http_form_json",
                    return_value={"status": "1", "msg": "https://theitzy.net/go?post_id=25611"},
                ) as ajax:
                    first = pipeline.fetch_member_delivery(item, task, [], state)
                    second = pipeline.fetch_member_delivery(item, task, [], state)
        self.assertEqual(ajax.call_count, 1)
        self.assertEqual(text.call_count, 3)
        self.assertEqual(first["links"], ["https://pan.baidu.com/s/1RealLink"])
        self.assertEqual(second["links"], first["links"])
        self.assertTrue(second["duplicate_skipped"])
        self.assertEqual(second["ajax_request_count"], 0)
        self.assertEqual(second["go_request_count"], 0)

    def test_member_delivery_request_budget_blocks_extra_ajax(self):
        task = {
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        page_html = (
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a data-id="1" class="go-down btn">立即下载</a>'
        )
        state = {
            "_requested_post_ids": set(),
            "_delivery_cache": {},
            "_ajax_request_count": 0,
            "_ajax_request_budget": 1,
        }
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(pipeline, "http_text", return_value=page_html), patch.object(
                pipeline,
                "http_form_json",
                return_value={"status": 0, "msg": "not available"},
            ) as ajax:
                first = pipeline.fetch_member_delivery(
                    {"id": "one", "source_id": 1, "page_url": "https://theitzy.net/one/"},
                    task,
                    [],
                    state,
                )
                second = pipeline.fetch_member_delivery(
                    {"id": "two", "source_id": 2, "page_url": "https://theitzy.net/two/"},
                    task,
                    [],
                    state,
                )
        self.assertEqual(ajax.call_count, 1)
        self.assertEqual(first["ajax_request_count"], 1)
        self.assertEqual(second["status"], "request_budget_exhausted")
        self.assertEqual(second["ajax_request_count"], 0)

    def test_run_deduplicates_different_item_ids_with_same_source_id(self):
        task = {
            "name": "同一文章不同ID防护",
            "source": "theitzy",
            "keywords": ["AI"],
            "output_limit": 15,
            "rights_confirmed": True,
            "source_config": {"base_url": "https://theitzy.net", "fetch_member_delivery": True},
        }
        first = {
            "id": "theitzy:one",
            "source_id": 99,
            "title": "AI 课程一",
            "page_url": "https://theitzy.net/one/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "课程资料",
            "cover_url": "",
        }
        second = {**first, "id": "theitzy:two", "title": "AI 课程重复"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pipeline, "fetch_source", return_value=[first, second]), patch.object(
                pipeline, "validate_member_cookie", return_value={}
            ), patch.object(
                pipeline,
                "fetch_member_delivery",
                return_value={"status": "found", "links": ["https://pan.baidu.com/s/mock"], "request_count": 1},
            ) as fetch_delivery:
                summary = run_task(task, output_dir=Path(tmp))
        self.assertEqual(summary["count"], 1)
        self.assertEqual(fetch_delivery.call_count, 1)
        self.assertEqual(summary["diagnostics"]["source_duplicate_count"], 1)

    def test_run_deduplicates_source_ids_before_member_requests(self):
        task = {
            "name": "重复课程防护",
            "source": "theitzy",
            "keywords": ["AI"],
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        first = {
            "id": "theitzy:1",
            "source_id": 1,
            "title": "AI 课程一",
            "page_url": "https://theitzy.net/one/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "课程资料",
            "cover_url": "",
        }
        second = {**first, "id": "theitzy:2", "source_id": 2, "title": "AI 课程二", "page_url": "https://theitzy.net/two/"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pipeline, "fetch_source", return_value=[first, first.copy(), second]), patch.object(
                pipeline, "validate_member_cookie", return_value={}
            ), patch.object(
                pipeline,
                "fetch_member_delivery",
                return_value={
                    "status": "found",
                    "links": ["https://pan.baidu.com/s/mock"],
                    "passwords": [],
                    "request_count": 1,
                    "ajax_request_count": 1,
                    "go_request_count": 1,
                    "request_trace": [{"stage": "user_down_ajax", "post_id": "1"}],
                },
            ) as fetch_delivery:
                summary = run_task(task, output_dir=Path(tmp))
        self.assertEqual(summary["count"], 2)
        self.assertEqual(fetch_delivery.call_count, 2)
        self.assertEqual(summary["diagnostics"]["source_raw_count"], 3)
        self.assertEqual(summary["diagnostics"]["source_duplicate_count"], 1)
        self.assertEqual(summary["diagnostics"]["member_delivery_request_count"], 2)

    def test_single_run_requests_each_of_ten_courses_once(self):
        task = {
            "name": "十条请求计数",
            "source": "theitzy",
            "keywords": ["AI"],
            "output_limit": 15,
            "rights_confirmed": True,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_request_interval_seconds": 0,
            },
        }
        raw = [
            {
                "id": f"theitzy:{index}",
                "source_id": index,
                "title": f"AI 课程 {index}",
                "page_url": f"https://theitzy.net/course/{index}/",
                "published_at": "2999-01-01T00:00:00+00:00",
                "categories": ["AI"],
                "summary": "课程资料",
                "cover_url": "",
            }
            for index in range(1, 11)
        ]
        page_html = (
            '<script>var caozhuti={"ajaxurl":"https:\\/\\/theitzy.net\\/wp-admin\\/admin-ajax.php"}</script>'
            '<a data-id="1" class="go-down btn">立即下载</a>'
        )

        def fake_http_text(url, **kwargs):
            if "/go?post_id=" in url:
                post_id = url.rsplit("=", 1)[-1]
                return "<html>redirected</html>", f"https://pan.baidu.com/s/course{post_id}"
            return page_html

        def fake_ajax(url, data, **kwargs):
            return {"status": "1", "msg": f"https://theitzy.net/go?post_id={data['post_id']}"}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(pipeline, "fetch_source", return_value=raw), patch.object(
                pipeline, "validate_member_cookie", return_value={}
            ), patch.object(pipeline, "load_env_file"), patch.dict(
                os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}
            ), patch.object(pipeline, "http_text", side_effect=fake_http_text) as text, patch.object(
                pipeline, "http_form_json", side_effect=fake_ajax
            ) as ajax:
                summary = run_task(task, output_dir=Path(tmp))
        diagnostics = summary["diagnostics"]
        self.assertEqual(summary["count"], 10)
        self.assertEqual(ajax.call_count, 10)
        self.assertEqual(diagnostics["member_delivery_ajax_request_count"], 10)
        self.assertEqual(diagnostics["member_delivery_go_request_count"], 10)
        self.assertEqual(diagnostics["member_delivery_page_request_count"], 10)
        self.assertEqual(diagnostics["member_delivery_request_budget"], 10)
        self.assertEqual(diagnostics["member_delivery_ajax_request_budget_remaining"], 0)
        self.assertFalse(diagnostics["member_delivery_request_budget_exhausted"])
        self.assertEqual(len(diagnostics["member_delivery_unique_post_ids"]), 10)
        self.assertEqual(
            sum(event["stage"] == "user_down_ajax" for event in diagnostics["member_delivery_request_trace"]),
            10,
        )
        self.assertEqual(text.call_count, 20)

    def test_run_lock_blocks_a_second_process_in_same_output_dir(self):
        task = {"name": "锁测试", "source": "theitzy", "keywords": ["AI"]}
        with tempfile.TemporaryDirectory() as tmp:
            with pipeline._exclusive_run_lock(Path(tmp)):
                with self.assertRaisesRegex(RuntimeError, "已有本地整理任务运行中"):
                    run_task(task, output_dir=Path(tmp))

    def test_member_cookie_validation_blocks_invalid_login(self):
        task = {
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
            },
        }
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(pipeline, "http_text", return_value='<form id="loginform"><input name="log"></form>'):
                with self.assertRaises(RuntimeError):
                    validate_member_cookie(task)

    def test_member_cookie_validation_rejects_logout_text_without_account(self):
        task = {
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
            },
        }
        stale_page = "<html><script>const action = 'logout';</script><p>请浏览课程</p></html>"
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(pipeline, "http_text", return_value=stale_page):
                with self.assertRaisesRegex(RuntimeError, "无法确认"):
                    validate_member_cookie(task)

    def test_member_cookie_validation_accepts_account_with_logout_link(self):
        task = {
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
            },
        }
        member_page = (
            '<a href="https://theitzy.net/user">jiangzb</a>'
            '<a href="https://theitzy.net/wp-login.php?action=logout">退出登录</a>'
        )
        cookie = "wordpress_logged_in_test=jiangzb%7Cnew-token"
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=old%7Ctoken"}):
            with patch.object(pipeline, "http_text", return_value=member_page) as request:
                result = validate_member_cookie(task, cookie_override=cookie)
        self.assertEqual(result["username"], "jiangzb")
        self.assertEqual(request.call_args.kwargs["headers"]["Cookie"], cookie)

    def test_member_cookie_validation_reports_gateway_errors_clearly(self):
        task = {
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
            },
        }
        error = pipeline.HTTPError("https://theitzy.net/user/?action=vip", 502, "Bad Gateway", {}, None)
        with patch.dict(os.environ, {"THEITZY_COOKIE": "wordpress_logged_in_test=jiangzb%7Ctoken"}):
            with patch.object(pipeline, "http_text", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "站点网关/服务器临时异常"):
                    validate_member_cookie(task)

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

    def test_unpublished_course_is_skipped_after_three_selections(self):
        raw = [{
            "id": "theitzy:repeat",
            "title": "AI 课程",
            "page_url": "https://theitzy.net/repeat/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "课程资料",
            "cover_url": "",
        }]
        task = {"name": "重复推荐限制", "source": "theitzy", "keywords": ["AI"], "source_config": {"base_url": "https://theitzy.net"}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "fetch_source", return_value=raw):
            output_dir = Path(tmp)
            self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
            self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
            self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
            self.assertEqual(run_task(task, output_dir=output_dir)["count"], 1)
            fifth = run_task(task, output_dir=output_dir)
        self.assertEqual(fifth["count"], 0)
        self.assertEqual(fifth["diagnostics"]["skipped_repeated_unpublished_count"], 1)

    def test_unpublished_selection_reuses_cached_item(self):
        task = {"name": "缓存测试", "source": "theitzy", "keywords": ["AI"], "source_config": {"base_url": "https://theitzy.net"}}
        first_raw = [{
            "id": "theitzy:cache",
            "title": "AI 课程旧标题",
            "page_url": "https://theitzy.net/cache/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "旧摘要",
            "cover_url": "",
        }]
        second_raw = [{**first_raw[0], "title": "AI 课程新标题", "summary": "新摘要"}]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(pipeline, "fetch_source", return_value=first_raw):
                run_task(task, output_dir=output_dir)
            with patch.object(pipeline, "fetch_source", return_value=second_raw):
                summary = run_task(task, output_dir=output_dir)
        self.assertEqual(summary["diagnostics"]["reused_unpublished_count"], 1)
        self.assertEqual(summary["items"][0]["title"], "AI 课程旧标题")

    def test_reused_unpublished_item_records_member_delivery_skip_status(self):
        raw = [{
            "id": "theitzy:cache-delivery",
            "title": "AI 课程缓存发货状态",
            "page_url": "https://theitzy.net/cache-delivery/",
            "published_at": "2999-01-01T00:00:00+00:00",
            "categories": ["AI"],
            "summary": "旧摘要",
            "cover_url": "",
        }]
        first_task = {"name": "缓存发货状态", "source": "theitzy", "keywords": ["AI"], "source_config": {"base_url": "https://theitzy.net"}}
        second_task = {
            **first_task,
            "source_config": {
                "base_url": "https://theitzy.net",
                "fetch_member_delivery": True,
                "member_cookie_env": "THEITZY_COOKIE",
            },
            "rights_confirmed": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(pipeline, "fetch_source", return_value=raw), patch.object(pipeline, "maybe_ai_copy", return_value=""), patch.object(pipeline, "ai_copy_configured", return_value=False):
                run_task(first_task, output_dir=output_dir)
            with patch.object(pipeline, "fetch_source", return_value=raw), patch.object(pipeline, "validate_member_cookie", return_value={}), patch.object(pipeline, "maybe_ai_copy", return_value=""), patch.object(pipeline, "ai_copy_configured", return_value=False):
                summary = run_task(second_task, output_dir=output_dir)
            self.assertEqual(summary["diagnostics"]["reused_unpublished_count"], 1)
            item_path = output_dir / summary["items"][0]["folder"] / "item.json"
            item = json.loads(item_path.read_text(encoding="utf-8"))
            self.assertEqual(item["member_delivery"]["status"], "skipped_rights_unconfirmed")

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
        self.assertEqual(boosted["market_reference_titles"][0]["link"], "https://goofish.test/item")

    def test_goofish_market_heat_signals_filter_and_sort(self):
        records = [
            {
                "爬取时间": "2026-07-13T10:00:00+08:00",
                "搜索关键字": "AI课程",
                "任务名称": "AI虚拟课程热度关键词监控",
                "商品信息": {
                    "商品ID": "low",
                    "商品标题": "Coze 工作流 AI课程",
                    "当前售价": "¥3.9",
                    "“想要”人数": 0,
                    "浏览量": 5,
                    "卖家昵称": "卖家A",
                    "商品链接": "https://goofish.test/item?id=low",
                },
                "ai_analysis": {"is_recommended": True, "matched_keywords": ["Coze"], "keyword_hit_count": 1},
            },
            {
                "爬取时间": "2026-07-13T10:00:00+08:00",
                "搜索关键字": "AI课程",
                "任务名称": "AI虚拟课程热度关键词监控",
                "商品信息": {
                    "商品ID": "rising",
                    "商品标题": "RAG 项目实战 AI课程",
                    "当前售价": "¥9.9",
                    "“想要”人数": 1,
                    "浏览量": 30,
                    "卖家昵称": "卖家B",
                    "商品链接": "https://goofish.test/item?id=rising",
                },
                "ai_analysis": {"is_recommended": True, "matched_keywords": ["RAG"], "keyword_hit_count": 1},
            },
            {
                "爬取时间": "2026-07-13T11:00:00+08:00",
                "搜索关键字": "AI课程",
                "任务名称": "AI虚拟课程热度关键词监控",
                "商品信息": {
                    "商品ID": "rising",
                    "商品标题": "RAG 项目实战 AI课程",
                    "当前售价": "¥9.9",
                    "“想要”人数": 4,
                    "浏览量": 80,
                    "卖家昵称": "卖家B",
                    "商品链接": "https://goofish.test/item?id=rising",
                },
                "ai_analysis": {"is_recommended": True, "matched_keywords": ["RAG"], "keyword_hit_count": 1},
            },
            {
                "爬取时间": "2026-07-13T11:05:00+08:00",
                "搜索关键字": "AI课程",
                "任务名称": "AI虚拟课程热度核心热点",
                "商品信息": {
                    "商品ID": "top",
                    "商品标题": "AIGC 视频制作 AI课程",
                    "当前售价": "¥19.9",
                    "“想要”人数": 8,
                    "浏览量": 120,
                    "卖家昵称": "卖家C",
                    "商品链接": "https://goofish.test/item?id=top",
                },
                "ai_analysis": {"is_recommended": True, "matched_keywords": ["AIGC"], "keyword_hit_count": 1},
            },
        ]

        def fake_http_json(url, **_kwargs):
            if url.endswith("/api/results/files"):
                return {"files": ["AI课程_full_data.jsonl"]}
            return {"items": records}

        task = {
            "keywords": ["RAG", "AIGC", "Coze"],
            "goofish_market": {
                "enabled": True,
                "base_url": "https://goofish.test",
                "result_keywords": ["AI课程"],
                "limit": 100,
                "result_pages": 1,
                "signal_mode": "heat",
                "local_sort_by": "views",
                "local_sort_order": "desc",
                "min_views": 20,
            },
        }
        with patch.object(pipeline, "http_json", side_effect=fake_http_json):
            market = pipeline.fetch_goofish_market_signals(task)

        signals = market["signals"]
        self.assertEqual([signal["views"] for signal in signals], [120.0, 80.0, 30.0])
        self.assertGreater(signals[0]["heat_score"], 0)
        rising = [signal for signal in signals if signal["item_id"] == "rising"]
        self.assertEqual(rising[0]["views_delta"], 50.0)
        self.assertEqual(rising[0]["wants_delta"], 3.0)


if __name__ == "__main__":
    unittest.main()
