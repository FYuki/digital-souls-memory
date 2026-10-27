# Phase 1 実モデル性能 ST：R14 統合後の最終確認

[要約JSON・全210標本](2026-10-26-core-st-performance-after-r14-summary.json)。
起点 `d811f86aed4d5d8a7775c25c99b71d9c16bc060c`（epic/phase1-consistency、#86 / PR #88・#87 / PR #89 統合済み、migration 0014）。
比較対象は [PR #85 の測定](2026-10-26-core-st-performance-after-r12.md)（1b216bb）。
relation の取り消し修正（PR #88）と、必要な旧 ID に同値探索・同期を限定し turn 索引を追加した PR #89 統合後の確認。

## 結果

**標準 runner は100件・1000件を完走、終了コード0、必達 PASS。**
実行期間 `2026-10-25T21:33:45.367792+00:00`〜`2026-10-25T21:36:37.788244+00:00`。
各水準 warm-up 5/5、本計測100/100成功。失敗・timeout・欠測0、除外標本なし。
warm-up は別記し、nearest-rank の50番目・95番目を全100標本から独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 153.100 | 171.948 | PASS | 達成 | 未達 |
| 1000 | 234.577 | 263.965 | PASS | 達成 | 未達 |

一覧 API（1000件）は **0.688048秒**。
runner 完走後、同じ DB に標準 MemoryClient で1回測定し、完全な型付き応答1000件の検証まで含む。
timeout は既定と同じ30秒。runner 内の一覧事前確認の単独時間は未計測。

## 1000件の準備時間

runner・アプリに計時処理を挿入せず、前回と同じ Docker UTC アクセスログの
応答ログ出力境界から算出。ヘッダー送信時点であり、本文転送・型検証完了ではない。
driver と一覧事前確認を含む。抽出・索引は投入と並行し、独立の処理時間には分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session作成応答 → 100件一覧事前確認応答 | 16.008564 |
| 追加900件：100件水準の最終想起応答 → 1000件一覧事前確認応答 | **118.111528** |
| うち追加900件投入：最終想起応答 → 最終Turn投入応答 | 55.059231 |
| うち残りの索引待ち・一覧事前確認 | 63.052297 |
| 1000件までの累積準備（100件水準の想起を除く） | **134.120092** |

追加900件は `2026-10-25T21:34:15.315746853Z`〜`2026-10-25T21:36:13.427274218Z`。
最終1000/1000文書 ready。既定の索引待ち1800秒以内で完了。

## PR #85 との比較

| 件数 | PR #85 p50 / p95 ms | 今回 p50 / p95 ms | 差 p50 / p95 ms |
|---|---:|---:|---:|
| 100 | 153.348 / 169.529 | 153.100 / 171.948 | -0.248 / +2.419 |
| 1000 | 239.105 / 267.108 | 234.577 / 263.965 | -4.528 / -3.143 |

| 指標 | PR #85 秒 | 今回 秒 | 差 秒 |
|---|---:|---:|---:|
| 一覧 API | 0.267094 | 0.688048 | +0.420954 |
| 追加900件準備 | 116.525632 | 118.111528 | +1.585896 |
| 累積1000件準備 | 130.119357 | 134.120092 | +4.000735 |

必達と p95 目標は両件数で達成、p50 目標は両件数で未達。
単一の逐次測定であり、統計的な回帰有無は断定しない。候補集合・負荷・クロック・キャッシュを
固定しておらず、差を PR #89 の変更だけに帰属させない。意味的品質の受入はこの測定の対象外。

## 独立記憶1件の部分再構築診断

計測終了後の1000件合成DBで、READ ONLY transaction 内から生成記憶1件を起点に
projection_neighbors の閉包が収束するまで調べ、接続の execute 呼出数を数えた。
R12 は起点 1b216bb の transaction class 全体、R14 は実 image の class を使用。
両方をR14のDB schema上で実行した比較であり、旧版server全体の再測定ではない。
runner・アプリへの計時挿入や待ち時間の変更はない。

| 指標 | R12 class | R14 class |
|---|---:|---:|
| 閉包サイズ | 1 | 1 |
| 収束反復数 | 1 | 1 |
| projection_neighbors SQL execute回数 | 7 | 7 |

この独立ケースでは閉包・SQL数の削減は観測しなかった。
SQLの対象行は絞られているが、同値な旧新版・relationを持たない合成ケースなので、
PR #89 が狙う対象での改善や再構築全体のSQL数は評価していない。
memory_generated / memory_candidates / derived_memories / recall_documents は各1000件、
memory_relations / memory_candidate_relations は各0件。
全SQLと診断範囲は要約JSONの projection_diagnostic に記録した。
一覧APIの単回測定は前回比+0.421秒、準備も増加しており、全体の改善とは判断しない。

## 条件と runner 無変更の証跡

- 隔離 worktree `/tmp/wt-perf-r14`、branch `test/phase1-performance-st-after-r14`。
  main checkout のファイルは変更していない。変更は報告2ファイルと既存レポートへのリンクのみ。
- 正本は docs/core-st.md、compose.core-st.yml、compose.core-st.performance.yml、tests/st/performance.py、manifest template。
  runner / support / latency / DB初期化ツール / Compose は一時変更なし。
  wrapper・mount差し替え・monkeypatch・ローカル override は使用していない。
  外側の収集用 Python は起動・ログ・終了コード・後続計測のみを扱い、runner の待ち時間を変えていない。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423` は PR #85 と一致。
  support / latency / DB初期化ツールもホスト・実 Memory image で一致。manifest は実行前に固定し終了後 hash 一致。
  索引待ち1800秒・一覧事前確認30秒・想起30秒。
- 専用 project `memory-core-perf-r14-20261026`、新規 tmpfs 合成 DB。
  loopback 公開のみ（DB 55439 / Memory 18039 / embedding 18049 / reranker 18050）。
  起点から DB / client image を build。実イメージ ID と DB version は要約 JSON の manifest に記載。
  PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2、実 DB migration 0014。
- Intel Core i7-14700K, 28 logical CPUs; NVIDIA RTX 4070 Ti SUPER 16376 MiB; driver 610.88。
  Ubuntu / WSL2、Python 3.12.3、uv 0.8.22、frozen lock、UV_CACHE_DIR は /tmp 配下。
- ローカル GGUF を hash し採用定義・PR #85 と一致。
  embedding bge-m3 Q8_0 / CLS / 1024次元、SHA-256
  `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
  reranker bge-reranker-v2-m3 Q8_0、SHA-256
  `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` は前回と同一。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embedding は --embeddings --pooling cls、reranker は --embeddings --pooling rank --reranking。
- 入力「日本語」3文字、合成 Turn のみ。抽出は fake、embedding・検索・reranker・tokenizer は実構成。
  candidate_limit=200、rerank_limit=20、result_limit=5、token_limit=1500、閾値=-2。
  各水準 warm-up 5、本計測100、並列度1。完全な型付き応答までを計測。
  モデルロード済み・索引 ready 後の定常測定。OS / DB / GPU キャッシュ消去なし。
  性能測定中は少数のログ・DB件数読取のみ。品質試験は測定後に分離。

```sh
export PATH=/tmp/uvenv/bin:$PATH UV_CACHE_DIR=/tmp/uv-cache-perf-r14
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-after-r14-20261026
# 起点からbuild、モデルhealth確認、manifest固定後
# 同じComposeファイルで up -d --no-build --wait memory を実施済み
docker compose -p memory-core-perf-r14-20261026 -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 検証・cleanup・未実施

品質検査結果：

- ruff_check: **PASS**（終了0）
- ruff_format_check: **PASS**（終了0）
- mypy: **PASS**（終了0）
- ut: **PASS**（終了0）
- it1: **PASS**（終了0）
- postgres: **PASS**（終了0）
- lock: **PASS**（終了0）
- shell: **PASS**（終了0）
- compose: **PASS**（終了0）
- build: **PASS**（終了0）
- package: **PASS**（終了0）

追加品質試験は Draft PR 作成後に実施した。UT 1130件、IT1 1095件、POSTGRES 776件。すべてskip/xfail/xpassなし。

今回起動した専用 ST project とモデル2 container を停止・削除し、container / network / volume 残存0を確認（**PASS**）。品質試験専用DBとsocketも削除・消失を確認。他プロジェクトのcontainerには操作していない。
生証跡は `/tmp/core-st-performance-after-r14-20261026`、hash は要約 JSON。
秘密・利用者ホーム絶対パス・モデル本体はコミットしない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、設定比較、機能ST再実行、反復実験。
短い合成入力と指定件数を超える性能は保証しない。merge はしない。
