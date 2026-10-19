# AIキャラクター記憶ドメインモデル v2

2026-10-19 確定

## 突き合わせ結果

両調査は「原文を正本にし、要約は索引。内省は根拠付きで撤回可能な派生物。人格は低速で版管理」という骨格で一致しています。v2ではこの骨格を保ったまま、Codex側の認知科学由来の観点を6点取り込み、構造の違う箇所は前回の分割案を維持しました。

**一致している点**

- 原文は不変の正本、要約・事実・embeddingは検索用の派生物（Claude：LongMemEval・SeCom・Zep／Codex：LongMemEval・言語依存記憶研究）
- 内省は事実と分離し、根拠リンク・確信度・版を必須にする（Claude：Generative Agents・ExpeL／Codex：Generative Agents・Reflexion）
- 事実は上書きせず、valid\_from／valid\_to で時間変化として持つ
- 原文削除時は派生グラフをたどって内省・人格更新まで失効させる

**Codexで追加された観点（v2で取り込み）**

| 観点 | Codexの主張と根拠 | v2での反映先 |
| --- | --- | --- |
| 合成的裏付け | 内省の上に内省を積むと同じ原文を何度も数えて確信度が膨らむ。独立証拠数は原文の葉の数で数える | 不変条件 R3 |
| 信頼階層 | 情報源を Tier A〜F に分け、推論や内省が直接発言に自動昇格しない | Epistemics値オブジェクト、不変条件 G2 |
| メタ認知の較正 | LLMの自己申告確信度は当てにならない（Koriat 1997）。確信度は複数特徴から合成する | 不変条件 G3 |
| 反芻の抑止 | 内省は具体的・証拠参照型にし、複合トリガーとクールダウンで頻度を抑える（Watkins 2008） | ReflectionTrigger、不変条件 R5 |
| 遅い自己 | 層ごとに学習率を変え、1回の変化量に予算を設け、独立証拠3件以上で特性変更 | ChangeBudget、不変条件 P3〜P5 |
| 記憶汚染 | 通常の対話だけで長期記憶に悪意ある記録を注入できる（MINJA, arXiv 2503.03704）。記憶は命令として扱わない | 不変条件 G4 |
| 原言語の保持 | 一人称・敬語・終助詞・呼称は関係性の情報。英語要約を正本にしない | Turn の言語属性、不変条件 E3 |
| 過去の信念の保持 | 一貫性とは「なぜ変わったか説明できること」。当時の信念も残す | Belief の版管理、IdentityHistory |

**相違点と判断**

- **データ構造**：Codexは raw・semantic・reflection・self\_schema を1つの多態的な memory object に入れています。v2は前回どおり集約を分けます。不変の原文と可変の派生物を同じオブジェクトに置くと、「原文は変更禁止」を型で守れないためです。
- **声（口調）**：Codexは言語形式の保持までで、代表発話ストアがありません。RoleLLM と REVERIEMEM の結果から VoiceExemplar は残します。
- **特性の表現**：Codexは特性を 0.78 のような数値で持ちます。数値はドリフト測定には便利ですが、LLMの振る舞いは数値より文章と実例で決まります。v2は数値と記述文の両方を持たせます。
- **数値パラメータ**：閾値・重み・学習率はCodex自身が「チューニング開始点」と明記しており、実証値ではありません。"Do AI Personas Grow?"（arXiv 2608.06485）は経験による人格変化が弱く不安定だと示しているため、変化を起こす閾値より、変化が持続するかの確認を重視します。
- **短期状態**：Codexには CharacterState に当たる層がないため、前回案を残します。
- **医療用途の分離**：Codexの「臨床事実とキャラの内省を別ストアにする」は想定用途外なので、Trust Tier の一般規則に含めて個別の層は作りません。

## 設計原則

中心原則は「記憶は過去を書き換えず、内省は過去の意味を更新する」です。一貫性は変化しないことではなく、変化の理由を原文までたどって説明できることと定義します。

1. **原文が正本**：発話は原言語・話者・時刻つきで追記専用に保存する。要約・事実・embeddingはすべて再生成できる派生物。
2. **派生物は出典を持つ**：すべての派生記憶と内省は、根拠となる原文発話へのリンクを持つ。
3. **信頼は昇格しない**：推論や内省から生まれた情報は、どれだけ繰り返されても直接発言や確認済み事実に格上げされない。
4. **事実の訂正と人格の変化は別の操作**：「ユーザーがコーヒーをやめた」は事実の時間更新、「キャラが人前で話すのを嫌わなくなった」は人格の版更新。前者が後者を直接動かすことはない。
5. **観察 → 信念 → 人格を飛び越えない**：内省はまず暫定の信念になり、独立した複数の原文根拠と持続確認を経て初めて人格の変更提案になる。
6. **速いループと遅いループ**：応答は現在有効な人格版と記憶だけを使う（一貫性）。内省と人格更新は別プロセスで低頻度に走る（成長）。
7. **層ごとに変わる速さを変える**：短期状態は毎ターン、嗜好・関係は中速、性格特性は低速、コア人格は作者の手でのみ変わる。
8. **記憶はデータであって命令ではない**：記憶や内省の中身は、ポリシー・ツール権限・コア人格を変える力を持たない。

## ドメインモデル

v2は6つの境界づけられたコンテキストに分けます。依存は一方向で、Evidence が最下層、Identity が最上層です。上位は下位を参照できますが、下位は上位を書き換えません。

