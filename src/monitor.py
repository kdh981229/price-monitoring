#!/usr/bin/env python3
"""Public-web listing collector and deterministic first-pass briefing generator."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PRICE_RE = re.compile(r"(?<!\d)([1-9]\d{1,2}(?:,\d{3})+|[1-9]\d{3,6})\s*원")
PHONE_RE = re.compile(r"(?<!\d)(0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4})(?!\d)")
BUSINESS_RE = re.compile(r"(?<!\d)(\d{3}[-\s]?\d{2}[-\s]?\d{5})(?!\d)")
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.I | re.S)
HREF_RE = re.compile(r"\bhref\s*=\s*(['\"])(.*?)\1", re.I | re.S)
JSONLD_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*(['\"])application/ld\+json\1[^>]*>(.*?)</script>",
    re.I | re.S,
)


def compact(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value or "")).lower()


def visible_text(fragment: str) -> str:
    fragment = SCRIPT_STYLE_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def canonical_url(raw_url: str, base_url: str) -> str | None:
    raw_url = html.unescape(raw_url).strip()
    if not raw_url or raw_url.startswith(("javascript:", "#")):
        return None
    absolute = urllib.parse.urljoin(base_url, raw_url)
    parsed = urllib.parse.urlsplit(absolute)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("url", "u", "target", "redirect", "redirect_url"):
        value = query.get(key)
        if value and value[0].startswith("http"):
            absolute = urllib.parse.unquote(value[0])
            parsed = urllib.parse.urlsplit(absolute)
            break
    if parsed.scheme not in ("http", "https"):
        return None
    kept = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith("utm_") or key.lower() in {
            "n_media", "n_query", "n_rank", "n_ad_group", "n_ad", "n_keyword",
            "source", "ref", "referrer", "tracking", "clickkey",
        }:
            continue
        kept.append((key, value))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(kept), ""))


def domain_matches(url: str, domains: list[str]) -> bool:
    if not domains:
        return True
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in domains)


def likely_search_navigation(url: str, source_url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    source_host = urllib.parse.urlsplit(source_url).hostname or ""
    if parsed.hostname != source_host:
        return False
    path = parsed.path.lower()
    return any(token in path for token in ("/search", "/search.naver", "/search.daum"))


def price_candidates(text: str) -> list[int]:
    values = []
    for match in PRICE_RE.finditer(text):
        value = int(match.group(1).replace(",", ""))
        if 10_000 <= value <= 2_000_000:
            values.append(value)
    return sorted(set(values))


def shipping_from(text: str) -> tuple[int | None, str]:
    normalized = compact(text)
    if "무료배송" in normalized or "배송비무료" in normalized:
        return 0, "confirmed"
    match = re.search(r"배송비\s*([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{2,5})\s*원", text)
    if match:
        return int(match.group(1).replace(",", "")), "confirmed"
    return None, "unknown"


def display_price(price: int | None, shipping: int | None, status: str) -> str:
    base = "가격 확인불가" if price is None else f"{price:,}원"
    if status == "confirmed" and shipping == 0:
        return f"{base} (무료배송)"
    if status == "confirmed" and shipping is not None:
        return f"{base} (+{shipping:,}원)"
    return f"{base} (배송비 확인불가)"


def classify_product(title: str, context: str, products: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    combined = compact(title + " " + context)
    title_compact = compact(title)
    for product in products:
        if any(compact(term) in combined for term in product.get("exclude_terms", [])):
            continue
        if any(compact(phrase) in combined for phrase in product.get("strong_phrases", [])):
            return product, "high"
        weak = [compact(term) for term in product.get("weak_terms", [])]
        contexts = [compact(term) for term in product.get("context_terms", [])]
        if any(term in title_compact for term in weak) and any(term in combined for term in contexts):
            return product, "medium"
    return None, "none"


def evaluate(policy: dict[str, Any], url: str, price: int | None) -> tuple[str, str]:
    if policy["type"] == "exposure_prohibited_except_allowlist":
        allowlist = [canonical_url(item, item) for item in policy.get("allowlist_urls", [])]
        if canonical_url(url, url) in allowlist:
            return "allowed_exception", "허용 예외 URL"
        if not allowlist:
            return "review_required", "허용 예외 URL이 아직 등록되지 않아 노출 확인 필요"
        return "critical", "허용 목록 외 노출"
    if price is None:
        return "review_required", "표시가격 확인 불가"
    critical = int(policy["critical_below_krw"])
    normal = int(policy["normal_min_krw"])
    if price < critical:
        return "critical", f"절대 하한 {critical:,}원 미만"
    if price < normal:
        return "warning", f"관리 하한 {normal:,}원 미만"
    return "normal", f"관리 하한 {normal:,}원 이상"


@dataclass
class HttpResult:
    url: str
    body: str
    status: int


class Fetcher:
    def __init__(self, config: dict[str, Any]):
        self.timeout = int(config["timeout_seconds"])
        self.retries = int(config["retries"])
        self.user_agent = config["user_agent"]

    def get(self, url: str, *, headers: dict[str, str] | None = None, retries: int | None = None) -> HttpResult:
        last_error: Exception | None = None
        attempts = self.retries if retries is None else retries
        for attempt in range(attempts + 1):
            request_headers = {
                "User-Agent": self.user_agent,
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
            }
            request_headers.update(headers or {})
            request = urllib.request.Request(
                url,
                headers=request_headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = response.read(5_000_000)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return HttpResult(response.geturl(), data.decode(charset, errors="replace"), response.status)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(str(last_error))


def extract_candidates(page: HttpResult, source: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for match in ANCHOR_RE.finditer(page.body):
        href_match = HREF_RE.search(match.group("attrs"))
        if not href_match:
            continue
        url = canonical_url(href_match.group(2), page.url)
        if not url or url in seen or likely_search_navigation(url, page.url):
            continue
        if not domain_matches(url, source.get("allowed_domains", [])):
            if source["id"] in {"11st", "gmarket", "auction"}:
                continue
        title = visible_text(match.group("body"))
        if len(title) < 2:
            continue
        window = page.body[max(0, match.start() - 900): min(len(page.body), match.end() + 1200)]
        context = visible_text(window)
        candidates.append({"url": url, "title": title[:500], "context": context[:3000]})
        seen.add(url)
        if len(candidates) >= limit:
            break
    return candidates


def naver_shopping_candidates(payload: dict[str, Any], page_url: str, limit: int) -> list[dict[str, Any]]:
    """Map documented Naver Shopping fields without inspecting ambiguous surrounding HTML."""
    candidates = []
    for item in payload.get("items", [])[:limit]:
        url = canonical_url(str(item.get("link") or ""), page_url) or str(item.get("link") or "")
        title = visible_text(str(item.get("title") or ""))
        mall = visible_text(str(item.get("mallName") or ""))
        try:
            price = int(str(item.get("lprice") or ""))
        except ValueError:
            price = None
        if price is not None and price <= 0:
            price = None
        if not url or not title:
            continue
        candidates.append({
            "url": url,
            "title": title,
            "context": f"판매몰 {mall}" if mall else "",
            "metadata": {"company_name": mall or None, "metadata_source": "naver_shopping_api"},
            "price_observation": {
                "source": "naver", "collection_method": "naver_shopping_api",
                "basis": "naver_shopping_lprice", "price_krw": price,
                "shipping_status": "unknown", "actionable": price is not None,
            },
        })
    return candidates


def kakao_web_candidates(payload: dict[str, Any], page_url: str, limit: int) -> list[dict[str, Any]]:
    """Daum Web API is discovery-only: it does not provide a seller-level price contract."""
    candidates = []
    for item in payload.get("documents", [])[:limit]:
        url = canonical_url(str(item.get("url") or ""), page_url) or str(item.get("url") or "")
        title = visible_text(str(item.get("title") or ""))
        if url and title:
            candidates.append({
                "url": url, "title": title, "context": visible_text(str(item.get("contents") or "")),
                "metadata": {"metadata_source": "kakao_web_api"},
            })
    return candidates


def source_candidates(fetcher: Fetcher, source: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    """Source adapters keep price evidence contracts separate from broad discovery methods."""
    method = source.get("collection_method", "public_html")
    if method == "naver_shopping_api":
        client_id = os.environ.get("NAVER_API_KEY_ID")
        client_secret = os.environ.get("NAVER_API_KEY")
        if not client_id or not client_secret:
            raise RuntimeError("official API configuration missing: NAVER_API_KEY_ID or NAVER_API_KEY")
        page = fetcher.get(source["url"].format(query=urllib.parse.quote(query), display=limit), headers={
            "X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret, "Accept": "application/json",
        })
        return naver_shopping_candidates(json.loads(page.body), page.url, limit)
    if method == "kakao_web_api":
        rest_key = os.environ.get("KAKAO_REST_API_KEY")
        if not rest_key:
            raise RuntimeError("official API configuration missing: KAKAO_REST_API_KEY")
        page = fetcher.get(source["url"].format(query=urllib.parse.quote(query), size=limit), headers={
            "Authorization": f"KakaoAK {rest_key}", "Accept": "application/json",
        })
        return kakao_web_candidates(json.loads(page.body), page.url, limit)
    page = fetcher.get(source["url"].format(query=urllib.parse.quote(query)))
    return extract_candidates(page, source, limit)


def actionable_price(observations: list[dict[str, Any]]) -> int | None:
    """Only an adapter with an explicit price contract may affect an automatic price alert."""
    values = {int(item["price_krw"]) for item in observations if item.get("actionable") and item.get("price_krw") is not None}
    return values.pop() if len(values) == 1 else None


def flatten_jsonld(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(flatten_jsonld(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(flatten_jsonld(child))
    return found


def detail_metadata(page: HttpResult) -> dict[str, Any]:
    text = visible_text(page.body)
    data: dict[str, Any] = {"final_url": canonical_url(page.url, page.url), "page_text": text[:30_000]}
    nodes: list[dict[str, Any]] = []
    for _, raw in JSONLD_RE.findall(page.body):
        try:
            nodes.extend(flatten_jsonld(json.loads(html.unescape(raw.strip()))))
        except (json.JSONDecodeError, TypeError):
            continue
    for node in nodes:
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "Product" in types:
            data.setdefault("title", node.get("name"))
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                raw_price = offers.get("price") or offers.get("lowPrice")
                try:
                    data["price"] = int(float(str(raw_price).replace(",", "")))
                except (TypeError, ValueError):
                    pass
                seller = offers.get("seller")
                if isinstance(seller, dict):
                    data.setdefault("company_name", seller.get("name"))
        if any(t in types for t in ("Organization", "LocalBusiness", "Store")):
            data.setdefault("company_name", node.get("name"))
            data.setdefault("phone", node.get("telephone"))
            address = node.get("address")
            if isinstance(address, dict):
                data.setdefault("address", " ".join(str(address.get(k, "")) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")).strip())
            elif isinstance(address, str):
                data.setdefault("address", address)
    phones = PHONE_RE.findall(text)
    businesses = BUSINESS_RE.findall(text)
    data.setdefault("phone", phones[0] if phones else None)
    data["business_registration_number"] = businesses[0] if businesses else None
    labels = {
        "company_name": ("상호명", "업체명", "회사명", "법인명"),
        "representative_name": ("대표자", "대표이사"),
        "address": ("사업장 소재지", "사업장주소", "영업소재지", "주소"),
    }
    for field, field_labels in labels.items():
        if data.get(field):
            continue
        for label in field_labels:
            match = re.search(re.escape(label) + r"\s*[:：]?\s*([^|\n]{2,120})", text)
            if match:
                data[field] = match.group(1).strip()
                break
    return data


def seller_key(meta: dict[str, Any], url: str) -> str:
    values = [
        meta.get("business_registration_number"), meta.get("phone"),
        meta.get("company_name"), meta.get("address"),
    ]
    identity = "|".join(compact(str(v)) for v in values if v and v != "확인불가")
    if not identity:
        identity = urllib.parse.urlsplit(url).hostname or url
    return "seller-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def seller_confidence(meta: dict[str, Any]) -> str:
    if meta.get("business_registration_number"):
        return "confirmed"
    if sum(bool(meta.get(k)) for k in ("phone", "company_name", "address")) >= 2:
        return "high"
    if meta.get("company_name") or meta.get("phone"):
        return "inferred"
    return "unknown"


def confirmed_or_unavailable(value: Any) -> str:
    return str(value).strip() if value else "확인불가"


def load_seller_registry(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load only human-verified seller records; an empty registry is a valid initial state."""
    path = Path(str(config.get("seller_registry_path", "config/seller_registry.json")))
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in payload.get("records", []) if item.get("verification_status") == "verified"]


