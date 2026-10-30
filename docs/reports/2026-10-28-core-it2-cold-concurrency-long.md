# Phase 1 実モデル IT2・cold・並列・長文試験

[要約JSON（固定manifest・全標本・実行記録）](2026-10-28-core-it2-cold-concurrency-long-summary.json)。
起点 `b154b4e25f13bf92f00bb6d9a4f4a0fcecb67b0f`（PR #115 統合済みの origin/epic/phase1-consistency）。
作業ブランチ `test/phase1-it2-cold-concurrency-evidence`、別 worktree `/tmp/wt-it2-cold-concurrency`。
手順の正本は [core-st.md](../core-st.md) と通常・長文の manifest template。
承認済みGPUとモデルを使用し、合成データだけで実行した。試験・runner・製品コードは変更していない。

## IT2

全 `it2` marker は `tests/it2/test_recall_models.py` と `tests/postgres/test_recall_it2.py` にあり、全3件を実行。
**PASS 3 / FAIL 0 / SKIP 0 / ERROR 0**、pytest・使い捨てDB runner の終了コード0。
実embeddingの1024次元・単位ノルム・決定性・日本語意味順序、rerankerの順位・raw閾値、DB結合の原文区間・関連2件・提供記録・JSON全体1500トークン以内を検証。
Starlette TestClient に既存の DeprecationWarning が1件あり、失敗・skipではない。

## 性能

各条件・件数で warm-up 5回、本計測100回、nearest-rank。p50は50番目、p95は95番目を全標本から独立再計算した。
失敗標本は除外しない。計測は公開MemoryClientの呼出し直前から完全な型付き応答の取得・検証完了まで。
並列度4/8も **p95 ≤600msで参考判定するが、並列は必達の対象外**。
通常・長文・cold後warmは必達 p95 ≤600ms。cold初回は独立標本1回/件数で、warm分位点へ混ぜない。
cold初回も600ms以内かを別途照合し、両件数で適合した。

| 条件 | 件数 | 判定 | p50 ms | p95 ms | 失敗率 | throughput 回/秒 |
|---|---:|---|---:|---:|---:|---:|
| warm-c1 | 100 | PASS | 151.470 | 170.360 | 0% | 7.000 |
| warm-c1 | 1000 | PASS | 199.746 | 248.960 | 0% | 4.794 |
| warm-c4 | 100 | PASS（参考） | 264.814 | 319.820 | 0% | 14.574 |
| warm-c4 | 1000 | PASS（参考） | 398.268 | 488.417 | 0% | 9.826 |
| warm-c8 | 100 | PASS（参考） | 481.493 | 575.818 | 0% | 15.965 |
| warm-c8 | 1000 | FAIL（参考） | 741.480 | 896.109 | 0% | 10.257 |
| cold-c1 | 100 | PASS | 132.950 | 169.799 | 0% | 7.153 |
| cold-c1 | 1000 | PASS | 207.396 | 254.204 | 0% | 4.642 |
| long-c1 | 100 | FAIL | 1223.324 | 1259.989 | 0% | 0.817 |
| long-c1 | 1000 | FAIL | 4215.531 | 4251.681 | 0% | 0.237 |

throughput は失敗を含む完了試行数 / 本計測区間の実秒数。投入・索引待ち・再起動・warm-upを含まない。
並列度はworker数、100回はworkerごとでなく総試行数。モデルサーバ内部のparallelは1を保持。

## cold-start

**再起動対象は Memory サービスのみ**。PostgreSQL・モデルサーバ・OSキャッシュは保持。
各件数で投入・索引ready確認後、HTTP clientを閉じ、Memoryを再起動し、health/config確認後の新clientによる最初の想起を測定した。
readinessは想起を呼ばない。数値は再起動操作の所要時間を含まず、readiness後の初回想起遅延を示す。

| 件数 | 初回結果 | cold初回 ms | warm p50 ms | 差 cold − warm p50 ms |
|---|---|---:|---:|---:|
| 100 | PASS | 414.909 | 132.950 | 281.959 |
| 1000 | PASS | 419.080 | 207.396 | 211.684 |

## 固定環境・入力

