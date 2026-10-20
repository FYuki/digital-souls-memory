# Issue #9 検索品質乖離の調査

## 結論と確認範囲

Memoryのbge-m3 acceptanceは両方式・各3回とも26/55で品質FAILのままである。
不合格29件のうち28件は、適格な正解が合成スコア閾値0.75を通らず検索結果が空になった経路で説明できる。
残る1件は正解5件の全包含に成功し、実スコア順と固定goldの全順序が異なるため失敗した。
禁止ID返却と返却予算による除外は、この29件ではともに0件だった。

背景の「Core 61/62」は回答の品質条件の値である。Core指定版の
`docs/evidence/2026-10-10-semantic-real-model-evaluation-bge-m3.md:131`では検索は各回53/62、
`:134`では回答品質61/62・回答全条件53/62で、検索・回答の全体判定はともにFAILである。
Coreの閾値0.52もcosineへの直接適用ではない。検索同士で比較し、尺度・候補・goldの差を分ける必要がある。

主な説明要因は、合成スコアに対して高い閾値を選ぶtuning規則と、Coreとは異なる検索・採点条件である。
ただし28件の閾値未満スコアは保存されておらず、閾値を下げたacceptanceの改善件数や、
日英横断のモデル自体の品質を数値で確定することはできない。原因への寄与率も未確定である。
仕様と実処理の不一致は今回の確認範囲で特定していない。実装・テスト・固定評価入力は変更していない。
以下の変更候補は親担当の判断待ちであり、採用値・goldの変更や品質PASSの宣言ではない。
実モデルIT2とSTは本調査では **NOT RUN**。

## 参照版と証跡

作業基点はMemory `5f6b9c9`、ブランチは`fix/retrieval-quality-gap`。
保存済みIT2の実行元は`dd635d1e26787e8660507b6547319ee42e65912a`。
Core参照版は`b062a49a32d261d0ce472938c40711c6cb41d7c0`で、読み取りだけを行った。
Coreの測定自体の実行commitは`69e44f58c0bfd3676478851d1ab73dd184bd8eaf`である。
入力4ファイルは`evals/retrieval/provenance.json`のhashと指定Core版の`git show`出力を照合した。

[元の実測記録](2026-10-20-retrieval-it2-host.md)と
[summary](2026-10-20-retrieval-it2-summary.json)を参照し、外部のJSONとgzipを4組とも
`archives`のSHA-256・圧縮前後byte数・展開内容で照合した。
`validate_public_report`でtuningとacceptanceを両モデルとも再検証した。
これは保存済み観測の整合性検査であり、モデル品質のPASSや新しい実モデル測定を意味しない。
e5の部分acceptanceは実行ERRORの記録のまま扱う。

| 外部証跡（`eval-artifacts/2026-10-20/`配下） | JSON SHA-256 |
| --- | --- |
| `2026-10-20-retrieval-it2-bge-m3-tuning.json` | `c8f2adbf885f0a490e96c8f85cde8ea2acd80cdac54c51f369be3b91a5dc8e42` |
| `2026-10-20-retrieval-it2-bge-m3-acceptance.json` | `f52e1705e8cfeab7fae819b64d014f6e637b8b5ad868766214b9ce6216392310` |
| `2026-10-20-retrieval-it2-multilingual-e5-large-tuning.json` | `331d06d839d74f28564d6eef2d8aa039fedf1c9b0e390985362e483a5ee42d1e` |
| `2026-10-20-retrieval-it2-multilingual-e5-large-acceptance.json` | `bd7e05dc1e5ee697816f319ceac1d30b707a7905bf7381ecdafc181d9ef72e12` |

| 固定入力 | SHA-256 |
| --- | --- |
| `evals/retrieval/tuning/suite.json` | `af3ea5271e0603a0726ef6bc0f39bd87bf086a18072cf4559b9ef6a0bcd423ad` |
| `evals/retrieval/acceptance/suite.json` | `3c8b05394dd0fde5fe1551d40d46c4e38caa2f3e1de18ab1f7f8dd5cb442a006` |
| `evals/retrieval/protocol.json` | `8e396df88bf28b559b7c1a537861cd467c921376bf15c94cbb05cf2b69ef3ece` |
| `evals/retrieval/candidates.json` | `a9643f83ba2dbca0cf485f9cd13512ad6f2f3da543db2139382260f5427d3327` |
| `evals/retrieval/provenance.json` | `73463f9417550eaf0540f770abb612c4ae978c1cb30fd689bfda5c3ae4cac775` |

## 実在する検索・選定経路

以下のPythonパスは`src/digital_souls_memory/evaluation/`配下。
現在の観測可能な検索契約は変更せず、各段の値の接続を読み取りで確認した。

