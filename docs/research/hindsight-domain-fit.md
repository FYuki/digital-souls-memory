# HindsightでAIキャラクター記憶ドメインを実現できるか

2026-10-26。対象は `vectorize-io/hindsight` の `f29ba70c825094c839daaf27b2b5a564de46fb4e`（api-slim 1.1.0）。**調査・構成提案であり、採用決定や承認済みADRの変更ではない。**

## 結論

1. **Hindsightを記憶処理の中核にする構成は成立し得る。** 原文の検索用コピー、抽出、検索、同一Factの非同期統合、出典削除後の再統合を任せられる。
2. 判断対象は[ドメインモデル](../domain-model.md)である。既存 `src` との互換性・移行費用・置換工数は評価していない。
3. 出典は削除対象を追跡できる文書単位でもよい。粗い出典なら影響する文書・bankを広めに再構築する。Turn単位の厳密な抽出出典をPhase 1の採用条件にしない。
4. 同じFactの言い換え3件は、自動worker経路で1 observation・3出典に統合できた。異なる数値・否定・主体は別のobservationに残った。ただし意味判定はfixtureであり、実モデルの品質は未検証。
5. 通常の文書削除では古いobservationが消え、残った根拠から再統合が完了し、削除した秘密文字列がrecallにも残らなかった。
6. **削除と統合の競合には欠陥を再現した。** 複数出典を読んだ統合応答が削除後に届くと、生存出典だけを持つobservationに削除内容の本文が残る。
7. 削除中のretainおよび削除後の同ID・同内容の再送でも文書が復活した。tombstone、ジョブの排他・完了確認、読取り遮断が必要。
8. M6には**共通Evidence台帳＋キャラ別bankへの知覚射影**を第一案とする。共有bankのcombinedでは私的詳細の漏れも再現した。共有bankは強制scopeとキャラ単位の明示observation scopeが条件となる。
9. observationsは推論を含む派生知識、reflectは内省候補の生成器、mental modelsは信念・プロフィールのキャッシュに使える。信頼区分、主張単位の根拠、信念の状態遷移、人格の審査は外側で保証する。
10. Phase 1の記憶基盤として有力、Phase 2の生成部品として有力、Phase 3の人格成長・統制をnative機能だけで満たす適合性は低い。実モデル品質と全経路のp95を測って採否を決める。

## 評価の範囲と判定方法

正本として `docs/domain-model.md:25–53, 175–204, 262–314`、ADR [0019](../decisions/0019-phase1-deletion.md)、[0022](../decisions/0022-perceived-by.md)、[0023](../decisions/0023-extraction-unit-and-independent-evidence.md)、[0017](../decisions/0017-recall-latency-budget.md)、[0007](../decisions/0007-postgres-single-store.md)を読んだ。[前回比較](phase1-oss-reassessment.md)と既存probeは出発点として扱う。

今回の本人指示を優先するため、正本に残る厳密な `source_turn_ids`・再強化互換は採用基準から外す。**ただし、知覚外の原文を混ぜないこと、削除可能性、信頼を昇格しないこと、人格変更の独立根拠の条件は外していない。** R3の独立根拠を数える段階で、文書数やnative `proof_count`をそのまま使うことはできない。

「そのまま使える」は記載した機能単位がnativeにあること、「設定・拡張で使える」は設定・extension・薄い射影で達成できること、「外側に自作が必要」は状態機械・台帳・権限制約など別のドメイン実装が必要なこと、「適合しない」はそのnative概念を当該ドメイン概念の正本として使うと矛盾することを示す。行の主判定はエンティティ全体について行う。外側の実装が必要でも候補全体を不採用とはしない。

以下の `[Hxx]` は末尾の**固定commitの文書／コードのパス・行番号**への参照。`probe:` は[結果JSON](../reports/2026-10-26-phase1-oss/hindsight-domain-results.json)の `results[].test` を示す。機能の説明はローカルの固定ソースで確認し、外部サイト・実モデルには接続していない。

## エンティティ対応表

