# 0018. ContextTrace を提供記録に置き換え、Memory が何を渡したかだけを記録する

- 日付：2026-10-20
- 状態：承認（0013 の ContextTrace を置き換える）

## 背景

0013 では、キャラの各発話に、参照した記憶・人格の版・プロンプトテンプレートの版・モデルを記録する ContextTrace を置くとしていた。0015 で応答生成は Core の責務になり、Memory は渡した記憶が実際にプロンプトに使われたかを知ることができない。

## 決定

- Memory は、常駐コンテキスト API とターン想起 API の応答ごとに提供記録（ProvisionRecord）を残す。
- 提供記録には、provision_id、種別（resident／turn）、character_id、session_id、surface、要求の内容（検索文または起点の turn_id）、返した派生記憶・原文区間・VerbatimAnchor・VoiceExemplar・Motivation の ID、Temperament と PersonalityProfile の版、各候補のスコア、処理時間、提供日時を持たせる。
- 渡した記憶が応答に使われたかどうか、プロンプトテンプレートの版、モデルは Core の責務とし、Memory は記録しない。
- 提供記録は新しさの起点（0012）を更新しない。

## 検討した案

- **キャラの発話 Turn に ContextTrace を必須で紐付ける（0013 当初の決定）**：使用の有無は Core 側の責務であり、Memory が保証できないため不採用。
- **提供したら新しさを再強化する**：よく検索される記憶ほど検索されやすくなる自己強化が起きるため不採用（0012）。

## 影響

- ChangeBudget の調整や監査は、提供記録・IdentityHistory・Core 側のログを突き合わせて行う。
- 削除フロー（G5）では、削除した Turn を参照する提供記録の ID 参照も失効させる。
