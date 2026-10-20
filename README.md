# digital-souls-memory

AIキャラクターの一貫性と成長を支える記憶サービスです。会話の原文を正本として保存し、長期記憶（一貫性）と内省（成長・自己変革）を、不変の気質・可変の性格・代表発話とあわせて管理します。

会話ランタイムの Core（脳）から HTTP API で呼ばれる独立サービス（精神）として提供します。FE（Web／PC ネイティブ）は Core に接続し、本サービスを直接は呼びません。

## ドキュメント

- [ドメインモデル](docs/domain-model.md)：境界づけられたコンテキスト、エンティティ、不変条件、主要フロー、評価指標、実装に向けた決定事項
- [決定記録（ADR）](docs/decisions/)：設計判断とその理由
- 調査資料
  - [長期記憶と内省における原文の重要性](docs/research/raw-vs-summary-memory.md)
  - [内省と長期記憶の相互作用（認知科学からの設計）](docs/research/reflection-and-memory-cognitive-science.md)

## 構成の概要

| コンテキスト | 役割 |
| --- | --- |
| Evidence | 発話・出来事の原文を追記専用で保存する正本 |
| Memory | 原文から作る出典つきの派生記憶（事実・関係・約束・検索キー） |
| Reflection | 原文を根拠にした内省、暫定の信念、動機・直近の興味 |
| Identity | コア人格・気質（登録API）、性格（Big Five 10アスペクト）、変更提案と履歴 |
| Voice | 逐語で保持する代表発話と、言い回し自体が重要な原文への参照 |
| Governance | 出典グラフ、削除の連鎖、書き込み権限と信頼階層 |

## 技術スタック

- Python / FastAPI
- PostgreSQL（pgvector、日本語全文検索）
- Docker

## セットアップ

Python **3.12.3**、uv **0.8.22** を使います。DB を利用する場合は Docker Engine と Compose v2 も必要です。

```sh
python3 -m pip install uv==0.8.22
uv sync --frozen
uv run --no-sync uvicorn digital_souls_memory.api:create_app --factory --host 127.0.0.1 --port 8000
```

`GET http://127.0.0.1:8000/health` は `{"status":"ok"}` を返します。
DB 接続・実モデル・認証情報を必要としない liveness です。
Phase 0 で実装するのはこの基盤までで、ドメインモデル・想起 API・LLM 接続は Phase 1 で扱います。

開発 DB は次の手順で起動・初期化します。パスワードと接続 URL はローカル環境で指定し、
リポジトリには保存しません。URL 内のパスワードは必要に応じて URL エンコードしてください。

```sh
# MEMORY_POSTGRES_PASSWORD と MEMORY_DATABASE_URL を環境へ設定する。
# URL の形式: postgresql://memory:<URLエンコード済みパスワード>@127.0.0.1:55432/memory
docker compose up -d --wait postgres
uv run --no-sync alembic upgrade head
uv run --no-sync alembic current
# 開発 DB を停止する（volume は保持）
docker compose down
```

PostgreSQL / pgvector イメージはタグと digest で固定しています。初期 migration は
`vector` 拡張のみを有効にし、日本語全文検索の方式は Phase 1 で比較します。

```sh
uv run --no-sync pytest -m ut -q
uv run --no-sync pytest -m it1 -q
# 開発 DB と独立した、使い捨ての合成 PostgreSQL 試験
bash tools/with-test-postgres.sh
```

lint・format・型検査・wheel 検証とブランチ運用は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。