| ドメイン要素 | 判定 | Hindsightに任せられる部分と残る責務 | 根拠 |
| --- | --- | --- | --- |
| Session | 外側に自作が必要 | `document_id`をsessionに対応できるが、開始／終了・surface・変更不可のreports_delivery・セッション版の契約はない。共通台帳で管理する | [H02]、[H03]、domain-model:65 |
| Turn：原文・言語・actor・時刻 | 設定・拡張で使える | document/chunkで原文と時刻を保存。1 Turn＝1 documentならID対応も容易。ただし不変性の強制は外側。Hindsightのdocumentは可変の検索用コピーにする | [H01]、[H02]、[H03]、`probe:reprocess_preserves_original_and_rebuilds_units` |
| Turn：kind | 外側に自作が必要 | domainのutterance／observation／thoughtは台帳属性。Hindsightのworld／experience／observationは別の軸であり、thought＝observation等と機械的に同一視しない | [H04]、domain-model:66,73–83 |
| Turn：perceived_by | 設定・拡張で使える | キャラ別bankへの射影、またはキャラtagsと強制scope。入力を作る前にCore確定の知覚を検証し、空の知覚には射影しない | [H07]、[H08]、[H09]、domain-model:73–83 |
| Turn：addressees／address_mode／reply_to | 外側に自作が必要 | metadataや原文内の構造として渡せるが、必須／null／self／既存返信先等の整合性・不変性を保証する台帳が必要。宛先を知覚権限に使わない | [H02]、[H01]、ADR0022:決定 |
| DeliveryReceipt | 外側に自作が必要 | 本人には生成全文、他キャラにはspoken_untilまでを射影。未着保留・既定10分のみなし確定・HTTP 422制約をHindsightは扱わない | [H03]、[H07]、domain-model:67,75–77 |
| ProvisionRecord | 外側に自作が必要 | operationsはretain／consolidation等の実行管理。Coreへ渡した記憶・逐語・人格版・候補スコアを各応答で保存する台帳とは異なる | [H18]、[H04]、domain-model:68 |
| Episode | 外側に自作が必要 | topicごとのdocument、metadata、検索は借りられる。窓口別の話題分割、turn_spanと機会IDの正本は外側 | [H02]、[H03]、domain-model:69、ADR0023 |
| DerivedMemoryの共通保存・再生成・検索 | 設定・拡張で使える | 原文→world/experience→observation→recallを利用。source document集合＋抽出設定／モデル版の記録で削除・再構築する。厳密な既存schemaへの互換は不要 | [H01]、[H04]、[H05]、[H10] |
| Fact／Preference | 設定・拡張で使える | nativeの抽出とobservationsによる非同期統合を使う。必要ならcustom抽出でSPOを生成。Fact用の表示には`fact_type=["observation"]`を使いraw mentionsを並べない。文脈・時制・相手・公開可否を外側で検証する | [H01]、[H05]、[H06]、`probe:auto_consolidation_equivalent_mentions`、`probe:observation_only_recall_unifies_fact_view` |
| Relationship | 外側に自作が必要 | 関係の文章抽出はできる。affection／trustの独立した値、counterpart、status・版の規約はnative entity graphとは別に持つ | [H01]、[H04]、domain-model:91,99 |
| Commitment | 外側に自作が必要 | 約束の検索・原文取得は借りる。deadline・必須の逐語アンカー・self除外・相手の確定は外側 | [H03]、[H11]、domain-model:92,99 |
| Summary／Key | そのまま使える | 抽出factsをキーとしchunkを値にする。継続的な要約にはmental modelも利用可能。正確な原文必須時は独自組立を通す | [H03]、[H11]、[H12] |
| Epistemics／Trust Tier | 外側に自作が必要 | metadataとtagsは格納・分離の部品。A〜F、direct/observed/inferred/reflection、複数特徴のconfidence、機会と話者での独立根拠数はない。観測のproof_countを信頼区分や独立根拠数にしない | [H04]、[H06]、[H13]、ADR0023 |
| MemoryRelation | 外側に自作が必要 | source参照・因果／時間／意味／entity linksはある。updates／extends／derives／contradictsと有効期間の意味を保持する型付き関係は別途定義・保存する | [H01]、[H04]、[H14]、domain-model:96 |
| CharacterProfile | 設定・拡張で使える | キャラ単位のmental modelを「常駐要約のキャッシュ」にする。ProfileEntry選抜・token上限・信頼・セッション版固定は外側。保護された人格の正本は入れない | [H12]、[H15]、domain-model:97 |
| Reflection | 設定・拡張で使える | reflectにmission・directives・response_schema・include.factsを設定し内省候補を作る。主張ごとの根拠、葉の存在、原文再読、candidate→採用、履歴は外側で検証・保存 | [H16]、[H17]、domain-model:107–111 |
| Belief | 外側に自作が必要 | observationsは「belief」と呼ばれ、mental modelにも履歴がある。しかしprovisional／held／abandoned、原文根拠によるconfidence、supporting_reflection_ids・valid_toはドメイン状態機械。mental modelはそのread modelに使う | [H05]、[H12]、[H15]、domain-model:109 |
| Motivation | 外側に自作が必要 | reflectで候補を生成できる。held Beliefが必須の根拠であること、interest／goal／aversion・strength・horizon・終了状態は別途保証する | [H16]、domain-model:110,195 |
| CoreIdentity | 外側に自作が必要 | 作者の登録APIだけが変更できる正本として保持。bank mission/directivesにはその読取専用の射影を渡せるが、更新権限を隔離する | [H16]、[H07]、domain-model:117,196 |
| Temperament | 外側に自作が必要 | version付き不変記述を外側で保持。Hindsightのdispositionはskepticism／literalism／empathyの1〜5であり、気質の代用には適合しない | [H13]、domain-model:118 |
| PersonalityProfile | 外側に自作が必要 | 10 BFAS aspects、版・renderer・Belief根拠・次セッション適用は外側。mission/dispositionや自動更新するmental modelを正本にする方式は適合しない | [H13]、[H15]、domain-model:119,196–199 |
| ChangeBudget／TraitChangeProposal／IdentityHistory | 外側に自作が必要 | 独立葉数・複数サイクル・変化量・累積ドリフト・審査・rollbackをnativeのconsolidationには任せられない | [H06]、[H13]、domain-model:120–122,142–158,197–199 |
| VoiceExemplar／VerbatimAnchor | 外側に自作が必要 | document/chunkは逐語保存・取得に使える。承認と型・用途・必ず返すアンカー・口調被覆の選抜は別管理。LLMを呼ぶverbatim抽出モードだけで無改変を保証しない | [H01]、[H03]、[H11]、domain-model:160–165,200 |
| CharacterState | 外側に自作が必要 | 窓口・session単位の揮発mood／temperature／topicは短期状態ストアで管理。永続memoryのentity stateをそのまま使う設計にはしない | [H04]、domain-model:123 |
| DeletionRequest | 外側に自作が必要 | native deleteと再統合を使うが、作者認可、対象actor/addresseesの和集合、tombstone、競合排他、再試行、cascade_resultと完了条件は外側 | [H10]、[H19]、[H20]、ADR0019／0022、競合probe |
| ProvenanceGraph | 外側に自作が必要 | document→fact→observationとreflectのbased_onは下位のDAG部品。Reflection→Belief→人格版→Voice／Provisionまでの逆引き、失効・差戻しは別のDAGとする | [H06]、[H17]、[H21]、domain-model:171,203 |

### native機能をそのまま置き換えると適合しないもの

| 置き換え | 判定 | 理由 |
| --- | --- | --- |
| unrestrictedな可変documentをTurnの唯一の正本にする | 適合しない | replaceと再送でE1を破れる。外側の不変台帳または書込API・DB制約が必要。[H02]、文書probe |
| shared scopeのuntagged observationを複数キャラの共通知識にする | 適合しない | その知識を全キャラが知覚した証拠がない。default anyでは漏れ、strictでは見えない。[H08]、scope probe |
| observation数・proof_countの増加をA/B相当の信頼や人格変更の根拠数にする | 適合しない | proof_countはsource memory UUIDの数で、機会・話者の独立性もTrust Tierも表さない。[H06]、[H13]、ADR0023 |
| mental model／bank dispositionを保護された人格版そのものにする | 適合しない | 自動refresh・文書履歴・3 disposition値は、作者専用コア・10 aspects・審査済み変化予算と異なる。[H12]、[H13]、[H15] |

## 不変条件の対応表

G1〜G3は正本が1行にまとめて定義しているため、以下も一組として評価する。独自に別々の番号へ意味を割り振らない。

