# 常駐コンテキスト・短期状態・Episode API

Core が Session 登録後、開始時に `POST /characters/{character_id}/resident` を呼びます。
入力は必須の `session_id` と省略可能な非空白 `query` です。追加フィールドは拒否します。
CoreIdentity、セッション開始時に選択・保存した Temperament と PersonalityProfile の記述、
CharacterProfile、承認済みの逐語 VoiceExemplar、空の `motivations` を返します。
性格数値を返さず、人格生成・内省・権限変更を起動しません。
保存済み null の人格版もターン想起 API と共有し、新版で補完しません。
Voice は選択した性格版のものに限定し、query があれば既存 lexical 関数で降順評価、
同点と query 省略時は exemplar_id 昇順です。query をモデルへ送りません。

## Profile 設定

`PUT /characters/{character_id}/profile` は `token_limit`（非負整数）と `entries` を受けます。
未設定は上限0・空配列。entry_id は設定内で一意です。

```json
{"token_limit":10000,"entries":[
  {"entry_id":"profile-1","kind":"turn","turn_id":"turn-1","start":0,"end":3},
  {"entry_id":"profile-2","kind":"memory","owner_id":"char-b","memory_id":"memory-1"}
]}
```

原文区間は Unicode コードポイントの半開区間で、現在利用可能な原文内に限定します。
派生物参照は保存済み索引から読み、公開範囲・支持出典・全生成入力の依存を検査します。
自由本文・未知フィールド・重複ID・不正出典は422です。
取得時も出典を再評価し、Receipt 訂正等で利用不能になった entry は設定を保持して返却から除きます。
順序は設定順です。本文を切断せず、5件制限や常駐全体の固定予算は設けません。

計数対象は返却 entries 配列の JSON 全体（出典参照・本文・話者・原言語を含む）です。
Python `json.dumps` の `ensure_ascii=False, sort_keys=True, separators=(",", ":")` を使い、
既存 TokenCounter で数えます。空配列は0です。明示的に非機微と設定したラベルだけを
CloudInput にし、それ以外は LocalOnly として計数します。上限ちょうどは受理し、超過は422、
計数・保存依存失敗は503です。失敗した置換は旧設定を保持します。

## 提供記録

応答と同じ返却集合から kind=resident の ProvisionRecord を作り、commit 後に成功します。
返した ProfileEntry ID、memory ID、原文 span、Voice ID、CoreIdentity 版、気質・性格版、
要求・窓口・提供日時を保存します。スコア参照は kind と identifier を持ち、
適用しない項目は null、Voice の lexical も query 省略時は null です。
処理時間はユースケース開始から保存呼出し直前までのミリ秒です。
保存 transaction 内で Profile 設定版・索引・出典・人格選択・Voice・CoreIdentity を再検査し、
不一致・保存失敗は503、提供ID衝突は409です。既存記録を上書きしません。
提供だけでは last_reinforced_at・source_turn_ids・独立根拠数を変更しません。

## State と Episode

`GET /characters/{character_id}/sessions/{session_id}/state?surface=...` は `{"state":...}`、
`GET /characters/{character_id}/sessions/{session_id}/episodes?surface=...` は `{"episodes":[...]}` を返します。
State はキャラ・セッション・窓口で分離し、初期 TTL は30分（正の state_ttl で変更可）です。
期限到達または終了報告後は null。同じ入力の走査で期限を延長せず、次 Session へ持ち越しません。
Episode は State の期限・終了後破棄を適用せず、turn_span と原文を復元した source_spans、
時間範囲・生成モデル・プロンプト版・生成日時・入力依存を返します。
追加 Turn だけを理由に利用可能な既存 Episode を無効化しません。
両取得は現在の出典範囲を再検査し、訂正後の範囲外本文を含む旧成果物を返しません。

DB 設定時は0008を事前適用します。起動時 DDL は行いません。
create_app に ContextGateway と ContextStore を解決すると lifespan が worker を起動します。
Gateway なしでも保存済み成果物を取得できます。暗黙の fake 保存へ切り替えません。
Turn・Receipt・終了記録の永続通知から裏処理を発見し、登録 HTTP はモデル完了を待ちません。
機微・未知・混合入力は LocalOnly、全件が明示的非機微の場合のみ CloudInput を使います。
原文・原言語を保持し、宛先・返信先から知覚を補完しません。
配達宣言 false は全文、true のキャラ発話は他キャラに対して Receipt 範囲のみ、
未着は recorded_at から既定10分（delivery_timeout で変更可）保留してみなし全文にします。
本人・thought・observation は共通の配達判定に従います。

state と episodes は独立した claim・lease・処理状態を持ちます。
モデル呼出し中に DB transaction を保持せず、保存時に入力・終了・通知版・所有tokenを再検査します。
不正出力・ID衝突は failed / invalid_output、依存失敗は retry / dependency_unavailable、
入力変更は retry / input_changed とします。新バッチは全体を検証し、衝突時は旧バッチを保持します。
停止時は取得を止め、実行中呼出しを回収して claim を解放します。