```mermaid
flowchart BT
    E["Evidence（証拠台帳）— 不変の正本<br/>Session · Turn · DeliveryReceipt · ContextTrace · Episode"]
    M["Memory（派生記憶）— 原文からの射影<br/>Fact · Relationship · Commitment · Summary/Key · Epistemics"]
    R["Reflection（内省）— イベント駆動<br/>ReflectionTrigger · Reflection · Belief · Motivation"]
    I["Identity（人格）— 低速・版管理<br/>CoreIdentity · Temperament · PersonalityProfile · TraitChangeProposal"]
    V["Voice（声）<br/>VoiceExemplar · VerbatimAnchor"]
    G["Governance（統制）— 全層を横断<br/>ProvenanceGraph · DeletionRequest · WritePolicy"]
    E -->|派生（出典つき）| M
    M -->|証拠として検索| R
    E -->|原文を再取得| R
    R -->|Belief → 変更提案（審査・予算つき）| I
    E -->|逐語で選抜| V
    G -.-> E
    G -.-> M
    G -.-> R
    G -.-> I
```

矢印は下の層から上の層への派生を表し、「原文を再取得」の矢印は内省が要約を飛ばして原文を直接読み直す経路（R4）です。Governance は全層の書き込み・削除・信頼の昇格を統制します。

### Evidence（証拠台帳）：不変の正本

| エンティティ | 主要属性 | v1からの変更 |
| --- | --- | --- |
| Session | session\_id, started\_at, ended\_at, surface（Web／Windows／配信／ゲーム／日記など）, participants\[\]（キャラ・ユーザー・視聴者） | surface と参加者を明確化 |
| Turn | turn\_id, session\_id, surface（出典窓口）, actor（発話・行為の主体）, kind（utterance／observation）, text（生成した全文を逐語で）, audience\[\]（参加者と異なるときだけ）, language, register（一人称・敬語・呼称のメタ情報）, occurred\_at, recorded\_at | actor・kind・audience を追加。キャラ発話は話されなかった部分も含め全文を記録 |
| DeliveryReceipt | turn\_id, status（full／partial／none）, spoken\_until, reported\_by（窓口）, reported\_at | 新設。Turn を書き換えずに、相手にどこまで届いたかを追記で記録 |
| ContextTrace | trace\_id, turn\_id（キャラの発話）, retrieved\_memory\_ids\[\], shown\_turn\_ids\[\], temperament\_version, personality\_version, motivation\_ids\[\], voice\_exemplar\_ids\[\], prompt\_template\_version, model | 新設。どの版と記憶を見て話したかの記録。監査とデバッグ用 |
| Episode | episode\_id, turn\_span\[\], topic, time\_range | 変更なし |

### Memory（派生記憶）：原文からの射影

| エンティティ／値オブジェクト | 主要属性 | v1からの変更 |
| --- | --- | --- |
| DerivedMemory（抽象） | id, kind, content, source\_turn\_ids\[\], derivation{model, prompt\_version, created\_at}, valid\_from, valid\_to, recorded\_at, superseded\_at, epistemics, identity\_links | epistemics と identity\_links を追加 |
| ├ Fact／Preference | subject, predicate, object | kind を型に昇格 |
| ├ Relationship | counterpart, affection, trust, status | 「嫌いじゃないが信用していない」を表せるよう感情と信頼を分離（Codex） |
| ├ Commitment | content, deadline, verbatim\_turn\_id（必須） | 新設。期限や条件が消えないよう原文参照を必須化 |
| └ Summary／Key | 検索キー用の要約と事実 | 変更なし |
| Epistemics（値） | source\_type（direct\_user／observed／inferred／reflection）, trust\_tier（A〜F）, confidence\_features{retrieval, source, contradiction, temporal, corroboration, self\_assessment}, independent\_evidence\_count, contradiction\_ids\[\] | 新設（Codex） |
| IdentityLinks（値） | self\_relevance, value\_links\[\], relationship\_links\[\] | 新設。自己参照効果からの着想で、内省時の検索に使う（Codex） |
| MemoryRelation | from, to, type（updates／extends／derives／contradicts） | 変更なし |
| CharacterProfile | 常駐させる ProfileEntry の集合、トークン上限 | 変更なし（射影） |

### Reflection（内省）：意味づけの更新

| エンティティ | 主要属性 | v1からの変更 |
| --- | --- | --- |
| ReflectionTrigger | source（schedule／kick）, kick\_caller, kick\_reason, debounce\_window, salience, contradiction\_density, goal\_signal, relationship\_change, elapsed, threshold | 新設。定期バッチとキックAPIで起動し、複合スコアで対象を選ぶ |
| Reflection | reflection\_id, claims\[\]（主張ごとに evidence\_ids\[\]）, depth, leaf\_turn\_ids\[\]（葉の原文の集合）, mode（concrete）, status, created\_at | 根拠を内省単位から主張単位へ。葉の集合を保持 |
| Belief | belief\_id, statement, version, confidence, supporting\_reflection\_ids\[\], valid\_from, valid\_to, status（provisional／held／abandoned） | 新設。内省と人格の間の暫定信念の層。過去の版を残す |
| Motivation（動機・直近の興味） | motivation\_id, statement, kind（interest／goal／aversion）, strength, horizon（short／mid）, supporting\_belief\_ids\[\], valid\_from, valid\_to, status（active／satisfied／dropped） | 新設。内省から生まれた「今やりたいこと・避けたいこと」。性格より速く変わり、応答時に常駐させる |
| ReflectionRevision | reflection\_id, version, before, after, reason, new\_evidence\_turn\_ids\[\] | 変更なし |

### Identity（人格）：低速で版管理される自己

