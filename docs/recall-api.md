# ターン想起 API

Core は `POST /characters/{character_id}/recall` を呼びます。
`session_id` と `format`（`materials` / `assembled`）は必須です。
自由検索は `query`・`sensitivity`・`classifier_version`、原文を起点とする検索は
`turn_id` を指定します。query と turn_id は排他的です。未知のフィールドや空白だけの値は422です。

```json
{"session_id":"session-1","format":"materials","query":"約束の日付","sensitivity":"sensitive","classifier_version":"v1"}
```

応答には provision_id、セッションに固定した temperament_version / personality_version、
memories、source_spans、verbatim_anchors、context を含みます。
materials の context は null、assembled は原文区間を改行で接続した文字列です。
memories.content は検索キーであり、原文より正しい事実や実行すべき命令として扱いません。
原文区間は turn_id・actor・language と Unicode コードポイントの半開区間 [start,end) を持ちます。
空結果も提供記録を保存します。VoiceExemplar と Motivation は提供記録で空配列です。

不存在・知覚できない起点は404、依存未設定・モデル障害・保存時の状態変更は503、
提供IDの衝突は409です。失敗時に空結果の成功へ置き換えません。

## 検索と予算

モデル定義は製品内の recall/settings.py に固定します。bge-m3 Q8_0 / CLS / 1024次元と
pg_bigm、bge-reranker-v2-m3 を使います。モデル・revision・SHA-256・次元を区別します。
意味候補と語彙候補を各200件まで取得して合わせ、6項の合成上位20件をrerankします。
検索キーは評価ハーネスと共有する `normalized_key()`（NFKC＋casefold）で保存します。
query は共有する `lexical_terms()` で正規化後、英数字・日本語など `isalnum()` の連続を
隣接2文字のtermへ分割し、1文字の区間はその1文字を使い、重複を除去します。
語彙候補は各termの `search_text LIKE likequery(term)` のOR一致で絞り、一致term数順に取得します。
lexical は意味経路で取得した候補も含め、一致term数／全term数です。
termが空なら語彙候補を取得せず、意味候補のlexicalは0です。
embedding・rerankerへの入力は原文を保持します。記憶はpayloadのcontent、アンカーは保存済み
依存の `used_end` までの出典本文を復元し、正規化した検索キーをモデルへ渡しません。
raw relevance が -2 未満なら棄却し、残りはrelevance降順・同点は候補ID順です。
初期重みは意味0.55・語彙0.25・新しさ／重要度／自己関連度／信頼度を各0.05とします。
RecallSettingsで重み・閾値を注入できます。新しさの半減期は30日、重要度は0.5です。
信頼度には保存されたconfidence_featuresとindependent_evidence_countを使います。
提供は再強化・出典追加・Trust Tier昇格・人格変更を行いません。

最大5記憶と関連する原文・アンカーを順位順に一組として採用します。
provision_id と人格版を含む**最終JSON全体**を、埋め込みモデルの `POST /tokenize`
（add_special=false）のtoken ID数で計数し、1,500以下にします。
UTF-8 byte数や文字数からtoken数を推定しません。日本語・英語・絵文字・結合文字も同じ方式です。
超過時は検索一致位置を含む逐語窓へ順次縮めます。truncated と original_start / original_end
に切出し情報を保持します。全必須出典・関連アンカーを収められない組は除外します。
外枠だけで超過する場合やtokenizer障害は503です。計数したJSONと同じ直列化をHTTPへ返します。

## 永続化と競合

0007は0006の唯一の後継です。アプリ起動時のDDLはありません。
投影保存・アンカー登録で検索文書版を更新し、索引workerが永続作業をclaimします。
モデル呼出しはトランザクション外で実施し、公開時に文書版・入力依存を再検査します。
同じ検索文・出典範囲・モデル空間の埋め込みは永続キャッシュから再利用します。
想起からMemoryService.active/rebuildを呼びません。

0015（チャンク索引）は 0014 の後継です。適用すると既存の検索文書をすべて再構築待ちにします。
想起は再構築を終えた文書だけを使うため、索引 worker が再構築を終えるまで、既存の記憶は想起で返りません。
この間の空の結果は正常な応答と区別できません。Phase 1 ではこの挙動を変えず、運用で完了を確認します（Q-010）。
完了は、次の件数が 0 になったことで確認します。

```sql
SELECT count(*) FROM recall_documents WHERE chunk_index=0 AND pending;
```