| 契約 | 値・状態の接続 | 今回の扱い |
| --- | --- | --- |
| C01・C02 | suiteのquery・memories → `runner.py:77`の`eligible_memories(case)` → `:93`の派生`content`埋め込み → `PostgresSearch.index(filtered, vectors)` | 適格性・本文の所有者を保持。空結果28件でも全正解IDが適格集合にあることを照合 |
| C01・C02 | `adapters/postgres.py:174`の全適格候補への`embedding <=> query_vector` → `score_components` → `rank_results(observations, case['protocol'])` | cosine変換・6項加重和・閾値・順位を保持。近似索引や候補20の打切りはない |
| C01 | 閾値通過候補 → DBから原文・actor・language・offsetを回収 → `runner.py:117`の`bounded_results` → `score_case(...threshold=resolved['threshold'])` | 原文を切らない最大5件・1,500byte契約を保持。29件の予算除外は0 |
| C01・C03 | runnerの`retrieved_hits`と`hits` → `report.py:309`の固定suiteによる復元 → `validate_report`内の`evaluate_case`再計算 → 公開JSON → 本報告 | hashは原文復元にのみ利用。欠落スコアの代用にしない |
| C04・C05 | `parameter_grid` → `tuning_candidates` → tuning実測 → CLIのhash検証と`validate_public_report` → `selected_candidates` → candidate.parameters → `resolved_protocol` → acceptanceの検索・採点・再検証 | tuningだけが選定を所有。acceptanceで再調整しない |

DB所有権はCREATE成功後に獲得し、ExitStackで接続を閉じてから所有DBを削除する。
例外時の後片付けやfixtureへのfallback禁止を変更していない。
Coreのdispatch・回答破棄は別の契約であり、Memoryの検索前snapshotと同等とはしない。
特定の変化をまたぐ同一実体の追従要求はない。

## 不合格29件の原因分類（C01）

候補`bge-m3-pg_bigm`と`bge-m3-pgroonga`、`run_index=1,2,3`の全6 runで、
合否・取得ID・retrieved ID・全失敗フラグ・予算除外が一致した。
表の正解はsuite.goldの`required_ids`、取得は`hits[].memory_id`。
空結果28件は`retrieved_hits=[]`・`hits=[]`・`recall_at_5=0`・`excluded=[]`。
正解が適格で全件採点される実経路から、直接の除外段階を閾値と判断する。
具体的なcosine・合成値と、低閾値時の正解順位は未保存であり再実行が必要である。
`threshold_passed=true`は返却候補に閾値未満がないことの判定で、空集合では真になる。
正解が閾値で除かれていない証明にはならない。

| ケースID | 分類 | 正解ID | 取得ID | 落ちた条件 | 推定原因・不足証拠 |
| --- | --- | --- | --- | --- | --- |
| `paraphrase-weekend-ja` | paraphrase | `weekend-cycle` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-herb` | cross_language | `windowsill-mint` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `multisource-picnic` | multisource | `picnic-items` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `deleted-source` | deleted_source | `deleted-source-visible` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `long-text-explicit-detail` | long_text | `long-note-memory` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `synonym-02` | synonym | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `synonym-03` | synonym | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `synonym-04` | synonym | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `synonym-08` | synonym | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `synonym-10` | synonym | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-03` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-04` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-06` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-08` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-09` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `paraphrase-10` | paraphrase | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-02` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-03` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-04` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-05` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-06` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-07` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-08` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-09` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `cross-language-10` | cross_language | `target` | ∅ | 正解全包含（recall=0） | 閾値0.75で除外。閾値前の成分未保存 |
| `valid-fact-attachment` | fact_attachment | `trip-episode`, `destination-fact` | ∅ | 正解全包含（recall=0）、必須原文欠落 | 閾値0.75で除外。閾値前の成分未保存 |
| `stale-fact-link` | fact_version | `destination-fact` | ∅ | 正解全包含（recall=0）、必須原文欠落 | 閾値0.75で除外。閾値前の成分未保存 |
| `revoked-fact-source` | fact_revocation | `trip-episode` | ∅ | 正解全包含（recall=0）、必須原文欠落 | 閾値0.75で除外。閾値前の成分未保存 |
| `equivalent-band-order` | equivalent_order | `tie-a`, `tie-b`, `tie-c`, `tie-d`, `tie-null` | `tie-null`, `tie-a`, `tie-b`, `tie-d`, `tie-c` | 指定順位（recall=1） | 固定全順序と実スコア順の不一致。閾値・予算・recallは成功 |

