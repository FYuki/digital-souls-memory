# 長い原文のチャンク索引・品質比較と入力段階別ST（2026-10-29）

## 条件・方法

- 本人依頼により採用実モデル・GPU・専用合成PostgreSQLを実行。AGENTS.md、CONTRIBUTING.md、docs/core-st.md、ADR 0021 / 0017の追補を確認した。旧ナレッジのCore記録は別リポジトリの履歴であり、今回の正本・権限として使わない。
- 起点は取得済み `origin/feat/recall-chunked-index-v2` の `975faf7`（PR #126、Issue #122）。隔離worktree `/tmp/recall-chunk-eval`、ブランチ `test/phase1-recall-chunk-eval`。メインcheckoutとsrcは無変更。push・PR作成・mergeなし。
- チャンク長512 Unicode文字、重なり64文字。semantic=0.55 / lexical=0.25 / recency・importance・self_relevance・trust各0.05、raw relevance閾値-2、候補200 / rerank20 / memory最大5 / 最終JSON 1500tokenを固定。チューニングなし。
- bge-m3 Q8_0 / CLS / 1024次元: SHA-256 `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`、revision `9eba04c5d75ba5a1595e45de734d36bef4e5cb98`。bge-reranker-v2-m3 Q8_0 / rank: SHA-256 `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`、revision `3093af03b1a635e67b084b1d8c03c5f5e020fd05`。実GGUF hashを照合。追加ダウンロード・推論API課金なし。
- llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`。GPU layers99、ctx/batch/ubatch2048、parallel1、threads/threads-batch8、cache-ram0。loopback 18121 / 18122、読み取り専用モデルmount。
- RTX 4070 Ti SUPER 16376MiB、driver 610.88、Ubuntu WSL2。Python3.12.3 / uv0.8.22 / frozen lockの専用Dockerイメージ。DBはPostgreSQL18 + vector0.8.2 + pg_bigm。GPU競合の確認結果は後片付け欄に記載。

## 品質比較 memory-v2.2

既存memory-v2.1のtuning82件・acceptance55件へ、各splitに局所事実2件・指示語の文脈依存2件を追加。計86件 / 59件、各独立3回。両splitのbyte hashを開始前に固定し、tuning結果のbyte hashをacceptance開始前に固定した。採用値1設定のみを確認し、tuning未達を見ても再調整していない。memory-v2.1の結果・入力は変更しない。

[固定ケース・再現手順](../../evals/retrieval/memory-v2.2/README.md)、[集計JSON](2026-10-29-recall-chunk-eval-summary.json)。比較アダプタは既存の実PostgresSearch / embedding / reranker / tokenizerと採点器を再利用し、製品のsplit_chunksで索引本文を分割する。各チャンクを検索・rerankし、元IDへ重複排除する。原文spanは元Turnから取り、選択チャンクを窓の検索中心とする。既存validator/採点器の互換性用protocol識別子はmemory-v2.1のまま、新版は外側manifestで識別する。

この比較器のJSONは旧比較用のmemory_id + source_spansであり、製品HTTPのmemory.content等を含むJSONとは異なる。窓の予算配分も旧比較方式。したがって比較器PASSだけで製品の返却有用性・HTTP全体の品質をPASSにしない。

| split | 安全（各回） | 最低想起（各回） | 最低該当なし（各回） | 判定 |
| --- | --- | --- | --- | --- |
| tuning固定値確認 | 100% / 100% / 100% | 100% / 100% / 100% | 66.7% / 66.7% / 66.7% | FAIL |
| acceptance | 100% / 100% / 100% | 90% / 90% / 90% | 100% / 100% / 100% | PASS |

acceptanceのparaphrase / cross_languageは各9/10、追加したchunk_local_fact / chunk_contextは各2/2、他想起分類は全件合格。分類別precision・誤取得件数と候補スコアレンジは集計JSONに保持した。該当なしは全件空。tuningのthreshold分類の該当なしは4/6で70%未達（他の分類・全ケースの詳細はJSON）。tuning FAILは保持し、acceptanceに戻して閾値・goldを調整していない。

## 段階別p50 / p95・判定

[2026-10-28ベースライン](2026-10-28-recall-input-tiers-baseline.md)と同じ各段階のinput_text全文・保存本文を使用し、両方を100 / 256 / 1000 Unicode文字に固定。100件→1000件、各100試行+warm-up5、並列1、warm、段階ごとに空tmpfs DBを作った。`tests.st.performance`によるHTTPクライアントの呼出し直前〜型付き応答取得を計測し、失敗・欠測を除外しない。全600本計測と30 warm-upをJSONに保持。nearest-rankの50番目・95番目を独立再計算した。

既存性能runnerを`tests.st.chunk_performance`から呼び、計測完了後にだけ全提供記録を保存する。測定中の定期SQL監査は行わない（旧ベースラインには約10秒ごとの読取監査負荷が含まれる）。この差と共有ホストの負荷を含む一回ずつの比較であり、チャンク化だけの厳密な速度差と断定しない。通常・削除ロールは分離し、管理接続は初期化・索引完了確認・計測後監査に限定する。実イメージIDは各ビルド後・開始前にmanifestへ固定し、JSONに記録した。

実測期間（UTC）: 100文字 2026-10-29T01:06:38.707004+00:00〜2026-10-29T01:10:03.250645+00:00、256文字 2026-10-29T01:10:16.498358+00:00〜2026-10-29T01:13:55.783177+00:00、1000文字 2026-10-29T01:01:48.997772+00:00〜2026-10-29T01:06:25.647721+00:00。

単位ms。音声100文字・テキスト256文字は必達p95 ≤600ms、長文1000文字は参考で合否なし。目標はp50 ≤150ms / p95 ≤300ms。

| 文字数 | 原文件数 | p50 | p95 | 旧p50 / p95 | p95差 | ADR判定 | 目標p50 / p95 |
| ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| 100 | 100 | 247.0 | 283.9 | 247.9 / 272.2 | +11.7 | PASS | 未達 / 達成 |
| 100 | 1000 | 335.0 | 370.5 | 315.2 / 352.7 | +17.8 | PASS | 未達 / 未達 |
| 256 | 100 | 325.7 | 351.2 | 312.8 / 339.7 | +11.5 | PASS | 未達 / 未達 |
| 256 | 1000 | 424.2 | 450.9 | 413.6 / 444.0 | +7.0 | PASS | 未達 / 未達 |
| 1000 | 100 | 555.9 | 595.8 | 673.3 / 745.4 | -149.6 | 参考（合否なし） | 未達 / 未達 |
| 1000 | 1000 | 718.9 | 773.2 | 756.5 / 791.9 | -18.7 | 参考（合否なし） | 未達 / 未達 |

1000文字・1000件で共通runnerの600ms判定はFAIL（shell終了1）となったが、ADR上は参考値であり性能の合否は付けない。計測成功をPASSへ読み替えず、共通runner値も生JSONに保持する。

## 1000文字の返却と原因切り分け

**両件数条件とも、100本計測 + 5 warm-upの全応答でmemoryが1件返った（計210/210）**。旧ベースラインの部分監査203件は全て0件だったため、今回の固定合成条件では返却0件を再現しない。全提供記録で採用候補のraw relevanceは `4.151885986328125`、閾値-2を上回る。返却spanは512文字、`truncated=true`、元区間は0〜1000で、原文の逐語部分文字列だった。

1000件の原文からreadyな索引行3000件・embedding3000件を確認した。短文の1行/原文に対して3チャンク/原文となる。チャンクrerankと逐語窓により長文の返却を確認できたが、旧ベースラインが0件だった直接原因は当時独立検証されておらず、今回の結果から応答予算だけが原因だったと断定しない。

返却予算・閾値を変えずに1件返っているため、今回「返らない場合」の原因切り分け条件には該当しない。比較器の追加ケースは局所事実と指示語の包含を採点する一方、性能入力は同じ本文の反復であり、一般の長文会話の有用性を保証しない。

## 通常検証・制約

- TDD: 新規比較アダプタの区間保持・rerank後重複排除テストを追加し、未実装時の失敗→実装後2件PASSを確認した。
- lock、ruff check / format、mypy（341ファイル）、UT（1297件）、IT1（1143件）、wheel/sdist build、ハッシュ固定install・隔離import、shell構文、Compose設定: **PASS**。Starletteのanyio alias非推奨警告1件あり。合成PostgreSQL suite: **PASS（857件、20分16秒）**。skip / xfailなし。
- 予備試行は正式600本に混ぜていない。比較器の初回集計呼出しでTypeErrorが出たため修正後に独立再実行した。100文字初回は実行中の評価シェル更新で終了時に構文エラー（exit2）が出た。256文字初回を含め実イメージ情報の固定を整え、両段階を再測定した。予備試行の結果・終了状態はローカル証跡の`tier-100-preliminary` / `tier-256-preliminary`に保持する。
- 通常検証のコンテナ実行ではGit環境不足でIT1の6件がFAIL、独立installはオフラインキャッシュ不足でFAILとなった。ホストにPython3.12.3 / uv0.8.22の固定環境を作り、別キャッシュで再検証した。モデルの追加取得はなく、開発依存のみ取得した。これらの失敗をPASSへ読み替えていない。
- NOT RUN: cold、並列負荷、10,000件、一般入力分布、製品HTTP上での旧55ケース全体の移植採点、削除競合の追加実モデル試験、Windows側を含むGPU完全排他・電力費測定。今回の比較器PASSをこれらの成功に一般化しない。

## 後片付け・証跡

- 正式3段階と予備2段階の全Compose projectについて、`down --volumes --remove-orphans`後にcontainer / network / volumeを再確認し、全コマンド終了0・出力なしを確認。postgres suiteの使い捨てDBコンテナとUnix socketも削除済み。tmpfs内の合成一時データは破棄された。
- 品質比較の各run DBはDROP済み。専用管理DB・embedding・rerankerコンテナもログ保存後に削除し、残存0件を確認した。他作業のコンテナ・モデル本体は変更しない。GPU競合は観測していない。専用2モデル以外のGPU割当コンテナは観測範囲で0件だった。Windows側の完全排他は検証していない。
- 630応答すべての提供記録IDをtrial / warm-upと照合した。原文span不一致0、採用memoryの閾値違反・score欠落0。100文字は各応答4件、256文字は2件、1000文字は1件を返した。製品の予算適用は実行されているが、HTTP全応答JSONを外部で再tokenizeする独立検証はNOT RUN。
- llama.cppログのrelease行で非0の`truncated`は両serverとも0件（embedding 11616行、reranker 23610行）。これは原文窓の`truncated=true`とは別の入力切詰め検査。
- `/tmp/recall-chunk-evidence`とworktreeを保持する。正式全trial・warm-up・manifest、分類別ゲート、原文監査集計、raw byte hash、cleanup結果は[集計JSON](2026-10-29-recall-chunk-eval-summary.json)に保存。品質raw JSON、モデルログ、予備試行、build / cleanupログはローカル証跡に保持し、巨大な本文監査をcommitへ含めない。
- 再実行は固定manifestを各段階の証跡ディレクトリへ置き、`MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:18121 MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:18122 MEMORY_CORE_ST_EVIDENCE_DIR=/absolute/evidence bash tools/with-chunk-performance.sh performance`。品質比較は固定ケースREADMEの手順を使う。