| 条件 | 判定 | 実現方法／不足 | 根拠 |
| --- | --- | --- | --- |
| E1：Turnの追記専用・作者削除 | 外側に自作が必要 | 台帳のINSERT-only、作者認可、tombstone、ID再送の冪等性。projectionのreplace／reprocessは許容する | [H02]、[H10]、retain競合／append probe |
| E2：原文優先・派生失効 | 設定・拡張で使える | 元documentから再抽出し旧observationを失効・再統合。supersededの業務状態と競合完了条件は追加する | [H10]、[H19]、[H20] |
| E3：原言語正本・翻訳は派生 | 設定・拡張で使える | raw文書と翻訳を別に保存。原文をredact/normalizeする経路の唯一のコピーにしない | [H03]、[H22]、reprocess probe |
| M1：出典とderivation・再生成 | 設定・拡張で使える | document/fact/observationの参照を使い、bank世代・入力文書・model/prompt/config版を付ける。出典は安全に削除・再構築できる単位でよい | [H02]、[H04]、[H06]、[H20] |
| M2：細部には原文区間を返す | 設定・拡張で使える | include_chunks＋source factsの参照を解決し、詳細質問で原文必須にする。token予算による切断／未取得を確認し、足りなければ台帳再取得 | [H11]、`probe:strict_scope_returns_allowed_chunks` |
| M3：自身の発話も対象 | 設定・拡張で使える | utterance/thoughtを本人bankにも射影。experienceは保存部品であり本人判定はCore確定actorに従う | [H01]、[H04]、ADR0022 |
| M4：有効版固定・提供記録 | 外側に自作が必要 | session開始時の人格snapshotと各応答のProvisionRecordを保存。operations/webhooksだけでは満たさない | [H18]、domain-model:187,216–220 |
| M5：無選別な自己生成物の追加防止 | 設定・拡張で使える | validate_retain、custom抽出／retain mission、選別adapterを通す。promptは検証器の代わりにならず、chunksモードは抽出ゲートを省略する | [H01]、[H07]、[H23] |
| M6：知覚と配達範囲だけで派生・内省 | 設定・拡張で使える | 入力の切出し＋bank分割または強制strict scopes。Receipt待機・thought本人限定は外側。検索filterだけ後付けしても抽出時漏洩は防げない | [H07]、[H08]、[H09]、scope probe、ADR0022 |
| R1：各主張に原文の葉 | 外側に自作が必要 | reflectのbased_onは回答全体の参照集合。主張別schemaを出し、非空・実在・知覚・本文支持を検証し、無根拠はcandidateにとどめる | [H16]、[H17]、domain-model:190 |
| R2：撤回・改訂・履歴・contested | 外側に自作が必要 | memory curation、observation history、mental model historyは利用可能。ReflectionRevision／Beliefの状態遷移と保持期間を別途保証する | [H14]、[H15]、[H19]、[H27] |
| R3：独立した原文根拠数 | 外側に自作が必要 | source集合を葉へ戻し（機会,actor）が両方重複しない最大集合を計算。粗い出典の全Turnを「支持」として足す方式は不可 | [H06]、ADR0023、domain-model:192 |
| R4：人格等の内省で原文再読 | 設定・拡張で使える | reflect／mental model refreshのinclude_chunksを有効にし、対象種別では原文取得・読取をadapterで必須化。自動で必ず原文を読む保証はない | [H11]、[H12]、[H17] |
| R5：トリガー・cooldown・具体的参照 | 外側に自作が必要 | cron／refresh_after_consolidation／min_refresh_intervalは部品として使う。salience等の複合条件・kick/debounceは独自に判定する | [H12]、domain-model:194,225 |
| R6：Motivationはheld Belief由来 | 外側に自作が必要 | 外側のFK／状態検証と失効連鎖で保証 | [H16]、domain-model:195 |
| P1：コア・気質は登録APIのみ変更 | 外側に自作が必要 | 正本を外側に隔離し、Hindsightの通常worker／Coreには更新権限を渡さない | [H07]、[H13]、domain-model:196 |
| P2：審査済み提案・旧版保存 | 外側に自作が必要 | author_seed例外、Proposal審査、valid_toによる版管理を実装。mental model historyは代替しない | [H15]、domain-model:197 |
| P3：独立葉・持続・予算 | 外側に自作が必要 | ChangeBudgetと別モデル審査、cooldown、IdentityHistoryの検査が必要 | [H06]、[H13]、domain-model:198,232 |
| P4：Fact更新は人格を直接変更しない | 外側に自作が必要 | Factの時間更新とBelief→Proposalの経路を別コマンドにする。native occurred_start/endだけをvalid_from/toと同一視しない | [H04]、[H14]、domain-model:199,236 |
| V1：逐語・承認 | 外側に自作が必要 | 別の承認済みアンカーストアから逐語で返す。chunkはコピーの保存・検索に使う | [H01]、[H03]、domain-model:200 |
| G1〜G3：非昇格・多特徴confidence | 外側に自作が必要 | ソース種別を不可逆の出自として保持。観測は推論のまま、反復はTierを変えない。confidenceは支持・矛盾・時間等を別計算 | [H04]、[H06]、[H13]、domain-model:201 |
| G4：記憶は命令ではない | 外側に自作が必要 | directives/missionは補助。LLM出力に権限を与えないAPI境界、書込allowlist、コア人格の隔離が必要。memory-defenseは秘密/PIIのregex検査であり注入耐性の保証ではない | [H07]、[H16]、[H22] |
| G5：全層の削除連鎖 | 外側に自作が必要 | native文書→facts→observationsの削除とmental model refresh補助は使う。信念・人格版・Voice・Provision・履歴・cacheまでのDAGと競合完了条件は独自管理 | [H10]、[H19]、[H21]、競合probe |
| G6：機微ラベル必須・局所処理 | 設定・拡張で使える | validate_retainでラベル欠落を拒否。ローカルLLM/embeddingまたは送信前マスクを設定する。memory-defenseは既定OFF、原文もredactするためEvidence正本を別に保つ | [H22]、[H23]、domain-model:204 |

## 知覚、内省、信頼をどう表すか

### M6：bank、tags、強制scopeは別の役割

**第一案は1キャラ＝1 bank。** Evidenceは共通台帳のまま、各bankにはそのキャラが知覚した本文だけを複写する。bank自体をキャラ所有のEvidenceにしない。本人の生成全文と他キャラの配達済みprefixが違っても、別bankなら同じ論理Turnから別の検索文書を作れる。thoughtは本人bankだけ、perceived_by=[]ならbankへ入れない。人づての話は元Turnを共有せず、新しいTurnとしてretainする。

共有bankは、本文の知覚範囲が同じ単位に文書を切り、`character:a`等を付け、`resolve_tag_scope`でキャラの`any_strict`を強制する。`resolve_write_tag_scope`はタグ・文書・observation_scopesの偽造を拒否できる。ただしこれらは自動で付く設定ではなく、自作extensionでCoreの認証済みキャラと対応させる。[H07]

