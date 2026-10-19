# 原文を正本に据え、要約を索引に降格させよ

**結論を先に述べる。** 長期記憶でも内省でも、会話履歴の原文は重要である。ただし役割が違う。長期記憶では、原文は**ソースオブトゥルースであり、読み手（応答生成LLM）に渡す「値」**として必要になる。要約や抽出事実を値として使うと、事実想起で一貫して負ける。要約・事実が力を発揮するのは**検索キー・時間インデックス・常駐プロフィール**として使うときである（LongMemEval、SeCom、Fidelity Before Structure ほか）。内省では、毎回すべての原文を読み直す必要はない。しかし**内省の根拠（evidence）と検証対象**として原文への到達可能性が必須である。内省の結果を内省に再投入するループは、幻覚の混入、経験の模倣による誤り伝播、思考の退化を起こすことが複数研究で観察されている（ExpeL、Xiong et al. 2025、HaluMem、Huang et al. ICLR 2024）。キャラクター固有の観点では、**キャラ自身の過去発話・口調・約束・日付・感情の具体性**が要約で失われやすいリスクの筆頭になる（Zep の assistant 側想起低下、RoleLLM、REVERIEMEM、ProMem の事例）。したがって推奨する基盤は「追記専用の原文ログ」である。その上に、provenance付きの派生記憶（要約・事実・キー）、根拠リンク付きで改訂可能な内省、固定コア＋版管理された可変人格層、原文そのままの代表発話ストアを載せる。2026年の研究（PHASE-Tree、Emergi-PersonaOS、TierMem、Eywa）と実装（Zep/Graphiti、Letta MemFS、Mem0 2026）もこの方向に収束している。ただし注意点がある。**原文と要約を比較して、キャラの口調ドリフトや長期の内省ドリフトを直接測った研究は見つかっていない。** キャラ領域の結論は、事実想起ベンチマークや層別アブレーションからの外挿である。また論文ごとにベンチマーク・バックボーン・評価者が異なるため、**論文をまたいだ数値は直接比較できない**（以下の数値はすべて同一論文の同一表内での比較として読むこと）。

---

## 事実想起では原文が値として勝ち、要約は「キー」に回ると効く

