# memory-v2.2（チャンク索引比較）

ADR 0021追補の固定評価。bge-m3 Q8_0 / CLS / 1024次元、pg_bigm、
bge-reranker-v2-m3、512文字・重なり64文字、rerank上位20チャンク、
semantic 0.55 / lexical 0.25 / 残り4項各0.05、raw relevance -2、
同じ記憶IDを返却前に統合、最大5件・JSON 1500tokenを固定する。
探索・閾値再調整はしない。

既存memory-v2.1のtuning 82件・acceptance 55件を保持し、両splitへ
局所事実2件・「前者」「その箱」を含む文脈依存2件を合成追加した。
局所事実は長い無関係文の中央にだけ置き、原文全体を索引本文とする。
必須出典は正解文全体。IDと本文をsplitごとに分離する。

既存のPostgresSearch、実embedding/reranker/tokenizer port、適格性判定、
score_case / aggregate_runを`tests/chunk_evaluation.py`から使用する。
既存validatorと採点器が版を固定しているため、suite内のprotocolは互換性用の
`memory-v2.1`を維持し、ディレクトリ・実行manifest・結果を`memory-v2.2`で識別する。
この比較器は製品HTTP、ProvisionRecord、削除競合の検証を代替しない。
返却JSON形状と窓の縮小方式は旧比較器のものであり、製品との違いを報告に残す。

```sh
# CONFIGは旧比較と同形式のloopback専用合成DB・採用モデルidentity設定
python -m tests.chunk_evaluation CONFIG /absolute/evidence/quality
```

開始前に両suiteのbyte hashを固定し、tuning結果のhashをacceptance開始前に固定する。
両splitとも独立した使い捨てDBで3回。tuningのFAILをacceptanceのPASSで消さない。
安全は各分類・各回100%、想起は各分類・各回90%以上、該当なしは70%以上。
実測値と制約は`docs/reports/2026-10-29-recall-chunk-eval.md`を参照する。
