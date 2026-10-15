# クラウド側の構成（M2: Gateway と Web/API）

デバイスとの通信は `PROTOCOL.md` が正。ここはクラウドの内側（サービス間の API、保存先、権限）を決める。

## 1. サービスと権限

| サービス | Cloud Run 名 | サービスアカウント | 持つもの | 持たないもの |
|---|---|---|---|---|
| Gateway | `mugemuge-gateway` | `mugemuge-gateway@` | デバイスの WebSocket、Firestore の読み書き | 署名鍵 |
| Web/API | `mugemuge-web` | `mugemuge-web@` | **署名鍵（Secret Manager）**、ログイン、承認カード、Firestore | 非常停止・その解除・記録の停止以外の exec |
| ビルド | — | `mugemuge-build@` | `roles/run.builder`（Cloud Build でイメージを作るだけ） | 実行時の権限 |
| 仮想デバイス（M4） | `mugemuge-virtual` | `mugemuge-virtual@` | 模擬ハードのデーモン（公開の Gateway に wss でつなぐだけ） | ロールなし |
| Agent（M3） | `mugemuge-agent` | `mugemuge-agent@` | Gateway と Web/API を呼ぶ権限 | **署名鍵（Secret Manager の権限そのもの）** |

- 署名鍵の Secret（`mugemuge-approval-key`）に `roles/secretmanager.secretAccessor` を付けるのは `mugemuge-web@` だけ。プロジェクト全体に付けない
- Gateway は `--allow-unauthenticated`（デバイスは IAM の ID トークンを持てない）。そのかわり `/v1/device` はデバイストークン、`/internal/*` はアプリ内で Google の ID トークンを検証する（audience＝Gateway の URL、呼び出し元は `INTERNAL_CALLERS` に列挙した SA のみ）
- **Web/API が `/internal/exec` で送れる op は `sys.estop`・`sys.estop_clear`・`log.stop` と、テスター画面の `i2c.read`・`gpio.read` だけ。承認つきで送れるのは `sys.estop_clear` だけ**（2026-10-15、SPEC_FIXES_3.md P2。読み出しの2つと承認の制限は、P2 のデプロイでテスター画面が読めなくなったため同日に追加）。読み出しは承認を付けられないので、デバイスが自動で許可するもの（バインド済みの read、`write_allow_regs` のコマンド）しか実行されない。16 ビットの reg の `i2c.read` はデバイスが書き込みとして判定して承認を求めるが、Web/API はそこにグラントを付けて送り直せない。Web/API は署名鍵を持つので、署名と送信の両方を持つと、侵害されたときに承認が要る操作を単独で実行できる。何でも送れるのは `EXEC_FULL_CALLERS` に列挙した SA（Agent）だけで、それ以外の呼び出し元から上の3つ以外が来たら Gateway が 403（`op_not_allowed_for_caller`）。`EXEC_FULL_CALLERS` が空なら、誰も何でもは送れない（安全側）。`sys.estop_clear` は承認つきで送れるが、承認を作るのはデバイス自身の request からだけ（4章）。`log.stop` は安全側の操作（PROTOCOL.md 0章の6）で、画面の「記録を止める」ボタンが使う
- Web/API も `--allow-unauthenticated`（人がブラウザで開く）。画面はログイン必須、Agent 向けの `/api/*` は ID トークンで検証する
- ヘルスチェックは `/health`（Cloud Run では `z` で終わるパス、たとえば `/healthz` は予約されていてアプリに届かない）
- 認証なしの開発モード（`MUGEMUGE_DEV_INSECURE=1`）は、Cloud Run 上（`K_SERVICE` がある）では起動を拒否する

## 2. Gateway の内部 API

### POST /internal/exec

Agent（と Web/API の非常停止ボタン）がデバイスに exec を送る。デバイスの `result` が返るか、`deadline_ms`＋5秒が切れるまで応答を保留する。

```json
{"device_id": "pi-desk-01", "op": "i2c.read", "args": {"bus": 1, "addr": 64, "reg": 254, "len": 2},
 "run_id": "run_…", "step_id": "stp_0004", "id": "x-…", "traceparent": "00-…-01",
 "deadline_ms": 2000, "approval": null}
```

