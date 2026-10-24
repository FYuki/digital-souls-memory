# Phase 1 実モデル性能 ST：runner 無変更での再測定

[要約JSON・全標本](2026-10-24-core-st-performance-unmodified-summary.json)。起点 `0d471408d3a5089fb9a00ff6615e46f0fd8ff8cf`（PR #58 / #59 統合後）。

## 結果

標準 runner は100件・1000件を **PASS** で完走し、終了コード **0**。
各水準でwarm-up 5/5、本計測100/100成功。失敗・timeout・欠測0。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 139.340 | 155.220 | PASS | 達成 | 達成 |
| 1000 | 213.386 | 232.242 | PASS | 達成 | 未達 |

1000件の一覧APIは **0.237611秒**。前回72.786秒の約 **1/306.3**。
これはrunner完走後に、同じDB・標準MemoryClient・timeout 30秒で別途1回計測した値で、型付き応答の検証まで含む。
runner自身の一覧事前確認も既定30秒以内に成功したが、その呼出し単独の時間は計測していない。

## 実行条件と無変更の証明

- `test/phase1-performance-st-unmodified` の隔離worktreeを使用。main checkoutは変更していない。
- `docs/core-st.md`、標準Compose 2枚、`tests/st/performance.py`、manifest templateが正本。
  追跡されたrunner・tools・Compose・製品コードは一時変更なし。mountによる置換、monkeypatch、待ち時間延長なし。
  最終差分はレポート2件と既存STレポートへのリンクのみ。
- `tools/core_st_db.py` は原本のままAlembic headと照合し **0012** まで初期化成功。
  runner・support・初期化ツールのホストとコンテナ内SHA-256一致。原本hashをJSONに保存。
- 専用project `memory-core-perf-unmod-20261024`、新規tmpfs合成DB。
  DB/Memory/モデルの公開先はloopbackのみ（55439 / 18039 / 18049 / 18050）。ローカルCompose overrideも使用していない。
- 環境：Ubuntu / WSL2; Python 3.12.3; uv 0.8.22; Docker Compose dedicated tmpfs synthetic DB。Intel Core i7-14700K, 28 logical CPUs; NVIDIA RTX 4070 Ti SUPER 16376 MiB; driver 610.88。
  Docker 29.6.0 / Compose 5.2.0。
- モデル2件の実ファイルSHA-256は前回と一致（revisionは外部再取得せず、採用定義と照合）。
  bge-m3 Q8_0 / CLS / 1024次元：`aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
  bge-reranker-v2-m3 Q8_0：`a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cpp imageは前回と同じ `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは `--embeddings --pooling cls`、rerankerは `--embeddings --pooling rank --reranking`。
- DB image `sha256:f3f0e9dad59c58bed88b9ea223e2e679f2646adbb480063a6d064debc98c7e14`、client image `sha256:002505aef9806b5451610d784c412934f1c4dc11b14cd8c909efc797174cc6f9`。
  DB image IDは前回と異なる。PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2は一致。
  clientは起点ソースとfrozen lockからbuildした。
- 入力・記憶本文「日本語」（3文字）の合成データのみ。抽出はfake、索引・embedding・検索・reranker・tokenizerは実構成。
  candidate_limit=200（各検索経路）、rerank_limit=20、result_limit=5、token_limit=1500、閾値=-2。
- 各水準warm-up 5回を別記録、本計測100回、並列度1、nearest-rank。
  クライアント呼出し直前から型付き完全応答まで計測。全標本保存、400ms打切り・失敗や遅い標本の除外なし。
- 既定の索引待ち1800秒、一覧事前確認30秒、想起timeout 30秒を維持。
  モデルロード済み・索引ready後の定常測定で、OS/DB/GPUキャッシュは消去していない。
  負荷・GPUクロックは固定しておらず、前回との差をコードだけに帰属しない。
- 実行UTC：2026-10-24T09:02:03.789493+00:00 ～ 2026-10-24T09:05:27.045962+00:00。外部から約2秒間隔の読取SQLで進捗観測。
  品質チェックと診断は受入測定後に実行した。

```sh
# 実行前にmanifest.jsonを固定し、同一モデルを起動済みにする
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-unmodified-20261024
docker compose -p memory-core-perf-unmod-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml build
docker compose -p memory-core-perf-unmod-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml up -d --no-build --wait memory
docker compose -p memory-core-perf-unmod-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 準備時間と前回比較

100件は最初の追加TurnのHTTP成功応答から全100件readyの外部観測まで約 **19.984秒**。
1000件は100件の計測終了後に追加900件を投入し、最初の追加Turn成功応答から全1000件ready観測まで約 **148.290秒**。
ready到達は最後の未完了観測から最初の完了観測までの区間（**146.206～148.290秒**）内。
最初の1件の要求開始から応答までの時間は含まない。前回159.012秒は別の観測方法による追加900件の準備値であり、厳密な差分比較はしない。
空DBで最初のTurn成功応答から1000件ready観測までは約 **181.658秒**（100件の計測を含む）。
外部観測であり、runner内部に計時を追加していない。

前回は想起必達が両水準PASSでも、一覧事前確認timeoutによりrunner全体はFAIL、初期化assertの一時差し替えも必要だった。
今回は両方とも原本のまま成功。前回の想起p50 / p95は100件150.257 / 164.859ms、1000件194.338 / 231.057ms。
今回の候補・rerank・返却は100件で100 / 20 / 5、1000件で310 / 20 / 5（各105回）。
前回1000件の候補和集合295件とは異なるが、各経路200・rerank20・返却5の条件は同じ。
候補内容や品質の同一性は未評価。全210 ProvisionRecord監査結果はJSONを参照。

## 別診断

受入測定後、同じ1000件DB・実モデルに対する別HTTP APIへ一時診断コードを載せ、warm-up 2回＋10回を採取した。
受入アプリの背景workerはidle、診断APIのlifespanはoff。追跡ファイルは変更せず、診断コードはコミットしない。
診断10回の平均：embedding 3.650ms、検索49.006ms（意味22.616 / 語彙16.895）、
rerank 45.478ms、provision保存50.263ms、HTTP全体168.799ms。
接続1回、SQL execute 23回、commit 6回、tokenizer 7回。処理別の詳細と診断標本はJSONを参照。重なる区間を含むため加算しない。受入p50 / p95そのものの分解ではない。

## 検証・cleanup・未実施

ruff check / ruff format --check、mypy、UT、IT1、実PostgreSQL試験、wheel build・独立locked install/importを実行。結果・件数はJSONに保存。
初回uv syncはsandboxのDNS制約でFAIL、その後の許可付きfrozen syncはPASS。測定に使った依存は固定lockから取得した。
今回起動した専用ST/品質projectとモデル・診断containerだけを停止・削除し、container / network / volume残存0を確認。
初回残存照会では削除前の診断containerを検出（FAIL）。診断container削除後の再照会は残存0（PASS）で、初回記録もローカル証跡に保持。
既存livekit containerの稼働継続も確認。mergeは行っていない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、server設定比較、機能ST再実行、100件の別診断。
今回のデータ量・短い入力分布を超える性能は保証しない。
