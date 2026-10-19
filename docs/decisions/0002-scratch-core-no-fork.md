# 0002. ドメインコアはフルスクラッチで作り、既存OSSはフォークしない

- 日付：2026-10-19
- 状態：承認

## 背景

Letta、Graphiti、Mem0、OpenVault、Smart Memory などを比較した。原文の保持と出典リンクは Graphiti が最も近いが、内省・信念・人格・声・統制の層はどのOSSにも存在しない。Letta はメモリブロックを出典なしで書き換える設計で、V1 APIサーバーは退役している。SillyTavern 系の拡張は要約中心で UI に密結合し、OpenVault は AGPL-3.0 である。

## 決定

- 6コンテキスト（Evidence, Memory, Reflection, Identity, Voice, Governance）のドメイン層とユースケースは自作する。LLMに依存しない純粋なドメイン層とする。
- 外部依存はポート（EvidenceStore, DerivedMemoryStore, Retriever, LLMGateway, Reviewer）の裏のアダプタに閉じ込める。
- 既存OSSはフォークせず、設計のアイデアだけを流用する（Generative Agents の検索スコア、Graphiti の二時間軸と無効化、LongMemEval のキー拡張、OpenVault の内省トリガー）。

## 影響

- 上流への追従コストが発生しない代わりに、保存・検索部分も自前で組む。
- 時間付き事実グラフが必要になった場合は、Graphiti を DerivedMemoryStore のアダプタとして追加できる。
