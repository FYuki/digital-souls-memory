# 検索評価ハーネス

Issue #9の評価専用パッケージです。製品schema・migration・想起APIは変更しません。
実検索、固定条件によるtuning、公開証跡の再検証まで実装しています。
当初の実モデル比較NOT RUN記録は保存しています。ホスト実行ではGPUを確認し、
bge-m3は完走して品質FAIL、e5は長文で実行ERRORでした。
[実測記録](reports/2026-10-20-retrieval-it2-host.md)を参照してください。

## 固定入力

`evals/retrieval/acceptance/suite.json`は55件、`tuning/suite.json`は82件です。
`source-inventory.json`は指定Core commitの4ファイルをgit showで読み、byte hashを照合して
取り出した元IDの固定snapshotです。台帳自身から期待IDを作る検証とは区別します。
151件の対応、14件の適用不能部分と代替テスト、意味変更は`provenance.json`に記録しています。
Core runtime・旧Binding・permission・dispatchに依存しません。
Coreコード/ケースのライセンス表示は未特定で、モデルのMIT表示で補完しません。

残る15件は`boundary-fixtures.json`で再構築しました。閾値直下・境界・直上は6項が
0.49・0.50・0.51となる合成条件、完全同点はID昇順で確認します。query・原文・正解/禁止IDの
意味は変えていません。順位ケースには独立goldの`required_order`を追加しました。
実モデルでは合成成分を使わず、実スコアが期待と異なればFAILです。
返却量による除外で正解を失ってもgoldを緩和しません。

## 比較条件

`protocol.json`と両suiteの`comparison_version=memory-v1`が固定条件です。

| 項目 | 条件 |
| --- | --- |
| 意味 | cosine distance dからmax(0,min(1,1-d)) |
| 検索キー | query・派生本文だけNFKC/casefold。Turn原文は保持 |
| 語彙 | Unicode文字・数字の連続列から重複しない隣接2文字。1文字の列はその文字。全query語のDB一致率、語なしは0 |
| pg_bigm | gin_bigm_opsとliteralをescapeしたLIKE |
| PGroonga | pgroonga索引と&@。本文をquery言語式にしない |
| 新しさ | 出典occurred_at最大値とlast_reinforced_atの新しい方から30日半減。負の経過は0日 |
| 減衰例外 | Commitment・VerbatimAnchorは1。検索による再強化なし |
| 他3項 | fixtureのimportance・self_relevance・trust。検索では変更しない |
| 重み | semantic=0.55/0.65/0.75、lexical=0.80-semantic、他4項=各0.05 |
| 閾値・順位 | 0.20〜0.90、0.05刻み。score >= threshold。スコア降順、完全同点だけID昇順 |
| ゲート | 分類ごと・各回100%。正解上位5件包含、禁止ID不在、該当なし空結果、原文・範囲・閾値・指定順位 |
| 指標 | 分類別recall@5・precision、該当なし成功率。正解なしはrecall分母から除外 |
| 反復 | 各設定3回、新規ポート・一時DB。vector・結果cacheなし |

全適格候補を完全検索で評価し、近似vector索引は使いません。
tuningは45通りを各3回実行します。必須ゲート違反件数の少なさ、分類別成功率の
最小値、平均recall、平均precisionの順で選びます。同値なら大きい閾値、大きいsemantic重みの順です。
実行エラーのあるtuningから選びません。acceptance前にtuning reportのhashを固定し、
その再検証から候補別parametersを解決します。acceptance結果を選定へ戻しません。

`candidates.json`は次の2モデル×両全文検索方式の4構成です。

| モデル | 量子化/次元/pooling | query / passage接頭辞 | revision / SHA-256 |
| --- | --- | --- | --- |
| bge-m3 | Q8_0 / 1024 / cls | 空 / 空 | 9eba04c5d75ba5a1595e45de734d36bef4e5cb98 / aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173 |
| multilingual-e5-large | Q8_0 / 1024 / mean | `query: ` / `passage: `（末尾半角空白） | 272a0975bd1a67886c0719767af84dfd2cbd9aa4 / 6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3be |

e5は指定Core資料`docs/evidence/2026-10-10-semantic-embedding-candidates.md`に基づきます。
候補の定義は実GGUFや稼働serverのidentityの証明ではありません。

## fixture実行

```sh
uv sync --frozen
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/smoke/suite.json \
  --candidates evals/retrieval/smoke/candidates.json \
  --fixture evals/retrieval/smoke/fixture.json \
  --mode fixture --output /tmp/memory-retrieval-smoke.json
```

