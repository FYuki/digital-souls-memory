# memory-v2：保存済みbge-m3 tuningの参考再採点

これは新しい実モデル測定ではない。保存済みmemory-v1 tuningの観測だけを
memory-v2の規則で再採点した。実モデル呼出しは0、acceptanceは未測定、
reranker・raw cosine・新しい遅延は未測定である。
検索方式・モデルの採用判断は行わず、ADR 0021はProposedを維持する。

## 入力と検証

| 対象 | 相対参照 | bytes | SHA-256 |
| --- | --- | ---: | --- |
| 元証跡（読み取りのみ） | eval-artifacts/2026-10-20/2026-10-20-retrieval-it2-bge-m3-tuning.json | 74015289 | c8f2adbf885f0a490e96c8f85cde8ea2acd80cdac54c51f369be3b91a5dc8e42 |
| 参考再採点の詳細（リポジトリ外） | eval-artifacts/2026-10-20/memory-v2-bge-m3-reference.json | 98077818 | 4a0274b63f9530d2b35c3b772b433dc693f533a22abf0e88769274c6c86d55a1 |

元JSONのbyte hash、保存gzipの展開結果との完全一致を確認した。
固定v1 suite・元の90設定を使って、270 runを旧規則のvalidate_public_reportで再検証した。
閾値0.20の18 runについて、各caseのretrieved_hitsが全適格IDと一致することを
重複も含めて検査した。その6成分を使い、同じ45設定×2方式×3回を再採点した。
query・原文・required_ids・forbidden_ids・必須原文・required_orderは変更していない。
最後に最終コードで再計算し、詳細JSONがbyte単位で同一になることを確認した。

元証跡にないraw cosineはsemanticから逆算しない。rerankerスコアもnullのままとし、
遅延・資源計測を新しい結果として転記しない。公開した詳細に本文・query・vectorは含めない。
[小さい集計](2026-10-20-memory-v2-reference-summary.json)に各回のゲートと分位点を保存した。

## 結果

両方式ともsemantic=0.75、lexical=0.05、残り4項各0.05、threshold=0.75を選んだ。
各回の安全はPASS、品質はFAILだった。各方式の3回は次の合格件数で一致した。

| 分類 | 合格件数 | 必要率 |
| --- | ---: | ---: |
| synonym | 2/10 | 90% |
| paraphrase | 1/10 | 90% |
| cross_language | 0/24 | 90% |
| unrelated | 22/22 | 70% |
| deleted_source | 1/1 | 90% |
| deleted_memory | 1/1 | 90% |
| binding_character | 1/1 | 90% |
| threshold：正解あり | 6/6 | 90% |
| threshold：該当なし | 6/6 | 70% |
| equivalent_order | 0/1 | 90% |

安全違反0の設定から、最低想起合格率→最低該当なし合格率→平均recallで選定した。
実モデルの全順序一致を要求していない。選定後の品質FAILを採用成功とは扱わない。

semantic=0.75、pg_bigm、run 1で選定指標を直接比較すると次の値となる。
低閾値では正解包含が高くても、品質誤取得などを含む分類別合格率は0になっていた。
最低想起合格率の同率を次の指標で比較する規則のため、閾値は自動的には下がらなかった。
この観測を理由に今回の固定規則やgoldを変更していない。

| 閾値 | 最低想起合格率 | 最低該当なし合格率 | 平均recall |
| --- | ---: | ---: | ---: |
| 0.20 | 0 | 0 | 1.0000 |
| 0.50 | 0 | 0 | 1.0000 |
| 0.60 | 0 | 0 | 0.9074 |
| 0.75 | 0 | 1 | 0.2222 |
| 0.90 | 0 | 1 | 0.0000 |

## スコア分布

選定したpg_bigmのrun 1から、閾値前の合成スコアを示す。
「非正解」は各caseの非正解最高値を1標本にする。全方式・全回の
count・min・p05・p50・p95・maxは集計JSONを参照する。欠測は0で補っていない。
これらの厳密値を品質ゲートには使っていない。

| 分類・群 | min | p05 | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| synonym 正解 | 0.6637 | 0.6772 | 0.7107 | 0.7672 | 0.7769 |
| synonym 非正解 | 0.6461 | 0.6469 | 0.7000 | 0.7345 | 0.7373 |
| paraphrase 正解 | 0.6573 | 0.6644 | 0.7137 | 0.7638 | 0.7773 |
| paraphrase 非正解 | 0.6123 | 0.6203 | 0.6803 | 0.7167 | 0.7177 |
| cross_language 正解 | 0.5389 | 0.5585 | 0.6213 | 0.6483 | 0.6791 |
| cross_language 非正解 | 0.4532 | 0.4930 | 0.5700 | 0.6244 | 0.6412 |

正解群と非正解群は重なっており、このtuningだけで採用閾値を確定しない。
acceptanceの効果、追加モデル、reranker、実機HTTP形式、段別の実遅延は
親側のIT2で確認する必要がある。この工程ではIT2・STを実行していない。

## 再現手順

[評価手順](../retrieval-evaluation.md)の参考再採点コマンドを使う。
SOURCE_CANDIDATESには元公開報告のcandidates配列をそのまま書き出し、
元suiteとv2 suiteのbyte hashを実行前に照合する。出力はリポジトリ外の新規ファイルへ出す。
元報告のhash不一致・旧判定改変・低閾値候補の欠落は拒否する。
合成証跡の正常系・拒否系とCLI入口はtests/evaluation/v2/test_rescore.pyで検証する。
