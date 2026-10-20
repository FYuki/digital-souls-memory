# 0021. 検索方式・埋め込みモデルの比較（判断未完了）

- 日付：2026-10-20
- 状態：Proposed

## 背景

Issue #9でpg_bigm・PGroonga、およびbge-m3・multilingual-e5系の比較を要求している。
本書は比較途中の記録であり、採用方式やモデルの承認を表さない。

## 確認した結果

同一の比較イメージ上の一時DBでvector・pg_bigm・PGroongaを有効化した。
両方式の実adapterで成分生成・日本語検索・保存Turnの原文回収・自己発話・ケース分離を確認し、
runnerから公開reportと独立再検証までを合成vectorで実行した。
DB所有権・例外時の接続closeと廃棄、製品migrationも通常postgres試験の対象である。
構築条件と再現コマンドは[評価手順](../retrieval-evaluation.md)を参照する。
今回の通常検証はUT 126件・IT1 57件・postgres 11件がPASS。
コマンド・終了値・固定ツールチェーン・対象ファイルhashは
[検証証跡](../reports/2026-10-20-retrieval-validation.json)に保存した。

fixtureによるrunner・report・CLI・HTTP境界の成功は、実モデルの品質証拠ではない。
実モデルによる候補別3回の比較はNOT RUN。GPU情報取得はOSに拒否され、
実行前に必要なGPU資源余裕を確認できていない。

## Fへ渡す条件

検索方式・採用モデル・次元は未選定。Fで本書を採用指示として利用しない。
bge-m3比較時に必要な条件はQ8_0・CLS・1024次元・接頭辞なし、
revision 9eba04c5d75ba5a1595e45de734d36bef4e5cb98、
SHA-256 aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173。
これは指示書の比較基準であり、今回の実モデル確認結果ではない。
e5比較条件はmultilingual-e5-large Q8_0・mean・1024次元、
query接頭辞`query: `・passage接頭辞`passage: `（末尾半角空白）、
revision 272a0975bd1a67886c0719767af84dfd2cbd9aa4、
SHA-256 6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3beに固定した。
実GGUF・稼働serverの確認結果やbgeとの優劣・採否理由は未取得である。

Memory用6項スコア、30日半減と減衰例外、完全同点のID順、45通りのtuning範囲・選定規則、
分類別・各回100%の品質ゲート、5件/UTF-8 1,500byte推定を実測前の比較条件に固定した。
これは製品仕様や採用方式の承認ではない。旧Coreの0.52や同等帯0.002は使わない。
151件の元ID・元ファイルhash照合、137件のMemory入力と14件の適用不能部分・代替テスト、
閾値/完全同点の15件の再構築を保存した。Coreコード/ケースのライセンス表示は未特定で、
配布判断は親へ残す。

F向けには、両拡張とvectorの同居が通常DB試験で成立する比較イメージと、
固定した2モデル×2方式の比較手順を渡す。実比較が済むまで方式・モデルを選定しない。
製品schema・vector列・索引・拡張の適用はFの1 revisionへ残す。

## 判断前に必要な証拠

実identity、GPU事前確認、固定tuningによる候補別parameters、4構成×3回の実acceptance、
実CPU/RAM/VRAM・時間・返却量と品質の証跡が必要。
実行中標本の計測は実装済みだが、ロード前/ロード後/モデル解放後のGPU観測も実行担当が保存する。
製品HTTP・ProvisionRecord書込みは未計測で、製品ターン想起全体の性能適合は断定しない。
品質FAIL・実行エラー・NOT RUNを区別し、未達候補の採用判断と最終承認を親へ残す。
本書の作成だけではC18の完了義務を満たしていない。
