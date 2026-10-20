# Evidence API

[ADR 0022](decisions/0022-perceived-by.md) に従い、知覚・宛先・返信先を独立して保存します。
address_mode の actor 別省略規則は2026-10-20の追加仕様を適用します。

この API の呼出し元は Core です。登録前の機微分類も Core の責務です。
Memory は本文を命令として解釈せず、翻訳・空白除去・Unicode 正規化を行いません。

既存の `digital_souls_memory.api:create_app --factory` で起動します。
Evidence 操作には `MEMORY_DATABASE_URL`（`postgresql://` または
`postgresql+psycopg://`）と、別コマンドでの `alembic upgrade head` が必要です。
起動時や `/health` では DB 設定の検証・接続・DDL を実行しません。

## 入口と応答

| メソッド・パス | 操作 | 成功 |
| --- | --- | --- |
| `POST /sessions` | Session 登録 | 201、保存した Session |
| `GET /sessions/{session_id}` | Session 取得 | 200、Session |
| `POST /turns` | Turn 登録 | 201、保存した Turn |
| `GET /turns/{turn_id}` | Turn 取得 | 200、Turn |
| `POST /turns/{turn_id}/receipts` | 配送報告の追記 | 201、保存した報告 |
| `GET /turns/{turn_id}/receipts` | 全配送報告の取得 | 200、報告の配列 |
| `GET /characters/{character_id}/turns` | 知覚可能な Turn 取得 | 200、Turn の配列 |

存在しない Session・取得対象・Receipt の親は404、存在しない返信先は422、
同じ Session / Turn ID の登録は409、
必須項目の欠落・不正な値は422、DB 未設定・接続障害は503です。
重複登録で既存行を上書きしません。通常の PUT / PATCH / DELETE は提供せず405です。
DB の通常 UPDATE / DELETE もトリガーで拒否します。作者削除は今回の対象外です。

422 の本文は、入力 DTO の検証違反（必須項目、型、識別子形式、ラベル等）では
`{"detail": [...]}` です。配列の各要素は対象フィールドを示す `loc` 配列、
`type` 文字列、`msg` 文字列を持ちます。付随フィールドや説明文は入力によって変わります。
ドメイン検証（配列重複、知覚・宛先・返信先、Receipt 受付・範囲の不整合等）では
`{"detail": "..."}` の非空文字列です。
どちらも保存前に拒否し、書込件数を増やしません。

両一覧は全件を JSON 配列として逐次送信します。取得窓は有限で、ページングはありません。
接続・クエリ開始・最初の取得／変換での DB 障害は応答開始前に503へ変換します。
送信開始後の取得・変換・送信・資源終了の失敗は異常終了となり、正常な配列終端を送りません。
呼出し側は完全な JSON 配列の受信を成功条件としてください。
完了・失敗・切断時にカーソルと接続を閉じます。

キャラ別取得は、Turn の `perceived_by` に指定キャラが含まれる行だけを取得元で
限定します。actor・宛先・mode・返信先・Session から知覚を補完しません。
登録キャラ actor の本人知覚は登録時に要求し、自動追加しません。
同じ Session の前後の Turn や、人づての新しい observation も元の知覚を変更しません。
キャラ別取得の順序は `occurred_at`、`recorded_at`、`turn_id` の昇順です。
この入口は Core 向けで、キャラ ID は認証主体を表すものではありません。

## 入力

時刻はタイムゾーン付き ISO 8601。保存と復元では同じ瞬間を保持し、
応答時の UTC オフセット表記は入力と異なる場合があります。
HTTP の識別子（Session の `session_id`、Turn の `turn_id`・
`session_id`・`perceived_by[]`・`addressees[]`・`reply_to_turn_id`）は
ASCII 英数字・`_`・`-` からなる1文字以上の文字列です。
この形式を満たさない値（`/`、空白、非ASCII文字等）は保存前に422で拒否し、
置換・切詰めは行いません。受理した ID は上記の取得・Receipt操作・キャラ別取得で使用できます。
surface・actor・language・報告者・ラベル・分類器版は非空白の文字列です。
本文は空文字も含む文字列、`register` は文字列値の JSON オブジェクトです。

