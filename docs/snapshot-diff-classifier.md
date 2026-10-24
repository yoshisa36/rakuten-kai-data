# Snapshot差分の保守的分類（Draft PR）

この段階では **オフラインの判定関数と人工テストのみ**。GitHub APIアクセス、定期実行、Secrets、GAS変更、投稿・返信・書き込みは一切追加しません。

`scripts/classify_snapshot_diff.py` の `classify(comparison, current_source_commit, snapshot_source_commit)` は、既存の `verify_snapshot.py` が出す `results` を持つ辞書と、呼び出し元が**独立に確認した**2つの40桁コミットSHAを受け取ります。SHA文字列の書式検査は行いますが、GitHub上の存在・履歴・取得元の真正性は証明しません。コミットの取得とスナップショットmanifestの検証は将来の別工程です。

- MATCH: 同じコミットで全ファイル一致。
- ALERT: 同じコミットで有効なJSON間に差分がある。
- EXPECTED_CHANGE: 異なるコミットで有効なJSON間に差分がある。**正常更新と断定せず、追加調査が必要な「想定される古さ」**を表す。履歴上の前後関係は未検証。
- UNKNOWN: データ不正・不足、取得失敗、矛盾する比較結果、異なるコミットで内容一致等。自動修復禁止。

予約IDの不一致は同じコミットならALERT、異なるコミットなら古いスナップショットの可能性として扱う。ただし予約IDの重複やJSON不正はUNKNOWNとして扱い、無条件に安全とはみなさない。

運用前に必要な追加工程: 比較元コミットの独立取得、manifestの検証、対象6ファイルの網羅確認、比較対象が指定コミットに由来することの検証、差分レポートの秘匿、正常な更新の判定基準。既存の正本は `rakuten-kai-data` のまま。自動返信はOFF維持。
