# digital-souls-memory

AIキャラクターの一貫性と成長を支える記憶サービスです。会話の原文を正本として保存し、長期記憶（一貫性）と内省（成長・自己変革）を、不変の気質・可変の性格・代表発話とあわせて管理します。

会話ランタイムの Core（脳）から HTTP API で呼ばれる独立サービス（精神）として提供します。FE（Web／PC ネイティブ）は Core に接続し、本サービスを直接は呼びません。

## ドキュメント

- [ドメインモデル](docs/domain-model.md)：決定した内容（Core との境界、境界づけられたコンテキスト、エンティティ、不変条件、主要フロー、評価指標、実装の構成）
- [決定記録（ADR）](docs/decisions/)：設計判断とその理由、検討した案
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

実装後に追記します。