- `id` は省略可（Gateway が採番）。通信断のあとで**同じ `id`** で再送すれば、デバイスが前回の結果を返す（二重実行しない）
- 応答 200: デバイスの `result` メッセージそのもの（`ok`, `data` / `error`, `policy`, …）
- 応答 503 `{"error": "device_offline"}`、504 `{"error": "device_timeout", "id": "x-…"}`（同じ `id` で再送してよい）
- Gateway は exec の中身を判定しない（判定はデバイス）。形式だけ `protocol.schema.json` で検査し、違反は 400

### GET /internal/devices/{device_id}

`{"device_id", "online", "session_id", "manifest", "manifest_sha256", "state", "last_seen_ms"}`。Agent のツール説明の生成（M3）と UI に使う。

### GET /internal/devices/{device_id}/samples/{job_id}

`{"rows": [{"t": 0, "v": ["0a14", null]}, …], "batches", "missing_seq", "errors"}`。生データのまま返す（換算は Agent のデコーダ）。Agent に Firestore の権限を持たせないための口。

### GET /internal/devices/{device_id}/events?limit=50

新しい順の event（`job_end`、`output_reverted`、`deadman_tripped` など）。

## 3. デバイス向け（PROTOCOL.md 2章の実装）

- `GET /v1/device`（WebSocket、`Sec-WebSocket-Protocol: mugemuge.v1`、`Authorization: Bearer mgd_…`）
  - トークンの sha256 で `devices` を引き、`hello.device_id` と一致しなければ `bye device_mismatch`
  - `hello.manifest_sha256` が `manifest` の正規化 JSON の sha256 と一致しなければ切る
  - 同じデバイスが張り直したら古い接続を閉じる。`session_id` は接続ごとに新しく払い出す
  - 心拍の `ping` は3秒ごと。`samples` と `event` は Firestore に生のまま書く
- `POST /v1/pair` `{"code": "123456", "device_id": "pi-desk-01"}` → `{"token": "mgd_…"}`
  - コードは Web/API が発行（6桁、10分、1回限り）。失敗は1分あたり全体で20回まで（総当たり対策）
  - トークンは 32 バイトの乱数。保存は sha256 だけ。再ペアリングで古いトークンは無効になる

## 4. Web/API

### 画面（ログイン必須）

| パス | 中身 |
|---|---|
| `GET /` | 承認待ちのカード（デバイスが返した `request` をそのまま表示: `summary`、`op`、`args`、`rule`、デバイス、セッション）と、デバイス一覧（オンライン状態、非常停止ボタン） |
| `POST /approvals/{id}/approve` / `deny` | 承認すると `approval.sign_grant` で署名し、グラントを保存する |
| `POST /devices/pair-code` | ペアリングコードを発行する |
| `POST /devices/{id}/estop` | Gateway に `sys.estop` を送る（安全側なので確認なしで通る） |

- ログインは ID とパスワード。利用者は Secret `mugemuge-users`（`{"id": "scrypt$…"}` の JSON）から読む。審査員用のテストアカウントもここに入れる
- セッションは HMAC 署名つきの Cookie（`Secure`、`HttpOnly`、`SameSite=Lax`、12時間）。フォームには CSRF トークンを入れる
- 承認は**デバイスが返した `request` の `args` に対して**署名する（クラウドで引数を言い換えない）。署名前に `args_sha256` を計算し直し、`request.args_sha256` と一致しなければ署名しない

### Agent 向け API（ID トークン）

- `POST /api/approvals` — body はデバイスが返した `error.request` に `run_id` を足したもの → `{"approval_id": "apr_…", "status": "pending"}`
  - Web/API は、Gateway が記録した「デバイス自身が返した request」（`devices/{id}/requests/{session_id}_{args_sha256}`）と照合し、なければ 400。承認カードの表示にもこの記録を使う。侵害された Agent が `summary` を偽って（例: 中身はリレー ON なのに「センサを読む」と書いて）人に承認させることはできない
  - `session_id` がデバイスの今のセッションと違えば 409（古いセッションのグラントはデバイスが拒否するため）