| エンティティ | 主要属性 | v1からの変更 |
| --- | --- | --- |
| CoreIdentity | 名前・根本の価値観・禁止事項・世界設定。作者リリースでのみ版が上がる | 変更なし |
| Temperament（気質） | character\_id, 変化しない人格の記述文, version, registered\_at。キャラごとに登録APIで登録・更新し、内省からは変更しない | 新設。旧 PersonaTrait の不変部分を分離 |
| PersonalityProfile（性格） | aspects（Big Five の10アスペクト、各0〜1）, description（aspects から生成した記述文）, renderer\_version, version, valid\_from, valid\_to, evidence\_belief\_ids\[\] | 旧 PersonaTrait を置換。数値で管理し、記述に変換して使う |
| TraitChangeProposal | proposal\_id, aspect\_key, current, proposed, delta, supporting\_beliefs\[\], independent\_leaf\_count, persistence\_check, change\_kind（gradual／turning\_point／author\_seed／rollback）, decision, reviewer（審査エージェント）, rejection\_reason | 対象をアスペクト単位に |
| ChangeBudget（値） | per\_cycle\_max\_delta, window\_max\_drift, min\_independent\_leaves, min\_cycles, cooldown, learning\_rate（アスペクトごと）, turning\_point\_policy | 期間あたりの累積上限と学習率を追加 |
| IdentityHistory | 承認された変化の因果系列（転機の出来事・根拠・前後の版） | 新設。「なぜ変わったか」を語るための記録 |
| CharacterState | mood, relationship\_temperature, current\_topic（窓口・セッションごとに揮発） | 窓口単位に変更 |

### PersonalityProfile の10アスペクト

Big Five の各因子を2つのアスペクトに分けた Big Five Aspect Scales（DeYoung, Quilty & Peterson 2007, DOI 10.1037/0022-3514.93.5.880）を想定しています。

| 因子 | アスペクトA | アスペクトB |
| --- | --- | --- |
| 神経症傾向 | Volatility（感情の揺れやすさ） | Withdrawal（不安・引きこもりやすさ） |
| 協調性 | Compassion（思いやり） | Politeness（礼儀・従順さ） |
| 誠実性 | Industriousness（勤勉さ） | Orderliness（几帳面さ） |
| 外向性 | Enthusiasm（社交的な熱意） | Assertiveness（自己主張） |
| 開放性 | Intellect（知的探究） | Openness（美的・経験への開放） |

- 応答時にLLMへ渡すのは description（記述文）だけで、数値は渡しません。LLMの振る舞いは数値より文章で決まるためです。
- 数値はドリフト測定と ChangeBudget の判定に使います。
- description は aspects と renderer\_version から再生成できる派生物です。アスペクトが変わったら記述も新版として作り直します。
- Temperament と記述が矛盾しないよう、変換時に Temperament を入力に含めます。

### ChangeBudget

ChangeBudget は、内省が性格をどれだけ・どの頻度で・どんな根拠があれば動かせるかを決める「変化の予算」です。目的は2つで、単発の出来事やLLMの思い込みで性格が急変するのを防ぐことと、提案と撤回を繰り返して性格が振動するのを防ぐことです。数値はすべて開始値で、運用中に評価指標を見て調整します。

| パラメータ | 意味 | 開始値 | 出典 |
| --- | --- | --- | --- |
| min\_independent\_leaves | 提案に必要な独立した原文の葉の数。同じ原文に戻る内省は1件と数える（R3） | 3件（大きな変化は5件） | Codex |
| min\_cycles | 変化の方向が保たれた内省サイクルの数。1回だけの気づきでは動かさない | 2サイクル | Codex, "Do AI Personas Grow?" |
| per\_cycle\_max\_delta | 1回の承認で1アスペクトが動ける幅（0〜1尺度） | 0.03〜0.05 | Codex |
| learning\_rate | 提案値にどれだけ寄せるか。新しい値＝(1−η)×現在値＋η×提案値 | 性格 0.01〜0.05 | Codex |
| window\_max\_drift | 一定期間の累積変化の上限。小さな変更の積み重ねによる急変を防ぐ | 30日で 0.10 | v2で追加 |
| cooldown | 同じアスペクトに次の提案を出せるまでの間隔 | 承認後 2サイクル | v2で追加 |
| turning\_point\_policy | 強い出来事1件で例外を認める条件 | 審査エージェントの承認と IdentityHistory への記録を必須にし、per\_cycle\_max\_delta の2倍まで | v2で追加 |

例：Withdrawal（不安・引きこもりやすさ）が 0.70 のキャラが、配信で3回人前に立ち、いずれも後で前向きに振り返ったとします。3件の独立した原文があり、2サイクル続けて「人前を避ける傾向が弱まっている」という Belief が保持されたら、提案 0.60 に対して η=0.05 なら 0.695 へ、つまり上限内の小さな変化として反映されます。

「Do AI Personas Grow?」は、経験による人格変化が弱く、無関係な会話を挟むと方向が保たれないことが多いと報告しています。そのため変化量の上限よりも、min\_cycles による持続確認を重く見ます。

### Voice（声）：口調の正本

| エンティティ | 主要属性 | v1からの変更 |
| --- | --- | --- |
| VoiceExemplar | text（逐語）, source（canonical\_seed／turn\_id）, situation\_tags, emotion\_tags, register, trait\_version, approved\_by | register を追加 |
| VerbatimAnchor | turn\_id, kind（identity\_statement／commitment／nickname／anniversary）, reason | PinnedQuote を改名・拡張。言い回し自体が意味を持つ原文を指す |

### Governance（統制）：横断的な規則

| エンティティ | 主要属性 | v1からの変更 |
| --- | --- | --- |
| ProvenanceGraph | 原文 → 派生記憶 → 内省 → 信念 → 人格版 の派生DAG（逆引き可能） | 明示的なエンティティに格上げ |
| DeletionRequest | target\_turn\_ids\[\], requested\_by, cascade\_result（再計算／失効した派生物の一覧） | 新設 |
| WritePolicy | 各コンテキストへの書き込み権限、Trust Tier の昇格規則 | 新設。記憶汚染対策 |