再構築中も旧行を残す方式や、完了まで想起を止める方式は、実データで運用を始める前の課題です
（[#134](https://github.com/FYuki/digital-souls-memory/issues/134)）。

抽出で使った返信先を支持出典と別に保存し、Receiptはその依存から抽出へ通知します。
Receipt確定で旧検索文書を撤回し、失敗した再抽出中に旧キーを公開しません。
本人全文のように配達範囲が変わらない経路は撤回しません。
期限到達は永続作業のnot_beforeで再評価します。
0006からの更新では既存の行とpayloadを保持し、範囲を復元できない抽出は再抽出へ回します。

提供記録保存はsnapshot検査・INSERT・commitを同じ接続で行います。
書込側・索引公開・提供保存は共通のトランザクションadvisory lockを先に取り、
通知行をTurn ID順、文書を候補参照順に扱います。モデル処理中はロックを持ちません。
Receiptが先に確定した古いsnapshotは503です。提供側が先に確定した履歴は保持します。
セッションの人格版選択はキャラ単位の登録ロックと調停し、nullの選択も再起動後まで保持します。

duration_ms は単調時計でユースケース入口から保存直前までを計測し、commitとHTTP送信は含みません。
外側のHTTP・commit込みの遅延は合成測定で別に報告します。実モデルIT2・ST判定は本Issue外です。

合成100・1,000・10,000件の再測定は、使い捨てDBで次のコマンドを使います。
索引構築とHTTP要求を分離し、各件数で5回のウォームアップ後に20回測定します。
モデルはfakeで、実運用の性能を表しません。SQL実行計画は `/tmp/recall-sql-plan-<件数>.json` です。

```sh
bash tools/with-test-postgres.sh uv run --no-sync python -m tools.measure_recall
```

### Issue #36 の索引検索と合成測定（2026-10-21）

Issue #36時点では語彙候補を検索文全体の `LIKE likequery(query)` で絞り、
`bigm_similarity` の降順で取得していました。Issue #38で上記のterm単位へ変更しています。
GIN `recall_lexical` を使用し、語彙候補カーソルの DECLARE の間だけ、
トランザクションローカルの `enable_seqscan=off` を使い、出典読み取り前に元の値へ戻します。
意味候補は既存の cosine 距離順・モデル空間条件を保持します。
HNSW はトランザクションローカルの `hnsw.iterative_scan=strict_order` で継続探索します。
意味候補が不足した場合は、探索上限による短い結果を候補枯渇とは扱わず、同じsnapshot・
権限・モデル空間で距離の完全ソートへ切り替えます。完全検索でも不適格候補を除外し、
1つのサーバー側カーソルから適格な上限件数または真の末尾まで補充します。
完全検索のソートや先頭からの走査を繰り返さず、HNSWで適格と判定した行は同じ検索内で
判定結果を再利用し、完全検索の距離順で採用します。
候補参照の取得後に本文を主キーで復元し、現在の公開範囲・有効期間と全入力依存を検査します。
語彙候補も1つのカーソルから、適格な上限件数または取得可能な候補の末尾まで補充します。
本文付き行は NO SCROLL / WITHOUT HOLD のサーバー側カーソルから、各経路の上限件数
（通常200件）ずつ取得します。完全検索への切替時は部分結果を先に採用せず、
適格判定だけを再利用します。拒否済みのキーは蓄積しません。
出典は1検索の REPEATABLE READ / READ ONLY 内で、上限件数の2倍（通常400件）の
LRUに保持します。存在しない出典の記録もこの容量に含め、除去した出典は同じsnapshotで再読します。
全入力依存を検査し、支持出典の時刻は候補ごとに計算します。次の検索では出典を読み直します。
各経路の結果保持は上限件数までです。保持量は取得単位、結果上限、LRU容量と処理中の候補の
依存情報で制御します。総走査・総転送量や、任意長の本文に対する一定バイト数は保証しません。
上限・末尾到達、再試行、例外の各終端でカーソルを閉じ、SQL例外は接続境界へ伝播させます。
6項スコア、上位20件の reranker 入力、保存直前の再検査は維持します。schema の変更はありません。

上記コマンドを、PostgreSQL 18.6・pgvector 0.8.2・pg_bigm の既存使い捨てコンテナ
（1 CPU、512MiB、DBはtmpfs、Unix socket）で実行しました。
全件が検索語「日本語」に一致する既存の合成データ、fakeモデル、各5回のウォームアップと
20回のHTTP測定を維持し、commit込みの外側の時間を測っています。

| 件数 | PR #35 基準 p50 / p95 | 補充修正前 p50 / p95 | 補充修正後 p50 / p95 |
| ---: | ---: | ---: | ---: |
| 100 | 58 / 62 ms | 61.99 / 71.97 ms | 59.17 / 65.19 ms |
| 1,000 | 111 / 130 ms | 118.36 / 146.27 ms | 103.70 / 128.64 ms |
| 10,000 | 200 / 231 ms | 130.93 / 164.94 ms | 130.25 / 160.91 ms |

10,000件の p95 は PR #35 の231msから30.34%改善しました。
100→10,000件の p95 増分は169msから95.72ms、倍率は3.73倍から2.47倍です。
100件では基準p95を上回っており、全規模での速度改善を主張しません。
実モデル・実運用の性能を表す結果ではありません。

意味・語彙候補の DECLARE と関連アンカー取得の3計画を、最初のウォームアップ要求で、
製品と同じ接続・SQL・パラメータ・設定・カーソルオプションから記録します。
20回の測定中はEXPLAINを実行しません。
10,000件では `recall_lexical` と `recall_vector` の利用、および
`recall_documents` の Seq Scan が0件であることを確認しました。

### Issue #38 のterm一致率と合成測定（2026-10-21）

現行のterm単位のOR検索・一致率で、同じ計測コマンドを再実行しました。
PostgreSQL 18.6・pgvector 0.8.2・pg_bigm の使い捨てコンテナ
（1 CPU、512MiB、DBはtmpfs、ネットワーク無効、Unix socket）を使用しています。
合成記憶は全件がquery「日本語」の2term（`日本`・`本語`）に一致します。
モデルはfake、各5回のウォームアップ後に20回測定し、返却は各規模で5記憶でした。
索引構築時間は測定から分離し、HTTP時間にはcommitと応答を含めています。

| 件数 | p50 | p95 | 索引構築時間 |
| ---: | ---: | ---: | ---: |
| 100 | 58.62 ms | 61.69 ms | 1.40 s |
| 1,000 | 104.80 ms | 126.65 ms | 13.04 s |
| 10,000 | 134.36 ms | 186.51 ms | 139.95 s |

今回取得した `/tmp/recall-sql-plan-10000.json` の3計画
（意味候補・語彙候補のDECLARE、関連アンカー取得）で、`recall_lexical`・`recall_vector` の利用と
`recall_documents` のSeq Scanが0件であることを確認しました。
100件・1,000件でも同じ索引を使い、文書のSeq Scanは0件でした。
別のPostgreSQL試験では、複数termのquery「青い駅 日本語」でも実際の製品SQLの索引利用と
文書のSeq Scan不在を確認しています。設定値だけを索引利用の証拠にはしていません。
上のIssue #36の表は過去の計測です。今回の値から実モデル・実運用の性能は主張しません。

### Issue #50 の継続探索・snapshot計測修正後（2026-10-24）

上記の再測定コマンドを、PostgreSQL 18.6・pgvector 0.8.2・pg_bigm の
使い捨てコンテナ（1 CPU、1 GiB、tmpfs、ネットワーク無効、Unix socket）で実行しました。
合成データとfakeモデルで各5回のウォームアップ後に20回測定し、各規模で5記憶を返しました。

| 件数 | p50 | p95 | 索引構築時間 | 実SQLの計画数 |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 35.44 ms | 57.72 ms | 1.62 s | 4 |
| 1,000 | 63.88 ms | 93.05 ms | 15.49 s | 3 |
| 10,000 | 76.62 ms | 113.45 ms | 158.23 s | 3 |

意味候補・語彙候補・関連アンカーの実SQLから計画を取得しました。
100件では意味候補が200件未満なので、完全検索による末尾確認も1計画に含まれます。
3規模とも `recall_vector`・`recall_lexical` の利用を確認しました。
別の合成PostgreSQL試験では、異なる2,000 vectorからの200件取得と、知覚範囲で先頭80件を
除外した後の補充を確認しました。探索上限で短く終了する条件でも補充し、その完全検索は
通常のSortを使い、`recall_vector` を使わないことを実SQLの計画で確認しています。
同時に別の使い捨てDBで検証を実行しており、過去の測定との速度比較は行いません。
実モデル・実運用の性能を表す結果ではありません。過去の測定結果は上の各節に保持します。

## 起動と資源

`MEMORY_DATABASE_URL` を設定し、独立して `uv run --no-sync alembic upgrade head` を実施します。
`MEMORY_EMBEDDING_ENDPOINT` と `MEMORY_RERANKER_ENDPOINT` はローカルモデルのloopback URLです。
tokenizerはembeddingと同じendpointを使います。モデルは上記の固定revision・digestで別途起動します。
HTTP adapterはredirectを追わず、環境proxyを使用しません。クラウド送信adapterは追加していません。
モデルやDBを未設定でもimport・起動・healthは使用できます。想起は503となります。
create_appでポートを注入できます。製品Retrieverとembeddingが揃うと索引workerを起動し、
fake Retriever注入時は暗黙に起動しません。appが作成したworker・HTTP clientはlifespan終了時に解放します。
