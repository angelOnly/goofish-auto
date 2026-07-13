"""Local, review-first resource trend pipeline.

This module intentionally collects public metadata only. It does not download
course/video/archive payloads or bypass login, rate limits, robots.txt, or
anti-bot controls. Asset downloads are limited to same-origin images and are
disabled unless the task explicitly confirms distribution rights.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
TASKS_FILE = ROOT / "tasks.json"
ENV_FILE = ROOT / ".env"
USER_AGENT = "XinliResourcePipeline/0.1 (local review; respects robots.txt)"
LOGGER = logging.getLogger("goofish_auto.pipeline")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def strip_html(value: str, limit: int = 1200) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    text = " ".join(parser.parts)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def parse_metric(value: Any) -> float:
    """Parse values such as 3.13K / 1.2M into a number."""
    if value is None:
        return 0.0
    raw = str(value).strip().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KM万亿]?)", raw, re.I)
    if not match:
        return 0.0
    number = float(match.group(1))
    multiplier = {"k": 1_000, "m": 1_000_000, "万": 10_000, "亿": 100_000_000}.get(
        match.group(2).lower(), 1
    )
    return number * multiplier


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def http_text(url: str, *, timeout: int = 30, headers: Optional[Dict[str, str]] = None) -> str:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_json(url: str, *, timeout: int = 30) -> Any:
    return json.loads(http_text(url, timeout=timeout, headers={"Accept": "application/json"}))


def sleep_between_requests(last_request: List[float], interval: float) -> None:
    if interval <= 0 or not last_request:
        return
    remaining = interval - (time.monotonic() - last_request[0])
    if remaining > 0:
        time.sleep(remaining)


def extract_image_url(content_html: str, page_url: str) -> Optional[str]:
    patterns = [
        r'data-src=["\']([^"\']+)["\']',
        r'<img[^>]+srcset=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content_html or "", flags=re.I):
            candidate = html.unescape(match.group(1)).strip().split(",")[0].split(" ")[0]
            if candidate and not candidate.startswith("data:"):
                return urljoin(page_url, candidate)
    return None


def normalise_title(value: str) -> str:
    value = strip_html(value, 240)
    value = re.sub(r"\s*\|\s*[^|]{2,100}$", "", value).strip()
    return value or "未命名资源"


def parse_wp_post(raw: Dict[str, Any], category_map: Dict[int, str]) -> Dict[str, Any]:
    page_url = str(raw.get("link") or "")
    title = normalise_title(str((raw.get("title") or {}).get("rendered", "")))
    content_html = str((raw.get("content") or {}).get("rendered", ""))
    categories = [category_map.get(int(item), f"分类-{item}") for item in raw.get("categories", [])]
    categories = [item for item in categories if item]
    image_url = raw.get("jetpack_featured_media_url") or extract_image_url(content_html, page_url)
    excerpt = strip_html(str((raw.get("excerpt") or {}).get("rendered", "")), 500)
    if not excerpt:
        excerpt = strip_html(content_html, 500)
    return {
        "id": f"theitzy:{raw.get('id')}",
        "source": "theitzy",
        "source_id": raw.get("id"),
        "title": title,
        "page_url": page_url,
        "published_at": raw.get("date"),
        "categories": categories,
        "tags": raw.get("tags", []),
        "summary": excerpt,
        "cover_url": str(image_url or ""),
    }


def fetch_theitzy(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = task.get("source_config", {})
    base_url = str(source.get("base_url", "https://theitzy.net")).rstrip("/")
    per_page = max(1, min(int(source.get("per_page", 20)), 100))
    max_pages = max(1, min(int(source.get("max_pages", 1)), 10))
    interval = float(source.get("request_interval_seconds", 10))
    last_request: List[float] = []
    LOGGER.info(
        "fetch_theitzy start task=%s base_url=%s per_page=%s max_pages=%s interval=%s",
        task.get("name"),
        base_url,
        per_page,
        max_pages,
        interval,
    )

    def get_json(url: str) -> Any:
        sleep_between_requests(last_request, interval)
        data = http_json(url)
        last_request[:] = [time.monotonic()]
        return data

    category_map: Dict[int, str] = {}
    raw_posts: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        query = urlencode(
            {
                "page": page,
                "per_page": per_page,
                "orderby": source.get("orderby", "date"),
                "order": source.get("order", "desc"),
                "_fields": "id,date,link,title,content,excerpt,jetpack_featured_media_url,categories,tags",
            }
        )
        url = f"{base_url}/wp-json/wp/v2/posts?{query}"
        LOGGER.info("fetch_theitzy request page=%s url=%s", page, url)
        try:
            data = get_json(url)
        except HTTPError as exc:
            if exc.code == 400 and page > 1:
                LOGGER.info("fetch_theitzy stop page=%s reason=http_400_no_more_pages", page)
                break
            raise RuntimeError(f"TheItzy API 请求失败: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"TheItzy API 请求失败: {exc}") from exc
        if not isinstance(data, list):
            raise RuntimeError("TheItzy API 返回格式不是列表")
        LOGGER.info("fetch_theitzy response page=%s count=%s", page, len(data))
        raw_posts.extend(data)
        if len(data) < per_page:
            LOGGER.info("fetch_theitzy stop page=%s reason=last_page count=%s", page, len(data))
            break

    # The site has more than 100 categories, so resolving only the first page
    # would leave many useful IDs as "分类-数字". Resolve only the IDs seen in
    # this run to keep the request count bounded.
    category_ids = sorted({int(value) for post in raw_posts for value in post.get("categories", [])})
    if category_ids:
        try:
            include = ",".join(str(value) for value in category_ids[:100])
            categories = get_json(
                f"{base_url}/wp-json/wp/v2/categories?include={include}&per_page=100&_fields=id,name"
            )
            category_map.update({int(item["id"]): str(item["name"]) for item in categories})
            LOGGER.info("fetch_theitzy categories resolved=%s requested=%s", len(category_map), len(category_ids))
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
            # Category names are helpful but not required for metadata monitoring.
            LOGGER.exception("fetch_theitzy categories failed requested=%s", len(category_ids))
            pass
    parsed = [parse_wp_post(item, category_map) for item in raw_posts]
    LOGGER.info("fetch_theitzy done raw_posts=%s parsed=%s", len(raw_posts), len(parsed))
    return parsed


def fetch_source(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = str(task.get("source", "")).lower()
    if source in {"1337x", "1337x.to"}:
        raise RuntimeError(
            "已禁用 1337x 抓取：该站点常包含未授权影视/软件等资源，本原型不用于发现、搬运或售卖此类内容。"
            "请替换成你拥有分发权的官方 API、RSS 或素材库。"
        )
    if source in {"theitzy", "theitzy.net"}:
        return fetch_theitzy(task)
    raise RuntimeError(f"不支持的数据源: {source or '未填写'}")


def score_item(item: Dict[str, Any], keywords: Iterable[str]) -> Dict[str, Any]:
    text = " ".join(
        [
            str(item.get("title", "")),
            " ".join(str(value) for value in item.get("categories", [])),
            str(item.get("summary", "")),
        ]
    ).lower()
    terms = [str(value).strip().lower() for value in keywords if str(value).strip()]
    matched: List[str] = []
    for term in terms:
        if term in text and term not in matched:
            matched.append(term)
    now = datetime.now(timezone.utc)
    published_at = str(item.get("published_at") or "")
    age_days = 365.0
    if published_at:
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - published).total_seconds() / 86400)
        except ValueError:
            pass
    freshness = max(0.0, 30.0 * math.exp(-age_days / 14.0))
    keyword_score = min(50.0, len(matched) * 16.0)
    image_bonus = 5.0 if item.get("cover_url") else 0.0
    score = round(min(100.0, keyword_score + freshness + image_bonus), 1)
    return {
        **item,
        "matched_keywords": matched,
        "age_days": round(age_days, 2),
        "hotness_score": score,
        "hotness_reasons": [
            *(f"命中关键词：{', '.join(matched)}" for _ in [0] if matched),
            *("发布时间较近" for _ in [0] if age_days <= 14),
            *("有封面素材" for _ in [0] if item.get("cover_url")),
        ],
    }


def safe_slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value).strip("-")
    return value[:80] or "item"


def same_origin(url: str, base_url: str) -> bool:
    left, right = urlparse(url), urlparse(base_url)
    return left.scheme in {"http", "https"} and left.netloc.lower() == right.netloc.lower()


def download_authorized_cover(item: Dict[str, Any], task: Dict[str, Any], asset_dir: Path) -> Optional[str]:
    if not item.get("cover_url"):
        return None
    if not task.get("rights_confirmed") or not task.get("authorized_assets"):
        return None
    base_url = str(task.get("source_config", {}).get("base_url", "https://theitzy.net"))
    if not same_origin(str(item["cover_url"]), base_url):
        return None
    try:
        request = Request(str(item["cover_url"]), headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
        with urlopen(request, timeout=30) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
            content_type = str(response.headers.get("Content-Type", "")).lower()
        if len(payload) > 8 * 1024 * 1024 or (content_type and not content_type.startswith("image/")):
            return None
    except (HTTPError, URLError, TimeoutError):
        return None
    extension = ".jpg"
    if "png" in content_type:
        extension = ".png"
    elif "webp" in content_type:
        extension = ".webp"
    filename = hashlib.sha256(payload).hexdigest()[:16] + extension
    path = asset_dir / filename
    path.write_bytes(payload)
    return str(path.relative_to(asset_dir.parent.parent)).replace("\\", "/")


def _copy_tags(item: Dict[str, Any]) -> str:
    values = item.get("matched_keywords") or item.get("categories") or ["数字资料"]
    tags = []
    for value in values:
        tag = re.sub(r"\s+", "", str(value).strip())
        if tag and tag not in tags:
            tags.append(tag)
    return " ".join(f"#{tag}" for tag in tags[:8])


def _summary_bullets(summary: str, *, limit: int = 4) -> List[str]:
    parts = [part.strip(" ，。；;、") for part in re.split(r"[。；;]\s*", summary or "") if part.strip()]
    if not parts:
        return ["以实际审核通过的内容清单为准，适合先了解主题框架再系统学习。"]
    return parts[:limit]


def template_copy(item: Dict[str, Any], task: Dict[str, Any]) -> str:
    title = item["title"]
    categories = item.get("categories") or ["数字资料"]
    category_text = "、".join(categories)
    summary = item.get("summary") or "以页面公开信息为准，具体目录请以交付前确认内容为准。"
    bullets = _summary_bullets(summary)
    topic = categories[0] if categories else "数字资料"
    tags = _copy_tags(item)
    audit_prefix = ""
    if not task.get("rights_confirmed"):
        audit_prefix = "【内部审核草稿｜暂勿发布】\n当前未完成内容权利确认，禁止发布、售卖或交付。审核通过后再改成正式上架文案。\n\n"

    return (
        audit_prefix
        + f"{title}\n"
        + f"{tags}\n\n"
        + "【核心价值】\n"
        + f"围绕 {topic} 的系统学习/资料整理方向，适合想快速判断内容价值、补齐知识框架的人。购买前建议先确认目录、格式和交付清单。\n\n"
        + "【内容聚焦】\n"
        + "\n".join(f"- {line}" for line in bullets)
        + "\n\n【适合人群】\n"
        + f"- 想系统了解 {topic}，但不想零散搜索资料的人\n"
        + "- 需要按目录学习、复盘或做项目参考的人\n"
        + "- 想先看清内容范围，再决定是否深入学习的人\n\n"
        + "【资料优势】\n"
        + "- 主题明确，适合做学习路线或选品需求判断\n"
        + "- 支持先确认目录/格式/适用人群，再决定是否拍下\n"
        + "- 如用于正式发布，请只交付自有或已授权的网盘内容\n\n"
        + "【关键信息】\n"
        + f"主题分类：{category_text}\n"
        + "交付方式：审核通过后填写你自己的授权百度网盘链接\n"
        + "温馨提示：数字资料非实物，拍下前请确认内容清单、文件格式和售后规则。"
    )


def maybe_ai_copy(item: Dict[str, Any], task: Dict[str, Any]) -> Optional[str]:
    if not task.get("rights_confirmed") or not os.getenv("AI_API_KEY"):
        return None
    base_url = os.getenv("AI_BASE_URL", "").rstrip("/")
    model = os.getenv("AI_MODEL", "")
    if not base_url or not model:
        return None
    prompt = (
        "你是电商文案编辑，只能根据下面的已授权数字产品元数据写一版中文闲鱼草稿。"
        "不得声称官方授权、不得虚构目录/时长/格式/售后，不得使用夸张承诺。输出：标题、卖点、交付说明、注意事项。\n"
        + json.dumps(
            {
                "title": item.get("title"),
                "categories": item.get("categories"),
                "summary": item.get("summary"),
            },
            ensure_ascii=False,
        )
    )
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "只输出可审核的商品文案，不输出分析过程。"},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        request = Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ['AI_API_KEY']}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return str(data["choices"][0]["message"]["content"]).strip()
    except (HTTPError, URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
        return None


def load_tasks(path: Path = TASKS_FILE) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"seen": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seen": {}}


def run_task(task: Dict[str, Any], *, output_dir: Path = OUTPUT_DIR, include_seen: bool = False) -> Dict[str, Any]:
    load_env_file()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "state.json"
    state = load_state(state_path)
    task_name = str(task.get("name") or "未命名任务")
    task_seen = state.setdefault("seen_by_task", {}).setdefault(task_name, {})
    legacy_seen = state.setdefault("seen", {})
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = run_dir / "assets"
    asset_dir.mkdir(exist_ok=True)
    keywords = [str(value) for value in task.get("keywords", []) if str(value).strip()]
    output_limit = max(1, min(int(task.get("output_limit", 20)), 100))
    run_log: List[Dict[str, Any]] = []

    def note(message: str, **fields: Any) -> None:
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "message": message,
            **fields,
        }
        run_log.append(event)
        LOGGER.info("run_id=%s task=%s %s %s", run_id, task_name, message, fields)

    note(
        "run_start",
        source=task.get("source"),
        include_seen=include_seen,
        keyword_count=len(keywords),
        output_limit=output_limit,
    )
    raw_items = fetch_source(task)
    note("source_fetched", fetched_count=len(raw_items))

    skipped_seen = 0
    candidates: List[Dict[str, Any]] = []
    for raw in raw_items:
        item_id = str(raw.get("id") or "")
        if not include_seen and item_id in task_seen:
            skipped_seen += 1
            continue
        scored = score_item(raw, keywords)
        candidates.append(scored)

    matched_candidates = [item for item in candidates if item.get("matched_keywords")]
    pool = matched_candidates if matched_candidates else candidates
    fallback_used = bool(candidates and not matched_candidates)
    pool.sort(key=lambda item: item.get("hotness_score", 0), reverse=True)
    items = pool[:output_limit]

    zero_reason = ""
    if not raw_items:
        zero_reason = "数据源没有返回文章，请检查 TheItzy API、网络或站点状态。"
    elif skipped_seen == len(raw_items) and not include_seen:
        zero_reason = "本次抓到的文章都已经在这个任务里处理过；如需复查旧数据，请勾选“包含已处理”。"
    elif not candidates:
        zero_reason = "抓到了文章，但没有可参与评分的候选项。"
    elif not items:
        zero_reason = "候选项为空，未生成输出。"

    diagnostics = {
        "fetched_count": len(raw_items),
        "skipped_seen_count": skipped_seen,
        "candidate_count": len(candidates),
        "matched_count": len(matched_candidates),
        "unmatched_count": max(0, len(candidates) - len(matched_candidates)),
        "selected_count": len(items),
        "output_limit": output_limit,
        "fallback_used": fallback_used,
        "zero_reason": zero_reason,
        "keywords": keywords,
        "top_candidates": [
            {
                "title": item.get("title"),
                "hotness_score": item.get("hotness_score"),
                "matched_keywords": item.get("matched_keywords", []),
            }
            for item in pool[:5]
        ],
    }
    note(
        "selection_done",
        fetched_count=diagnostics["fetched_count"],
        skipped_seen_count=skipped_seen,
        candidate_count=len(candidates),
        matched_count=len(matched_candidates),
        selected_count=len(items),
        fallback_used=fallback_used,
        zero_reason=zero_reason,
    )

    for item in items:
        item["cover_local_path"] = download_authorized_cover(item, task, asset_dir)
        item["rights_review"] = "confirmed" if task.get("rights_confirmed") else "required"
        ai_copy = maybe_ai_copy(item, task)
        item["copy"] = ai_copy or template_copy(item, task)
        item["copy_source"] = "ai" if ai_copy else "template"
        item["delivery_links"] = list(task.get("owned_delivery_links", []))

    for index, item in enumerate(items, start=1):
        slug = f"{index:02d}-{safe_slug(item['title'])}"
        item_dir = run_dir / slug
        item_dir.mkdir(exist_ok=True)
        (item_dir / "copy.md").write_text(item["copy"], encoding="utf-8")
        delivery_status = "可进入授权审核后的发布流程" if task.get("rights_confirmed") else "未确认分发权，禁止发布"
        delivery_links = "\n".join(f"- {link}" for link in item["delivery_links"]) or "- 待填入你自己的授权网盘链接"
        cover_info = (
            f"- 本地：{item['cover_local_path']}"
            if item.get("cover_local_path")
            else f"- 公开封面地址（仅供人工核验）：{item.get('cover_url') or '无'}"
        )
        delivery = (
            "# 发货信息（待审核）\n\n"
            f"商品：{item['title']}\n"
            f"状态：{delivery_status}\n"
            "百度网盘交付链接：\n"
            f"{delivery_links}\n\n"
            "图片信息：\n"
            f"{cover_info}\n"
            "- 上架建议：使用你自己有权使用的封面图、课程目录长图或重新制作的说明图；公开来源图不要直接当作可商用素材。\n\n"
            "原始页面（仅作来源核验）：\n"
            f"- {item.get('page_url') or ''}\n"
        )
        (item_dir / "delivery.md").write_text(delivery, encoding="utf-8")
        (item_dir / "item.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        seen_record = {"run_id": run_id, "title": item.get("title")}
        if item.get("id"):
            task_seen[str(item["id"])] = seen_record
            legacy_seen[str(item["id"])] = seen_record

    note("run_complete", count=len(items), run_dir=str(run_dir))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "run_id": run_id,
        "task_name": task_name,
        "source": task.get("source"),
        "rights_review": "confirmed" if task.get("rights_confirmed") else "required",
        "count": len(items),
        "diagnostics": diagnostics,
        "run_log": run_log,
        "items": [
            {
                "id": item["id"],
                "title": item["title"],
                "hotness_score": item["hotness_score"],
                "matched_keywords": item.get("matched_keywords", []),
                "page_url": item["page_url"],
                "folder": str((run_dir / f"{index:02d}-{safe_slug(item['title'])}").relative_to(output_dir)).replace("\\", "/"),
            }
            for index, item in enumerate(items, start=1)
        ],
    }
    (run_dir / "run.log").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in run_log) + ("\n" if run_log else ""),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_named_task(name: str, *, include_seen: bool = False, output_dir: Path = OUTPUT_DIR) -> Dict[str, Any]:
    tasks = load_tasks()
    task = next((item for item in tasks if item.get("name") == name), None)
    if not task:
        raise ValueError(f"找不到任务：{name}")
    return run_task(task, include_seen=include_seen, output_dir=output_dir)