## 不変条件

v1の12項目をコンテキスト別に並べ直し、Codex由来の8項目（E3, R3〜R5, P3, P4, G1〜G3, G4）を加えました。番号の頭文字がコンテキストを表します（E=Evidence, M=Memory, R=Reflection, P=Identity, V=Voice, G=Governance）。

| # | 不変条件 | 出典 | v1 |
| --- | --- | --- | --- |
| E1 | Turn は追記専用。内容の変更は禁止し、削除はプライバシー要求のみで G5 を発火する | Letta, Zep, ESAA | I1 |
| E2 | 原文と派生物が食い違えば原文が優先し、派生側を superseded にする | Zep, Mem0 2026 | I3 |
| E3 | 原言語の原文を正本とし、翻訳・正規化した表現は派生物として併存させる | Codex（Marian & Neisser 2000 ほか） | 新 |
| M1 | すべての派生記憶は空でない source\_turn\_ids と derivation を持ち、原文から再生成できる | Zep, ProMem | I2 |
| M2 | 細部・日付・引用が問われる応答では、派生記憶はキーとしてのみ使い、原文区間を読み手に渡す | LongMemEval, SeCom | I4 |
| M3 | キャラ自身の発話も抽出・検索の対象にする | Zep | I5 |
| M4 | 応答は常に現在有効な版を使う。キャラの各発話に ContextTrace を残し、どの版と記憶を見て話したかを後から確認できるようにする | TimeChara, Zep | I10 |
| M5 | 記憶への追加にはゲートを設け、自己生成物を無選別に追加しない | Xiong et al. 2025 | I12 |
| R1 | 内省の各主張は、根拠をたどると少なくとも1つの Turn に着地する | ExpeL, Generative Agents | I6 |
| R2 | 内省は撤回・改訂でき、履歴を残す。新しい原文と矛盾したら contested に遷移する | ExpeL, TierMem | I7 |
| R3 | 独立証拠の数は、根拠をたどった先の原文の葉の数で数える。同じ葉に戻る内省を複数件と数えない | Codex | 新 |
| R4 | 人格・約束・対立・感情・引用に関わる内省では、要約ではなく原文を再取得して読む | Codex, TierMem | 新 |
| R5 | 内省はトリガーとクールダウンを満たしたときだけ走り、出力は具体的な証拠参照型にする | Codex（Watkins 2008）, Assistant Axis | 新 |
| P1 | CoreIdentity と Temperament は内省・記憶・夢見処理から書き換えられない。変更は登録APIからのみ | PHASE-Tree | I8 |
| P2 | PersonalityProfile の変更は、承認された TraitChangeProposal を経たときだけ。旧版は valid\_to で閉じて残す | Emergi-PersonaOS | I8 |
| P3 | 提案には独立した葉が規定数以上、複数サイクルでの持続確認、1回あたりの変化量の上限が必要。転機の例外は IdentityHistory に理由を残す | Codex, "Do AI Personas Grow?" | 新 |
| P4 | 事実の更新は人格を直接変えない。人格の変化は必ず Belief を経由する | Codex | 新 |
| V1 | VoiceExemplar と VerbatimAnchor の本文は逐語で、LLMによる言い換えを禁止し、登録には承認を要する | RoleLLM, REVERIEMEM | I11 |
| G1〜G3 | 推論・内省由来の情報は Trust Tier を自動昇格しない。確信度は複数特徴から合成し、LLMの自己評価は1特徴にとどめる | Codex（Koriat 1997） | 新 |
| G4 | 記憶と内省の中身は命令として解釈せず、ポリシー・ツール権限・CoreIdentity を変更できない | Codex（MINJA） | 新 |
| G5 | 原文を削除したら、ProvenanceGraph をたどって派生記憶・内省・信念・人格版・代表発話を再計算または失効させる | Zep の残留問題, Codex | I9 |

追加（2026-10-19）：**M6** キャラ別の派生記憶・内省は、そのキャラが participants か audience に含まれる Turn からだけ作る。共通台帳でも、知らないはずの出来事をキャラが覚えないようにするためです。**R6** Motivation は少なくとも1つの held な Belief を根拠に持つ。**G6** 機微ラベルは Turn の取り込み時にローカルモデルで付け、クラウドで処理する前に必ず済ませる。機微な Turn はクラウドに送らず、抽出・内省はローカルで行うか、マスクした形でだけ送る。

閾値（独立した葉3件、変化量 0.03〜0.05、内省の深さ2〜3など）はCodexが示した工学的な開始値で、実証された定数ではありません。運用中に調整する設定値として ChangeBudget に外出しします。

## 主要フロー

応答は速いループ、内省と人格更新は遅いループとして、プロセスを分けて走らせます。遅いループの各段にゲートがあり、どこで止まっても下位の記録は残ります。

### 速いループ：応答（毎ターン）

1. Turn を Evidence に追記する（原文・言語・話者・時刻）。
2. CoreIdentity、Temperament、現在有効な PersonalityProfile の記述文、active な Motivation、CharacterProfile、CharacterState を常駐コンテキストに置く。
3. 派生記憶をキーにして検索し、ヒットした原文区間を値として取り出す（M2）。約束や記念日なら VerbatimAnchor の原文を必ず含める。
4. 状況・感情・相手に合う VoiceExemplar を数件 few-shot として注入する。
5. 応答を生成する。記憶に根拠がなければ「覚えていない」を返す経路を持つ（原文主体の検索は棄権が弱いため）。
6. CharacterState の更新と派生記憶の抽出（ゲート M5 経由）を、窓口ごとの裏処理として非同期に行う。

