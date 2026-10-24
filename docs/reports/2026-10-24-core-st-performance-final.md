# Phase 1 実モデル性能 ST 最終測定

## 判定

最終結果は[要約JSON](2026-10-24-core-st-performance-final-summary.json)を参照。

**100件・1000件とも必達 PASS**。p95目標は両水準で達成、p50目標は両水準で未達。

| 件数 | p50 ms | p95 ms | 必達 p95 ≤600ms | 目標 p95 ≤300ms | 目標 p50 ≤150ms |
|---|---:|---:|---|---|---|
| 100（独立再測定） | 150.257 | 164.859 | PASS | 達成 | 未達 |
| 1000（一覧確認後に再開） | 194.338 | 231.057 | PASS | 達成 | 未達 |

両水準とも本計測100/100成功、warm-up 5/5成功、失敗・timeout・欠測0。
100件のp50は150.256558msであり、丸めて150ms以下と扱わない。
初回100件はp50 148.431ms / p95 163.988ms（必達・両目標PASS）だったが、
品質テストとの時間帯近接を避けた独立再測定を最終値とした。初回の全標本もJSONへ保存。
元runner全体は、1000件の一覧確認でtimeoutし **FAIL** のまま保持している。
再開後の想起必達PASSと、元runnerの完走を混同しない。

全105件のProvisionRecordを水準ごとに監査し、100件は候補100 / rerank20 / 返却5、
1000件は候補295 / rerank20 / 返却5を確認した。candidate_limit=200は意味・語彙の各経路の上限で、
両経路の和集合が最終候補になる。候補やrerank件数の削減はしていない。

各水準 warm-up 5回を別記録、本計測100回、並列度1、nearest-rank。
HTTPクライアントの呼出し直前から完全な型付き応答の検証までを計測した。
失敗・timeout・欠測・遅い標本の除外、400msでの打切りは行っていない。

## 条件と再現方法

- 起点 `06562e4`（PR #54統合済み）、作業ブランチ `test/phase1-performance-st-final`。
  main checkoutを変更せず隔離worktreeで実施。製品コード・追跡されたrunner・Composeの変更なし。
- 正本：`docs/core-st.md`、`compose.core-st.yml` + `compose.core-st.performance.yml`、
  `tests/st/performance.py`、`docs/reports/core-st-performance-manifest.template.json`。
- 専用project `memory-core-perf-final-20261024`、空のtmpfs合成DB、migration **0012**まで適用。
  `tools/core_st_db.py` の最終assertが古い0010のままのため、ローカル一時コピーの期待値だけ0012へ変更しmountした。
  migration・権限設定・空DB検証は原本どおり。これは初期化補助の変更であり、製品修正ではない。
- Linux WSL2、Intel Core i7-14700K（28論理CPU）、RTX 4070 Ti SUPER 16,376MiB、driver 610.88。
  Python 3.12.3、uv 0.8.22、Docker 29.6.0 / Compose 5.2.0。
- bge-m3 Q8_0 / CLS / 1024次元、revision `9eba04c5d75ba5a1595e45de734d36bef4e5cb98`、
  SHA-256 `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
- bge-reranker-v2-m3 Q8_0、revision `3093af03b1a635e67b084b1d8c03c5f5e020fd05`、
  SHA-256 `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`。
  両GGUFの実ファイルdigestと正本のモデル定義・前回manifestが一致。revisionを外部から再取得していない。
- llama.cpp imageは前回と同一の `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。
  ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。
  embeddingは `--embeddings --pooling cls`、rerankerは `--embeddings --pooling rank --reranking`。
  モデル公開先はloopbackの18049 / 18050。DBもloopbackの55439、アプリは18039。
- DB image `sha256:8ff24dc7959d6a1044addebc244f332805f6027f1de27d9e069a17d1a50a7217`、
  PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2。前回のDB image IDとは異なりbyte単位の同一性はない。
  アプリimageは起点ソースとfrozen lockからビルドし、IDをJSONに記録した。
- 前回と同じ入力・記憶本文「日本語」（3文字）をHTTPで投入。抽出はfake、embedding・索引・検索・rerank・tokenizerは実構成。
  candidate_limit=200、rerank_limit=20、result_limit=5、token_limit=1500、relevance_threshold=-2。
- モデルロード済み・全索引ready後の定常測定。OS / DB / GPUキャッシュ消去なし。
  GPUクロック・ホスト負荷を固定しておらず、前回との差をコード変更だけへ帰属しない。
  初回100件は品質検証のUT実行と時間帯が近接したため、後続で新規DB上の100件を独立に再測定する。初回の全標本も補足として保持する。