- `GET /api/approvals/{id}` → `{"status": "pending" | "approved" | "denied" | "expired", "reason"?: "session_ended", "grant": {…}}`
  - Agent はこれを待ち（M3 では ADK の長時間実行ツール）、`approved` ならグラントを付けて**同じ op と args**で exec を送り直す
  - 承認カードは2段に分ける（2026-10-06）。**上の段**は、デバイスが返した request（`summary`）と、Web/API がそのデバイスのマニフェスト・登録済みの部品・部品カタログから作る「場所（Raspberry Pi の40ピンの番号。例: I2C 端子＝40ピンの 3番 SDA・5番 SCL、GPIO17＝11番）・相手（アドレスと、登録済みの部品か部品カタログの候補）・操作の意味（未登録なら「SHT30 なら…」と条件付き）」と、該当ピンを黄色にした40ピンの図（`services/web/explain.py`）。求められた時点の状態で作って承認待ちに残す。**下の段**は、エージェントが `purpose`（`POST /api/approvals` の任意の欄。200文字まで）で書いた目的で、「エージェントの説明（確認されていません）」と明記して別の枠に出す。エージェントは LLM なので、上の段には一切その言葉を入れない（注入されたエージェントが説明を偽っても、上の段の事実は変わらない）。生の引数は「詳細」に畳む
  - 承認待ちは10分で `expired`
  - **デバイスのセッションが終わったら、そのセッション向けの承認待ちは `expired`（`reason: "session_ended"`）**。Gateway がデバイスの切断（`bye` のあとの切断を含む）と新しい `hello` を処理したときに、Firestore の承認待ちを書き換える。承認はセッションに縛られ、セッションが変わったあとに承認しても `wrong_session` になるだけなので、押せないカードを残さない。Web/API も、承認を押された時点でデバイスのセッションが変わっていれば署名せずに `expired` にする（Gateway の後始末より先に押された場合）。チャットのカードは「接続が切れたため無効。エージェントが必要なら再度求めます」と出し、Agent には「人が却下したのではない。必要なら同じ操作をもう一度求める」と返す

## 5. Firestore

| コレクション | ドキュメント | 中身 |
|---|---|---|
| `devices` | `{device_id}` | `owner`, `token_sha256`, `online`, `session_id`, `manifest`, `manifest_sha256`, `state`, `last_seen_ms` |
| `devices/{id}/samples` | `{job_id}_{seq:06d}` | `job_id`, `seq`, `ts`, `errors`, `rows: [{"t": 0, "v": ["0a14", null]}]`（Firestore は配列の中に配列を置けないので行を map にする） |
| `devices/{id}/events` | 自動 ID | event メッセージそのもの |
| `devices/{id}/requests` | `{session_id}_{args_sha256}` | デバイスが `approval_required` / `approval_invalid` で返した `request` と `ts`（Gateway が書く） |
| `pairing_codes` | `{code}` | `owner`, `expires_ms` |
| `devices/{id}/logs` | `{log_id}` | 記録ジョブの情報: `name`, `args`, `columns`, `started_ts`, `status`（running / completed / stopped / estop / error）, `ended_ts`, `rows`, `photos`, `last_row`, `last_photo`, `parts`（開始時に登録済みだった部品）, `decoders`（Agent が登録する列ごとの換算方法） |
| `devices/{id}/logs/{log_id}/batches` | `{seq:06d}` | バッチ: `rows: [{"t", "v"}]`、`photos: [{"t", "blob_id"}]`、`errors`、`started_ts`。保存したら Gateway がデバイスに `ack` を返す。再送は同じ seq に上書き |
| `devices/{id}/blobs` | `{blob_id}` | 写真の情報: `path`（Cloud Storage）, `content_type`, `bytes`, `sha256`, `ts`, `meta`（`job_id`・`t` か `exec_id`） |
| `gateway` | `leader` | いちばん新しく起動した Gateway インスタンス（6章） |
| `approvals` | `{approval_id}` | `request`, `run_id`, `status`, `grant`, `decided_by`, `created_ms`, `decided_ms` |