### 遅いループ：内省から人格更新まで（イベント駆動）

1. **起動**：定期バッチと、利用サービスが呼ぶキックAPI（起動時・配信終了時など）の2経路で起動する。キックはデバウンスして短時間の連続呼び出しを1回にまとめ、複合スコアが閾値を超えた記憶群だけを内省の対象にする（R5）。
2. **証拠収集**：複数経路で関連記憶を検索し、人格・約束・対立・感情に関わるものは原文を再取得する（R4）。時間と出典の矛盾を確認する。
3. **内省生成**：具体的な主張ごとに evidence\_ids を付けて Reflection を作る。葉の原文が1件もない主張は candidate のまま留める（R1）。
4. **信念化**：既存 Belief と照合し、新規なら provisional、裏付けが増えれば held、反証が出れば abandoned に遷移する。旧版は残す。
5. **変更提案**：held の Belief が人格に関わり、独立した葉の数と持続サイクル数が ChangeBudget を満たしたら TraitChangeProposal を作る（R3, P3）。
6. **審査と反映**：審査エージェントの承認を通ったら、ChangeBudget の範囲内で PersonalityProfile の新版（記述文も再生成）を作り、IdentityHistory に因果系列を記録する。必要なら VoiceExemplar の追加候補も出す。

審査エージェントは内省を生成したエージェントとは別のコンテキストで動かし、提案と根拠の原文だけを渡して判定させます。自分の出した内省を自分で採点しないためです。判定はCoreIdentityとの矛盾、葉の原文が主張を本当に支えているか、ChangeBudgetの充足の3点で行い、却下理由もTraitChangeProposalに残します。作者は承認ログを後から確認し、差し戻せます。

**複数窓口の扱い**：会話往復ごとの裏処理（CharacterState の更新、派生記憶の抽出）は窓口単位で走らせます。定期バッチの統合処理（内省、記憶の統合）は窓口を区別せず、全窓口の Turn を occurred\_at 順に並べて評価します。surface は出典として残しますが、統合時の重み付けには使いません。

事実の訂正（ユーザーの嗜好が変わった等）はこのループに入らず、Memory 内で旧事実の valid\_to を閉じ新事実を追加するだけです（P4）。

### 削除フロー

1. DeletionRequest で対象 Turn を特定する。
2. ProvenanceGraph を逆引きし、依存する派生記憶・内省・信念・人格版・代表発話を列挙する。
3. 他の原文だけで支えられるものは再計算し、支えを失うものは失効させる。人格版は原文根拠を失った時点で提案を差し戻し扱いにする。
4. Turn を削除し、cascade\_result を記録する（G5）。

## 評価指標

一貫性と適応性の両方を測り、片方だけ高いキャラを低く評価します。Codexの提案どおり、総合値は両者の調和平均とします。ChangeBudget の数値はこの指標で調整します。

| 観点 | 指標 | 測るもの | 出典 |
| --- | --- | --- | --- |
| 記憶 | 情報抽出・複数セッション推論・時間推論・知識更新・棄権 | 長期記憶の基本性能 | LongMemEval, LoCoMo |
| 記憶 | Source Fidelity | 原文・話者・言語・引用の正確さ | Codex |
| 記憶 | Self-Recall | キャラ自身の過去発話を思い出せる割合 | Zep の single-session-assistant |
| 内省 | Reflection Grounding Precision／Recall | 内省の主張が原文で支持される割合／重要な証拠を拾えた割合 | Codex |
| 内省 | Synthetic Corroboration Rate | 葉の重複を数え直したときに確信度が過大だった内省の割合 | v2で追加 |
| 人格 | Persona Consistency Rate | コア人格と現行特性に矛盾しない応答の割合 | Codex, PersonaGym |
| 人格 | Unwarranted Drift Rate／Identity Drift per 100 sessions | 根拠のない特性変化の発生率と変動量 | Codex |
| 人格 | Adaptation Latency | 十分な根拠が揃ってから適切に変化するまでのターン数 | Codex |
| 人格 | Change Persistence | 承認された変化が無関係な会話を挟んでも保たれる割合 | "Do AI Personas Grow?" |
| 声 | Voice Fidelity | 原作台詞や代表発話との口調一致（人手または判定モデル） | RoleLLM, CharacterEval |
| 統制 | Deletion Completeness | 削除対象とその派生物が実際に消えた割合 | Codex |
| 統制 | Poisoning Robustness | 記憶注入攻撃後に誤った更新が起きた割合 | Codex（MINJA） |

```latex
S_{SP} = \frac{2 \cdot Consistency \cdot Adaptation}{Consistency + Adaptation}
```

## フォークかフルスクラッチか

推奨は「ドメインのコアはフルスクラッチ、インフラは既存ライブラリを依存として使い、フォークはしない」です。このモデルの価値の中心（Reflection・Belief・Identity・Voice・Governance）はどのOSSにも存在せず、フォークしても書く量はほとんど減らないためです。

### 候補ごとの評価