scopeの選択は次のとおり。

| 選択 | 観測と推奨 |
| --- | --- |
| `combined` | `[A,B]`の共有事実は`any_strict(A)`でも読めることを実測。公式説明の「単独tagにno match」は単純なrecallではなく、単独scopeとして独立統合されない意味と区別する。さらに共有観測へA専用の詳細をfoldするとBへ本文が漏れた。複数知覚集合が混在する共有bankのM6境界には採用しない |
| `per_tag` | キャラtagだけを付ける小規模例ではA／Bそれぞれのobservation・chunkを分離できた。sessionやtrustなど他のtagも付けると、それぞれのtagだけのscopeまで生成するため注意 |
| custom | 共有bankの推奨。`[["character:a"],["character:b"]]`のように知覚を許されたキャラscopeだけを明示。trustの異なる派生を分けるなら`[character,tier/source-kind]`の複合scopeにして、原文から独立に読めるscopeを増やさない |
| globs | custom配列内の`character:*`等のパターン。単独の`"custom"`／`"glob"`というモード名を渡す仕様ではない。globが複数tagに一致すると1 scope内にまとめるので、`[["character:*"]]`はper-character分割の代わりにならない。[H08] |
| `all_combinations` | scopeが増え、不要な共有scopeも作る。明示customより検証負担が大きく、M6の既定にしない |
| `shared` | untagged観測へ全tagの情報を集める。strictなら観測が見えず、default anyへ緩めると別キャラの秘密が見えた。複数キャラ共有bankのM6には使わない。キャラ別bank内でのsession横断統合なら候補 |

**scope missionは秘匿境界ではない。** `consolidation_strategies`はscope別の抽出・観測missionと上限等を設定する機能であり、すでに範囲外の原文をモデルに渡したことを取り消せない。[H05] 出典session IDはmetadataに入れ、知覚scopeを不用意に分割するtagにはしない。

chunkの判定単位は**document側のtags**である。[H11] 1 session文書にAだけの本文とBだけの本文を混ぜてA/B両tagを付けると、原文の内部に知覚境界はない。Receiptのprefixや知覚が異なるTurnを同じraw documentに詰め込まない。共有文書に部分可視の原文を置かず、必要ならprojection文書を分割する。

自動の内部ジョブは呼出元の強制read scopeを継承せず、`internal=True`ではscope制限を受けない。[H07] **保存時の分離とobservation scopeが必要**で、APIのread filterだけでは十分でない。

### Reflection／Beliefの代替範囲

observationsは複数のfactsを統合した「beliefs」と公式に説明される。ただし、これは製品用語としてのbeliefであり、本ドメインのheld Beliefの認定ではない。Factのまとめ・矛盾整理・傾向検出を任せ、内省入力に使うのは妥当である。[H05]

reflectはmission、directives、dispositionを使って検索と推論を行う。`response_schema`でclaimsとevidence IDsの形を指定でき、`include.facts`で回答全体のbased_onを得られる。ただしstructured_outputはmarkdown回答からの追加抽出であり、欠落／失敗もある。引用IDが実在・取得済みでも「この主張を原文が支持する」という意味の検証にはならない。[H16]、[H17]

推奨は、reflect出力を未採用候補として受け、各主張を元document/chunkまで解決し、必要な原文を再読してからReflectionを確定する方式。葉がない候補は採用しない。Beliefのprovisional／held／abandonedと改訂・contestedは別の状態機械で管理し、mental modelはその状態から作る要約cacheにする。

mental modelは保存済みreflect回答で、自動refresh、cron、最小refresh間隔、原文chunk取得、他mental modelの参照除外、履歴がある。[H12]、[H15] R4対策には`include_chunks=true`、R1/R3対策には`exclude_mental_models=true`などを使えるが、主張別の保証は外側に残る。履歴は既定50件の上限があり、完全なIdentityHistoryとは扱わない。

### principle 3／G1とobservation

HindsightにはドメインのTrust Tier A〜Fはない。worldは外界の情報、experienceはbank主体の経験、observationは統合した知識であり、直接性や確認済みの区別そのものではない。[H04] とくにobservationの生成は推論であり、何度一致してもdirect_user／確認済みへ変更してはいけない。

native `proof_count`は現在のsource memory UUID集合の件数で、同じ話者の反復や同じ往復からの分割factsも数え得る。[H06] これは検索のevidence strengthに使われるが、ドメインの独立根拠数やconfidenceそのものではない。[H13] 元の出自は外側に保存し、反復やmental model refreshがTierを変える経路を作らない。

tagsは分離には使えるが、custom scopeからtrust/source-kind tagを落とせばobservation側にも残らない。observationのmetadataが元の任意metadataをそのまま継承する保証もなく、生成時の引数は本文・出典・scope・時間等である。[H06] 信頼は原文出自のサイドカー台帳から解決し、混在した情報は推論として保守的に扱う。G1を「promptで指示したから満たした」と判定しない。

## 実PostgreSQLでの確認

再現コードは[probe](../../tools/research/phase1_oss/probe_hindsight_domain.py)と[runner](../../tools/research/phase1_oss/run-hindsight-domain.sh)。結果は[JSON](../reports/2026-10-26-phase1-oss/hindsight-domain-results.json)。

Hindsightのtracked sourceは変更せず、native retain（chunks）→auto submit→`WorkerTaskBackend`→標準`WorkerPoller.claim_batch/execute_task`→標準consolidator→実PostgreSQLを通した。pollerの常駐loopだけを有限回のdrainに置き換え、active tasks・DB operationの完了を確認した。**observationのSQL直接seed、consolidator関数の直接実行、DB書込のスタブ化はしていない。**

LLM provider境界のみfixtureに置換した。言い換え／否定／主体／数値の意味判定は既知の合成例を判定するfixture。384次元の全候補同一ベクトルでcosine=1にし、近似候補の検索後にmerge/keep判定へ進ませる。これは閾値0.97付近の検索再現率も、自然言語理解の精度も測っていない。保持はchunksなのでconcise/custom等のLLM抽出品質も測っていない。

