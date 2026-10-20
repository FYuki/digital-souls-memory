# Issue #9 実モデル比較：ホスト実行記録

bge-m3 は品質 FAIL、multilingual-e5-large は長文で実行 ERROR。採用を承認する結果ではない。
既存の NOT RUN 記録を保存し、本書と別の新規証跡を追加した。評価コードのロジック変更はない。

実行ソース Memory `dd635d1e26787e8660507b6547319ee42e65912a`（開始前clean）、Core `b062a49a32d261d0ce472938c40711c6cb41d7c0`（clean）。
実行時刻（UTC、観測値）：2026-10-20T06:42:12.674854+00:00〜2026-10-20T07:16:16.210712+00:00。
Python 3.12.3 / uv 0.8.22、llama.cpp b11347（5fc4f3c8c）。
llama.cpp image `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`、DB image `sha256:8bdb522b0631253212b46f6e1226a3c1a9bd048972c9724b211d0768894eaf5d`。
同じ PostgreSQL 18 イメージで vector 0.8.2 / pg_bigm 1.2 / PGroonga 4.1.0 を毎run有効化。
GPU は RTX 4070 Ti SUPER / 16,376 MiB、driver 610.88。合成データ・数値loopbackのみ。
GGUFは既存ファイルのSHA-256を照合してread-onlyで利用し、再ダウンロードは不要だった。

## 固定条件

2モデル×2方式、tuning 82件×45設定×3回。tuningだけで44,280ケース・88,560実呼出し、実行エラー0。
全tuning設定は品質FAIL。独立acceptance 55件×3回は、bgeが完走、e5は各回10件完了後の長文呼出しで失敗。
semantic重み0.75・lexical重み0.05・他4項各0.05を選択。閾値はbge 0.75、e5 0.80。
入力・gold・固定グリッド・100%ゲートを変更せず、acceptanceで再調整していない。
モデルごとに直列起動し、ctx/batch/ubatch=2048、parallel=1、GPU layers=99、threads=8、cache-ram=0。
各runに新規ポートと一時DBを用意し、vector/結果cacheなし。DB/OS cacheは制御せず、cold cacheとは呼ばない。

| alias | 量子化・次元・pooling | query / passage 接頭辞 | revision / SHA-256 |
| --- | --- | --- | --- |
| bge-m3 | Q8_0 / 1024 / cls | なし / なし | `9eba04c5d75ba5a1595e45de734d36bef4e5cb98` / `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173` |
| multilingual-e5-large | Q8_0 / 1024 / mean | `query: ` / `passage: ` | `272a0975bd1a67886c0719767af84dfd2cbd9aa4` / `6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3be` |

## acceptance 品質とレイテンシ

p50/p95は各回の値の範囲であり、回を混ぜた分位点や平均ではない。recallは正解ありケースの平均、precisionは全ケース平均。

| 候補 | 状態 / 各回 | recall@5 / precision | p50 ms | p95 ms |
| --- | --- | --- | --- | --- |
| bge-m3-pg_bigm | 品質FAIL / 各26/55 | 0.3636 / 0.2909 | 11.64–12.11 | 15.24–15.76 |
| bge-m3-pgroonga | 品質FAIL / 各26/55 | 0.3636 / 0.2909 | 11.51–12.03 | 14.97–16.13 |
| multilingual-e5-large-pg_bigm | 実行ERROR / 各10/55の後に中断 | 判定不能 | 未算出 | 未算出 |
| multilingual-e5-large-pgroonga | 実行ERROR / 各10/55の後に中断 | 判定不能 | 未算出 | 未算出 |

bgeは両方式・各回とも同義語5/10、言い換え3/10、日英横断0/10、該当なし10/10。
長文、複数出典、一部の失効/Fact出典必須、指定順位も未達。禁止IDの返却は0。
sources_valid=falseは必要原文を取得できない場合も含み、禁止情報返却と同一視しない。
bgeのp95は目標300ms・必達600ms以内だが、製品HTTPやProvisionRecord書込みを除くハーネス内の測定である。

## acceptance 資源（e5は部分実行）

CPUは各runの累積CPU時間差の範囲。RAMはDocker memory_stats.usageの採取最大、VRAMはGPU全体の採取範囲。
e5の値は10件/回までの途中採取で、完走したbgeとの同条件比較には使えない。

| 候補 | DB / model CPU ms | DB / model RAM MiB max | GPU全体 MiB | 索引byte中央値 / 作成ms中央値 |
| --- | --- | --- | --- | --- |
| bge-m3-pg_bigm | 227.88–239.59 / 1054.54–1643.37 | 160.8 / 2232.6 | 3245–3269 | 16384 / 0.347 |
| bge-m3-pgroonga | 427.83–506.55 / 980.87–1110.20 | 344.0 / 2231.9 | 3268–3273 | 35033088 / 2.058 |
| multilingual-e5-large-pg_bigm | 92.55–98.25 / 154.57–446.94 | 176.8 / 620.1 | 3445–3445 | 16384 / 0.326 |
| multilingual-e5-large-pgroonga | 138.73–142.51 / 150.55–176.15 | 207.0 / 620.3 | 3445–3451 | 35033088 / 2.109 |

