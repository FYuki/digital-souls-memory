# 0020. マイグレーションは Alembic と psycopg 3 で管理する

- 日付：2026-10-20
- 状態：承認（2026-10-20、親エージェントがレビューして承認）

## 背景

[ADR 0007](0007-postgres-single-store.md) でストレージを PostgreSQL（pgvector）一本、
Docker 運用とした。Python サービスの初期基盤には、空 DB から同じ状態を作れ、
変更履歴と適用版を追跡できるマイグレーションが必要である。
目的は Memory 自身の DB スキーマの版管理であり、Core からのデータ移設ではない。
Phase 1 以降に追加する Turn などのテーブルも管理し、運用開始後に蓄積する原文台帳を
消さずにスキーマを変更できるようにする。Core のデータは開発時のものなので移行しない。

## 決定

- Alembic 1.16.5 と psycopg 3.3.6（binary 配布）を使い、revision を Git で管理する。
- Alembic の実行基盤として SQLAlchemy 2.0.43 の `postgresql+psycopg` dialect を使う。
  ORM によるドメインモデルや自動 schema 生成の採用は決めない。
- DB 接続先は `MEMORY_DATABASE_URL` で明示する。認証情報は設定ファイルやログへ出さない。
- migration は依存データを暗黙に削除しない。取り消しが依存データを壊す場合は失敗させる。
- アプリ起動時には migration を行わず、適用を独立したコマンドにする。
- 日本語全文検索（pg_bigm / PGroonga）は導入せず、Phase 1 の比較で決定する。

## 検討した案

- **SQL ファイルと独自 runner**：小さく始められるが、適用版の管理・順序・取り消しの仕組みを
  独自に保守する必要がある。既存の Python migration 基盤を優先する。
- **起動時の DDL / create_all**：適用順序と履歴・取り消しを追跡しにくく、
  複数プロセスの起動と schema 変更が混ざるため採用しない。
- **外部 migration CLI**：Python と別の配布・固定・実行基盤が必要になるため、今回は採用しない。
- **psycopg 2**：参考実装 Core と合わせて psycopg 3 を使う。

## 影響

- SQLAlchemy は Alembic の実行基盤として必要になる。
- スキーマの適用はアプリ起動と分離し、デプロイ側が管理する。
- 依存オブジェクトを壊す取り消しを許可しないため、取り消せない変更もある。
