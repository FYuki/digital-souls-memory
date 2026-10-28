# 想起の入力長段階別ベースライン（2026-10-28）

音声会話100文字、テキスト256文字は、100件・1000件の両条件で必達p95 ≤600msを満たした。目標p50 ≤150msは全条件で未達。1000文字は参考計測として扱い、合否を付けない。長文の監査済み提供記録は返却memoryが0件であり、有用な想起の品質達成を示す結果ではない。

## 条件・方法

- 本人依頼により実GPU・採用モデルを実行。変更前にAGENTS.md、CONTRIBUTING.md、docs/core-st.md、[ADR 0017追補](../decisions/0017-recall-latency-budget.md)、[入力長診断](2026-10-28-recall-input-length-diagnosis.md)を確認した。
- 最新取得origin/epic/phase1-consistency `032e3d0a7ca2f6a9a1ccebefb2fe70911e708973` を起点とする隔離worktree `/tmp/recall-input-tiers-baseline`、ブランチ `test/phase1-recall-input-tiers`。PR #120の高速化を含み、Issue #122のチャンク索引実装前。メインcheckoutは変更しない。push・PR作成・mergeは行わない。
- 製品・試験・Composeコードは無変更。既存 `tests/st/performance.py` と `tools/with-core-st.sh performance` を使用し、manifestのinput_text / input_lengthだけで長さを作った。計測前後の追跡済み492ファイルのSHA-256が一致した。
- 本計測期間: `2026-10-28T05:40:24.084973+00:00` 〜 `2026-10-28T05:52:23.513864+00:00`（UTC）。各段階を別の空tmpfs DBから開始し、診断報告と同じ100→1000件へ増やした。件数はreadyなrecall_documentsの実数。10,000件条件はNOT RUN。
- 各条件warm-up 5回、本計測100回、並列度1。計600本計測・30 warm-up。HTTPクライアントの呼出し直前から型付き応答取得までを測定し、全試行を保持した。nearest-rankの50番目 / 95番目を独立再計算して一致確認。server duration_msは判定に使わない。
- クエリと全保存本文を双方100 / 256 / 1000 Unicode文字に固定。架空の町、図書館、公園、買い物を扱う合成日本語を使用し、実在個人情報・秘密は含めない。100・256文字は同じ固定文のprefix、1000文字は同じ段落の反復を含む。全文を生データmanifestに保存した。各条件内は同一本文の高一致率データで、一般の会話分布ではない。
- Intel i7-14700K / 28論理CPU、RTX 4070 Ti SUPER / 16376 MiB、WSL2 Linux 6.18.54.1、driver 610.88。Python 3.12.3、uv 0.8.22、frozen lockを使う既存イメージ。実DBはPostgreSQL 18.6、vector 0.8.2、pg_bigm 1.2。
- bge-m3 / bge-reranker-v2-m3のGGUF SHA-256を実ファイルと照合。revision / digest・DB / client image ID・起動引数は生データに保存。llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`、Q8_0、pooling cls / rank、GPU layers=99、parallel=1、ctx / batch / ubatch=2048、threads / threads-batch=8、cache-ram=0。両プロセスのCUDA backend / libcuda / cuBLASのロードとGPU割当・使用量を確認した。
- 候補上限200、rerank20、返却memory最大5件、最終HTTP JSON 1500token、raw relevance閾値-2を維持。抽出等は既存ST用fake、embedding / pg_bigm検索 / reranker / tokenizerは実採用構成。
- 他のIT1コンテナの一時稼働を観測した。最初の確認ではGPU割当なしで、ホスト上で観測したllama-serverは専用2プロセスのみ。GPU競合は観測していない。共有ホストのCPU負荷やWindows側GPU利用を排他的に固定していない。

## 段階別p50 / p95・判定

単位ms。必達p95 ≤600ms、目標p50 ≤150ms / p95 ≤300ms。音声100文字の必達未達は統合不可、256文字の未達はFAILと改善Issueが必要というADR追補の判定を適用する。今回は両段階とも必達PASSのため、未達に伴う改善Issue作成条件には該当しない。

| 入力・本文文字数 | 件数 | p50 | p95 | 本試行 / エラー | warm-up / エラー | ADR必達判定 | 目標p50 | 目標p95 |
| ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |
| 100 | 100 | 247.9 | 272.2 | 100 / 0 | 5 / 0 | PASS | 未達 | 達成 |
| 100 | 1000 | 315.2 | 352.7 | 100 / 0 | 5 / 0 | PASS | 未達 | 未達 |
| 256 | 100 | 312.8 | 339.7 | 100 / 0 | 5 / 0 | PASS | 未達 | 未達 |
| 256 | 1000 | 413.6 | 444.0 | 100 / 0 | 5 / 0 | PASS | 未達 | 未達 |
| 1000 | 100 | 673.3 | 745.4 | 100 / 0 | 5 / 0 | 参考（合否なし） | 未達 | 未達 |
| 1000 | 1000 | 756.5 | 791.9 | 100 / 0 | 5 / 0 | 参考（合否なし） | 未達 | 未達 |

1000文字の共通runnerは長さによらず600msを判定するため、生データの `required_pass` / `status` とshell exit statusがFAILになる。これらは改変せず保持し、ADR上は「参考・合否なし」と解釈する。性能runnerの計測成功・エラー0を性能PASSへ読み替えない。

## 品質・安全チェック

- **PASS（実モデルIT2 2件）**: 既存 `tests/it2/test_recall_models.py` により1024次元、単位ノルム、決定性、日本語関連文の意味順序、rerankerの関連順序とraw relevance -2の両側を確認。256文字の100件計測後〜1000件への投入中に実行し、性能本計測とは重ねていない。これは固定した別の合成例によるモデル確認であり、各段階の検索品質スコアではない。
- **PASS（全試行のHTTP・型検証・索引件数）**: 各条件100本計測+5 warm-upの失敗・欠測なし。各100 / 1000件の実索引完了とlist_memories件数を既存runnerが検証した。
- **提供記録の部分監査**: 専用DBへ約10秒ごとに読み取り専用SQLを実行。下表は最後のsnapshotで、全210記録を網羅しない。保存本文・query長は各段階の指定文字数と一致し、観測範囲で原文span不一致、採用memoryの閾値違反は0。計測中の監査によるDB / CPU負荷を含み、監査なし対照はNOT RUN。

| 文字数 | 監査記録数 / 全記録 | 返却memory最大 | rerank件数範囲 | 原文span不一致 | 採用閾値違反 |
| ---: | --- | ---: | --- | ---: | ---: |
| 100 | 188 / 210 | 4 | 20〜20 | 0 | 0 |
| 256 | 186 / 210 | 2 | 20〜20 | 0 | 0 |
| 1000 | 203 / 210 | 0 | 20〜20 | 0 | 0 |

- 1000文字の監査範囲は全記録が返却memory0件。span整合・採用閾値の検査も空集合に対する成立を含むため、有用性のPASSとはしない。応答予算の影響が考えられるが、原因の独立検証はNOT RUN。
- モデルserverの全保存ログを走査し、`truncated` が非0のrelease行はembedding / rerankerとも0件。quality IT2の追加要求もログに含む。安全な合成入力とloopback endpoint、毎段階の専用DB / 通常・削除ロール分離を既存ST構成で使用した。
- **NOT RUN**: 各性能応答JSONの1500token上限の独立再計数、一般入力・多様な関連/無関連/否定文コーパスの品質評価、段階ごとの削除競合・知覚権限回帰、正式なDB結合IT2・機能ST suite。UT / IT1 / postgres suite・build等の全開発ゲートも今回の文書のみの成果では再実行していない。SKIPをPASSへ変換した結果はない。

## 後片付け・証跡

- 各段階の `with-core-st.sh` が `down --volumes --remove-orphans` を実行。終了後に各projectのcontainer / network / volume確認を再実行し、すべて終了0・出力なしを確認した。1000文字のshell終了1は共通性能判定に由来し、資源破棄の失敗ではない。
- 試験専用embedding / rerankerコンテナもログ保存後に削除し、残存なしを確認。tmpfs DBと合成一時データはコンテナ破棄により削除済み。既存コンテナ・モデル本体・キャッシュ・他作業の資源は保持した。
- 再現用証跡ディレクトリ `/tmp/recall-input-tiers-evidence` とworktreeは保持した。manifest、全trial / warm-up、環境、モデル設定、部分監査、cleanup確認、ローカル証跡SHA-256を [生データ](2026-10-28-recall-input-tiers-baseline-raw.json) に保存。巨大なserverログと一時実行スクリプトはcommit対象外。
- 本結果の保証範囲は今回の固定合成文・100 / 1000件・並列度1・warm条件のみ。cold、10,000件、大規模運用や多様な会話分布への保証は行わない。
