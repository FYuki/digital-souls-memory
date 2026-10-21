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

## 起動と資源

`MEMORY_DATABASE_URL` を設定し、独立して `uv run --no-sync alembic upgrade head` を実施します。
`MEMORY_EMBEDDING_ENDPOINT` と `MEMORY_RERANKER_ENDPOINT` はローカルモデルのloopback URLです。
tokenizerはembeddingと同じendpointを使います。モデルは上記の固定revision・digestで別途起動します。
HTTP adapterはredirectを追わず、環境proxyを使用しません。クラウド送信adapterは追加していません。
モデルやDBを未設定でもimport・起動・healthは使用できます。想起は503となります。
create_appでポートを注入できます。製品Retrieverとembeddingが揃うと索引workerを起動し、
fake Retriever注入時は暗黙に起動しません。appが作成したworker・HTTP clientはlifespan終了時に解放します。
