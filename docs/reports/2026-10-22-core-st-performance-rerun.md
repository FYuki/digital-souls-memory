# Phase 1 実モデル性能 ST 再測定（#43 / #44 統合後）

## 判定

**100件の必達 PASS**。`39352b9cb7aecb123822993f25c9671f1f00e4be`（epic/phase1-consistency、PR #44 統合済み）で、
HTTPクライアント計測100回の **p50 104.973ms / p95 140.719ms**。
必達 p95 ≤600ms、目標 p95 ≤300ms / p50 ≤150ms はすべて達成。
100/100成功、warm-upも5/5成功、失敗・timeout・欠測0。遅い標本の除外なし、400msでの打切りなし。
nearest-rankは昇順50番目と95番目。最小 97.443ms、平均 115.268ms、最大 149.455ms。

**1000件は NOT RUN**。追加投入・抽出・索引の準備が30分予算を超えたため、専用runnerを停止した。
1000件のwarm-up・本計測には到達していない。100件のPASSを1000件やrun全体の完走へ一般化しない。

[要約・固定manifest・全100標本・warm-up・診断標本・証跡hash](2026-10-22-core-st-performance-rerun-summary.json)。
製品コード・test実装・モデル・性能条件の変更なし。計測wrapperは一時ファイルで、コミットしていない。

## 前回との比較

| 指標 | [前回](2026-10-22-core-st-performance.md) | 今回100件 | 短縮率 |
|---|---:|---:|---:|
| p50 | 3,089.064ms | 104.973ms | 96.60% |
| p95 | 3,350.914ms | 140.719ms | 95.80% |
| 必達 p95 ≤600ms | FAIL | PASS | — |
| 目標 p95 ≤300ms / p50 ≤150ms | 両方未達 | 両方達成 | — |

前回revisionは `68c8edb9f95c6ce4451cfe576399249f967cb8cb`。今回も100候補を採点、20候補をrerank、5件を返却。
全105件のProvisionRecord（warm-upを含む）で件数を監査した。候補削減による高速化ではない。
両モデルのローカルGGUFをSHA-256で検証し、前回のrevision・digestと正本のモデル定義が一致した。
revision自体の外部再取得は行わず、前回artifact・正本の対応と実ファイルdigestを照合した。

## 固定条件と再現方法

- 正本は `docs/core-st.md`、`compose.core-st.yml` + `compose.core-st.performance.yml`、`tests/st/performance.py`。
  専用project `memory-core-perf-rerun-20261022`、新規tmpfs合成DBを使用。
- UTC開始 `2026-10-22T10:12:01.576739+00:00`。100件の保存済みcheckpoint検知は `2026-10-22T10:23:10.922056+00:00`。
  1000件準備開始はcheckpoint検知を起点とし、監視間隔2秒で1800秒を超えた時に停止。
- Linux WSL2、Python 3.12.3、uv 0.8.22、i7-14700K（論理28）、RTX 4070 Ti SUPER、16,376MiB、driver 610.88。
  Docker / Composeと実hardwareの詳細はmanifestに記録。
- embeddingはbge-m3 Q8_0、1024次元、cls pooling、revision `9eba04c5d75ba5a1595e45de734d36bef4e5cb98`、
  SHA-256 `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
- rerankerはbge-reranker-v2-m3 Q8_0、revision `3093af03b1a635e67b084b1d8c03c5f5e020fd05`、
  SHA-256 `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
- llama.cppは前回と同じimage `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7` と起動引数。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは `--embeddings --pooling cls`、rerankerは `--embeddings --pooling rank --reranking`。
  ホスト公開は127.0.0.1:18049 / 18050だけ。起動引数全体はJSONへ保存。
- DBはPostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2。前回DB image IDはローカルに存在せず、
  今回は `sha256:172dd70ff434d5a2d8e4c848a74ac0dca61c3ae6eed688ca843829be261d40e7` をローカルoverrideで固定した。
  前回 `sha256:8490caa743a009824333d154ca954e454ad2444731f074d380237cc19d64bdfa` とIDが異なる。DBのバージョン・拡張とDockerfileの構成は同じだが、
  byte単位で同一のDB imageによる比較ではない。アプリimageは修正後ソースからビルドした
  `sha256:510b0501c1c26cabfb3fceef3b6bec76a2dab80a82ad448654e10f5d523f9f70`。
- 入力・各メモリ本文は前回と同じ「日本語」（3文字）、HTTPでTurn投入・fake抽出、実embedding・索引化を待機。
  candidate_limit=200、rerank_limit=20、result_limit=5、token_limit=1500、relevance_threshold=-2、
  warm-up=5、本計測100回、並列度1。実reranker・実tokenizerを使用。
- モデルロード済み、全100件ready後のwarm定常測定。OS / DB / GPUキャッシュ消去なし。
  GPUクロック・ホスト負荷を固定しておらず、観測された短縮の全量をコード変更だけへ帰属しない。
- frozen manifestは実行前に保存。uv 0.8.22・frozen lockでアプリをビルドし、
  `docker compose -p memory-core-perf-rerun-20261022 -f compose.core-st.yml -f compose.core-st.performance.yml -f <local-db-image-override> up -d --wait memory`、
  同じComposeで `run --no-deps --rm runner` を実行。runnerは正本を変更せずそのまま使った。

