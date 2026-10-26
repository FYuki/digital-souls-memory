# Phase 1のOSS採用再評価（Hindsight追加）

2026-10-26。調査・提案であり、ADR 0002の変更や採用決定ではない。

## 判断

**新規に保存・抽出・検索を組むなら、Hindsightを最初に試す価値がある。** PostgreSQL上で原文・検索・出典削除後のobservation失効まで借りられる。一方、**Phase 1のサービス全体をそのまま置き換えられる候補は確認できなかった。** 人格・声・配達・提供記録だけでなく、不変のTurn、正確な出典、知覚範囲は独自の契約として残る。同一Factの複数言及をまとめる仕組みは、各OSSの統合・重複解消を使えるか評価する。

- **Hindsight：条件付きで有力。** Evidence・Identity・Voiceを独自側に保ち、派生記憶・検索の下回りとして接続する案を優先する。フォークして正本まで移す案は、不変性・ID・削除の契約変更が大きい。
- **Graphiti：時間付き事実グラフが必要なら有力。** 最新版の削除処理は旧調査より改善。ただしサマリ再構築失敗時に古い本文が残るため、そのまま削除完了にはできない。
- **Mem0：抽出時の重複抑制を使う候補。** 同一の抽出Factはhashで重複登録を抑制できる。すでに投入した別表現のFactを後から統合する点は、Hindsight／Graphitiより確認範囲が狭い。履歴削除・依存失効は独自実装が必要。
- **Letta Code：会話ランタイムを含む構成を選ぶ場合の候補。** Memoryだけを独立サービスとして置く現在の境界では追加責務が大きい。固定人格を保護する機構は存在し、一律に「人格を書き換えるから使えない」とは評価しない。
- **Smart Memory：キャラ記憶の設計・部品の参考。** 出典範囲・旧記憶失効・キャラ別知識を持つ。独立したMemoryサービスへ移すにはST依存と契約差の解消が必要。
- **CharMemory：ST上の体験を早く作る用途向け。** 現在の独立サービスの正本・出典・削除契約との距離は大きい。
- **OpenVault：旧比較対象は参照不能。** 現在見つかる同名OSSを別候補として評価した。目撃者・メッセージIDは持つが、STのメタデータを中心とする構成であり、そのままの置換は見込まない。

**新しい基準では、同一Factの整理を自作必須とは評価しない。** HindsightとGraphitiは標準の統合機構を借りる見込みがあり、再強化の厳密互換を求めた評価より採用への距離は縮まる。

これは**構成と契約の適合に基づく暫定順位**で、想起品質・遅延・総開発工数の順位ではない。現在すでに存在するPhase 1実装をHindsightへ置き換える判断は保留する。構築当初の選択と、今の置換を分けて考える。

## 評価基準と対象版

要求の固定点は `origin/epic/phase1-consistency` の `4c8493916fa8350cd610a97b68351a5a14f22a92`。作業開始時のmain checkoutは古かったため、fetch後のepicから調査用worktreeを分離した。

