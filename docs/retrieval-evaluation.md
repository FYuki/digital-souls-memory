# 検索評価ハーネス

Issue #9の評価専用パッケージです。製品schema・migration・想起APIは変更しません。
実検索、固定条件によるtuning、公開証跡の再検証まで実装しています。
実モデル比較はNOT RUNです。前段でNVML取得をOSに拒否され、GPU deviceも
確認できなかったという報告を引き継ぎ、同条件のGPU確認は反復していません。

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
DB/OS cacheは制御せずcold cacheと記載しません。実資源計測はNOT RUNで通常試験はfake transportです。
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