Session の入力・応答例（合成データ）:

```json
{
  "session_id": "session-1",
  "started_at": "2026-01-01T09:00:00Z",
  "ended_at": null,
  "surface": "synthetic-chat",
  "reports_delivery": false
}
```

`ended_at` は省略可能で null になります。接続窓口ごとに Core が ID を渡します。
セッション終了の更新操作はありません。
`reports_delivery` は必須の厳密な bool です。省略・null・数値0/1・文字列true/falseを
拒否し、登録後に変更できません。テキスト窓口は false を宣言します。
旧 `participants` 付き Session、旧 `audience` 付き Turn は422です。

Turn の入力例（合成データ）:

```json
{
  "turn_id": "turn-1",
  "session_id": "session-1",
  "surface": "synthetic-chat",
  "actor": "char-a",
  "kind": "utterance",
  "text": "こんにちは🙂",
  "perceived_by": ["char-a", "char-b"],
  "addressees": ["char-b"],
  "address_mode": "direct",
  "reply_to_turn_id": null,
  "language": "ja",
  "register": {"first_person": "私", "politeness": "敬語"},
  "sensitivity": "synthetic-sensitive",
  "classifier_version": "synthetic-v1",
  "occurred_at": "2026-01-01T10:00:00Z"
}
```

`kind` は `utterance` / `observation` / `thought`。自己発話・AI 同士・未反応の視聴者コメントを
除外しません。ゲーム・日記等は `observation` とその `surface` を渡します。
`perceived_by`・`addressees` は必須の文字列配列です。空配列を許可し、
欠落・null・不正型・非文字列要素・重複・空白のみを拒否します。
`perceived_by` は Issue #7 の CoreIdentity または Temperament が登録された
キャラ ID だけを受け付けます。未登録 ID はキャラとして扱わず、見た目で推測しません。
登録キャラ actor は本人を `perceived_by` に含める必要があります。
Identity 照会障害は503となり、利用者として処理しません。
`addressees` は利用者 ID も受け付け、順序を保持します。

| actor | address_mode の省略・null | 明示値 |
| --- | --- | --- |
| 登録キャラ | 422・書込0件 | direct / broadcast / self |
| 利用者（登録キャラ以外） | addressees が空なら broadcast で保存。非空なら422 | 同左の3値 |

direct は宛先1件以上、broadcast / self は空宛先だけを許可します。
未知値・空文字・不正型を補完せず422にします。省略・null を補った保存値・応答は
通常の broadcast で、補完フラグはありません。
`reply_to_turn_id` は省略・null なら返信先なし、指定時は単一の既存 Turn ID が必須です。
不存在・複数指定は422で、返信先から知覚を拡張しません。
`register` の固定キーは要求しません。
`sensitivity` と `classifier_version` は補完せず、欠落・null・空文字・空白のみ・
文字列以外を拒否します。ラベル体系は呼出し側が決めます。
応答にはサービスの時計が生成した `recorded_at` が加わります。
`occurred_at` は呼出し側の発生時刻として独立して保存します。

observation の例（上記 Turn の kind・本文等を変更して登録）:

```json
{"kind": "observation", "actor": "viewer", "text": "キャラが入室した",
 "perceived_by": ["char-a"], "addressees": [], "address_mode": "broadcast"}
```

thought の例（同様に他の必須属性も付けて登録）:

```json
{"kind": "thought", "actor": "char-a", "text": "心の中の言葉",
 "perceived_by": ["char-a"], "addressees": [], "address_mode": "broadcast"}
```

