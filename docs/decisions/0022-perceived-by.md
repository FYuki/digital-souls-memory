# 0022. 知覚・宛先・返信先を分け、キャラが知覚した範囲だけを記憶の根拠にする

- 日付：2026-10-20
- 状態：承認
- 置換対象：[ADR 0006](0006-shared-ledger-and-perception.md) の知覚範囲（participants／audience）の定義のみ。共通台帳・コメント保存・経験の記録・セッションと話題分割の決定は同 ADR を維持する
- 関連：[ADR 0005](0005-record-all-generated-text.md)、[ADR 0019](0019-phase1-deletion.md)

## 背景

本サービスはキャラクターが知覚したものを記憶として管理する。Evidence はキャラを所有者として持たない共通台帳であり、生成した文は全文を Turn に記録する。
従来の「参加者または聞き手」による M6 の判定では、1人へのささやきを他の参加者も知覚してしまう。
知覚を perceived_by だけで表すと、今度は利用者を含む返答の相手と、どの発言への返答かが失われる。
関係性・約束・利用者ごとの事実の抽出には相手の情報が必要であり、宛先と知覚を分ける。

在席や視聴者の出入りは、リアルタイムの応答にも記憶の判定にも不要である。
配信の出入りはノイズが多く、グループのメンバー管理はチャット側がすでに持つため、記憶サービスでは管理しない。

## 決定

### Turn の主体・知覚・宛先・返信先

| 項目 | 意味と制約 |
| --- | --- |
| kind | utterance（声に出す発言）／observation（経験・観測）／thought（口に出さない内心） |
| actor | 発言・行為の主体。キャラ ID または利用者 ID |
| perceived_by | utterance では話された範囲を知覚したキャラ ID、observation では経験を知覚したキャラ ID、thought では actor 本人だけの文字列配列。キャラクターのみ。重複と空白のみの ID は不可。どのキャラも知覚しない場合は空配列可。actor がキャラなら本人を常に含む |
| addressees | 誰に向けた発言かを示す利用者 ID・キャラ ID の文字列配列。重複と空白のみの ID は不可 |
| address_mode | 必須。direct（特定の相手宛て）／broadcast（全体宛て）／self（独り言）。direct は addressees が1件以上、broadcast と self は空配列のみ |
| reply_to_turn_id | 任意・単一。存在する Turn を参照し、どの発言への返答かを示す |

Core は登録時にこれらを確定する。Turn は追記専用（E1）であり、宛先・知覚・返信先を登録後に変更しない。
reply_to_turn_id は会話の対応づけと並行会話での取り違え防止に使い、知覚を付与する根拠にはしない。
Relationship・Commitment・利用者ごとの Fact の相手は addressees と actor から決める。
声に出す独り言は kind=utterance / address_mode=self とする。self の発言は Relationship と Commitment の抽出対象から外す。
キャラ本人の self は、Phase 2 の内省で Belief・Motivation の根拠として使う。
self と broadcast の区別は抽出のためであり、知覚範囲を変えるためではない。self もその場の他キャラが通常の規則で知覚し、Core が perceived_by に含める。利用者の self でも知覚したキャラを含める。
addressees と address_mode は M6 の知覚判定に使わず、「誰に向けたか」と「誰が知覚したか」を独立して扱う。

### 知覚の規則と M6

以下は発言の知覚規則であり、口に出さない thought は後述の本人限定規則に従う。

- 聞き手が指定された発言（ささやきなど）は、指定されたキャラだけが知覚する。
- 聞き手の指定がない発言は、その場にいるキャラが知覚する。
- 発言者がキャラなら、本人は自分の発言を常に知覚し、perceived_by に含める。
- 利用者だけに向けたささやきは、addressees はその利用者、perceived_by は発言者本人のみとなる。
- 聞き手の指定と宛先は別の入力である。特定の利用者への返答でも、ささやきでなければその場のキャラ全員が知覚する。
- Core が窓口の情報から Turn ごとに perceived_by を確定し、Memory は在席や Session から補完しない。窓口をまたぐ利用者 ID の同一人物への対応づけも Core の責務とする。

M6 は「キャラ別の派生記憶・内省は、そのキャラが perceived_by に含まれる Turn（発言者本人は常に含む）からだけ作る。
発言者以外のキャラは、Session の報告宣言と DeliveryReceipt から定まる配達範囲だけを根拠にする」とする。
本人の自己発話も抽出・検索の対象（M3）とし、自己生成物の抽出はゲート M5 を通す。
途中参加後に過去の Turn の知覚範囲を広げない。
人づての話は「A が B に話した」という新しい Turn として B が知覚し、元の Turn の perceived_by は変えない。

### 口に出さない内心

口に出さない内心は address_mode ではなく kind=thought で表す。actor はキャラに限り、perceived_by は actor 本人だけとし、本人の記憶にだけ使う。
内心を実際に生成・登録するかは Core の判断とし、Memory は受け取れる契約を用意する。
thought は配達を伴わず、Session の報告宣言にかかわらず DeliveryReceipt は不要で、届いた Receipt は拒否する（HTTP 422）。
これは ADR 0005 の「生成したが話されなかった部分は本人の思考として存在したとみなす」と同じ考え方に沿う。
address_mode=self は声に出した独り言であり、self だけで内心を表現しない。thought の知覚と配達は kind で制約し、address_mode では変更しない。

### Session の配達報告宣言と、生成全文・話された範囲

Session に reports_delivery（必須の真偽値）を持たせ、窓口が Session 登録時に Core 経由で「届いた範囲を報告するか」を宣言する。
Session は追記専用であり、この値を登録後に変更しない。
テキストの窓口（グループチャットなど）は false を宣言し、DeliveryReceipt を送らない。

