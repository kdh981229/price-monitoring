"""이 PC의 네이버쇼핑 최저가 수집기 결과를 기존 판별·판정 규칙으로 정리해 Drive에 올린다.

GitHub Actions는 네이버 쇼핑에 로그인할 수 없어(307 로그인 리다이렉트) 이 수집은 사용자 PC에서만 돈다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.monitor import classify_product, evaluate, markdown_cell, post_drive_gateway  # noqa: E402

RULE_ORDER = {"critical": 0, "warning": 1, "review_required": 2, "allowed_exception": 3, "normal": 4}


def build_rows(items_by_query: dict[str, list[dict[str, Any]]], products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify collector items with the shared product rules; one row per Naver catalog/product id."""
    rows: dict[str, dict[str, Any]] = {}
    for query, items in items_by_query.items():
        for item in items:
            product, confidence = classify_product(str(item.get("title") or ""), "", products)
            key = str(item.get("product_id") or item.get("url"))
            if not product or key in rows:
                continue
            price = item.get("price") if isinstance(item.get("price"), int) else None
            rule, reason = evaluate(product["policy"], str(item.get("url") or ""), price)
            rows[key] = {
                "product_name": product["name"], "confidence": confidence, "query": query,
                "title": item.get("title"), "price_krw": price, "shipping_fee_krw": item.get("shipping_fee"),
                "benefit_total_krw": item.get("estimated_total"), "url": item.get("url"),
                "rule": rule, "reason": reason, "collected_at": item.get("collected_at"),
            }
    return sorted(rows.values(), key=lambda row: (RULE_ORDER.get(row["rule"], 9), row["price_krw"] or 0))


def won(value: Any) -> str:
    return f"{value:,}원" if isinstance(value, int) else "확인불가"


def briefing(date: str, rows: list[dict[str, Any]], failed: list[str]) -> str:
    lines = [
        f"# {date} PC 네이버쇼핑 가격",
        "",
        "- 출처: 사용자 PC의 네이버쇼핑 최저가 수집기(로그인된 크롬, 가격 낮은 순 검색 화면)",
        "- 판매가는 검색 화면에 표시된 최저가이며, 판매자는 링크를 열어 확인해야 합니다.",
        f"- 수집 실패 검색어: {', '.join(failed) if failed else '없음'} (실패는 가격 없음·정상으로 판정하지 않음)",
        "",
        "| 판정 | 상품 | 판매가 | 배송비 | 혜택 반영가 | 제목 | 링크 | 사유 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(markdown_cell(value) for value in [
            row["rule"], row["product_name"], won(row["price_krw"]), won(row["shipping_fee_krw"]),
            won(row["benefit_total_krw"]), row["title"], row["url"], row["reason"],
        ]) + " |")
    if not rows:
        lines.append("| - | 해당 상품 없음 | - | - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", help="pc-collector/results/YYYY-MM-DD")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "monitoring.json"))
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    results = Path(args.results_dir)
    date = results.name
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    items_by_query = {
        path.name.removesuffix("_lowest.json").replace("_", " "): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(results.glob("*_lowest.json"))
    }
    log = (results / "log.txt").read_text(encoding="utf-8-sig") if (results / "log.txt").exists() else ""
    failed = sorted({line.split(" FAILED ", 1)[1].split(" (")[0] for line in log.splitlines() if " FAILED " in line}
                    - set(items_by_query))
    rows = build_rows(items_by_query, config["products"])

    snapshot = {"record_type": "pc_price_snapshot", "date": date, "created_at": datetime.now().astimezone().isoformat(),
                "failed_queries": failed, "rows": rows}
    json_raw = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    md_raw = briefing(date, rows, failed).encode("utf-8")
    (results / "snapshot.json").write_bytes(json_raw)
    (results / "briefing.md").write_bytes(md_raw)
    if args.no_upload:
        return 0

    url, secret = os.environ.get("PRICE_GATEWAY_URL"), os.environ.get("PC_SHARED_SECRET")
    if not url or not secret:
        raise RuntimeError("PRICE_GATEWAY_URL and PC_SHARED_SECRET are required (run setup-secret.ps1 once)")
    payload = {
        "action": "pc_price_snapshot", "pc_secret": secret, "date": date,
        "json_base64": base64.b64encode(json_raw).decode("ascii"), "json_sha256": hashlib.sha256(json_raw).hexdigest(),
        "md_base64": base64.b64encode(md_raw).decode("ascii"), "md_sha256": hashlib.sha256(md_raw).hexdigest(),
    }
    decoded = post_drive_gateway(url, payload, timeout=60)
    if not decoded.get("ok"):
        raise RuntimeError(f"Drive gateway rejected payload: {json.dumps(decoded, ensure_ascii=False)[:1000]}")
    print(json.dumps(decoded, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
