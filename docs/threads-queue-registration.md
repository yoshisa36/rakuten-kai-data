# 承認済みThreads予約の登録

安全チェックの解除や迂回は行いません。権限が認められたGitHub Contents APIで予約JSONを更新する登録プログラムです。過去のChatGPT側ブロックの根本原因は未確定で、このプログラムでもすべての拒否を解消できるとは限りません。

## 実行方法

1. 承認済み投稿を `batches/YYYY-MM-DD.json` に保存します。10/5分を実例として同梱しています。
2. GitHub Actionsの「Register approved Threads queue」で「Run workflow」を選び、mainとバッチのパスを指定します。
3. `registered_and_verified` または `already_registered` を確認します。
4. GASの予約シートで、同じ予約IDが「予約済み」になったことを確認します。投稿時刻を過ぎたら投稿IDとThreads本体を確認します。

プログラムは投稿本文の生成・ユーザー承認・GASへのインストール・Threadsへの投稿は行いません。Actionsは手動起動です。既存の分析自動化からこの登録工程を呼ぶ場合も、バッチに明示的な承認が必要です。

ローカルで使う場合は、Contents書き込み権限を持つ正規のトークンを環境変数 `GITHUB_TOKEN` に設定し、`python3 scripts/register_threads_queue.py batches/2026-10-05.json` を実行します。トークンをコード・バッチ・チャットに記載しないでください。ActionsではGitHubが発行するGITHUB_TOKENを使います。組織の制約・ブランチ保護があれば通常どおり適用されます。

## 検証と失敗の扱い

- 最新SHAを取得して追加し、HTTP409の競合だけ最大3回、最新版へ再統合します。
- 同じ予約ID・同じ投稿内容は追加しません。既存の「投稿済み」などの状態を戻しません。
- 別IDによる同じ実運用時刻への登録、同じIDの本文変更、未承認、テスト、期限切れの新規登録は拒否します。
- 既存の投稿と追加フィールドを保持します。過去のテスト予約は自動承認しません。
- 保存後に全対象IDと本文・時刻・リンク方式・URL・承認を再取得して照合します。updatedAtだけで成功判定しません。
- 権限拒否、ポリシー拒否、検証エラーは停止します。別資格情報や別エンドポイントへの切替はありません。
- 通信切断時は成功扱いにせず、再実行で同一IDを確認します。既に保存済みなら重複追加しません。

GitHub側の `status="承認待ち"` と `approvalStatus="approved"` は現在の既存データ形式を維持しています。GASが取り込んだかは別に確認します。GitHub登録だけでは「Threads予約完了」と断定しません。

6件の単体テストで、重複、状態保持、ID/時刻競合、承認・期限、SHA競合と権限拒否、保存後検証の失敗を確認しています。

公式資料: https://docs.github.com/en/rest/repos/contents と https://docs.github.com/en/actions/tutorials/authenticate-with-github_token