`sources_valid=false`の3件は必須原文も空結果に伴って欠落した。
`scoring.py:69`はgold.required_source_spansの包含も検査するため、これだけでは原文回収の故障を示さない。
順位ケースの期待は`tie-a, tie-b, tie-c, tie-d, tie-null`、実際は
`tie-null, tie-a, tie-b, tie-d, tie-c`。各scoreは順に
0.7881942634 / 0.7783157629 / 0.7782615420 / 0.7734461038 / 0.7686660692で、
完全同点ではない。実装はscore降順という現行仕様どおりである。

## スコア尺度とtuning（C01・C02・C04）

Memoryは`retrieval.py:63`の加重和、`:107`のscore以上閾値を使う。
選ばれた条件では、`S=0.75c+0.05l+0.05r+0.05i+0.05s+0.05t`。
ここでcはclampしたcosine、lは語彙一致率、rは新しさ、残り3項はfixtureの属性である。
属性が各0.5なら`S=0.75c+0.05(l+r)+0.075`。
例えばl=0・r=0.652176なら、c=0.6〜0.7はS=0.557609〜0.632609で0.75に届かず、
通過にc≧0.856522が必要になる。これは式による例で、空結果28件の実測値ではない。

Coreは`memory_ranking.py:125`でベクトルを単位化し、`:130`で
`R=1/(1+sqrt(二乗L2))`を計算する。単位ベクトルでは二乗L2=2−2cosineなので、
R≧0.52はcosine≧約0.573964に対応する。0.52をMemoryのcosine閾値や合成閾値として移せない。
Memoryのグリッドは0.20〜0.90を含み、低いスコア域を探索していないわけではない。
問題候補は探索域の欠落より、複数の失敗をどう重み付けして設定を選ぶかにある。

`comparison.py:87`はsources・threshold・forbidden・order・recall・no_matchの
失敗条件をケース内でも別々に加算し、その少なさを最優先する。
その後、分類成功率の最小値、平均recall、平均precision、高い閾値、高いsemanticの順に選ぶ。
これは[評価手順](../retrieval-evaluation.md)に明記された規則と一致する。

下表は保存済みbge tuningの全45設定×両方式×各3回を検算したもの。
各設定の件数は全6 runで一致したため1 run（82ケース）の値を示す。
「禁止」は禁止IDを1件以上返したケース数、「recall」は正解全包含失敗数。
sources・threshold違反は全設定0、order違反は全設定1である。
違反合計はこれらを含み、合格ケース数とは別の尺度である。

| semantic | 閾値 | 合格/82 | 禁止 | recall | no_match | 違反合計 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.55 | 0.20 | 47 | 34 | 0 | 28 | 63 |
| 0.55 | 0.25 | 47 | 34 | 0 | 28 | 63 |
| 0.55 | 0.30 | 47 | 34 | 0 | 28 | 63 |
| 0.55 | 0.35 | 47 | 34 | 0 | 28 | 63 |
| 0.55 | 0.40 | 47 | 34 | 0 | 28 | 63 |
| 0.55 | 0.45 | 44 | 34 | 3 | 28 | 66 |
| 0.55 | 0.50 | 33 | 28 | 20 | 22 | 71 |
| 0.55 | 0.55 | 35 | 22 | 24 | 16 | 63 |
| 0.55 | 0.60 | 33 | 20 | 28 | 14 | 63 |
| 0.55 | 0.65 | 34 | 10 | 37 | 6 | 54 |
| 0.55 | 0.70 | 35 | 4 | 43 | 1 | 49 |
| 0.55 | 0.75 | 36 | 0 | 46 | 0 | 47 |
| 0.55 | 0.80 | 30 | 0 | 52 | 0 | 53 |
| 0.55 | 0.85 | 28 | 0 | 54 | 0 | 55 |
| 0.55 | 0.90 | 28 | 0 | 54 | 0 | 55 |
| 0.65 | 0.20 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.25 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.30 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.35 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.40 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.45 | 47 | 34 | 0 | 28 | 63 |
| 0.65 | 0.50 | 46 | 33 | 2 | 27 | 63 |
| 0.65 | 0.55 | 46 | 24 | 11 | 18 | 54 |
| 0.65 | 0.60 | 37 | 21 | 23 | 15 | 60 |
| 0.65 | 0.65 | 36 | 17 | 28 | 12 | 58 |
| 0.65 | 0.70 | 36 | 6 | 40 | 2 | 49 |
| 0.65 | 0.75 | 37 | 0 | 45 | 0 | 46 |
| 0.65 | 0.80 | 30 | 0 | 52 | 0 | 53 |
| 0.65 | 0.85 | 28 | 0 | 54 | 0 | 55 |
| 0.65 | 0.90 | 28 | 0 | 54 | 0 | 55 |
| 0.75 | 0.20 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.25 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.30 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.35 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.40 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.45 | 47 | 34 | 0 | 28 | 63 |
| 0.75 | 0.50 | 48 | 33 | 0 | 27 | 61 |
| 0.75 | 0.55 | 52 | 28 | 1 | 22 | 52 |
| 0.75 | 0.60 | 54 | 22 | 5 | 16 | 44 |
| 0.75 | 0.65 | 38 | 20 | 23 | 14 | 58 |
| 0.75 | 0.70 | 42 | 9 | 31 | 5 | 46 |
| 0.75 | 0.75 | 40 | 0 | 42 | 0 | 43 |
| 0.75 | 0.80 | 30 | 0 | 52 | 0 | 53 |
| 0.75 | 0.85 | 28 | 0 | 54 | 0 | 55 |
| 0.75 | 0.90 | 28 | 0 | 54 | 0 | 55 |