環境はPython 3.12.3、PostgreSQL 18.6、pgvector 0.8.2、rerankerはRRF、全文検索backendはnative。PGroongaが使用可能なイメージだが**今回PGroonga検索を有効化してはいない**。実際のDB拡張・versionは結果JSONに記録した。初期runのイメージdigestは`sha256:52000025fc027d9c9ca22ba99e4c4e0409efb9a23e773ab096a43da81a648454`。この指定のイメージをローカルで解決できなかったため、最終runでは取得済みの`sha256:2c8eb6e2ae7d7736b0689b6711b0d871af3f90906f6b4d6a20139179da614cd9`を明示した。新たなpull/buildはしていない。DBは新規・tmpfs・`--network none`・Unix socketのみ。PythonはAF_UNIX以外のconnectを拒否し、実モデル・外部APIを呼んでいない。作成したコンテナは各run終了時に削除し、既存LiveKitコンテナには触れていない。

### 件数の意味

| 集計 | PASS | FAIL | SKIP | NOT RUN |
| --- | ---: | ---: | ---: | ---: |
| 最終probeの観測実行 `execution` | 33 | 0 | 0 | 0 |
| probeで検証した条件 `assertion` | 27 | 6 | 0 | 0 |
| 明示した未実施の評価領域 | 0 | 0 | 0 | 5 |

観測実行PASSは結果を取得できた意味であり、ドメイン適合を意味しない。assertion PASSにも「危険設定で他キャラの情報が返る」という陽性対照が1件ある。assertion FAILの6件のうち1件は`shared`でstrictを使うと観測が見えないという**設定の不適合**で、他は削除競合・再送・combinedの知覚漏れ・appendの要件未充足。候補全体の合否を件数比では決めない。

| 要求 | 主な結果 | 判定 |
| --- | --- | --- |
| a：削除後の再統合完了 | 自動生成した2出典のobservationを削除。古い本文は直ちに消え、生存factが再queueされ、worker完了後は生存factだけによる1 observationに再生成。秘密文字列がmemory_unitsにもrecallにもない | 条件PASS |
| b：出典が全部消える統合競合 | LLM応答直前で待機→文書削除→応答解放。observationは作られず、recallにも戻らない | 条件PASS |
| b：一部出典だけ消える統合競合 | 下記の手順で削除済み秘密がobservationとrecallに残った | 条件FAIL |
| b：retain競合・削除後再送 | 既存文書のforce_reextractをembeddingで待機→削除→再開すると同ID文書・observationが復活。改めて削除して同ID・同本文をretainしても復活 | 2条件FAIL |
| c：per_tag／combined＋strict、主題が異なる入力 | A専用、B専用、共有の合成文書をretain。A/Bそれぞれで他方のobservation・原文がなく、許されたchunkが返る。combinedでも共有文書は両者に見える | この入力では条件PASS |
| c：共有観測＋同主題の私的詳細 | AB共有観測の後でAだけの詳細を統合。per_tagとキャラ別customではAだけが詳細を取得できたが、combinedではABタグの観測本文へ混入しBへ返った。Bに私的chunkは返らない | per_tag／customはPASS、combinedはFAIL |
| c：強制read/write scope | 呼出元がtagsを付けなくても他方の記憶／文書を読めず、Bタグを偽造するAのretainはOperationValidationErrorで拒否 | 条件PASS |
| c：shared | strictでは共有observationも不可視。default anyにするとB専用のobservationをAへ返すことを確認 | strictでの必要観測取得はFAIL。漏洩の陽性対照は観測PASS・M6不適合 |
| d：自動統合 | 7 raw facts→5 observations。茶の好みの3言及は1観測・3出典。2匹／3匹、否定、赤という異なる主体は別観測。observations-only recallも5件 | 条件PASS。意味品質はNOT RUN |
| e：append／replace／reprocess | appendは既存本文を保持するが同じTurn再送で本文を再追記。replaceは旧本文・旧派生を除去。reprocessは原文を維持してmemory UUIDを作り直す | 保持・置換・再生成はPASS。appendの冪等性はFAIL |

### 一部出典が削除される競合の再現と原因

1. 同じbankにdocument `doomed`＝「青はお茶が好き。秘密符号=MIXED_SECRET」、`kept`＝「青はお茶が好き。」をretainし、auto-consolidationをpendingにする。
2. workerが両factを読み、fixtureが両者を1つに統合する応答を返す直前でEvent待機する。
3. `delete_document("doomed")`を完了させる。この時点ではまだobservationが存在しない。
4. 応答を解放してworkerを完了まで進める。
5. `doomed`のfact UUIDは消えるが、**本文「青はお茶が好き。秘密符号=MIXED_SECRET」・source_memory_ids=[keptのUUID]**のobservationが新規作成され、recallに返る。

コードはsourceが全滅したときはcreateをskipするが、一部だけ生きていればsource一覧を縮め、LLMが古い全sourceから作った本文は再生成せず挿入する。[H06] `_sources_changed_since_read`も削除をchangedとみなさず、個別のliveness checkに委ねる。[H24] したがって「dangling referenceはない」ことと「削除内容が本文から消える」ことは別である。

本probeは既知の合成入力に正しく対応するfixture応答を返している。モデルの幻覚を必要とせず、DBのロックだけでこの残留を防げるとの保証を反証している。観測の作成完了後に削除する通常経路は、依存observationを削除して再統合できるので問題を再現しない。

これ以外の全scheduleを網羅した競合安全性はNOT RUN。fixは本調査では上流／製品のどちらにも入れていない。

### combinedで共有観測へ私的詳細が混入する反例

AB両者のdocumentから「青はお茶が好き。」を先に統合し、Aだけのdocument「青はお茶が好き。秘密符号=A_ONLY_DETAIL」を追加して自動統合を進める。fixtureが同主題の情報をmergeと判定すると、combinedでは観測のtagがABのまま詳細が追加され、Bの`any_strict` recallにも私的文字列が返る。chunk側のdocumentタグ検査は正しく動き、Bには私的原文を返さなかった。

原因は、観測候補検索の`all_strict(A)`がABのsupersetを許し、foldは既存観測のtagを保持したまま新しいsourceと本文を加えること。[H28] 同一の**厳密な**tag集合だけでreconciliationするという保証にはなっていない。UPDATE経路も既存tagと新sourceのtagをunionする実装である（consolidator.py:3176–3212）。

これは**fixtureがmergeを返した場合の機械的な境界検証**であり、実モデルでの発生率や意味判定の品質は測っていない。認可の成立をモデルのkeep判断だけに依存できないため、共有bank案では、観測の全scopeをキャラ別singletonに限定する。`per_tag`と明示customの同じ合成例は漏れず、scopeがsuperset関係になるcombinedとは区別する。既存のcombined観測が残った状態で設定だけを変更せず、再構築する。

### document運用とE1の両立

