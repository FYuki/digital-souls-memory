# Phase 0 リポジトリ基盤のローカル検証

- 日付：2026-10-20
- 対象：`infra/phase0-foundation`（正確な最終 head と CI 結果は対応する PR を参照）
- 環境：Ubuntu、Python 3.12.3、uv 0.8.22、Docker Engine 29.6.0
- 設計参照：PR #1 の head `9b06af572a92b6980d5bfb52c22bbd3cc225b8c6`。
  実装ブランチは指示どおり最新 `origin/main` から作成し、PR #1 の設計変更は取り込んでいない。

## 失敗からの実装

パッケージ実装前に UT / IT1 / PostgreSQL migration のテストを書いた。
`uv sync --frozen --no-install-project` 後の `uv run --no-sync pytest -m ut -q` は
`digital_souls_memory` 未実装のため collection error（終了 2）となった。
その後、API・必須 suite hook・migration を実装した。

## ローカル結果

| コマンド | 結果 |
| --- | --- |
| `uv lock --check` / `uv sync --frozen` | PASS |
| `ruff check src tests migrations` | PASS |
| `ruff format --check src tests migrations` | PASS（9 files） |
| `mypy` | PASS（strict、9 source files） |
| `pytest -m ut -q` | PASS 10件、FAIL 0、SKIP 0（5件 deselected） |
| `pytest -m it1 -q` | PASS 4件、FAIL 0、SKIP 0（11件 deselected） |
| `bash tools/with-test-postgres.sh` | PASS 1件、FAIL 0、SKIP 0（14件 deselected） |
| `bash -n tools/with-test-postgres.sh` | PASS |
| `docker compose config --no-interpolate --quiet` | PASS |
| `uv build --no-build-isolation` | PASS（sdist と wheel） |
| `uv export --frozen --no-dev --no-emit-project --format requirements-txt` | PASS |
| 独立 `.package-check` 環境で hash 検証付き依存 install → wheel の `--no-deps` install → `python -I` import | PASS |
| `git diff --check` | PASS |
| IT2 / ST | NOT RUN（Phase 0 に実装・実行対象なし） |

ruff / mypy / pytest はすべて `uv run --no-sync` で実行した。
required-suite hook の試験は、成功・失敗・skip・xfail・xpass・空 discovery・全件 deselect の
子 pytest 実行結果を検証し、外側の UT では全件 PASS となる。
deselected は指定した別 marker の対象外であり、SKIP や未実施 suite の PASS 化ではない。

UT / IT1 / PostgreSQL の collection 時に、参考実装と共通の Starlette / AnyIO に由来する
`BlockingPortal` alias の DeprecationWarning が 1 件ある。失敗や skip は発生していない。
pytest-socket 0.7.0 は型情報を持たないため、socket 禁止の試験で使う import のみ
`type: ignore[import-untyped]` とし、他の検査は strict を維持した。

実 DB は pgvector 0.8.2 / PostgreSQL 18 のタグ＋digest 固定イメージを使用した。
ローカルではネットワークなし・公開ポートなし・tmpfs の使い捨て DB と Unix socket を使い、
空 DB、upgrade head、vector 拡張の版・距離演算、再 upgrade、downgrade base を検証した。
実モデル・外部 LLM・実データは使っていない。