semantic 0.75・閾値0.60の54/82・44違反より、0.75の40/82・43違反が優先される。
低閾値では同一の該当なしケースで禁止IDとno_matchを二重に数える一方、空結果は主にrecallの1違反になる。
空結果を選びやすい比較上の非対称性はあるが、文書化された規則であり実装不具合とはしていない。
全270 runを独立再集計し、既存selectorの両方式0.75/0.75選択とも一致した。

閾値0.20の各重み・各方式・各3回では、全適格IDが`retrieved_hits`に含まれることも照合した。
このtuning観測なら、原文をsuiteから復元し、既存`rank_results`・`bounded_results`・`score_case`を使う
別の重み・閾値の探索は可能である。ただし新しい実測ではなく、同じ観測成分上の再採点に限られる。
本報告の表は既存45設定の実測集計であり、未実行の設定の成績は主張しない。
acceptanceの空結果にはこの完全性がないため、tuningのスコアを流用して欠落候補を補完しない。

## Coreとの比較と入力処理（C02）

Coreの指定3文書（`docs/memory-evaluation.md`、上記実測証跡、`docs/adr/0024-multilingual-memory-embedding.md`）と
実コードを読み、`provenance.json`で同じ出典IDに対応する55件を照合した。
Coreの共通55件は各回46/55、Memoryは26/55。
同じIDでもFactの役割やgoldが変わっているため、この20件差は単一要因の因果効果ではない。

| 比較軸 | Core（指定版・実測） | Memory（固定評価） | 解釈 |
| --- | --- | --- | --- |
| 検索品質 | 53/62、共通IDは46/55 | 26/55 | 回答61/62と混ぜない |
| 同義語・言い換え・日英 | 各10/10 | 5/10・3/10・0/10 | 同じquery群でも選ばれた閾値の尺度が異なる |
| 該当なし | unrelated 3/10、threshold 0/1 | unrelated 10/10、threshold 1/1 | 高閾値の利得と取りこぼしの交換関係 |
| 長文 | 0/1、正常な空結果 | 0/1、正常な空結果 | Memory固有の原文長破損とは断定できない |
| 検索対象 | Episode/Semanticのnormalized_text。FactはEpisode添付 | DerivedMemory.content。Factも独立候補 | Fact添付成功率を独立Factの検索成功率と同一視しない |
| 候補・返却 | 意味距離候補20・最大5、同等帯0.002内で時刻→ID | 全適格候補を6項採点、score順、完全同点のみID、最大5・1,500byte | 候補20の不足は今回のMemory空結果の原因ではない |
| 品質ゲート | 各回各分類90%以上、必須ゲート全件 | 各回各分類100%、全ケースの指定順位も必須 | 90%へ変えても5/10・3/10・0/10はFAIL |
| 実モデル順位 | top-1の上位5件包含。全順序はfixtureのみ | required_orderの全順序一致 | equivalent-band-orderの1件差を説明 |
| 出典・lifecycle | Factリンク・dispatch直前検証・回答破棄を含む | 原文・話者・言語・offset、検索前のsnapshot | 旧認可・dispatchの代替実装にはしない |

`runner.py:93,107`は派生contentとqueryをそのままembeddingへ渡す。
`adapters/llamacpp.py:49`は候補に宣言されたprefixだけを付加し、bge-m3は両prefixが空である。
`retrieval.py:70`のNFKC/casefoldはDB語彙検索キーとquery語彙に使い、
原文やembedding入力を書き換えない。言語別翻訳・接頭辞の自動補完・言語による候補除外はこの経路にない。
Coreも`memory_retrieval.py:151`でqueryと候補のnormalized_textをembeddingへ渡すため、
「Coreは原文、Memoryは派生本文」という一律の説明は適切でない。

