# Core クライアントと Compose 機能 ST の実測

- 基点 revision: 3b6070404f02563206b9b06bce7cc630de52fa61。未コミット差分を含む。TAKT が commit / staging / push を担当するため実行していない。
- 環境: Linux 6.18.54.1-microsoft-standard-WSL2 x86_64、Python 3.12.3、uv 0.8.22、Docker Engine 29.6.0、Compose v5.2.0。
- 合成データのみ。実モデル・GPU・実会話・既存 DB は使っていない。
- 機能 ST build の image config: DB `sha256:aa48e5bb08fed8cd7a8c83a534cfd2b7d4ac4a8d0476a6406be69a702668edab`、Memory/runner `sha256:fa2f8222591b838d5fa6de9ff7b3874655da7433c00e92620d94f7919dbb8a14`（build.log）。

## 通常品質ゲート

先行implementで実行した UT は977 PASS、IT1 は906 PASS、実 PostgreSQL は494 PASS。
skip / xfail / xpass はない。各 suite は明示 marker で実行した。
lock確認・frozen sync、ruff check / format、mypy strict、既存と新規shellのbash -n、
既存と機能／性能Composeのconfig検査、wheel build・独立環境への
runtime hash付きinstall・API / evaluation / client import、diff検査も終了status 0。
詳細コマンドと個別契約対応はTAKTの指定implementation-report.mdに記録した。

## 機能結果

`MEMORY_CORE_ST_EVIDENCE_DIR=/tmp/core-st-functional-10 bash tools/with-core-st.sh`
を2026-10-22T04:36:14Zから実行。終了 status 0、4 passed、40.89秒。
プロジェクトは `memory-core-st-1792643774-3`。
専用 tmpfs PostgreSQL と実 Memory プロセス、Python HTTP client、実索引を使用した。
compose.logのHTTP結果ではSession登録201が4件、Turn登録201が14件、Turn登録422が7件、
Session重複409が1件、作者削除200が1件、未認証削除401が1件だった。
fake は人格生成・審査・抽出・embedding・reranker・tokenizer・短期状態／episode生成。

確認した操作は、人格・声・アンカー登録、Session / Turn / Receipt 往復、
同じ宛先で異なる知覚、自己発話・self・thought・空知覚、即時全文、
保留→599秒→600秒のみなし→partial→none、常駐と想起の版・提供記録、
提供による非強化・自己再表明の非同期再強化、命令文字列と人格不変、
作者認可、actor/addresseesによる削除、複数出典再計算・単独出典失効・
直接提供参照の失効、削除後非返却。
正確な assertion は `tests/st/test_functional.py` にある。

削除後の最初の想起では snapshot の再検証により503を1回観測した。
クライアントは失敗を返し、試験側が警告を記録して明示的に再試行した。
その後の成功応答で削除対象の出典がないことを検証した。
503を空成功へ置換していない。性能試験ではこのような失敗も全件を測定記録へ残す。

終了時は `down --volumes --remove-orphans` を実行し、当該 project の
container / network / volume が残っていないことを runner が確認した。
raw証跡は上記ディレクトリの tests.log / compose.log / cleanup.log /
result.txt / source-sha256.txt。後者は実行前の変更ファイルと未追跡ファイルの SHA-256。
永続証跡は cleanup の対象にしていない。

中断確認は /tmp/core-st-term。実行中のshellへSIGTERMを送り、前景のCompose run終了後に
trapが実行され、終了status143と専用資源の消失を確認した
（project memory-core-st-1792644175-3、tests自体は4 PASS/38.86秒）。
先に試したtoolのPTY Ctrl-Cではtrapの証跡が作られず専用資源が残ったため、
memory-core-st-1792644139-2を明示して手動downで全て除去した。
このtoolの中断機構でshell trapが実行されなかった理由は未確認であり、
その試行をcleanup成功としない。通常のSIGTERM送信とは試行条件が異なる。

## 先行実行の履歴

