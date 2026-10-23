# Core 向け Python HTTP クライアント

`digital_souls_memory.client.MemoryClient` は同期 HTTP クライアント。
DB、検索、抽出、知覚・配達判断には直接アクセスしない。
業務 DTO は `digital_souls_memory.client.models` から import する。

```python
from digital_souls_memory.client import MemoryClient
from digital_souls_memory.client.models import RecallRequest

with MemoryClient(base_url="http://127.0.0.1:8000", timeout=30.0) as memory:
    response = memory.recall("char-x", RecallRequest(
        session_id="session-1", format="materials", turn_id="turn-1",
    ))
```

Session の reports_delivery は必須の厳密 bool。
Turn の kind、perceived_by、addressees、address_mode は必須。
宛先と知覚を独立して渡し、旧 participants / audience は拒否する。
登録キャラ、本人知覚、thought、宛先と mode の組合せ、返信先の存在はサーバーが検証する。
本文を strip・翻訳・Unicode 正規化しない。register は JSON 上も register として運ぶ。
occurred_at とサーバー生成の recorded_at を区別する。

Session / Turn / Receipt、人格初期登録、声・アンカー登録、想起の materials / assembled、
常駐取得、作者削除を公開メソッドで提供する。
ST から使う記憶一覧・抽出状態・Profile 設定・短期状態取得も HTTP 経由。
返却値は Pydantic 型。配列は最後まで受信・検証してから返し、途中受信を部分成功にしない。

HTTP 非成功は `MemoryHTTPError` の status_code と detail で識別する。
detail は文字列と検証エラー配列の両方を保持する。
timeout は `MemoryTimeoutError`、その他の通信失敗は `MemoryTransportError`、
応答 JSON・型の違反は `MemoryResponseError`。
例外の文字列表現にヘッダー・資格情報・応答 detail を載せない。
detail 自体をログへ出す場合は呼出し側が機微情報を確認する。

timeout は正の有限秒で明示し、httpx の connect/read/write/pool へ渡す。
総時間の400ms打切りは実装しない。自動再試行・失敗時の空成功はない。
書込み再試行が必要なら呼出し側で公開 API の重複規則を考慮して明示実行する。
client は HTTP client と transport を所有し、with の正常・例外終了または close で閉じる。
複数呼出しで同じ client を再利用できる。

author_token は delete 操作だけに付け、通常登録や想起へ付けない。
redirect は追従せず、環境の proxy 設定は継承しない。