Coreから移した137件（acceptance55・tuning82）のqueryは全件同一だった。
同一IDのembedding本文の相違はacceptanceの`updated-morning-drink / morning-drink-fact`と
`stale-fact-link / destination-fact`の2箇所で、更新Factを現行内容へ変更したprovenanceと対応する。
日英10件の本文差はこの比較では検出していない。これだけでは稼働backendのvector一致や
日英の実cosineを証明しない。閾値前の追加計測で確認する必要がある。

移設の意味変更は特に次の箇所に注意する。

- `valid-fact-attachment`はEpisodeとFactの両方を独立正解にし、両方の原文区間を必須化した。
- `stale-fact-link`は旧Factを閉じ、新Factを独立検索する。Coreのリンクがなく新Factを添付しない期待とは異なる。
- `revoked-fact-source`はFact出典欠落を検索前snapshotに表す。Episode原文は必要なままである。
- `equivalent-band-order`はCoreの同等帯を移さず、完全同点の合成成分でID順を固定した。
  実モデルの異なるscoreにも同じgoldを適用する条件は、Coreの実モデルゲートより厳しい。
- Coreの151元ID中14件は旧permission/Binding/dispatch等の契約を移さず代替テストへ対応付けた。
  移設137件のうち15境界ケースは再構築した合成条件を持ち、元の実モデル条件とは区別する。

## 親担当へ提案する変更候補（C04）

いずれも **未確定の提案**。protocol・gold・ゲート・グリッドを今回変更していない。
採用する場合は新しい比較版を測定前に固定し、旧証跡を保持する。

| 候補 | 根拠 | 確認できた効果・追加確認 | リスクと採否の条件 |
| --- | --- | --- | --- |
| 選定をケース単位の失敗と安全ゲートに分けて再検討 | 0.60で54合格/44違反、0.75で40合格/43違反。禁止とno_matchの重複加算 | 保存済みtuningから再集計可能。0.60の禁止22件・no_match16件は残るため採用推奨値にはしない | 安全違反を弱める危険。禁止情報・出典・閾値混入は厳格に保持し、検索成功と棄権の優先を親が定義する |
| 合成尺度に合わせた閾値の細分化・語彙重みを含むグリッド再評価 | 高semanticで語彙寄与が小さく、0.60→0.65で54→38合格 | 既存低閾値tuningは全適格候補を保存しており再採点できる。acceptance低閾値の効果は再実行が必要 | tuning過適合と誤取得増加。acceptanceで調整しない。ADR0012の6項・係数合計1・新しさ上限を保持 |
| 実モデルの全順序と決定的な同点契約を分ける | required_orderだけで落ちた1件。Coreは実モデルに全順序を要求しない | 順位条件だけを外す仮定なら保存済みacceptanceは27/55、各回FAILのまま | 事後緩和になり得る。順序が製品要件かを先に確認し、fixtureでscore降順・完全同点ID順を保持する |
| goldの意味変更をM2と照合し再承認 | 独立Factと添付Factの役割、更新Fact、原文区間の必須化が異なる | 現goldでの失敗を保持。正解集合変更時の成績はここでは算出しない | 正解・禁止IDを合格目的で緩めない。独立検索が必要か、原文として何を返すべきかを製品契約から判断 |
| 分類90%と100%の役割を明文化 | Coreとのゲート差 | 現結果は90%でも主要3分類FAILで、閾値前欠落は解消しない | 少数分類は依然1/1。全体合格を得る目的で変更せず、安全ゲートと品質目標の役割を親が判断 |

閾値前の全適格候補成分を診断時に保存する追加計測も親へ提案する。
これは今回未実装であり、本文・query・vectorを公開しない保存範囲を先に決める必要がある。
入力・モデル・スコアのどこを変えて再評価するかは親が決め、本報告はreranker・翻訳・分割を追加しない。

## 集計を再現する手順

GPU・DB・モデル起動を使わない。`EVIDENCE_ROOT`は外部保存先の`eval-artifacts/2026-10-20`に設定する。
プロジェクトルートでlocked環境から以下を実行する。表示するのはID・集計・ハッシュのみ。