| 試行 | 結果 | 条件・修正 |
|---|---|---|
| functional-1 | FAIL、試験開始前 | compose run が完了済み init を再起動し、空 DB 検証が拒否した。--no-deps で起動済みサービスを使うよう修正 |
| functional-2 | 2 FAIL / 2 PASS | 空 approved_by を DTO が先に拒否したため raw HTTP の422検証へ変更。Receipt縮小後のfake候補IDへ本文を含め、異なる抽出結果を同一IDで再登録しない |
| functional-3 | 1 FAIL / 3 PASS | 元の記憶選択がRelationshipを含んでいたため、sourceを含むfactを選ぶ |
| functional-4 | 1 FAIL / 3 PASS | ended_at入りfixtureにより短期状態が作られなかった。ST用Sessionを未終了で作る |
| functional-5 | 1 FAIL / 3 PASS | 全Turnの同時刻によりID順で再表明が先行。occurred_atへ投入順を与える |
| functional-6 | 1 FAIL / 3 PASS | 削除後の想起503。活動中記憶の実索引完了を待つ確認を追加 |
| functional-7 | 4 PASS | 専用資源破棄も成功。その後、固定時計と索引retryの接続を明示した |
| functional-8 | 1 FAIL / 3 PASS | 索引完了観測後もsnapshot競合で503を観測 |
| functional-9 | 1 FAIL / 3 PASS | 削除後の抽出signature変更を必須にした観測が期限内に成立しなかった。元要件は最終保存内容・再想起の整合でありsignature更新ではないため、この追加条件は採用しなかった |
| functional-10 | 4 PASS | 実索引待機と、503を記録する呼出し側の明示retry後に最終出力を検証。製品規則・クライアントの失敗契約は保持 |

履歴はそれぞれ /tmp/core-st-functional-N の実測に基づく。未着保留とReceipt訂正は
同じプロセス・Session・client・DBで観測し、再起動で代用していない。
画像・モデルの保存先変更や回数変更だけを環境制約の解決案として扱っていない。

## 実モデル性能

**前回FAIL（[実モデル性能レポート](2026-10-22-core-st-performance.md)）**。親が引き継いだ実GPU・実モデルHTTP STでは、
合成データ100件・warm-up 5回を除く100回すべて成功応答だが、p50 **3,089.064ms** / p95 **3,350.914ms**。
必達p95 ≤600ms、目標p95 ≤300ms / p50 ≤150msのすべてに未達。1000件は準備中断により **NOT RUN**。
固定revision・モデルdigest・全標本・別診断の内訳・原因・修正案・cleanupは上記レポートと付属JSONを参照。
修正 #43 / PR #44 統合後の[再測定](2026-10-22-core-st-performance-rerun.md)は、100件で **必達PASS**（p50 104.973ms / p95 140.719ms）、目標も両方達成。1000件は30分の準備予算超過で **NOT RUN**。
PR #54統合後の[最終測定（100件・1000件）](2026-10-24-core-st-performance-final.md)と[要約JSON](2026-10-24-core-st-performance-final-summary.json)を参照。
PR #58 / #59統合後の[runner無変更での再測定](2026-10-24-core-st-performance-unmodified.md)と[要約JSON](2026-10-24-core-st-performance-unmodified-summary.json)を参照。100件・1000件とも標準runnerが終了0で完走し、必達PASS。
PR #63 / #64統合後の[再測定](2026-10-24-core-st-performance-after-r5.md)と[要約JSON](2026-10-24-core-st-performance-after-r5-summary.json)を参照。標準runnerは終了0、100件・1000件とも必達PASS。1000件p95は242.906ms、p50目標は未達。

PR #67統合後の[最終確認](2026-10-24-core-st-performance-after-r6.md)と[要約JSON](2026-10-24-core-st-performance-after-r6-summary.json)を参照。runner無変更・終了0、100件/1000件とも必達PASS。1000件p95は245.452ms、p50目標は未達。一覧0.258秒、累積準備183.751秒。前回比で一覧と準備の代替指標に増加あり。
以下の過去時点のNOT RUN記録は、その時点の履歴として保持する。