出力先は新規ファイルにします。smokeの2次元vector・閾値・重みはテスト専用です。
fixtureは独立観測を返し、goldを読みません。fixtureの終了0は実行・証跡検証の成功で、品質はnullです。
realは品質FAILで1、実行エラーで2、品質PASSで0です。fixture fallbackはありません。

## 実環境実行

IT2はIssueで承認されています。合成データ・loopbackのみを使い、既存サービスの停止/設定変更・
クラウド送信へ拡張しません。モデルは直列で評価します。固定候補から1モデルの2方式を
抜き出す場合も、その候補ファイルのhashを保存します。

実行担当はGPU余裕・既存負荷、GGUF SHA-256、alias・次元・pooling・接頭辞、llama.cpp build/image、
比較DB image/拡張版を実際に確認します。ロード前・ロード後・モデル解放後の時刻付きGPU全体VRAMは、
実行中の標本と対応付けた別の実測証跡に保存します。CLIはserverの起動・停止を行いません。

`--manifest`はmemory_commit/core_commit、memory_dirty/core_dirty、db_image、db_extensions
（vector/pg_bigm/pgroongaの版）、llama_cpp_build/llama_cpp_image、gpu_preflight_confirmedを持ちます。
imageはsha256:形式です。boolや名前の記入だけで実identity確認を代替しません。

`--real-config`には以下を明示します。接続情報・設定本文は公開reportへ出しません。

```json
{
  "admin_url": "postgresql://memory_synthetic@127.0.0.1:15432/memory_synthetic",
  "endpoints": {"bge-m3": "http://127.0.0.1:18083"},
  "model_identities": {"bge-m3": {"model_alias":"bge-m3", "sha256":"実測hash", "revision":"固定revision", "dimensions":1024, "pooling":"cls", "query_prefix":"", "passage_prefix":""}},
  "resources": {"db_container":"専用DBコンテナID", "model_containers":{"bge-m3":"専用モデルコンテナID"}, "gpu_device":"0"}
}
```

専用の空のmemory_syntheticを明示し、数値loopbackかUnix socketへ限定します。
通常のMEMORY_DATABASE_URLは読みません。model_identitiesは実行担当が確認した条件で、全候補との
不一致を送信前に拒否します。HTTPは環境proxy・redirectを使わず、応答alias・件数・次元・vectorを検証します。
Docker socketは計測の読み取りに使います。

```sh
# 各digest変数は実測前に固定したbyte hashを渡す。
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/tuning/suite.json --suite-sha256 "$TUNING_SHA256" \
  --candidates candidate-model.json --manifest manifest.json --real-config local-config.json \
  --tune --mode real --output tuning-report.json
# 全runが実行できた品質FAIL（終了1）のtuningも保持する。
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/acceptance/suite.json --suite-sha256 "$ACCEPTANCE_SHA256" \
  --tuning-suite evals/retrieval/tuning/suite.json --tuning-sha256 "$TUNING_SHA256" \
  --tuning-report tuning-report.json --tuning-report-sha256 "$TUNING_REPORT_SHA256" \
  --candidates candidate-model.json --manifest manifest.json --real-config local-config.json \
  --mode real --output acceptance-report.json
```

programmatic入口もresolved_protocolで同じ候補parametersを検索・採点・再検証へ渡します。
調整済み候補はselected_candidatesで取得し、実環境を呼ぶ側がmanifest・identityを確認して
ResourceMonitorをresource_factoryとして渡します。テストの計数付きfakeは実モデル証明にしません。

## 計測・公開証跡

| フィールド | 観測範囲 |
| --- | --- |
| passage_embedding_ms | passageのHTTP埋め込みとvector検証 |
| database.insertion_ms / index_creation_ms | ケース投入 / 全文索引作成。embeddingを含めない |
| index_ms | DB準備・投入・索引作成・サイズ観測の合計 |
| database.index_bytes | bigmは索引relation。PGroonga 4.1.0はobject_listで当該索引のSources/Lexiconと列を列挙しobject_inspectのdisk_usageを合算。共有管理表は除外 |
| table_bytes / vector_bytes | Turn/記憶表サイズ / vector列値のpg_column_size合計 |
| query_embedding_ms / search_ms | query埋め込み / DB成分・順位・原文回収・返却片構築 |
| recall_ms | query埋め込み開始から返却片構築完了まで。独立採点は除外 |
| latency | 各runの標本から線形補間p50/p95、p95 300ms/600msとの比較。回を平均しない |
| harness_cpu_ms | runのPython CPU時間 |
| harness_process_peak_rss_kib | Linux getrusageのプロセス全寿命の最大RSS。run増分ではない |
| resources | 専用DB/モデルのDocker CPU累積ns差分とmemory_stats.usage。100ms待機間隔の標本と実elapsed_ms。通信・観測時間を含み厳密な100ms周期を保証しない |
| gpu_total_used_mib | nvidia-smiのGPU全体VRAM。モデル専用値ではない |

