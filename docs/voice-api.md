# Voice API

Core 専用の登録・取得 API です。作者が承認した逐語の代表発話と Turn 参照を保存します。
認証の追加方式を定める API ではなく、approved_by は承認者の監査情報です。
本文に含まれる命令を権限として解釈しません。

## 起動と保存

通常の `digital_souls_memory.api:create_app --factory` で router が有効になります。
`MEMORY_DATABASE_URL` を指定し、明示的に `alembic upgrade head` を実行してください。
起動時の自動 DDL はありません。資料と初期整合チェックは PostgreSQL に保存します。
`create_app(voice_store=..., store=..., identity_store=..., llm_gateway=..., reviewer=..., clock=...)`
で各ポートを注入できます。実モデル接続は提供していません。

## 代表発話

`POST /characters/{character_id}/voice-exemplars` は、次の一括入力を受け取ります。

```json
{
  "exemplars": [
    {
      "exemplar_id": "voice-1",
      "text": "私は約束を守ります。\nI shall keep it.",
      "source": "canonical_seed",
      "turn_id": null,
      "situation_tags": ["約束"],
      "emotion_tags": ["穏やか"],
      "register": {"first_person": "私", "politeness": "敬語"},
      "trait_version": 1,
      "approved_by": "synthetic-author"
    }
  ]
}
```

source は `canonical_seed` または `turn_id` です。前者の turn_id は null、
後者は存在する Turn の ID を指定します。trait_version は同じキャラの人格版を参照します。
Turn 由来の text は利用可能な原文の連続部分と完全一致する必要があります。
空白除去・翻訳・Unicode 正規化・言い換えはしません。
空のセットや空白のみの承認者を拒否し、不正な項目があるセットは全体を保存しません。

初回の少数の承認済みセットでも、既存 LLMGateway の `generate_typical_responses` に
気質・人格版・代表発話を渡し、典型的状況への応答を生成します。
Reviewer の `review_voice` が応答と資料の整合を審査し、
201 の `consistency_check: {"consistent": true/false, "reason": "..."}` として作者へ返します。
不一致でも作者向けの審査結果として返し、自動修正・人格更新・自動承認はしません。
30〜50件は推奨であり最低件数ではありません。
初回チェックと資料はキャラ単位で直列化し、同じ transaction で保存します。
成功後の追加登録は保存済みの結果を返し、取得やアプリ再生成でも再審査しません。
生成・審査・保存失敗は登録を公開せず、再試行できます。
失敗した外部モデル呼出しの厳密な一回実行は保証しません。

`GET /characters/{character_id}/voice-exemplars` と
`GET /characters/{character_id}/voice-exemplars/{exemplar_id}` で、
登録属性と character_id・registered_at を取得できます。

## 逐語アンカー

`POST /characters/{character_id}/verbatim-anchors`:

```json
{
  "anchor_id": "anchor-1",
  "turn_id": "turn-1",
  "kind": "commitment",
  "reason": "作者が選んだ約束の原文",
  "approved_by": "synthetic-author"
}
```

kind は `identity_statement`、`commitment`、`nickname`、`anniversary` の4種類です。
不存在 Turn・未知 kind・未承認入力を拒否します。
`GET /characters/{character_id}/verbatim-anchors` と
`GET /characters/{character_id}/verbatim-anchors/{anchor_id}` を提供します。
201 および取得結果は登録属性・character_id・registered_at と利用可能な Turn 原文の text を返します。
本文は独立保存しません。時間による減衰・取得による再強化を行いません。

## 知覚・配達範囲

Turn 由来の登録と、個別取得・一覧取得のすべてで既存の can_perceive を使います。
キャラが perceived_by に含まれない資料を返しません。
addressees・address_mode・reply_to_turn_id・在席から知覚を補完しません。
self は声に出す発言として通常の知覚を適用し、thought は本人だけです。

reports_delivery=false は即時全文です。true のキャラ utterance について本人は生成全文を使えます。
本人以外は追記順の最後の Receipt の spoken_until までを使います。
none は空本文、未着は recorded_at から既定10分まで保留し、期限到達でみなし全文です。
`create_app(delivery_timeout=timedelta(seconds=30))` のように期間を設定できます。
負の期間は起動時に拒否します。
保留中の登録は拒否し、保留中・範囲外の個別取得は404、一覧からは除外します。
保留状態の永続化・抽出スケジュールは実装しません。

Receipt 追記と時計の変化を毎回再評価し、保存済みの資料にも適用します。
登録時は初回審査後、Receipt追記と共有するロックを取得して同じtransactionの接続で
本文を再読します。その判定から保存可否と201の本文を確定します。
保存済み代表発話が新しい配達範囲を超えたら返却から除外し、切り詰めた別の代表発話にはしません。
アンカーは同じ Turn 参照のまま、返す text だけを現在利用可能な原文範囲に制限します。
既存の Evidence 原文台帳 API は生成全文を保持します。

## 拒否・障害

入力違反は422、参照や取得可能な資料の不在は404、ID重複は409、
依存ポート・DBの利用不能は503です。通常権限での登録後 UPDATE・DELETE は DB が拒否します。
人格の書込・再 seed、ランキング、完全な削除連鎖、実モデルの口調評価は本 API の対象外です。
Voice Fidelity は未測定です。
