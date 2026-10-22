# Issue #14 実モデル性能 ST：必達 FAIL と原因調査

## 判定と範囲

**必達 FAIL**。revision `68c8edb9f95c6ce4451cfe576399249f967cb8cb`、実モデル・HTTP・合成データ100件、warm-up 5回を除く100回で、**p50 3,089.064ms / p95 3,350.914ms**。必達 p95 ≤600ms、目標 p95 ≤300ms / p50 ≤150ms のすべてに未達。100回すべて成功応答で、欠測・失敗・遅い標本を除外していない。warm-upも5/5成功。最小2,795.821ms、最大3,456.465ms。

**1000件は NOT RUN**。追加投入の準備中に runner を手動停止し、1000件のwarm-up・100回計測には到達していない。製品コードの変更・修正後の受入試験・mergeは行っていない。

[結果・全100標本・診断10標本・証跡hashのJSON](2026-10-22-core-st-performance-summary.json)を併記する。元STと原因診断は別実験で、診断10回を受入100回の代替にしない。

## 条件と引き継ぎ

- UTC開始 `2026-10-22T07:17:34.407968+00:00`。元Compose projectは `memory-core-perf-final-20261022`。停止後のcleanup完了は `07:41:00.708132+00:00`。
- Linux WSL2、Python 3.12.3、uv 0.8.22、Docker 29.6.0 / Compose v5.2.0。CPU i7-14700K（論理28）、GPU RTX 4070 Ti SUPER（16,376MiB）、driver 610.88。
- PostgreSQL 18.6 / vector 0.8.2 / pg_bigm 1.2、tmpfsの専用合成DB。PGroongaはイメージに同居するがこの経路では未使用。
- embedding: bge-m3 Q8_0、1024次元、cls pooling。reranker: bge-reranker-v2-m3 Q8_0。revision / GGUF digest / DB・アプリ・モデルserverのimage digestは付属JSONに固定。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。ctx / batch / ubatch=2048、parallel=1、GPU layers=99、threads / threads-batch=8、cache-ram=0。2モデルともGPUを使用。
- 入力と各メモリ本文は「日本語」（3文字）。candidate_limit=200、rerank_limit=20、result_limit=5、JSON全体token_limit=1500、relevance_threshold=-2、並列度1。nearest-rankで集計。
- モデルはロード済み、データ投入後の索引完了を待つwarm定常測定。OS・DB・GPUキャッシュの消去なし。cold-startの性能を主張しない。
- 元の `performance.json` は100件完了後の中間保存。`finished_at` がなく、全体のrunner終了137は1000件準備の中断による。100件の100標本を再確認し、保存済みの分位点・FAIL判定と一致した。完走した全体runとして扱わない。
- 前の試行ディレクトリ2つは今回の受入標本へ合算していない。

1000件準備の停止時点で、DB直接観測はturns=256、documents=128、ready=0、pending=128、ProvisionRecord=105。監視では `07:39:56Z` にturns=249、documents=124、ready=0。投入・抽出・索引の処理は継続していたが、完了していなかった。turns件数は目的のメモリ件数とは異なる。

引き継いだ `preparation-budget.json` には600秒予算と監視起点 `07:34:00.191579Z` がある。今回の手動停止はその600秒到達前であり、「予算超過による自動停止」とは記録しない。元の終了処理がAPI・DB・モデルを削除したため、診断用環境を再作成した。

## 段階別計測

同じ固定image・実モデル・実DBを使い、別の `memory-core-perf-diagnostic-20261022` 環境で製品の `POST /characters/{character_id}/recall` を実行。一時的なwrapperで `perf_counter`、呼出し回数、入力文字数を採取した。計測コードはリポジトリへ含めていない。

診断の準備はHTTPで100件の原文を登録し、EchoGatewayと同じ本文・subject・object・単一出典の合成メモリをPostgresMemoryStoreへ直接登録、実IndexWorker.processで索引を公開した。これは抽出・再構築の待ちを短縮するためで、元STの投入・抽出全体を再実行したものではない。HTTP診断中はAPI lifespanを無効にし、抽出・索引の背景workerを起動していない。全100件ready、pending=0を前後に確認した。

warm-up 2回＋診断10回はすべてHTTP 200。10回のHTTP平均2,671.798ms、service平均2,670.330ms。次表は**同じ10回の各段階の算術平均**であり、元STのp50/p95を分割した値ではない。

