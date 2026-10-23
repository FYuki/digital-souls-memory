# 作者削除API

Coreから、作者専用のBearer credentialを付けて呼び出す。
`MEMORY_AUTHOR_ID`と`MEMORY_AUTHOR_TOKEN`をサーバー側で設定する。
`MEMORY_DELETION_DATABASE_URL`は削除専用接続であり、通常接続への代替はない。
未設定でもアプリと`/health`は起動する。削除入口の利用時に503を返す。
アプリへのAuthorizer・DeletionStore・RecalculationGatewayの注入を優先する。

`POST /deletion-requests`の本文は、`request_id`と次のどちらか一方。

- `target_turn_ids`: 非空のTurn ID配列。重複を除去し、整列する。
- `user_id`: 非空の利用者ID。actor一致またはaddresseesへの包含だけで対象を選ぶ。

本文の`requested_by`を含む未知のフィールドは拒否する。要求者は認証結果から記録する。
observationも同じ条件で選ぶ。本文内のJSON、知覚、返信、Session、在席は選択条件にならない。
不在IDは対象に数えず、対象なしは空の成功結果となる。

`GET /deletion-requests/{request_id}`も作者認証を要求する。
応答にはcommand、認証済みrequested_by、固定したtarget_turn_ids、status、cascade_result、
固定分類のerror、requested_at、completed_atを含める。監査へ本文やcredentialを保存しない。

成功は200、入力不正は422、認証拒否は401、要求衝突・実行中・snapshot競合は409、
要求不在は404、設定・依存障害は503。同じrequest_idで選択条件や要求者を変更できない。
状態はrunning・done・failed。doneの再要求は保存済み結果を返し、対象を再選択しない。
failedまたは5分のleaseが切れたrunningは、新しいtokenで固定対象を再試行する。
古いtokenによる確定と失敗更新は現在の試行へ反映しない。

残存原文を知覚・配達範囲内で読み、機微材料はLocalOnly、許可ラベルだけはCloudInputへ渡す。
支えのないコピーは除去し、支えがあれば再計算gatewayを要求する。
gatewayのNoneは失効だが、例外や未設定は削除成功にならない。
記憶ID・owner・種類・公開範囲・source_typeを維持し、Tier昇格と原文の付け替えを拒否する。
独立根拠数は残存出典から再計数する。人格・気質・権限を更新しない。
返信先などの抽出入力依存からも保存成果物を特定し、実際に適用された強化・更新・
矛盾の射影効果を再計算へ接続する。返信Turn自体を削除対象へ追加しない。
本文中の参照風文字列は対象選択に使わない。
全コピーを検証したうえで、再計算後のprojectionを後続射影のbaselineへ確定する。
旧baselineの別結果で、その最終内容・出典・使用範囲を上書きしない。
時間属性は元baselineと存続する更新・矛盾・再強化から再構成し、削除した効果は残さない。
再計算後のbaselineには最終状態に加え、元baselineの時間属性も区別して保持する。
提供記録の記憶・アンカー・代表発話は、提供時に記録した種別・所有者・IDで照合する。
共有記憶の所有者を提供先から推測せず、queryや本文の参照風文字列は解析しない。
索引の差替えと文書撤去では、他文書から参照されなくなったdigestの埋め込みを回収する。

モデル呼出しはtransaction外で実行し、確定時にsnapshotと処理権を再検証する。
再計算・直接参照処理・原文削除・成功監査は同一transactionで確定する。
snapshotの競合照合は全対象表・全列を安定順序で走査し、64行ずつ逐次hashへ渡す。
影響外の更新でも安全側に409となることがある。
残存Turnの返信参照だけをNULLにし、通常UPDATE/DELETE禁止は維持する。
専用接続にも保護対象を直接変更する権限経路を与えない。

配備時に管理接続で0009を適用し、専用ログインへ`memory_deletion_executor`を付与する。
専用ログインには必要なアプリ表のDMLとschema usageを設定する。
要求表のINSERT・UPDATE・DELETEは、DML権限があっても通常ロールでは拒否する。
書込みは削除専用ロールのメンバーとmigration管理者に限定する。
確定時にはcommand・認証済み要求者・初回固定対象と現在のattemptの一致も検証する。
`memory_deletion_owner`はNOLOGINで、アプリ接続へmembershipを付与しない。
限定関数は固定search_pathとschema修飾を使い、検証済み要求とtokenの対象だけを削除する。
起動時の自動DDLは行わない。

0009は抽出本文コピーをハッシュ・使用範囲へ移行し、三payloadの出典を順序付き表へ分離する。
派生内容は保持し、本文中の参照風文字列は解析しない。
downgradeは残存Turnから使用範囲を復元し、ハッシュ不一致なら全体を失敗させる。
運用DBの破壊的downgradeや既存Coreデータの移行は本作業の対象外。

Phase 1ではsource_turn_idsとProvisionRecordなどの直接参照を処理する。
ProvenanceGraph、内省、Belief、人格版、代表発話全体までの完全連鎖は実装対象外。
Sessionで選択済みの人格版は保持する。利用者本人・視聴者の削除入口は提供しない。

0011はactor種別台帳、baselineの元の時間属性、所有者付き提供参照を追加する。
既存の合成提供記録の所有者は保存済み候補参照・ProfileEntry・代表発話から照合する。
一意に復元できない場合はmigration全体を取り消し、提供先による推測補完はしない。
移行時に過去の未参照埋め込みも回収する。upgrade・downgradeは合成DBで検証する。