返却片はmemory_idとsource_spans（原文・話者・言語・offset）のJSON配列です。
sort_keys、ensure_ascii=False、separators=(',',':')、allow_nan=Falseを固定し、UTF-8の1byteを
1トークン相当と推定します。実token数でも全tokenizerの上限保証でもありません。
順位順に最大5件・1,500byteへ収まる記憶を選び、原文を切らず、除外IDと理由を残します。
公開reportは除外前の観測も本文hashだけで保持し、固定入力から復元して返却量・採点・
加重和・集計・分位点・CPU差分を再検証できます。

製品HTTP・ProvisionRecord書込みは未計測です。標本間のRAM/VRAM peakは保証しません。
DB/OS cacheは制御せずcold cacheと記載しません。実資源計測は新しいホスト実行証跡に保存しました。通常試験はfake transportです。
これらの値から製品ターン想起全体への適合を断定しません。
公開出力は原文・query・任意追加欄・外部例外本文を含めず、byte hashと正規JSON hash、候補parameters、
各runの実呼出し件数を保存します。validate_public_reportに固定suiteと調整済み候補を渡して再検証します。

## 実PostgreSQL試験

```sh
docker compose -f compose.retrieval-evaluation.yml build
bash tools/with-test-postgres.sh
```

Compose・wrapper・CIは同じDockerfileを使用します。基底はPGroonga 4.1.0/PG18のamd64 manifest digest固定です。
pgvector 0.8.2・pg_bigm固定commitをSHA-256で検証し、apt直接依存版・全package一覧を固定します。
取得不能時に別版へfallbackしません。通常試験は新規コンテナ・専用socket・tmpfs・network noneで、
公開port・製品volumeを使いません。生成imageのregistry公開・配布digestの決定はしていません。

CREATE成功後だけDBを所有し、正常・例外とも接続close後に所有DBを廃棄します。
名前衝突で既存DBを削除せず、ケース投入ごとに前ケースを消して論理IDの原文を分離します。
両方式の検索・原文・自己発話・成分・runner/report往復・例外廃棄と製品migrationを実DBで検証します。
合成vectorの成功は実モデル品質の証明ではありません。

## memory-v2

`evals/retrieval/memory-v2/` に別のprotocol・両splitのcases/gold/suiteを固定します。
v1の固定入力・証跡・過去報告を保持し、版はsuite境界で明示的に選びます。
query・原文・正解集合はv1と同一です。forbidden_idsの和集合を保持したまま、
safety_forbidden_idsとquality_forbidden_idsへ重複なく分類します。
安全分類の正解もrecall評価に含めます。

| 項目 | memory-v2の条件 |
| --- | --- |
| 安全 | 各回100%。適格性による検索前除外、安全禁止ID不在、原文区間・話者・言語・出典、5件・1,500byte推定予算 |
| 品質 | 必須正解IDがすべて上位5件に含まれれば想起caseは合格。品質上の誤取得や順序差では落とさない。想起分類は各回90%以上、該当なし分類は空結果だけを合格とし各回70%以上。caseの最終合否は安全判定との論理積 |
| 空結果 | 正解欠落は品質失敗。これだけでは安全失敗にしない |
| threshold分類 | required_idsがある側と空の側を別々にゲート・選定する |
| 順位 | 実モデルではrequired_orderを合否に使わない。完全同点のID順は合成fixtureで確認 |
| 選定 | 全3回で安全違反0の設定だけから、分類・回ごとの最低想起合格率→最低該当なし合格率→全正解ありケースの平均recall。完全同率なら固定グリッドの先頭 |
| 不適格 | 安全違反0の設定がなければ選定を拒否し、acceptanceへ進めない |
| 重み・閾値 | v1と同じ45設定。6項・係数合計1・新しさ係数0.1以下 |
| 観測 | Search.observeは閾値前の全適格候補を返す。上位5件・閾値後への限定は禁止 |