| 段階 | 回数/想起 | 平均 ms | 内容 |
| --- | ---: | ---: | --- |
| SessionIdentity選択 | 1 | 4.919 | session単位の選択・保存 |
| query embedding | 1 | 7.520 | 実 `/v1/embeddings` |
| 候補検索 | 1 | 84.703 | search_rowsのDB照合・100件の復元を含む |
| 候補resolve | 100 | 2,175.843 | **下の出典material取得を含む** |
| ↳ 出典material取得 | 100 | 2,172.496 | turn/session/actor/receiptの再取得 |
| rerank | 1 | 259.224 | **3文字×20文書を1 HTTPリクエスト** |
| tokenizer | 7 | 6.536 | `/tokenize` の合計 |
| anchor検索 | 5 | 27.128 | 今回は該当anchorなし |
| ProvisionRecord.save | 1 | 92.959 | 全100候補のsnapshot再検証、INSERT、commitまで |

初期session/character照合、組立て、細かな処理の残りを含めservice全体は2,670.330ms。親子の段階は重複するため、出典materialとcandidate.resolveを足さない。

| 横断的なDB計測（上表に内包） | 回数/想起 | 平均 ms |
| --- | ---: | ---: |
| psycopg.connect | **510** | **1,689.029** |
| Connection.execute | **1,318** | 454.626 |
| Connection.commit | **510** | 60.068 |

Connection.execute件数はnamed cursorのexecute/fetchや暗黙のトランザクション操作を含まないため、全SQL件数ではない。HTTP adapterの時間はネットワーク・JSON処理を含み、GPU kernelだけの時間ではない。

追加1回だけPostgreSQLの `log_min_duration_statement=0` を有効化してサーバー側も観測。parse / bind / execute / transactionを含むduration行4,632区間の合計306.379ms、最長3.966ms。ProvisionRecord INSERTはparse 0.061ms / bind 0.302ms / execute **0.353ms**。この別標本のログ有効化には測定負荷があり、上表の10回平均へ加算しない。`save`の約93msはINSERT単体の遅さではなく、再検証・多数の読出し・ロック・commit等を含む。

## 主因と除外できた仮説

1. **候補ごとの出典再取得と接続の増幅が主因。** [RecallService._material](../../src/digital_souls_memory/recall/use_cases.py)は各出典についてget_turn、get_session、is_character、list_receiptsを呼ぶ。未登録の合成user actorについて [IdentityCharacters](../../src/digital_souls_memory/adapters/identity_characters.py) がget_coreとget_temperamentの両方を呼ぶため、**1出典につき独立5接続、100出典で500接続**。残り10接続は初期照合・session選択・検索・anchor5回・save。依存出典の同一turnキャッシュはあるが、今回の100件は別turnなので解消しない。
2. [search_rows](../../src/digital_souls_memory/adapters/recall_search.py)側では同一接続上で既に原文・session・actor・receiptを読み権限を確認している。その後serviceで同じ出典を別接続で再度取得し、さらに [PostgresProvisionStore.save](../../src/digital_souls_memory/adapters/postgres_provision.py) で原文・document snapshotを再検証する。最終の原子性検証は必要だが、これらの取得・検証を1件ずつ繰り返す構造が遅い。
3. **rerankerの文書別HTTP逐次呼出しはない。** [HTTP adapter](../../src/digital_souls_memory/adapters/recall_models.py)はdocuments配列で20件を1回送る。今回の入力は全て3文字で、長文が主因でもない。モデルserver内部の実行形態と製品のHTTP回数は別。約259msは次の制約だが、約2.17秒の出典解決の方が大きい。
4. tokenizerは空応答1回、5件の追加判定、最終応答再確認の計7回。入力JSONは172 / 488 / 806 / 1,124 / 1,442 / 1,760 / 1,760文字。回数は増幅しているが合計約7msなので主因ではない。query embeddingも1回のみ。
5. 元STの監視標本は `07:28:54Z`〜`07:33:55Z` にdocuments=100 / ready=100を維持し、ProvisionRecord数が7→104へ増加した。harness自体にも全件ready待ちがある。**索引未完了のまま100回を測った根拠はない**。元workerのGPU/DB占有率は精密に分離していないが、背景workerなしの診断でも約2.67秒となり、競合はFAILの必要条件ではない。

元ST平均3,101.597msと別診断平均2,671.798msの差約430msは、背景workerの有無・fixture生成経路・環境変動・計測負荷が異なるため厳密には帰属できない。診断値を元100回の正確な内訳と偽らない。

## IT2との違い

