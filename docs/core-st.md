# Core クライアント ST

リポジトリルートで `bash tools/with-core-st.sh functional` を実行する。
Docker Engine / Compose v2 以降が必要。通常 CI では ST / IT2 を実行しない。
UT / IT1 の socket 禁止は保持し、Compose runner だけ明示解除する。

この ST は合成データ専用。DB 名 memory_core_st、管理者 memory_st_admin、
通常接続 memory_st_app、削除接続 memory_st_delete を分離する。
synthetic-only / synthetic-st-credential は公開合成値であり、実 credential・秘密ではない。
新しい Compose project と tmpfs を毎回作り、管理接続で空 DB を確認して既存0009まで適用。
通常ロールには memory_deletion_executor を付けない。
管理接続の利用は初期化と読取監査だけで、会話・人格・声・Profile は HTTP で投入する。

DB は既存 docker/retrieval-postgres の固定イメージを流用した。
vector・pg_bigm のほか未使用 PGroonga を含む。製品検索は pg_bigm のまま。
評価用 Compose と製品 migration は変更しない。
Memory / runner は Python 3.12.3、uv 0.8.22 と frozen lock を使う。

機能モードは tests.st.app 専用 factory にだけ fake の人格生成・審査・抽出・
embedding・reranker・tokenizer・短期状態を注入する。
検索・索引・知覚・Receipt・抽出ゲート・提供 commit・削除は製品実装と実 PostgreSQL。
この測定から実モデル性能を主張しない。
専用 /_st/advance は同じプロセスの試験時計だけを進め、本番の既定10分を変えない。

証跡は既定 /tmp/memory-core-st-evidence.*、または絶対パスの
MEMORY_CORE_ST_EVIDENCE_DIR に保存する。
build/start/tests/compose/cleanup のログ、revision、開始日時、コマンド、終了 status を残す。
成功・失敗・INT/TERM で down --volumes を実行し、project の container/network/volume が
残っていないことを確認する。証跡ディレクトリは削除しない。
shellだけへSIGTERMを送った場合は、前景コマンドの終了後にtrapが処理される。
制御端末から foreground process group へ Ctrl-C を送った実 Compose 試験では、
status 130 と専用資源の破棄を確認した。正常・失敗・端末中断の回帰試験は
`uv run --no-sync pytest -m it1 -q tests/it1/test_core_st_cleanup.py` で実行できる
（Docker CLI だけを fake にし、実 shell と制御端末を使う）。
今回の実行ツールのPTY Ctrl-Cでは、別sessionの子プロセスもINT/TERM/HUP handlerの
記録を残さず停止し、runnerのtrapも完了しなかった。終了の具体的な内部方式は未確認。
親エージェント（Claude）の追加判断により、この実行ツールのPTY中断経路での自動破棄は
本Issueの受入対象外とする。通常終了・失敗・端末Ctrl-C（INT/TERM）の後片付けは
引き続き受入対象。PTY中断の資源残存は成功に変更せず、追加調査や外部回答を待たず
文書補完後のレビューへ進む。

外部ツールによる停止、SIGKILL・ホスト停止等で専用資源が残った場合は、証跡の
`result.txt` の `project=` を確認する。result.txtがなければ `start.log` 等の
`memory-core-st-...` から対象の専用project名を確認し、リポジトリルートで次を実行する。
project名を推測せず、確認したprojectだけを破棄する。証跡ディレクトリは削除しない。

```bash
st_project='<result.txtまたはstart.log等で確認した専用project名>'

docker compose -p "$st_project" -f compose.core-st.yml down -v --remove-orphans

docker ps -aq --filter "label=com.docker.compose.project=$st_project"
docker network ls -q --filter "label=com.docker.compose.project=$st_project"
docker volume ls -q --filter "label=com.docker.compose.project=$st_project"
```

破棄コマンドの成功後、3つの確認コマンドがそれぞれ成功し、すべて出力なしであれば
container・network・volumeの残存なしと確認できる。コマンド失敗を出力なしと同一視しない。

## 実モデル性能

今回の TAKT 環境では GPU・実モデル server を使えないため実行結果は NOT RUN。
親が採用済みモデルを起動してから次を実行する。
embedding と tokenizer は同じ loopback endpoint、reranker も loopback。
性能用 Memory / runner は Ubuntu の host network、DB 公開は 127.0.0.1:55439 のみ。
機能モードと同じ専用初期化・破棄を使い、既存 DB・既存 volume を使用しない。

1. `uv run --no-sync python -c 'from tests.st.performance import Manifest; import json; print(json.dumps(Manifest.model_json_schema(), indent=2))'` で schema を取得する。
2. `docs/reports/core-st-performance-manifest.template.json` の空欄を埋め、実行前に
   evidence ディレクトリの manifest.json として固定する。空欄の template は実行できない。
3. `MEMORY_EMBEDDING_ENDPOINT=http://127.0.0.1:<port> MEMORY_RERANKER_ENDPOINT=http://127.0.0.1:<port> MEMORY_CORE_ST_EVIDENCE_DIR=/absolute/evidence bash tools/with-core-st.sh performance`

manifest は hardware、environment、DB/image ID、client image ID、revision、期間、cold 条件を
空欄にできない。モデル値は src/digital_souls_memory/recall/settings.py の EMBEDDING_MODEL /
RERANKER_MODEL と完全一致させる。起動する server の revision / digest を親が照合し、
端点だけを別モデルへ付け替えない。モデル本体は repo に置かない。

初期条件例：data_counts=[100,1000,10000]、input_text="日本語の合成会話" とその文字数、
candidate_limit=200、rerank_limit=20、result_limit=5、token_limit=1500、
relevance_threshold=-2、repetitions=100、warm_up=5、concurrency=1、
dimensions=1024、aggregation="nearest-rank"。
各件数まで HTTP で合成 Turn を投入し、実索引完了を待つ。抽出は fake だが、
embedding・pg_bigm 検索・reranker・tokenizer は製品の採用構成を使う。

性能 runner は呼出し直前から完全な型付き応答を得るまでを計測する。
全試行と warm-up を別々に保存し、timeout・通信・HTTP・応答検証失敗を除外しない。
失敗、欠測、非有限値、p95 > 600ms は PASS にならない。
目標 p95 <= 300ms、p50 <= 150ms は別の達否。
server duration_ms は性能判定に使わない。
performance.json の全試行、p50/p95、判定、環境 manifest、日時、例外の型を証跡に残す。
データ件数・入力分布を超える大規模運用保証は行わない。