def manual_seller_metadata(records: list[dict[str, Any]], url: str) -> dict[str, Any]:
    """Exact URLs only: never infer a seller from a whole marketplace domain."""
    canonical = canonical_url(url, url)
    for record in records:
        urls = [canonical_url(str(item), str(item)) for item in record.get("match_urls", [])]
        if canonical and canonical in urls:
            return {
                "company_name": record.get("company_name"),
                "representative_name": record.get("representative_name"),
                "phone": record.get("public_phone"),
                "address": record.get("business_address"),
                "business_registration_number": record.get("public_business_registration_number"),
                "seller_info_urls": record.get("verification_urls", record.get("match_urls", [])),
                "seller_info_status": "manual_verified",
                "metadata_source": "manual_registry",
            }
    return {}


def source_error(source: dict[str, Any], query: str, exc: Exception, stage: str) -> dict[str, Any]:
    message = str(exc)
    category = "network_error"
    classification = "pending"
    if "official API configuration missing" in message:
        category = "configuration_error"
        classification = "error"
    elif source["id"] == "naver" and "401" in message:
        # A documented API endpoint returning 401 means the deployed credential
        # pair/app registration is invalid, not a marketplace access limitation.
        category = "configuration_error"
        classification = "error"
    elif "403" in message or "429" in message:
        category = "access_limited"
        if source["id"] in {"gmarket", "auction"} and "403" in message:
            classification = "limitation"
    elif "timed out" in message.lower():
        category = "timeout"
    return {
        "source": source["id"], "query": query, "stage": stage,
        "category": category, "classification": classification, "message": message[:1000],
        "response": "다음 실행에서 재시도하고, 반복 시 Lessons.md의 해당 소스 대응 절차로 보완",
    }


