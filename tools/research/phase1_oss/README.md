# Phase 1 OSS評価probe

製品実装・実モデルの品質試験とは独立した、合成データ限定の調査コード。
判断は[比較レポート](../../../docs/research/phase1-oss-reassessment.md)、実行結果は
[2026-10-26証跡](../../../docs/reports/2026-10-26-phase1-oss/)を参照。

## 対象と再現条件

- Python 3.12.3、Node 24.15.0、Docker。
- 上流コードは[sources.lock.json](sources.lock.json)のURL／完全SHAに固定。
- HindsightとMem0はprotobufの要求版が異なるため別venv。
- PostgreSQLは製品repoの比較イメージ、Neo4jは5.26-communityを利用。
  実行時イメージのIDは結果ディレクトリのenvironment.jsonに記録。
- embeddingは決定的なfixture、GraphitiのLLMは成功／例外のfixture。
  Hindsightの保存はchunksモードでLLMを使わず、同値判定にはfixtureを与える。Mem0はinfer=falseでLLM呼出しを禁止し、infer=trueの重複抑制試験だけfixture抽出器を使う。
- 外部TCP接続をprobe内で拒否。Graphitiだけ専用loopbackポート18957を許可。
- ST系・Lettaはnative純粋関数を直接実行。OpenVaultはutilsのID生成・logだけstub。
  STブラウザー／Letta全runtime／実モデル品質の合格を意味しない。

## 準備

`OSS_PROBE_ROOT`の既定は`/tmp/phase1-oss-reassessment`。
同名コンテナや同じsocket／portを使う別実行と並行させない。

各sourceをその名前のサブディレクトリへ取得し、lockのcommitをcheckoutする。
たとえばHindsightは以下（他候補もlockのURL／SHAで同じ手順）。

```bash
git init /tmp/phase1-oss-reassessment/hindsight
git -C /tmp/phase1-oss-reassessment/hindsight fetch --depth 1 https://github.com/vectorize-io/hindsight.git f29ba70c825094c839daaf27b2b5a564de46fb4e
git -C /tmp/phase1-oss-reassessment/hindsight checkout --detach FETCH_HEAD
python3 -m venv /tmp/phase1-oss-reassessment/venv
python3 -m venv /tmp/phase1-oss-reassessment/retrieval-venv
```

Hindsightは`hindsight-api-slim`を使い、`all`／`local-ml` extraを入れない。
固定した依存は以下。ネットワーク取得が必要な準備と、モデルを使わない実行を分ける。

```bash
/tmp/phase1-oss-reassessment/venv/bin/python -m pip install -r docs/reports/2026-10-26-phase1-oss/hindsight-dependencies.txt
/tmp/phase1-oss-reassessment/venv/bin/python -m pip install --no-deps -e /tmp/phase1-oss-reassessment/hindsight/hindsight-api-slim
/tmp/phase1-oss-reassessment/retrieval-venv/bin/python -m pip install -r docs/reports/2026-10-26-phase1-oss/retrieval-dependencies.txt
/tmp/phase1-oss-reassessment/retrieval-venv/bin/python -m pip install --no-deps -e /tmp/phase1-oss-reassessment/mem0 -e /tmp/phase1-oss-reassessment/graphiti
```

runnerの既定イメージは当日使ったPostgreSQLのローカルIDとNeo4jのdigestに固定し、自動pullしない。
PostgreSQLの同じローカルイメージがなければ、repoの`compose.retrieval-evaluation.yml`からビルドし、
`OSS_PROBE_POSTGRES_IMAGE`へ新しいIDを明示する。その場合は元runとイメージIDが違う再現として記録する。
Neo4jはenvironment.jsonのdigestを取得できる。別イメージを明示する場合は`OSS_PROBE_NEO4J_IMAGE`を使う。

## 実行

```bash
bash tools/research/phase1_oss/run-probes.sh
```

runnerは固定commitを検査し、新規の合成DBだけを作り、終了時に自分が作ったコンテナだけを削除する。
同名が既存なら失敗し、既存コンテナを先に削除しない。PostgreSQLはネットワーク無効・Unix socketのみ。
Neo4jはloopback限定・統計送信無効。データはtmpfs。
環境によりDB socket／Dockerの利用許可が必要。

結果JSONは`OSS_PROBE_ROOT`に保存する。**execution=PASSは観測の成功**であり、
`phase1`にGAPと記載した挙動は要件不適合。Graphitiは正常／再構築失敗の2シナリオを記録する。
途中で失敗した実行を最終成功JSONと混同しないよう、runner実行前に旧結果を退避する。

## 検証上の限界

再現用freezeは当日の解決版を固定するが、配布物hash付きlockではない。
probe内で使う上流はソースcommitと合わせて確認する。
Hindsightの最初のobservationはfixtureで直接seedした。同一Factの統合はnative候補検索→fixture同値判定→native統合書込みを、削除はnativeのdelete_documentを検証した。
LLMによる意味的同値判定・生成・再consolidationの品質は未実施。Graphitiは実Neo4j上のnative削除だが、サマリ本文はfixture。
日本語検索品質・長期人格・実モデル込みp95はこのprobeで判定しない。