thought は登録キャラ actor と厳密な `[actor]` の知覚だけを許可します。
利用者 actor・空・本人欠落・他キャラ混在を422で拒否し、専用 mode は設けません。
self は声に出す独り言で、利用者・キャラとも指定した他キャラの知覚を保持します。
例えば `perceived_by=["char-a","char-b"]` は両者の一覧に現れます。

## 配送報告

```json
{
  "status": "partial",
  "spoken_until": 2,
  "reported_by": "synthetic-window",
  "reported_at": "2026-01-01T10:00:01Z"
}
```

`spoken_until = n` は保存した本文の先頭から Unicode コードポイントで数えた
半開区間 `[0, n)` の終端です。バイト数、書記素数、音声時間ではありません。
結合文字は個別に数えます。上記の「こんにちは🙂」の本文長は6です。

| status | 本文長 L に対する条件 |
| --- | --- |
| `full` | `n = L` |
| `partial` | `0 < n < L` |
| `none` | `n = 0` |

真偽値・小数・文字列を整数へ変換せず拒否します。負数・範囲外・status 不整合も422です。
空本文は `full: 0` または `none: 0` のみ成立します。
応答にはパスの `turn_id` も含めます。
reports_delivery=false の Session の Turn と kind=thought の Turn は Receipt を
422・書込0件で拒否します。DB の直接 INSERT でも拒否します。
false の Turn は登録時点で全文配達扱い、thought は本人の記憶にだけ使い Receipt を待ちません。
許可された Turn の報告なしは空配列であり、配送状況不明です。自動で full を生成しません。
同じ Turn へ none → partial → full → partial と追加しても、過去の報告と生成全文を
すべて保持します。報告一覧は追記順で返し、最新の報告への集約や上書きを行いません。

## アプリケーションからの再利用

`evidence.ports.EvidenceStore` が Session / Turn の `add_*`・`get_*`、
Receipt の `add_receipt`・`list_receipts` と `list_perceived_turns(character_id)` を定義します。
参照先・キャラ登録照会・mode 確定・Receipt 適格性と範囲検証・登録時計は
`EvidenceService` が担います。`CharacterRegistry` は Evidence 所有の読取ポートです。
PostgreSQL アダプタは INSERT のみを行い、DB 制約とトリガーも適用されます。
操作ごとに transaction と接続を閉じ、失敗した transaction を次の操作へ持ち越しません。
両一覧の戻り値は context manager が所有する iterator です。直接利用時も必ず
`with store.list_receipts(turn_id) as values:`（または `list_perceived_turns`）の範囲内で
消費してください。全件消費・途中終了・例外のいずれでも管理範囲を出ると資源を解放します。
Receipt の親不存在を404にする確認はサービスが担い、ストアへの直接照会は空結果です。
テストでは `create_app(store=..., clock=..., identity_store=...)` で fake と時計を注入できます。

## 合成データの migration

0004 は0003の後に直列適用します。旧 Session は reports_delivery=true、
旧 Turn は空宛先・broadcast・返信先なしで埋め、宛先や返信先を推測しません。
旧 audience が非空ならその配列、空なら旧 Session.participants から明示的合成対応表の
キャラだけを順序どおり選び、未収録のキャラ actor 本人を末尾に一度追加します。
対応表は migration 内の char-a / char-b / char-c（キャラ）、viewer（利用者）です。
未知 ID または未登録キャラがあれば停止し、トランザクションで変更を取り消します。

downgrade は perceived_by を旧 audience にコピーし、全 participants を空にします。
元の在席一覧・旧配列中の利用者・空配列の表現差・宛先・mode・返信先・配達宣言は
復元できません。thought が1件でもあれば変更前に拒否し、部分適用しません。
一時的な変更保護解除は migration トランザクション内だけで、通常経路には公開しません。
合成データ専用の支援であり、運用 DB・実会話の移行には使いません。
本文切出し・Receipt 未着の保留と10分のみなし確定・状態保存は #10 の担当です。