| 運用 | 利点 | 必要な制御 |
| --- | --- | --- |
| 1 Turn＝1 document | 個別削除と知覚別射影が容易。reprocessはコピーを再抽出するだけなのでEvidenceのE1を破らない | 文脈なしの短いTurnの意味解決は別途必要。ADR0023の1往復を採るなら、返信先を文脈として合成したprojection documentと、その依存Turn集合を登録する。contextに入れたTurnを削除した場合も再構築対象にする |
| 1往復＝1 projection document | 返信と元Turnの意味を一緒に抽出できる。厳密な主張別出典を求めず、両Turnを削除依存集合に含められる | 知覚／Receipt範囲を両Turnごとに切る。元Turn削除が複数の往復文書へ及ぶ逆引きを持つ。1往復は重複排除の機会IDとして残す |
| 1 session＝1 document、append | 会話を順に追加でき、native delta retainが未変更chunkを再抽出しない | Evidenceを別に追記保存し、Turn ID付きoutboxで重複appendを止める。知覚範囲・配達範囲が同じキャラ別の本文だけを送る。個別Turn削除時は生存Turnから全文replaceし、依存観測／cacheを再構築する |
| 1 session＝1 document、毎回全文replace | 原文からの再生成という今回の緩和基準に合う。appendのexactly-onceに頼らずよい | 更新epochと削除tombstone、古い全文snapshotの遅延到着を拒否する制御が必要。再抽出量・日本語chunk境界・p95への影響は要評価 |

**appendを選んだだけでE1を満たすわけではない。** 同じTurn IDを二重に受けない台帳が必要である。逆にprojectionにreplace/reprocessを使っても、原文の正本が不変ならE1には抵触しない。正本の訂正は作者削除＋新規Turnで表し、projectionの技術的再生成と区別する。

## Hindsightを中核にする構成案

### 案A：共通Evidence＋キャラ別bank＋薄い統制サービス（推奨）

Core→Memoryの自作APIがSession/Turn/ReceiptをPostgreSQLの共通台帳へ追記し、outboxからキャラ別projectionを作る。Hindsightには文脈を含む1往復document、またはキャラ別session documentをretainし、抽出・検索・observations統合・documentからの再処理を任せる。HTTP adapter／HttpExtensionのどちらでも構成でき、Hindsightをフォークする前提はない。[H23]

外側に残すものは、共通台帳と知覚／配達射影、M5ゲート、Trust/Epistemics、Relationship/Commitmentの型と相手の検証、人格・Voiceの正本、常駐コンテキストとProvisionRecord、作者削除とtombstone、Phase 2以降のReflection/Belief/Motivation状態機械、Phase 3のChangeBudget/Reviewer/DAGである。保存・検索・統合の内実まで全部自作する必要はない。

想起はHindsightのraw/observation検索結果をキーに、台帳から知覚範囲内の原文・承認済みアンカーを組み立てる。一般Factの表示には観測を優先し、raw factsをそのまま二重提示しない。人格はsession開始時snapshotから返す。**recallのスコアは真実性ではなく検索順位としてだけ利用する。**

削除は当該キャラbankの書込を止め、処理中retain／consolidation／refreshの完了を待ち、対象を消して再統合を完了させてから再開する。古いジョブを終わらせる前のdelete→再開は今回の競合で破れる。停止制御はAPIの入口だけでなく、worker実行・読取応答・cacheにも及ぼす必要がある。

より強い分離には**bank世代**を使う。削除開始時に該当bankの提供を停止し、生存Evidenceだけから新bankを構築し、検証後にadapterのactive generationを切り替える。旧ジョブは旧bankにしか書けず、旧epochの遅延応答は返さない。旧bank・履歴・cacheも最後に削除する。世代IDはenqueue時に実体へ解決し、遅延ジョブが動的aliasを新世代へ解決する実装にしない。この方式は構成提案であり、今回動作検証していない。

| 段階 | 適合度 | 主な追加部分 |
| --- | --- | --- |
| Phase 1 | 記憶エンジンとして高い。サービス全体としては条件付き | Evidence/Receipt/Provision、人格登録・Voice、型とTrust、削除の完了保証。日本語品質とレイテンシが採用ゲート |
| Phase 2 | 生成・集約部品として高い。内省のドメイン保証は中程度 | reflect候補の根拠検証、R1〜R6、Belief状態、ProvenanceGraph。人格はまだ変更しない |
| Phase 3 | 検索・生成基盤として継続利用可能。統制のnative適合度は低い | 審査、独立根拠、ChangeBudget、版・rollback、全層の削除連鎖。Hindsightの自動更新へ人格変更を委ねない |

主要リスクはbank間の重複保存・再構築費用、投影の非同期遅延、削除停止時間、2つのschema間の整合、モデルによる誤統合・抽出漏れ、p95。PostgreSQL一本というADR0007の構成には合うが、別サービス／別トランザクションなのでEvidenceとHindsightの更新を自動的に1 transactionにできるわけではない。outboxとepochで整合させる。

### 案B：共通Evidence＋共有bank＋認可extension＋明示scope

知覚・配達範囲が同じprojection documentを共有bankに入れ、`character:*`を付ける。全入口でread/write scopeを強制し、custom observation_scopesで許可キャラだけに統合する。キャラ以外の情報（session、surface等）はmetadataを基本にする。Hindsightには案Aと同じ検索・統合・reflect・mental modelの部品を任せる。ドメインの状態機械と正本は案Aと同じく外側で管理する。

共有可能な原文・factを重複しにくい利点がある。反面、本人と他キャラで本文の範囲が違う場合は文書の複製が必要であり、厳密なscope管理は省略できない。M6の検証対象はrecallだけでなくget-by-id、chunk、reflect tools、mental model refresh、export、内部consolidationまで広がる。今回のAPI強制scopeとper_tag試験は一部を通しただけで、この全経路の検証は完了していない。

| 段階 | 適合度 | 主なリスク |
| --- | --- | --- |
| Phase 1 | 条件付きで中〜高 | 強制scope・文書分割を正しく設計できれば利用可能。shared／非strict／空tagsの取り違えが漏洩につながる |
| Phase 2 | 条件付きで中 | 内部ジョブは呼出元scopeで絞られない。scopeごとの派生と出自・Trustの保持を全経路で検証する必要 |
| Phase 3 | 統制部分のnative適合度は低い | 案Aと同じ自作DAG・人格統制に加え、共有bankの削除が多キャラの統合を巻き込む。世代再構築の範囲が広くなる |

案Bを選ぶ理由は重複容量や共有計算の削減であり、ドメイン実装の削減ではない。キャラ数・履歴量が小さい段階では、案Aのほうが知覚境界を確認しやすい。