## 段階別診断

別project `memory-core-perf-rerun-diag-20261022`、同じアプリimage・同じimage/起動設定の実モデルserver・同じDB構成で計測。
診断初期化失敗時のcleanupでモデルserverも終了し、最終診断前に同じ設定で再起動した。
受入測定のDB image IDが診断開始時にはローカルに存在せず、初期化は2回失敗した（想起計測前）。
最終診断DBは同じ固定Dockerfile・package lockでの再ビルドimage
`sha256:3c84c8a7dd1996b84faff720dc74942e63f6fa323a1a0ed8dbe834d98ed3c1c8` を使用。受入測定とはDB image IDが異なる。
初期化失敗ログ・cleanup記録もローカル証跡とhashに保持した。
前回診断と同様、HTTPで100原文を登録し、EchoGatewayと同等の単一出典メモリを直接登録して
実IndexWorkerで索引を公開した。抽出・再構築の準備時間を短縮する診断用fixtureであり、受入STと区別する。
API lifespanはoff、背景workerなし。前後に100件ready / pending=0を確認。
接続回数はASGIのHTTP境界から採取し、サービス呼出し前の接続確保と終了処理も含める。
service平均はRecallService.recallの区間であり、HTTP境界外側の接続確保時間を含まない。
warm-up2回＋診断10回はすべてHTTP 200、100候補・rerank20件・返却5件。
HTTP平均 **99.972ms**、service平均 **95.001ms**。
以下は同じ診断10回の算術平均で、受入100回のp50/p95の分割ではない。

| 段階 | 回数/想起 | 平均ms |
|---|---:|---:|
| SessionIdentity選択 | 1 | 1.140 |
| query embedding | 1 | 3.712 |
| 候補検索・material一括取得 | 1 | 20.611 |
| 候補resolve | 100 | 0.517 |
| 個別material取得 | 0 | 0.000 |
| rerank（20文書・HTTP 1回） | 1 | 45.511 |
| tokenizer | 7 | 6.466 |
| anchor検索 | 5 | 2.197 |
| ProvisionRecord.save | 1 | 11.451 |
| psycopg.connect（HTTP境界を含む） | 1 | 3.646 |
| Connection.execute（他段階に内包） | 21 | 12.996 |
| Connection.commit（他段階に内包） | 6 | 0.625 |

前回診断のpsycopg.connectは510回・1,689.029ms、候補resolveは2,175.843ms
（個別material取得2,172.496msを内包）だった。今回の回数・時間は上表のとおり。
`Connection.execute`はnamed cursorのexecute/fetchや暗黙操作を含む全SQL件数ではない。
接続・SQLはHTTP全体に含まれる横断計測であり、表の処理段階へ加算しない。
モデルadapter時間にはHTTP・JSON処理も含む。別fixture・背景worker条件の診断差を
受入100回の厳密な改善内訳にしない。DB image差や実行時のGPU状態による差も分離していない。

## 1000件の準備と未実施

追加準備の起点・停止日時・最終件数はJSONの `run_1000` を参照。
投入中にgenerated memoryの再構築と索引無効化が再発し、ready=0が継続した。
準備中のDB観測で、抽出側トランザクションと索引側のrecall-state advisory lock待ちを確認。
30分停止は想起の遅い標本を除外する操作ではなく、未開始の1000件測定の準備打切りである。
原本performance.jsonの全体statusは中間保存のFAIL、finished_atは未記録、runner終了は
137。100件の全100標本と5 warm-upは完了しており、全体runの完走とは区別する。

1000件以降は、準備の再構築頻度・索引無効化範囲・recall-state lock保持時間の調査を優先し、
投入・抽出・索引と想起の性能を分離して再測定する。これは残る準備課題であり、今回修正はしていない。
NOT RUN：1000件の想起性能、cold-start、並列度>1、長文・実利用入力、server設定比較、モデル品質の再評価。
今回の短い合成入力からそれらの性能・品質を保証しない。クラウド推論API・モデル追加取得なし、電力費未計測。

## 品質ゲートとcleanup

- ruff check / ruff format --check：PASS（268 files）。mypy：PASS（268 source files）。
- UT 984 PASS、IT1再実行910 PASS、PostgreSQL 504 PASS。
  最初のsandbox内IT1は進捗停止により中断（終了143）。再実行はsandbox外で終了0、26.71秒。
  中断実行をPASSへ置き換えず履歴として記録する。原因の詳細切分けは未実施。
- uv lock --check / frozen sync、shell構文、wheel build・独立環境へのhash付きlocked install/import：PASS。
  既存機能STの再実行はNOT RUN。今回の変更は証跡文書のみ。
- 自分が起動した上記2つの専用Compose projectと、2モデル・診断APIだけを停止・削除。
  各projectのcontainer / network / volume残存0、cleanup完了 `2026-10-22T10:55:14.624614+00:00`。
  他projectのcontainerには触れていない。cleanupの個別確認結果をJSONに保存。
- 生証跡・一時wrapper・ログはローカル `core-st-performance-rerun-20261022` に保持。
  コミット対象はこのレポート・要約JSON・既存レポートへのリンクのみ。秘密・ユーザーhome絶対パス・モデル本体を含めない。