```sh
uv run --no-sync python - <<'PY'
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
from digital_souls_memory.evaluation.comparison import (
    parameter_grid, selected_candidates, tuning_candidates,
)
from digital_souls_memory.evaluation.report import validate_public_report
from digital_souls_memory.evaluation.retrieval import eligible_memories

read = lambda p: json.loads(Path(p).read_bytes())
sha = lambda b: hashlib.sha256(b).hexdigest()
root = Path(os.environ['EVIDENCE_ROOT'])
summary = read('docs/reports/2026-10-20-retrieval-it2-summary.json')
for archive in summary['archives']:
    path = root / Path(archive['file']).name
    compressed = path.read_bytes()
    raw = gzip.decompress(compressed)
    assert sha(compressed) == archive['sha256']
    assert len(compressed) == archive['compressed_bytes']
    assert sha(raw) == archive['uncompressed_sha256']
    assert len(raw) == archive['uncompressed_bytes']
    assert raw == path.with_suffix('').read_bytes()
    print(path.name, sha(raw), 'hash/size PASS')
suites = {s: read(f'evals/retrieval/{s}/suite.json')
          for s in ('tuning', 'acceptance')}
base = read('evals/retrieval/candidates.json')
for model in ('bge-m3', 'multilingual-e5-large'):
    candidates = [c for c in base if c['model_alias'] == model]
    tuning = read(root / f'2026-10-20-retrieval-it2-{model}-tuning.json')
    validate_public_report(tuning, suite=suites['tuning'],
                          candidates=tuning_candidates(candidates))
    selected = selected_candidates({**tuning, 'suite': suites['tuning']}, candidates)
    acceptance = read(root / f'2026-10-20-retrieval-it2-{model}-acceptance.json')
    validate_public_report(acceptance, suite=suites['acceptance'], candidates=selected)
    print(model, 'public validation PASS', [c['parameters'] for c in selected])
    if model != 'bge-m3':
        continue
    fields = ('case_id', 'category', 'passed', 'recall_at_5', 'precision',
              'threshold_passed', 'sources_valid', 'forbidden_returned',
              'no_match_correct', 'order_passed', 'return_budget')
    def signature(run):
        return [([x[f] for f in fields],
                 [h['memory_id'] for h in x['hits']],
                 [h['memory_id'] for h in x['retrieved_hits']])
                for x in run['case_results']]
    assert all(signature(r) == signature(acceptance['runs'][0])
               for r in acceptance['runs'])
    cases = {c['id']: c for c in suites['acceptance']['cases']}
    gold = {g['case_id']: g for g in suites['acceptance']['gold']}
    failures = [x for x in acceptance['runs'][0]['case_results'] if not x['passed']]
    assert len(failures) == 29
    assert sum(not x['retrieved_hits'] for x in failures) == 28
    without_order = sum(
        (x['recall_at_5'] == 1 if x['recall_at_5'] is not None
         else x['no_match_correct'] is True)
        and not x['forbidden_returned'] and x['sources_valid'] and x['threshold_passed']
        for x in acceptance['runs'][0]['case_results'])
    assert without_order == 27
    print('counterfactual without required_order only', without_order, '/55')
    for x in failures:
        required = gold[x['case_id']]['required_ids']
        assert set(required) <= {m['id'] for m in eligible_memories(cases[x['case_id']])}
        print(x['case_id'], x['category'], required,
              [h['memory_id'] for h in x['hits']],
              {f: x[f] for f in fields[3:]})
    print('failures by category', dict(collections.Counter(x['category'] for x in failures)))
    names = ('sources', 'threshold', 'forbidden', 'order', 'recall', 'no_match')
    def violations(xs):
        return dict(zip(names, map(sum, zip(*[
            (x['sources_valid'] is not True, x['threshold_passed'] is not True,
             bool(x['forbidden_returned']), x['order_passed'] is not True,
             x['recall_at_5'] is not None and x['recall_at_5'] != 1,
             x['no_match_correct'] is False) for x in xs]))))
    tuning_cases = {c['id']: c for c in suites['tuning']['cases']}
    stats = {}
    for candidate in tuning['candidates']:
        runs = [r for r in tuning['runs'] if r['candidate_id'] == candidate['id']]
        counts = [violations(r['case_results']) for r in runs]
        passes = [sum(x['passed'] for x in r['case_results']) for r in runs]
        assert len(runs) == 3 and counts[0] == counts[1] == counts[2]
        assert passes[0] == passes[1] == passes[2]
        stats[candidate['id']] = (passes[0], counts[0])
        if candidate['parameters']['threshold'] == 0.2:
            for run in runs:
                for x in run['case_results']:
                    assert {m['id'] for m in eligible_memories(tuning_cases[x['case_id']])} == {
                        h['memory_id'] for h in x['retrieved_hits']}
    for index, parameters in enumerate(parameter_grid()):
        a = stats[f'bge-m3-pg_bigm.p{index:02d}']
        b = stats[f'bge-m3-pgroonga.p{index:02d}']
        assert a == b
        print(parameters['weights']['semantic'], parameters['threshold'],
              a[0], a[1], sum(a[1].values()))
    for candidate in candidates:
        identifiers = [f"{candidate['id']}.p{i:02d}" for i in range(45)]
        minimum = min(sum(stats[i][1].values()) for i in identifiers)
        best = [i for i in identifiers if sum(stats[i][1].values()) == minimum]
        assert best == [f"{candidate['id']}.p41"] and minimum == 43
        print(candidate['id'], 'minimum violations', best, minimum)
PY
```