## 前回の「追加実装が残るもの」の見直し

| 前回の項目 | 不要になった／借りられる部分 | 依然として必要な部分 |
| --- | --- | --- |
| 不変の正本 | 現行srcのID・期待版・schemaと完全互換なadapter。projectionを不変にする要求 | E1の台帳または強制書込境界、作者認可、遅延ジョブ・再送・削除tombstone。これは既存実装の都合ではなくドメイン要件 |
| 根拠の精度 | 全主張の厳密なsource_turn_ids一致をPhase 1の採用ゲートにすること | 削除依存のdocument/session集合と原文への逆引き。R1の非空根拠とR3の独立性を判定する段階では、粗い文書数の水増しを防ぐこと |
| 知覚の制御 | tags等をすべて自作し直すこと。キャラ固有の検索DBそのものの実装 | native bank/tags/forced scopeを設定するadapter、perceived_byとReceiptの入力射影、全読取経路の認可。M6は今回も必須 |
| 再計算の完了 | 残存根拠からobservationを作る統合器の自作。通常削除後の自動再統合は実証済み | 削除の受付／完了状態、失敗時の非提供、競合排他／世代、Provision・外部派生の清掃。並行削除は今回新たに未充足と判明 |
| 人格・声 | 要約cache・検索・生成をすべて自作すること。missionやmental modelは補助に利用可能 | P1〜P4、V1、版固定・審査・承認・逐語アンカー |
| 検索規則 | 現在の実装のスコア式との互換、再強化スコア・時刻・rollback | ドメインに残る意味／語彙／重要度／自己関連／信頼等の方針との整合。HindsightのRRF＋乗算boostは加重和と同じではないため、厳密に採るならrerank側に追加。[H13] |
| 同一Factの扱い | 即時の一意Fact挿入・再強化の厳密再現・統合器自作 | native非同期統合を利用し、表示／検索に観測を使う。実モデルで誤統合・統合漏れ・再生成の品質を評価する |
| Phase 2/3の完全連鎖 | Phase 1で完全人格DAGまで必須とすること | ADR0019どおりPhase 1は記憶とProvision、Phase 2は内省削除の手動確認を残し、Phase 3で全連鎖を完成させる |

この調査から「既存の実装を捨てる費用が高いから採用しない」とは結論しない。また、対応表の行数を開発工数の割合に換算しない。

## 次に検証すべきことと未検証

1. **実モデル品質（NOT RUN）**：明示許可を別途得てローカル多言語embedding／rerankerと抽出・統合・reflectモデルを使う。日本語の言い換え、短い同意、同名別人、数値・否定、時間更新、自己発話、ささやき、Receiptのprefix、thought、未知情報での棄権を評価する。同一Fact統合率と別Fact誤統合率を別に測る。公式はbge-m3／bge-reranker-v2-m3／PGroongaを案内するが、今回の推奨品質を実証したわけではない。[H25]
2. **全経路p95（NOT RUN）**：100／1,000件を出発点に、cold/warm・日本語入力・並行retain/統合/削除で測る。embedding、recall、原文／アンカー取得、信頼検証、提供記録、通信まで含める。目標p50 150ms／p95 300ms、必達p95 600ms、Coreの400ms打切りを別々に評価する。公式100〜600msは構成条件の目安でありp95の保証ではない。[H26] `budget=low/mid/high`、reranker有無、RRF、PGroongaを比較する。
3. **競合scheduleの網羅（NOT RUN）**：今回の待機点を設けた順序以外にdelete/retain/append/replace/reprocess/edit/invalidations、複数worker、失敗・再試行、DB切断、refreshを組み合わせる。tombstone＋停止／世代方式を実装した合成probeで削除本文が一切提供されないことを先に確認する。今回のFAILは解消していない。
4. **Reflection/Belief/人格の実装と全DAG（NOT RUN）**：R1/R3/R4、trust混在、重複した葉、contested遷移、ChangeBudget、Reviewer、Voice/Provision/人格履歴までの削除を検証する。API形状とソース調査だけでPhase 2/3をPASSにしていない。
5. **常駐workerの障害復旧（NOT RUN）**：native claim/executeは実行したが、workerプロセスのkill、lease期限、再起動、cron、webhook配信、mental modelの実refreshは試していない。shared bankのcustom/glob/all_combinationsの網羅的動作（customの同主題3文書例は実施済み）、機微データのrouting、PGroongaの日本語検索品質もここまでのprobeでは未実施。

## 再現と変更範囲

```bash
OSS_PROBE_POSTGRES_IMAGE=sha256:2c8eb6e2ae7d7736b0689b6711b0d871af3f90906f6b4d6a20139179da614cd9 \
  bash tools/research/phase1_oss/run-hindsight-domain.sh
# 結果：/tmp/phase1-oss-reassessment/hindsight-domain-results.json
```

必要なら`OSS_DOMAIN_OUTPUT`で別ファイルを指定する。runnerは固定upstream HEADとtracked差分なしを確認する。`execution`にFAILがあれば起動／probe失敗、`assertion`のFAILは観測した条件の未充足であり、runnerの終了0だけで採用可とはしない。再開後の任意再実行は同イメージのローカル欠落でrunner終了125（起動FAIL）、probe本体はNOT RUNとなった。既存の最終JSONは上書きせず保存した。再現には同イメージの用意、または別イメージIDを記録した検証が残る。準備・fixture修正のrunと最終結果は[試行記録](../reports/2026-10-26-phase1-oss/hindsight-domain-attempts.json)で区別する。

変更は本レポート、前回からの導線、研究probe/README、合成結果JSONのみ。製品src/tests/migrations・承認済みADR・private-knowledgeは変更していない。ローカルcommitのみで、push・PR・外部共有はしない。今回の研究用チェックと製品回帰suiteの実施結果は[validation](../reports/2026-10-26-phase1-oss/hindsight-domain-validation.json)へ記録する。 UTは引継ぎの完了ログで1,210件PASS、IT1は進行停止したsandbox内の試行を中断後、sandbox外の再実行で1,139件PASS。初回未完走をPASSに数えない。PostgreSQLも引き継いだ試験が825件PASSで完走した（1,231.69秒）。製品回帰は合計3,174件PASS、IT2／STはNOT RUN。今回の所有プロセス・コンテナ・一時DB socketの残存はなく、起動失敗も同JSONに別記する。

## 固定ソースの根拠