false_positive_countは返却上位5件のうちrequired_idsに含まれない件数です。
品質禁止集合に未列挙の非正解も含め、分類別に合計します。precisionにも誤取得を反映し、
これらの指標を合否・設定選定へ追加しません。

各caseのcandidate_observationsはmemory_id、raw cosine、6成分、合成score、
reranker_scoreを保持します。負のcosineとclamp後semanticを区別します。
score_rangesは分類・runごとに正解の全標本と各caseの非正解最高値を集計し、
count・min・p05・p50・p95・maxを線形補間で出します。rerankerは同じ形で別に集計します。
正解なし・非正解なし・未測定のrerankerはnullで、0で補いません。厳密値は品質の合否条件にしません。
公開証跡は候補の完全性・加重和・cosine整合・返却との接続・分位点を再検証します。

任意のrerankerへは適格候補の本文とqueryだけを渡します。全候補のrerank後に
6項合成scoreの閾値と返却予算を適用します。rerankerを第7成分にはしません。
無効時はscore降順・完全同点ID順を保持します。不正応答・通信失敗時は停止し、
無効経路へfallbackしません。有効・無効の比較は別の候補ファイルで実行してください。
rerank_ms、result_selection_msとstage_latencyの各段p50/p95を記録し、
recall_msにはrerankと返却片構築を含めます。全体の300/600ms比較は各runで維持します。

### 追加候補の確定とIT2

この変更でIT2・STは実行しません。次の手順は親側の実行担当向けです。
`candidate-templates.json` のQwen3-Embedding-0.6B・ruri-v3-310mのnullは未確定です。
実行候補として利用する前に、実GGUFのrevision・SHA-256・次元・pooling・量子化、
接頭辞（空文字も明示）・max_context_lengthを確定し、選んだ候補だけを
`candidate-model.json` へ保存してください。未確定値は通信前に拒否します。
real-configのmodel_identitiesにも同じ確定値を記録します。
追加モデルの量子化・文脈長も送信前照合の対象です。

rerankerを有効化する場合は各候補のrerankerにmodel_alias・revision・sha256・
quantization・max_context_lengthを確定して記載し、real-configへ
reranker_endpointsとreranker_identitiesをalias別に追加します。
HTTPアダプタが対象にする形式はPOST /rerank、
model/query/documents（文字列配列）/top_nを送信し、
results内のindex/relevance_scoreを受け取る形式です。proxy・redirectは無効です。
指定llama.cpp buildとの実機互換性は未確認です。親は本測定前にこの形式とidentityを照合し、
対応しないbuildでは実行せず、形式を確認してアダプタを更新してください。
fake HTTP試験を実機互換性の証明にしません。

候補ファイルを確定してからhashを保存し、suiteとtuning reportのbyte hashを固定します。
以下のARTIFACT_ROOTはリポジトリ外のディレクトリを指定します。
既存のmanifest・GPU余裕・稼働identity・image/build・DB拡張の事前確認も必要です。

```sh
sha256sum candidate-model.json
sha256sum evals/retrieval/memory-v2/tuning/suite.json
sha256sum evals/retrieval/memory-v2/acceptance/suite.json
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/memory-v2/tuning/suite.json --suite-sha256 "$TUNING_SHA256" \
  --candidates candidate-model.json --manifest manifest.json --real-config local-config.json \
  --tune --mode real --output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/v2-tuning.json" \
  --summary-output v2-tuning-summary.json \
  --artifact-reference eval-artifacts/2026-10-20/v2-tuning.json
sha256sum "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/v2-tuning.json"
uv run --no-sync memory-evaluate-retrieval \
  --suite evals/retrieval/memory-v2/acceptance/suite.json --suite-sha256 "$ACCEPTANCE_SHA256" \
  --tuning-suite evals/retrieval/memory-v2/tuning/suite.json --tuning-sha256 "$TUNING_SHA256" \
  --tuning-report "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/v2-tuning.json" \
  --tuning-report-sha256 "$TUNING_REPORT_SHA256" \
  --candidates candidate-model.json --manifest manifest.json --real-config local-config.json \
  --mode real --output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/v2-acceptance.json" \
  --summary-output v2-acceptance-summary.json \
  --artifact-reference eval-artifacts/2026-10-20/v2-acceptance.json
```

