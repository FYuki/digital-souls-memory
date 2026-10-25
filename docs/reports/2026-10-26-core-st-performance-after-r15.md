# Phase 1 実モデル性能 ST：R15 統合後の最終確認

[要約JSON・全210標本・SQL実測/EXPLAIN](2026-10-26-core-st-performance-after-r15-summary.json)。
起点 `582b51283d1dd4a2490be8f050c586994ec1e172`（epic/phase1-consistency、#91 / PR #92 統合済み、migration 0014）。
作者削除の取り下げIDを閉包の起点へ加える製品修正後の確認。
比較対象は [PR #90 / R14](2026-10-26-core-st-performance-after-r14.md)。

## 結果

標準 runner は100件・1000件を完走、終了コード **0**、**PASS**。
期間 `2026-10-25T23:40:40.530967+00:00`〜`2026-10-25T23:43:45.191536+00:00`。
各水準 warm-up 5回と本計測100回、並列度1。失敗・timeout・欠測0、除外なし。
nearest-rank の50番目・95番目を独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 144.935 | 184.315 | PASS | 達成 | 達成 |
| 1000 | 207.687 | 245.808 | PASS | 達成 | 未達 |

## 一覧 API（1000件）

runner 終了後、同じDB・MemoryClient・timeout=30秒で5回連続計測。
呼出し前から完全な型付き応答1000件の検証完了まで。各回1000件、失敗/欠測なし。
PR #90 と同じ計測境界、キャッシュ消去なし。

| 回 | 秒 |
|---|---:|
| 1 | 0.644772 |
| 2 | 0.653884 |
| 3 | 0.642478 |
| 4 | 0.650912 |
| 5 | 0.673952 |

中央値 **0.650912秒**、最大値 **0.673952秒**。
PR #90 は単回0.688048秒、PR #85 は単回0.267094秒。
中央値の PR #90 比は -0.037136秒。

## 一覧 API の内訳・回帰調査

runner 終了後の別プロセスで READ ONLY / REPEATABLE READ の同じ1000件 snapshot を使用。
`projection_input` → `read_projection_evidence` → `project` を各5回、交互順序で実行。
R14 は d811f86 の transaction class、R12対照は現行 class の `candidates_for_turns` だけを
1b216bb のメソッドに置換し history_ids を無視する診断専用 class。
稼働 server・runner・リポジトリには適用していない。接続確立・HTTP・直列化・型検証は含まない。
SQL 時間は client execute+fetchall/fetchone、Python復元処理は input/evidence の残余に含む。
transaction設定・所有者取得の2 SQL は集計外。全 SQL と各時間、現行/R12対照の全 SQL の
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) は要約 JSON に収録。

中央値（ms）：

| 診断 | 投影入力 | 証拠入力 | 投影計算 | 合計 | SQL計 | SQL回数 |
|---|---:|---:|---:|---:|---:|---:|
| r15 | 586.098 | 31.785 | 33.488 | 657.627 | 501.017 | 29 |
| r14 | 588.096 | 32.267 | 33.530 | 657.437 | 492.155 | 29 |
| r15_with_r12_candidates | 212.198 | 32.649 | 38.274 | 279.391 | 108.644 | 29 |

**回帰ありと判断する。PR #89 の `candidates_for_turns` の SQL 前絞り込みが主因。**
5回のHTTP測定は0.642〜0.674秒に集中し、R12時点の単回0.267秒との差を単なる単回の外れ値では説明しにくい。
同一snapshotで当該メソッドだけをR12 SQLへ戻した対照では、処理全体の中央値が
657.627msから279.391msへ短縮（差378.236ms）。当該SQL（query_index=15、0始まり）は
373.174msから2.826msへ短縮した。R14 classの全体中央値657.437msは現行とほぼ同じで、
PR #92 による追加回帰はこの一覧経路では観測しない。

一覧の投影・証拠入力は各回29 SQL、N+1によるSQL本数の増加はない（transaction設定等を除く）。
うち `candidates_for_turns` は空のhistory対象で1回と、1000件のhistory対象で1回。
EXPLAINでは後者の `memory_candidates_turn` Index Scanの下に、
`memory_generated_turn` の相関 SubPlan が Actual Loops=1000で現れる。
内側の Index Cond は character_id / turn_id に加えて `memory_id=ANY(<1000 IDs>)` を含む。
最初のEXPLAINは実行365.305ms（planning0.373ms）、R12対照は実行0.264ms。
現行でShared Read Blocks=0、SubPlan Shared Hit Blocks=3000。
ディスクのcold readではなく、相関索引検索の繰返しに時間がかかっている。

別の読み取り専用SQL診断を各3回実施（要約JSONの `sql_variants`）：

| SQL | EXPLAIN実行時間3回 ms |
|---|---|
| 現行 | 377.798 / 372.226 / 391.992 |
| JOIN + DISTINCT案 | 1.627 / 1.364 / 1.446 |
| 内側のhistory_id ANYだけ除去 | 2.468 / 2.491 / 2.434 |
| 同一ID比較だけ残す（jsonb意味同値分岐を除去） | 1.932 / 1.556 / 1.739 |

history_idsのANYだけを除去してもSubPlanは1000回だが、内側の単回時間は約0.001msとなる。
同一ID比較だけにするとhashed SubPlanが1回に変わる。
したがってこのデータでは「jsonb演算自体が重い」とは断定しない。
jsonb同値比較のORを含む相関EXISTSと、大きなANY配列を内側の索引条件に置く組合せが
遅い実行計画を生むことが主因である。各変更は計画も変えるので、CPU命令単位の内訳ではない。