- Ubuntu WSL2、Intel Core i7-14700K / 28 logical CPUs、RTX 4070 Ti SUPER 16376 MiB、driver610.88。共有ホストで背景負荷の完全隔離なし。
- Python3.12.3 / uv0.8.22、frozen lock。coldのhost実バージョン・コマンドはJSONのhost_controlに保存。
- 各条件は標準shell runnerが新規Compose projectとtmpfs合成DBを作成。既存DB・volumeは使わない。
- PostgreSQL18.6 / vector0.8.2 / pg_bigm1.2 / migration0014。実DBのversion監査をJSONに保存。
- 採用モデル bge-m3 / bge-reranker-v2-m3、Q8_0。重みのSHA-256を実ファイルから計算してtemplateと一致確認。revision・digest・poolingは各固定manifestに保持。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
  ctx/batch/ubatch=2048、parallel=1、GPU layers99、threads/threads-batch8、cache-ram0、embedding pooling cls / reranker pooling rank。
  モデルファイルはread-only mount。loopback18081/18082のみ公開。起動コマンドと実コンテナ引数をJSONに保存。
- 通常入力「日本語」3文字。長文はtemplateの固定日本語全文1039文字をそのまま利用し、投入本文・queryとも切り詰めていない。
- candidate_limit200 / rerank_limit20 / result_limit5 / token_limit1500 / raw relevance閾値−2、data_counts=[100,1000]。
- 抽出fake、embedding・tokenizer・reranker・pg_bigm検索・DBは実構成。timeoutは標準値（索引1800秒、想起30秒）。
- DB/clientイメージID、各条件のUTC開始終了日時、全warm-up・本計測・cold標本、例外型、shell終了コードをJSONに保存。

## 実行と検証

外側収集は `python3 /tmp/run-it2-evidence.py`。script全文はJSONのcommand_sourceに保存。
既存runnerを差し替えず、以下の正本コマンドを実行した。

```sh
uv sync --frozen
# digest照合後、承認済み定数からモデルmanifestを生成
bash tools/with-test-postgres.sh uv run --no-sync pytest -m it2 --force-enable-socket -q tests/it2 tests/postgres/test_recall_it2.py --junitxml=/tmp/core-it2-cold-concurrency-long-20261028/it2.xml
# 各条件の固定manifestとMEMORY_CORE_ST_EVIDENCE_DIRを指定
bash tools/with-core-st.sh performance
bash tools/with-core-st.sh performance-cold
```

全追跡ファイルの事前・事後SHA-256一致と、各manifestの事前・事後一致を確認。
結果の件数・nearest-rank分位点・標本数を独立照合し、JSON parseと `git diff --check` を実施。
コード変更がないためUT・IT1・通常postgres・機能ST・ruff・mypy・package品質ゲートの再実行は **NOT RUN**。
このIT2や性能試験の結果を、それらのPASSへ読み替えない。

保存標本は本計測1000回・warm-up50回・cold初回2回の計1052回。全標本で失敗・timeout・欠測0。
後片付け **PASS**：各専用projectのcontainer/network/volume、今回のモデル2コンテナ、新規IT2 DBの残存なしを成功した確認コマンドで照合。過去の別試験の停止済みDBは作成日時で区別し、操作していない。

生証跡 `/tmp/core-it2-cold-concurrency-long-20261028`。全ファイルのSHA-256をJSONに保持。
各専用Compose projectのcleanupログ・最終status、今回のモデル2コンテナの削除結果をJSONに保存。

## 結果・制約

- warm-c1: runner **PASS**、exit_status=0; project=memory-core-st-1793152005-77353。
- warm-c4: runner **PASS**、exit_status=0; project=memory-core-st-1793152182-117922。
- warm-c8: runner **FAIL**、exit_status=1; project=memory-core-st-1793152348-153550。
- cold-c1: runner **PASS**、exit_status=0; project=memory-core-st-1793152517-190604。
- long-c1: runner **FAIL**、exit_status=1; project=memory-core-st-1793152702-232831。

並列度8・1000件と長文入力の性能超過をPASSとは扱わない。モデル内部parallel=1、同一の単一時刻の測定であり、スループット増加や遅延差から設定変更の因果効果・大規模運用の保証は主張しない。
coldはMemoryのみの再起動であり、モデル未ロード状態やOSキャッシュ消去後の性能を示さない。
製品修正・mergeなし。
