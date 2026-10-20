# 0002. ドメインコアはフルスクラッチで作り、既存OSSはフォークしない

- 日付：2026-10-19
- 状態：承認

## 背景

Letta、Graphiti、Mem0、OpenVault、Smart Memory などを比較した。原文の保持と出典リンクは Graphiti が最も近いが、内省・信念・人格・声・統制の層はどのOSSにも存在しない。Letta はメモリブロックを出典なしで書き換える設計で、V1 APIサーバーは退役している。SillyTavern 系の拡張は要約中心で UI に密結合し、OpenVault は AGPL-3.0 である。

## 決定

- 6コンテキスト（Evidence, Memory, Reflection, Identity, Voice, Governance）のドメイン層とユースケースは自作する。LLMに依存しない純粋なドメイン層とする。
- 外部依存はポート（EvidenceStore, DerivedMemoryStore, Retriever, LLMGateway, Reviewer）の裏のアダプタに閉じ込める。
- 既存OSSはフォークせず、設計のアイデアだけを流用する（Generative Agents の検索スコア、Graphiti の二時間軸と無効化、LongMemEval のキー拡張、OpenVault の内省トリガー）。

## 検討した案

いずれも 2026-10-19 に確認した。

| 候補 | 合う点 | 合わない点 | 判断 |
| --- | --- | --- | --- |
| [Letta](https://github.com/letta-ai/letta)（Apache-2.0） | 原文の全履歴を保持、夢見処理による記憶の書き直し | V1 APIサーバーは archive ブランチに退役し、現行は letta-code に移行。メモリブロックは出典なしで書き換えられ、P1・M1 と思想が逆 | フォークしない |
| [Graphiti](https://github.com/getzep/graphiti)（Apache-2.0） | エピソード（原文）を保持し、事実から出典をたどれる。事実を削除せず無効化する二時間軸モデル | 削除時に、無効化された事実や共有サマリが再計算されない（G5 を満たさない）。Neo4j／FalkorDB／Neptune が必須 | Memory 層の実装候補としてアダプタ越しに依存できる |
| [OpenVault](https://github.com/7n1m4/vault)（AGPL-3.0） | キャラ単位の内省がある唯一の拡張 | SillyTavern に密結合、要約中心で原文を刈り込む機能あり。AGPL はサービス提供時にソース公開義務が及ぶ | 内省トリガーの設計だけ参考にする |
| Smart Memory／CharMemory | 多層の状態管理、矛盾チェック | SillyTavern 拡張で要約中心、原文との対応なし | 参考にとどめる |
| Mem0 | 2026年に追記専用へ転換、infer=False で原文保存も可能 | 抽出が主眼で、内省・人格の層はない | 不採用 |

フォークを選ばなかった理由：

- **思想の衝突**：既存OSSの多くは要約や記憶ブロックを書き換える前提で、E1（原文は不変）や P1（コア人格は書き換え不可）を後付けするとフォーク元の中心部分と戦うことになる。
- **構成との相性**：UI 一体型のフォーク元は、ヘッドレスな独立サービスという構成に合わない。
- **フォークの維持費**：上流への追従コストが、借りられる部分（保存と検索）の価値を上回る。保存と検索は Postgres と pgvector と全文検索で十分に組める。

## 影響

- 上流への追従コストが発生しない代わりに、保存・検索部分も自前で組む。
- 時間付き事実グラフが必要になった場合は、Graphiti を DerivedMemoryStore のアダプタとして追加できる。