生成した全文を Turn.text に保存する（ADR 0005）。utterance の perceived_by は「話された範囲を知覚した者」を表し、話されなかった部分を知覚できるのは発言者本人だけとする。
発言者本人は生成全文を根拠にできる。発言者以外のキャラに使える本文は次のとおりとする。

- **reports_delivery=false**：Turn 登録時点で全文が届いたものとして確定し、保留しない。この Session の Turn への DeliveryReceipt は宣言と矛盾するため拒否する（HTTP 422）。
- **reports_delivery=true のキャラ発話**：相手に届いた範囲を DeliveryReceipt（full／partial／none、spoken_until）で追記する。全文／text の先頭から spoken_until まで／根拠本文なし、をそれぞれ使う。未着中は他キャラの抽出を保留し、Turn.recorded_at から既定10分（設定可）を過ぎても未着なら全文が届いたとみなす。
- 通常の利用者発話・observation は Receipt 待ちにしない。thought は上記の本人限定規則を優先し、配達報告の対象にしない。

報告不要の宣言による即時全文確定・保留中・Receipt に基づく確定・全文のみなし確定を区別して記録できるようにする。
みなし確定は実際の配達確認と同一視しない。reports_delivery=false の宣言による確定とも混同しない。Turn 本文や perceived_by は書き換えない。

### 在席を持たない共通台帳

共通台帳、視聴者コメントの全件記録と反応したものだけの抽出、会話以外の経験と surface、接続単位の Session と裏処理による話題分割は [ADR 0006](0006-shared-ledger-and-perception.md) に従う。

- Session.participants と Turn.audience を廃止する。Session は reports_delivery を追加し、終了の記録は別レコードへの追記とする。
- 在席・視聴者の出入りは配信サービスとグループチャットサービスの責務とする。Memory は在席ログや視聴者一覧を持たない。
- キャラにとって意味のある出入り（常連の来訪、新しいメンバーの加入など）だけ、窓口側が Core 経由で kind=observation の Turn として登録する（例：「Bが入ってきた」）。キャラが反応したときやセッション終了時の登録でよく、リアルタイムである必要はない。
- 取得したチャンネル・部屋などの場所属性は持たない。開示は Fact／Preference のキャラ間公開範囲の方向で記憶の内容ごとに制御し、取得場所や現在の場所では絞り込まない。知覚と開示は別の判断とする。

### 利用者を指定した削除の対象範囲

利用者を指定した削除要求では、actor がその利用者である Turn と、addressees にその利用者を含む Turn の和集合を連鎖処理の対象とする。
Phase 1 は ADR 0019 に従い作者だけが利用者を指定して要求し、source_turn_ids と ProvisionRecord の直接参照をたどる。
利用者本人からの要求窓口と G5 の完全な連鎖は Phase 3 とする。在席ログの削除は各サービスの責務とする。

## 登録例

| 状況 | kind | actor | addressees | address_mode | perceived_by | reply_to_turn_id |
| --- | --- | --- | --- | --- | --- | --- |
| 1対1で利用者 A に返答 | utterance | X | [A] | direct | [X] | A の発言 |
| グループで利用者 A に返答（X・Y が聞く） | utterance | X | [A] | direct | [X, Y] | A の発言 |
| キャラ X が Y にささやく | utterance | X | [Y] | direct | [X, Y] | 任意 |
| キャラ X が利用者 A にささやく | utterance | X | [A] | direct | [X] | 任意 |
| キャラ X の独り言（X・Y が聞く） | utterance | X | [] | self | [X, Y] | なし |
| 利用者 A の独り言（X・Y が聞く） | utterance | A | [] | self | [X, Y] | なし |
| キャラ X が全体へ話す（X・Y が聞く） | utterance | X | [] | broadcast | [X, Y] | 任意 |

## 検討した案

- **participants／audience による知覚判定**：参加者というだけでささやきまで知覚してしまうため採用しない。
- **perceived_by だけを保持する**：利用者を含む返答の相手と返信先が失われるため、addressees・address_mode・reply_to_turn_id を分けて持つ。
- **空の addressees だけで全体宛てと独り言を表す**：Relationship・Commitment の抽出可否を判別できないため採用しない。broadcast に特定の相手を併記する方式も採用しない。
- **Memory が在席履歴を再構成する／メンバー変更ごとに Session を分割する**：サービス間の責務と重複し、出入りのノイズや接続履歴の断片化を招くため採用しない。
- **全窓口で10分保留する**：テキスト窓口は配達範囲を報告しないため、届いた全文の抽出を毎回10分遅らせるだけになる。報告しない Session は即時全文確定とする。
- **報告する Session でも Receipt 未着を直ちに全文とみなす／無期限に保留する**：前者は未発話部分を他キャラの根拠にし、後者は抽出を永久に止めるため、期限付き保留とする。
- **self を本人だけの知覚にする／内心も address_mode で表す**：声に出す独り言と口に出さない内心を混同するため採用しない。知覚は前者では通常の規則、後者では kind=thought による本人限定とする。

## 影響

Core は Session 登録時に配達報告宣言を、Turn 登録時に kind・主体・宛先・知覚・返信先を渡す必要がある。Memory はこれらを在席から推測しない。
キャラ別の抽出・想起・資料登録・内省は同じ M6 と発言者別の本文範囲を守る。
共通台帳の全文保存と、キャラ別に根拠として使える範囲を区別する。
取得順序は occurred_at、recorded_at、turn_id のままとする。
