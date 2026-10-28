# Phase 1 実モデル性能 ST：10000件水準

[要約JSON・全標本・固定manifest](2026-10-28-core-st-performance-10000-summary.json)。
起点 `a5e386c55c1c961feb22b3a44e4a9fe31ec55545`（最新 origin/epic/phase1-consistency = main、PR #110統合済み）。
手順は [R20証跡](2026-10-26-core-st-performance-after-r20.md)、manifest template、tests/st/performance.py に従った。
runner・製品コード・Composeは変更していない。合成データのみ。

## 想起性能

標準runner **PASS**、終了コード **0**。
期間 `2026-10-27T23:43:20.547002+00:00`〜`2026-10-28T00:33:29.799022+00:00`（UTC、JSTでは2026-10-28）。
各水準warm-up5回と本計測100回、並列度1。予定315呼出し、保存標本315。
nearest-rankの50番目・95番目を全標本から独立再照合した。標本の除外なし。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms | 本計測 失敗 / timeout / 欠測 |
|---|---:|---:|---|---|---|---|
| 100 | 146.312 | 164.235 | PASS | 達成 | 達成 | 0 / 0 / 0 |
| 1000 | 212.815 | 254.361 | PASS | 達成 | 未達 | 0 / 0 / 0 |
| 10000 | 279.009 | 317.392 | PASS | 未達 | 未達 | 0 / 0 / 0 |

warm-upの失敗・timeout・欠測はJSONの各runs.counts.warm_upに記録。
runner例外：`なし`。

## 一覧 API（10000件、5回）

runner終了後の同じ専用DBで、MemoryClient.list_memoriesを5回連続実行。
GET呼出し直前から完全な型付き応答の取得・検証完了まで。timeout=30秒、キャッシュ消去なし。
中央値 **3.289471秒**、最大 **3.453752秒**。
一覧計測 **PASS**、失敗0、timeout0、欠測0。

| 回 | 秒 | 応答件数 | 結果 | 例外 / 原因 |
|---|---:|---:|---|---|
| 1 | 3.289471 | 10000 | PASS | なし |
| 2 | 3.276151 | 10000 | PASS | なし |
| 3 | 3.261829 | 10000 | PASS | なし |
| 4 | 3.453752 | 10000 | PASS | なし |
| 5 | 3.362054 | 10000 | PASS | なし |

## 準備時間

R20と同じDocker UTCアクセスログの応答ヘッダー出力境界。
最初の区間はsession応答→100件一覧事前確認応答、その後は前水準の最終想起応答→次水準の一覧事前確認応答。
投入区間の終点は該当水準の最終Turn応答。残区間はその応答→一覧事前確認応答。
本文転送・型検証完了は含まない。投入と索引は並行し、索引単体時間へ分解しない。

| 区間 | 準備合計 秒 | 投入区間 秒 | 残りの索引待ち・一覧事前確認 秒 |
|---|---:|---:|---:|
| 100件まで（追加100件） | 14.437398 | 4.643018 | 9.79438 |
| 1000件まで（追加900件） | 115.105576 | 52.665856 | 62.43972 |
| 10000件まで（追加9000件） | 2815.790287 | 1168.354801 | 1647.435486 |

累積10000件準備（前水準の想起計測を除く）：**2945.333261秒**。
投入後の区間1647.435秒は既定索引待ち1800秒に近く、背景負荷増加時のtimeoutが懸念となる（同区間には事前一覧確認も含む）。累積準備は約49分5秒、追加9000件だけで約46分56秒を要した。
途中の監視はrecall_documentsの件数であり、Turn投入件数ではない。最終Turn応答はUTC 00:05:33.107098160、10000件一覧事前確認応答は00:33:00.542584190。
各区間のUTC境界はJSONのpreparation.segmentsに保存。最終ready監査：`perf-656f74fe3f114733b75f91a62d4d1a80|10000`。

## 固定条件と環境

- worktree `/tmp/wt-perf-10000`、branch `test/phase1-performance-st-10000`。
  専用project `memory-core-perf-10000-20261028`、新規tmpfs合成DB。モデルは既存ローカルモデルディレクトリからread-only mount。
- Ubuntu WSL2; Python 3.12.3; uv 0.8.22; dedicated tmpfs synthetic DB; migration 0014; loopback endpoints。
- Intel Core i7-14700K, 28 logical CPUs; NVIDIA RTX 4070 Ti SUPER 16376 MiB; driver 610.88。NVIDIA-SMIユーザー版610.57.01 / KMD610.88。
  共有ホストで背景負荷の完全隔離なし。開始時のGPU情報・他コンテナの記録はJSONに保存。
