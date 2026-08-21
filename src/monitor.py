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
from datetime import datetime, timezone
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

    def get(self, url: str) -> HttpResult:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = response.read(5_000_000)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return HttpResult(response.geturl(), data.decode(charset, errors="replace"), response.status)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                last_error = exc
                if attempt < self.retries:
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
    identity = "|".join(compact(str(v)) for v in values if v)
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


def source_error(source: dict[str, Any], query: str, exc: Exception, stage: str) -> dict[str, Any]:
    message = str(exc)
    category = "network_error"
    if "403" in message or "429" in message:
        category = "access_limited"
    elif "timed out" in message.lower():
        category = "timeout"
    return {
        "source": source["id"], "query": query, "stage": stage,
        "category": category, "message": message[:1000],
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


RETRYABLE_DRIVE_STATUS = {404, 408, 429, 500, 502, 503, 504}


def post_drive_gateway(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Post an idempotent Drive-gateway payload, retrying one transient failure only."""
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status >= 300:
                    raise RuntimeError(f"Drive gateway HTTP {response.status}: {body[:1000]}")
                decoded = json.loads(body)
                if not isinstance(decoded, dict):
                    raise RuntimeError("Drive gateway returned a non-object response")
                return decoded
        except urllib.error.HTTPError as exc:
            if attempt or exc.code not in RETRYABLE_DRIVE_STATUS:
                raise RuntimeError(f"Drive gateway HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt:
                raise RuntimeError(f"Drive gateway request failed: {exc}") from exc
        time.sleep(5)
    raise AssertionError("unreachable")


def slot_already_archived(gateway_url: str, secret: str, now: datetime, slot_hour: int) -> bool:
    """Ask the Drive gateway whether the intended KST slot already completed."""
    payload = {
        "action": "slot_status",
        "secret": secret,
        "date": now.strftime("%Y-%m-%d"),
        "scheduled_slot_hour_kst": slot_hour,
    }
    decoded = post_drive_gateway(gateway_url, payload, timeout=30)
    if not decoded.get("ok"):
        raise RuntimeError(f"Drive gateway slot status failed: {decoded.get('error', 'unknown error')}")
    return bool(decoded.get("completed"))


def collect(config: dict[str, Any], now: datetime, explicit_slot: int | None = None) -> dict[str, Any]:
    fetcher = Fetcher(config["collection"])
    errors: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    source_status: dict[str, dict[str, Any]] = {}
    for source in config["sources"]:
        attempted = succeeded = candidates_count = 0
        for product in config["products"]:
            for query in product["queries"]:
                attempted += 1
                search_url = source["url"].format(query=urllib.parse.quote(query))
                try:
                    page = fetcher.get(search_url)
                    succeeded += 1
                    candidates = extract_candidates(page, source, int(config["collection"]["max_candidates_per_query"]))
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
                        "seen_on": [], "queries": [], "search_prices": [],
                    })
                    if source["id"] not in record["seen_on"]:
                        record["seen_on"].append(source["id"])
                    if query not in record["queries"]:
                        record["queries"].append(query)
                    record["search_prices"] = sorted(set(record["search_prices"] + price_candidates(candidate["context"])))
        source_status[source["id"]] = {
            "queries_attempted": attempted, "queries_succeeded": succeeded,
            "matched_candidates": candidates_count,
            "status": "ok" if succeeded == attempted else ("partial" if succeeded else "failed"),
        }

    listings = []
    sellers: dict[str, dict[str, Any]] = {}
    detail_limit = int(config["collection"]["max_detail_pages_per_run"])
    for index, candidate in enumerate(merged.values()):
        meta: dict[str, Any] = {}
        if index < detail_limit:
            try:
                detail = fetcher.get(candidate["url"])
                meta = detail_metadata(detail)
            except Exception as exc:
                errors.append({
                    "source": ",".join(candidate["seen_on"]), "url": candidate["url"],
                    "stage": "listing_detail", "category": "detail_unavailable",
                    "message": str(exc)[:1000],
                    "response": "검색 결과 증거는 보존하고 상세 URL은 다음 실행 또는 Codex 심층 분석에서 재검증",
                })
        final_url = meta.get("final_url") or candidate["url"]
        product = next(p for p in config["products"] if p["id"] == candidate["product_id"])
        search_prices = candidate["search_prices"]
        price = meta.get("price") or (min(search_prices) if search_prices else None)
        shipping, shipping_status = shipping_from(candidate["context"] + " " + meta.get("page_text", ""))
        status, reason = evaluate(product["policy"], final_url, price)
        sid = seller_key(meta, final_url)
        sellers.setdefault(sid, {
            "seller_id": sid,
            "company_name": meta.get("company_name"),
            "representative_name": meta.get("representative_name"),
            "public_phone": meta.get("phone"),
            "business_address": meta.get("address"),
            "public_business_registration_number": meta.get("business_registration_number"),
            "seller_info_urls": [final_url],
            "identity_confidence": seller_confidence(meta),
        })
        listing_id = "listing-" + hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:16]
        listings.append({
            "listing_id": listing_id, "product_id": product["id"],
            "product_name": product["name"], "title": meta.get("title") or candidate["title"],
            "final_url": final_url, "seen_on": sorted(candidate["seen_on"]),
            "matched_queries": sorted(candidate["queries"]), "match_confidence": candidate["match_confidence"],
            "seller_id": sid, "displayed_price_krw": price,
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
    return {
        "schema_version": "1.0", "record_type": "immutable_scan_run",
        "run": {
            "run_id": run_id, "started_at": now.isoformat(), "completed_at": datetime.now(ZoneInfo(config["timezone"])).isoformat(),
            "timezone": config["timezone"], "scheduled_slot_hour_kst": slot_hour,
        },
        "rules_snapshot": config["products"], "source_statuses": source_status,
        "listings": listings, "sellers": list(sellers.values()), "errors": errors,
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
    lines += ["", "## 후속 처리", "", "- 2차 분석 AI는 `analysis-input`의 최근 3일 통합 문서를 우선 대조하고, 예외 확인이 필요할 때만 원본을 추가 조회합니다.", ""]
    return "\n".join(lines)


def markdown_cell(value: Any) -> str:
    """Keep generated Markdown tables readable without changing source data."""
    if value is None or value == "":
        return "확인불가"
    return re.sub(r"\s+", " ", str(value)).replace("|", "\\|")


def analysis_input(result: dict[str, Any]) -> str:
    """Build one compact, deterministic daily input for the second-pass AI analysis."""
    run = result["run"]
    summary = result["summary"]
    sellers = {item["seller_id"]: item for item in result["sellers"]}
    lines = [
        f"# {run['started_at'][:10]} AI 가격 분석 입력",
        "",
        "> GitHub Actions가 당일 08시 수집 결과를 분석용으로 정규화한 자료입니다.",
        "> 수집 실패·부분 성공은 상품 없음으로 해석하지 말고, 최종 판단에는 아래 상태와 신뢰도를 함께 반영하십시오.",
        "",
        "## 실행 정보",
        "",
        f"- 실행 ID: `{run['run_id']}`",
        f"- 수집 시작: `{run['started_at']}`",
        f"- 예정 회차: `{run['scheduled_slot_hour_kst']:02d}:00 KST`",
        f"- 탐지 URL: {summary['listing_count']}건 / 판매자 레코드: {summary['seller_count']}건 / 수집 오류: {summary['error_count']}건",
        f"- 판정: 치명 {summary['rule_counts'].get('critical', 0)}, 확인 필요 {summary['rule_counts'].get('review_required', 0)}, 경고 {summary['rule_counts'].get('warning', 0)}, 정상 {summary['rule_counts'].get('normal', 0)}, 허용 예외 {summary['rule_counts'].get('allowed_exception', 0)}",
        "",
        "## 채널별 수집 상태",
        "",
        "| 채널 | 상태 | 수집 방식 | 성공/시도 검색 | 탐지 후보 | 장애 성격 |",
        "|---|---|---|---:|---:|---|",
    ]
    for source_id, status in result["source_statuses"].items():
        incident = status.get("incident") or {}
        lines.append(
            f"| {markdown_cell(source_id)} | {markdown_cell(status.get('status'))} | "
            f"{markdown_cell(status.get('collection_method'))} | "
            f"{status.get('queries_succeeded', 0)}/{status.get('queries_attempted', 0)} | "
            f"{status.get('matched_candidates', 0)} | {markdown_cell(incident.get('classification') or '-')} |"
        )

    lines += [
        "",
        "## 상품 노출",
        "",
        "| 상품 키 | 판정 | 상품 | 표기가격 | 판매자 키 | 판매자 | 판매자 신뢰도 | 노출 채널 | URL |",
        "|---|---|---|---:|---|---|---|---|---|",
    ]
    for item in result["listings"]:
        seller = sellers.get(item["seller_id"], {})
        lines.append(
            f"| `{markdown_cell(item.get('listing_id'))}` | {markdown_cell(item.get('rule_status'))} | "
            f"{markdown_cell(item.get('title'))} | {markdown_cell(item.get('price_display'))} | "
            f"`{markdown_cell(item.get('seller_id'))}` | {markdown_cell(seller.get('company_name'))} | "
            f"{markdown_cell(seller.get('identity_confidence'))} | {markdown_cell(', '.join(item.get('seen_on', [])))} | "
            f"[원본]({item.get('final_url')}) |"
        )
    if not result["listings"]:
        lines.append("| - | 판단 유보 | 탐지 결과 없음(채널별 수집 상태 확인 필요) | - | - | - | - | - | - |")

    lines += [
        "",
        "## 판매자 식별 자료",
        "",
        "| 판매자 키 | 상호 | 대표자 | 사업자등록번호 | 전화번호 | 식별 상태 | 신뢰도 |",
        "|---|---|---|---|---|---|---|",
    ]
    for seller in result["sellers"]:
        lines.append(
            f"| `{markdown_cell(seller.get('seller_id'))}` | {markdown_cell(seller.get('company_name'))} | "
            f"{markdown_cell(seller.get('representative_name'))} | "
            f"{markdown_cell(seller.get('public_business_registration_number'))} | "
            f"{markdown_cell(seller.get('public_phone'))} | {markdown_cell(seller.get('seller_info_status'))} | "
            f"{markdown_cell(seller.get('identity_confidence'))} |"
        )
    if not result["sellers"]:
        lines.append("| - | 확인불가 | 확인불가 | 확인불가 | 확인불가 | unavailable | none |")

    lines += ["", "## 적용 가격 규칙", ""]
    for product in result["rules_snapshot"]:
        policy = product["policy"]
        if policy["type"] == "minimum_display_price":
            rule = f"정상 하한 {int(policy['normal_min_krw']):,}원 / 치명 하한 {int(policy['critical_below_krw']):,}원"
        else:
            rule = f"허용 목록 외 노출 금지 / 등록된 허용 URL {len(policy.get('allowlist_urls', []))}개"
        lines.append(f"- `{markdown_cell(product.get('id'))}` {markdown_cell(product.get('name'))}: {rule}")

    lines += ["", "## 오류 및 수집 공백", ""]
    if result["errors"]:
        for error in result["errors"]:
            lines.append(
                f"- 채널 `{markdown_cell(error.get('source'))}` / 단계 `{markdown_cell(error.get('stage'))}` / "
                f"분류 `{markdown_cell(error.get('category'))}`·`{markdown_cell(error.get('classification'))}`: "
                f"{markdown_cell(error.get('message'))}"
            )
    else:
        lines.append("- 기록된 수집 오류 없음")

    lines += ["", "## 실행 사고", ""]
    if result.get("incidents"):
        for incident in result["incidents"]:
            lines.append(
                f"- `{markdown_cell(incident.get('scope'))}` / `{markdown_cell(incident.get('classification'))}`: "
                f"{markdown_cell(incident.get('reason'))}"
            )
    else:
        lines.append("- 기록된 실행 사고 없음")

    lines += [
        "",
        "## 2차 분석 지침",
        "",
        "- 최근 3일의 이 폴더 내 동일 형식 문서만 기본 입력으로 사용합니다.",
        "- `listing_id`, `seller_id`, URL과 판매자 신뢰도를 함께 사용해 동일 상품·판매자를 대조합니다.",
        "- `failed` 또는 `partial` 채널은 위반 없음이나 상품 삭제로 단정하지 않습니다.",
        "- 원본 JSON은 식별 충돌, 급격한 가격 변동 등 예외를 확인해야 할 때만 추가로 조회합니다.",
        "",
        "## 원본 참조",
        "",
        f"- 원본 실행 JSON: `history/{run['started_at'][:4]}/{run['started_at'][5:7]}/{run['started_at'][8:10]}/{run['run_id']}.json`",
        f"- 기본 브리핑: `briefings/{run['started_at'][:4]}/{run['started_at'][5:7]}/{run['started_at'][:10]}_08시_기본_브리핑.md`",
        "",
    ]
    return "\n".join(lines)


def post_to_drive(url: str, secret: str, result_path: Path, briefing_path: Path | None,
                  analysis_input_path: Path | None = None) -> None:
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
    if analysis_input_path:
        analysis_raw = analysis_input_path.read_bytes()
        payload.update({
            "analysis_input_name": analysis_input_path.name,
            "analysis_input_base64": base64.b64encode(analysis_raw).decode("ascii"),
            "analysis_input_sha256": hashlib.sha256(analysis_raw).hexdigest(),
        })
    decoded = post_drive_gateway(url, payload, timeout=60)
    if not decoded.get("ok"):
        raise RuntimeError(f"Drive gateway rejected payload: {json.dumps(decoded, ensure_ascii=False)[:1000]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/monitoring.json")
    parser.add_argument("--output-dir", default="out")
    parser.add_argument("--force-briefing", action="store_true")
    parser.add_argument("--scheduled-slot-hour", type=int,
                        help="Intended KST schedule slot supplied by GitHub Actions; preserves 08시 briefing on delayed starts.")
    parser.add_argument("--skip-if-slot-exists", action="store_true",
                        help="Exit successfully when the Drive gateway already has this date and schedule slot.")
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
    result = collect(config, now, intended_slot)
    result_path = output_dir / f"{result['run']['run_id']}.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    is_briefing_slot = result["run"]["scheduled_slot_hour_kst"] == int(config["briefing_hour_kst"])
    briefing_path = None
    analysis_input_path = None
    if args.force_briefing or is_briefing_slot:
        briefing_path = output_dir / f"{now:%Y-%m-%d}_08시_기본_브리핑.md"
        briefing_path.write_text(basic_briefing(result), encoding="utf-8")
        analysis_input_path = output_dir / f"{now:%Y-%m-%d}_AI_가격분석_입력.md"
        analysis_input_path.write_text(analysis_input(result), encoding="utf-8")
    if not args.no_upload:
        gateway = os.environ.get("DRIVE_WEBHOOK_URL")
        secret = os.environ.get("DRIVE_SHARED_SECRET")
        if not gateway or not secret:
            raise RuntimeError("DRIVE_WEBHOOK_URL and DRIVE_SHARED_SECRET GitHub secrets are required")
        post_to_drive(gateway, secret, result_path, briefing_path, analysis_input_path)
    print(json.dumps({"result": str(result_path), "briefing": str(briefing_path) if briefing_path else None, "analysis_input": str(analysis_input_path) if analysis_input_path else None, "summary": result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