[Issue #9 IT2レポート](2026-10-21-retrieval-it2-v21.md)と実acceptance artifactを照合した。bge-m3＋rerankerの実値はpg_bigm p95 **31.39〜38.46ms**、PGroonga **36.24〜37.33ms**。依頼の概数35〜44msと区別する。

- IT2の55ケースの候補数は、43ケースが2件、9ケースが1件、2ケースが3件、1ケースが5件。rerank対象数も同じ。製品STの100候補・20 rerankとは負荷が異なる。
- [評価runner](../../src/digital_souls_memory/evaluation/runner.py)はpassage embeddingとindex構築を計測区間の前で済ませ、query embedding→検索→rerank→token予算付き選択をrecall_msに含める。query embedding・rerankを丸ごと除外した速さではない。
- IT2は評価専用のmemories/turns JSONテーブルと再利用するDB接続を使う。事前のeligible判定はharness内で行い、製品のHTTP認証・SessionIdentity選択・各候補のDB出典再取得・ProvisionRecordの原子的snapshot再検証/保存を含まない。
- IT2の集計はlinear分位点、今回STはnearest-rank。分位点方式の差だけで約3秒との差を説明することはできない。IT2の品質PASSを製品STの性能PASSへ移さない。

## 修正案（親が別TDDタスクで扱う優先順）

1. **想起1回の候補materialを一括取得し、同じ読取snapshot内で共有する。** turn / session / actor登録有無 / receipt集約をまとめ、search_rowsとserviceの重複読出しを減らす。リクエスト内だけのsession・actor情報再利用も対象。500接続を生む現在の経路を先に解消する。知覚・削除・配信期限・権限の判定と最終commit前の再検証を保持し、並行変更で不正応答しないことをテストする。
2. **Provision保存のsources/documents照合も一括化する。** 対象は返却5件だけでなくsnapshotに含む全候補100件。ロック順序・recall-state gate・全snapshot一致・commit-before-successを維持してSQL往復を減らす。単に接続poolを入れるだけでは、診断の総時間から接続時間を引いても約981ms残るため、必達達成は保証できない。
3. **rerankの実モデル処理を別途最適化・再測定する。** HTTPは既に1回batchなので「batch化」で直ったとしない。20候補・判定品質を維持したserver設定や実行方式を検証する。入力長上限は長文に対する別の対策で、3文字の今回のFAILは改善しない。rerankを外す/候補を減らすなら品質・契約変更として別評価が必要。
4. **1000件準備の再構築・索引無効化を調べる。** 追加投入中はgenerated memoryの再構築に伴い索引が未準備になり、当時のDB観測ではturn INSERTとindex workerがrecall-state advisory lockを待っていた。MemoryWorkerの再構築回数・無効化範囲・投入方法を別タスクで検証し、準備と計測を分離する。全件ready待ちは既に存在するため、待機追加だけを100件FAILの修正にしない。
5. tokenizerの重複最終判定やHTTP往復の削減は低優先。JSON全体1500tokenの正確な確認を保持する。

これらは根拠に基づく提案であり、修正・性能改善・受入PASSは未確認。

## 検証・cleanup・未実施

- `UV_CACHE_DIR`を一時ディレクトリへ向け、`/tmp/uvenv/bin/uv run --no-sync ruff check src tests migrations`: **PASS**。
- 同じuvで `ruff format --check src tests migrations`: **PASS（264 files）**。最初はuv既定cacheがread-onlyで起動失敗し、一時cacheで再実行した。
- 製品コード・test実装・固定manifest templateの変更なし。この証跡追加でUT / IT1 / postgres suite / mypy / buildは **NOT RUN**。過去のsuite結果を今回の実行結果に数えない。
- 元projectは既存終了処理でcontainers / networks / volumes残存0、両モデル削除済み。診断project・API・両モデルも `07:48:01.999079Z` までに停止・削除。最終確認で **memory-core-perf-* のcontainers / networks / volumesすべて0件**。他projectのcontainerには触れていない。
- 生標本・ローカル計測コード・DB/serverログは一時証跡ディレクトリ `core-st-performance-20261022-final` に保持し、公開commitには集計・性能標本・hashのみを含める。秘密・個人データ・ユーザーhome絶対パス・モデルファイルを含めない。
- **NOT RUN**：1000件性能、修正後100回以上のST、cold-start、並列度>1、長文/実利用データ、元ST内の段階別精密profile。推論クラウドAPIを使わず、モデル追加取得なし。電力費は未計測。
