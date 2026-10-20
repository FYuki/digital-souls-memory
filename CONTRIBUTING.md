# 開発規約

## 正本と言語

この文書を開発規約の正本とし、AGENTS.md はエージェント向けの入口とします。
製品の設計は [ドメインモデル](docs/domain-model.md) と [ADR](docs/decisions/) を参照します。
人間向けの文書・PR 本文は日本語、コミットメッセージは日本語の Conventional Commits
（`feat:` / `infra:` / `docs:` / `test:` / `fix:` など）で書きます。
レビュー前の ADR は Proposed とし、承認と実装済みを混同しません。

## ブランチ・レビュー・merge

1. 最新の `origin/main` から `epic/*` を作ります。
2. epic から目的別の作業ブランチを切り、別 worktree で変更します。
3. 作業ブランチから対応する epic 宛ての Draft PR を出します。
4. 作業 PR → epic は **最終 head の CI グリーン**を条件とします。
5. epic → main は **最終 head の CI グリーンと実際の CodeRabbit レビュー完了・指摘対応**を条件とします。

bot の概要・review disabled 通知は CodeRabbit レビュー完了ではありません。
blocking 指摘を残したまま merge しません。作業ブランチから main への直接 PR は作りません。
これらの条件は merge 権限を与えません。依頼で許可された担当だけが merge します。
branch protection・ruleset・GitHub App などの設定は別途の判断なしに変更しません。
push 前に remote・対象ブランチ・差分を確認し、秘密情報・私的ログ・実データを含めません。

```sh
git fetch origin
git branch epic/phase0-foundation origin/main
git push -u origin epic/phase0-foundation
git worktree add -b infra/phase0-foundation ../digital-souls-memory-worktrees/phase0-foundation epic/phase0-foundation
```

## 固定ツールチェーンと品質ゲート

Python **3.12.3**（`.python-version`）と uv **0.8.22** を使います。
直接依存は `pyproject.toml` で固定し、推移依存・配布物ハッシュは `uv.lock` に記録します。
インストールにはネットワークが必要ですが、UT / IT1 に API キー・実モデル・GPU は不要です。
TDD に従い、振る舞いを検証する失敗テストを先に書いてから実装します。
依存更新は明示的に行い、lockfile の差分をレビューします。

```sh
python3 -m pip install uv==0.8.22
uv lock --check
uv sync --frozen
bash -n tools/with-test-postgres.sh
docker compose config --no-interpolate --quiet
uv run --no-sync ruff check src tests migrations
uv run --no-sync ruff format --check src tests migrations
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q
uv run --no-sync pytest -m it1 -q
bash tools/with-test-postgres.sh
uv build --no-build-isolation
uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/memory-runtime.txt
uv venv .package-check
uv pip install --python .package-check/bin/python --require-hashes -r /tmp/memory-runtime.txt
uv pip install --python .package-check/bin/python --no-deps dist/*.whl
.package-check/bin/python -I -c "from digital_souls_memory.api import create_app; assert create_app().title == 'Digital Souls Memory'"
git diff --check
```

ruff の check と format-check、mypy strict、UT、IT1、実 PostgreSQL 試験、wheel の
build・独立環境への locked install・import を必須とします。
CI は全 PR と `main` / `epic/**` への push で `Foundation checks / quality` と
`Foundation checks / postgres` を実行します。Actions は SHA 固定、権限は
`contents: read` のみとし、実モデル・secrets・`pull_request_target` は使いません。
IT2 / ST は CI で実行しません。required check 設定自体は所有者の判断です。

## テスト区分と結果

| marker | 区分・依存 | CI |
| --- | --- | --- |
| `ut` | 単体。ネットワーク・実 DB・実モデルを使わない | 必須 |
| `it1` | fake / fixture・TestClient によるプロセス内の結合。外部通信なし | 必須 |
| `postgres` | 実 DB コンテナの契約。合成データだけを使う | 必須 |
| `it2` | 明示的に承認された実モデルによる結合 | 実行しない |
| `st` | Compose 上で HTTP を通す E2E | 実行しない |

UT / IT1 は pytest-socket でネットワーク socket を禁止します。TestClient のため
Unix socket は許可します。psycopg の libpq による接続は pytest-socket の対象外なので、
実 DB を `postgres` marker へ分離し、接続先を使い捨ての合成 DB に限定します。
既定の pytest 実行は IT2 / ST を除外します。DB の用意がない通常実行では
`-m ut` または `-m it1` を明示してください。

必須 suite は `tests/conftest.py` により skip / xfail / xpass / 0 件を非 0 終了にします。
marker で全件除外した場合も失敗です。FAIL・SKIP・NOT RUN は PASS ではありません。
不足する検証は明示し、mock の成功を実環境の成功にしません。
IT2 / ST は実行許可・対象環境・revision・日時・秘密を除いた入力と結果・費用や制約を記録します。
Phase 0 は liveness と DB 拡張の基盤までで、ドメイン・想起 API・LLM は実装しません。

## 合成 PostgreSQL 試験

Docker Engine と Compose v2 を用意し、`uv sync --frozen` 後に次を実行します。

```sh
bash tools/with-test-postgres.sh
# または、同じ使い捨て DB 上でコマンドを指定する
bash tools/with-test-postgres.sh uv run --no-sync pytest -m postgres -q
```

スクリプトは `compose.yml` のタグ＋digest 固定イメージを使い、ネットワーク無効・公開ポートなし・
DB 本体は tmpfs の新規コンテナへ、一時 Unix socket から接続します。
終了時にコンテナと socket を削除し、開発 DB の volume は使いません。取得時のみネットワークが必要です。
CI は同じイメージのサービスコンテナと runner の localhost を使います。
`MEMORY_TEST_DATABASE_URL` は **空の専用合成 DB** だけに指定してください。
未設定なら試験は失敗し、通常実行用の `MEMORY_DATABASE_URL` にはフォールバックしません。
空 DB → upgrade head → vector 拡張・距離演算 → 再 upgrade → downgrade base を確認します。

## マイグレーション

[Alembic ＋ psycopg 3（ADR 0020）](docs/decisions/0020-alembic-psycopg-migrations.md) を使います。
SQLAlchemy は Alembic の接続・実行基盤で、製品の ORM 採用を意味しません。
接続先は `MEMORY_DATABASE_URL` を必須とし、`postgresql://` または
`postgresql+psycopg://` を受け付けます。認証情報を ini や revision に書きません。
アプリ起動時の自動適用はせず、デプロイ時に専用の実行手順として適用します。

```sh
uv run --no-sync alembic upgrade head
uv run --no-sync alembic current
uv run --no-sync alembic revision -m 'describe the change'
# 使い捨て DB での取り消し確認（運用 DB には実行しない）
uv run --no-sync alembic downgrade base
```

revision は手でレビューし、upgrade / downgrade を合成 DB で検証します。
初期 revision は `CREATE EXTENSION vector` だけを行い、ドメインテーブルは作りません
（Alembic 自身の版管理テーブルは作られます）。downgrade は `DROP EXTENSION vector` とし、
`CASCADE` を使わず依存データがあれば失敗させます。実 DB の適用前はバックアップと復旧手順を確認します。
日本語全文検索（pg_bigm / PGroonga）とモデルの選定は Phase 1 に残します。
