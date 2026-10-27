# Phase 1 実モデル性能 ST：R9 統合後の最終確認

[要約JSON・全210標本](2026-10-25-core-st-performance-after-r9-summary.json)。
起点 `291f62d991b143d9823e4f482b2886fbc55263ba`（epic/phase1-consistency、#74 / PR #75、ADR 0024、migration 0014）。
比較対象は [PR #72 の正式測定](2026-10-25-core-st-performance-after-r7.md)（e0c82e0、migration 0013）。

## 結果

**標準 runner は100件・1000件を完走、終了コード0、必達 PASS。**
2026-10-25 UTC に実行。各水準 warm-up 5/5、本計測100/100成功、失敗・timeout・欠測0。
warm-up は別記し、除外標本なし。nearest-rank の50番目・95番目を全100標本から独立再照合した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 151.938 | 167.551 | PASS | 達成 | 未達 |
| 1000 | 230.704 | 253.388 | PASS | 達成 | 未達 |

一覧 API（1000件）は **0.189505秒**。runner 完走後、同じ DB に標準 MemoryClient で1回測定し、
完全な型付き応答1000件の検証まで含む。timeout は既定と同じ30秒。
runner 内の一覧事前確認の単独時間は未計測。

## 1000件の準備時間

前回と同じく、runner・アプリへ計時処理を挿入せず、Docker の UTC 付き HTTP アクセスログの
応答ログ出力境界で算出。ヘッダー送信時点であり、本文転送・型検証完了ではない。
driver の処理と一覧事前確認を含む。抽出・索引は投入と並行するため独立の処理時間には分解しない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session作成応答 → 100件一覧事前確認応答 | 13.040515 |
| 追加900件：100件水準の最終想起応答 → 1000件一覧事前確認応答 | **107.452103** |
| うち追加900件投入：最終想起応答 → 最終Turn投入応答 | 48.674262 |
| うち残りの索引待ち・一覧事前確認 | 58.777841 |
| 1000件までの累積準備（100件水準の想起を除く） | **120.492618** |

追加900件は `2026-10-25T08:55:03.416867068Z`〜`2026-10-25T08:56:50.868970744Z`。
最終1000/1000文書 ready。既定の索引待ち1800秒以内で完了した。

## PR #72 との比較

| 件数 | PR #72 p50 / p95 ms | 今回 p50 / p95 ms | 差 p50 / p95 ms |
|---|---:|---:|---:|
| 100 | 151.021 / 174.363 | 151.938 / 167.551 | +0.917 / -6.812 |
| 1000 | 232.648 / 259.573 | 230.704 / 253.388 | -1.944 / -6.185 |

| 指標 | PR #72 秒 | 今回 秒 | 差 秒 |
|---|---:|---:|---:|
| 一覧 API | 0.176466 | 0.189505 | +0.013039 |
| 追加900件準備 | 159.073006 | 107.452103 | -51.620903 |
| 累積1000件準備 | 176.913309 | 120.492618 | -56.420691 |

今回の観測では、1000件の準備と想起 p50 / p95 に増加はなく、一覧 API は約7.4%増えた。
p50 目標は両件数とも引き続き未達。単一の逐次測定なので、統計的な回帰なしとは断定しない。
候補 / rerank / 返却数は100件で100 / 20 / 5、1000件で290 / 20 / 5（各105回）。
PR #72 の1000件候補は320であり、同一候補集合の比較ではない。
candidate_limit=200 は各検索経路の上限で、候補は和集合。負荷・クロック・キャッシュの変動も含み、
他セッションの CPU 負荷やクロックは制御していない。差を ADR 0024 の変更だけに帰属させない。再表明の品質・同一 SPO 集約の意味的な受入はこの測定の対象外。

## 条件と runner 無変更の証跡

- 隔離 worktree `/tmp/wt-perf-r9`、branch `test/phase1-performance-st-after-r9`。
  main checkout のファイルは変更していない。変更は報告2ファイルと既存レポートへのリンクのみ。
- 正本は docs/core-st.md、compose.core-st.yml、compose.core-st.performance.yml、tests/st/performance.py、manifest template。
  runner / support / latency / DB初期化ツール / Compose は一時変更なし。
  wrapper・mount差し替え・monkeypatch・ローカル override は使用していない。
  外側 shell はログと終了コードだけを記録し、runner の待ち時間を変えていない。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423` は PR #72 と一致。
  support / latency / DB初期化ツールもホスト・実 Memory image で一致。manifest は実行前に固定し、終了後 hash 一致。
  索引待ち1800秒・一覧事前確認30秒・想起30秒。
- 専用 project `memory-core-perf-r9-20261025`、新規 tmpfs 合成 DB。
  loopback 公開のみ（DB 55439 / Memory 18039 / embedding 18049 / reranker 18050）。
  起点から DB / client image を build。実 Memory image と manifest の client image は一致。
  PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2、実 DB の migration は0014。
- Intel Core i7-14700K、28 logical CPUs、NVIDIA RTX 4070 Ti SUPER 16376 MiB、driver 610.88。
  Ubuntu / WSL2、Python 3.12.3、uv 0.8.22、frozen lock、UV_CACHE_DIR は /tmp 配下。
- ローカル GGUF を hash し、採用定義・PR #72 と一致。
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
export PATH=/tmp/uvenv/bin:$PATH UV_CACHE_DIR=/tmp/uv-cache-perf-r9
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-after-r9-20261025
# 起点からbuild、モデルhealth確認、manifest固定後
# 同じComposeファイルで up -d --no-build --wait memory を実施済み
docker compose -p memory-core-perf-r9-20261025 -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 検証・cleanup・未実施

ruff check / ruff format --check（308 files）、mypy（308 source files）、UT 1112件、IT1 935件、
実 PostgreSQL 724件（743.73秒）、lock / shell / Compose 検査、wheel build・独立環境への
locked install/import は **PASS**。追加品質試験は Draft PR #76 作成後に実施した。

初回 IT1 は sandbox 内で約5分進捗がなく、自分の pytest のみを中断（終了143、PASS扱いなし）。
SIGINT では終了せず SIGTERM で終了。原因は未確定。sandbox 外で全 IT1 を再実行し終了0。
再実行時は診断用 faulthandler_timeout=60（スタック出力のみ）を指定し、試験の timeout や成功条件は変更していない。
性能 runner の実行とは別である。

品質 DB は標準 tools/with-test-postgres.sh の新規合成 tmpfs DB（network none、公開ポートなし、cpus=1 / memory=1g）。
受入 DB と別 image ID（両方の実 ID・PostgreSQL 18.6 の確認値は JSON）を使い、受入結果と混ぜていない。
ホストの初回 uv sync は sandbox の DNS 制限で FAIL。ネットワーク許可下の frozen sync 再試行は PASS。
ST の build / 起動 / runner 自体には失敗なし。

今回起動した専用 ST project とモデル2 container だけを停止・削除し、container / network / volume 残存0を確認（PASS）。
追加品質試験の自分の DB container と一時 socket も削除・消失を確認。
既存の他 project の container には操作していない。生証跡は `/tmp/core-st-performance-after-r9-20261025`、
hash は要約 JSON。秘密・利用者ホーム絶対パス・モデル本体はコミットしない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、設定比較、機能ST再実行、反復実験。
短い合成入力と指定件数を超える性能は保証しない。merge はしない。