出力先の親ディレクトリは事前に作り、出力は新規ファイルにしてください。
終了0=実行・検証成功（fixtureは実品質null）、1=実品質FAIL、2=入力・実行エラーです。
リポジトリにはsummaryだけを置き、詳細JSONを相対参照・SHA-256・byte数で対応付けます。
検証時は詳細JSONのbyte hashを照合してからvalidate_public_reportへ固定suite・候補を渡します。
詳細の公開射影に原文・query・vector・接続情報・例外本文を含めません。

### 保存済みv1 tuningの参考再採点

元JSONのhashとgzip一致、v1規則の公開検証を確認し、閾値0.20の全適格候補が
各ケースで完全にある場合だけ再採点します。元ファイルは書き換えません。
SOURCE_CANDIDATESは元公開報告のcandidates配列をそのまま保存した90設定のファイルです。
以下の各hashは実ファイルから照合してください。

```sh
uv run --no-sync python -m digital_souls_memory.evaluation.rescore \
  --source "$SOURCE_TUNING" --source-sha256 "$SOURCE_SHA256" \
  --source-suite evals/retrieval/tuning/suite.json --source-suite-sha256 "$V1_SUITE_SHA256" \
  --target-suite evals/retrieval/memory-v2/tuning/suite.json --target-suite-sha256 "$V2_SUITE_SHA256" \
  --candidates "$SOURCE_CANDIDATES" \
  --output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/memory-v2-bge-m3-reference.json"
```

新しい実測ではなく、raw cosine・reranker・遅延は再測定しません。
保存されていない値を補完せず、acceptanceへの効果は新たなIT2で確認します。
[参考再採点報告](reports/2026-10-20-memory-v2-reference.md)を参照してください。

## memory-v2.1による再測定

棄却尺度・返却予算・必須出典区間の品質判定が変わるため、比較版を
`memory-v2.1`へ上げます。`evals/retrieval/memory-v2.1/`を使用し、v2の入力・gold・
実測証跡は保持します。両splitのquery・Turn原文・派生本文・goldはv2と同じです。
評価入力の記憶に`source_spans`（turn_id/start/end）だけを追加します。
短い派生本文が出典原文の部分文字列ならその範囲、要約・複数出典なら対応する原文範囲を
明示します。長い作業記録の出典は、小物入れの目印について述べた一文です。
範囲の作成と検索はgoldを参照しません。オフセットはPython文字位置で、原文を正規化しません。

| 項目 | memory-v2.1の条件 |
| --- | --- |
| 候補取得 | 適格候補だけを埋め込み・索引へ渡す。6項加重和の降順、同点ID順で上位20件をrerankerへ送る |
| reranker有効 | llama.cppのraw relevanceを変換せず使用。relevance < thresholdを棄却し、残りをrelevance降順・同点ID順で返す。合成閾値を再適用しない |
| 有効時グリッド | raw relevanceの-12〜12を1刻み、既存の3種類の重みと組み合わせた75設定。品質合格を保証する範囲ではない |
| reranker無効 | 既存の合成スコア順位・包含的閾値・45設定を維持。返却量の判定は新版のトークン相当へ変更 |
| 計数 | 記憶ID・原文・話者・言語・オフセット・切り出し情報を含む返却JSON配列全体。scoreは含めない。正規JSONの条件は旧版と同じ |
| 通常試験 | `FixtureTokenCounter`の`approximate-characters-v1`。Unicode文字数をトークン相当数とする決定的近似で、日本語・英語に同じ規則を使う |
| 実モデル | 埋め込みモデルと同じendpoint・identityの`POST /tokenize`。contentは返却JSONそのもの、接頭辞なし、add_special=false。tokens配列の長さを使用 |
| 返却予算 | 最大5件・1,500トークン相当。部分の計数値を加算せず、追加するたびに全体を再計数する |
| 窓 | 単体で予算を超えた記憶は各出典を連続窓へ切り出す。queryの完全一致、またはqueryの隣接2文字が最も多く一致する文の一致位置を中心にする。一致がなければ先頭を中心とする |
| 窓の大きさ | 複数出典の文字数に比例して配分し、実際の全体JSONを再計数しながら幅を二分探索する。予算内の窓を確認できなければ除外し、理由を残す |
| 切り出し情報 | `truncated`と返却start/end、元のoriginal_start/original_endを保持。本文は原文の部分文字列だけで、要約・言い換えなし |
| 品質 | 必須IDの包含に加え、goldのrequired_source_spansの本文が指定Turnの返却区間に含まれること。欠落を合格にしない |
| 報告 | return_budget.token_count/token_counterに計数値・方式を記録。score_rangesは全適格候補から分類別・各回で算出。未送信rerankerスコアはnull、0補完なし |