長期会話記憶の主要ベンチマークで、**要約そのものを値として読ませる設計が勝った例はない**。LoCoMo（Maharana et al. 2024）は GPT-3.5-turbo-16k の RAG で検索単位を比べた。セッション要約は Recall@10 が **90.7%** と高いのに、QA F1 は **31.5** にとどまった。著者はこれを「対話から要約への変換時の情報損失」に帰している（[LoCoMo, arXiv 2402.17753](https://arxiv.org/html/2402.17753)）。一方、話者ごとに生成した短い「observation」（原子的な主張文）は、k=5 で F1 **41.4** と最良だった。特に時間的質問で強く、F1 は約41–42、生の対話は約21–26である。ただし単一ホップの詳細では、k を大きくすると原文対話が上回った（50.9 対 44.0）。マルチホップでは要約が最大 16.6 にとどまり、明確に弱い（同上）。つまり**「要約」と「原子的事実」は別物として扱うべき**である。

LongMemEval（Wu et al., ICLR 2025）はこの区別をさらに明確にした。値を要約や抽出ユーザー事実に置き換えると、情報損失で全体性能が下がる。例外は**マルチセッション集約**で、事実分解が効いた。最良の値粒度は「ラウンド」（ユーザー1ターン＋アシスタント1ターン）とされた（[LongMemEval](https://arxiv.org/html/2410.10813)）。決定的なのはキー拡張の表（LongMemEval-M）である。キー=事実のみ（R@10 0.654）やキー=値のみ（0.692）よりも、**値は原文ラウンドのまま、キーに抽出事実を足した構成（K=V+fact）**が R@10 **0.784**、GPT-4o QA@10 **0.720** で最良だった。セッション単位でも K=V+fact が R@10 **0.862** で最良である。逆にキーを要約のみにすると、セッション単位の QA@10 は 0.252 まで落ちた（[LongMemEval PDF](https://arxiv.org/pdf/2410.10813)）。著者自身も「圧縮形式を単独で使っても想起は改善しない」と述べている。

方法論文も同じ傾向を示す。SeCom（Pan et al., ICLR 2025）の同一表では、要約メモリ（SumMem 53.87 / 63.42、RecurSum 56.25 / 62.96）が LoCoMo / Long-MT-Bench+ の両方で最下位かそれに近い。Long-MT-Bench+ では生ターン（82.85–84.91）に約20ポイント負けた。最良は**原文の話題セグメントを保持し、検索時にだけ LLMLingua-2 で圧縮して雑音を除く**方式で、LoCoMo 71.57、Long-MT-Bench+ 88.81 だった（[SeCom](https://arxiv.org/html/2502.05589)）。2026年の単著プレプリント "Fidelity Before Structure"（Tao An）は、保存表現だけを変える統制比較を行った。逐語チャンクは LoCoMo **43.9%**・LongMemEval-S **67.4%**、抽出アーティファクトは **28.0% / 45.4%** で、グラフの有無・予算・検索方式を揃えても差は残った。チャンクは成功しアーティファクトは失敗した質問のうち、**69.0%** はどのアーティファクトにも該当事実が存在しなかった。両者の和集合（42.5% / 64.6%）は逐語のみとほぼ同等である（[An 2026, arXiv 2601.00821](https://arxiv.org/html/2601.00821v4)）。結論は「構造化記憶は逐語テキストを置き換えるのではなく補強すべき」というものだった。

第三者評価では、抽出専用システムの弱さが目立つ。MemoryAgentBench（Hu, Wang, McAuley, ICLR 2026）の Accurate Retrieval 平均では、生チャンクへの BM25 が **60.5**、Mem0 が **32.6**、Zep が **37.5** だった。EventQA では BM25 74.6 に対し Mem0 37.5 である（[MemoryAgentBench](https://arxiv.org/html/2507.05257v3)）。2026年のコスト性能分析（Pollertlam & Kornsuwannawit）では、Mem0 型のフラット事実メモリが LoCoMo **57.68** だった。全履歴ロングコンテキスト（GPT-5-mini）は **92.85** である（[arXiv 2603.04814](https://arxiv.org/html/2603.04814v1)）。ProMem（Yang et al. 2026）は、抽出記憶を**原文対話に照合し直す**ことで LongMemEval **69.57%** を得た。原文 RAG は 65.09%、Mem0 は 43.31% だった（[ProMem](https://arxiv.org/pdf/2601.04463)）。抽出は原文に再接地して初めて競争力を持つ。

反対方向の証拠も公平に示しておく。Mem0 の自社論文では、LoCoMo の J スコアが Mem0 66.88、生チャンク RAG の最良値 60.97 だった（フルコンテキストは 72.90）。生チャンク RAG はチャンクサイズと k によって 36.8–61.0 と大きく揺れた（[Mem0, arXiv 2504.19413](https://arxiv.org/html/2504.19413)）。ただしこれはベンダー自身の数値である。Zep 側が評価に異議を唱えたとされるが、その経緯は**未検証**である。Fidelity Before Structure でも、逐語チャンクは回答不能質問での棄権が弱かった（46.7%、ナイーブ RAG は 70.0%）（[An 2026](https://arxiv.org/html/2601.00821v4)）。原文主体の設計では「記憶にない」を明示的に扱う必要がある。コスト面では派生記憶が有利である。Mem0 は約1.8kトークンでフルコンテキスト約26kと比べてp95レイテンシ 17.1秒→1.44秒（[Mem0](https://arxiv.org/html/2504.19413)）。事実メモリは100kコンテキスト時に約10ターンでロングコンテキストより安くなる（[arXiv 2603.04814](https://arxiv.org/html/2603.04814v1)）。毎ターン常駐させる層は小さな派生プロフィールにし、原文は都度検索する。これが妥当な折衷になる。

## 要約で消えるのは、キャラにとって最も大事なもの

要約・抽出で失われる情報には再現性のあるパターンがある。キャラクター用途では、その中身が致命的になる。

第一に**日付と時間**。2026年の費用分析では、事実メモリに訪問日が保存されず「2回の訪問の間の日数」を計算できなかった（[arXiv 2603.04814](https://arxiv.org/html/2603.04814v1)）。LongMemEval でも、時間的質問を回収するには時間を考慮したクエリ拡張（再現率 +11.3%）が必要だった（[LongMemEval PDF](https://arxiv.org/pdf/2410.10813)）。第二に**正確な言い回し**。"type hints everywhere" が一般的な嗜好に丸められた例があり、合成プローブでは要約の完全一致 **14.0%** に対し逐語検索は **91.0%** だった（[An 2026](https://arxiv.org/html/2601.00821v4)）。約束・あだ名・内輪ネタを扱うキャラには直接響く。第三に**間接的な自己開示**。LongMemEval の予備調査は、Coze が「間接的に与えられたユーザー情報を記録し損ねる」こと、ChatGPT が「会話が進むと重要情報を上書きする」ことを報告した（[LongMemEval](https://arxiv.org/html/2410.10813)）。

第四が、キャラクター設計で最も重要な**アシスタント側（キャラ自身）の発話**である。Zep は LongMemEval の single-session-assistant カテゴリで、gpt-4o のフルコンテキスト **94.6%** から **80.4%** に落ちた（相対 −17.7%）（[Zep, arXiv 2501.13956](https://arxiv.org/html/2501.13956v1)）。ユーザー中心の事実抽出は、AI が何を言ったかを落とす。自分の過去の発言と矛盾しないことがキャラの一貫性の核心である以上、これは原文保持の最も直接的な根拠になる。第五に**感情・態度・文体**。ProMem の事例では、要約メモリが「洞察と精神的支えをくれた」という具体的な感情的事実を「キャリア成長に良い影響」と平板化した（[ProMem HTML](https://arxiv.org/html/2601.04463v1)）。EMem も、事実志向の抽出器では態度・文体情報が「過圧縮または脱落しうる」ことを自ら認めている（[EMem, arXiv 2511.17208](https://arxiv.org/html/2511.17208v1)）。

ただし反例もある。Zep の派生グラフは single-session-preference を **20.0%→56.7%**（gpt-4o）に改善した（[Zep](https://arxiv.org/html/2501.13956v1)）。事実メモリはペルソナ一貫性（安定した嗜好や習慣）の質問で競争力を保った（PersonaMem v2 で 62.48 対 69.75、[arXiv 2603.04814](https://arxiv.org/html/2603.04814v1)）。**抽象化は「安定した嗜好」には効き、「具体的な細部」には害がある。** そう読むのが整合的である。要約の誤り率については、再帰要約（Wang et al., Neurocomputing 2025）が最終セッションの記憶100件を人手で監査している。結果は捏造 **2.7%**・関係誤り **3.2%**・詳細欠落 **3.9%**、合計約 **9.8%** だった。ただし**反復による誤りの累積は測定されていない**（[arXiv 2308.15022](https://arxiv.org/html/2308.15022v3)）。要約の要約を重ねる「ローリング要約」は、最も証拠が乏しく、最もドリフトが予想される設計である。

## キャラの声は、説明文ではなく原文の実例で保たれる

ペルソナドリフトはよく記録されている。Li et al.（COLM 2024 とされるが会場は**未検証**）は、LLaMA2-chat-70B 同士の自己対話で「**8ラウンド以内**に有意な指示ドリフト」を報告した。エージェントが徐々に相手（模擬ユーザー）のペルソナを取り込む現象も観察している。原因はシステムプロンプトへの注意減衰という**仮説**である（[arXiv 2402.10962](https://arxiv.org/html/2402.10962v4)）。Anthropic らの "The Assistant Axis"（Lu et al. 2026、ミラー経由で確認）は、ドリフトの引き金を特定した。**モデル自身のプロセスについてのメタ内省を求める会話**、感情的に脆弱なユーザー、セラピー的・哲学的な会話である（[hyper.ai mirror of arXiv 2601.10387](https://hyper.ai/en/papers/2601.10387)）。これはコンパニオンの中核ユースケースそのものであり、**内省ループ自体がドリフトの圧力源になりうる**ことを意味する。活性化レベルの対策（persona vectors、activation capping）はオープンウェイトが前提である（[Anthropic: Persona Vectors](https://www.anthropic.com/research/persona-vectors)）。API 利用では、プロンプトか記憶による錨付けが唯一のレバーになる。

その記憶側の錨として、**原文の発話例**が効くという間接証拠が一貫している。RoleLLM（Wang Z.M. et al.）の GPT-4 判定では、勝率が zero-shot プロンプト 9.3%、few-shot プロンプト 29.8%、**対話形式のデモ 63.3%** だった。さらに、プロフィールから LLM が生成した Context-Instruct データは「話し方の特徴が薄い」と著者自身が観察している。**LLM が書き直した素材は口調を失う**という直接的な観察である（[RoleLLM, arXiv 2310.00746](https://arxiv.org/html/2310.00746v3)）。ただし、ノイズのある対話例を BM25 で検索しても知識スコアは改善しなかった。原文は文体には効くが万能ではない（同上）。REVERIEMEM（Tang et al. 2026）は層別アブレーションを行った。**例文抜粋を持つ人格層**を除くと、Character Fidelity の勝率が約93%から **58.3%** に落ちた。一方でエピソード層は一人称の場面*要約*で構成されている。つまり「出来事は要約、人格と声は原文抜粋」というハイブリッドが強い（[REVERIEMEM, arXiv 2606.25632](https://arxiv.org/pdf/2606.25632)）。ChatHaruhi は原作台詞を毎ターン検索してプロンプトに入れる設計である。根拠は、説明文だけでは文体が「基盤モデルに強く引きずられる」という点にある。ただし**定量的アブレーションはない**。この多数例方式は通常チャットの「10–20倍」のコストがかかる（[ChatHaruhi, arXiv 2308.09597](https://arxiv.org/pdf/2308.09597)、[GitHub](https://github.com/LC1332/Chat-Haruhi-Suzumiya)）。日本の AITuber 実装例（akakage, Zenn 2025）も、ユーザー/キャラの往復を原文チャンクのまま ChromaDB に保存し、上位3件をシステムプロンプトに入れている。要約は使っていない（[Zenn](https://zenn.dev/akakagerengoku/articles/a30793755289f4)）。

時間の扱いも見落とせない。TimeChara（Ahn et al., ACL 2024 Findings）は、物語上その時点では知らないはずのことをキャラが知ってしまう「時点ハルシネーション」が GPT-4o でも顕著であることを示した（[TimeChara](https://preview.aclanthology.org/setup/2024.findings-acl.197)）。成長するキャラでは、人格と知識の版を**時間インデックス付き**にしなければ、「成長」が「矛盾」に見えてしまう。

## 内省は原文に接地しないと、自分の誤りを増幅する

Generative Agents（Park et al. 2023。UIST 2023 とされるが、本調査の arXiv ページでは会場**未検証**）は、内省設計の原型である。重要度の累積が150を超えると内省が起動する（1日2–3回程度）。直近**100件の記録**（生の観察と最近の内省）から3つの問いを立て、それを検索クエリにする。検索結果には過去の内省も含まれる。洞察は「…（because of 1, 2, 8, 15）」の形式で出力され、**引用した記憶への参照付きで保存**される。内省が内省を引用するため、葉が生の観察である木構造ができる（[Generative Agents PDF](https://arxiv.org/pdf/2304.03442)）。TrueSkill のアブレーションでは、内省を除くと 29.89→**26.88** と、単一要素として最大の低下になった（同上）。ただしこれは約2ゲーム日の信憑性評価にすぎない。**内省そのものの正しさ、改訂、長期の忠実性は評価されていない**。引用された葉と照合して内省を再検証・撤回する仕組みもない。同じ論文は、Isabella が他者の提案を自分の関心として取り込んでいく現象も報告している。入力によって同一性が形作られる、ドリフトの一形態である。

内省を内省に重ねる危険を最も直接示すのは ExpeL（Zhao et al.、AAAI 2024 とされるが**未検証**）である。HotpotQA で、生の成功/失敗軌跡から抽出した洞察は **39%**、**内省を含めて抽出した洞察は 29%** だった。著者は内省が「時に幻覚を起こし抽出を誤導する」と説明している。同じ論文の別の比較では、洞察のみ 36%、生の軌跡検索のみ 31%、**両方 39%** だった。抽象と原文例の併用が最良である。ExpeL は投票（ADD/EDIT/UPVOTE/DOWNVOTE、初期値2で0になれば削除）により、新しい生の証拠で洞察を改訂する明示的な仕組みも備える（[ExpeL, arXiv 2308.10144](https://arxiv.org/pdf/2308.10144v3)）。

失敗モードは、現在では直接測定されている。Xiong et al.（2025）は、エージェントが検索された類似記録の出力をほぼ逐語的に模倣する「経験追従」を示した。保存された誤りは後のタスクで**複製・増幅**される。全件追加ポリシーでは 4,100件で 55.48 となり、固定100件の 67.53 を下回った。厳格な選択的追加は 70.95 で最良だった（[arXiv 2505.16067](https://arxiv.org/html/2505.16067v2)）。HaluMem（Chen et al. 2025）は、記憶システムが抽出・更新時に「幻覚を生成し蓄積する」ことを報告している（[arXiv 2511.03506](https://arxiv.org/abs/2511.03506v2)）。Huang et al.（ICLR 2024）は、外部フィードバックのない自己修正で「性能がかえって劣化する」ことを示した（[arXiv 2310.01798](https://arxiv.org/abs/2310.01798)）。MAR（2025、学生プロジェクトのため重みは低い）は、同一モデルが行為者・評価者・内省者を兼ねると内省が元の誤りを合理化することを報告した。内省が**別のタスク仕様を捏造した**事例もある（[arXiv 2512.20845](https://arxiv.org/html/2512.20845v1)）。キャラ用途では、生の会話に現れるユーザーの反応が、Huang et al. のいう「外部信号」に最も近い。これは、内省の入力に要約だけでなく原文ターンを含めるべき理由になる。

反対論も検討する。Think-in-Memory（Liu et al. 2023、要旨のみ確認）は、質問のたびに生履歴から推論し直すと結論が揺れる（"biased thoughts"）と論じる。そのうえで、**推論後の思考を保存する**ことで一貫性を得ると主張している（[arXiv 2311.08719](https://arxiv.org/abs/2311.08719)）。これは「内省結果を保存して安定した同一性を保つ」ことの正当化として妥当である。毎回原文から人格を再生成する設計は、かえって不安定になる。両立させる道は、**内省を保存しつつ原文へのリンクを必須にし、定期的に再検証する**ことである。それを実装しているのが、2026年の provenance 重視設計である。TierMem（Zhu et al., ICLR 2026 Workshop MemAgents）は、provenance 付き要約から**不変の生ログ**へ選択的にエスカレーションし、根拠が確認された結果だけを書き戻す。LoCoMo で 0.851 を記録した。常に原文に接地する構成は 0.873 で、これに迫りつつ入力トークンを −54.1%、レイテンシを −60.7% 削減した（[TierMem](https://iclr.cc/virtual/2026/10021250)）。Eywa（単著、要旨のみ確認）は「信念の前に証拠を」を掲げ、不変の証拠を先に保存してから事実を導出する（[arXiv 2605.30771](https://arxiv.org/abs/2605.30771)）。RMM（Tan et al., ACL 2025）は、内省で得た（話題要約キー、原文対話値）の組を記憶単位にする。粒度アブレーションでは Recall@5 **78** / 正解率 **49** となり、セッション単位の 69 / 34、ターン単位の 47 / 29 を上回った（[RMM PDF](https://arxiv.org/pdf/2503.08026)、[ACL Anthology](https://aclanthology.org/2025.acl-long.413)）。

認知科学の補完学習システム理論（Kumaran, Hassabis & McClelland 2016）は、あくまで**類推**として整合する。海馬は個別経験を速く学び、新皮質は構造化知識を遅く学ぶ。急な書き換えは既存知識を壊すため、経験を重要度で重み付けしてリプレイし、少しずつ統合する（[TICS 2016 PDF](https://web.stanford.edu/~jlmcc/papers/KumaranHassabisMcClelland16FinalMS.pdf)）。再固定化研究は、想起された記憶が不安定化して書き換わることを示した（[Nader et al. 2000, Nature](https://ideas.repec.org/a/nat/nature/v406y2000i6797d10.1038_35021052.html)）。A-MEM のように想起のたびに隣接ノートを上書きする設計は、これに近いリスクを持つ（[A-MEM, arXiv 2502.12110](https://arxiv.org/abs/2502.12110)。版履歴は記述されていない）。不変の原文ログは、人間にはない「改変されていない原本」をエージェントに与える。

## 成長は「固定コア＋証拠付き・審査付き・版管理された可変層」に収束しつつある

2026年の成長研究は、層構造で一致している。PHASE-Tree（Tang et al. 2026、HF Papers 経由で確認）は、**不変のアイデンティティ根**の下に persona / session / moment の可変層を置く。更新は変更のない特性に触れない局所的なフィールド編集で行う。LongEvoRoleBench では 12セル中12セルで外部ベースラインに勝った（[HF Papers 2608.06975](https://huggingface.co/papers/2608.06975)）。Emergi-PersonaOS（Fu et al. 2026、要旨確認）は、持続的な信念と現在の活性化状態を分離する。**信念の更新は追跡可能な証拠と版記録を伴う明示的な審査を通り、却下されうる**（[alphaXiv 2609.27417](https://www.alphaxiv.org/abs/2609.27417)）。ただし定量評価はない。

自律的成長の限界も実証されている。"Do AI Personas Grow?"（Wang M. et al. 2026）は、100ペルソナ×11ライフイベントで BFI-44 を測定した。変化は実在するものの、方向が人間の縦断研究と一致したのは **51.9%** にとどまり、退職の効果は全モデルで逆転した。ペルソナ間の分散は圧縮され（中央値 SD 0.19、人間は0.5–0.8）、無関係な3ターン後に方向を保ったのは **62.6–85.3%** だった。内省ステップの効果はモデル依存で、Qwen3.5-9B では変化を増やし、GPT-5.5 では減らした（[arXiv 2608.06485](https://arxiv.org/pdf/2608.06485)）。AutoPersonas（単著、内部は商業上非公開）は「自己ロック」を命名した。自己生成の人生が慣れた環境や停滞へ収束する現象で、40日ストレステストでは5日間の行動カテゴリ反復が 95.2–97.6% に達した（[alphaXiv 2607.08252](https://www.alphaxiv.org/abs/2607.08252)）。**内省だけでは成長は起きにくく、起きても平均回帰的で持続しにくい。** 外部イベント（実際の会話）を根拠にし、持続性を検査する仕組みが要る。

実装側もこの構造を持つ。Letta/MemGPT は、全メッセージを recall storage に保存し、退避時にも削除しない。そのうえで、コアメモリブロックと退避メッセージの再帰要約を書き換える（[MemGPT PDF](https://arxiv.org/pdf/2310.08560)、[Letta docs](https://docs.letta.com/guides/agents/memory)）。新しい MemFS では記憶を git 管理の Markdown とし、「すべての記憶編集をコミット」する。バックグラウンドの "dreaming" が最近の会話を記憶に統合し、適用前にレビューする設定もある（[Letta MemFS](https://docs.letta.com/concepts/memfs)、[Letta Memory & dreaming](https://docs.letta.com/configuration/memory)）。ただし MemFS のファイルには元メッセージへの逆参照がない。Zep/Graphiti は生エピソードを「非損失」の正本とする。事実エッジは `episodes`（ソースエピソード ID のリスト）、`valid_at`/`invalid_at`/`expired_at`、`reference_time` を持ち、矛盾は削除せず無効化する（[Zep paper](https://arxiv.org/html/2501.13956v1)、[graphiti edges.py](https://raw.githubusercontent.com/getzep/graphiti/main/graphiti_core/edges.py)）。Mem0 は2026年に、破壊的な UPDATE/DELETE から **ADD のみ**へ移行した。上書きや削除が文脈を壊したことが理由で、新旧の事実を並存させる（[Mem0 blog](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)。数値はベンダー自己申告）。Supermemory は updates / extends / derives の関係と `isLatest` で、過去を消さずに最新を引く（[Supermemory docs](https://supermemory.ai/docs/concepts/graph-memory)）。削除の意味論には差がある。Zep では、エピソードを削除しても無効化マークと共有ノード要約が残る（[Zep deletion](https://help.getzep.com/deleting-data-from-the-graph)）。Claude は既存チャットから記憶統合を再計算し、削除したチャットを24時間以内に除外する（[Claude Help](https://support.claude.com/en/articles/11817273)）。ChatGPT は保存メモリをチャットと切り離して管理する（[OpenAI Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)）。ESAA-Conversational（単著、自己事例のみ）は、追記専用ログを正本とし、要約類を決定的な射影に限定して「索引が正本になるのを防ぐ」ことを提案している（[arXiv 2606.23752](https://arxiv.org/pdf/2606.23752)）。

## 判定：原文はどこで、どの役割で必要か

| 観点 | 原文は重要か | 根拠の強さ | 要約・派生データの適所 |
|---|---|---|---|
| 長期記憶：事実・細部・日付の想起 | **必須（値として）** | 強い：複数ベンチマークの同一表内比較と統制比較（LongMemEval, SeCom, An 2026, MemoryAgentBench） | 検索キー、時間インデックス、マルチセッション集約 |
| 長期記憶：キャラ自身の過去発話 | **必須** | 中：Zep の assistant 側低下（単一研究） | なし（抽出対象から漏れやすい） |
| 長期記憶：安定した嗜好・習慣 | 補助的 | 中：Zep の preference 改善、PersonaMem v2 | **常駐プロフィールとして有効** |
| キャラの口調・声の一貫性 | **重要（代表発話の逐語例として）** | 間接：RoleLLM, REVERIEMEM, ChatHaruhi（原文と要約の直接比較はなし） | 出来事の一人称要約 |
| 内省の入力 | **重要（直近の原文＋検索した原文を含める）** | 中：ExpeL（内省混入で 39→29%）、Xiong et al., Huang et al. | 過去の内省は補助入力に限定 |
| 内省の根拠・検証 | **必須（引用先・再検証対象として）** | 中：Generative Agents の引用木、TierMem、RMM、Eywa | 内省自体は保存してよい（TiM の一貫性論） |
| 人格の成長（特性変更） | **必須（証拠として）** | 弱〜中：PHASE-Tree, Emergi-PersonaOS（設計提案中心） | 版管理された特性フィールド |

したがって基盤データは**原文**にすべきである。要約・事実は原文から再生成可能な派生物（射影）として扱い、正本にしてはならない。抽出プロンプトやモデルが改善されたときに派生層を作り直せることも、原文を正本にする実利である（推論。[LongMemEval](https://arxiv.org/pdf/2410.10813)、[ESAA-Conversational](https://arxiv.org/pdf/2606.23752) の設計思想と整合）。

## ドメインモデル案：エンティティ、関係、不変条件

以下は、上記の証拠から導いた**設計提案**である（研究で検証済みの構成そのものではない）。

### エンティティ

| 層 | エンティティ | 主要属性 | 主な根拠 |
|---|---|---|---|
| 正本 | **Turn（原文発話）** | turn_id, session_id, speaker（user / character / system）, text（逐語）, occurred_at, recorded_at, channel | LongMemEval のラウンド粒度、Letta recall、Zep Episode |
| 正本 | **Session** | session_id, started_at, ended_at, 参加者 | LoCoMo/LongMemEval のセッション構造 |
| 正本由来 | **Episode（エピソード／話題セグメント）** | episode_id, turn_span[], topic, occurred_at 範囲 | SeCom のセグメント、RMM の話題単位 |
| 派生 | **DerivedMemory（抽象型）** | id, kind（Summary / Fact / Observation / Key / ProfileEntry）, content, **source_turn_ids[]**, derivation{model, prompt_version, created_at}, valid_from, valid_to, recorded_at, superseded_at, is_latest | Zep の bi-temporal、Mem0 2026 の追記専用、EMem の出典ターン |
| 派生 | **MemoryRelation** | from, to, type（updates / extends / derives / contradicts） | Supermemory |
| 派生 | **CharacterProfile（常駐要約）** | ProfileEntry の集合、トークン上限 | Letta core block、Mem0 のコスト論 |
| 内省 | **Reflection** | reflection_id, insight, trigger（重要度閾値 / 定期 / イベント）, **evidence[]**（Turn / Episode / Reflection への参照と役割）, depth（最寄りの原文の葉までの距離）, status（candidate / active / contested / retired）, support_score, created_at | Generative Agents の引用木、ExpeL の投票 |
| 内省 | **ReflectionRevision** | reflection_id, version, 変更前後, 理由, 新しい根拠 turn_ids | ExpeL EDIT、Letta MemFS のコミット |
| 人格 | **CoreIdentity（固定コア）** | 名前・根本的価値観・禁止事項・世界設定。作者のリリースによってのみ版が上がる | PHASE-Tree の不変根 |
| 人格 | **PersonaTrait（可変層）** | trait_key, value, version, valid_from, valid_to, evidence_reflection_ids[], evidence_turn_ids[] | PHASE-Tree、Emergi-PersonaOS、TimeChara |
| 人格 | **TraitChangeProposal** | proposal_id, 対象 trait, 提案値, 根拠となる内省, 審査結果（approved / rejected）, reviewer（人 / 審査エージェント）, 持続性チェック結果 | Emergi-PersonaOS の審査・却下、"Do AI Personas Grow?" の持続性 |
| 人格 | **CharacterState（短期状態）** | mood, 関係温度, 現在の話題。セッション内で揮発 | PHASE-Tree の moment 層 |
| 声 | **VoiceExemplar（代表発話）** | exemplar_id, text（逐語）, source（canonical_seed / turn_id）, 状況・感情タグ, approved_by, approved_at, trait_version（どの人格版での発話か） | RoleLLM, ChatHaruhi, REVERIEMEM |
| 声 | **PinnedQuote（逐語固定）** | 約束・あだ名・記念日など、言い回し自体が重要な原文への参照 | An 2026 の言い回し損失、Character.AI のピン留め（二次情報） |

### 関係

Session は Turn を時系列で包含する。Episode は連続する Turn の区間を参照する。DerivedMemory は1つ以上の Turn（または Episode）から導出され、その逆引き（その Turn から何が導出されたか）も引ける。これは Zep の双方向インデックスに相当する。Reflection は Turn / Episode / DerivedMemory / 他の Reflection を根拠として引用し、木（DAG）を成す。TraitChangeProposal は Reflection と、その葉にある Turn を根拠に PersonaTrait の新版を提案する。承認されると、新しい PersonaTrait 版が valid_from 付きで作られ、旧版は valid_to で閉じる。VoiceExemplar は speaker=character の Turn か、作者が用意した正典台詞を参照し、どの人格版の時期の発話かを持つ。応答時の検索は、DerivedMemory（キー）で当たりを付け、対応する Turn 区間（値）を読み手に渡す。CharacterProfile と CoreIdentity は常駐させ、VoiceExemplar は状況に応じて few-shot として注入する。

### 不変条件

| # | 不変条件 | 根拠と意図 |
|---|---|---|
| I1 | Turn は**追記専用**である。内容の変更は禁止。削除はプライバシー要求によってのみ行い、削除時は I9 を発火する | 原文を正本にする（Letta、Zep、ESAA） |
| I2 | すべての DerivedMemory は空でない `source_turn_ids` と derivation メタデータを持ち、**原文から再生成可能**である | provenance と再構築（Zep、EMem、ProMem） |
| I3 | 原文と派生記憶が食い違うときは**原文が優先**し、派生側を無効化（superseded）する。派生記憶同士の矛盾は削除ではなく無効化で表現する | Zep の無効化、Mem0 2026 の追記専用 |
| I4 | 細部・日付・引用が問われる応答では、派生記憶は**キー**としてのみ使い、読み手には対応する原文区間を渡す | LongMemEval の K=V+fact、SeCom |
| I5 | speaker=character の Turn も抽出・索引の対象とし、ユーザー発話と同等に検索可能でなければならない | Zep の single-session-assistant 低下 |
| I6 | Reflection は、根拠をたどると**少なくとも1つの Turn（葉）**に到達しなければならない。他の Reflection だけを根拠とする内省は depth 上限を設け、status=candidate に留める | ExpeL（内省混入で性能低下）、Generative Agents の木構造 |
| I7 | Reflection は撤回・改訂でき、その履歴（ReflectionRevision）を保持する。新しい Turn が根拠と矛盾した場合は contested に遷移させる | ExpeL の投票、TierMem の検証済み書き戻し |
| I8 | CoreIdentity は内省・dreaming から**書き換え不可**とする。PersonaTrait の変更は TraitChangeProposal の承認を経た場合のみとし、承認には原文根拠と持続性チェックを要する | PHASE-Tree、Emergi-PersonaOS、"Do AI Personas Grow?" |
| I9 | Turn を削除したら、それを根拠にもつ DerivedMemory・Reflection・VoiceExemplar を**再計算または失効**させる（参照カウントだけで済ませない） | Zep の残留問題、Claude の再計算方式 |
| I10 | 時点 t での応答は、valid_from ≤ t の PersonaTrait 版・DerivedMemory だけを参照する（未来の知識・人格を漏らさない） | TimeChara、Zep の bi-temporal |
| I11 | VoiceExemplar の text は**逐語**である。LLM による言い換えや要約は禁止し、登録には承認を要する | RoleLLM の Context-Instruct 観察、REVERIEMEM |
| I12 | 記憶への追加にはゲートを設ける（重要度・検証）。自己生成物を無選別に全件追加しない | Xiong et al. の全件追加劣化 |

運用上の補足を加える。内省プロンプトは、メタ内省がドリフトの引き金になる（Assistant Axis）ことを踏まえて範囲を限定する。内省の入力には直近の原文ターンを必ず含め、ユーザー反応を外部信号として扱う。原文主体の検索は棄権が弱い（An 2026）ので、「記憶にない」を返す経路を読み手側に用意する。これらはいずれも上記研究からの推論である。

## 検証済みの知見、未検証の項目、そして証拠の空白

**本報告で検証済みとして扱った主な知見**は、ノート作成時に一次ソース（arXiv HTML/PDF、ACL Anthology、公式ドキュメント、コード）を実際に読んで確認されたものである。LoCoMo・LongMemEval・SeCom・Mem0・Zep・EMem・RMM・再帰要約・ProMem・MemoryAgentBench・An 2026・Pollertlam 2026 の数値、Generative Agents の内省手順とアブレーション、ExpeL、Xiong et al.、Li et al.、RoleLLM、REVERIEMEM、ChatHaruhi、"Do AI Personas Grow?"、TimeChara、Letta/Zep/Mem0/Supermemory/AITuberKit/Claude/ChatGPT の公式記述がこれに当たる。Letta/MemGPT の recall storage による全文保持は、一方のノートでは未検証とされていたが、別のノートで MemGPT 論文 PDF と Letta ドキュメントにより確認済みである。Assistant Axis は arXiv 本体ではなくミラーで、PHASE-Tree は HF Papers で、Emergi-PersonaOS と AutoPersonas は alphaXiv の要旨で確認した。CLIN、MemoryOS、TiM、AWM、ReasoningBank、HaluMem、Eywa も**要旨のみ**の確認である。

**ノートが未検証と明記している項目**は次のとおりで、本文では根拠に用いていないか、未検証と明示した。会場の帰属（LoCoMo の ACL 2024、Generative Agents の UIST 2023、Reflexion の NeurIPS 2023、ExpeL の AAAI 2024、A-MEM の NeurIPS 2025、MemoryOS の EMNLP 2025、Li et al. の COLM 2024、Character-LLM の EMNLP 2023 など）。MemoryBank が原文＋日次/全体要約＋人物像を保存するという構成。Mem0 と Zep の LoCoMo 評価をめぐる論争。MSC の要約メモリ対検索の数値。Shumailov et al.（Nature 2024）のモデル崩壊（取得失敗。そもそもパラメトリック学習の話でテキスト記憶への類推にすぎない）。Character.AI の Story Memory（二次情報のみ）。Mem0 OSS の `history()` API。Replika・Nomi・Kindroid・Neuro-sama の内部構造（一次情報なし）。単著・学生・ベンダーによる文献（An 2026、AgentMemBench、MAR、Eywa、AutoPersonas、ESAA-Conversational、Mem0 ブログ）は、査読や再現の点で重みを下げて読むべきである。REVERIEMEM では要旨（+34.6）と表（35.6）の数値に不一致がある。

**数値の比較可能性**についても注意する。LoCoMo の F1、Mem0 の J、SeCom の GPT4Score、EMem の正解率は、同じ LoCoMo でも指標・バックボーン・評価者がすべて異なる。たとえば「Zep 65.99（Mem0 論文）」と「Zep 0.616（EMem 論文）」は同じものを測っていない。本報告の数値比較はすべて**同一論文内**に限っている。

**証拠の空白**は次のとおりである。第一に、単一の永続キャラクターについて、原文ログ記憶と要約記憶を比較し、**口調・語彙（idiolect）の一貫性、感情履歴の忠実度、長期ドリフト**を測った統制実験は見つからなかった。声に関する結論は、デモ対説明（RoleLLM）、層アブレーション（REVERIEMEM）、設計論（ChatHaruhi）からの間接推論である。第二に、**原文に対する内省と要約に対する内省**を、他条件を揃えて長期の人格で比較した研究もない。最も近い代理指標は ExpeL の「内省混入」変種である。第三に、再帰要約の反復回数に伴う**誤り累積**と、内省木の深さに伴う誤り累積は、どちらも測定されていない。第四に、要約による**感情トーンの喪失**の定量研究はなく、ProMem の事例が1件あるのみである。第五に、根拠ポインタ付き洞察とポインタなし洞察の幻覚率を比べた実験もない。第六に、コンパニオン/AITuber 型の数か月にわたる感情的対話で、ハイブリッド設計を端から端まで評価した研究はない。日本語の学術文献（JSAI・言語処理学会など）も今回は見つかっていない。

## 結論

この調査で見えてきたのは、「原文か要約か」という二択が問いとして粗すぎるということである。正しい問いは「どの役割に何を使うか」になる。原文は**正本・読み手への値・内省の証拠・声の手本**という4つの役割を担う。要約・事実が担うのは**検索キー・時間軸・常駐プロフィール**である。この分担は、事実想起ベンチマークで最も強く支持されている。そして、キャラクターにとって最も重要な情報（自分の過去の発言、約束の言い回し、感情の具体性）ほど要約で失われやすい。だから、キャラ用途では一般的なアシスタント以上に原文保持の価値が高いと推論できる。

内省と成長について、文献が示す危険は「要約で忘れること」よりも「自分の生成物を根拠に自分を書き換え続けること」である。経験追従、幻覚の蓄積、自己ロック、平均回帰的な人格変化がその現れである。したがって、ドメインモデルの要点は記憶の形式より**根拠への到達可能性と書き込み権限の設計**にある。具体的には、内省が必ず原文の葉に着地すること、人格の変更が審査と版を経ること、削除が派生物まで波及することである。今後は、この設計の上で原文と要約の口調ドリフトを自前で比較測定できるよう、VoiceExemplar と PersonaTrait 版を評価用の基準点として初期から記録しておくことを勧める。文献の空白を、自分のログで埋められる構造にしておくことが最大の実利になる。

---

## 参考文献

### 長期会話記憶のベンチマークと評価
1. Maharana, A., Lee, D.-H., Tulyakov, S., Bansal, M., Barbieri, F., Fang, Y. "Evaluating Very Long-Term Conversational Memory of LLM Agents" (LoCoMo). arXiv 2402.17753, 2024（ACL 2024 とされるが会場未検証）. https://arxiv.org/html/2402.17753
2. Wu, D., Wang, H., Yu, W., Zhang, Y., Chang, K.-W., Yu, D. "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory." ICLR 2025, arXiv 2410.10813. https://arxiv.org/html/2410.10813 / https://arxiv.org/pdf/2410.10813
3. Xu, J., Szlam, A., Weston, J. "Beyond Goldfish Memory: Long-Term Open-Domain Conversation" (MSC). ACL 2022. https://parl.ai/projects/msc/ / https://preview.aclanthology.org/setup/2022.acl-long.356
4. Hu, Y., Wang, Y., McAuley, J. "Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions" (MemoryAgentBench). arXiv 2507.05257 v3, ICLR 2026 poster. https://arxiv.org/html/2507.05257v3 / https://iclr.cc/virtual/2026/poster/10010781
5. Pollertlam, N., Kornsuwannawit, W. "Beyond the Context Window: A Cost-Performance Analysis of Fact-Based Memory vs. Long-Context LLMs for Persistent Agents." arXiv 2603.04814, 2026. https://arxiv.org/html/2603.04814v1
6. An, T. "Fidelity Before Structure: Verbatim Chunks Beat Lossy Artifact Extraction in Long-Conversation LLM Memory." arXiv 2601.00821 v4, 2026（単著・会場なし）. https://arxiv.org/html/2601.00821v4
7. Chen, D. et al. "HaluMem: Evaluating Hallucinations in Memory Systems of Agents." arXiv 2511.03506, 2025（要旨確認）. https://arxiv.org/abs/2511.03506v2
8. Cherif, A. "AgentMemBench: A Systematic Benchmark for Evaluating Long-Term Memory Management Strategies in Conversational AI Agents." arXiv 2608.00009, 2026（単著）. https://arxiv.org/pdf/2608.00009

### 記憶の方法論
9. Pan, Z. et al. "On Memory Construction and Retrieval for Personalized Conversational Agents" (SeCom). ICLR 2025, arXiv 2502.05589. https://arxiv.org/html/2502.05589 / https://iclr.cc/virtual/2025/poster/27790
10. Chhikara, P., Khant, D., Aryan, S., Singh, T., Yadav, D. "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory." arXiv 2504.19413, 2025. https://arxiv.org/html/2504.19413
11. Rasmussen, P., Paliychuk, P., Beauvais, T., Ryan, J., Chalef, D. "Zep: A Temporal Knowledge Graph Architecture for Agent Memory." arXiv 2501.13956, 2025. https://arxiv.org/html/2501.13956v1
12. Zhou, S. "A Simple Yet Strong Baseline for Long-Term Conversational Memory of LLM Agents" (EMem). arXiv 2511.17208, 2025. https://arxiv.org/html/2511.17208v1
13. Tan, Z. et al. "In Prospect and Retrospect: Reflective Memory Management for Long-term Personalized Dialogue Agents" (RMM). ACL 2025 long, arXiv 2503.08026. https://arxiv.org/pdf/2503.08026 / https://aclanthology.org/2025.acl-long.413
14. Wang, Q., Fu, Y., Cao, Y., Wang, S., Tian, Z., Ding, L. "Recursively Summarizing Enables Long-Term Dialogue Memory in Large Language Models." Neurocomputing 2025, arXiv 2308.15022. https://arxiv.org/html/2308.15022v3
15. Yang, C., Sun, Z., Wei, W., Hu, W. "Beyond Static Summarization: Proactive Memory Extraction for LLM Agents" (ProMem). arXiv 2601.04463, 2026. https://arxiv.org/pdf/2601.04463 / https://arxiv.org/html/2601.04463v1
16. Ma, W., Nan, J., Wu, W., Chen, Y. "Nemori." arXiv 2508.03341, 2025（要旨確認）. https://arxiv.org/abs/2508.03341v2
17. Packer, C. et al. "MemGPT: Towards LLMs as Operating Systems." arXiv 2310.08560, 2023. https://arxiv.org/pdf/2310.08560
18. Xu, W. et al. "A-MEM: Agentic Memory for LLM Agents." arXiv 2502.12110, 2025. https://arxiv.org/abs/2502.12110
19. Kang, J., Ji, M., Zhao, Z., Bai, T. "Memory OS of AI Agent" (MemoryOS). arXiv 2506.06326, 2025. https://arxiv.org/html/2506.06326v1
20. Liu, L. et al. "Think-in-Memory." arXiv 2311.08719, 2023（要旨確認）. https://arxiv.org/abs/2311.08719
21. "MIRIX." arXiv 2507.07957, 2025. https://arxiv.org/html/2507.07957v1
22. Zhu, Q., Chen, S., Yu, R., Wu, Z., Wang, B. "From Lossy to Verified: A Provenance-Aware Tiered Memory for Agents" (TierMem). ICLR 2026 Workshop MemAgents. https://iclr.cc/virtual/2026/10021250
23. Joshi, R. "Eywa: Provenance-Grounded Long-Term Memory for AI Agents." arXiv 2605.30771, 2026（単著・要旨確認）. https://arxiv.org/abs/2605.30771
24. "ESAA-Conversational." arXiv 2606.23752, 2026（単著）. https://arxiv.org/pdf/2606.23752

### 内省・自己改善と失敗モード
25. Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., Bernstein, M. S. "Generative Agents: Interactive Simulacra of Human Behavior." arXiv 2304.03442, 2023（UIST 2023 とされるが未検証）. https://arxiv.org/pdf/2304.03442 / https://arxiv.org/abs/2304.03442
26. Shinn, N. et al. "Reflexion: Language Agents with Verbal Reinforcement Learning." arXiv 2303.11366, 2023. https://arxiv.org/pdf/2303.11366
27. Zhao, A. et al. "ExpeL: LLM Agents Are Experiential Learners." arXiv 2308.10144 v3. https://arxiv.org/pdf/2308.10144v3
28. Majumder, B. P. et al. "CLIN." arXiv 2310.10134, 2023（要旨確認）. https://arxiv.org/abs/2310.10134
29. Ouyang, S. et al. "ReasoningBank." arXiv 2509.25140（要旨確認）. https://arxiv.org/abs/2509.25140
30. Wang, Z. Z., Mao, J., Fried, D., Neubig, G. "Agent Workflow Memory." arXiv 2409.07429（要旨確認）. https://arxiv.org/abs/2409.07429
31. Xiong, Z. et al. "How Memory Management Impacts LLM Agents: An Empirical Study of Experience-Following Behavior." arXiv 2505.16067 v2, 2025. https://arxiv.org/html/2505.16067v2
32. Huang, J. et al. "Large Language Models Cannot Self-Correct Reasoning Yet." ICLR 2024, arXiv 2310.01798. https://arxiv.org/abs/2310.01798
33. Ozer et al. "MAR: Multi-Agent Reflexion Improves Reasoning Abilities in LLMs." arXiv 2512.20845, 2025（学生プロジェクト）. https://arxiv.org/html/2512.20845v1
34. Huang et al. "A Survey of Agent Memory in the Second Half." arXiv 2602.06052（要旨確認）. https://arxiv.org/abs/2602.06052
35. Du, P. "Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers." arXiv 2603.07670（要旨確認）. https://arxiv.org/abs/2603.07670

### ペルソナ一貫性・キャラクター・成長
36. Li, K., Liu, T., Bashkansky, N., Bau, D., Viégas, F., Pfister, H., Wattenberg, M. "Measuring and Controlling Instruction (In)Stability in Language Model Dialogs." arXiv 2402.10962 v4, 2024（COLM 2024 とされるが未検証）. https://arxiv.org/html/2402.10962v4
37. Chen, R. et al. "Persona Vectors: Monitoring and Controlling Character Traits in Language Models." arXiv 2507.21509; Anthropic, 2025. https://www.anthropic.com/research/persona-vectors / https://arxiv.org/abs/2507.21509
38. Lu, C., Gallagher, J., Michala, J., Fish, K., Lindsey, J. "The Assistant Axis: Situating and Stabilizing the Default Persona of Language Models." arXiv 2601.10387, 2026（ミラーで確認）. https://hyper.ai/en/papers/2601.10387
39. Samuel, V. et al. "PersonaGym: Evaluating Persona Agents and LLMs." EMNLP Findings 2025, arXiv 2407.18416. https://arxiv.org/abs/2407.18416
40. Wang, X. et al. "InCharacter." ACL 2024 long. https://aclanthology.org/2024.acl-long.102
41. Tu, Q. et al. "CharacterEval." ACL 2024 long. https://aclanthology.org/2024.acl-long.638
42. Wang, Z. M. et al. "RoleLLM: Benchmarking, Eliciting, and Enhancing Role-Playing Abilities of LLMs." arXiv 2310.00746. https://arxiv.org/html/2310.00746v3
43. Ahn, J. et al. "TimeChara: Evaluating Point-in-Time Character Hallucination of Role-Playing LLMs." ACL 2024 Findings. https://preview.aclanthology.org/setup/2024.findings-acl.197
44. Li, C. et al. "ChatHaruhi: Reviving Anime Character in Reality via Large Language Model." arXiv 2308.09597, 2023. https://arxiv.org/pdf/2308.09597 / https://github.com/LC1332/Chat-Haruhi-Suzumiya
45. Shao, Y., Li, L., Dai, J., Qiu, X. "Character-LLM: A Trainable Agent for Role-Playing." arXiv 2310.10158. https://arxiv.org/abs/2310.10158
46. Zhong, W. et al. "MemoryBank: Enhancing Large Language Models with Long-Term Memory." arXiv 2305.10250. https://arxiv.org/abs/2305.10250
47. Tang, X. et al. "Staying In Character: Perspective-Bounded Memory for Book-Based Role-Playing Agents" (REVERIEMEM). arXiv 2606.25632, 2026. https://arxiv.org/pdf/2606.25632
48. Wang, M. et al. "Do AI Personas Grow? Analyzing and Benchmarking Personality Evolution in LLM Agents After Life Events." arXiv 2608.06485, 2026. https://arxiv.org/pdf/2608.06485
49. Tang, B. et al. "PHASE-Tree: Modeling Character-State Evolution in Long-Horizon Role-Playing Dialogue." arXiv 2608.06975, 2026. https://huggingface.co/papers/2608.06975
50. Fu, H. et al. "Emergi-PersonaOS: A Persona Agent Operating System for Situational Adaptation and Controllable Evolution." arXiv 2609.27417, 2026. https://www.alphaxiv.org/abs/2609.27417
51. Li, M. "AutoPersonas: A Multi-Timescale Loop Engine for Open-Ended Persona Evolution." arXiv 2607.08252, 2026（単著）. https://www.alphaxiv.org/abs/2607.08252

### 認知科学（類推）
52. Kumaran, D., Hassabis, D., McClelland, J. L. "What Learning Systems do Intelligent Agents Need? Complementary Learning Systems Theory Updated." Trends in Cognitive Sciences 20(7), 2016. https://web.stanford.edu/~jlmcc/papers/KumaranHassabisMcClelland16FinalMS.pdf
53. Nader, K., Schafe, G. E., LeDoux, J. E. "Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval." Nature 406, 2000. https://ideas.repec.org/a/nat/nature/v406y2000i6797d10.1038_35021052.html

### 実装・製品ドキュメント
54. Letta docs: Memory. https://docs.letta.com/guides/agents/memory
55. Letta docs: Sleep-time agents. https://docs.letta.com/guides/agents/architectures/sleeptime/index.md
56. Letta docs: MemFS. https://docs.letta.com/concepts/memfs
57. Letta docs: Memory & dreaming. https://docs.letta.com/configuration/memory
58. getzep/graphiti, graphiti_core/edges.py. https://raw.githubusercontent.com/getzep/graphiti/main/graphiti_core/edges.py
59. Graphiti docs: Adding Episodes / Overview. https://help.getzep.com/graphiti/core-concepts/adding-episodes / https://help.getzep.com/graphiti/getting-started/overview
60. Zep: Deleting Data from the Graph. https://help.getzep.com/deleting-data-from-the-graph
61. Mem0 blog: the token-efficient memory algorithm (2026). https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm
62. Mem0 docs: Add memory. https://docs.mem0.ai/core-concepts/memory-operations/add
63. LangMem conceptual guide. https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
64. Cognee docs overview. https://docs.cognee.ai/core-concepts/overview
65. memodb-io/memobase README. https://github.com/memodb-io/memobase
66. Supermemory docs: Graph memory. https://supermemory.ai/docs/concepts/graph-memory
67. OpenAI Help: Memory FAQ. https://help.openai.com/en/articles/8590148-memory-faq
68. Claude Help Center: How does Claude's memory work. https://support.claude.com/en/articles/11817273
69. AITuberKit docs: Memory settings. https://docs.aituberkit.com/en/guide/memory-settings
70. akakage「長期記憶システム（RAG）実装」Zenn, 2025-09-06. https://zenn.dev/akakagerengoku/articles/a30793755289f4
71. RoboRhythms: Character AI Story Memory（二次情報・未検証）. https://www.roborhythms.com/character-ai-adds-chat-memories/