def scheduled_slot_hour(config: dict[str, Any], now: datetime, explicit_slot: int | None = None) -> int:
    """Return the intended KST collection slot, even if Actions starts late."""
    hours = [int(hour) for hour in config["schedule_hours_kst"]]
    if explicit_slot is not None:
        if explicit_slot not in hours:
            raise ValueError(f"scheduled slot must be one of {hours}, got {explicit_slot}")
        return explicit_slot
    return max(hour for hour in hours if hour <= now.hour)


def trigger_metadata(now: datetime, slot_hour: int, trigger_type: str, scheduled_minute: int | None,
                     github_run_id: str | None, github_run_url: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Record schedule provenance so a late execution is diagnosable, not guessed later."""
    trigger: dict[str, Any] = {
        "trigger_type": trigger_type or "manual",
        "github_run_id": github_run_id or None,
        "github_run_url": github_run_url or None,
    }
    incidents: list[dict[str, Any]] = []
    if trigger_type != "schedule" or scheduled_minute is None:
        return trigger, incidents
    planned = now.replace(hour=slot_hour, minute=scheduled_minute, second=0, microsecond=0)
    if planned > now:
        planned -= timedelta(days=1)
    lateness_minutes = max(0, int((now - planned).total_seconds() // 60))
    trigger.update({
        "scheduled_for_kst": planned.isoformat(),
        "started_late_minutes": lateness_minutes,
        "scheduled_minute_kst": scheduled_minute,
    })
    if lateness_minutes >= 10:
        incidents.append({
            "classification": "error", "scope": "scheduler", "status": "active",
            "reason": f"예정 시각보다 {lateness_minutes}분 늦게 시작됨",
            "next_action": "GitHub 예약 지연을 Watchdog에서 감시하고, 반복 시 외부 스케줄러를 검토",
        })
    return trigger, incidents


def slot_already_archived(gateway_url: str, secret: str, now: datetime, slot_hour: int) -> bool:
    """Ask the Drive gateway whether the intended KST slot already completed."""
    payload = {
        "action": "slot_status",
        "secret": secret,
        "date": now.strftime("%Y-%m-%d"),
        "scheduled_slot_hour_kst": slot_hour,
    }
    request = urllib.request.Request(
        gateway_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        decoded = json.loads(response.read().decode("utf-8", errors="replace"))
    if not decoded.get("ok"):
        raise RuntimeError(f"Drive gateway slot status failed: {decoded.get('error', 'unknown error')}")
    return bool(decoded.get("completed"))


def collect(config: dict[str, Any], now: datetime, explicit_slot: int | None = None,
            trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    fetcher = Fetcher(config["collection"])
    verified_sellers = load_seller_registry(config)
    errors: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    source_status: dict[str, dict[str, Any]] = {}
    for source in config["sources"]:
        attempted = succeeded = candidates_count = 0
        for product in config["products"]:
            for query in product["queries"]:
                attempted += 1
                try:
                    candidates = source_candidates(
                        fetcher, source, query, int(config["collection"]["max_candidates_per_query"])
                    )
                    succeeded += 1
                except Exception as exc:  # per-source failure is data, not a whole-run crash
                    errors.append(source_error(source, query, exc, "search_fetch"))
                    continue
                for candidate in candidates:
                    matched, confidence = classify_product(candidate["title"], candidate["context"], config["products"])
                    if not matched:
                        continue
                    candidates_count += 1
                    key = candidate["url"]
                    record = merged.setdefault(key, {
                        "url": key, "title": candidate["title"], "context": candidate["context"],
                        "product_id": matched["id"], "match_confidence": confidence,
                        "seen_on": [], "queries": [], "price_observations": [], "search_metadata": {},
                    })
                    if source["id"] not in record["seen_on"]:
                        record["seen_on"].append(source["id"])
                    if query not in record["queries"]:
                        record["queries"].append(query)
                    observation = candidate.get("price_observation")
                    if observation and observation not in record["price_observations"]:
                        record["price_observations"].append(observation)
                    record["search_metadata"].update({
                        key: value for key, value in candidate.get("metadata", {}).items() if value
                    })
        status = "ok" if succeeded == attempted else ("partial" if succeeded else "failed")
        status_record = {
            "queries_attempted": attempted, "queries_succeeded": succeeded,
            "matched_candidates": candidates_count,
            "status": status,
            "collection_method": source.get("collection_method", "public_html"),
        }
        if status == "failed" and source["id"] in {"gmarket", "auction"}:
            status_record["incident"] = {
                "classification": "limitation",
                "reason": "현재 public_html 직접 수집 방식에서 모든 검색 요청이 반복적으로 차단됨",
                "next_action": "공식 API 또는 제휴 데이터 경로를 검토하고, 0건으로 판정하지 않음",
            }
        source_status[source["id"]] = status_record

    listings = []
    sellers: dict[str, dict[str, Any]] = {}
    detail_limit = int(config["collection"]["max_detail_pages_per_run"])
    detail_interval = float(config["collection"].get("detail_request_interval_seconds", 2.0))
    detail_attempts = 0
    for candidate in merged.values():
        meta: dict[str, Any] = dict(candidate.get("search_metadata", {}))
        manual_meta = manual_seller_metadata(verified_sellers, candidate["url"])
        product = next(p for p in config["products"] if p["id"] == candidate["product_id"])
        observations = candidate["price_observations"]
        preliminary_price = actionable_price(observations)
        preliminary_status, _ = evaluate(product["policy"], candidate["url"], preliminary_price)
        needs_enrichment = preliminary_status in {"critical", "warning", "review_required"}
        if needs_enrichment and not manual_meta and detail_attempts < detail_limit:
            try:
                # Detail requests are a bounded best-effort enrichment only. They never decide
                # whether a price exposure was detected, and do not retry to avoid rate-limit bursts.
                if detail_attempts:
                    time.sleep(detail_interval)
                detail = fetcher.get(candidate["url"], retries=0)
                meta.update(detail_metadata(detail))
                meta["seller_info_status"] = "public_page_observed"
                detail_attempts += 1
            except Exception as exc:
                detail_attempts += 1
                classification = "error" if "429" in str(exc) else "pending"
                errors.append({
                    "source": ",".join(candidate["seen_on"]), "url": candidate["url"],
                    "stage": "listing_detail", "category": "detail_unavailable", "classification": classification,
                    "message": str(exc)[:1000],
                    "response": "가격 탐지는 보존하고, 판매자 정보는 확인불가로 기록한다. 포털 상세 요청을 재시도하지 않는다.",
                })
        meta.update({key: value for key, value in manual_meta.items() if value})
        final_url = meta.get("final_url") or candidate["url"]
        # Detail-page prices can be coupon/member/option prices. They enrich seller identity only.
        price = actionable_price(observations)
        shipping = None
        shipping_status = "unknown"
        status, reason = evaluate(product["policy"], final_url, price)
        sid = seller_key(meta, final_url)
        sellers.setdefault(sid, {
            "seller_id": sid,
            "company_name": confirmed_or_unavailable(meta.get("company_name")),
            "representative_name": confirmed_or_unavailable(meta.get("representative_name")),
            "public_phone": confirmed_or_unavailable(meta.get("phone")),
            "business_address": confirmed_or_unavailable(meta.get("address")),
            "public_business_registration_number": confirmed_or_unavailable(meta.get("business_registration_number")),
            "seller_info_urls": meta.get("seller_info_urls") or [final_url],
            "identity_confidence": seller_confidence(meta),
            "seller_info_status": meta.get("seller_info_status") or (
                "search_result_observed" if meta.get("company_name") else "unavailable"
            ),
        })
        listing_id = "listing-" + hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:16]
        listings.append({
            "listing_id": listing_id, "product_id": product["id"],
            "product_name": product["name"], "title": meta.get("title") or candidate["title"],
            "final_url": final_url, "seen_on": sorted(candidate["seen_on"]),
            "matched_queries": sorted(candidate["queries"]), "match_confidence": candidate["match_confidence"],
            "seller_id": sid, "displayed_price_krw": price,
            "price_observations": observations,
            "shipping_fee_krw": shipping, "shipping_status": shipping_status,
            "price_display": display_price(price, shipping, shipping_status),
            "rule_status": status, "rule_reason": reason,
            "observed_at": now.isoformat(),
        })
    severity_order = {"critical": 0, "review_required": 1, "warning": 2, "normal": 3, "allowed_exception": 4}
    listings.sort(key=lambda item: (severity_order.get(item["rule_status"], 9), item["product_id"], item["final_url"]))
    counts = {key: 0 for key in severity_order}
    for item in listings:
        counts[item["rule_status"]] = counts.get(item["rule_status"], 0) + 1
    run_id = now.strftime("run-%Y%m%dT%H%M%S%z")
    slot_hour = scheduled_slot_hour(config, now, explicit_slot)
    run_trigger, incidents = trigger_metadata(
        now, slot_hour, (trigger or {}).get("trigger_type", "manual"),
        (trigger or {}).get("scheduled_minute_kst"), (trigger or {}).get("github_run_id"),
        (trigger or {}).get("github_run_url"),
    )
    return {
        "schema_version": "1.0", "record_type": "immutable_scan_run",
        "run": {
            "run_id": run_id, "started_at": now.isoformat(), "completed_at": datetime.now(ZoneInfo(config["timezone"])).isoformat(),
            "timezone": config["timezone"], "scheduled_slot_hour_kst": slot_hour, "trigger": run_trigger,
        },
        "rules_snapshot": config["products"], "source_statuses": source_status,
        "listings": listings, "sellers": list(sellers.values()), "errors": errors, "incidents": incidents,
        "summary": {"listing_count": len(listings), "seller_count": len(sellers), "error_count": len(errors), "rule_counts": counts},
    }


def basic_briefing(result: dict[str, Any]) -> str:
    run = result["run"]
    summary = result["summary"]
    lines = [
        f"# {run['started_at'][:10]} 08시 기본 브리핑",
        "",
        "> GitHub Actions 규칙 엔진이 만든 1차 자료입니다. 최종 판단 자료가 아니며 Codex 심층 분석 전 상태입니다.",
        "",
        f"- 실행 ID: `{run['run_id']}`",
        f"- 탐지 URL: {summary['listing_count']}건 / 판매자 식별: {summary['seller_count']}건 / 수집 오류: {summary['error_count']}건",
        f"- 판정: 치명 {summary['rule_counts'].get('critical', 0)}, 확인 필요 {summary['rule_counts'].get('review_required', 0)}, 경고 {summary['rule_counts'].get('warning', 0)}, 정상 {summary['rule_counts'].get('normal', 0)}",
        "",
        "| 1차 판정 | 상품 | 표기가격 | 판매자 | 노출 경로 | URL |",
        "|---|---|---:|---|---|---|",
    ]
    sellers = {item["seller_id"]: item for item in result["sellers"]}
    for item in result["listings"]:
        seller = sellers.get(item["seller_id"], {})
        seller_name = seller.get("company_name") or "확인불가"
        title = str(item["title"]).replace("|", "\\|")[:80]
        lines.append(
            f"| {item['rule_status']} | {title} | {item['price_display']} | {seller_name} | {', '.join(item['seen_on'])} | [접근]({item['final_url']}) |"
        )
    if not result["listings"]:
        lines.append("| - | 탐지 결과 없음 | - | - | - | - |")
    lines += ["", "## 수집 공백", ""]
    if result["errors"]:
        for error in result["errors"]:
            lines.append(f"- `{error.get('source', 'unknown')}` / `{error.get('stage')}` / {error.get('category')}: {error.get('message')}")
    else:
        lines.append("- 기록된 수집 오류 없음")
    lines += ["", "## 후속 처리", "", "- Codex Sol/high가 원본 JSON, 오류 기록, Lessons.md를 대조한 뒤 `final-briefings`에 별도 최종본을 생성합니다.", ""]
    return "\n".join(lines)


def post_to_drive(url: str, secret: str, result_path: Path, briefing_path: Path | None) -> None:
    raw = result_path.read_bytes()
    payload: dict[str, Any] = {
        "secret": secret,
        "run_id": json.loads(raw)["run"]["run_id"],
        "json_name": result_path.name,
        "json_base64": base64.b64encode(raw).decode("ascii"),
        "json_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if briefing_path:
        briefing_raw = briefing_path.read_bytes()
        payload.update({
            "briefing_name": briefing_path.name,
            "briefing_base64": base64.b64encode(briefing_raw).decode("ascii"),
            "briefing_sha256": hashlib.sha256(briefing_raw).hexdigest(),
        })
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8", errors="replace")
        if response.status >= 300:
            raise RuntimeError(f"Drive gateway HTTP {response.status}: {body[:1000]}")
        decoded = json.loads(body)
        if not decoded.get("ok"):
            raise RuntimeError(f"Drive gateway rejected payload: {body[:1000]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/monitoring.json")
    parser.add_argument("--output-dir", default="out")
    parser.add_argument("--force-briefing", action="store_true")
    parser.add_argument("--scheduled-slot-hour", type=int,
                        help="Intended KST schedule slot supplied by GitHub Actions; preserves 08시 briefing on delayed starts.")
    parser.add_argument("--skip-if-slot-exists", action="store_true",
                        help="Exit successfully when the Drive gateway already has this date and schedule slot.")
    parser.add_argument("--trigger-type", default="manual", choices=("manual", "schedule"))
    parser.add_argument("--scheduled-minute-kst", type=int, choices=(7, 17))
    parser.add_argument("--github-run-id")
    parser.add_argument("--github-run-url")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    now = datetime.now(ZoneInfo(config["timezone"]))
    intended_slot = scheduled_slot_hour(config, now, args.scheduled_slot_hour)
    if args.skip_if_slot_exists:
        gateway = os.environ.get("DRIVE_WEBHOOK_URL")
        secret = os.environ.get("DRIVE_SHARED_SECRET")
        if not gateway or not secret:
            raise RuntimeError("DRIVE_WEBHOOK_URL and DRIVE_SHARED_SECRET GitHub secrets are required")
        if slot_already_archived(gateway, secret, now, intended_slot):
            print(json.dumps({"status": "skipped", "reason": "scheduled slot already archived", "slot_hour_kst": intended_slot}, ensure_ascii=False))
            return 0
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = collect(config, now, intended_slot, {
        "trigger_type": args.trigger_type,
        "scheduled_minute_kst": args.scheduled_minute_kst,
        "github_run_id": args.github_run_id,
        "github_run_url": args.github_run_url,
    })
    result_path = output_dir / f"{result['run']['run_id']}.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    is_briefing_slot = result["run"]["scheduled_slot_hour_kst"] == int(config["briefing_hour_kst"])
    briefing_path = None
    if args.force_briefing or is_briefing_slot:
        briefing_path = output_dir / f"{now:%Y-%m-%d}_08시_기본_브리핑.md"
        briefing_path.write_text(basic_briefing(result), encoding="utf-8")
    if not args.no_upload:
        gateway = os.environ.get("DRIVE_WEBHOOK_URL")
        secret = os.environ.get("DRIVE_SHARED_SECRET")
        if not gateway or not secret:
            raise RuntimeError("DRIVE_WEBHOOK_URL and DRIVE_SHARED_SECRET GitHub secrets are required")
        post_to_drive(gateway, secret, result_path, briefing_path)
    verification = {
        source_id: {
            "status": item["status"], "collection_method": item["collection_method"],
            "queries_succeeded": item["queries_succeeded"], "queries_attempted": item["queries_attempted"],
        }
        for source_id, item in result["source_statuses"].items()
    }
    error_verification = [{
        "source": item.get("source"), "stage": item.get("stage"), "category": item.get("category"),
        "classification": item.get("classification"), "message": item.get("message"),
    } for item in result["errors"]]
    print(json.dumps({
        "result": str(result_path), "briefing": str(briefing_path) if briefing_path else None,
        "summary": result["summary"], "source_verification": verification,
        "error_verification": error_verification,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