計数通信の失敗・不正応答では実行エラーを記録し、近似への切替は行いません。
公開報告には本文を含めず、範囲・hash・切り出し情報・計数方式を保持します。
`public_report`、`validate_report`、`validate_public_report`には実行時と同じidentityの
`token_counter_factory`を必ず渡します。CLIは固定tuning報告の検証時点からこの依存を構築し、
acceptanceの実行と公開再検証まで渡します。factory不在を暗黙の近似で補いません。
対象llama.cpp buildの`/tokenize`互換性・実モデル品質は親の測定担当による確認事項です。
MockTransportの成功は実機互換性の証明ではありません。

### 8構成の実行コマンド（親の測定担当用）

新ディレクトリの`candidates-bge-m3.json`、`candidates-bge-m3-rerank.json`、
`candidates-Qwen3-Embedding-0.6B.json`、`candidates-Qwen3-Embedding-0.6B-rerank.json`は
各2方式（pg_bigm/PGroonga）を含み、合計8構成です。無効構成にreranker設定は不要です。
`candidate-templates.json`にはruri-v3-310mを残します。公式配布にGGUFがないため
**NOT RUN**という既存の理由を維持し、変換モデルで補いません。
既存の[モデル報告](reports/2026-10-20-retrieval-it2-v2-models.json)の理由も保持します。

実行前にモデルの実GGUF hash・revision・量子化・文脈長、GPU余裕、専用DB、
llama.cpp build/image・DB拡張版を確認してください。各構成名に対応する
`config/<name>.json`と`manifest/<name>.json`を用意します。real-configの
model_identitiesには候補と同じ全identity項目（quantization/max_context_lengthも含む）を
記録します。rerankerありのconfigにはreranker_endpoints/reranker_identitiesも必要です。
ARTIFACT_ROOTはリポジトリ外の新規出力先です。以下は実行せずに示した再測定手順です。

```sh
TUNING_SHA256=$(sha256sum evals/retrieval/memory-v2.1/tuning/suite.json | cut -d ' ' -f 1)
ACCEPTANCE_SHA256=$(sha256sum evals/retrieval/memory-v2.1/acceptance/suite.json | cut -d ' ' -f 1)
for name in bge-m3 bge-m3-rerank Qwen3-Embedding-0.6B Qwen3-Embedding-0.6B-rerank; do
  candidate="evals/retrieval/memory-v2.1/candidates-$name.json"
  target="$ARTIFACT_ROOT/eval-artifacts/2026-10-20/memory-v2.1-$name"
  mkdir -p "$target"
  sha256sum "$candidate"
  uv run --no-sync memory-evaluate-retrieval \
    --suite evals/retrieval/memory-v2.1/tuning/suite.json --suite-sha256 "$TUNING_SHA256" \
    --candidates "$candidate" --manifest "manifest/$name.json" --real-config "config/$name.json" \
    --tune --mode real --output "$target/tuning.json" \
    --summary-output "$target/tuning-summary.json" \
    --artifact-reference "eval-artifacts/2026-10-20/memory-v2.1-$name/tuning.json"
  status=$?
  if [ "$status" -gt 1 ]; then exit "$status"; fi
  TUNING_REPORT_SHA256=$(sha256sum "$target/tuning.json" | cut -d ' ' -f 1)
  uv run --no-sync memory-evaluate-retrieval \
    --suite evals/retrieval/memory-v2.1/acceptance/suite.json --suite-sha256 "$ACCEPTANCE_SHA256" \
    --tuning-suite evals/retrieval/memory-v2.1/tuning/suite.json --tuning-sha256 "$TUNING_SHA256" \
    --tuning-report "$target/tuning.json" --tuning-report-sha256 "$TUNING_REPORT_SHA256" \
    --candidates "$candidate" --manifest "manifest/$name.json" --real-config "config/$name.json" \
    --mode real --output "$target/acceptance.json" \
    --summary-output "$target/acceptance-summary.json" \
    --artifact-reference "eval-artifacts/2026-10-20/memory-v2.1-$name/acceptance.json"
  status=$?
  if [ "$status" -gt 1 ]; then exit "$status"; fi
done
```

終了1の品質FAILも保存し、acceptanceで閾値を再調整しません。終了2は入力・実行エラーです。
過去のv2 FAILは新版の結果で上書きしません。詳細JSONのbyte hashとsummaryを対応付け、
計数方式・分類別スコア分位点・品質ゲートを各回で確認してください。
