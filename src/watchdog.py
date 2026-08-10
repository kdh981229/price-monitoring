"""Independent collection watchdog for the price monitoring pipeline."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
VALID_SLOTS = (0, 4, 8, 12, 16, 20)


def post_json(url: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Drive gateway request failed: {exc}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"
        raise RuntimeError(f"Drive gateway rejected watchdog request: {error}")
    return result


def slot_completed(url: str, secret: str, date: str, slot: int) -> bool:
    result = post_json(
        url,
        {
            "action": "slot_status",
            "secret": secret,
            "date": date,
            "scheduled_slot_hour_kst": slot,
        },
    )
    if not isinstance(result.get("completed"), bool):
        raise RuntimeError("Drive gateway returned no boolean completed status")
    return result["completed"]


def build_report(
    *, date: str, slot: int, completed: bool, checked_at: datetime, workflow_url: str
) -> dict[str, Any]:
    compact_date = date.replace("-", "")
    check_id = f"watchdog-{compact_date}-{slot:02d}"
    return {
        "schema_version": "1.0",
        "record_type": "watchdog_check",
        "check_id": check_id,
        "checked_at": checked_at.astimezone(KST).isoformat(timespec="seconds"),
        "expected_date_kst": date,
        "expected_slot_hour_kst": slot,
        "outcome": "healthy" if completed else "missing",
        "collector_grace_minutes": 28,
        "workflow_url": workflow_url or None,
        "message": (
            f"{date} {slot:02d}:00 KST collection exists in Drive."
            if completed
            else f"{date} {slot:02d}:00 KST collection is missing after the fallback window."
        ),
    }


def upload_report(url: str, secret: str, report: dict[str, Any]) -> dict[str, Any]:
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return post_json(
        url,
        {
            "action": "watchdog_check",
            "secret": secret,
            "report_name": f"{report['check_id']}.json",
            "report_base64": base64.b64encode(data).decode("ascii"),
            "report_sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether one scheduled collection reached Drive.")
    parser.add_argument("--date", help="Expected KST date (YYYY-MM-DD); defaults to today in KST")
    parser.add_argument("--scheduled-slot-hour", type=int, required=True, choices=VALID_SLOTS)
    parser.add_argument("--dry-run", action="store_true", help="Print the report without uploading it")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(KST)
    date = args.date or now.date().isoformat()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must use YYYY-MM-DD") from exc

    url = os.environ.get("DRIVE_WEBHOOK_URL", "").strip()
    secret = os.environ.get("DRIVE_SHARED_SECRET", "").strip()
    if not args.dry_run and (not url or not secret):
        raise SystemExit("DRIVE_WEBHOOK_URL and DRIVE_SHARED_SECRET are required")

    completed = False if args.dry_run else slot_completed(url, secret, date, args.scheduled_slot_hour)
    report = build_report(
        date=date,
        slot=args.scheduled_slot_hour,
        completed=completed,
        checked_at=now,
        workflow_url=os.environ.get("WATCHDOG_WORKFLOW_URL", ""),
    )
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    response = upload_report(url, secret, report)
    print(json.dumps({"report": report, "storage": response}, ensure_ascii=False, indent=2))
    if not completed:
        print("::error::Scheduled collection is missing; incident was recorded.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
