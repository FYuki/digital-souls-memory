# リポジトリ内の作業指示

変更前に [CONTRIBUTING.md](CONTRIBUTING.md) を読んでください。
開発規約の正本は CONTRIBUTING で、このファイルはエージェント向けの入口です。

- 隔離した worktree と作業ブランチで、対応する `epic/*` 宛ての Draft PR を作成します。
- 作業 PR → epic は最終 head の CI、epic → main は最終 head の CI と実際の CodeRabbit レビューが必要です。merge 権限は依頼で確認します。
- TDD を守り、依頼された実装範囲を超えないでください。
- 合成データだけを使い、実モデル・実環境の実行権限をこの文書から推定しません。
- FAIL・SKIP・NOT RUN は PASS ではありません。結果を区別して報告してください。