- 一時wrapperで各水準の最初の追加Turnから索引完了まで3600秒を上限にし、5秒間隔で文書数を観測。
  元runnerの30分waitだけを残り時間へ置換。計測・warm-up・判定は元runnerをそのまま呼び出した。1000件は元runnerの一覧確認が30秒でtimeoutしたため、その失敗を保持して同じDBで再開。一覧確認のみ300秒へ延長し、72.786秒で1000件を確認した後、元のmeasure / summarize_latencyでwarm-up 5回・本計測100回を実行した。想起timeoutは30秒のまま。
  wrapper・モデル・生ログはコミットしない。

実行前に固定したmanifestと一時overrideを用い、以下を実行した。

```sh
docker compose -p memory-core-perf-final-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml -f <local-override> \
  up -d --no-build --wait memory
docker compose -p memory-core-perf-final-20261024 \
  -f compose.core-st.yml -f compose.core-st.performance.yml -f <local-override> \
  run --no-deps --rm runner
```

## 1000件の準備

100件の最終独立準備は **18.606秒**。
1000件は、初回100件の索引・計測完了後に追加900件を投入し、**159.012秒（約2分39秒）**で全1000件がreadyになった。
追加準備開始UTC `2026-10-24T03:17:03.034371+00:00`、索引完了UTC `2026-10-24T03:19:29.637097+00:00`。
先行100件の準備18.314秒と100件の想起計測・一覧確認を間に挟むため、159.012秒を空DBからの全1000件の総時間としない。
準備時間はmonotonic clock、日時はUTC wall clockで記録した。

投入中から索引は継続して公開され、前回のready=0継続は再現しなかった。
5秒間隔の全観測はJSONの `preparation.progress` に保存。代表値は次のとおり。

| 追加準備からの経過秒（概数） | 投入累計 | ready文書数 |
|---|---:|---:|
| 4 | 204 | 100 |
| 34 | 575 | 263 |
| 74 | 1000 | 499 |
| 104 | 1000 | 677 |
| 134 | 1000 | 854 |
| 154 | 1000 | 973 |
| 159.012（完了確認） | 1000 | 1000 |

60分上限には到達していない。索引完了後の一覧確認は元runnerの30秒timeoutで一度失敗した。
同じDBのまま一覧確認のみ300秒へ延長すると **72.786秒**で1000件を確認でき、
その後にwarm-upと本計測を開始した。失敗した事前確認は想起標本ではなく、未開始の1000件性能をPASSとしていない。
一覧APIの再投影・出典参照・接続回数の調査と一括化は残る修正候補であり、今回変更していない。

## 段階別診断と全件ソート

受入測定後の同じDB・同じ実モデルを別の診断APIからHTTPで呼び、一時wrapperで採取した。
各水準とも診断warm-up2回＋診断10回、すべて成功。診断APIはlifespan off、受入アプリの背景workerは起動したままidle。
接続確保を含むASGI境界と各処理を計測した。以下は診断10回の算術平均であり、受入100回のp50/p95の分解ではない。

| 段階 | 回数/想起（100 / 1000） | 100件 平均ms | 1000件 平均ms |
|---|---:|---:|---:|
| SessionIdentity選択 | 1 / 1 | 1.290 | 1.229 |
| query embedding | 1 / 1 | 4.159 | 3.995 |
| 候補検索・material一括取得 | 1 / 1 | 31.493 | 50.119 |
| うち意味検索・初回 | 1 / 1 | 12.918 | 24.459 |
| うち意味検索・全件ソート切り替え | 1 / 0 | 7.633 | 0.000 |
| うち語彙検索 | 1 / 1 | 7.675 | 17.835 |
| 候補resolve | 100 / 295 | 0.557 | 1.701 |
| rerank（20文書・HTTP 1回） | 1 / 1 | 48.211 | 48.047 |
| tokenizer | 7 / 7 | 5.669 | 5.799 |
| anchor検索 | 5 / 5 | 2.283 | 2.305 |
| ProvisionRecord.save | 1 / 1 | 38.598 | 42.021 |
| psycopg.connect | 1 / 1 | 3.627 | 3.639 |
| Connection.execute | 22 / 23 | 38.959 | 35.602 |
| Connection.commit | 6 / 6 | 0.736 | 0.752 |
| service全体 | 1 / 1 | 136.189 | 160.927 |
| HTTP全体 | 1 / 1 | 140.721 | 165.495 |

