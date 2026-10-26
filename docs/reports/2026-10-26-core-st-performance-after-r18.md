# Phase 1 実モデル性能 ST：R18 統合後の最終確認

[要約JSON・全210標本・固定manifest](2026-10-26-core-st-performance-after-r18-summary.json)。
起点 `df28f4e43fd3693073e10246ba02aa70addb40bb`（epic/phase1-consistency、#95 / PR #97・#96 / PR #101・#100 / PR #102 統合済み、migration 0014）。
部分再構築・一覧APIのJOIN修正・ADR0024 Q-007統合後の最終確認。
比較対象は [PR #94 / R15](2026-10-26-core-st-performance-after-r15.md)。

## 結果

標準 runner は100件・1000件を完走、終了コード **0**、**PASS**。
期間 `2026-10-26T07:05:15.062854+00:00`〜`2026-10-26T07:08:04.295880+00:00`。
各水準 warm-up 5回と本計測100回、並列度1。失敗・timeout・欠測0、除外なし。
nearest-rank の50番目・95番目を独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 136.177 | 162.856 | PASS | 達成 | 達成 |
| 1000 | 222.822 | 252.281 | PASS | 達成 | 未達 |

## 一覧 API（1000件）

runner 終了後、同じDB・MemoryClient・timeout=30秒で5回連続計測。
呼出し前から完全な型付き応答1000件の検証完了まで。各回1000件、失敗/欠測なし。
PR #94 と同じ計測境界、キャッシュ消去なし。

| 回 | 秒 |
|---|---:|
| 1 | 0.271404 |
| 2 | 0.252772 |
| 3 | 0.252386 |
| 4 | 0.260132 |
| 5 | 0.256941 |

中央値 **0.256941秒**、最大値 **0.271404秒**。
PR #94 の中央値は0.650912秒、PR #85 の単回は0.267094秒。
今回の5回は0.252〜0.271秒で、約0.27秒の水準への復帰を確認。
中央値の PR #94 比は -0.393972秒。

## 1000件の準備時間

前回同様、Docker UTC アクセスログの応答ヘッダー出力境界を使用。
本文転送・型検証完了は含まない。投入と索引は並行し、独立の実行時間に分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session応答 → 一覧事前確認応答 | 13.808519 |
| 追加900件：100件最終想起応答 → 1000件一覧事前確認応答 | **118.211290** |
| うち追加900件投入 | 52.121663 |
| 残りの索引待ち・一覧事前確認 | 66.089627 |
| 累積1000件準備（100件水準の想起を除く） | **132.019809** |

追加900件区間 `2026-10-26T07:05:43.079411588Z`〜`2026-10-26T07:07:41.290701643Z`。
最終1000/1000文書 ready。

## PR #94 との比較

| 件数 | PR #94 p50 / p95 ms | 今回 p50 / p95 ms | 差 p50 / p95 ms |
|---|---:|---:|---:|
| 100 | 144.935 / 184.315 | 136.177 / 162.856 | -8.758 / -21.459 |
| 1000 | 207.687 / 245.808 | 222.822 / 252.281 | +15.134 / +6.473 |

追加900件準備は129.442358秒から118.211290秒（-11.231068秒）。
累積準備は146.121526秒から132.019809秒（-14.101717秒）。
想起の差は同一条件の別試行の観測であり、PR #102 の因果効果とは断定しない。

## 条件・runner 無変更の証跡

- 隔離 worktree `/tmp/wt-perf-r18`、branch `test/phase1-performance-st-after-r18`。
  main checkout は変更せず、コミット対象は報告2ファイルと既存レポートのリンクのみ。
- 正本4資料を確認。runner / support / latency / DB初期化 / Compose は一時変更なし。
  wrapper、mount差し替え、overrideはなし。外側の収集scriptは起動・記録・終了後診断のみ。
  索引待ち1800秒、一覧事前確認30秒、想起30秒は既定値のまま。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423`。
  主要4ファイルの実 image hash とホストが一致し、実行後も無変更。manifestも事前/事後hash一致。
- 専用 project `memory-core-perf-r18-20261026`、新規 tmpfs 合成DB。起点からimageをbuild。
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

cleanup: **NOT RUN**。今回起動した専用 project とモデル2台のみ、証跡回収後に削除する。

生証跡は `/tmp/core-st-performance-after-r18-20261026`、hashは要約JSON。
秘密・利用者ホーム絶対パス・モデル本体はコミットしない。製品コード修正・mergeはしない。

未実施：10000件、cold-start、並列度>1、長文/実データ、モデル品質再評価、設定比較、
機能ST再実行、旧revision server全体の再build/同条件HTTP比較。
短い合成入力を超える性能・意味的品質はこの試験では受け入れない。