- [Phase 1の範囲](../domain-model.md#段階的な進め方)：Evidence、派生記憶と想起、人格初期登録、代表発話・逐語アンカー、短期状態、登録時チェック、常駐／ターンAPI、作者削除。
- [ADR 0019](../decisions/0019-phase1-deletion.md)：出典を直接たどり、残る根拠から再計算するか失効させる。
- [ADR 0022](../decisions/0022-perceived-by.md)：Turn単位の知覚、内心、配達途中の可視範囲。
- [ADR 0023](../decisions/0023-extraction-unit-and-independent-evidence.md)：1往復を抽出単位とし、文脈に使っただけのTurnを根拠にしない。機会と話者で独立根拠を数える。
- **2026-10-26の本人指示で評価基準を修正。** 再強化は除外する。同じFactへの複数言及を1つの事実として扱える仕組みが要求で、応答速度を優先した取り込み時の重複は許容し、非同期の統合でもよい。スコアの加算・last_reinforced_at・寄与時刻の巻き戻しは採用条件にしない。[ADR 0024](../decisions/0024-restatement-as-reinforcement.md)の既存実装との厳密互換性を比較基準にしない。製品仕様・コードそのものは今回変更していない。
- [ADR 0017](../decisions/0017-recall-latency-budget.md)：ターン想起の必達p95 600ms。今回の合否測定は未実施。

**内省・人格成長・人格版まで含む完全な削除連鎖はPhase 2／3であり、Phase 1の不採用条件にしない。** また、10/21以降に具体化された0022〜0024を、10/19時点の不採用判断の正当化へ遡及させない。10/20当初のPhase 1だけでも、人格登録・声・配達／提供記録・削除は独自部分として残るが、その時点で全面自作が最短だったとの工数比較はない。

| 候補 | 固定commit（完全値はlock） | 公称版 | ライセンス表記／注意 |
| --- | --- | --- | --- |
| Hindsight | `f29ba70c8250` | api-slim 1.1.0 | MIT |
| Graphiti | `5c27edfa8d3e` | 0.31.2 | Apache-2.0 |
| Mem0 | `2c81af3bbe27` | 2.2.1 | Apache-2.0。同じ版番号でも旧probeとcommitが異なる |
| Letta Code | `1fb6ea4d3094` | 0.34.24 | Apache-2.0 |
| Smart Memory | `a7f6c8e67efb` | 1.8.3 | AGPL-3.0-or-later |
| CharMemory | `35fcf22bf0a5` | manifest 2.3.1 | 今回のtreeにLICENSEなし。権利条件は未確定 |
| OpenVault（unkarelian） | `713873319340` | 0.2.0 | AGPL-3.0。旧ADRの参照先との同一性は未確認 |

[sources.lock.json](../../tools/research/phase1_oss/sources.lock.json)にURLと完全SHAを保存。Mem0のREADMEのベンチマークにはmanaged platformの独自最適化を含むと明記されているため、OSS版の品質証明へ流用していない。

## 機能別のマッチ

「部品あり」は同等の製品契約を満たす意味ではない。「独自」はラッパー・schema・ユースケースの実装が必要。「未検証」は不合格とは区別する。

| Phase 1の契約 | Hindsight | Graphiti | Mem0 | Letta Code | Smart Memory | CharMemory | OpenVault現候補 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 原文を逐語保持 | document／chunkあり | Episodeあり | infer=falseで保存可能 | 会話履歴あり | STの原文履歴に依存 | STの原文履歴に依存 | STの原文履歴に依存 |
| Turnの変更禁止・再送・削除後の復活防止 | 独自。置換・再登録を実測 | 独自 | 独自。Factの一時重複とは分ける | 独自契約へ変換 | 独自契約へ変換 | 独自契約へ変換 | 独自契約へ変換 |
| 派生物→実際に支持するTurn | document/chunk/observationの参照あり。Turn精度は独自 | Episode→fact参照あり。Turn精度は独自 | metadataのみで整合性は独自 | 記憶ファイル→Turnの契約は独自 | chat＋抽出window。主張単位の精度は不足 | chat/date単位。Turn参照は独自 | 抽出batchのmessage_ids。主張単位の精度は不足 |
| 知覚・内心・配達途中 | strict tags等を組む。配達範囲は独自 | group等を組む。配達範囲は独自 | filterを組む。配達範囲は独自 | 独自 | キャラ別・witness機構あり。配達範囲は独自 | group機能あり。正確な知覚は独自 | witnessあり。正確な知覚は独自 |
| 抽出・意味／語彙・時間検索 | 多くの部品あり。独自抽出・スコア規則との対応は追加 | 多くの部品あり。グラフDBが追加 | 保存・抽出・検索あり。独自規則は追加 | 会話記憶・検索あり。独自規則は追加 | 抽出・hybrid scoringあり | 抽出が中心。検索はST側 | 抽出・簡易／LLM選択あり |
| 同一Factの複数言及を1つとして扱う | observationへの非同期統合・近似重複解消あり | 重複edgeを同じfactへ解決し出典Episodeを追加 | 抽出時の意味重複抑制prompt＋同文hash。投入済み別表現の統合は未確認 | dreamingでファイルを整理。構造化Factとしての統合は未検証 | native reconciliationで同じ内容を1件へ。ST側に統合処理あり | LLMによるmap/reduce consolidationあり | promptで重複抽出を抑制。投入済みFactの統合は未確認 |
| 作者削除・残存根拠から再計算 | native observation失効・再queueを確認。全契約には追加 | 正常時の再構築を確認。失敗時は追加対策必須 | 履歴と依存処理を追加 | 最新runtimeの全削除経路は未検証 | 全runtimeの連鎖削除は未検証 | 出典schemaから追加が必要 | 出典schemaから追加が必要 |
| Identity初期登録・BFAS版・Voice・CharacterState | 独自。bank mission等を同一視しない | 独自 | 独自 | 保護機構は利用可能。登録／版契約は独自 | profile等は参考。登録／版契約は独自 | cardはST側。登録／版契約は独自 | card/感情状態は参考。登録／版契約は独自 |
| resident／turn API・DeliveryReceipt・ProvisionRecord | 独自 | 独自 | 独自 | 独自 | 独立サービスへの移設が必要 | 独立サービスへの移設が必要 | 独立サービスへの移設が必要 |
| 日本語想起品質・実モデル込みp95 | 未検証 | 未検証 | 未検証 | 未検証 | 未検証 | 未検証 | 未検証 |

## Hindsight：追加した候補の評価

### 借りられるもの

同じFactへの複数言及は、生のfactを残しつつ統合済みobservationを1つにする形で扱える。このobservationを本サービスの内省やBeliefと同一視せず、同一事実の投影として使えるかを評価する。異なるFactを同じ話題の要約へまとめただけでは、この要件を満たしたとしない。標準の非同期consolidationに加え、近似重複を既存observationへまとめるreconciliationがある。実DBではnative候補検索→fixtureのmerge判定→native書込みを通し、1つのobservationが2つの言及元を保持し、生の言及も2件残ることを確認した。意味判定はfixtureであり、実モデルでの同値判定精度は未測定。[公式observations](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/observations.mdx)

PostgreSQL＋pgvector上のサービスで、retain／recall／reflectとHTTP・クライアントが用意されている。原文document/chunk、world／experienceの記録、source付きobservationを分け、意味・キーワード・グラフ・時間の検索を持つ。現在のPostgreSQL一本という構成との相性はGraphitiの追加グラフDBよりよい。[公式storage](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/storage.md)、[公式retrieval](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/retrieval.md)

今回の実コードでは、`chunks`モードによりLLM抽出を使わず入力を保存でき、recallの`include_chunks`で原文を返せた。原文は標準で保存される。[retainのモード](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/retain.md)、[raw text設定](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/configuration.mdx)

文書削除はnative memory unitsと関連observationを対象にする。共有observationは、根拠が1つ消えた時点で古い本文を失効させ、残った根拠を再consolidation対象へ戻すことを実PostgreSQLで確認した。**「出典削除をたどれない」という旧候補の一括評価はHindsightには当てはまらない。** [native delete_document](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L11276)、[observation失効実装](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-api-slim/hindsight_api/engine/memories/pg/writes.py#L243)

### 追加実装が残るもの

1. **不変の正本。** retainは同じdocument_idの本文を標準で置換でき、削除後の再登録も通った。Turn ID・期待版・削除tombstone・再送の拒否は独自側に残す。
2. **根拠の精度。** document/chunkへの参照があることと、1往復のうち実際に主張を支えたTurnだけを出典にすることは別。1文書＝1Turnにすれば対応は容易になるが、返信文脈を使う抽出と主張ごとの出典検証は追加が必要。
3. **知覚の制御。** tagsの標準`any`はuntaggedな記録も含むことを確認した。strictなfilter、bank分割や認可extensionを必須にし、配達途中の原文は独自側で切り出す。tags自体を認可の代替にしない。[tagの仕様](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/recall.mdx#L190)
4. **再計算の完了条件。** 古いobservationの失効と残存根拠の再queueまでは確認したが、今回auto-consolidationを無効にしたため、削除後の再consolidationによるobservation生成完了は未検証。削除要求の完了条件・再試行・提供記録の掃除を独自契約へ合わせる。
5. **人格・声と独自検索規則。** bankのmission/dispositionをCoreIdentity／Temperament／BFASの版や代表発話と同一視しない。独自の検索スコアとの対応付けも別途必要。ただし再強化スコア・再強化時刻は今回の採用基準から除外する。[bankの機能](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/memory-banks.mdx)

文書の矛盾もあった。固定版の[retain API説明](https://github.com/vectorize-io/hindsight/blob/f29ba70c825094c839daaf27b2b5a564de46fb4e/hindsight-docs/docs/developer/api/retain.mdx#L31)には原文を保存しない趣旨の記述があるが、設定文書・実装・実DBの結果は原文保存を示す。今回はこちらを実測で解消した。これを理由に原文保存を欠くと判定しない。

## 既存候補の見直し

### Graphiti

同文・同端点のfactはnative resolverが既存edgeを再利用し、複数の出典Episodeを同じedgeへ付ける。言い換えもLLMの重複判定により既存edgeへ解決する経路がある。同文fast pathと、fixtureによる言い換え判定の両方を実行し、同じfactのedge IDへまとまることを確認した。取り込み時の新Factを常に新規レコードとする必要はない。[重複解決実装](https://github.com/getzep/graphiti/blob/5c27edfa8d3e0f49b2571bcebb4261bcc89ee037/graphiti_core/utils/maintenance/edge_operations.py#L864)

最新0.31.2の`remove_episode`は、共有factから削除対象Episodeの参照を取り除き、他のEpisodeが支えるfactを残し、共有Entityのサマリを残存Episodeから再構築する。正常時の動作を実Neo4j＋fixture LLMで確認した。したがって、旧ADRの「共有サマリを再計算しない」は最新コードには当てはまらない。[実装](https://github.com/getzep/graphiti/blob/5c27edfa8d3e0f49b2571bcebb4261bcc89ee037/graphiti_core/graphiti.py#L2174)

ただし、サマリ生成を意図的に失敗させると、削除呼出しは例外にせず戻り、DB上の古いサマリ本文が残った。これは上流の[失敗時テスト](https://github.com/getzep/graphiti/blob/5c27edfa8d3e0f49b2571bcebb4261bcc89ee037/tests/test_graphiti_mock.py#L845)でも意図された動作。削除前の退避・検索停止・成功までの再試行等を独自側で設計する必要がある。

また、低レベル`EpisodicNode.save`→取得では`episode_metadata`が保持されなかった。これは当該経路の観測で、高レベルadd_episodeの全経路が失敗するとの結論にはしない。独自Turn情報をmetadataだけに置かず、アダプタで往復を検証する。[Nodeの実装](https://github.com/getzep/graphiti/blob/5c27edfa8d3e0f49b2571bcebb4261bcc89ee037/graphiti_core/nodes.py#L318)

### Mem0

標準のinfer=trueは既存の関連記憶と直近の抽出内容をモデルへ渡し、意味的に同じなら再抽出しないよう指示する。さらに同一抽出本文のhashを比較して二重登録を抑える。fixture抽出器で2回の言及から同じFactを返すと、1件だけ保存されることを確認した。infer=falseで本文を2回登録できること自体は不採用理由にしない。後の言及の出典metadataは自動では統合されなかったため、出典を両方残すには追加対応が必要。[標準抽出prompt](https://github.com/mem0ai/mem0/blob/2c81af3bbe27d9bccb09c772d583df9e9be72ee5/mem0/configs/prompts.py#L468)

最新固定commitの`infer=False`で日本語本文とsource_turn_ids metadataを保持できた。ただし同じ本文の再追加は新しいUUIDになり、削除はそのレコードだけを消し、同じ出典metadataを持つ別の派生記憶は残った。削除本文はSQLiteのhistoryにも残った。これらは実Qdrant local＋SQLiteで再確認した。[add/delete実装](https://github.com/mem0ai/mem0/blob/2c81af3bbe27d9bccb09c772d583df9e9be72ee5/mem0/memory/main.py)、[history実装](https://github.com/mem0ai/mem0/blob/2c81af3bbe27d9bccb09c772d583df9e9be72ee5/mem0/memory/storage.py)

汎用の保存・検索部品として使う余地は残る。内省や人格成長が標準でないことはPhase 1の除外理由にしない。一方、source metadataを入れられるだけでは、参照整合性や作者削除を満たしたことにはならない。

### Letta Code

現行版には、legacy memoryファイルの`read_only`を使い、既存の固定ファイル編集と保護flagの除去を拒否するvalidatorがある。実関数を直接呼んで両経路を確認した。この結果をmemfs-v2や全runtimeの権限保証へ一般化しない。[validator](https://github.com/letta-ai/letta-code/blob/1fb6ea4d30942ef9220b096ba7f4185f18184fc1/src/memory-frontmatter.ts)

今回の対象は現行Letta Codeで、退役したV1 serverとは分けた。会話ランタイムを含めて採用する案なら価値があるが、Coreが会話を実行しMemoryだけを分離する現在の構成では責務が重なる。旧probeの圧縮要約・MemFS履歴への本文残存は旧版の記録として残し、**現行0.34.24で同じ会話削除試験を実施済みとはしない。** [現行機能と構成](https://github.com/letta-ai/letta-code/blob/1fb6ea4d30942ef9220b096ba7f4185f18184fc1/README.md)

### Smart Memory

現在の1.8.3には、`source_messages`、`source_chat_id`、`witnessed_by`、`valid_from/valid_to`、`supersedes/superseded_by`がある。抽出windowとchat IDを付け、旧記憶を失効させて検索対象から外す。**以前の「原文との対応なし」は訂正する。** [parser](https://github.com/senjinthedragon/Smart-Memory/blob/a7f6c8e67efb0442921d060e048b40fab00caa07/parsers.js)、[出典付与・失効・統合処理](https://github.com/senjinthedragon/Smart-Memory/blob/a7f6c8e67efb0442921d060e048b40fab00caa07/longterm.js#L495)

同一Factを1件へまとめるnative reconciliationと、別のFactを残す対照を実行した。parserの出典・失効fieldと公式parser試験25/25も確認。一方、出典は抽出windowの範囲であり、統合後の合成記憶には最新windowを補うfallbackもあるため、主張ごとの正確な原文根拠と同一視しない。ST依存を切って独立サービスへ移す負担と、配達・削除・正確な原文出典の契約差は残る。全ブラウザーruntimeの削除伝播は今回未実施。

### CharMemory

複数の記憶をLLMで整理するmap/reduce consolidationがある。2つの言及を受けて1つのFactを返すfixture出力を、native orchestratorが扱えることを確認した。ただし統合内容はモデルの出力で、純粋関数の機械的な意味重複判定ではない。[統合orchestrator](https://github.com/bal-spec/sillytavern-character-memory/blob/35fcf22bf0a5b4273fd19e06a2ff82d0ed520ab3/consolidation.js)

native formatはchat・date・bulletsを保存する。serializer/parserを実行すると、本文とchatは往復するが、追加したsource_turn_ids・perceived_byは保持されなかった。ST Data Bankへの書込みを担い、検索はST Vector Storageの責務なので、独立したMemory全体を借りる候補としては範囲が狭い。[形式と責務](https://github.com/bal-spec/sillytavern-character-memory/blob/35fcf22bf0a5b4273fd19e06a2ff82d0ed520ab3/docs/architecture.md)、[serializer/parser](https://github.com/bal-spec/sillytavern-character-memory/blob/35fcf22bf0a5b4273fd19e06a2ff82d0ed520ab3/lib.js)

### OpenVault

旧ADRの `https://github.com/7n1m4/vault` はGitHub API・Webとも404。代わりに確認した `unkarelian/openvault` と同一である証拠はなく、旧候補の最新版と扱わない。

今回の同名候補0.2.0には`message_ids`と`witnesses`があり、native parserの実行で保持を確認した。ただしmessage_idsは入力batch全件で、主張を支えた個別Turnの識別ではない。原文を削除するとの根拠は今回得ておらず、READMEのauto-hideを原文削除と混同しない。記憶はSTのchat metadataに保存される。[README](https://github.com/unkarelian/openvault/blob/7138733193407de4e70e297a056fccff4eb1be1c/README.md)、[parser](https://github.com/unkarelian/openvault/blob/7138733193407de4e70e297a056fccff4eb1be1c/src/extraction/parser.js)

## 動作確認の結果

| 対象 | 実施範囲 | 最終実行 | 契約上の主な結果 |
| --- | --- | --- | --- |
| Hindsight | native engine＋実PostgreSQL、chunks、fixture embedding／同値判定 | PASS・12観測 | 原文・chunk取得・native observation失効は確認。不変性・復活防止は追加 |
| Graphiti | native remove_episode＋実Neo4j、fixture LLM/embedding | PASS・正常／障害2シナリオ | 同一factへの出典統合・共有fact維持・サマリ再構築は確認。生成失敗時の古い本文残存も確認 |
| Mem0 | native Memory＋Qdrant local＋SQLite、fixture embedding／抽出結果 | PASS・9観測 | 抽出Factの重複抑制を確認。削除history・依存本文の残存も確認 |
| ST系＋Letta | native純粋関数。OpenVaultだけID/log依存をmodule stub化 | PASS・12観測 | Fact統合経路、graph field、保護、保存format、batch出典を確認 |
| Smart Memory公式parser試験 | `node test-harness.mjs --parsers --quiet` | PASS・25/25 | parserの既存回帰試験。実抽出品質ではない |
| 実LLM・実embedding・日本語品質・長期人格・p95受入 | 実施していない | NOT RUN | 採用確定の根拠にしない |
| STブラウザー全体・Lettaの最新会話削除 | 実施していない | NOT RUN | 純粋関数の成功から全runtimeへ拡張しない |

**PASSは想定した挙動を観測できた意味で、Phase 1要件のPASSとは限らない。** たとえば「削除後も本文が残る」観測テストはPASSだが、削除契約には不適合である。結果JSONもexecutionとphase1の評価を分けた。

準備中には依存解決・DB起動・fixtureに失敗があった。HindsightとMem0はprotobuf要求版が衝突したためvenvを分離。DBのtmpfs/socket・ログ事前確保を修正し、native型の必須時刻をfixtureへ補った。初回のHindsight可視範囲試験はfixture embeddingと問い合わせが一致せずFAILとなり、意味品質を測らない同文queryへ修正して再試験した。候補コードを変更してPASSにしていない。最終結果は上表、準備失敗を製品の失敗数や品質順位へ算入していない。

証跡： [Hindsight](../reports/2026-10-26-phase1-oss/hindsight-results.json)／[Graphiti](../reports/2026-10-26-phase1-oss/graphiti-results.json)／[Mem0](../reports/2026-10-26-phase1-oss/mem0-results.json)／[ST系・Letta](../reports/2026-10-26-phase1-oss/extensions-results.json)。再現コードと環境条件は[probe README](../../tools/research/phase1_oss/README.md)。

## 採用へ進める場合のゲート

Hindsightを独自の派生検索adapterとして試す案が第一候補。最低限、次を同じ合成入力で現実装と比較してから決める。

1. 実モデルで、1往復からの主張と実際のsource_turn_idsが一致すること。未知情報で棄権でき、日本語の逐語・固有名・日付を保つこと。
2. 本人・他キャラ・途中まで届いた相手の3種類で、知覚外本文が抽出にも想起にも入らないこと。
3. 同じFactを複数回言及したとき、取り込み時に重複しても、非同期処理後に1つの事実として検索・利用できること。同文・言い換えをまとめ、数値・否定・主体が違う別のFactは誤統合しないこと。時刻の再強化やスコア加算は求めない。
4. 文書削除と並行するretain／recall／再処理でも、削除した内容が提供されず復活しないこと。再処理失敗も含めて判定すること。
5. 100／1000件で、原文取得・独自再検証・提供記録を含むp95 600msを満たすこと。fixture embeddingの速さを実モデル性能としないこと。

現時点で「Phase 1を9割満たす」「自作工数を9割削減できる」という数値は出さない。**Hindsightを候補から外す根拠はなく、保存・検索まで全面自作に固定する必要もない。一方、独自ドメイン契約のかなりの部分が残るため、全サービスをフォークで置換する方が安いとの結論にも至っていない。**


## 変更と回帰確認

READMEから本調査への導線、再現probe、固定source・依存・結果JSONを追加した。製品のsrc／既存tests／migrationsと、承認済みADRは変更していない。

最終確認はUT **1,210件**、IT1 **1,139件**、合成PostgreSQL **825件**がPASS。
ruff・format・mypy・lock整合・wheel build・独立環境へのhash付きinstall/import・shell/JS構文・文書内リンク・diff検査もPASS。
最初の中断runはsuite PASSに数えていない。詳細は[validation.json](../reports/2026-10-26-phase1-oss/validation.json)。
