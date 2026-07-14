"""Local, review-first resource trend pipeline.

This module defaults to public metadata only. Optional member-page delivery
link extraction uses a user-provided session cookie and still does not download
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
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
TASKS_FILE = ROOT / "tasks.json"
ENV_FILE = ROOT / ".env"
CONTENT_RULES_FILE = OUTPUT_DIR / "content_rules.json"
USER_AGENT = "XinliResourcePipeline/0.1 (local review; respects robots.txt)"
LOGGER = logging.getLogger("goofish_auto.pipeline")
LOCAL_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
DEFAULT_CONTENT_RULES = {"forbidden_words": ["chatgpt", "gpt"], "replacement": "AI工具"}
GENERIC_MARKET_TERMS = {
    "ai",
    "人工智能",
    "ai课程",
    "ai教程",
    "ai资料",
    "课程",
    "教程",
    "资料",
    "项目",
    "实战",
    "项目实战",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return datetime.now(LOCAL_TIMEZONE)


def _normalise_forbidden_words(values: Iterable[Any]) -> List[str]:
    words: List[str] = []
    seen: set[str] = set()
    raw_values: Iterable[Any] = [values] if isinstance(values, str) else values
    for value in raw_values:
        for part in re.split(r"[\n,，;；]+", str(value or "")):
            word = part.strip()
            key = word.lower()
            if word and key not in seen:
                seen.add(key)
                words.append(word)
    return words


def load_content_rules(path: Path = CONTENT_RULES_FILE) -> Dict[str, Any]:
    rules = dict(DEFAULT_CONTENT_RULES)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            rules.update(data)
    rules["forbidden_words"] = _normalise_forbidden_words(rules.get("forbidden_words") or [])
    if not rules["forbidden_words"]:
        rules["forbidden_words"] = list(DEFAULT_CONTENT_RULES["forbidden_words"])
    rules["replacement"] = str(rules.get("replacement", DEFAULT_CONTENT_RULES["replacement"]))
    return rules


def save_content_rules(rules: Dict[str, Any], path: Path = CONTENT_RULES_FILE) -> Dict[str, Any]:
    forbidden_words = _normalise_forbidden_words(rules.get("forbidden_words") or [])
    replacement = str(rules.get("replacement", DEFAULT_CONTENT_RULES["replacement"]))
    if not forbidden_words:
        raise ValueError("禁用词不能为空")
    for word in forbidden_words:
        if replacement and re.search(re.escape(word), replacement, re.IGNORECASE):
            raise ValueError("替换词不能包含禁用词")
    value = {"forbidden_words": forbidden_words, "replacement": replacement}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return value


def apply_content_rules(text: str, rules: Optional[Dict[str, Any]] = None) -> str:
    value = str(text or "")
    rules = rules or load_content_rules()
    words = _normalise_forbidden_words(rules.get("forbidden_words") or [])
    replacement = str(rules.get("replacement", ""))
    for word in sorted(words, key=len, reverse=True):
        value = re.sub(re.escape(word), replacement, value, flags=re.IGNORECASE)
    return value


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
        key = key.strip()
        value = value.strip().strip('"\'')
        reloadable_keys = {
            "THEITZY_COOKIE",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "OPENAI_MODEL_NAME",
            "AI_API_KEY",
            "AI_BASE_URL",
            "AI_MODEL",
        }
        if key in reloadable_keys:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def http_text(url: str, *, timeout: int = 30, headers: Optional[Dict[str, str]] = None) -> str:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_json(url: str, *, timeout: int = 30) -> Any:
    return json.loads(http_text(url, timeout=timeout, headers={"Accept": "application/json"}))


def http_form_json(
    url: str,
    data: Dict[str, Any],
    *,
    timeout: int = 30,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    request_headers.update(headers or {})
    request = Request(
        url,
        data=urlencode(data).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _wordpress_cookie_username(cookie: str) -> str:
    match = re.search(r"wordpress_logged_in_[^=]*=([^;]+)", cookie or "")
    if not match:
        return ""
    value = unquote(match.group(1))
    return value.split("|", 1)[0].strip()


def validate_member_cookie(task: Dict[str, Any]) -> Optional[Dict[str, str]]:
    source_config = task.get("source_config") or {}
    if not source_config.get("fetch_member_delivery"):
        return None
    cookie_env = str(source_config.get("member_cookie_env") or "THEITZY_COOKIE")
    cookie = os.getenv(cookie_env, "").strip()
    if not cookie:
        raise RuntimeError(f"会员链接抓取已开启，但未设置 {cookie_env}。请在本机 .env 或环境变量里填写 TheItzy 登录 Cookie。")
    if "wordpress_logged_in_" not in cookie:
        raise RuntimeError(f"{cookie_env} 中没有 wordpress_logged_in 登录 Cookie，无法确认 TheItzy 会员登录态。")

    base_url = str(source_config.get("base_url") or "https://theitzy.net").rstrip("/")
    check_url = str(source_config.get("member_cookie_check_url") or f"{base_url}/user/?action=vip")
    timeout = int(source_config.get("member_timeout", 30))
    try:
        content = http_text(
            check_url,
            timeout=timeout,
            headers={
                "Accept": "text/html",
                "Cookie": cookie,
                "Referer": base_url,
            },
        )
    except HTTPError as exc:
        if exc.code >= 500:
            raise RuntimeError(
                f"TheItzy 会员校验页返回 HTTP {exc.code}，这是站点网关/服务器临时异常，不是 Cookie 缺失；请稍后重试。"
            ) from exc
        if exc.code in {401, 403}:
            raise RuntimeError(f"{cookie_env} 已失效或无权访问会员校验页；请重新从浏览器复制 Cookie。") from exc
        raise RuntimeError(f"TheItzy 会员校验页返回 HTTP {exc.code}，无法继续本地资源整理。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法访问 TheItzy 会员校验页：{exc}") from exc
    username = _wordpress_cookie_username(cookie)
    haystack = f"{content}\n{strip_html(content, 20000)}".lower()
    configured_markers = [str(value).strip() for value in source_config.get("member_cookie_valid_markers", []) if str(value).strip()]
    valid_markers = configured_markers or [value for value in [username, "logout", "退出"] if value]
    if any(marker.lower() in haystack for marker in valid_markers):
        return {"cookie_env": cookie_env, "check_url": check_url, "username": username}

    invalid_markers = ["wp-login.php", "name=\"log\"", "用户登录", "登录后", "请登录", "loginform"]
    if any(marker.lower() in haystack for marker in invalid_markers):
        raise RuntimeError(f"{cookie_env} 已失效或未登录 TheItzy；请重新从浏览器复制 Cookie 后再运行本地资源整理。")
    raise RuntimeError(f"无法确认 {cookie_env} 是否有效：会员校验页没有出现登录用户名或退出标识。")


def get_first(mapping: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None and value != "":
                return value
    return default


def parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


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


def _normalise_extracted_url(value: str) -> str:
    candidate = html.unescape(value or "").replace("\\/", "/").strip().strip("\"'()[]{}<>，。；;、")
    return candidate


def extract_baidu_delivery_from_html(content_html: str, page_url: str = "") -> Dict[str, Any]:
    """Extract visible Baidu Netdisk delivery metadata from an authorized page."""
    text = html.unescape(content_html or "").replace("\\/", "/")
    link_pattern = re.compile(
        r"https?://(?:pan|yun)\.baidu\.com/(?:s/[A-Za-z0-9_-]+|share/(?:init|link)\?[^\"'<>\\\s]+)",
        flags=re.I,
    )
    links: List[str] = []
    for match in link_pattern.finditer(text):
        candidate = _normalise_extracted_url(match.group(0))
        if candidate and candidate not in links:
            links.append(candidate)

    password_pattern = re.compile(
        r"(?:提取码|访问码|文件密码|网盘密码|百度网盘密码|密码)\s*[:：]?\s*([A-Za-z0-9]{4,12})",
        flags=re.I,
    )
    passwords: List[str] = []
    for match in password_pattern.finditer(strip_html(text, 20000)):
        password = match.group(1).strip()
        if password and password not in passwords:
            passwords.append(password)

    return {
        "status": "found" if links else "not_found",
        "links": links,
        "passwords": passwords,
        "source_url": page_url,
    }


def _decode_js_url(value: str) -> str:
    return html.unescape(value or "").replace("\\/", "/")


def extract_member_download_context(content_html: str, page_url: str) -> Dict[str, str]:
    post_id = ""
    ajax_url = ""
    post_match = re.search(
        r'class=["\'][^"\']*\bgo-down\b[^"\']*["\'][^>]*\bdata-id=["\'](\d+)["\']',
        content_html or "",
        flags=re.I,
    )
    if not post_match:
        post_match = re.search(
            r'\bdata-id=["\'](\d+)["\'][^>]*class=["\'][^"\']*\bgo-down\b',
            content_html or "",
            flags=re.I,
        )
    if post_match:
        post_id = post_match.group(1)

    ajax_match = re.search(r'"ajaxurl"\s*:\s*"([^"]+)"', content_html or "", flags=re.I)
    if ajax_match:
        ajax_url = _decode_js_url(ajax_match.group(1))
    if not ajax_url:
        parsed = urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            ajax_url = f"{parsed.scheme}://{parsed.netloc}/wp-admin/admin-ajax.php"
    return {"post_id": post_id, "ajax_url": ajax_url}


def _merge_delivery(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    links: List[str] = []
    passwords: List[str] = []
    for source in (left, right):
        for link in source.get("links", []) or []:
            if link and link not in links:
                links.append(link)
        for password in source.get("passwords", []) or []:
            if password and password not in passwords:
                passwords.append(password)
    status = "found" if links else str(right.get("status") or left.get("status") or "not_found")
    merged = {**left, **right, "status": status, "links": links, "passwords": passwords}
    return merged


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
    text = item_search_text(item)
    terms = [str(value).strip().lower() for value in keywords if str(value).strip()]
    matched: List[str] = []
    for term in terms:
        if term in text and term not in matched:
            matched.append(term)
    now = utc_now()
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


def item_search_text(item: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("title", "")),
            " ".join(str(value) for value in item.get("categories", [])),
            str(item.get("summary", "")),
        ]
    ).lower()


def matches_any_keyword(item: Dict[str, Any], keywords: Iterable[str]) -> bool:
    text = item_search_text(item)
    return any(str(value).strip().lower() in text for value in keywords if str(value).strip())


def _normalise_term(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_market_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue
    return None


def _as_int_config(config: Dict[str, Any], key: str, default: int, *, minimum: int = 0, maximum: int = 10_000) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _as_float_config(
    config: Dict[str, Any], key: str, default: float, *, minimum: float = 0.0, maximum: float = 10_000.0
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _market_terms_from_record(record: Dict[str, Any], task_keywords: Iterable[str]) -> List[str]:
    title = _normalise_term(record.get("title"))
    ai = record.get("ai_analysis") or {}
    raw_terms = list(ai.get("matched_keywords") or [])
    for keyword in task_keywords:
        term = _normalise_term(keyword)
        if term and term in title:
            raw_terms.append(term)
    terms: List[str] = []
    for value in raw_terms:
        term = _normalise_term(value)
        if len(term) < 2:
            continue
        if term in GENERIC_MARKET_TERMS:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:12]


def _title_cluster_key(signal: Dict[str, Any]) -> str:
    terms = [term for term in signal.get("terms", []) if term not in GENERIC_MARKET_TERMS]
    if terms:
        return "|".join(sorted(terms[:4]))
    title = _normalise_term(signal.get("title"))
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", title)
    filtered = [
        token
        for token in tokens
        if token not in GENERIC_MARKET_TERMS and not token.isdigit() and len(token) >= 2
    ]
    return "|".join(filtered[:6]) or title[:32]


def _identity_key(signal: Dict[str, Any]) -> str:
    item_id = str(signal.get("item_id") or "").strip()
    if item_id:
        return f"item:{item_id}"
    link = str(signal.get("link") or "").split("&", 1)[0].strip()
    if link:
        return f"link:{link}"
    return f"title:{_normalise_term(signal.get('title'))[:80]}"


def _base_market_heat_score(views: float, wants: float, keyword_hit_count: int, is_recommended: bool) -> float:
    views_score = min(35.0, math.log1p(max(0.0, views)) / math.log1p(1_000.0) * 35.0)
    wants_score = min(25.0, math.log1p(max(0.0, wants)) / math.log1p(200.0) * 25.0)
    match_score = min(10.0, max(0, keyword_hit_count) * 2.0)
    recommend_score = 5.0 if is_recommended else 0.0
    return views_score + wants_score + match_score + recommend_score


def _annotate_market_heat(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_identity: Dict[str, List[Dict[str, Any]]] = {}
    by_cluster: Dict[str, List[Dict[str, Any]]] = {}
    by_seller: Dict[str, List[Dict[str, Any]]] = {}
    for signal in signals:
        identity = _identity_key(signal)
        cluster_key = _title_cluster_key(signal)
        seller = _normalise_term(signal.get("seller_nickname"))
        signal["identity_key"] = identity
        signal["title_cluster_key"] = cluster_key
        by_identity.setdefault(identity, []).append(signal)
        by_cluster.setdefault(cluster_key, []).append(signal)
        if seller:
            by_seller.setdefault(seller, []).append(signal)

    deltas: Dict[str, Dict[str, float]] = {}
    for identity, group in by_identity.items():
        ordered = sorted(
            group,
            key=lambda item: _parse_market_timestamp(item.get("crawl_time")) or datetime.min.replace(tzinfo=LOCAL_TIMEZONE),
        )
        first, last = ordered[0], ordered[-1]
        deltas[identity] = {
            "views_delta": max(0.0, float(last.get("views", 0.0)) - float(first.get("views", 0.0))),
            "wants_delta": max(0.0, float(last.get("wants", 0.0)) - float(first.get("wants", 0.0))),
        }

    for signal in signals:
        cluster = by_cluster.get(signal["title_cluster_key"], [])
        sellers = {_normalise_term(item.get("seller_nickname")) for item in cluster if item.get("seller_nickname")}
        seller = _normalise_term(signal.get("seller_nickname"))
        seller_repeat_count = len(by_seller.get(seller, [])) if seller else 0
        delta = deltas.get(signal["identity_key"], {"views_delta": 0.0, "wants_delta": 0.0})
        delta_score = min(
            20.0,
            math.log1p(delta["views_delta"] + delta["wants_delta"] * 8.0) / math.log1p(1_000.0) * 20.0,
        )
        density_score = min(12.0, math.log1p(len(cluster)) / math.log1p(20.0) * 12.0)
        seller_score = min(8.0, math.log1p(len(sellers)) / math.log1p(10.0) * 8.0)
        repeat_penalty = min(6.0, max(0, seller_repeat_count - 3) * 1.5)
        heat_score = _base_market_heat_score(
            float(signal.get("views", 0.0)),
            float(signal.get("wants", 0.0)),
            int(signal.get("keyword_hit_count", 0) or 0),
            bool(signal.get("is_recommended")),
        )
        heat_score = max(0.0, min(100.0, heat_score + delta_score + density_score + seller_score - repeat_penalty))
        signal.update(
            {
                "heat_score": round(heat_score, 1),
                "views_delta": round(delta["views_delta"], 1),
                "wants_delta": round(delta["wants_delta"], 1),
                "title_density": len(cluster),
                "title_cluster_seller_count": len(sellers),
                "seller_repeat_count": seller_repeat_count,
            }
        )
    return signals


def _filter_market_signals(signals: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    include_tasks = [_normalise_term(value) for value in config.get("task_name_keywords", []) if str(value).strip()]
    exclude_tasks = [_normalise_term(value) for value in config.get("exclude_task_name_keywords", []) if str(value).strip()]
    min_views = _as_float_config(config, "min_views", 0.0)
    min_wants = _as_float_config(config, "min_wants", 0.0)
    min_heat_score = _as_float_config(config, "min_heat_score", 0.0)
    min_title_density = _as_int_config(config, "min_title_density", 0)
    min_sellers = _as_int_config(config, "min_title_cluster_sellers", 0)
    recommended_only = bool(config.get("recommended_only", False))
    require_terms = bool(config.get("require_terms", False))

    filtered: List[Dict[str, Any]] = []
    for signal in signals:
        task_name = _normalise_term(signal.get("task_name"))
        if include_tasks and not any(value in task_name for value in include_tasks):
            continue
        if exclude_tasks and any(value in task_name for value in exclude_tasks):
            continue
        if require_terms and not signal.get("terms"):
            continue
        if recommended_only and not signal.get("is_recommended"):
            continue
        if float(signal.get("views", 0.0)) < min_views:
            continue
        if float(signal.get("wants", 0.0)) < min_wants:
            continue
        if float(signal.get("heat_score", 0.0)) < min_heat_score:
            continue
        if int(signal.get("title_density", 0) or 0) < min_title_density:
            continue
        if int(signal.get("title_cluster_seller_count", 0) or 0) < min_sellers:
            continue
        filtered.append(signal)
    return filtered


def _sort_market_signals(signals: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    sort_by = str(config.get("local_sort_by") or config.get("market_sort_by") or "").strip()
    if not sort_by:
        return signals
    allowed = {
        "heat_score",
        "views",
        "wants",
        "views_delta",
        "wants_delta",
        "title_density",
        "title_cluster_seller_count",
        "crawl_time",
        "price",
    }
    if sort_by not in allowed:
        return signals
    reverse = str(config.get("local_sort_order", "desc")).lower() != "asc"

    def sort_key(signal: Dict[str, Any]) -> tuple:
        if sort_by == "crawl_time":
            parsed = _parse_market_timestamp(signal.get("crawl_time"))
            return (parsed.timestamp() if parsed else 0.0, float(signal.get("heat_score", 0.0)))
        value = signal.get(sort_by)
        if value is None:
            value = 0.0
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        return (number, float(signal.get("heat_score", 0.0)))

    return sorted(signals, key=sort_key, reverse=reverse)


def parse_goofish_result_record(raw: Dict[str, Any], task_keywords: Iterable[str]) -> Optional[Dict[str, Any]]:
    product = raw.get("商品信息") or raw.get("商品信息".encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")) or {}
    if not isinstance(product, dict):
        product = {}
    seller = raw.get("卖家信息") or {}
    if not isinstance(seller, dict):
        seller = {}
    ai = raw.get("ai_analysis") or {}
    price_insight = raw.get("price_insight") or {}
    title = get_first(product, ["商品标题", "title", "标题"], "")
    if not title:
        return None
    price = parse_price(get_first(product, ["当前售价", "价格", "price"], None))
    if price is None:
        price = parse_price(price_insight.get("current_price"))
    wants = parse_metric(get_first(product, ["“想要”人数", "想要人数", "want_count"], 0))
    views = parse_metric(get_first(product, ["浏览量", "views"], 0))
    keyword_hit_count = int(ai.get("keyword_hit_count") or 0)
    is_recommended = bool(ai.get("is_recommended"))
    terms = _market_terms_from_record({"title": title, "ai_analysis": ai}, task_keywords)
    heat_score = _base_market_heat_score(views, wants, keyword_hit_count, is_recommended)
    strength = 1.0 + min(8.0, wants * 0.35) + min(6.0, views * 0.04)
    strength += keyword_hit_count * 1.5
    if is_recommended:
        strength += 5.0
    if price is not None and price > 0:
        strength += min(4.0, price / 10.0)
    return {
        "title": str(title),
        "price": price,
        "wants": wants,
        "views": views,
        "item_id": get_first(product, ["商品ID", "item_id", "id"], ""),
        "publish_time": get_first(product, ["发布时间", "publish_time"], ""),
        "crawl_time": raw.get("爬取时间"),
        "link": get_first(product, ["商品链接", "link"], ""),
        "search_keyword": raw.get("搜索关键字"),
        "task_name": raw.get("任务名称"),
        "seller_nickname": get_first(seller, ["卖家昵称", "seller_nickname"], "")
        or get_first(product, ["卖家昵称", "seller_nickname"], ""),
        "is_recommended": is_recommended,
        "analysis_source": ai.get("analysis_source"),
        "keyword_hit_count": keyword_hit_count,
        "matched_keywords": list(ai.get("matched_keywords") or []),
        "terms": terms,
        "heat_score": round(heat_score, 1),
        "strength": round(strength, 2),
    }


def fetch_goofish_market_signals(task: Dict[str, Any]) -> Dict[str, Any]:
    config = task.get("goofish_market")
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return {"enabled": False, "signals": [], "files": [], "error": ""}
    base_url = str(
        config.get("base_url")
        or os.getenv("GOOFISH_API_BASE_URL")
        or "https://goofish.xiaolicloud.cn:18443"
    ).rstrip("/")
    limit = _as_int_config(config, "limit", 100, minimum=1, maximum=100)
    max_files = _as_int_config(config, "max_files", 3, minimum=1, maximum=10)
    result_pages = _as_int_config(config, "result_pages", 1, minimum=1, maximum=10)
    result_keywords = [_normalise_term(value) for value in config.get("result_keywords", []) if str(value).strip()]
    task_keywords = [str(value) for value in task.get("keywords", []) if str(value).strip()]
    timeout = int(config.get("timeout", 30))
    try:
        files_payload = http_json(f"{base_url}/api/results/files", timeout=timeout)
        files = [str(value) for value in files_payload.get("files", []) if str(value).endswith(".jsonl")]
        if result_keywords:
            files = [name for name in files if any(term in _normalise_term(name) for term in result_keywords)]
        files = files[:max_files]
        signals: List[Dict[str, Any]] = []
        for filename in files:
            for page in range(1, result_pages + 1):
                query = urlencode(
                    {
                        "limit": limit,
                        "page": page,
                        "sort_by": config.get("sort_by", "crawl_time"),
                        "sort_order": config.get("sort_order", "desc"),
                        "include_hidden": str(bool(config.get("include_hidden", False))).lower(),
                    }
                )
                url = f"{base_url}/api/results/{quote(filename, safe='')}?{query}"
                payload = http_json(url, timeout=timeout)
                raw_items = payload.get("items", [])
                if not raw_items:
                    break
                for raw in raw_items:
                    if not isinstance(raw, dict) or raw.get("_effective_hidden"):
                        continue
                    signal = parse_goofish_result_record(raw, task_keywords)
                    if signal:
                        signal["result_file"] = filename
                        signals.append(signal)
                if len(raw_items) < limit:
                    break
        fetched_count = len(signals)
        signals = _annotate_market_heat(signals)
        if str(config.get("signal_mode", "")).lower() == "heat":
            for signal in signals:
                signal["relevance_strength"] = signal.get("strength", 0.0)
                signal["strength"] = round(max(float(signal.get("strength", 0.0)), float(signal.get("heat_score", 0.0)) / 2.0), 2)
        signals = _filter_market_signals(signals, config)
        signals = _sort_market_signals(signals, config)
        LOGGER.info("goofish_market fetched files=%s signals=%s filtered=%s", len(files), fetched_count, len(signals))
        return {"enabled": True, "signals": signals, "files": files, "error": ""}
    except Exception as exc:  # noqa: BLE001 - market signals are optional.
        LOGGER.exception("goofish_market fetch failed")
        return {"enabled": True, "signals": [], "files": [], "error": str(exc)}


def apply_market_signals(item: Dict[str, Any], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = item_search_text(item)
    matched_terms: List[str] = []
    matched_refs: List[Dict[str, Any]] = []
    score = 0.0
    prices: List[float] = []
    for signal in signals:
        terms = [term for term in signal.get("terms", []) if term and term in text]
        if not terms:
            continue
        for term in terms:
            if term not in matched_terms:
                matched_terms.append(term)
        score += float(signal.get("strength", 0.0)) * min(2.0, 0.7 + 0.35 * len(terms))
        if signal.get("price") is not None:
            prices.append(float(signal["price"]))
        if len(matched_refs) < 5:
            matched_refs.append(
                {
                    "title": signal.get("title"),
                    "price": signal.get("price"),
                    "wants": signal.get("wants"),
                    "views": signal.get("views"),
                    "heat_score": signal.get("heat_score"),
                    "views_delta": signal.get("views_delta"),
                    "wants_delta": signal.get("wants_delta"),
                    "title_density": signal.get("title_density"),
                    "title_cluster_seller_count": signal.get("title_cluster_seller_count"),
                    "seller_repeat_count": signal.get("seller_repeat_count"),
                    "matched_terms": terms,
                    "is_recommended": signal.get("is_recommended"),
                    "link": signal.get("link"),
                }
            )
    market_score = round(min(35.0, score), 1)
    total_score = round(min(100.0, float(item.get("hotness_score", 0.0)) + market_score), 1)
    return {
        **item,
        "base_hotness_score": item.get("hotness_score", 0.0),
        "hotness_score": total_score,
        "market_match_score": market_score,
        "market_matched_terms": matched_terms[:12],
        "market_reference_count": len(matched_refs),
        "market_reference_titles": matched_refs,
        "market_avg_price": round(sum(prices) / len(prices), 2) if prices else None,
        "market_median_price": median(prices),
        "market_price_min": min(prices) if prices else None,
        "market_price_max": max(prices) if prices else None,
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


def fetch_member_delivery(
    item: Dict[str, Any],
    task: Dict[str, Any],
    last_request: List[float],
) -> Optional[Dict[str, Any]]:
    source_config = task.get("source_config") or {}
    if not source_config.get("fetch_member_delivery"):
        return None
    if not task.get("rights_confirmed"):
        return {
            "status": "skipped_rights_unconfirmed",
            "links": [],
            "passwords": [],
            "source_url": item.get("page_url", ""),
            "message": "未确认分发权，已跳过会员网盘链接抓取。",
        }
    page_url = str(item.get("page_url") or "")
    if not page_url:
        return {"status": "missing_page_url", "links": [], "passwords": [], "source_url": ""}
    cookie_env = str(source_config.get("member_cookie_env") or "THEITZY_COOKIE")
    cookie = os.getenv(cookie_env, "").strip()
    if not cookie:
        return {
            "status": "missing_cookie",
            "links": [],
            "passwords": [],
            "source_url": page_url,
            "message": f"未设置 {cookie_env}，无法请求会员页。",
        }
    interval = float(source_config.get("member_request_interval_seconds", source_config.get("request_interval_seconds", 10)))
    timeout = int(source_config.get("member_timeout", 30))
    try:
        sleep_between_requests(last_request, interval)
        content = http_text(
            page_url,
            timeout=timeout,
            headers={
                "Accept": "text/html",
                "Cookie": cookie,
                "Referer": str(source_config.get("base_url") or "https://theitzy.net"),
            },
        )
        last_request[:] = [time.monotonic()]
        delivery = extract_baidu_delivery_from_html(content, page_url)
        if not delivery.get("links"):
            context = extract_member_download_context(content, page_url)
            post_id = str(item.get("source_id") or "").strip() or context.get("post_id", "")
            ajax_url = context.get("ajax_url", "")
            if post_id and ajax_url:
                ajax_payload = http_form_json(
                    ajax_url,
                    {"action": "user_down_ajax", "post_id": post_id},
                    timeout=timeout,
                    headers={
                        "Cookie": cookie,
                        "Referer": page_url,
                    },
                )
                ajax_msg = str(ajax_payload.get("msg") or "")
                ajax_delivery = extract_baidu_delivery_from_html(ajax_msg, page_url)
                if not ajax_delivery.get("links") and ajax_payload.get("status") in {1, "1"} and ajax_msg:
                    go_url = urljoin(page_url, _decode_js_url(ajax_msg))
                    go_content = http_text(
                        go_url,
                        timeout=timeout,
                        headers={
                            "Accept": "text/html",
                            "Cookie": cookie,
                            "Referer": page_url,
                        },
                    )
                    ajax_delivery = extract_baidu_delivery_from_html(go_content, go_url)
                if ajax_delivery.get("links"):
                    delivery = _merge_delivery(delivery, ajax_delivery)
                elif ajax_payload.get("msg"):
                    delivery["message"] = str(ajax_payload.get("msg"))
            else:
                delivery["message"] = "页面没有找到下载按钮 data-id 或 ajaxurl。"
        LOGGER.info(
            "member_delivery fetched course_id=%s status=%s links=%s",
            item.get("id"),
            delivery.get("status"),
            len(delivery.get("links") or []),
        )
        return delivery
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        LOGGER.exception("member_delivery fetch failed course_id=%s", item.get("id"))
        return {
            "status": "error",
            "links": [],
            "passwords": [],
            "source_url": page_url,
            "message": str(exc),
        }


def format_delivery_links(item: Dict[str, Any], task: Dict[str, Any]) -> str:
    member_delivery = item.get("member_delivery") if isinstance(item.get("member_delivery"), dict) else None
    if task.get("rights_confirmed") and member_delivery and member_delivery.get("links"):
        lines = [f"- {link}" for link in member_delivery.get("links", [])]
        passwords = [str(value) for value in member_delivery.get("passwords", []) if str(value)]
        if passwords:
            lines.append(f"- 提取码/文件密码：{', '.join(passwords)}")
        lines.append("- 来源：TheItzy 会员页（请确认你拥有二次分发权后再发货）")
        return "\n".join(lines)

    owned_links = [str(link) for link in item.get("delivery_links", []) if str(link).strip()]
    if owned_links:
        return "\n".join(f"- {link}" for link in owned_links)

    if member_delivery and member_delivery.get("status") in {"missing_cookie", "not_found", "error", "missing_page_url"}:
        status_text = {
            "missing_cookie": member_delivery.get("message") or "未设置会员 Cookie，无法请求会员页。",
            "not_found": "会员页中没有识别到百度网盘链接。",
            "error": f"会员页请求失败：{member_delivery.get('message') or '未知错误'}",
            "missing_page_url": "缺少课程页地址，无法请求会员页。",
        }.get(str(member_delivery.get("status")), "")
        return f"- 待填入你自己的授权网盘链接\n- 会员链接抓取状态：{status_text}"

    if member_delivery and member_delivery.get("status") == "skipped_rights_unconfirmed":
        return "- 待填入你自己的授权网盘链接\n- 未确认分发权，已跳过会员网盘链接抓取。"

    return "- 待填入你自己的授权网盘链接"


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
    market_terms = item.get("market_matched_terms") or []
    market_reference = ""
    if market_terms:
        price_hint = ""
        if item.get("market_median_price") is not None:
            price_hint = f"；闲鱼同类监控中位价约 ¥{item.get('market_median_price')}"
        market_reference = (
            "\n【市场参考】\n"
            f"闲鱼热点监控命中：{', '.join(str(value) for value in market_terms[:8])}{price_hint}。"
            "该信息仅用于判断需求，不代表可直接发布或交付第三方内容。\n\n"
        )

    copy = (
        f"{title}\n"
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
        + market_reference
        + "【关键信息】\n"
        + f"主题分类：{category_text}\n"
        + "交付方式：审核通过后填写你自己的授权百度网盘链接\n"
        + "温馨提示：数字资料非实物，拍下前请确认内容清单、文件格式和售后规则。"
    )
    return apply_content_rules(copy)


def ai_copy_config() -> Dict[str, str]:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY") or ""
    base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("AI_BASE_URL") or "").rstrip("/")
    model = os.getenv("OPENAI_MODEL_NAME") or os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL") or ""
    return {"api_key": api_key, "base_url": base_url, "model": model}


def ai_copy_configured() -> bool:
    config = ai_copy_config()
    return bool(config["api_key"] and config["base_url"] and config["model"])


def maybe_ai_copy(item: Dict[str, Any], task: Dict[str, Any]) -> Optional[str]:
    config = ai_copy_config()
    if not config["api_key"]:
        return None
    base_url = config["base_url"]
    model = config["model"]
    if not base_url or not model:
        return None
    member_delivery = item.get("member_delivery") if isinstance(item.get("member_delivery"), dict) else {}
    delivery_ready = bool(task.get("rights_confirmed") and member_delivery.get("links"))
    content_rules = load_content_rules()
    forbidden_words = content_rules.get("forbidden_words") or []
    replacement = str(content_rules.get("replacement") or "")
    prompt = (
        "你是闲鱼数字课程商品文案编辑。请根据下面的课程元数据写一版可直接复制到闲鱼的中文商品文案。"
        "要求：\n"
        "1. 不声称官方授权，不虚构目录、时长、格式、售后或效果承诺。\n"
        "2. 不在商品文案里暴露百度网盘链接或提取码。\n"
        "3. 语气自然，适合个人卖家，避免夸张营销词。\n"
        "4. 输出结构固定为：标题、正文、适合人群、交付说明、注意事项、标签。\n"
        "5. 如果 delivery_ready=false，只能写“拍下前先确认内容清单”，不要写自动发货或网盘已备好。\n"
        "6. 如果 delivery_ready=true，可以写“拍下后发送百度网盘链接和提取码”。\n"
        f"7. 商品文案禁止出现这些词：{', '.join(str(word) for word in forbidden_words)}；如需表达，用“{replacement}”替代。\n"
        + json.dumps(
            {
                "title": item.get("title"),
                "categories": item.get("categories"),
                "summary": item.get("summary"),
                "market_median_price": item.get("market_median_price"),
                "market_price_min": item.get("market_price_min"),
                "market_price_max": item.get("market_price_max"),
                "market_matched_terms": item.get("market_matched_terms", []),
                "rights_confirmed": bool(task.get("rights_confirmed")),
                "delivery_ready": delivery_ready,
                "delivery_format": "百度网盘链接+提取码/文件密码" if delivery_ready else "待审核确认",
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
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return apply_content_rules(str(data["choices"][0]["message"]["content"]).strip(), content_rules)
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


def selection_db_path(output_dir: Path = OUTPUT_DIR) -> Path:
    return output_dir / "selection.sqlite3"


def init_selection_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS course_selections (
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_course_selections_published ON course_selections(published)")


def get_selection_statuses(course_ids: Iterable[str], db_path: Path) -> Dict[str, Dict[str, Any]]:
    ids = [str(value) for value in course_ids if str(value)]
    if not ids:
        return {}
    init_selection_db(db_path)
    placeholders = ",".join("?" for _ in ids)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT course_id, published, published_at, selection_count, last_selected_at
            FROM course_selections
            WHERE course_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    return {
        str(row["course_id"]): {
            "published": bool(row["published"]),
            "published_at": row["published_at"],
            "selection_count": int(row["selection_count"] or 0),
            "last_selected_at": row["last_selected_at"],
        }
        for row in rows
    }


def get_published_course_ids(db_path: Path) -> set[str]:
    init_selection_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT course_id FROM course_selections WHERE published = 1").fetchall()
    return {str(row[0]) for row in rows}


def get_cached_unpublished_items(course_ids: Iterable[str], db_path: Path) -> Dict[str, Dict[str, Any]]:
    ids = [str(value) for value in course_ids if str(value)]
    if not ids:
        return {}
    init_selection_db(db_path)
    placeholders = ",".join("?" for _ in ids)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT course_id, raw_json
            FROM course_selections
            WHERE published = 0 AND raw_json IS NOT NULL AND raw_json != '' AND course_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    cached: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            value = json.loads(str(row["raw_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            cached[str(row["course_id"])] = value
    return cached


def _merge_cached_item(cached: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    dynamic_keys = {
        "hotness_score",
        "base_hotness_score",
        "market_match_score",
        "market_matched_terms",
        "market_reference_count",
        "market_reference_titles",
        "market_avg_price",
        "market_median_price",
        "market_price_min",
        "market_price_max",
        "selection_status",
        "age_days",
    }
    merged = {**cached}
    for key in dynamic_keys:
        if key in current:
            merged[key] = current[key]
    merged["cache_reused"] = True
    return merged


def should_reuse_cached_item(cached: Dict[str, Any], task: Dict[str, Any]) -> bool:
    if not cached.get("copy") or not cached.get("delivery"):
        return False
    expected_rights = "confirmed" if task.get("rights_confirmed") else "required"
    if cached.get("rights_review") != expected_rights:
        return False
    configured_links = [str(link) for link in task.get("owned_delivery_links", []) if str(link).strip()]
    cached_links = [str(link) for link in cached.get("delivery_links", []) if str(link).strip()]
    if configured_links != cached_links:
        return False
    if ai_copy_configured() and cached.get("copy_source") != "ai":
        return False
    source_config = task.get("source_config") or {}
    needs_member_delivery = bool(source_config.get("fetch_member_delivery") and task.get("rights_confirmed"))
    if needs_member_delivery:
        member_delivery = cached.get("member_delivery") if isinstance(cached.get("member_delivery"), dict) else {}
        return bool(member_delivery.get("links"))
    return True


def save_selected_courses(
    items: List[Dict[str, Any]],
    *,
    run_id: str,
    task_name: str,
    db_path: Path,
) -> Dict[str, Dict[str, Any]]:
    init_selection_db(db_path)
    now = local_now().isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            for item in items:
                course_id = str(item.get("id") or "")
                if not course_id:
                    continue
                existing = conn.execute(
                    "SELECT first_selected_at, selection_count, published, published_at FROM course_selections WHERE course_id = ?",
                    (course_id,),
                ).fetchone()
                first_selected_at = existing[0] if existing else now
                selection_count = int(existing[1] or 0) + 1 if existing else 1
                published = int(existing[2]) if existing else 0
                published_at = existing[3] if existing else None
                conn.execute(
                    """
                    INSERT OR REPLACE INTO course_selections (
                        course_id, title, source, page_url, first_selected_at, last_selected_at,
                        last_run_id, last_task_name, selection_count, published, published_at,
                        updated_at, last_hotness_score, last_market_match_score, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course_id,
                        str(item.get("title") or ""),
                        str(item.get("source") or ""),
                        str(item.get("page_url") or ""),
                        first_selected_at,
                        now,
                        run_id,
                        task_name,
                        selection_count,
                        published,
                        published_at,
                        now,
                        float(item.get("hotness_score") or 0),
                        float(item.get("market_match_score") or 0),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
    return get_selection_statuses([str(item.get("id") or "") for item in items], db_path)


def set_course_published(course_id: str, published: bool, db_path: Path) -> Dict[str, Any]:
    init_selection_db(db_path)
    now = local_now().isoformat()
    published_at = now if published else None
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            existing = conn.execute(
                "SELECT course_id FROM course_selections WHERE course_id = ?",
                (course_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE course_selections
                    SET published = ?, published_at = ?, updated_at = ?
                    WHERE course_id = ?
                    """,
                    (1 if published else 0, published_at, now, course_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO course_selections (
                        course_id, title, source, page_url, first_selected_at, last_selected_at,
                        selection_count, published, published_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (course_id, course_id, "", "", now, now, 0, 1 if published else 0, published_at, now),
                )
    return get_selection_statuses([course_id], db_path).get(course_id, {"published": published})


def run_task(task: Dict[str, Any], *, output_dir: Path = OUTPUT_DIR, include_seen: bool = False) -> Dict[str, Any]:
    load_env_file()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "state.json"
    state = load_state(state_path)
    task_name = str(task.get("name") or "未命名任务")
    legacy_seen = state.setdefault("seen", {})
    db_path = selection_db_path(output_dir)
    published_course_ids = get_published_course_ids(db_path)
    run_id = local_now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = run_dir / "assets"
    asset_dir.mkdir(exist_ok=True)
    keywords = [str(value) for value in task.get("keywords", []) if str(value).strip()]
    exclude_keywords = [str(value) for value in task.get("exclude_keywords", []) if str(value).strip()]
    max_age_days_raw = task.get("max_age_days")
    max_age_days = float(max_age_days_raw) if max_age_days_raw not in {None, ""} else None
    output_limit = max(1, min(int(task.get("output_limit", 20)), 100))
    run_log: List[Dict[str, Any]] = []

    def note(message: str, **fields: Any) -> None:
        event = {
            "time": local_now().isoformat(),
            "message": message,
            **fields,
        }
        run_log.append(event)
        LOGGER.info("run_id=%s task=%s %s %s", run_id, task_name, message, fields)

    note(
        "run_start",
        source=task.get("source"),
        include_published=include_seen,
        keyword_count=len(keywords),
        exclude_keyword_count=len(exclude_keywords),
        max_age_days=max_age_days,
        output_limit=output_limit,
    )
    member_cookie_validation = validate_member_cookie(task)
    if member_cookie_validation:
        note(
            "member_cookie_validated",
            cookie_env=member_cookie_validation.get("cookie_env"),
            check_url=member_cookie_validation.get("check_url"),
            username=member_cookie_validation.get("username"),
        )
    market_data = fetch_goofish_market_signals(task)
    market_signals = list(market_data.get("signals", []))
    note(
        "market_signals_fetched",
        enabled=market_data.get("enabled"),
        file_count=len(market_data.get("files", [])),
        signal_count=len(market_signals),
        error=market_data.get("error", ""),
    )
    raw_items = fetch_source(task)
    note("source_fetched", fetched_count=len(raw_items))

    skipped_published = 0
    skipped_old = 0
    skipped_excluded = 0
    candidates: List[Dict[str, Any]] = []
    for raw in raw_items:
        item_id = str(raw.get("id") or "")
        if not include_seen and item_id in published_course_ids:
            skipped_published += 1
            continue
        scored = score_item(raw, keywords)
        scored = apply_market_signals(scored, market_signals)
        if max_age_days is not None and float(scored.get("age_days", 365.0)) > max_age_days:
            skipped_old += 1
            continue
        if exclude_keywords and matches_any_keyword(scored, exclude_keywords):
            skipped_excluded += 1
            continue
        candidates.append(scored)

    matched_candidates = [item for item in candidates if item.get("matched_keywords")]
    pool = matched_candidates if matched_candidates else candidates
    fallback_used = bool(candidates and not matched_candidates)
    pool.sort(key=lambda item: item.get("hotness_score", 0), reverse=True)
    items = pool[:output_limit]

    zero_reason = ""
    if not raw_items:
        zero_reason = "数据源没有返回文章，请检查 TheItzy API、网络或站点状态。"
    elif skipped_published == len(raw_items) and not include_seen:
        zero_reason = "本次抓到的文章都已经标记为已发布；如需复查已发布课程，请勾选“包含已发布”。"
    elif skipped_old and not candidates:
        zero_reason = f"本次抓到的文章都超过 {int(max_age_days or 0)} 天时效；已按新发布虚拟产品口径过滤。"
    elif skipped_excluded and not candidates:
        zero_reason = "本次抓到的文章都命中了排除词；已过滤远程安装、账号卡密、实体商品等不适合方向。"
    elif not candidates:
        zero_reason = "抓到了文章，但没有可参与评分的候选项。"
    elif not items:
        zero_reason = "候选项为空，未生成输出。"

    diagnostics = {
        "fetched_count": len(raw_items),
        "skipped_seen_count": skipped_published,
        "skipped_published_count": skipped_published,
        "skipped_old_count": skipped_old,
        "skipped_excluded_count": skipped_excluded,
        "candidate_count": len(candidates),
        "matched_count": len(matched_candidates),
        "unmatched_count": max(0, len(candidates) - len(matched_candidates)),
        "selected_count": len(items),
        "output_limit": output_limit,
        "fallback_used": fallback_used,
        "zero_reason": zero_reason,
        "keywords": keywords,
        "exclude_keywords": exclude_keywords,
        "max_age_days": max_age_days,
        "market_enabled": market_data.get("enabled"),
        "market_signal_count": len(market_signals),
        "market_files": market_data.get("files", []),
        "market_error": market_data.get("error", ""),
        "member_cookie_validated": bool(member_cookie_validation),
        "top_candidates": [
            {
                "title": item.get("title"),
                "hotness_score": item.get("hotness_score"),
                "matched_keywords": item.get("matched_keywords", []),
                "market_match_score": item.get("market_match_score", 0),
                "market_matched_terms": item.get("market_matched_terms", []),
            }
            for item in pool[:5]
        ],
    }
    note(
        "selection_done",
        fetched_count=diagnostics["fetched_count"],
        skipped_published_count=skipped_published,
        skipped_old_count=skipped_old,
        skipped_excluded_count=skipped_excluded,
        candidate_count=len(candidates),
        matched_count=len(matched_candidates),
        selected_count=len(items),
        fallback_used=fallback_used,
        zero_reason=zero_reason,
        market_signal_count=len(market_signals),
    )

    item_ids = [str(item.get("id") or "") for item in items]
    selection_statuses = get_selection_statuses(item_ids, db_path)
    reuse_unpublished = bool(task.get("reuse_unpublished_selections", True))
    cached_items = get_cached_unpublished_items(item_ids, db_path) if reuse_unpublished else {}
    source_config = task.get("source_config") or {}
    member_delivery_last_request: List[float] = []
    enriched_items: List[Dict[str, Any]] = []

    for item in items:
        course_id = str(item.get("id") or "")
        cached = cached_items.get(course_id)
        member_delivery_checked = False
        if cached and should_reuse_cached_item(cached, task):
            item = _merge_cached_item(cached, item)
        else:
            item["cache_reused"] = False
            item["cover_local_path"] = download_authorized_cover(item, task, asset_dir)
            item["rights_review"] = "confirmed" if task.get("rights_confirmed") else "required"
            item["delivery_links"] = list(task.get("owned_delivery_links", []))
            member_delivery = fetch_member_delivery(item, task, member_delivery_last_request)
            member_delivery_checked = True
            if member_delivery is not None:
                item["member_delivery"] = member_delivery
            ai_copy = maybe_ai_copy(item, task)
            item["copy"] = ai_copy or template_copy(item, task)
            item["copy_source"] = "ai" if ai_copy else "template"
        item["rights_review"] = "confirmed" if task.get("rights_confirmed") else "required"
        item["delivery_links"] = list(task.get("owned_delivery_links", []))
        if source_config.get("fetch_member_delivery") and not member_delivery_checked and not isinstance(item.get("member_delivery"), dict):
            member_delivery = fetch_member_delivery(item, task, member_delivery_last_request)
            if member_delivery is not None:
                item["member_delivery"] = member_delivery
        if item.get("copy"):
            item["copy"] = apply_content_rules(str(item["copy"]))
        item["selection_status"] = selection_statuses.get(course_id, item.get("selection_status") or {"published": False})
        enriched_items.append(item)
    items = enriched_items
    diagnostics["reused_unpublished_count"] = sum(1 for item in items if item.get("cache_reused"))
    diagnostics["member_delivery_found_count"] = sum(
        1
        for item in items
        if isinstance(item.get("member_delivery"), dict) and item["member_delivery"].get("links")
    )
    diagnostics["ai_copy_count"] = sum(1 for item in items if item.get("copy_source") == "ai")
    diagnostics["ai_model"] = ai_copy_config()["model"] if ai_copy_configured() else ""
    note(
        "items_enriched",
        reused_unpublished_count=diagnostics["reused_unpublished_count"],
        member_delivery_found_count=diagnostics["member_delivery_found_count"],
        ai_copy_count=diagnostics["ai_copy_count"],
        ai_model=diagnostics["ai_model"],
    )

    for index, item in enumerate(items, start=1):
        slug = f"{index:02d}-{safe_slug(item['title'])}"
        item_dir = run_dir / slug
        item_dir.mkdir(exist_ok=True)
        (item_dir / "copy.md").write_text(item["copy"], encoding="utf-8")
        delivery_status = "可进入授权审核后的发布流程" if task.get("rights_confirmed") else "未确认分发权，禁止发布"
        delivery_links = format_delivery_links(item, task)
        cover_info = (
            f"- 本地：{item['cover_local_path']}"
            if item.get("cover_local_path")
            else f"- 公开封面地址（仅供人工核验）：{item.get('cover_url') or '无'}"
        )
        delivery = item.get("delivery") or (
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
        item["delivery"] = delivery
        (item_dir / "delivery.md").write_text(delivery, encoding="utf-8")
        (item_dir / "item.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        seen_record = {"run_id": run_id, "title": item.get("title")}
        if item.get("id"):
            legacy_seen[str(item["id"])] = seen_record

    selection_statuses = save_selected_courses(items, run_id=run_id, task_name=task_name, db_path=db_path)
    for item in items:
        item["selection_status"] = selection_statuses.get(str(item.get("id") or ""), item.get("selection_status") or {"published": False})

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
                "base_hotness_score": item.get("base_hotness_score", item["hotness_score"]),
                "market_match_score": item.get("market_match_score", 0),
                "market_matched_terms": item.get("market_matched_terms", []),
                "market_reference_count": item.get("market_reference_count", 0),
                "market_reference_titles": item.get("market_reference_titles", []),
                "market_avg_price": item.get("market_avg_price"),
                "market_median_price": item.get("market_median_price"),
                "market_price_min": item.get("market_price_min"),
                "market_price_max": item.get("market_price_max"),
                "selection_status": item.get("selection_status", {"published": False}),
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