## reimplement の補完結果

runner の SHA-256 は `051a027f2500cc96be35462ce3e016a2be2b4682f4f0ccc9c497c3f7f8fc3fce`。
前段の runner と一致し、製品コード・shell・Compose・性能条件は変更していない。
`tests/it1/test_core_st_cleanup.py` を追加した。実 shell と制御端末、Docker CLI の
fake で正常終了0・失敗17・Ctrl-C終了130を観測し、各経路で logs/down と資源削除、
元の終了statusと永続証跡の保持を確認した（3 PASS）。

実 Compose を Python の `pty.fork()` で作った制御端末内から起動し、runner開始後に
端末へ `0x03` を書いた試行は `/tmp/core-st-reimplement-pty` に保存した。
runner status130、外側の検証status0。project `memory-core-st-1792644817-4` の
container/network/volume は0件。中断された機能試験自体をPASSとはしていない。

実行ツールのPTYへ同じ文字を送った試行は `/tmp/core-st-reimplement-before`。
tool終了1、result.txtなし、専用資源残存を再現し、project
`memory-core-st-1792644710-2` を手動downして除去した。
INT/TERM/HUP handlerを記録する最小Pythonプロセスにも同じ操作を実施した。
さらに `start_new_session=True` の別sessionの子でもhandler記録がなくheartbeatが停止した。
親のPID1のcommは `codex`。観測は `/tmp/core-st-interrupt-probe-result.json`。
通常の制御端末の中断との差は実行ツールのプロセス終了境界にあるが、内部のsignalと
終了順は未確認。shellを変更して解消できる欠陥は確認していない。C20のtool中断時の
自動破棄は未完了のまま計画担当へ返す。SIGTERM成功で置き換えていない。

機能シナリオ実行中の中断も別に確認した。`/tmp/core-st-reimplement-pty-active` では
`test_phase_one_http_lifecycle` の開始をtests.logで確認してから制御端末に0x03を送信。
runner終了130、外側の検証終了0、project `memory-core-st-1792645127-4` の破棄成功。
最初のPTY試行はrunner CLI開始時であり、この試行は実行中の試験まで到達した点が異なる。

今回再実行したUTは977 PASS、IT1は909 PASS、実PostgreSQLは494 PASS（200.08秒）。通常のCompose機能STは
`/tmp/core-st-reimplement-functional` で4 PASS / 41.97秒、runner終了0、cleanup成功。
この実行では削除後の503は観測していない。前段の503と明示retryの履歴は上記に保持する。
ruff check/format、mypy、lock/frozen sync、shell/Compose検査、wheel build・独立環境の
locked install/importも終了0。実モデル性能は引き続きNOT RUN。

## 親判断の反映（2026-10-22、上記試行後）

親エージェント（Claude）の追加判断により、C20のうち実行ツールのPTY中断で
handlerが動かず資源が残る経路は、本Issueの受入対象外とする。
上記の「未完了」は追加判断前の評価として保持し、現在のレビュー進行の停止理由にはしない。
通常終了・失敗・端末Ctrl-C（INT/TERM）の後片付けは引き続き受入対象であり、
既存の正常0・失敗17・端末中断130の回帰試験と実Composeの破棄証跡を保持する。
実行ツールの終了1・result.txt不在・資源残存・手動除去という観測は変更しない。
内部のsignal・終了順は未確認のまま追加調査を終了し、外部回答を進行条件にしない。

`docs/core-st.md` に、既存ログで確認した専用projectに限定した
`docker compose ... down -v --remove-orphans` と、同じproject labelによる
container・network・volumeの残存確認手順を補完した。証跡ディレクトリは保持する。
製品コード・runner・Compose・テスト・性能条件は変更せず、次工程はレビューとする。
実モデル性能は引き続きNOT RUNであり、親の実測義務を保持する。

## P01：機能STの時刻入力と期待値の整合（2026-10-22）

