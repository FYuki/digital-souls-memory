# Identity API

Core から人格を登録し、セッション開始時に利用する版を取得します。
`digital_souls_memory.api:create_app --factory` で起動します。保存・読取りには
`MEMORY_DATABASE_URL` と独立した `alembic upgrade head` の実行が必要です。
起動・health は DB やモデルを要求せず、起動時の DDL はありません。

## 登録

パスの `character_id` は英数字・`_`・`-` で指定します。
本文に `caller`・`source` などの権限フィールドは指定できません。
ネットワーク認証はこの API の実装には含まれません。Core 専用の接続境界で提供してください。

| POST パス | 本文 | 動作 |
| --- | --- | --- |
| `/characters/{character_id}/core-identities` | `{"values":["誠実"],"prohibitions":["欺かない"]}` | 新版を追記 |
| `/characters/{character_id}/temperaments` | `{"description":"褒められると照れて話題を変える。"}` | 新版を追記。profile があれば整合審査 |
| `/characters/{character_id}/personality-profiles/author-seed` | `aspects` と `renderer_version` | 初回だけ生成・審査・保存 |

`aspects` は Volatility / Withdrawal / Compassion / Politeness / Industriousness /
Orderliness / Enthusiasm / Assertiveness / Intellect / Openness の10キーすべてを要求します。
値は有限の0〜1（両端を含む）です。欠落・未知キー・数値文字列・真偽値は拒否します。
description は本文では受け付けず、確定数値・気質とその版・renderer_version を
LLMGateway に渡して生成します。生成元の気質版を保存し、気質更新後も維持します。
初回の `change_kind` は `author_seed`、`evidence_belief_ids` は空です。

実 LLM アダプタは今回含みません。生成には `create_app(llm_gateway=..., reviewer=...)`
から `LLMGateway` と `Reviewer` ポートを注入します。未設定のポートを必要とする操作は
503 になります。ダミー生成・自動承認はありません。読取りと profile のない気質登録は可能です。

登録成功は201で版を返します。キャラ単位の transaction 内で版採番・審査・保存を行い、
commit 完了後に返します。生成・審査・保存・commit が失敗した候補は公開しません。
再 seed と自由な性格更新は受け付けず、気質更新でも profile を再生成しません。

## 取得とセッションでの版選択

`GET /characters/{character_id}/identity/current` は `core_identity`、`temperament`、
`personality_profile` を返します。未登録の項目は null です。コア・気質は最新登録版、
profile は取得時点で `valid_from <= 時点 < valid_to` の版（valid_to が null なら無期限）です。
PostgreSQL では組合せを同じ snapshot から読みます。

各登録パスの末尾に `/{version}` を付ける GET で版を指定できます
（profile は `/personality-profiles/{version}`）。不存在版は404で、最新版へ代替しません。
Core はセッション開始時の版 ID と取得結果を保持し、次のセッションで current を取得します。
この API は進行中のセッションの人格を差し替えません。

profile の HTTP 応答は description と版・生成元・有効期間の情報に限定し、
性格数値を含めません。数値は保存・記述変換用であり、Core 向けコンテキストに混入しません。

## エラー

| status | 意味 |
| --- | --- |
| 422 | 不正入力、または整合審査の却下（理由を detail に返す） |
| 403 | 登録以外の呼出し文脈による変更禁止 |
| 409 | 重複版・再 seed |
| 404 | 指定版・seed に必要な気質の不存在 |
| 503 | 保存・生成・審査の依存が利用不能 |

旧版は DB の UPDATE・DELETE 拒否でも保護します。
登録文・生成文・Evidence の observation に含む命令文字列は、権限として解釈しません。
