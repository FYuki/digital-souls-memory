# 0021. 検索方式・埋め込みモデルの比較（採用保留案）

- 日付：2026-10-20
- 状態：Proposed

## 背景と比較条件

Issue #9でpg_bigm / PGroongaとbge-m3 / multilingual-e5-largeを比較する。
本書は実測結果とAIの推奨案であり、親エージェントによる承認・採用決定ではない。
Memory用6項スコア、45設定の独立tuning、分類ごと・各回100%の品質ゲート、
最大5件・UTF-8 1,500byte推定を実測前に固定し、旧Coreの0.52や同等帯0.002を流用していない。
ケース・gold・選定規則は測定中に変更していない。

## 観測結果

同一の固定PostgreSQL比較イメージでvector・pg_bigm・PGroongaを各run有効化した。
最新epic 669c51eの証拠台帳を統合し、UT 182・IT1 192・postgres 76件がPASS。
lock / lint / format / strict types / build / locked wheel install・importもPASS。
通常検証は[ホスト検証証跡](../reports/2026-10-20-retrieval-host-validation.json)。
旧fixture PASSと旧NOT RUNは実モデル品質PASSへ読み替えない。

実モデルの全tuningは540 run・44,280ケース・88,560実呼出し、実行エラー0で完了したが、品質は全設定FAIL。
acceptanceは以下の結果だった。

| 組み合わせ | 各3回の結果 | p50 / p95 ms（回ごとの範囲） |
| --- | --- | --- |
| bge-m3 + pg_bigm | 品質FAIL、各26/55、実行エラー0 | 11.64–12.11 / 15.24–15.76 |
| bge-m3 + PGroonga | 品質FAIL、各26/55、実行エラー0 | 11.51–12.03 / 14.97–16.13 |
| multilingual-e5-large + pg_bigm | 各10/55の後に実行ERROR、品質判定不能 | 全件集計なし |
| multilingual-e5-large + PGroonga | 各10/55の後に実行ERROR、品質判定不能 | 全件集計なし |

bgeは同義語5/10・言い換え3/10・日英横断0/10で、長文や一部出典必須・指定順位も未達。
該当なし10/10、禁止ID返却0でも全体の品質FAILは変わらない。
ハーネス内p95は300ms/600ms以内だが、製品HTTP・ProvisionRecord書込みは未計測。

e5は長文898トークンに対し、llama.cppが指定ctx 2048を学習時上限512へ制限しHTTP 400を返した。
別の使い捨てコンテナで再現済み。切り詰め・分割・コード修正はせずIT2を中断した。
これは品質FAILと区別した候補の実行制約で、評価コードの不具合と断定しない。

GPU事前確認、GGUF・稼働alias・次元・pooling・接頭辞、llama.cpp build/image、
DB拡張・image、実コマンド、CPU/RAM/VRAM、索引・作成時間、hash・各回結果・制約は
[実測記録](../reports/2026-10-20-retrieval-it2-host.md)とその新規証跡に保存した。
専用コンテナは削除し、既存サービスは変更していない。

## 検討した案

- **multilingual-e5-large：今回の採用候補から除外**。文脈長上限は512トークンで、
  Memoryの長文Turn（接頭辞込み898トークン）をそのまま埋め込むとHTTP 400、n_ctx=512となった。
  分割で対応すると比較の前提が変わるため、今回の比較では分割を導入しない。
  bge-m3は長い入力を扱え、今回のacceptanceを全件実行できた。
  `evals/retrieval/candidates.json` は過去の測定再現用として4候補を保持する。
  e5を削除すると既存の4候補・接頭辞検証テストが壊れるためであり、採用候補への復帰を意味しない。
- **bge-m3 + pg_bigm / PGroonga：採用保留**。両方式で全件実行できたが、各26/55で品質未達。
  pg_bigmの索引中央値16 KiBはPGroongaの約33.4 MiBより小さく、索引作成時間・DB資源も小さかった。
  小さな合成ケースの資源差だけで全文検索方式の採否を決めない。

## 未決事項

ADR 0021は **Proposedのまま** とする。親レビューでは、Memoryのbge-m3 acceptance 26/55と
Coreの同モデル評価61/62の大きな乖離が指摘された（Coreの数値は親レビュー提供値で、今回再実行していない）。
評価ハーネス側の原因をTAKTで調べ、比較条件・スコア・入力・出典判定等を確認してから採用を判断する。
現時点で原因をハーネスの不具合と断定しない。

全文検索方式（pg_bigm / PGroonga）も採用保留とする。選ばれたlexical重みは0.05で、
今回の条件では方式間の品質差が出なかった。方式の採否は原因調査・再評価後に決める。
26/55の品質未達や資源差だけでbge-m3 + pg_bigmを採用しない。

## Fへの引き渡しと判断待ち

採用方式・モデル・次元は未確定。Fで本書を採用指示として使わない。
比較用イメージの同居条件は成立したが、製品schema・vector列・索引・拡張はFの1 revisionへ残す。
bge基準はQ8_0 / CLS / 1024次元 / 接頭辞なし、revision
`9eba04c5d75ba5a1595e45de734d36bef4e5cb98`、SHA-256
`aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173`。
e5比較条件はQ8_0 / mean / 1024次元、`query: ` / `passage: `（末尾空白）、revision
`272a0975bd1a67886c0719767af84dfd2cbd9aa4`、SHA-256
`6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3be`。
実測前の条件と実ファイルhash照合・稼働サーバー確認を区別し、証跡で結び付けた。

e5は今回の採用候補から除外し、bge-m3の品質乖離はTAKTで調査してから再評価・採用判断へ戻す。
変更が必要ならテスト付きTAKTへ回し、固定goldを実測結果に合わせて変更しない。
151元ID・137 Memory入力・14適用不能部分と代替テスト・15境界ケースの出典はprovenanceに保存済み。
Coreケースのライセンス表示は未特定で、配布判断は親へ残す。
ST・実運用規模・製品全体性能・e5全件acceptanceは未受入。
実測記録の作成だけでC18の比較採用・承認義務が完了したとは扱わない。
