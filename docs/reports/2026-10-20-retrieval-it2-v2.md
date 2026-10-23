# memory-v2：GPU 実モデルによる検索方式比較

2026-10-20 の実モデル IT2。これは保存済み v1 の参考再採点ではない。
合成入力だけをローカル llama.cpp と専用 PostgreSQL で評価し、親の判断・レビューへ返す。
ADR 0021 は Proposed のまま。採用・承認とは区別する。

## 条件と測定範囲

- 起点は `f58b47e37bcf7c4fc67d9b7bae4c67075ace963b`。独立 worktree、Core は `b062a49a32d261d0ce472938c40711c6cb41d7c0` を読み取り参照。
- `memory-v2` の tuning 82件、acceptance 55件、固定45設定、両全文検索方式、各3回。vector・結果 cache なし。DB/OS cache は制御せず、cold cache とは呼ばない。
- 安全は各回100%、想起分類は必須正解すべての上位5件包含で各回90%以上、該当なし分類は空結果で各回70%以上。threshold の正解あり／なしは分離。precision と誤取得は報告指標であり合否・選定に加えない。
- 安全違反0の設定から、各分類・回の最低想起合格率、最低該当なし合格率、平均 recall の順で選び、完全同率なら固定グリッド先頭。acceptance を調整に戻さない。
- 6項加重和と最大5件・UTF-8 1,500byte 推定予算を維持。実モデルの全順序一致を要求しない。元入力・gold・スコア式・評価コードは変更していない。
- reranker は全適格候補を並べ替える。除外には6項合成スコアの閾値を使い、reranker 自身のスコアには棄却閾値を設けない。無効／有効は別候補ファイルで比較する。
- passage 埋め込み・索引作成は想起遅延の外。query 埋め込み・検索・rerank・返却片構築を想起合計に含め、独立採点は除く。製品 HTTP・ProvisionRecord 書込みは未計測。
- 専用 DB は CPU 1、RAM 上限1 GiB、tmpfs。llama.cpp は ctx/batch/ubatch=2048、parallel=1、GPU layers=99、threads=8、cache-ram=0。GGUF の最大文脈長と今回の起動文脈長を区別する。
- ゲストは Intel Core i7-14700K、28論理CPU、MemTotal 約46.9 GiB、Ubuntu 24.04.4 / WSL2。
- RTX 4070 Ti SUPER（16,376 MiB）、WSL2。VRAM は GPU 全体であり他のホスト負荷を含む。RAM/VRAM は採取最大で、標本間の peak は保証しない。
- llama.cpp image は `sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`、build `b11347-5fc4f3c8c`。
- 前回 DB image はローカルに残っていなかった。今回の実 image は `sha256:4a937b4ea899b940296c486d20e5dfcd300a73e18a2abb7fa3e0af273414a480`。固定 package 一覧との `cmp` 一致、vector 0.8.2 / pg_bigm 1.2 / PGroonga 4.1.0 を確認。前回と同じ image digest だとは主張しない。Qwen3 の準備前にはこの image が消失したため、同じ固定 Dockerfile の全段をキャッシュから再構築し `sha256:0e8192649a3ffe47d73c899587779a693d1ffaaa643d48b5934f48e2c49387d2` を使用した。両方の package 一致を確認したが、bge と Qwen の間は byte 単位で同一 image の比較ではない。初回の準備 ERROR は実モデル呼出し0で、復旧後の測定とは別記録。
- 公開ポートは数値 loopback のみ。既存サービスを停止・変更せず、比較用の専用コンテナを使う。

