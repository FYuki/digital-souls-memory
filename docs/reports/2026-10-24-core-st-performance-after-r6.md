# Phase 1 実モデル性能 ST：R6 修正統合後

[要約JSON・全210標本](2026-10-24-core-st-performance-after-r6-summary.json)。
起点は `541d1516352f353e7b39e2b3603209c6cc40114a`（`epic/phase1-consistency`、#65 / PR #67統合済み）。
比較対象は [PR #66の測定](2026-10-24-core-st-performance-after-r5.md)（起点6176895）。

## 結果

標準runnerは100件・1000件とも **PASS**、終了コード **0** で完走した。
各水準warm-up 5/5、本計測100/100成功。失敗・timeout・欠測は0。全標本をJSONへ保存した。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100 | 147.606 | 165.108 | PASS | 達成 | 達成 |
| 1000 | 208.747 | 245.452 | PASS | 達成 | 未達 |

| 件数 | 前回 p50 / p95 ms | 差 p50 / p95 ms | 増加率 p50 / p95 |
|---|---:|---:|---:|
| 100 | 139.822 / 156.768 | +7.784 / +8.341 | +5.57% / +5.32% |
| 1000 | 221.223 / 242.906 | -12.476 / +2.546 | -5.64% / +1.05% |

1000件の一覧APIは **0.258358秒**（前回0.240513秒、+0.017845秒 / +7.42%）。
runner終了後、同じDB・標準MemoryClient・timeout 30秒で1回別途測定し、完全な型付き応答検証を含む。
runner自身の一覧事前確認も既定30秒以内に成功したが、呼出し単独の時間は未計測。

## 1000件の準備時間と比較

runnerやアプリへ計時処理を挿入せず、Dockerの時刻付き標準HTTPアクセスログの応答ログ出力境界で算出した。
これはヘッダー送信時点で、本文転送・型検証の完了時点ではない。
以下にはdriverの微小な処理と一覧事前確認も含む。抽出・索引は投入と並行するため、各処理の独立時間ではない。

| 区間 | 秒 |
|---|---:|
| 最初の100件：session作成応答 → 100件一覧事前確認応答 | 18.506 |
| 追加900件：100件目水準の最終想起応答 → 1000件一覧事前確認応答 | **165.245** |
| うち追加900件投入：最終想起応答 → 最終Turn投入応答 | 72.766 |
| うち投入後の残りの索引待ち・一覧事前確認 | 92.479 |
| 1000件までの累積準備（18.506 + 165.245、100件水準の想起を除く） | **183.751** |

追加900件の区間はUTC `2026-10-24T14:44:07.028023Z`〜`14:46:52.273337Z`。
準備は既定の索引待ち1800秒以内に終了し、最終1000/1000文書がreadyだった。

前回は準備単独の計時値がなく、保存されたアプリログにも時刻がないため、同一定義の直接比較は **NOT RUN**。
双方のJSONから「runner内部の総時間 − 全210想起標本時間」を算出すると、
前回 **169.920秒** → 今回 **183.868秒**（**+13.948秒 / +8.21%**）。
この代替指標は登録・両水準の準備・一覧事前確認・Python/JSON等の処理を含む。
今回のログ由来の累積準備183.751秒に近いが、前回の1000件準備時間そのものとは扱わない。

必達と目標の達否は前回と同じ。一方、一覧APIと準備の代替指標には増加を観測したため、
「準備・一覧が悪化していない」とは結論しない。単一の逐次測定で負荷・クロック・キャッシュ等を固定した反復比較ではなく、
差をprojection_neighborsの閉包変更だけに帰属させない。
候補数 / rerank数 / 返却数は100件で100 / 20 / 5、1000件で300 / 20 / 5（各105回）で前回と一致。
候補の同一性やモデル品質は再評価していない。

## 条件・runner無変更

- 隔離worktree `/tmp/wt-perf-r6`、branch `test/phase1-performance-st-after-r6`。main checkoutは変更していない。
  変更は本報告・要約JSON・既存STレポートへのリンクだけ。製品コード・runner・support・tools・Composeの一時変更なし。
  mount置換・monkeypatch・wrapperによるrunner差し替えなし。ローカルoverrideも使用していない。
  外側のshellは終了コードと開始・終了日時を記録するだけ。
- 正本：`docs/core-st.md`、`compose.core-st.yml`、`compose.core-st.performance.yml`、
  `tests/st/performance.py`、manifest template。manifestは実行前に固定した。
- runner SHA-256 `4f8fefa42bda914ffa286dea9d984f0262d9897602fef103b3310ab695545423` は前回と一致。
  support / latency / DB初期化ツールもホストとコンテナで一致。hashはJSON参照。
  索引待ち1800秒、一覧事前確認・想起timeout 30秒を維持。
- 専用project `memory-core-perf-r6-20261024`、新規tmpfs合成DB、migration 0012。
  DB / Memory / モデルはloopbackのみ（55439 / 18039 / 18049 / 18050）。
- Ubuntu / WSL2、Intel Core i7-14700K（28 logical CPUs）、RTX 4070 Ti SUPER 16376 MiB、driver 610.88。
  Python 3.12.3 / uv 0.8.22 / frozen lock。モデルは既存の採用済みローカルファイル。
- embedding bge-m3 Q8_0 / CLS / 1024次元の実ファイルSHA-256：
  `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
  reranker bge-reranker-v2-m3 Q8_0：
  `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。前回・採用定義と一致。
- llama.cpp imageは前回と同じ `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは `--embeddings --pooling cls`、rerankerは `--embeddings --pooling rank --reranking`。
- DBとclientは起点ソースからbuild。image IDはmanifest・実コンテナ照合結果をJSONに記録し、前回と異なる。
  DB実版は前回同様 PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2。
- 入力は「日本語」（3文字）の合成データのみ。抽出はfake、embedding・検索・reranker・tokenizerは実構成。
  candidate_limit=200（各経路）、rerank_limit=20、result_limit=5、token_limit=1500、閾値=-2。
- 各水準warm-up 5回を別記、本計測100回、並列度1、nearest-rank。
  クライアント呼出し直前から型付き完全応答まで。遅い標本・失敗の除外なし。
  モデルロード済み・索引ready後の定常測定。OS / DB / GPUキャッシュの消去なし。
  計測中の追加観測は少数のログ読取・hash確認・DB件数照会のみ。品質試験は性能測定後に実行。

```sh
export PATH=/tmp/uvenv/bin:$PATH UV_CACHE_DIR=/tmp/uv-cache-perf-r6
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-after-r6-20261024
# manifest.jsonを固定し、同一設定のモデルを起動・health確認後
docker compose -p memory-core-perf-r6-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml build
docker compose -p memory-core-perf-r6-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml up -d --no-build --wait memory
docker compose -p memory-core-perf-r6-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

## 検証・cleanup・未実施

ruff check / ruff format --check（299 files）、mypy（297 source files）、UT 1102件、IT1 935件、
実PostgreSQL 625件（875.94秒）、lock / shell / Compose検査、wheel build・独立locked install / importは **PASS**。
追加品質試験用に受入DBのimageをタグ固定する初回操作はmetadata不在で **FAIL**。
前回の品質試験用ローカルimageへ専用タグを付けて再試行し、品質試験は終了0。
品質DBのimage ID・実版はJSONに別記。受入DBの実コンテナimage IDは事前manifestと一致しており、受入測定の失敗はない。

今回起動した受入ST・追加品質projectとモデルcontainerだけを停止・削除し、
container / network / volume残存0を確認した（**PASS**）。既存LiveKitは稼働継続を確認。
ローカル生証跡は `/tmp/core-st-performance-after-r6-20261024`、各証跡hashはJSON参照。
秘密・モデル本体・利用者ホームの絶対パスはコミットしない。

未実施：10000件、cold-start、並列度>1、長文・実データ、モデル品質再評価、設定比較、
機能ST再実行、SQL等の内訳診断、前回準備時間の同一定義での直接計測、反復による回帰判定。
短い合成入力・今回の件数を超える性能は保証しない。mergeは実施しない。