### 写真（Cloud Storage）

- バケット `mugemuge-agent-media`（`MEDIA_BUCKET`）。置き場所は `devices/{device_id}/{blob_id}.jpg|png`
- 書き込みは Gateway の SA（`roles/storage.objectCreator`）、読み取りは Web/API の SA（`roles/storage.objectViewer`）だけ。公開しない（画面には Web/API が中継して出す）
- ライフサイクルのルールで **90日後に削除**。数値（Firestore）は消さない（小さいので）
- Gateway は断片を組み立て、`total_bytes` と `sha256` を確かめてから保管する。1枚 16MB まで、組み立て中は1接続あたり8枚まで

## 6. Gateway の世代交代

Cloud Run は新しいリビジョンを出しても、既存の WebSocket を古いリビジョンのインスタンスに残す（`--timeout` の60分まで）。そのままだと、デバイスは古い Gateway に、Agent の exec は新しい Gateway に届き、デプロイのたびに最長60分 `device_offline` になる（2026-10-06 に本番で確認）。

- 各インスタンスは起動時に `gateway/leader` に `{instance: "<K_REVISION>:<乱数>", started_ms}` を書く
- 5秒ごとに読み、自分より新しい leader がいれば、つないでいるデバイスに `bye`（`reason: "server_drain"`）を送って閉じる（Firestore の読み取りは1インスタンスあたり 1回/5秒）
- デバイスは張り直し（PROTOCOL.md 2.3節のバックオフ）、新しいリビジョンにつながる。新しい `session_id` になるので、前のセッションのグラントは使えない
- その間に新しいインスタンスに来た `/internal/exec` は、すぐに `device_offline` にせず、`min(deadline_ms, 5秒)` までデバイスの接続を待ってから転送する。待っても来なければ `device_offline`（503）
- Agent は `exec.id` を自分で決め、`device_offline` を受けたら同じ `exec.id` で1回だけ送り直す。前の exec がデバイスに届いて実行されていても、デバイスは同じ id に前の結果を返す（`duplicate: true`。二重実行しない）

## 7. 仮想デバイス（M4）

審査員が実機なしで試すための、本物と同じデーモン（`mugemuge_agentd.daemon.Daemon`）を模擬ハード（`hw/virtual.py`: BMP280・SHT30・INA226、リレーとボタン）で動かしたもの。

- 画面の「仮想デバイスで試す」→ Web/API がペアリングコード（1分）を作り、`mugemuge-virtual` に渡す → 本物の Pi と同じく `/v1/pair` でトークンを得て、公開の Gateway に `wss` でつなぐ。Gateway・Agent・Web/API からは本物と区別がつかない
- **1つのブラウザのセッションに1台**（デバイス ID は `virt-xxxxxxxx`、持ち主は `vsession:<セッションのハッシュ>`）。同じ `test` アカウントを複数の審査員が使っても、仮想デバイスとその承認カードは作ったセッションにしか見えず、承認・非常停止もそのセッションだけ（PROTOCOL.md 13章の未決事項への答え）
- 実機（kusupi5）の承認カードは、ログインした人なら誰でも見えて承認できる（2026-10-06 の人の判断）
- 寿命は `VIRTUAL_TTL_S`（既定60分）、同時に `VIRTUAL_MAX_DEVICES`（既定10）台まで
- マニフェストは本番の公開鍵だけ（例専用の鍵は入れない）。判定・承認・デッドマン・監査ログは本物と同じ
- 費用: 外向きの WebSocket を保つので `--no-cpu-throttling`。最小0台なので、使う人がいなければ止まる（`docs/COST.md`）。Cloud Run はリクエストのない状態が続くとインスタンスを止めるので、Web/API は画面を開いている間 `/devices` を問い合わせて止めさせない。止まるときは全デバイスが `bye` を送ってから終わる

## 8. Cloud Run の設定

- Gateway: `--max-instances=1 --timeout=3600 --session-affinity --min-instances=1`（PROTOCOL.md 12章）
- Web/API: `--max-instances=2`
- リージョンは両方同じ。Firestore も同じリージョン（Native モード）