embedding／rerank の prompt cache 再利用除外も[固定 build の公式ソース](https://github.com/ggml-org/llama.cpp/blob/5fc4f3c8c/tools/server/server-context.cpp#L3215-L3217)で確認した。cache-ram=0 と合わせ、vector・結果・prompt の再利用を測定短縮に使わない。

## モデルの確定値

[モデル証跡](2026-10-20-retrieval-it2-v2-models.json)に配布元・revision・SHA-256・サイズ・実 GGUF メタデータを保存した。
実行用の確定候補は `evals/retrieval/memory-v2/candidates-*.json`。元の未確定テンプレートは保持する。

| モデル | GGUF / 次元 / pooling | 最大文脈長 | query / passage 接頭辞 |
| --- | --- | --- | --- |
| bge-m3 | Q8_0 / 1024 / CLS | 8192 | 空 / 空 |
| Qwen3-Embedding-0.6B | Q8_0 / 1024 / last | 32768 | 下記の固定 instruction / 空 |
| bge-reranker-v2-m3 | Q8_0 / rank pooling | 8192 | `/rerank` に query と適格な派生本文のみを渡す |
| ruri-v3-310m | 公式 GGUF 不在のため NOT RUN | 元モデルは8192 | 元モデルは `検索クエリ: ` / `検索文書: `。GGUF の確定値ではない |

Qwen3 の接頭辞は公式例の
`Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`
（`\n` は改行、末尾の空白なし）に固定した。tuning/acceptance の結果から接頭辞を変えていない。

- [bge-m3 固定配布版](https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/tree/9eba04c5d75ba5a1595e45de734d36bef4e5cb98)の既存ファイルを hash 照合して再利用。
- [Qwen3 公式 GGUF 固定配布版](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/tree/370f27d7550e0def9b39c1f16d3fbaa13aa67728)を取得。last pooling は同版の公式説明、文脈長・次元・量子化は実 GGUF でも確認。
- [reranker 固定配布版](https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/tree/3093af03b1a635e67b084b1d8c03c5f5e020fd05)を取得。配布済み GGUF を使い、独自変換・パッチは行わない。
- [ruri 公式配布版](https://huggingface.co/cl-nagoya/ruri-v3-310m/tree/18b60fb8c2b9df296fb4212bb7d23ef94e579cd3)と公式 org の関連リポジトリ一覧に GGUF は見つからなかった。[第三者版](https://huggingface.co/Targoyle/ruri-v3-310m-GGUF)は独自 llama.cpp パッチによる変換を明記。依頼条件に従い、代替・変換を推測せず NOT RUN。長文を試していないため ERROR や品質 FAIL としない。

## 実行結果

PASS/FAIL は品質判定。ERROR は実行失敗、NOT RUN は未実行。遅延の範囲は各回の p50/p95 の最小〜最大であり、反復を合算した分位点ではない。

| 構成 | 全文検索 | 状態 | precision（case平均） | 誤取得/回 | 想起 p50 / p95 ms |
| --- | --- | --- | ---: | ---: | --- |
| bge-m3 | pg_bigm | FAIL | 0.4667 | 57 | 14.32–14.84 / 16.55–22.17 |
| bge-m3 | pgroonga | FAIL | 0.4667 | 57 | 14.92–15.69 / 18.30–20.84 |
| bge-m3 + reranker | pg_bigm | FAIL | 0.4667 | 57 | 27.66–28.59 / 34.85–38.89 |
| bge-m3 + reranker | pgroonga | FAIL | 0.4667 | 57 | 27.69–28.27 / 36.82–41.35 |
| Qwen3-0.6B | pg_bigm | FAIL | 0.4667 | 57 | 13.89–14.17 / 16.04–20.32 |
| Qwen3-0.6B | pgroonga | FAIL | 0.4667 | 57 | 13.62–14.62 / 15.99–18.49 |
| Qwen3-0.6B + reranker | pg_bigm | FAIL | 0.4667 | 57 | 27.57–31.03 / 36.20–43.82 |
| Qwen3-0.6B + reranker | pgroonga | FAIL | 0.4667 | 57 | 28.33–29.21 / 36.03–37.28 |
| ruri-v3-310m | 両方式 | NOT RUN | — | — | — |

### 固定 tuning による選定

| 構成 | 全文検索 | semantic / lexical / 他4項 | 閾値 |
| --- | --- | --- | ---: |
| bge-m3 | pg_bigm | 0.55 / 0.25 / 各0.05 | 0.20 |
| bge-m3 | pgroonga | 0.55 / 0.25 / 各0.05 | 0.20 |
| bge-m3 + reranker | pg_bigm | 0.55 / 0.25 / 各0.05 | 0.20 |
| bge-m3 + reranker | pgroonga | 0.55 / 0.25 / 各0.05 | 0.20 |
| Qwen3-0.6B | pg_bigm | 0.55 / 0.25 / 各0.05 | 0.20 |
| Qwen3-0.6B | pgroonga | 0.55 / 0.25 / 各0.05 | 0.20 |
| Qwen3-0.6B + reranker | pg_bigm | 0.55 / 0.25 / 各0.05 | 0.20 |
| Qwen3-0.6B + reranker | pgroonga | 0.55 / 0.25 / 各0.05 | 0.20 |

各構成は tuning 270 run・22,140ケース、acceptance 6 run・330ケース。設定・方式ごとに3反復し、embedding HTTP 呼出しは tuning 44,280回・acceptance 660回。reranker 有効構成ではさらに tuning 22,140回・acceptance 330回を実呼出しした。事前確認の合成要求は別枠。全候補の成分は閾値前から詳細証跡へ保存している。

### 分類別 acceptance

表は各回の合格数、precision（P）、誤取得件数（FP）。両全文検索方式・3回を合わせて同値なら1組を表示し、異なる場合は範囲を表示する。想起分類は90%以上、unrelated と threshold の該当なし側は70%以上、安全は各回100%が必要。threshold の正解あり側は acceptance に存在せず、tuning の独立ゲートで評価した。

| 分類 | bge-m3 | bge-m3 + reranker | Qwen3-0.6B | Qwen3-0.6B + reranker |
| --- | --- | --- | --- | --- |
| synonym | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 |
| paraphrase | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 |
| cross_language | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 | 10/10 (100%); P=0.5000; FP=10 |
| unrelated | 0/10 (0%); P=0.0000; FP=20 | 0/10 (0%); P=0.0000; FP=20 | 0/10 (0%); P=0.0000; FP=20 | 0/10 (0%); P=0.0000; FP=20 |
| negation | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 |
| multisource | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| multisource_revocation | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| deleted_source | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| deleted_memory | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| binding_character | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| long_text | 0/1 (0%); P=0.0000; FP=0 | 0/1 (0%); P=0.0000; FP=0 | 0/1 (0%); P=0.0000; FP=0 | 0/1 (0%); P=0.0000; FP=0 |
| update | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| fact_attachment | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| fact_version | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 | 1/1 (100%); P=0.5000; FP=1 |
| fact_revocation | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| semantic_direct | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 |
| semantic_derived | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 | 1/1 (100%); P=0.3333; FP=2 |
| equivalent_order | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 | 1/1 (100%); P=1.0000; FP=0 |
| threshold | 0/1 (0%); P=0.0000; FP=1 | 0/1 (0%); P=0.0000; FP=1 | 0/1 (0%); P=0.0000; FP=1 | 0/1 (0%); P=0.0000; FP=1 |

各回の品質ゲートと正解/非正解スコア分位点は構成別 summary の `shared_acceptance_quality`、反復の参照は `acceptance_runs[].quality_ref` に保存した。安全結果も同じ参照先で確認できる。

### 段別遅延

各セルは p50 / p95 ms（各回の範囲）。reranker 無効時に計測される微小時間は分岐・計時の時間で、実モデル呼出しは0のため「—」とする。

| 構成 | 全文検索 | query 埋め込み | 検索 | rerank | 合計 |
| --- | --- | --- | --- | --- | --- |
| bge-m3 | pg_bigm | 8.94–9.28 / 11.28–15.97 | 5.09–5.26 / 6.42–6.78 | — | 14.32–14.84 / 16.55–22.17 |
| bge-m3 | pgroonga | 8.89–9.78 / 11.66–13.50 | 5.38–5.61 / 7.51–8.23 | — | 14.92–15.69 / 18.30–20.84 |
| bge-m3 + reranker | pg_bigm | 7.89–8.40 / 11.20–12.69 | 4.90–5.07 / 6.26–7.86 | 14.90–15.04 / 19.94–22.33 | 27.66–28.59 / 34.85–38.89 |
| bge-m3 + reranker | pgroonga | 7.91–8.12 / 11.51–12.82 | 4.81–5.17 / 7.54–7.98 | 14.29–14.63 / 21.43–22.07 | 27.69–28.27 / 36.82–41.35 |
| Qwen3-0.6B | pg_bigm | 9.52–9.76 / 11.20–13.70 | 4.11–4.28 / 5.35–6.75 | — | 13.89–14.17 / 16.04–20.32 |
| Qwen3-0.6B | pgroonga | 9.33–9.68 / 10.46–12.86 | 4.10–4.67 / 5.73–7.12 | — | 13.62–14.62 / 15.99–18.49 |
| Qwen3-0.6B + reranker | pg_bigm | 9.98–10.59 / 11.14–16.63 | 4.48–5.20 / 5.80–8.05 | 13.72–14.88 / 20.70–21.92 | 27.57–31.03 / 36.20–43.82 |
| Qwen3-0.6B + reranker | pgroonga | 9.56–10.10 / 11.13–12.11 | 4.75–4.90 / 6.73–7.02 | 13.89–14.49 / 19.14–19.60 | 28.33–29.21 / 36.03–37.28 |

passage 埋め込み、index、返却片構築、採点の段別 p50/p95 と passage の最大値等は summary に保存した。ADR 0017 の目標 p95 300ms・必達600msとの比較は各回の `latency` に記録し、製品ターン全体への適合とは区別する。

### CPU・RAM・VRAM・索引

CPU は各 run の累積差分の範囲、RAM は当該構成の採取最大、VRAM は GPU 全体の採取範囲。ハーネス RSS はプロセス寿命の最大値で、run 増分ではない。

| 構成 | 全文検索 | DB / 埋め込み CPU ms | DB / 埋め込み RAM MiB | GPU 全体 MiB |
| --- | --- | --- | --- | --- |
| bge-m3 | pg_bigm | 272.70–282.22 / 1163.08–1664.71 | 159.3 / 1323.8 | 4716–4724 |
| bge-m3 | pgroonga | 504.40–522.61 / 1168.54–1258.17 | 345.0 / 1324.3 | 4721–4721 |
| bge-m3 + reranker | pg_bigm | 273.79–284.19 / 1068.55–1608.21 | 158.8 / 1314.9 | 3971–4052 |
| bge-m3 + reranker | pgroonga | 485.45–490.72 / 1056.36–1112.21 | 346.1 / 1314.5 | 3957–4027 |
| Qwen3-0.6B | pg_bigm | 257.37–263.37 / 1266.09–1834.60 | 160.1 / 989.9 | 5742–5854 |
| Qwen3-0.6B | pgroonga | 464.73–487.86 / 1286.80–1313.26 | 344.8 / 990.2 | 5685–5749 |
| Qwen3-0.6B + reranker | pg_bigm | 265.17–289.94 / 1424.45–1757.07 | 160.1 / 973.9 | 5907–6135 |
| Qwen3-0.6B + reranker | pgroonga | 497.06–566.94 / 1356.29–1397.74 | 346.3 / 974.3 | 6025–6115 |

#### 索引・ハーネス資源

| 構成 | 全文検索 | 索引 byte p50（各回） | 索引作成 ms p50（各回） | ハーネス CPU ms / peak RSS MiB |
| --- | --- | --- | --- | --- |
| bge-m3 | pg_bigm | 16384 | 0.410–0.417 | 411.85–428.14 / 557.2 |
| bge-m3 | pgroonga | 35033088 | 2.419–2.514 | 479.85–487.75 / 557.2 |
| bge-m3 + reranker | pg_bigm | 16384 | 0.380–0.398 | 452.47–483.66 / 568.6 |
| bge-m3 + reranker | pgroonga | 35033088 | 2.262–2.320 | 513.55–522.95 / 568.6 |
| Qwen3-0.6B | pg_bigm | 16384 | 0.351–0.363 | 370.18–397.08 / 548.4 |
| Qwen3-0.6B | pgroonga | 35033088 | 2.057–2.107 | 420.93–444.09 / 548.4 |
| Qwen3-0.6B + reranker | pg_bigm | 16384 | 0.343–0.405 | 464.93–510.92 / 558.7 |
| Qwen3-0.6B + reranker | pgroonga | 35033088 | 2.165–2.325 | 518.81–525.53 / 558.7 |

索引は小さなケースごとに再作成した値。PGroonga は Sources/Lexicon/列の disk_usage 合計、pg_bigm は索引 relation のサイズ。table/vector byte・投入時間・索引作成 p95・各回値も summary に保持する。実運用規模や近似 vector 索引の測定ではない。reranker の CPU/RAM は別の外部採取証跡から後掲の環境集計に記録する（約1秒待機間隔、ハーネスのDB/埋め込み観測は100ms待機間隔）。

reranker の追加 CPU/RAM は各 CLI stage 全体の採取範囲で、6個の acceptance run の個別差分ではない。

| reranker 構成 | stage | CPU ms（最初〜最後の標本差） | RAM MiB 採取最大 |
| --- | --- | ---: | ---: |
| bge-m3 + reranker | tuning | 507408.78 | 514.7 |
| bge-m3 + reranker | acceptance | 4740.41 | 1345.0 |
| Qwen3-0.6B + reranker | tuning | 436324.63 | 514.7 |
| Qwen3-0.6B + reranker | acceptance | 4733.85 | 1345.8 |

### スコアのレンジ

正解は全正解候補、非正解は各ケースの非正解候補の最高値を集計する。範囲・分位点は分類別・各回で保持し、ここでは pg_bigm の第1回の主要分類を抜粋する。尺度の異なる6項合成 score と reranker の raw relevance score を混ぜない。

#### composite

| 構成 | 分類 | 正解 min / p05 / p50 / p95 / max | 非正解最高値 min / p05 / p50 / p95 / max |
| --- | --- | --- | --- |
| bge-m3 | synonym | 0.6253 / 0.6266 / 0.6490 / 0.6948 / 0.6970 | 0.5036 / 0.5052 / 0.5349 / 0.5724 / 0.5776 |
| bge-m3 | paraphrase | 0.5668 / 0.5829 / 0.6497 / 0.7036 / 0.7255 | 0.5023 / 0.5024 / 0.5263 / 0.5872 / 0.6042 |
| bge-m3 | cross_language | 0.4530 / 0.4575 / 0.4988 / 0.5218 / 0.5279 | 0.3638 / 0.3659 / 0.4409 / 0.5657 / 0.5681 |
| bge-m3 | unrelated | — | 0.4830 / 0.4837 / 0.5051 / 0.5430 / 0.5453 |
| bge-m3 + reranker | synonym | 0.6253 / 0.6266 / 0.6490 / 0.6948 / 0.6970 | 0.5036 / 0.5052 / 0.5349 / 0.5724 / 0.5776 |
| bge-m3 + reranker | paraphrase | 0.5668 / 0.5829 / 0.6497 / 0.7036 / 0.7255 | 0.5023 / 0.5024 / 0.5263 / 0.5872 / 0.6042 |
| bge-m3 + reranker | cross_language | 0.4530 / 0.4575 / 0.4988 / 0.5218 / 0.5279 | 0.3638 / 0.3659 / 0.4409 / 0.5657 / 0.5681 |
| bge-m3 + reranker | unrelated | — | 0.4830 / 0.4837 / 0.5051 / 0.5430 / 0.5453 |
| Qwen3-0.6B | synonym | 0.5425 / 0.5489 / 0.6095 / 0.6635 / 0.6689 | 0.4293 / 0.4359 / 0.5042 / 0.5478 / 0.5507 |
| Qwen3-0.6B | paraphrase | 0.5321 / 0.5409 / 0.6018 / 0.6744 / 0.6953 | 0.4564 / 0.4680 / 0.4923 / 0.5440 / 0.5689 |
| Qwen3-0.6B | cross_language | 0.3868 / 0.3913 / 0.4292 / 0.4667 / 0.4743 | 0.2884 / 0.2903 / 0.3628 / 0.5331 / 0.5465 |
| Qwen3-0.6B | unrelated | — | 0.4179 / 0.4234 / 0.4480 / 0.4746 / 0.4839 |
| Qwen3-0.6B + reranker | synonym | 0.5425 / 0.5489 / 0.6095 / 0.6635 / 0.6689 | 0.4293 / 0.4359 / 0.5042 / 0.5478 / 0.5507 |
| Qwen3-0.6B + reranker | paraphrase | 0.5321 / 0.5409 / 0.6018 / 0.6744 / 0.6953 | 0.4564 / 0.4680 / 0.4923 / 0.5440 / 0.5689 |
| Qwen3-0.6B + reranker | cross_language | 0.3868 / 0.3913 / 0.4292 / 0.4667 / 0.4743 | 0.2884 / 0.2903 / 0.3628 / 0.5331 / 0.5465 |
| Qwen3-0.6B + reranker | unrelated | — | 0.4179 / 0.4234 / 0.4480 / 0.4746 / 0.4839 |

#### reranker

| 構成 | 分類 | 正解 min / p05 / p50 / p95 / max | 非正解最高値 min / p05 / p50 / p95 / max |
| --- | --- | --- | --- |
| bge-m3 + reranker | synonym | -0.5761 / 0.9095 / 5.2524 / 6.5186 / 6.7530 | -10.5075 / -10.4442 / -8.0247 / -4.6679 / -4.3773 |
| bge-m3 + reranker | paraphrase | -2.8522 / -2.2534 / 3.9757 / 7.2511 / 7.5095 | -9.8243 / -9.4577 / -7.0559 / -5.0534 / -4.2179 |
| bge-m3 + reranker | cross_language | -5.8858 / -2.6436 / 1.9190 / 3.0688 / 3.0905 | -10.7216 / -10.6734 / -9.5759 / -3.5191 / -2.6621 |
| bge-m3 + reranker | unrelated | — | -10.3100 / -10.2825 / -9.7287 / -5.9830 / -5.2696 |
| Qwen3-0.6B + reranker | synonym | -0.5761 / 0.9095 / 5.2524 / 6.5186 / 6.7530 | -10.5075 / -10.4442 / -8.0247 / -4.6679 / -4.3773 |
| Qwen3-0.6B + reranker | paraphrase | -2.8522 / -2.2534 / 3.9757 / 7.2511 / 7.5095 | -9.8243 / -9.4577 / -7.0559 / -5.0534 / -4.2179 |
| Qwen3-0.6B + reranker | cross_language | -5.8858 / -2.6436 / 1.9190 / 3.0688 / 3.0905 | -10.7216 / -10.6734 / -9.5759 / -3.5191 / -2.6621 |
| Qwen3-0.6B + reranker | unrelated | — | -10.3100 / -10.2825 / -9.7287 / -5.9830 / -5.2696 |

## 制約と親の判断事項

### 推奨案

採用は保留する。全ゲートを満たす構成はなく、想起を保つ閾値をそのまま製品へ採用する推奨はしない。
次の検証の比較基準は **bge-m3 Q8_0 / CLS / 1024次元、pg_bigm、reranker なし**、
semantic=0.55・lexical=0.25・recency/importance/self_relevance/trust 各0.05、閾値0.20とする案。
これは固定 tuning が選んだ値を再現する基準であり、品質合格の採用値ではない。
既存の基準モデルを継続でき、今回の両全文検索方式の品質差はなく、pg_bigm の小さな合成索引は軽かった。
reranker は順位のみを変える条件では品質を改善せず、追加のモデル常駐・処理時間を要した。

Qwen3 は比較候補として保持する。選定設定の品質・precision・誤取得は同じで、
今回の観測では低い p95 も得た。ただし bge と Qwen は DB image digest が異なり、
GPU 全体には既存のホスト負荷も含むため、小さな時間差をモデル固有の優劣と断定しない。
最大文脈長32,768は GGUF の宣言値で、今回その上限を実測したという意味ではない。
ruri-v3-310m は公式 GGUF 不在のため未実行であり、品質の順位を付けない。

### 満たせなかった基準と理由

- **該当なし各回70%以上**：unrelated と threshold の該当なし側が0%。想起優先の固定選定で低閾値0.20が選ばれ、
  無関係候補も6項合成 score の閾値を通過する。閾値前の正解・非正解の score 分布は重なり、
  固定45設定には全ゲートを満たす設定がなかった。acceptance を使って再調整していない。
- **長文の想起各回90%以上**：0/1。モデルは固定長文を埋め込めたが、必須返却片の正規 JSON が4,060byteで、
  1,500byte予算を超える。bge の合成 score は約0.523、Qwen は約0.552で閾値0.20以上でも、
  `return_budget.excluded.reason=token_budget` により返却されない。入力上限の ERROR と区別する。
  固定の原文区間を維持したままでは、モデルや閾値を変えてもこの1件の返却予算は変わらない。
- **reranker による空結果の改善**：今回の仕様は reranker score を棄却に使わず、全適格候補の順位だけを変える。
  acceptance は各ケースの適格候補数が5件以下（1件×9、2件×43、3件×2、5件×1）で、
  返却予算に収まるケースでは上位5件の外へ非正解を落とす余地も小さい。
  今回の品質未達を reranker モデル一般の有効性の否定へ広げない。

安全は各回100%。想起の同義語・言い換え・日英横断などは基準を満たしたが、
これで該当なし・長文の FAIL を相殺しない。precision は合否に使わず、誤取得57件/回を残す。
遅延目標への適合もハーネス内の観測であり、品質合格や製品ターン全体の受入を意味しない。

### 判断を返す点

1. 原文区間を切らずに返す要件と、UTF-8 1byte=1トークン相当という1,500byte見積りをどう両立するか。
   実 tokenizer による予算・必要区間の取り方など、比較条件の変更は親の判断後の別の検証とする。
2. 想起を保って該当なし70%以上を目指すため、reranker score 等による棄却・no-match 判定を新しい条件として検証するか。
   今回は6項合成閾値・固定グリッドを変更せず、観測レンジだけを報告した。
3. 交差モデルの厳密な資源比較で、全構成を同一 byte hash の DB image に揃えた追試を必要とするか。
   今回の bge の有無比較は同じ4a937...、Qwen の有無比較は同じ0e819...であり、
   両方とも同じ固定 Dockerfile・拡張版・package 一覧を使ったが、モデル間は image digest が異なる。

評価コードの不具合は特定しておらず、評価コードの修正は行っていない。
準備段階の image 消失 ERROR は別証跡に保持し、復旧後の実モデル試験の ERROR 件数へ混ぜない。

## 証跡・再現・通常検証

- [bge-m3 集計](2026-10-20-retrieval-it2-v2-bge-m3-summary.json)：固定入力 hash、全 tuning 設定、選定値、分類別・各回品質、スコア分位点、段別遅延、資源。

- [bge-m3 + reranker 集計](2026-10-20-retrieval-it2-v2-bge-m3-rerank-summary.json)：固定入力 hash、全 tuning 設定、選定値、分類別・各回品質、スコア分位点、段別遅延、資源。

- [Qwen3-0.6B 集計](2026-10-20-retrieval-it2-v2-Qwen3-Embedding-0.6B-summary.json)：固定入力 hash、全 tuning 設定、選定値、分類別・各回品質、スコア分位点、段別遅延、資源。

- [Qwen3-0.6B + reranker 集計](2026-10-20-retrieval-it2-v2-Qwen3-Embedding-0.6B-rerank-summary.json)：固定入力 hash、全 tuning 設定、選定値、分類別・各回品質、スコア分位点、段別遅延、資源。

- [環境・外部証跡一覧](2026-10-20-retrieval-it2-v2-environment.json)：実行日時、image、GPU 前後観測、reranker 追加資源、実行コマンド、後片付け、外部ファイルの相対名・SHA-256・byte数。

詳細は `eval-artifacts/2026-10-20/` の新規ファイルへ保存し、リポジトリには小さい集計だけを置いた。各 run/case の閾値前の全適格候補、raw cosine、6成分、合成 score、reranker score は詳細 JSON に保持している。固定 suite と確定候補で `validate_public_report` による独立再検証を行った。summary は同じ値の重複を参照へまとめ、各回に差があれば保存する。反復の平均で合否を置き換えない。

### 再実行の入口

DB・モデルの起動と preflight は[既存手順](../retrieval-evaluation.md#実環境実行)に従い、
本報告の image・各段の起動引数と確定候補を使う。`manifest.json` と `local-config.json` は
今回の実環境で確認した値から新規作成する。reranker 有効時は `reranker_endpoints` と
`reranker_identities` を追加し、同じモデルの無効／有効を別々に実行する。
最大文脈長はモデルメタデータ、今回の起動 ctx は2048である。

```sh
# ARTIFACT_ROOT はリポジトリ外のローカル artifact ルートを指定する。
# RUN_NAME は既存証跡と重複しない新しい名前にする。
CANDIDATES=evals/retrieval/memory-v2/candidates-bge-m3.json
TUNING=evals/retrieval/memory-v2/tuning/suite.json
ACCEPTANCE=evals/retrieval/memory-v2/acceptance/suite.json
TUNING_SHA256=$(sha256sum "$TUNING" | cut -d ' ' -f 1)
ACCEPTANCE_SHA256=$(sha256sum "$ACCEPTANCE" | cut -d ' ' -f 1)
mkdir -p "$ARTIFACT_ROOT/eval-artifacts/2026-10-20"
uv run --no-sync memory-evaluate-retrieval \
  --suite "$TUNING" --suite-sha256 "$TUNING_SHA256" --tune \
  --candidates "$CANDIDATES" --manifest manifest.json --real-config local-config.json \
  --mode real --output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/$RUN_NAME-tuning.json" \
  --summary-output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/$RUN_NAME-tuning-bulk-summary.json" \
  --artifact-reference "eval-artifacts/2026-10-20/$RUN_NAME-tuning.json"
# 品質FAILの終了1と実行ERRORの終了2を区別する。ERRORのあるtuningから選定しない。
TUNING_REPORT_SHA256=$(sha256sum "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/$RUN_NAME-tuning.json" | cut -d ' ' -f 1)
uv run --no-sync memory-evaluate-retrieval \
  --suite "$ACCEPTANCE" --suite-sha256 "$ACCEPTANCE_SHA256" \
  --tuning-suite "$TUNING" --tuning-sha256 "$TUNING_SHA256" \
  --tuning-report "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/$RUN_NAME-tuning.json" \
  --tuning-report-sha256 "$TUNING_REPORT_SHA256" \
  --candidates "$CANDIDATES" --manifest manifest.json --real-config local-config.json \
  --mode real --output "$ARTIFACT_ROOT/eval-artifacts/2026-10-20/$RUN_NAME-acceptance.json"
```

reranker ありは `candidates-bge-m3-rerank.json`、Qwen3 は
`candidates-Qwen3-Embedding-0.6B.json`（有効時は `-rerank.json`）を選ぶ。
`tuning` suite の SHA-256 は `cbedec3b65f75ed1e4cd2b873155d7ce55d9ee9fbffd01e2b6c04651ec66fb29`、
`acceptance` は `d9506eac3619196cdb75c8588a2e96ad9100d1cefb7378f8b6b8fe9db885c794`。
確定候補・各 manifest・選定に用いた tuning report の byte hash は集計に記録した。
再検証は外部 JSON の byte hash・サイズを照合してから固定 suite・候補を使う。

### 通常検証と後片付け

[通常検証証跡](2026-10-20-retrieval-it2-v2-validation.json)：UT **427 PASS**、IT1 **481 PASS**、実 PostgreSQL **177 PASS**。skip/xfail/xpass は0。ruff check/format、mypy strict、lock check、shell/Compose検証、wheel build、locked依存の独立 install/import、git diff check も PASS。固定 suite・確定候補による公開証跡の独立再検証は全1,104 runで PASS。

GPU IT2 の品質 FAIL を通常検証の PASS で置き換えない。
CI の最終 head 確認・レビュー・merge は親担当へ返し、この作業では merge しない。
ST・実会話・製品 HTTP・実運用規模・モデル同時常駐による製品受入は未実施。
既存サービスは変更せず、専用の測定コンテナ・DB image 保持用の停止コンテナを削除した。
ダウンロードした2 GGUF は保持し、保存先は引き渡し報告で伝える。Core ケースのライセンス表示・
配布可否は今回も判断せず、既存 provenance の未確定事項を維持する。