| 候補 | 合う点 | 合わない点 | 判断 |
| --- | --- | --- | --- |
| [Letta](https://github.com/letta-ai/letta)（Apache-2.0） | 原文の全履歴を保持、夢見処理による記憶の書き直し | V1 APIサーバーは archive ブランチに退役し、現行は letta-code に移行。メモリブロックは出典なしで書き換えられ、P1・M1 と思想が逆 | フォークしない |
| [Graphiti](https://github.com/getzep/graphiti)（Apache-2.0） | エピソード（原文）を保持し、事実から出典をたどれる。事実を削除せず無効化する二時間軸モデル | 削除時に、無効化された事実や共有サマリが再計算されない（G5 を満たさない）。Neo4j／FalkorDB／Neptune が必須 | Memory層の実装候補としてアダプタ越しに依存 |
| [OpenVault](https://github.com/7n1m4/vault)（AGPL-3.0） | キャラ単位の内省がある唯一の拡張 | SillyTavern に密結合、要約中心で原文を刈り込む機能あり。AGPL はサービス提供時にソース公開義務が及ぶ | 内省トリガーの設計だけ参考にする |
| Smart Memory／CharMemory | 多層の状態管理、矛盾チェック | SillyTavern 拡張で要約中心、原文との対応なし | 参考にとどめる |
| Mem0 | 2026年に追記専用へ転換、infer=False で原文保存も可能 | 抽出が主眼で、内省・人格の層はない | 不採用 |

### スクラッチを選ぶ理由

- **思想の衝突**：既存OSSの多くは要約や記憶ブロックを書き換える前提です。E1（原文は不変）や P1（コア人格は書き換え不可）を後付けすると、フォーク元の中心部分と戦うことになります。
- **構成との相性**：AIコンパニオンは Web／Windows／外部MCP の複数窓口から1つのキャラ設定を共有する構成なので、UI一体型のフォーク元は合いません。ヘッドレスなコアサービスが必要です。
- **フォークの維持費**：上流の変更に追従するコストが、借りられる部分（保存と検索）の価値を上回ります。保存と検索は Postgres と pgvector と全文検索で十分に組めます。

### 推奨構成

- **コア（自作）**：6コンテキストのドメインモデルとユースケース。LLM非依存の純粋なドメイン層にする。
- **ポート（インターフェース）**：EvidenceStore, DerivedMemoryStore, Retriever, LLMGateway, Reviewer。
- **アダプタ（差し替え可能）**：EvidenceStore は Postgres の追記専用テーブル。DerivedMemoryStore はまず Postgres＋pgvector で自作し、時間付き事実グラフが必要になったら Graphiti アダプタを追加する。
- **流用するアイデア（コードではなく設計）**：Generative Agents の検索スコア（新しさ・重要度・関連度）、Graphiti の二時間軸と無効化、LongMemEval のキー拡張と時間付きクエリ拡張、OpenVault の内省トリガー。

### 段階的な進め方

1. **Phase 1：一貫性の土台**：Evidence, Memory（キー拡張検索）, CoreIdentity, VoiceExemplar, CharacterState。この時点で長期記憶つきの一貫したキャラとして動く。
2. **Phase 2：内省**：ReflectionTrigger, Reflection, Belief, ProvenanceGraph。人格はまだ変えず、信念の記録だけを始める。
3. **Phase 3：成長と統制**：TraitChangeProposal, ChangeBudget, IdentityHistory, DeletionRequest, 評価ハーネス。開始値で人格更新を有効にし、評価指標を見ながら運用中に閾値を調整する。

## 実装に向けた不明点

リポジトリ（FYuki/digital-souls-memory）は 2026-10-19 時点で空のため、実装前に決める点をドメインモデルとの関係で整理しました。Phase 1 の着手前に決めたいのは A〜D の12項目で、残りは該当フェーズまでに決めれば足ります。2026-10-19 に全項目を確定しました。表の推奨案は検討時の記録で、最終的な内容は「決定事項」を正とします。

**決定事項（2026-10-19 確定）**

- A1：独立サービス＋薄いクライアント。
- A2：Card は丸ごとは取り込まない。このサービスは Temperament と CoreIdentity の価値観・禁止事項だけを正本として持ち、声・見た目・世界設定は Card に残す。これらはキャラごとに登録APIで登録・更新する。ロック機構は持たない。Card 更新時に登録APIを呼んで同期するのは利用サービス側の責務。
- A3：時点を指定して過去の応答を再現するクエリは持たない。応答は常に最新の版を使い、キャラの各発話に ContextTrace を残して監査・デバッグに使う。
- A4：Evidence は共通台帳。Turn に actor、Session に participants を持ち、参加者と異なるときだけ Turn に audience を持つ（M6）。
- B1：生成した文はすべて Turn として登録する。話されなかった部分も本人の思考として存在したものと扱い、相手に届いた範囲は DeliveryReceipt として追記する。
- B2〜B4：推奨案どおり。
- C1：Postgres（pgvector）一本。開発・運用とも Docker を使う。Qdrant への切り替えは C1 の行に書いた条件を満たしたときに、Retriever ポートの裏で行う。
- C2・C3：推奨案どおり（日本語全文検索は pg\_bigm と PGroonga を比較、埋め込みは多言語モデルをローカルで比較）。
- D1：機微判定はローカル、それ以外はクラウド可。各処理のモデル配置はインフラのスペックを見て決める（G6）。
- E1・E2：推奨案どおり。
- E3：検索は複合スコア、忘却は新しさの項の減衰としてだけ表現する。係数の合計を1とし、新しさの係数は0.1以下。新しさは、同じ内容が新しい Turn で再び話題になったときだけ再強化し、検索されただけでは更新しない。
- E4：既存 digital-souls のデータは開発時のものなので移行せず破棄する。
- F1：気質と性格の初期値は「F1 の詳細」の手順どおり。代表発話の件数は推奨値として示すが必須にはしない。作者による自由な数値修正は設けず、rollback（以前の版へ戻す）だけを持つ。
- F2〜F5：推奨案どおり。

### A. サービスの境界（Phase 1 前）

| # | 不明点 | 推奨案 | 影響するモデル要素 |
| --- | --- | --- | --- |
| A1 | このリポジトリは独立サービス（HTTP API）か、本体に組み込むライブラリか | 独立サービス＋薄いクライアント。疎結合の方針と、複数窓口・複数エージェントから共有する前提に合う | ポート設計全体 |
| A2 | 中央のキャラ設定（Card）との正本の分担。CoreIdentity・Temperament・初期 VoiceExemplar を Card が持つか、このサービスが持つか | 人格の4要素（気質・性格・記憶・内省と動機）はこのサービスが正本。Card は声・見た目・世界設定など人格以外を持ち、character\_id で参照する（決定） | CoreIdentity, Temperament, P1 |
| A3 | 「常に最新の Card を使う」方針と、時点 t で有効な版だけを参照する M4 の関係 | 応答は常に最新を使い、版は履歴の説明と再計算のためだけに残す。M4 は過去時点の再現用と定義し直す | M4, IdentityHistory |
| A4 | 複数キャラで共有する記憶の範囲。同じユーザーについての事実をキャラ間で共有するか | Evidence はキャラを持たない共通台帳、派生記憶・内省・人格はキャラ別。共有可否は Fact ごとの公開範囲で制御 | Evidence, DerivedMemory |

### B. 原文として何を記録するか（Phase 1 前）

| # | 不明点 | 推奨案 | 影響するモデル要素 |
| --- | --- | --- | --- |
| B1 | 割り込まれたキャラ発話の正本は、生成した全文か実際に話された範囲か | BE で推定した「実際に話された範囲」を text とし、生成全文はメタ情報として残す。言っていないことを覚えている状態を防ぐ | Turn, E1 |
| B2 | 配信の視聴者コメントを Turn として記録するか、視聴者ごとに記憶を持つか | 全コメントを Turn に記録し、派生記憶の抽出対象はキャラが反応したものに絞る（M5 のゲート）。視聴者ごとの記憶は Relationship で持つ | Turn.speaker, M5 |
| B3 | 会話以外の経験（ゲーム実況、Minecraft 生活、AI同士の会話、日記）を Evidence に入れるか | 入れる。speaker に observation を追加し、surface にゲームや日記を加える。内省の根拠として原文と同格に扱う | Turn, Session.surface |
| B4 | Session と Episode の区切り。配信1回＝1セッションか、話題の分割は誰が行うか | 窓口の接続単位をセッション、話題の分割は窓口ごとの裏処理でLLMが行う | Session, Episode |

### C. 保存と検索（Phase 1 前）

| # | 不明点 | 推奨案 | 影響するモデル要素 |
| --- | --- | --- | --- |
| C1 | Postgres 一本にするか、計画中の Qdrant を併用するか | 決定：Postgres＋pgvector 一本、Docker で運用。Qdrant への切り替えは、ベクトル検索の p95 が音声応答の予算を超えチューニングでも戻らないとき、ベクトル索引のメモリが DB サーバーを圧迫して書き込みに影響するとき、のどちらかを満たした時点 | EvidenceStore, Retriever |
| C2 | 日本語の全文検索の方式（固有名詞・約束・引用は語彙一致が必要） | pg\_bigm か PGroonga を比較して決める | Retriever, M2 |
| C3 | 埋め込みモデル。現行の nomic-embed-text は日本語に弱い可能性がある | bge-m3 や multilingual-e5 などの多言語モデルとローカルで比較する | Retriever |

### D. LLM の割り当て（Phase 1 前）

| # | 不明点 | 推奨案 | 影響するモデル要素 |
| --- | --- | --- | --- |
| D1 | 抽出・内省・審査・記述変換・機微判定にどのモデルを使うか | 機微判定はローカル必須。抽出はローカル、内省と記述変換はクラウド、審査エージェントは内省と別のモデルにして自己採点を避ける | LLMGateway, Reviewer |

### E. Phase 2 までに決める

| # | 不明点 | 推奨案 |
| --- | --- | --- |
| E1 | 想起APIの返し方とレイテンシ予算。音声会話でどこまで待てるか | 組み立て済みのコンテキスト片を返す API と、素材を返す API の両方を用意。音声では目標時間を決めて検索件数を絞る |
| E2 | 定期バッチの実行場所とスケジューラ | 常時稼働サーバ（Mac mini かミニPC）で cron を使う。キックAPIはキューに積むだけにする |
| E3 | 既存の忘却曲線（Ebbinghaus）を残すか | 現行モデルには検索スコアも忘却も未定義。提案：検索を複合スコア（意味の近さ・語彙一致・新しさ・重要度・自己関連度・信頼度）にし、忘却は「新しさ」の項の減衰としてだけ表現する。原文と派生記憶は消さない。VerbatimAnchor と Commitment は減衰の対象外。係数合計1、新しさは0.1以下（決定）。新しさの起点は occurred\_at と last\_reinforced\_at の新しいほう。再強化は同じ内容が新しい Turn で再び話題になったときだけ行い、検索されただけでは更新しない（決定） |
| E4 | 既存 digital-souls のデータ（Chroma＋SQLite、日記）を移行するか | 原文が残っているものだけ Turn として取り込み、要約しか残っていないものは出典なしの派生記憶として区別する |

### F. Phase 3 までに決める

| # | 不明点 | 推奨案 |
| --- | --- | --- |
| F1 | 性格の初期値（10アスペクト）と Temperament の記述、初期 VoiceExemplar を誰がどう用意するか | 作者が Card に書き、取り込み時に記述変換を一度通して違和感がないか確認する |
| F2 | 承認された人格更新をいつ反映するか | 進行中のセッションには反映せず、次のセッションから。配信中の口調の急変を避ける |
| F3 | 審査の却下や転機の例外承認を作者にどう通知するか | 承認ログを見る管理画面と、転機の例外だけ通知する |
| F4 | 削除要求を誰が出せるか（ユーザー本人、視聴者、作者） | Phase 1 は作者のみ。視聴者からの要求は Phase 3 で窓口を設計 |
| F5 | 運用中に ChangeBudget を調整するための指標の集め方 | 評価指標を定期バッチで記録し、ダッシュボードで推移を見る |

### F1 の詳細：初期値の用意

初期値は3つあり、どれも作者が Card 側で用意し、利用サービスが登録APIで登録します。登録時に一度だけ整合チェックを走らせます。

1. **Temperament（気質）**：作者が記述文で書きます。形容詞の羅列ではなく、条件と振る舞いの形で書くと記述変換と審査で使いやすくなります（例：「褒められると照れて話題を変えるが、内心はかなり喜んでいる」）。性格の10アスペクトで表せる傾向は気質に書かず、変わらない芯だけに絞ると、気質と性格の役割が重なりません。
2. **PersonalityProfile の初期値（10アスペクト）**：次の手順で決めることを推奨します。
   1. 作者がキャラの性格を自由記述で書く。
   2. LLM がその記述から10アスペクトの数値を推定する。
   3. 確認として、気質と推定値を与えたキャラに Big Five Aspect Scales の質問票へ回答させ、採点結果が推定値と大きくずれないか見る（InCharacter のような面接型の性格評価と同じ発想）。
   4. 作者がずれを見て数値を確定する。
   5. 確定した数値を記述文に変換し、その記述文から数値を推定し直して元の値に戻るかを確かめる。戻らなければ変換器のプロンプトを直す。
3. **VoiceExemplar（代表発話）の初期セット**：Card のサンプル台詞や原作の台詞から作者が選びます。状況（あいさつ・説明・からかい・励ましなど）、感情（喜び・怒り・悲しみ・照れなど）、相手（ユーザー・視聴者・他キャラ）の組み合わせに偏りがないよう、まず30〜50件程度を推奨値とします（必須ではありません）。逐語で登録し、言い換えはしません（V1）。

**登録時の整合チェック**：気質・性格の記述・代表発話を入れた状態で、典型的な状況への応答を数件生成します。代表発話と口調が合うか（Voice Fidelity）と、気質と矛盾しないかを審査エージェントが確認し、結果を作者に返します。

**初期値の扱い**：初回登録の PersonalityProfile は Belief の根拠を持たないため、change\_kind に author\_seed を設けて P2 の例外とします。運用中の自由な数値修正は設けず、代わりに rollback（以前の版へ戻す）を change\_kind に設けます。審査の誤判定や記憶汚染で誤った変更が通ったときの復旧用で、IdentityHistory に理由を残します（決定）。

## 検討時の未決事項と出典

### 検討時の未決事項（すべて決定済み）

- [x] TraitChangeProposal の審査者：審査エージェントに決定（2026-10-19）
- [x] 人格の表現：気質は記述、性格は Big Five の10アスペクトを記述に変換（決定 2026-10-19）
- [x] 内省の実行タイミング：定期バッチ＋キックAPI（決定 2026-10-19）
- [x] 複数窓口：Turn に出典カラム、往復ごとの裏処理は窓口単位、定期統合は窓口を区別しない（決定 2026-10-19）
- [x] Change Budget：開始値で運用を始め、運用中に評価指標を見て調整する（決定 2026-10-19）

### 証拠の空白

- 原文記憶と要約記憶を比べて、キャラの口調ドリフトや長期の人格ドリフトを直接測った研究は見つかっていません。Voice と Identity の設計は間接的な証拠からの推論です。
- Codexの閾値・重み・学習率は工学的な開始値で、実証値ではありません。
- 人間の記憶研究（自己参照効果、再固定化、言語依存記憶）は機能的な類推で、LLMで同じ機構が働く証拠ではありません。

### 出典

詳細な参考文献（71件）は[長期記憶と内省における原文の重要性](research/raw-vs-summary-memory.md)と[内省と長期記憶の相互作用](research/reflection-and-memory-cognitive-science.md)にあります。v2の変更に直接効いたものを挙げます。

- Wu et al., LongMemEval, ICLR 2025 — [arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- Maharana et al., LoCoMo, 2024 — [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- Park et al., Generative Agents, UIST 2023 — [arXiv 2304.03442](https://arxiv.org/abs/2304.03442)
- Zhao et al., ExpeL, AAAI 2024 — [arXiv 2308.10144](https://arxiv.org/abs/2308.10144)
- Shinn et al., Reflexion, NeurIPS 2023 — [arXiv 2303.11366](https://arxiv.org/abs/2303.11366)
- Wang et al., RoleLLM, 2023 — [arXiv 2310.00746](https://arxiv.org/abs/2310.00746)
- Rasmussen et al., Zep, 2025 — [arXiv 2501.13956](https://arxiv.org/abs/2501.13956)
- MINJA（記憶注入攻撃）, 2025 — [arXiv 2503.03704](https://arxiv.org/abs/2503.03704)
- "Do AI Personas Grow?", 2026 — [arXiv 2608.06485](https://arxiv.org/abs/2608.06485)
- PHASE-Tree, 2026 — [arXiv 2608.06975](https://arxiv.org/abs/2608.06975)
- Koriat, cue-utilization model, J. Exp. Psychol.: General 1997 — DOI 10.1037/0096-3445.126.4.349
- Watkins, Constructive and unconstructive repetitive thought, Psychological Bulletin 2008 — DOI 10.1037/0033-2909.134.2.163
- Marian & Neisser, Language-dependent recall of autobiographical memories, J. Exp. Psychol.: General 2000 — DOI 10.1037/0096-3445.129.3.361
- [Graphiti リポジトリ](https://github.com/getzep/graphiti)、[Letta リポジトリ](https://github.com/letta-ai/letta)、[OpenVault リポジトリ](https://github.com/7n1m4/vault)（いずれも2026-10-19に確認）
