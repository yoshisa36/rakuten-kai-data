
#!/usr/bin/env python3
"""Rakuten affiliate data freshness monitor."""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

URL = (
    "https://raw.githubusercontent.com/"
    "yoshisa36/rakuten-kai-data/main/"
    "rakuten-affiliate.json"
)

JST = dt.timezone(dt.timedelta(hours=9))


def parse_time(value):
    if not value:
        raise ValueError("日時がありません")

    parsed = dt.datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        raise ValueError("タイムゾーンがありません")

    return parsed.astimezone(dt.timezone.utc)


def check_age(label, value, now, max_hours, errors):
    timestamp = parse_time(value)
    hours = (now - timestamp).total_seconds() / 3600

    print(
        f"{label}: "
        f"{timestamp.astimezone(JST).isoformat(timespec='seconds')}"
        f" / {hours:.1f}時間前"
    )

    if hours < -0.25:
        errors.append(f"{label}: 未来の日時です")
    elif hours > max_hours:
        errors.append(f"{label}: {max_hours}時間以上更新なし")

    return timestamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file")
    parser.add_argument("--url", default=URL, help="JSON source URL; --file takes precedence")
    parser.add_argument("--max-hours", type=float, default=36)
    args = parser.parse_args()

    try:
        if args.file:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        else:
            source = urlsplit(args.url)
            if source.scheme != "https" or not source.hostname or source.username or source.password:
                raise ValueError("JSON参照URLは認証情報を含まないHTTPS URLを指定してください")
            request = urllib.request.Request(
                args.url,
                headers={"User-Agent": "rakuten-freshness-check"}
            )
            with urllib.request.urlopen(
                request, timeout=15
            ) as response:
                raw = response.read().decode("utf-8-sig")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"JSON形式エラー: {exc}; "
                f"ファイル先頭: {raw[:100]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError("JSONの最上位がオブジェクトではありません")

        now = dt.datetime.now(dt.timezone.utc)
        errors = []

        generated = check_age(
            "JSON生成",
            data.get("generatedAt"),
            now,
            args.max_hours,
            errors
        )

        monitor = data.get("monitor")
        if not isinstance(monitor, dict):
            raise ValueError("monitorがありません")

        monthly_info = monitor.get("monthly")
        shop_info = monitor.get("shop")

        if not isinstance(monthly_info, dict):
            raise ValueError("monitor.monthlyがありません")
        if not isinstance(shop_info, dict):
            raise ValueError("monitor.shopがありません")

        monthly = check_age(
            "月別CSV",
            monthly_info.get("driveUpdatedAt"),
            now,
            args.max_hours,
            errors
        )

        shop = check_age(
            "ショップ別CSV",
            shop_info.get("driveUpdatedAt"),
            now,
            args.max_hours,
            errors
        )

        sync = check_age(
            "GAS取り込み",
            monitor.get("gasSyncLastSuccess"),
            now,
            args.max_hours,
            errors
        )

        if sync < max(monthly, shop):
            errors.append(
                "CSV更新後にGAS取り込みが実行されていません"
            )

        if generated < sync:
            errors.append(
                "GAS取り込み後にJSONが生成されていません"
            )

        daily = data.get("daily")
        if not isinstance(daily, list) or not daily:
            errors.append("日別実績がありません")
        else:
            print(f"日別実績: {len(daily)}件")

        if errors:
            for error in errors:
                print("異常:", error)
            return 1

        print("判定: 楽天データ連携正常")
        print(
            "注意: Drive更新日時は楽天側の"
            "データ確定を保証しません"
        )
        return 0

    except Exception as exc:
        print("監視エラー:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