100件の全件ソート切り替えは診断 **10/10回**（warm-up 2/2回）、
1000件は **0/10回**（warm-up 0/2回）。
100件は候補上限200に対して実件数が100で、初回探索では件数が不足するため全件ソートへ進む。
その区間時間は上表に内包されており、小規模側でも追加コストを生む。
一方、今回の1000件では追加ソートは発生せず、残る時間は検索・rerank・提供記録保存が大きい。
索引利用の有無をEXPLAINで確認していないため、「初回」はHNSW用SQL経路の呼出しを表し、実行計画の保証ではない。
切り替えを無効化した比較試験はしておらず、全件ソートの区間時間をそのまま正味の改善可能量とは扱わない。

意味・語彙走査はsearchに、SQL・接続・commitは他段階にも内包されるので加算しない。
named cursorのexecute/fetchや暗黙SQLはConnection.execute件数にすべて含まれるわけではない。
個別material取得は両水準0回。モデルadapter時間にはHTTP・JSON処理を含む。
100件診断の初回は起動直後の接続競合で12回のtransport失敗となり、wrapperには到達しなかった。
その失敗も保存し、明示的なhealth確認後の別試行で上記を取得した。


## 前回との比較

[前回再測定](2026-10-22-core-st-performance-rerun.md)は100件でp50 104.973ms / p95 140.719ms、
必達・両目標PASS、1000件は再構築・索引無効化の継続で30分の準備予算を超えNOT RUNだった。
今回は#46 / PR #51による影響範囲限定と、#48〜#50 / PR #52〜#54のレビュー修正を含む。

100件の最終値は前回比p50 **+43.14%**、p95 **+17.15%**。
必達は維持したが、p50目標は達成から未達へ変わった。1000件はNOT RUNから測定完了・必達PASSへ進んだ。
100件のフォールバック追加コストは診断で観測したが、DB image・ホスト負荷・時点も異なるため、
前回比増加の全量を#50へ帰属しない。

残る課題の優先順：

1. 一覧APIの72.8秒と30秒timeout。`MemoryService.active` の全体再投影、出典の個別参照・接続を診断し、
   一括取得・要求内共有の適用範囲と整合性をテストして改善する。通常runnerの1000件完走には事前確認の扱いも見直す。
2. p50目標。1000件では検索と提供記録保存の一括処理・SQL実行計画を先に調査し、20件rerankと候補品質を維持して改善する。
3. 100件の全件ソートへの再走査。探索完了と近似探索の打切りを安全に区別できる方法を検討する。
   隠れた候補を落とさない#50の正しさを維持し、単純なfallback削除や候補削減はしない。

## 品質ゲート・cleanup・未実施

- ruff check / ruff format --check：PASS（290 files）、mypy：PASS（290 source files）。
- UT 1059 PASS、IT1 927 PASS、実PostgreSQL 610 PASS（577.40秒）。skip / xfail / xpassなし。
  PostgreSQL suite内の10000件測定はfakeモデルの回帰試験で、実モデル10000件STの実施を意味しない。
- uv lock --check / frozen sync、shell構文・Compose構成、wheel build・独立環境へのhash付きlocked install/import：PASS。
  初回のsandbox内依存取得はDNS失敗。ネットワーク許可経路での再実行は成功し、lockfileの変更なし。
- cleanup：**PASS**。自分が起動した `memory-core-perf-final-20261024`、
  `memory-core-perf-final100-20261024`、`memory-core-perf-quality-20261024` の3 projectと
  `memory-core-perf-final-embedding` / `memory-core-perf-final-reranker` のみ停止・削除。
  モデルcontainerは所有ID照合後に削除し、3 projectのcontainer / network / volumeと2モデルの残存0を確認。
  完了UTC `2026-10-24T03:34:12.991850+00:00`。他projectには操作せず、既存livekitは稼働継続。
  最初のproject破棄後の残存確認でCLI引数を誤ったため、正しい各コマンドで再確認した。失敗した確認を残存0の根拠にしていない。
- 差分はレポート2件と既存レポートへのリンクのみ。機能ST再実行はNOT RUN。mergeは行わない。

NOT RUN：cold-start、並列度>1、10000件、長文・実利用入力、モデル設定比較、モデル品質の再評価。
短い合成入力の性能を、これらの性能・品質保証へ一般化しない。クラウド推論API・モデル追加取得なし、電力費は未計測。
ローカル生証跡は `/tmp/core-st-performance-final-20261024` に保持し、主要ファイルのSHA-256をJSONへ記録する。