**修正案（未実装）**：history_idsあり/なしを分岐し、ありの場合は選択済みhistoryを一度絞って
`memory_candidates` と character_id / turn_id で JOIN、ID一致または既存のjsonb意味同値比較を
維持し、`SELECT DISTINCT c.memory_id` でEXISTSの重複排除契約を保つ。
診断のJOIN案はhistory_idsとturn_idsの両制限・全jsonb比較を維持し、この1000件では元SQLと
1000 IDが一致。nullable history_idsの既存経路は別SQLとして維持する。
SQL案全文とEXPLAINは要約JSONに収録。既存turn索引は利用済みで、索引追加だけを主案にしない。

この案は独立した1turn/1memoryだけの合成データでのSQL診断であり、正式な修正受入ではない。
親の別タスクで、旧新版の意味同値・同一turn内の非同値・重複JOIN・空集合・NULL・
取り下げ/作者削除・relationsの各契約をTDDで確認し、一覧HTTPと部分再構築の両方を再測定する。
単純にhistory_ids制限やjsonb分岐を消す上記2つの条件除去は、原因切り分け専用で修正案ではない。

`projection_neighbors` は `MemoryService.active` → `PostgresMemoryListing.read` →
`projection_input` の一覧経路に登場せず、今回の一覧遅延の直接原因ではない。
閉包・削除・再構築経路における同メソッド自体の性能は今回NOT RUN。
旧revisionのserver全体を再buildしてHTTPを測った比較でもなく、全データ形状への結論には拡張しない。


## 1000件の準備時間

前回同様、Docker UTC アクセスログの応答ヘッダー出力境界を使用。
本文転送・型検証完了は含まない。投入と索引は並行し、独立の実行時間に分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session応答 → 一覧事前確認応答 | 16.679168 |
| 追加900件：100件最終想起応答 → 1000件一覧事前確認応答 | **129.442358** |
| うち追加900件投入 | 61.137008 |
| 残りの索引待ち・一覧事前確認 | 68.305350 |
| 累積1000件準備（100件水準の想起を除く） | **146.121526** |

追加900件区間 `2026-10-25T23:41:13.117788266Z`〜`2026-10-25T23:43:22.560146955Z`。
最終1000/1000文書 ready。

## PR #90 との比較

| 件数 | PR #90 p50 / p95 ms | 今回 p50 / p95 ms | 差 p50 / p95 ms |
|---|---:|---:|---:|
| 100 | 153.100 / 171.948 | 144.935 / 184.315 | -8.165 / +12.366 |
| 1000 | 234.577 / 263.965 | 207.687 / 245.808 | -26.890 / -18.156 |

追加900件準備は118.111528秒から129.442358秒（+11.330830秒）。
累積準備は134.120092秒から146.121526秒（+12.001434秒）。
想起の差は同一条件の別試行の観測であり、PR #92 の因果効果とは断定しない。

## 条件・runner 無変更の証跡

- 隔離 worktree `/tmp/wt-perf-r15`、branch `test/phase1-performance-st-after-r15`。
  main checkout は変更せず、コミット対象は報告2ファイルと既存レポートのリンクのみ。
- 正本4資料を確認。runner / support / latency / DB初期化 / Compose は一時変更なし。
  wrapper、mount差し替え、overrideはなし。外側の収集scriptは起動・記録・終了後診断のみ。
  索引待ち1800秒、一覧事前確認30秒、想起30秒は既定値のまま。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423`。
  主要4ファイルの実 image hash とホストが一致し、実行後も無変更。manifestも事前/事後hash一致。
- 専用 project `memory-core-perf-r15-20261026`、新規 tmpfs 合成DB。起点からimageをbuild。
  PostgreSQL18.6 / vector0.8.2 / pg_bigm1.2 / migration0014。実 image ID はmanifestに記録。
- loopback のみ（DB55439 / Memory18039 / embedding18049 / reranker18050）。
  Python3.12.3 / uv0.8.22 / frozen lock、UV_CACHE_DIR は /tmp 配下。
  Intel Core i7-14700K / RTX4070 Ti SUPER / driver610.88 / Ubuntu WSL2。
- ローカル GGUF の digest を検証し採用定義と一致：embedding
  `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`、reranker
  `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` は前回と同じ。
  ctx/batch/ubatch=2048、parallel=1、GPU layers=99、threads/threads-batch=8、cache-ram=0。
  embedding pooling=cls / 1024次元、reranker pooling=rank / reranking。
- 合成Turn、入力「日本語」3文字。抽出fake、embedding/検索/reranker/tokenizerは実構成。
  candidate_limit200 / rerank_limit20 / result_limit5 / token_limit1500 / 閾値-2。
  モデルロード済み・索引ready・warm-up後、OS/DB/GPUキャッシュ消去なし。
  SQL診断と品質検査は性能計測後に分離。

## 検証・cleanup・未実施

ruff check / ruff format --check: **PASS**（終了0、319 files）。その他の必須品質検査は **NOT RUN**。Draft PR 作成後に実施する。

cleanup: **PASS**。今回起動した `memory-core-perf-r15-20261026` と `memory-core-perf-r15-embedding` / `memory-core-perf-r15-reranker` を停止・削除。専用 container / network / volume とモデル container の残存0を各コマンドの終了0で確認。他プロジェクトには操作していない。

生証跡は `/tmp/core-st-performance-after-r15-20261026`、hashは要約JSON。
秘密・利用者ホーム絶対パス・モデル本体はコミットしない。製品コード修正・mergeはしない。

未実施：10000件、cold-start、並列度>1、長文/実データ、モデル品質再評価、設定比較、
機能ST再実行、旧revision server全体の再build/同条件HTTP比較。
短い合成入力を超える性能・意味的品質はこの試験では受け入れない。
