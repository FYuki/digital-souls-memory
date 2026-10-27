# Phase 1 実モデル性能 ST：R5 修正統合後

[要約JSON・全標本](2026-10-24-core-st-performance-after-r5-summary.json)。
起点は `6176895a54be158876e3ead1d6f78c98a74b3c9b`（`epic/phase1-consistency`、PR #63 / #64 統合後）。
比較対象は [PR #60 の runner 無変更測定](2026-10-24-core-st-performance-unmodified.md)。

## 結果と比較

標準 runner は100件・1000件とも **PASS** で完走し、終了コード **0**。
各水準で warm-up 5/5、本計測100/100が成功。失敗・timeout・欠測は0で、全210標本をJSONへ保存した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 139.822 | 156.768 | PASS | 達成 | 達成 |
| 1000 | 221.223 | 242.906 | PASS | 達成 | 未達 |

| 件数 | 前回 p50 / p95 ms | 今回との差 p50 / p95 ms | 増加率 p50 / p95 |
|---|---:|---:|---:|
| 100 | 139.340 / 155.220 | +0.483 / +1.548 | +0.35% / +1.00% |
| 1000 | 213.386 / 232.242 | +7.837 / +10.663 | +3.67% / +4.59% |

1000件の一覧APIは **0.240513秒**（前回0.237611秒、+0.002902秒）。
runner完走後、同じDB・標準MemoryClient・timeout 30秒で別途1回測定し、型付き応答の検証まで含む。
runner自身の一覧事前確認も既定30秒以内に成功したが、その呼出し単独の時間は計測していない。

1000件の遅延は今回やや増えたが、必達・p95目標の達否は維持した。p50目標の未達も前回と同じ。
単一の逐次測定比較で、負荷・GPUクロック・キャッシュ・候補集合を固定した反復比較ではないため、差を #62 のSQL変更だけに帰属させない。
候補数 / rerank数 / 返却数は100件で100 / 20 / 5、1000件で300 / 20 / 5（各105回）。前回1000件の候補和集合は310件で、候補の同一性・品質は再評価していない。

## 条件と runner 無変更の証明

- `test/phase1-performance-st-after-r5` を隔離worktreeに作成し、main checkoutは変更していない。
  差分は本レポート・要約JSON・既存STレポートのリンクだけ。
- 正本は `docs/core-st.md`、`compose.core-st.yml`、`compose.core-st.performance.yml`、`tests/st/performance.py`、manifest template。
  runner・製品コード・tools・Composeは一時変更なし。wrapperによる差し替え・mount置換・monkeypatch・timeout変更なし。
  ローカルCompose overrideも使用していない。下記shellは終了コード記録だけを行った。
- runner SHA-256は `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423`。
  support・latency・初期化ツールもホストとコンテナ内で一致し、各hashをJSONに保存。
  既定の索引待ち1800秒、一覧事前確認30秒、想起timeout 30秒を維持した。
- 専用projectは `memory-core-perf-r5-20261024`。新規tmpfs合成DB、migration 0012。
  DB / Memory / モデル公開先はloopbackのみ（55439 / 18039 / 18049 / 18050）。
- Ubuntu / WSL2、Intel Core i7-14700K（28 logical CPUs）、NVIDIA RTX 4070 Ti SUPER 16376 MiB、driver 610.88。
  Python 3.12.3、uv 0.8.22、frozen lock。ハードウェアは前回と一致。
- モデル実ファイルのSHA-256を照合し、前回・採用定義と一致。モデルrevisionは外部から再取得していない。
  embedding bge-m3 Q8_0 / CLS / 1024次元：`aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
  reranker bge-reranker-v2-m3 Q8_0：`a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cpp imageは前回と同じ `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは `--embeddings --pooling cls`、rerankerは `--embeddings --pooling rank --reranking`。
- DB imageは `sha256:fdd3b6ffae6f632ea8d32c8ba64332cfd4e1b37e7f79faeae55ce2f0a0b8ab3d`、client imageは `sha256:14c6fbd908219a2a8c8c39c108deb9155a02b4cc2444afec0ff0e70939dc3214`。
  image IDは前回と異なり、clientは起点ソースからbuildした。DB実版は前回と同じ PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2。
- 入力・記憶本文は「日本語」（3文字）の合成データだけ。抽出はfake、embedding・検索・reranker・tokenizerは実構成。
  candidate_limit=200（各検索経路）、rerank_limit=20、result_limit=5、token_limit=1500、閾値=-2。
- 各水準warm-up 5回を本計測から分離、本計測100回、並列度1、nearest-rank。
  クライアント呼出し直前から型付き完全応答までを測定し、失敗や遅い標本の除外なし。
  モデルロード済み・索引ready後の定常測定。OS / DB / GPUキャッシュの消去なし。
- 実行期間は付属JSONの `performance.started_at` / `finished_at` にUTCで記録。
  受入計測中の追加観測は少数回の読取DB照会とコンテナhash確認だけ。診断・品質ゲートは受入計測後に実行。

```sh
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-after-r5-20261024
# manifest.jsonを実行前に固定し、同一モデルを起動済みにする
docker compose -p memory-core-perf-r5-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml build
docker compose -p memory-core-perf-r5-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml up -d --no-build --wait memory
docker compose -p memory-core-perf-r5-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 別診断：内訳と完全検索

受入計測後、同じ1000件DB・モデルを使う別HTTP APIへ一時診断コードを載せ、warm-up 2回＋10回を採取した。
受入アプリの背景workerはidle、診断APIはlifespan off。診断コードはコミットしない。

10回の平均は embedding **3.795ms**、検索 **50.599ms**（意味近似 **23.241ms**、語彙 **17.242ms**）、
rerank **46.612ms**、provision保存 **46.788ms**、HTTP全体 **168.553ms**。
接続1回、SQL execute 23回、commit 6回、tokenizer 7回。前回診断の検索49.006msに対し+1.594ms。
区間には重なりがあり加算できない。候補区間はcursorの実行・消費・材料読込・適格性判定を含み、SQL単独時間ではない。
別プロセス・別時点の診断平均であり、受入p50 / p95そのものの分解ではない。

意味の完全検索への切り替えは **0回 / 12想起**（warm-up 2回、本計測10回とも0）。
従ってその区間の計測時間は0ms（未進入）で、完全検索経路が高速だったことを示さない。
語彙検索は各想起1回。完全検索からLIMITを外した経路を、この1000件診断では通っていない。
受入runner内部に計時を足していないため、受入210想起の切り替え回数は未計測。
100件の別診断・完全検索を強制する追加条件の診断は **NOT RUN**。

## 検証・cleanup・未実施

ruff check / ruff format --check（298 files）、mypy（296 files）、UT 1096件、IT1 935件、
実PostgreSQL 624件（819.13秒）、lock / shell / Compose検査、wheel build・独立locked install / importは **PASS**。
今回起動したST / 品質projectとモデル・診断containerだけを停止・削除し、
container / network / volume残存0を確認した（**PASS**）。既存livekit containerの稼働継続も確認。
初回の残存照会は、削除前の別名診断containerを検出してFAIL。診断container削除後に再照会し、残存0（PASS）を確認した。初回結果も保持した。
詳細結果は付属JSON、ローカルの生証跡は `/tmp/core-st-performance-after-r5-20261024` に保持。
初回のローカルfrozen syncはsandboxのDNS制約でFAIL、許可付き再試行でPASS。
追加品質DBは旧image IDと受入時image IDのmetadataが利用できず、起動前の2試行がFAIL。
共有DBタグの参照先変化を確認し、利用可能なローカルimageを専用タグへ固定して再試行した。
追加品質DBのimage IDはJSONに別記し、受入DBのimage IDとは混同しない。
受入・診断containerの実image IDはそれぞれ事前manifestと一致していた。
受入試験の失敗・timeout・欠測はない。mergeは実施しない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、server設定比較、機能ST再実行、
100件の別診断、完全検索を強制する診断、受入想起内訳の直接計測。
この短い合成入力・データ量を超える性能は保証しない。