Coreとの入力比較は`CORE_ROOT`を指定版checkoutへ設定して次を実行する。
Coreをimportせず、指定commitの入力hashを照合し、差のあるIDだけを出力する。

```sh
python3 - <<'PY'
import hashlib, json, os, subprocess
from pathlib import Path
core = Path(os.environ['CORE_ROOT'])
read = lambda p: json.loads(Path(p).read_bytes())
provenance = read('evals/retrieval/provenance.json')
inputs = {}
for item in provenance['files']:
    raw = subprocess.check_output(['git', '-C', str(core), 'show',
                                  provenance['core_commit'] + ':' + item['path']])
    assert hashlib.sha256(raw).hexdigest() == item['sha256']
    inputs[item['path']] = json.loads(raw)
for split in ('acceptance', 'tuning'):
    source = 'evals/semantic/' + ('tuning/' if split == 'tuning' else '') + 'cases.json'
    cases = {c['id']: c for c in inputs[source]['cases']}
    memory = {c['id']: c for c in read(f'evals/retrieval/{split}/suite.json')['cases']}
    changes = []
    for mapping in provenance['cases']:
        if mapping['source_split'] != split or not mapping['target_case_id']:
            continue
        old, new = cases[mapping['source_case_id']], memory[mapping['target_case_id']]
        assert old['query'] == new['query']
        texts = {r['id']: r['normalized_text']
                 for kind in ('episodes', 'facts', 'semantics') for r in old[kind]}
        changes.extend((new['id'], m['id']) for m in new['memories']
                       if m['id'] in texts and m['content'] != texts[m['id']])
    print(split, 'queries unchanged', len(memory), 'same-ID text differences', changes)
report = read(core / 'docs/evidence/2026-10-10-semantic-real-model-retrieval-bge-m3.json')
mapped = {m['source_case_id'] for m in provenance['cases']
          if m['source_split'] == 'acceptance' and m['target_case_id']}
for run in report['runs']:
    common = [c for c in run['cases'] if c['id'] in mapped]
    print('Core common cases / passed', len(common), sum(c['passed'] for c in common))
PY
```

## 親担当によるIT2再実行（C05）

本タスク内では実行しない。下記は**現行memory-v1を再現する**コマンドで、提案した新条件を採用するものではない。
実行担当は[評価手順](../retrieval-evaluation.md)に従ってGPU余裕・既存負荷、GGUF hash、
alias・次元・pooling・接頭辞、llama.cpp build/image、専用DB image/拡張を実測確認する。
専用の合成DBとbge-m3サーバーを用意し、既存サービスを変更しない。
e5は既存898 token / n_ctx=512 / HTTP400の制約を親が解決するまで全件再実行の対象に戻さない。
切り詰め・分割・ctxの名称変更だけで解消したことにしない。

`REPLAY_DIR`は新規のリポジトリ外ディレクトリ、`MANIFEST`と`REAL_CONFIG`は実行担当が確認した外部ファイルとする。
manifestは今回の実行commit・dirty状態・image・拡張版・GPU確認を記録し、旧manifestをそのまま流用しない。
接続情報や実測前後のGPU記録は私有領域に保持する。出力は上書きせず毎回新規ファイルにする。

