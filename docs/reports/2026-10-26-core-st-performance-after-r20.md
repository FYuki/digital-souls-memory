# Phase 1 実モデル性能 ST：R20 統合後の最終確認

[要約JSON・全210標本・固定manifest](2026-10-26-core-st-performance-after-r20-summary.json)。
起点 `3c151b5c7a7a737b7baa247dd4c5ccdaa705e149`（epic/phase1-consistency、#104 / PR #106・#105 / PR #107 統合済み、migration 0014）。
比較対象は [PR #103 / R18](2026-10-26-core-st-performance-after-r18.md)。

## 結果

標準 runner は100件・1000件を完走、終了コード **0**、**PASS**。
期間 `2026-10-26T11:53:23.613637+00:00`〜`2026-10-26T11:56:33.073540+00:00`。
各水準 warm-up 5回と本計測100回を分離保存、並列度1。
全210呼出しに失敗・timeout・欠測なし、除外なし。nearest-rank の50番目・95番目を独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 152.918 | 171.606 | PASS | 達成 | 未達 |
| 1000 | 222.374 | 253.690 | PASS | 達成 | 未達 |

## 一覧 API（1000件）

runner終了後、同じDB・MemoryClient・timeout=30秒で5回連続計測。
呼出し前から完全な型付き応答1000件の検証完了まで。全回1000件、失敗・欠測なし。
PR #103と同じ計測境界。キャッシュ消去なし。

| 回 | 秒 |
|---|---:|
| 1 | 0.301696 |
| 2 | 0.291769 |
| 3 | 0.323703 |
| 4 | 0.289816 |
| 5 | 0.294446 |

中央値 **0.294446秒**、最大 **0.323703秒**。
PR #103の中央値0.256941秒に対し **+0.037505秒（+14.6%）**。
今回の5回では前回より遅く、改善は確認できなかった。最大も前回0.271404秒から増加した。別時刻の単一実行同士の比較であり、PR #107単独の因果効果や統計的有意差は断定しない。
fixtureのSQL時間と、この型付きHTTP応答全体の時間は計測境界が異なる。

## 1000件の準備時間

PR #103と同じDocker UTCアクセスログの応答ヘッダー出力境界。
本文転送・型検証完了は含まない。投入と索引は並行し、独立の実行時間に分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session応答 → 一覧事前確認応答 | 14.972727 |
| 追加900件：100件最終想起応答 → 1000件一覧事前確認応答 | **135.511745** |
| うち追加900件投入 | 59.375393 |
| 残りの索引待ち・一覧事前確認 | 76.136352 |
| 累積1000件準備（100件水準の想起を除く） | **150.484472** |

追加900件区間 `2026-10-26T11:53:54.112397221Z`〜`2026-10-26T11:56:09.624142755Z`。
最終1000/1000文書ready。

## PR #103との比較

| 件数 | PR #103 p50 / p95 ms | 今回 p50 / p95 ms | 差 p50 / p95 ms |
|---|---:|---:|---:|
| 100 | 136.177 / 162.856 | 152.918 / 171.606 | +16.741 / +8.751 |
| 1000 | 222.822 / 252.281 | 222.374 / 253.690 | -0.447 / +1.409 |

追加900件準備は118.211290秒から135.511745秒（+17.300455秒）。
累積準備は132.019809秒から150.484472秒（+18.464663秒）。
想起性能の差も同一条件の別試行での観測値として扱う。

## 条件・runner無変更の証跡

- 隔離worktree `/tmp/wt-perf-r20`、branch `test/phase1-performance-st-after-r20`。main checkoutは変更していない。
- 正本 docs/core-st.md、Compose 2本、tests/st/performance.py、manifest templateを確認。
  runner / support / latency / DB初期化 / Compose は一時変更もなし。wrapper・mount差し替え・overrideなし。
  外側の収集scriptは起動・証跡収集・終了後の一覧測定のみ。runnerコマンドは要約JSONに記録。
  索引待ち1800秒、一覧事前確認30秒、想起30秒は既定値。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423`。
  主要4ファイルの実image内hashとホストが一致。計測後も主要6ファイル無変更。manifestも事前/事後hash一致。
- 専用project `memory-core-perf-r20-20261026`、新規tmpfs合成DB。起点からimageをbuild。
  PostgreSQL18.6 / vector0.8.2 / pg_bigm1.2 / migration0014。実image IDはmanifestに記録。
- loopbackのみ（DB55439 / Memory18039 / embedding18049 / reranker18050）。
  Python3.12.3 / uv0.8.22 / frozen lock、UV_CACHE_DIRは `/tmp` 配下。
  Intel Core i7-14700K / RTX4070 Ti SUPER / driver610.88 / Ubuntu WSL2。
  ホストは共有環境で、計測中に他projectのDB containerが存在した。背景負荷の完全な隔離はしていない。
- GGUF digestをローカルファイルで検証し採用値と一致：embedding
  `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`、reranker
  `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` はPR #103と同じ。
  ctx/batch/ubatch=2048、parallel=1、GPU layers=99、threads/threads-batch=8、cache-ram=0。
  embedding pooling=cls / 1024次元、reranker pooling=rank / reranking。
- 合成Turn、入力「日本語」3文字。抽出fake、embedding/検索/reranker/tokenizerは実構成。
  candidate_limit200 / rerank_limit20 / result_limit5 / token_limit1500 / 閾値-2。
  モデルロード済み・索引ready・warm-up後、OS/DB/GPUキャッシュ消去なし。

## 検証・cleanup・未実施

- ruff_check: **PASS**（終了0）
- ruff_format_check: **PASS**（終了0）

通常品質ゲートは性能計測後に実施。初回host uv syncはsandboxのDNS制限でFAIL、権限昇格後のfrozen syncは終了0。

cleanup: **NOT RUN**（計測資源を保持中、後続で実施）。

生証跡 `/tmp/core-st-performance-after-r20-20261026`、各hashは要約JSON。
秘密・利用者ホーム絶対パス・モデル本体はコミットしない。製品コード修正・mergeなし。

未実施：10000件、cold-start、並列度>1、長文/実データ、モデル品質再評価、設定比較、
機能ST再実行、旧revisionのHTTP全体再測定、closure/削除経路のprojection_neighbors性能測定。