- DB image `sha256:23895403ab60144a0e739ccc45182f38152f10a34c92f60d1156fe3229e1914f`。
  Memory/runner image `sha256:92cbd6093ef5187bfe6778d5dc7e8fa097b56b22eb3d56f2627efe7a9dc1a628`。起点からbuild。
  PostgreSQL18.6 / vector0.8.2 / pg_bigm1.2 / migration0014。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` はR20と同じ。
  ctx/batch/ubatch=2048、parallel=1、GPU layers=99、threads/threads-batch=8、cache-ram=0。
  embedding pooling=cls / 1024次元、reranker pooling=rank / reranking。実引数はJSONに保存。
- 承認済みbge-m3 / bge-reranker-v2-m3、Q8_0。両GGUFのSHA-256を実ファイルで照合し承認値と一致。
  revision・digestは固定manifestを参照。モデル本体・秘密・実会話をコミットしない。
- 合成Turn、入力「日本語」3文字、data_counts=[100,1000,10000]。
  candidate_limit200 / rerank_limit20 / result_limit5 / token_limit1500 / 閾値-2。
  抽出fake、embedding/検索/reranker/tokenizerは実構成。
- モデルロード済み・索引ready・各水準warm-up後、OS/DB/GPUキャッシュ消去なし。
  timeoutは索引待ち1800秒、事前一覧30秒、想起30秒の既定値。
- runner等主要6ファイルの事前/事後SHA-256、image内主要4ファイルとの一致、manifestの事前/事後一致を確認。

## 実行コマンド・検証・後片付け

固定環境変数：
```sh
export MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18049
export MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18050
export MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-performance-10000-20261028
```

起動・収集は `python3 /tmp/run-perf-10000.py`。
同scriptは固定Composeのbuild、専用モデル起動、`up -d --no-build --wait memory`、manifest固定、次の標準runner、終了後一覧5回測定を順に実行した。
```sh
docker compose -p memory-core-perf-10000-20261028 -f compose.core-st.yml -f compose.core-st.performance.yml run --no-deps --rm runner
```

コンテナ内コマンドは `python -m tests.st.performance /evidence/manifest.json /evidence/performance.json`。
外側scriptはrunner/support/latency/初期化の差し替えやoverrideを行わない。
一覧計測はMemoryコンテナ内のPythonから公開MemoryClientを使い、各例外を捕捉して5回すべてを記録した。
外側収集・品質確認・cleanupのscript全文もJSON command_sourcesに収録（実credentialなし、合成用固定値のみ）。
起動等の終了コードはJSON commands.setup_exit_codes、標準runner終了コードはrunner.exit_codeに保存。

通常品質ゲートは性能計測終了後 `python3 /tmp/quality-perf-10000.py` で実施。

| 検証 | 結果 | 終了コード |
|---|---|---:|
| ruff_check | PASS | 0 |
| ruff_format_check | PASS | 0 |
| mypy | PASS | 0 |
| ut | PASS | 0 |
| it1 | PASS | 0 |
| postgres | PASS | 0 |
| lock | PASS | 0 |
| shell | PASS | 0 |
| compose | PASS | 0 |
| build | PASS | 0 |
| package | PASS | 0 |

UT1210件 / IT1 1139件 / 実PostgreSQL826件は全件PASS（markerのdeselectedは除外選択数、skipではない）。
品質試験用の別DB container・一時socketディレクトリも終了後の残存なしを確認した。

cleanup：**PASS**。
`python3 /tmp/cleanup-perf-10000.py` により今回の専用Compose projectを `down --volumes --remove-orphans`、今回のモデル2コンテナを削除。
対象projectのcontainer/network/volumeとモデルcontainerの残存ゼロを、成功した確認コマンドで照合。
生証跡 `/tmp/core-st-performance-10000-20261028`。個別SHA-256は要約JSONに保存。

## 解釈と制約

10000件のp95は317.392msで目標300msを超過し、p50も279.009msで目標150msに未達。ただし必達600msには全水準適合。全315想起標本に失敗・timeout・欠測なし。一覧APIも5回すべて成功した。
R20との100/1000件比較差分はJSON comparison_r20を参照。
同条件の別時刻の単一試行同士であり、背景負荷・イメージ再buildを含む環境差があるため、個別変更の因果効果は断定しない。
10000件の想起性能と一覧取得・準備時間は別の境界で評価する。
未実施：cold-start、並列度>1、長文/実データ、モデル品質再評価、設定比較、機能ST再実行。
製品コード修正・mergeなし。
