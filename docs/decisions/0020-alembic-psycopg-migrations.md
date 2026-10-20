# 0020. マイグレーションは Alembic と psycopg 3 で管理する

- 日付：2026-10-20
- 状態：承認（2026-10-20、親エージェントがレビューして承認）

## 背景

[ADR 0007](0007-postgres-single-store.md) でストレージを PostgreSQL（pgvector）一本、
Docker 運用とした。Python サービスの初期基盤には、空 DB から同じ状態を作れ、
変更履歴と適用版を追跡できるマイグレーションが必要である。
Phase 0 ではドメインテーブルを作らず、pgvector 拡張の有効化までを対象にする。

## 決定

- Alembic 1.16.5 と psycopg 3.3.6（binary 配布）を使い、revision を Git で管理する。
- Alembic の実行基盤として SQLAlchemy 2.0.43 の `postgresql+psycopg` dialect を使う。
  ORM によるドメインモデルや自動 schema 生成の採用は決めない。
- DB 接続先は `MEMORY_DATABASE_URL` で明示する。認証情報は設定ファイルやログへ出さない。
- 初期 revision の upgrade は `CREATE EXTENSION vector` だけとする。
  downgrade は `DROP EXTENSION vector` とし、`CASCADE` で依存データを消さない。
- アプリ起動時には migration を行わず、適用を独立したコマンドにする。
- digest 固定の pgvector 0.8.2 / PostgreSQL 18 コンテナで、空 DB から upgrade head、
  拡張の有効化、再 upgrade、downgrade base を合成データだけで検証する。
- 日本語全文検索（pg_bigm / PGroonga）は導入せず、Phase 1 の比較で決定する。

## 検討した案

- **SQL ファイルと独自 runner**：小さく始められるが、適用版の管理・順序・取り消しの仕組みを
  独自に保守する必要がある。既存の Python migration 基盤を優先する。
- **起動時の DDL / create_all**：適用順序と履歴・取り消しを追跡しにくく、
  複数プロセスの起動と schema 変更が混ざるため採用しない。
- **外部 migration CLI**：Python と別の配布・固定・実行基盤が必要になるため、今回は採用しない。
- **psycopg 2**：参考実装 Core と合わせて psycopg 3 を使う。

## 影響

- SQLAlchemy は Alembic のための依存として加わる。migration revision は手動でレビューする。
- 拡張の作成権限を持つ DB role で適用する必要がある。
- 取り消しは依存オブジェクトがあると失敗する。運用時のバックアップ・復旧手順が別途必要となる。
- CI の DB 試験には実コンテナが必要だが、実モデル・秘密・本番データは不要である。