```sh
set -eu
: "${REPLAY_DIR:?set a new external output directory}"
: "${MANIFEST:?set a verified manifest file}"
: "${REAL_CONFIG:?set a verified private config file}"
export REPLAY_DIR MANIFEST REAL_CONFIG
# 3変数は担当者が事前に設定する。
python3 - <<'PY'
import json, os
from pathlib import Path
out = Path(os.environ['REPLAY_DIR'])
out.mkdir(mode=0o700, parents=True, exist_ok=False)
candidates = json.loads(Path('evals/retrieval/candidates.json').read_bytes())
subset = [c for c in candidates if c['model_alias'] == 'bge-m3']
assert len(subset) == 2
with (out / 'bge-m3-candidates.json').open('x') as f:
    json.dump(subset, f, ensure_ascii=False, indent=2)
    f.write('\n')
PY
TUNING_SHA256=af3ea5271e0603a0726ef6bc0f39bd87bf086a18072cf4559b9ef6a0bcd423ad
ACCEPTANCE_SHA256=3c8b05394dd0fde5fe1551d40d46c4e38caa2f3e1de18ab1f7f8dd5cb442a006
sha256sum evals/retrieval/{tuning,acceptance}/suite.json \
  "$REPLAY_DIR/bge-m3-candidates.json" "$MANIFEST" > "$REPLAY_DIR/input-sha256.txt"
set +e
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/tuning/suite.json --suite-sha256 "$TUNING_SHA256" \
  --candidates "$REPLAY_DIR/bge-m3-candidates.json" \
  --manifest "$MANIFEST" --real-config "$REAL_CONFIG" \
  --tune --mode real --output "$REPLAY_DIR/tuning-report.json"
tuning_status=$?
set -e
printf 'tuning=%s\n' "$tuning_status" > "$REPLAY_DIR/exit-status.txt"
test "$tuning_status" -eq 0 || test "$tuning_status" -eq 1
TUNING_REPORT_SHA256=$(sha256sum "$REPLAY_DIR/tuning-report.json" | cut -d ' ' -f 1)
printf '%s\n' "$TUNING_REPORT_SHA256" > "$REPLAY_DIR/tuning-report-sha256.txt"
set +e
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/acceptance/suite.json --suite-sha256 "$ACCEPTANCE_SHA256" \
  --tuning-suite evals/retrieval/tuning/suite.json --tuning-sha256 "$TUNING_SHA256" \
  --tuning-report "$REPLAY_DIR/tuning-report.json" \
  --tuning-report-sha256 "$TUNING_REPORT_SHA256" \
  --candidates "$REPLAY_DIR/bge-m3-candidates.json" \
  --manifest "$MANIFEST" --real-config "$REAL_CONFIG" \
  --mode real --output "$REPLAY_DIR/acceptance-report.json"
acceptance_status=$?
set -e
printf 'tuning=%s acceptance=%s\n' "$tuning_status" "$acceptance_status" \
  > "$REPLAY_DIR/exit-status.txt"
```

終了0=品質PASS、1=完走した品質FAIL、2=入力検証または実行ERROR。
tuningが2ならacceptanceへ進めない。終了1のtuningは保持し、再検証を通じて設定を選ぶ。
CLIはsuite・tuning reportのhashを検証し、候補subsetとmanifestのhashも出力へ記録する。
上記生成subsetの表現は旧ファイルと同一byteとは限らないため、新しいhashを固定する。
旧bge候補subset hashは`80f912c993c10066fcdfd39fc2188293e4e7ca7d0ff85c20485190b05ac80f93`。
旧manifest hashは`aecd51dfa58a5df11514e99824d66797b0aec259bb7275aca713ddd2c357a212`。
採用条件を変える場合は、先に計画・比較版・独立tuning入力を承認してから手順を更新する。

## 通常検証と残る判断（C03・C06〜C09）

Python 3.12.3 / uv 0.8.22、frozen lock、分離した開発venvとuv cacheを使って今回実行した。
`uv lock --check`、`uv sync --frozen`、ruff check・format-check、strict mypy（55 source files）、
UT 182件、IT1 192件、使い捨て実PostgreSQL 76件、shell構文、両Compose設定、
sdist/wheel build、hash固定runtimeの独立venv導入、wheel導入、`python -I`によるAPI/CLI importがPASS。
Starletteの既存DeprecationWarningが各suiteで1件あり、skip・xfail・xpassは0。

最初のPostgreSQL wrapperはDocker buildxの既定設定先が書込不可でexit1。
`DOCKER_CONFIG`を書込可能な一時ディレクトリへ分離し、同じwrapper・固定Dockerfile・合成DBで再実行して76 PASS。
最初の`uv build --no-build-isolation`はPATH上のPythonにhatchlingがなくexit2。
locked開発venvのbinをPATHへ含めて同じコマンドを実行し、sdist/wheelを作成できた。
初回のwheel導入とimportもbuild未生成のため失敗し、正常build後に両方を再実行して成功した。
依存・lock・製品コードを変更して環境問題を隠していない。

実装不具合が未特定のためC03の条件付き修正・RED→GREENは発生していない。
文書だけの変更に本文を固定するテストは追加しなかった。
製品schema・Alembic revision・Core runtime依存・固定protocol/gold/gridを保持した。
保存済みデータにないacceptanceの閾値前成分と、新条件の実モデル品質は未確認である。
追加計測と提案採否は親担当が判断する。GitHub最終head CI・独立レビュー・PR操作は本ステップではNOT RUN。
