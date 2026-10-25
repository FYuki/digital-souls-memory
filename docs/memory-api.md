# 派生記憶と非同期抽出

Core が既存の Session・Turn・Receipt API に登録すると、0006 のトリガーが同じ
トランザクションで処理通知を保存します。HTTP 登録はモデルの終了を待ちません。
原文・原言語・知覚・宛先・返信先は Evidence に保持し、Memory は更新しません。

## 構成

デプロイ前に `alembic upgrade head` を実行します。アプリ起動は DDL を実行しません。
`create_app` に次を注入します。

| 引数 | 意味・省略時 |
| --- | --- |
| `memory_store` | 派生物・処理状態の保存ポート。省略時は `MEMORY_DATABASE_URL` の PostgreSQL |
| `extraction_gateway` | Memory 専用の `ExtractionGateway`。設定すると lifespan 内で worker を起動。省略時は通知を保持し、抽出完了とは扱わない |
| `delivery_timeout` | `Turn.recorded_at` 起点の待機期限。既定10分、非負の `timedelta` |
| `non_sensitive_labels` | Core のラベルのうち、原文のクラウド送信を許す集合。既定は空集合 |
| `conversation_surfaces` | 通常の利用者発言を抽出する窓口の集合。既定は空集合 |
| `comment_surfaces` | 視聴者コメントを扱う窓口の集合。既定は空集合。通常会話集合との重複は拒否 |
| `store`, `identity_store`, `clock` | 既存の Evidence・キャラ照会・時計の注入口。合成試験では fake を使用 |

非キャラ utterance で窓口の分類が未設定なら採用を保留します。コメント窓口は、対象キャラ
本人がそのコメントを `reply_to_turn_id` で参照した登録済み Turn がある場合だけ処理します。
台帳のコメントを削除しません。キャラ自身の発話・observation はこのコメント条件と区別します。

モデル SDK と実モデル実行は含みません。Gateway アダプタは必ず呼出し時間を制限し、
`extract_local(LocalOnly)` と `extract_cloud(CloudInput)` を実装します。
Identity・Voice の Gateway に操作を追加する必要はありません。

## Core 向け API

| 操作 | 結果 |
| --- | --- |
| `GET /characters/{character_id}/memories` | 現在有効な派生物の配列。出典・生成情報・時間区間・型別属性・公開範囲・Epistemics・IdentityLinks を含む |
| `GET /characters/{character_id}/extractions/{turn_id}` | 配達判断・処理状態・期限・Receipt の追記版・失敗分類。未処理は404 |
| `POST /turns/{turn_id}/extractions` | 原文からの再抽出要求を永続化し202を返す。不存在 Turn は404 |

取得場所や現在の場所で記憶を絞りません。キャラの所有権と出典の知覚・現在の配達範囲を
検査します。`shared_with` は内容ごとのキャラ間公開範囲で、知覚を新たに付与しません。
原文取得は既存の Evidence API を使います。これはターン想起・検索 API ではありません。

## 抽出単位と支持出典

入力は新しい Turn と利用可能な直接の返信先だけです。祖先や無関係な最新発言は追加しません。
返信先なし・不存在・知覚範囲外・他キャラの thought・未着保留・配達本文なしでは
新しい Turn だけを使います。新しい Turn 自体が利用できなければ返信先だけの抽出は行いません。
各 Turn の Session・Receipt で本文を制限し、入力に機微な返信先があれば LocalOnly に送ります。
モデルは実際に内容を支える Turn だけを source_turn_ids に指定します。文脈として渡したことを
理由に出典を補完しません。往復全体の入力状態を署名に含め、文脈だけの返信先の配達変更も
再処理・取得・保存投影に反映します。

## 配達と補正

`delivery_basis` は以下を区別します。

| 値 | 使用本文 |
| --- | --- |
| `declared_full` | Session の `reports_delivery=false` による即時全文 |
| `pending` | 報告する Session のキャラ utterance を他キャラが待機中 |
| `receipt_confirmed` | 最新に追記された Receipt の Unicode コードポイント接頭辞。none は空 |
| `assumed_full` | 未着のまま recorded_at + delivery_timeout に到達した全文 |
| `original` | 本人の生成全文・thought、通常の利用者発言・observation。実 Receipt の確認を意味しない |

本文判定は `EvidenceMaterials` が所有し、Voice と Memory が共有します。本人の全文は
Receipt 変更で取り下げません。thought は本人に限定します。Receipt は reported_at の大小や
本文の長さで並べ替えず、追記順で評価します。

同じ保存先で再起動すると、処理の期限を recorded_at から再評価します。期限設定の変更は
保存済みのみなし確定結果にも適用し、延長後の期限前なら保留へ戻して旧結果を取り下げます。
遅着 Receipt・期限設定変更時は、古い候補の寄与をモデル呼出し前に取り下げます。
有効な候補と元の保存値から再計算し、生成本文だけでなく再強化・出典追加・旧 Fact の
valid_to・relation の寄与も補正します。再抽出失敗でも不適合な結果を再公開しません。
取得 API でも現在の使用本文と抽出時の本文を比較し、worker の補正前の旧結果を除外します。
作者削除の再計算で残った baseline も、保存した全生成入力の使用範囲を現在の配達範囲と
照合します。範囲が不足すれば取得から除外し、worker の限定再構築で失効を保存します。
Receipt の版だけが変わり、使用範囲が引き続き利用可能なら保持します。

