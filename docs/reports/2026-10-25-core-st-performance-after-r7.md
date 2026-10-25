# Phase 1 実モデル性能 ST：R7 修正統合後

[要約JSON・全210標本](2026-10-25-core-st-performance-after-r7-summary.json)。
起点 `e0c82e0a94d1182656bf84759b7d527c788eb768`（epic/phase1-consistency、#68 / PR #70統合済み）。
比較対象は [PR #69の正式測定](2026-10-24-core-st-performance-after-r6.md)（541d151、#68統合前）。
再起動で証跡を失った試行は最初からやり直した。

## 結果

標準runnerは100件・1000件を完走し、終了コード **0**、**PASS**。
各水準warm-up 5/5、本計測100/100成功。失敗・timeout・欠測0。warm-upは別記し、除外標本なし。
nearest-rankの50番目・95番目を全100標本から独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 151.021 | 174.363 | PASS | 達成 | 未達 |
| 1000 | 232.648 | 259.573 | PASS | 達成 | 未達 |

一覧API（1000件）は **0.176466秒**。runner終了後、同じDBへ標準MemoryClientで1回測定し、
完全な型付き応答検証を含む。timeoutは既定と同じ30秒。runner内の一覧事前確認の単独時間は未計測。

## 準備時間

runner・アプリへ計時処理を挿入せず、DockerのUTC付きHTTPアクセスログの応答ログ出力境界で算出。
ヘッダー送信時点であり、本文転送・型検証完了ではない。driverの処理と一覧事前確認を含む。
抽出・索引は投入と並行するため、独立した処理時間には分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session作成応答 → 100件一覧事前確認応答 | 17.840303 |
| 追加900件：100件水準の最終想起応答 → 1000件一覧事前確認応答 | **159.073006** |
| うち追加900件投入：最終想起応答 → 最終Turn投入応答 | 71.589144 |
| うち残りの索引待ち・一覧事前確認 | 87.483862 |
| 1000件までの累積準備（100件水準の想起を除く） | **176.913309** |

追加900件の区間は `2026-10-25T01:11:40.138700+00:00`〜`2026-10-25T01:14:19.211706+00:00`。
最終1000/1000文書ready、既定の索引待ち1800秒以内。

## 比較

| 件数 | PR #69 p50 / p95 ms | 今回との差 p50 / p95 ms | 中断した試行の値 p50 / p95 ms | 今回との差 p50 / p95 ms |
|---|---:|---:|---:|---:|
| 100 | 147.606 / 165.108 | +3.414 / +9.255 | 144.8 / 161.9 | +6.221 / +12.463 |
| 1000 | 208.747 / 245.452 | +23.901 / +14.121 | 205.2 / 237.9 | +27.448 / +21.673 |

| 指標 | PR #69 | 中断した試行の値 | 今回 | 差（PR #69 / 中断試行） |
|---|---:|---:|---:|---:|
| 一覧API 秒 | 0.258358 | 0.177 | 0.176466 | -0.081892 / -0.000534 |
| 追加900件準備 秒 | 165.245314 | 158.64 | 159.073006 | -6.172308 / +0.433006 |

中断した試行はユーザー提供の丸めた参考値で、生証跡は再起動で消失しており原記録の再検証はできない。
今回の標本には混ぜていない。PR #69と中断試行のrunner終了コードはいずれも0。
候補 / rerank / 返却数の全105回監査結果は要約JSON参照。PR #69の1000件候補は300、中断試行は280。
候補集合は各検索経路の和集合であり、candidate_limit=200は各経路の上限。
単一の逐次測定であり、候補の同一性・品質を再評価していない。負荷・クロック・キャッシュの変動を含み、
差をR7の修正だけに帰属させず、統計的な回帰なしとも断定しない。

## 条件とrunner無変更の証跡

- 隔離worktree `/tmp/wt-perf-r7`、branch `test/phase1-performance-st-after-r7`。main checkoutは変更していない。
  変更は報告・要約JSON・既存レポートへのリンクのみ。runner / support / tools / Compose / 製品コードの一時変更なし。
  wrapper・mount差し替え・monkeypatch・overrideは使用せず、外側shellは時刻と終了コードのみ記録。
- 正本は docs/core-st.md、compose.core-st.yml、compose.core-st.performance.yml、tests/st/performance.py、manifest template。
  manifestを実行前に固定し、終了後にhash一致を確認。説明欄・実DBとも **migration 0013**。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423` はPR #69と一致。
  support / latency / DB初期化ツールもホストと実コンテナで一致（JSON参照）。
  索引待ち1800秒・一覧事前確認30秒・想起30秒を変更していない。
- 専用project `memory-core-perf-r7-20261025`、新規tmpfs合成DB、loopback公開のみ（DB 55439 / Memory 18039 / モデル18049・18050）。
- Intel Core i7-14700K, 28 logical CPUs; NVIDIA RTX 4070 Ti SUPER 16376 MiB; driver 610.88。Ubuntu / WSL2、Python 3.12.3 / uv 0.8.22 / frozen lock。
  DB / clientを起点ソースからbuildし、manifestと実Memory containerのimage ID一致を照合。
  PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2。
- モデルは採用済みローカルファイル。embedding bge-m3 Q8_0 / CLS / 1024次元、実SHA-256
  `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
  reranker bge-reranker-v2-m3 Q8_0、実SHA-256 `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
  実ファイルをhashし、採用定義・PR #69と一致。モデル本体はコミットしない。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` はPR #69と同一。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは --embeddings --pooling cls、rerankerは --embeddings --pooling rank --reranking。
- 入力「日本語」（3文字）、合成Turnのみ。抽出はfake、embedding・検索・reranker・tokenizerは実構成。
  candidate_limit=200、rerank_limit=20、result_limit=5、token_limit=1500、閾値=-2。
  各水準warm-up 5回、本計測100回、並列度1。完全な型付き応答までを計測。
  モデルロード済み・索引ready後の定常測定。OS / DB / GPUキャッシュ消去なし。
  性能測定中は少数のログ・hash・DB件数の読取だけを行い、追加品質試験は後に実行。

```sh
export PATH=/tmp/uvenv/bin:$PATH UV_CACHE_DIR=/tmp/uv-cache-perf-r7
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-after-r7-20261025
# 起点からbuild、モデルhealth確認、manifest固定後
 docker compose -p memory-core-perf-r7-20261025 -f compose.core-st.yml -f compose.core-st.performance.yml up -d --no-build --wait memory
 docker compose -p memory-core-perf-r7-20261025 -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 検証・cleanup・未実施

ruff check / ruff format --checkはPASS。追加品質試験はDraft PR作成後に実施予定で、現時点はNOT RUN。

今回起動した専用ST projectとモデル2 containerだけを停止・削除し、container / network / volume残存0を確認（PASS）。
既存LiveKitは稼働継続。生証跡は `/tmp/core-st-performance-after-r7-20261025`、hashはJSONに保存。
秘密・利用者ホーム絶対パス・モデル本体はコミットしない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、設定比較、機能ST再実行、反復実験。
短い合成入力と指定件数を超える性能は保証しない。mergeはしない。
