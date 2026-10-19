# rakuten-kai-data

Threads × 楽天アフィリエイト運用のデータと補助スクリプトを管理するリポジトリです。

## 開発時の安全なテスト

Python 3.12で、リポジトリのルートから実行します。追加パッケージやトークンは不要です。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/run_offline.py
```

このランナーはテスト読み込み前からsocket通信を拒否し、既存6件を含む全単体テストを実行します。テスト入力は`tests/fixtures/` の最小限の人工JSON、固定時刻、メモリ上の模擬データ、FakeAPIです。外部API・GASへの実通信や、投稿・返信・商品登録・予約登録は行いません。本番JSONや送信設定を書き換えません。socketガードは事故防止用で、任意のコードや子プロセスを隔離するサンドボックスではありません。

- `tests/test_register_threads_queue.py`: 既存の予約登録6件。FakeAPIによる重複防止、状態保持、承認・期限拒否、競合、保存後検証。
- `tests/test_json_integrity.py`: JSON構文、重複キー、件数、ID・時刻重複、承認済み予約の形式、指標の型など。人工データで承認待ち表現と未承認テスト予約を再現します。
- `tests/test_rakuten_freshness.py`: 時計とファイル読み込みをモックし、正常、CSV期限切れ、未来日時、欠損値、閾値境界、処理順序を再現します。

個別テストの確認には次も利用できます（PRのCIでは通信ガード付きランナーを使用します）。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

## Pull Requestの自動チェック

`.github/workflows/python-tests.yml` がPR作成、コミット更新、再オープン、レビュー準備完了時に自動実行されます。Draft PRも対象です。手動起動やSecrets追加は不要で、権限は `contents: read` のみです。

PRの **Checks** タブで **Python offline tests / Offline unit tests (Python 3.12)** を開いてください。ログ末尾の `Ran ... tests` と `OK`、終了状態の成功を確認します。失敗時は該当するテスト名とエラーを確認し、修正コミットで再検証します。このworkflowは本番処理や自動マージを実行しません。必須チェックとしてのブランチ保護設定は、この変更には含みません。

## 単体テストと運用監視の分離

PRのテストでは、現在時刻に対する本番JSONの鮮度を判定しません。本番JSONは単体テストで読み込みません。人工fixtureの形式と整合性を検証します。鮮度判定ロジック自体は固定データのテストで検証します。

既存の `.github/workflows/rakuten-freshness.yml` は毎日10:30 JST予定の運用監視です。CSV・GAS取り込み・JSON生成の36時間超過等を検出します。結果は **Actions → Rakuten data freshness check** で確認してください。単体テスト成功は本番データの鮮度やGAS・Threadsの稼働を保証しません。

予約登録の操作手順は [docs/threads-queue-registration.md](docs/threads-queue-registration.md) を参照してください。開発テストのために登録スクリプトや予約登録workflowを実行する必要はありません。

移行準備と参照先設定は [docs/migration-phase1.md](docs/migration-phase1.md) を参照してください。