`TEST-NEW-tests-st-test_functional-L183` は resolved。人格登録後に既存の
`POST /_st/advance {"seconds":0}` でシナリオ時刻を取得し、Session開始時刻と
resident／turn提供記録の期待時刻に明示的に渡した。再強化時刻は引き続き
Turn.occurred_atと一致し、シナリオの提供時刻とは異なることを比較する。
HTTP・日時変換の失敗は伝播し、通信資源を処理内で閉じる。

修正前は通常順序が2回とも4 PASS、配達期限先行が2回とも1 FAIL / 3 PASS。
最初の失敗は旧test_functional.py:178の版比較で、実際は(None,None)、期待は(1,1)。
時計・気質登録・人格valid_from・両種の提供記録は11:10、Session開始は11:00だった。
日付はいずれも2026-01-02 UTC。修正後は先行順序でもSession開始・登録時刻が11:10、
版が(1,1)となり、両提供記録の時刻・版・内容の既存assertionを通過した。

| 段階 | 順序 | 試行 | 結果 | 秒 | 終了status | 専用資源破棄 |
|---|---|---|---|---|---|---|
| before | deadline-first | 1 | 3 PASS / 1 FAIL | 24.62 | 1 | 成功 |
| before | deadline-first | 2 | 3 PASS / 1 FAIL | 23.19 | 1 | 成功 |
| before | normal | 1 | 4 PASS / 0 FAIL | 38.53 | 0 | 成功 |
| before | normal | 2 | 4 PASS / 0 FAIL | 39.15 | 0 | 成功 |
| after | advance-one | 1 | 4 PASS / 0 FAIL | 42.8 | 0 | 成功 |
| after | deadline-first | 1 | 4 PASS / 0 FAIL | 41.75 | 0 | 成功 |
| after | deadline-first | 2 | 4 PASS / 0 FAIL | 43.2 | 0 | 成功 |
| after | normal | 1 | 4 PASS / 0 FAIL | 40.3 | 0 | 成功 |
| after | normal | 2 | 4 PASS / 0 FAIL | 39.56 | 0 | 成功 |

通常順序はlifecycle→thought True→thought False→配達期限、比較順序は
配達期限→lifecycle→thought True→thought False。advance-oneは人格登録前に
既存HTTP時計を1秒進め、その後に通常順序の全4ケースを実行した。
各phaseでrevision・image・Compose・fake・合成入力・並行度1・timeoutを固定し、
順序だけを比較した。各試行は新規専用tmpfs DB、ケース間は同じサービスとDBを維持。
修正前後のimage・対象2ファイルのSHA-256・開始日時・project・実コマンドは
core-client-st-evidence.jsonのp01と、各raw evidenceのresult.jsonに保存した。
未着→599秒→600秒→partial→none、thought両Session、削除後の非返却を保持した。
性能STのstarted_at=datetime.now(UTC)は維持し、機能時計には接続していない。

実行は `python3 .takt/p01_compare.py before` と `python3 .takt/p01_compare.py after`。
raw evidenceは `/tmp/core-st-p01-before` と `/tmp/core-st-p01-after`。
各試行のtests.logに時計・登録・Session・版・提供記録の観測値、compose.log、
cleanup.log、result.jsonを保持。初回の観測plugin読込失敗は試験前FAILとして
`/tmp/core-st-p01-before-setup-failed` に保持し、修正後に比較全体をやり直した。
これらの観測用scriptは.takt内のローカル検証資産で、製品・Composeの実行順序は変更していない。

通常品質ゲートはUT 977 PASS、IT1 909 PASS、postgres 494 PASS。
lock/frozen sync、ruff check/format、mypy、shell/Compose、wheel build・独立環境の
hash付きlocked install/import、diff検査も終了0。実行コマンドとログは
`/tmp/core-st-p01-quality/results.json`。時計helperの成功・日時変換失敗・HTTP失敗・
接続失敗と全ケースのcloseも合成transportで確認した（4 PASS）。
実モデル性能は引き続き **NOT RUN**。過去の失敗・PTY裁定は上記のまま保持する。