すべて`f29ba70c825094c839daaf27b2b5a564de46fb4e`。ローカルcloneは`/tmp/phase1-oss-reassessment/hindsight`。リンク先は固定版を示すためのもので、調査時はこのローカル内容を参照した。

- **H01**：`hindsight-docs/docs/developer/retain.md:18,53,109,281–325`。factsの抽出、world/experience、links、mission、5抽出モード、再抽出。[固定ソース][H01]
- **H02**：`hindsight-docs/docs/developer/api/retain.mdx:48–67,110–145`。会話文書、metadata、document ID、replace／append。[固定ソース][H02]
- **H03**：`hindsight-docs/docs/developer/api/documents.mdx:23–41,200–207`。raw chunk保存とdocument history。履歴は本文を持たず、非同期operationがない変更は記録されない。[固定ソース][H03]
- **H04**：`hindsight-api-slim/hindsight_api/engine/response_models.py:397–468`。MemoryFactの型、時間・metadata・chunk/source参照。[固定ソース][H04]
- **H05**：`hindsight-docs/docs/developer/observations.mdx:22–64,159–175,203–247`。観測、reconciliation 0.97／PG限定／scope、strategy。[固定ソース][H05]
- **H06**：`hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py:3917–3947`。生存sourceだけに縮めた後も本文をそのまま挿入する箇所。proof_countの意味。[固定ソース][H06]
- **H07**：`hindsight-api-slim/hindsight_api/extensions/operation_validator.py:965–1034`。read/write scope、全入口への適用、internal bypass。[固定ソース][H07]
- **H08**：`hindsight-docs/docs/developer/api/retain.mdx:175–275`。observation_scopesのモードとcustom/globの意味。[固定ソース][H08]
- **H09**：`hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py:3459–3537`。native consolidationの候補recallがstrict tagを使う。[固定ソース][H09]
- **H10**：`hindsight-api-slim/hindsight_api/engine/memory_engine.py:11276–11423`。文書削除・前後のobservation失効・auto再統合投入。[固定ソース][H10]
- **H11**：`hindsight-docs/docs/developer/api/recall.mdx:153–168,193–214`。chunkのdocumentタグ確認・切断、source facts、strict/exact。[固定ソース][H11]
- **H12**：`hindsight-docs/docs/developer/api/mental-models.mdx:24–45,108–139`。保存済みreflect、refresh・cron・interval・scope・原文・他model除外。[固定ソース][H12]
- **H13**：`hindsight-api-slim/hindsight_api/engine/response_models.py:268–305`の3 disposition値と検索スコア、`hindsight-docs/docs/developer/retrieval.md:219–309`のRRF／乗算boost。[disposition][H13]、[scoring][H13b]
- **H14**：`hindsight-docs/docs/developer/observations.mdx:78–109`。矛盾を履歴込みで書き直す観測の挙動。domainのvalid_to／関係型の契約とは別。[固定ソース][H14]
- **H15**：`hindsight-docs/docs/developer/api/mental-models.mdx:750–789`。変更履歴・保持上限。[固定ソース][H15]
- **H16**：`hindsight-docs/docs/developer/reflect.mdx:97–181`。mission／directives／disposition。[固定ソース][H16]
- **H17**：`hindsight-docs/docs/developer/api/reflect.mdx:79,207,268–280`。structured_output、based_onと引用ID検証。[固定ソース][H17]
- **H18**：`hindsight-docs/docs/developer/api/operations.mdx:1`、`api/webhooks.mdx:1`。非同期operation管理・通知。[operations][H18]、[webhooks][H18b]
- **H19**：`hindsight-api-slim/hindsight_api/engine/memories/pg/writes.py:243–332`。関連観測・そのhistoryを消し、生存出典を再queue。[固定ソース][H19]
- **H20**：`hindsight-api-slim/hindsight_api/engine/memory_engine.py:14012–14121`。保存した原文・retain_paramsからforce_reextractで再処理。[固定ソース][H20]
- **H21**：`hindsight-api-slim/hindsight_api/engine/memory_engine.py:20989–21068`。根拠が消えた自動refresh対象modelへのbest-effort再投入。手動refresh modelは対象外。[固定ソース][H21]
- **H22**：`hindsight-docs/docs/developer/memory-defense/index.md:7–47`。regexによるsecret/PII redact/block、既定OFF、正本コピーへの注意。[固定ソース][H22]
- **H23**：`hindsight-docs/docs/developer/extensions.md:42–74,88–116,304–355`。Http／OperationValidator／LLMRequest extension。[固定ソース][H23]
- **H24**：`hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py:722–744,2904–2940`。edit判定と削除は別で、sourceが一部消えた応答を全面破棄しない。[固定ソース][H24]
- **H25**：`hindsight-docs/docs/developer/multilingual.mdx:85–145`。多言語モデル・PGroonga。既定embedding／rerankerは英語用。[固定ソース][H25]
- **H26**：`hindsight-docs/docs/developer/performance.md:26–32,198–208`。100–600msの目安とbudget。[固定ソース][H26]
- **H28**：`hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py:293–415`のall_strict候補とfold、`hindsight-api-slim/hindsight_api/engine/memories/pg/consolidation.py:150–192`の既存tagを変えない更新。[候補][H28]、[書込][H28b]
- **H27**：`hindsight-docs/docs/developer/api/memories.mdx:101–127`。edit／invalidate／restore、observationは直接編集不可。[固定ソース][H27]

[H01]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/retain.md#L281
[H02]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/retain.mdx#L110
[H03]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/documents.mdx#L23
[H04]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/response_models.py#L397
[H05]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/observations.mdx#L22
[H06]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L3917
[H07]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/extensions/operation_validator.py#L965
[H08]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/retain.mdx#L175
[H09]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L3459
[H10]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L11276
[H11]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/recall.mdx#L153
[H12]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/mental-models.mdx#L108
[H13]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/response_models.py#L268
[H13b]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/retrieval.md#L219
[H14]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/observations.mdx#L78
[H15]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/mental-models.mdx#L750
[H16]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/reflect.mdx#L97
[H17]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/reflect.mdx#L207
[H18]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/operations.mdx#L1
[H18b]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/webhooks.mdx#L1
[H19]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memories/pg/writes.py#L243
[H20]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L14012
[H21]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L20989
[H22]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/memory-defense/index.md#L7
[H23]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/extensions.md#L42
[H24]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L722
[H25]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/multilingual.mdx#L85
[H26]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/performance.md#L26
[H27]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/memories.mdx#L101

[H28]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L293
[H28b]: https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memories/pg/consolidation.py#L150
