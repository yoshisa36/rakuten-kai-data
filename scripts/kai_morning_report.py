#!/usr/bin/env python3
"""Read-only morning report for Threads and Rakuten exported JSON files."""
import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))

def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(JST)

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def report(rakuten, threads, now):
    lines = ["# カイ朝分析（参考値）", ""]
    generated = parse_time(rakuten["generatedAt"])
    threads_at = parse_time(threads["updatedAt"])
    lines += [f"- 楽天JSON生成: {generated:%Y-%m-%d %H:%M} JST",
              f"- Threads更新: {threads_at:%Y-%m-%d %H:%M} JST"]
    for label, stamp in [("楽天JSON", generated), ("Threads", threads_at)]:
        age = (now - stamp).total_seconds() / 3600
        status = "要確認" if age > 36 or age < -0.25 else "更新時刻は36時間以内"
        lines.append(f"- {label}鮮度: {status}（{age:.1f}時間前）")
    monitor = rakuten.get("monitor", {})
    for key, label in [("monthly", "月別CSV"), ("shop", "ショップ別CSV")]:
        stamp = monitor.get(key, {}).get("driveUpdatedAt")
        if stamp:
            age = (now - parse_time(stamp)).total_seconds() / 3600
            lines.append(f"- {label}更新: {age:.1f}時間前（{'要確認' if age > 36 else '36時間以内'}）")
    s = rakuten.get("summary", {})
    lines += ["", "## 楽天（JSON記載の集計期間）",
              f"- クリック: {s.get('clicks', '不明')} / 売上: {s.get('sales', '不明')} / 報酬: {s.get('rewards', '不明')}円",
              "- 自己クリックの除外は未実装。突出値は成果と断定しない。"]
    daily = sorted(rakuten.get("daily", []), key=lambda x: x.get("date", ""))
    if daily:
        lines += ["", "### 日別クリック（直近の記録）"]
        for d in daily[-7:]:
            lines.append(f"- {d.get('date')}: {d.get('clicks', 0)}")
    buckets = defaultdict(list)
    for p in threads.get("posts", []):
        slot = p.get("postingTime")
        if slot in {"11:30", "14:30", "17:30", "20:00"} and isinstance(p.get("views"), (int, float)):
            buckets[slot].append(p["views"])
    lines += ["", "## Threads 通常投稿枠の参考平均表示回数"]
    for slot in ["11:30", "14:30", "17:30", "20:00"]:
        values = buckets[slot]
        lines.append(f"- {slot}: {sum(values)/len(values):.1f}回（{len(values)}投稿）" if values else f"- {slot}: データなし")
    lines += ["", "※ 投稿後の経過時間・投稿内容は未調整。時間帯の優劣や売上への因果関係は断定できない。",
              "※ 日別クリックと個別投稿は直接紐付かない。"]
    return "\n".join(lines) + "\n"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rakuten", default="rakuten-affiliate.json")
    parser.add_argument("--threads", default="threads-analysis.json")
    parser.add_argument("--output")
    args = parser.parse_args()
    output = report(load(args.rakuten), load(args.threads), dt.datetime.now(JST))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output, end="")

if __name__ == "__main__":
    main()