## 採否と時間更新

モデルは `DerivedMemory` の候補の tuple、または候補と relation を持つ `ExtractionProposal`
を返します。任意操作・ポリシー変更・人格書込を出力するインターフェースはありません。
候補に含まれる出典は全て実際の入力集合に属する必要があり、一件でも違えば処理全体を拒否します。
宛先は actor・addressees から検証し、reply_to_turn_id は実在参照のまま入力へ渡します。
self の Relationship・Commitment は採用せず、他の型は採否ゲートを通します。

M5 の開始値は、retrieval・source・temporal・corroboration・(1−contradiction)・self_assessment
の等重み平均が0.5以上です。自己評価だけでは採用できません。これは工学的な設計値で、
モデル品質が実証された閾値ではありません。
独立根拠数は、支持出典の直接の返信関係でまとめた機会と Turn.actor の二部グラフの
最大マッチングで数え、最小値は1です。同じ機会の異なる話者、別機会の同じ話者は
それぞれ1件となります。抽出・再強化・rebuild は同じ計算関数を使い、Trust Tier と
確信度の他特徴を変更しません。

現在有効な同一 subject・predicate・object の Fact の再表明は出典寄与として記録します。
閉鎖済み・superseded・未来のFactは再表明先にしません。
異なるSPOの明示extendsは従来の時間効果を保持します。
再強化時刻は残存する寄与のoccurred_atの最大値です。処理時刻は使いません。
明示の updates は旧 Fact の valid_to を閉じ、別 ID の新版を追加します。
更新候補の Trust Tier が旧 Fact より高ければ拒否します。source_type の変更によっても
昇格を許可しません。同等以下の Tier の通常更新は受理します。
再抽出で ID が変わった場合、同じ所有者・抽出元に属する同等の現在候補が一意にあるときだけ
履歴 ID への relation を解決し直します。別内容・別抽出元・曖昧な候補へは接続しません。
取得は一度の評価時刻で valid_from を含み、valid_to と superseded_at を含まない期間を使います。
relation のない別 object の Fact は両立し、明示の derives は元 Fact を終了・再強化しません。
明示の contradicts は原文に反する派生側を superseded にします。Commitment と Voice の
VerbatimAnchor は再強化しません。衝突した ID の別内容を上書きしません。

## 永続性と失敗

[ADR 0024](decisions/0024-restatement-as-reinforcement.md) に従い、再表明は
既存Factへの出典寄与として保存します。受理時にキャラのロック下で保存射影を検索し、
正確なSPOと現在の有効期間を確認して一度だけ参照先を決定します。
再表明から別のDerivedMemoryを作らず、同じ抽出内のrelation参照は保存前に書き換えます。
本文中の文字列は変更しません。寄与はキャラ・Fact・抽出元Turn・出典Turn・occurred_atを持ち、
同じFact・抽出元・出典の組を重複保存しません。relationの作用も抽出元に所属します。

全体投影・部分再構築・作者削除後の再計算は、保存された寄与を読み、
本体出典との和集合・残存寄与の最大イベント時刻・ADR 0023の独立根拠数を求めます。
ID・valid_from・出自・Trust Tierを寄与で変更しません。
再抽出・requeue・retry・作者削除では、旧寄与・relationと時間効果を取り消し、
残存データから投影と索引を更新します。取り消しと原因の操作は同一トランザクションです。
複数の抽出が支えるrelationは、一つを取り消しても残る作用を保持します。
作者削除で本体出典がなくなっても、寄与から本文を再計算できれば同じIDで存続し、
再計算できなければ取り下げます。寄与を他のFactへ移しません。

同じIDの再計算済みbaselineと元候補がある場合、本文と保護属性はbaselineが所有します。
本体出典と集約された出典は分けて保存し、取り消し後に古い寄与をbaselineから復活させません。
一覧は要求ごとのスナップショットを読み、想起は保存射影と索引を読みます。
通常経路には代表選定・代表解決・吸収連鎖の展開を残しません。
0014だけが0013の代表表・同一SPOの適用済みextendsを寄与へ移行し、
多段の旧ID参照を書き換えて代表表を削除します。
出典寄与が一件でもあるdowngradeは拒否します。寄与から元の吸収済みIDや候補を
復元できず、削除すると出典と参照を失うためです。

派生物、正規化した出典、relation、処理状態、抽出候補を PostgreSQL に保存します。
必須出典・存在する原文参照・知覚・Commitment の原文参照は DB 制約でも検査します。
キャラ単位のトランザクションロックで原子的に投影し、モデル呼出し中はトランザクションを保持しません。
窓口単位で処理を取得し、複数 worker の競合は5分の lease と処理 token で制御します。
異常終了後は lease 満了時に再取得できます。実行中に入力版が変われば古い結果を適用しません。

処理状態は `pending`・`running`・`done`・`retry`・`failed` です。一時依存障害は
1秒後から再試行し、不正出力・ID 衝突は failed として明示再投入を待ちます。
失敗情報には本文や例外の詳細を保存せず、分類だけを保存します。

非機微ラベルだけの入力は `PublicTurn` から `CloudInput` を構築します。未知ラベル・機微を含む
混在入力は `LocalOnly` へ渡し、LocalTurn を CloudInput に再包装すると拒否します。
既存記憶の本文はモデル入力へ含めず、候補との構造化属性の照合は Memory 内で行います。