索引サイズ・作成時間は小さなケースごとの値。PGroongaは索引Sources/Lexicon/列のdisk_usageを合算する。
標本間のpeak、実運用規模、近似vector索引、製品全体の性能は未受入。ハーネスCPU・RSS・投入時間等は集計JSONと元証跡に保存。

## e5 長文エラーと中断

固定長文は接頭辞込み898トークン。起動値2048に対し、llama.cppが学習時上限512へslotを制限した。
同じ固定GGUF・起動引数の別使い捨てコンテナで `/tokenize` と `/v1/embeddings` を確認し、
HTTP 400 `exceed_context_size_error`、n_prompt_tokens=898 / n_ctx=512 を再現した。
固定文の切り詰め・分割・モデル変更は実施していない。設定値だけでの解消には至らず、親へ戻す。
評価コードの不具合と断定せず、候補の実行制約として記録する。コード変更が必要ならTAKTへテスト付きで依頼する。

## 推奨案と親の判断待ち

採用は保留、ADR 0021はProposedのまま。e5は512トークン制約のため今回の採用候補から除外する。
候補JSONは既存テストとの整合・過去の測定再現用に保持するが、採用候補への復帰を意味しない。
bge-m3のMemory 26/55とCore 61/62（親レビュー提供値）の乖離について、評価ハーネス側の原因をTAKTで調査する。
lexical重み0.05では方式間の品質差が出なかったため、pg_bigm / PGroongaの採否も再評価後に決める。
26/55の品質未達を受入PASSへ変換しない。e5との品質優劣はacceptance未完了のため断定しない。
Coreケースのライセンス表示未特定と出典対応は既存provenanceのとおりで、承認済み扱いにしない。
製品schema・migrationは変更せず、Fへの採用モデル・方式の引き渡しは未確定。

## 証跡と再現

- [2026-10-20-retrieval-it2-summary.json](2026-10-20-retrieval-it2-summary.json)：分類別・各回品質、失敗ID、CPU/RAM/VRAM、索引、独立再検証と圧縮hash。
- [2026-10-20-retrieval-it2-host-environment.json](2026-10-20-retrieval-it2-host-environment.json)：commit・dirty、固定image、GPUロード前/後/解放後、実コマンド、後片付け。
- [2026-10-20-retrieval-it2-e5-long-input-diagnostic.json](2026-10-20-retrieval-it2-e5-long-input-diagnostic.json)：長文のHTTP 400再現。

生データはリポジトリ外に保存し、次の相対名と集計JSONの`archives`に記録したSHA-256・サイズで照合する。
各データの圧縮前JSONとgzipの両方を保存し、圧縮前後のhash・byte数を残した。公開リポジトリには生データを含めない。

- `eval-artifacts/2026-10-20/2026-10-20-retrieval-it2-bge-m3-tuning.json`（および`.gz`）：全270 run。
- `eval-artifacts/2026-10-20/2026-10-20-retrieval-it2-bge-m3-acceptance.json`（および`.gz`）：各方式3回。
- `eval-artifacts/2026-10-20/2026-10-20-retrieval-it2-multilingual-e5-large-tuning.json`（および`.gz`）：全270 run。
- `eval-artifacts/2026-10-20/2026-10-20-retrieval-it2-multilingual-e5-large-acceptance.json`（および`.gz`）：各方式3回の途中実観測。

再検証・再現時はgzipのSHA-256を照合して展開し、`uncompressed_sha256`・`uncompressed_bytes`も確認してから、
[評価手順](../retrieval-evaluation.md)のtuning-report引数へ渡す。
実行時のコマンドは環境証跡の各`command`に残す（当時のパス表記を保持）。再実行では`--tuning-report`と
`--output`を上記のリポジトリ外の保存先に置き換える。候補subsetは候補JSONからモデル別に抽出する。
各suite・gold・候補subset・manifestのhashは元reportと環境証跡で照合する。
生データ4本は移動前に`validate_public_report`で固定suite・調整済み候補から独立再検証してPASSだった。
今回の整理では保存前後のhashを照合し、実モデルは再実行していない。整合性検証PASSは実行完了・品質PASSを意味しない。
専用DB・モデル・診断コンテナを削除し、既存サービス・モデルファイルは変更していない。
DB診断snapshotで一時DB OIDへのFATALを6件観測した。原因は未確定。全tuningとbge acceptanceは実行エラー0で、e5失敗はHTTP 400として再現済み。
旧NOT RUN記録、fixtureと通常検証証跡は削除していない。ST・製品性能・e5全件acceptanceは未完了。
