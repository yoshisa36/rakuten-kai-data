#!/usr/bin/env python3
"""Check freshness of rakuten-affiliate.json without authentication or external dependencies.

Exit codes: 0=fresh, 1=stale, 2=invalid/unavailable.
Generated-at freshness is NOT proof that source CSV data is fresh.
"""
import argparse
import datetime as dt
import json
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/yoshisa36/rakuten-kai-data/main/rakuten-affiliate.json"
JST = dt.timezone(dt.timedelta(hours=9))

def parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read local JSON instead of GitHub")
    parser.add_argument("--max-hours", type=float, default=36)
    args = parser.parse_args()
    try:
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                data = json.load(f)
        else:
            request = urllib.request.Request(URL, headers={"User-Agent": "rakuten-freshness-check/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
        generated = parse_timestamp(data.get("generatedAt"))
        if generated is None:
            raise ValueError("generatedAt missing or invalid")
        now = dt.datetime.now(dt.timezone.utc)
        hours = (now - generated.astimezone(dt.timezone.utc)).total_seconds() / 3600
        if hours < -0.25:
            raise ValueError("generatedAt is in the future")
        daily = data.get("daily")
        if not isinstance(daily, list):
            raise ValueError("daily is missing or not an array")
        dates = sorted({r.get("date") for r in daily if isinstance(r, dict) and isinstance(r.get("date"), str)})
        print(f"JSON生成日時: {generated.astimezone(JST).isoformat(timespec='seconds')}")
        print(f"生成からの経過: {hours:.1f}時間")
        print(f"日別データ件数: {len(daily)}")
        print(f"最終記録日: {dates[-1] if dates else 'なし'}")
        print("注意: CSV取得日時は現行JSONに独立した項目がないため検証できません")
        if hours > args.max_hours:
            print("判定: STALE (JSONの更新遅延)")
            return 1
        print("判定: JSON更新は期限内 / 元CSVの鮮度は未判定")
        return 0
    except Exception as exc:
        print(f"判定: ERROR ({exc})", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
