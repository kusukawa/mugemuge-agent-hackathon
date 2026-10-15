# クラウドへのデプロイ（M2）

構成と権限の考え方は `docs/CLOUD.md`。ここは手順だけ。スクリプトは `deploy/cloud/`。

| 手順 | どこで | 何をする | 秘密を扱うか |
|---|---|---|---|
| 0 | Pi | `gcloud auth login` とプロジェクトの用意 | — |
| 1 | Pi | `01_setup.sh`: API、Firestore、サービスアカウント、Firestore の権限 | いいえ |
| 2 | **Cloud Shell** | `02_secrets_cloudshell.sh`: 署名鍵・セッション鍵・利用者の Secret | **はい**（Pi では実行しない） |
| 3 | Pi | マニフェストに公開鍵と gateway_url を書く | 公開鍵のみ |
| 4 | Pi | `03_deploy.sh`: Gateway と Web/API をデプロイ | いいえ（Secret は Cloud Run が注入） |
| 5 | Pi | `04_verify_iam.sh`: Agent の SA に Secret の権限がないことを確認 | いいえ |
| 6 | Pi | ペアリングしてデーモンを本番の Gateway につなぐ | デバイストークン |
| 7 | Pi＋ブラウザ | `tools/agent_cli.py` で承認付きの exec を送り、ブラウザで承認する | — |

## 0. 準備

```sh
gcloud auth login --no-launch-browser          # 表示された URL をブラウザで開いてコードを貼る
export PROJECT_ID=<プロジェクトID> REGION=asia-northeast1
gcloud config set project "$PROJECT_ID"        # 課金が有効なプロジェクト
```

## 2. 秘密（Cloud Shell）

承認の Ed25519 秘密鍵は **Pi 上で生成・保存しない**（CLAUDE.md の安全ルール）。Cloud Shell で `deploy/cloud/env.sh` と `deploy/cloud/02_secrets_cloudshell.sh` を同じディレクトリに置き（アップロードか貼り付け）、

```sh
export PROJECT_ID=<プロジェクトID> REGION=asia-northeast1
bash 02_secrets_cloudshell.sh
```

- 秘密鍵は一時ディレクトリで作って Secret Manager に入れ、すぐ消す。画面に出るのは**公開鍵だけ**
- 利用者（ログイン ID とパスワード）をその場で入力する。審査員用のテストアカウントもここで作る（パスワードは scrypt ハッシュだけが Secret に入る）
- 3つの Secret を読めるのは `mugemuge-web@` だけ

## 3. マニフェスト

`/etc/mugemuge/manifest.yaml`（開発中は手元のコピー）を直す:

```yaml
link:
  gateway_url: wss://mugemuge-gateway-<プロジェクト番号>.<リージョン>.run.app/v1/device
approval:
  public_keys:
    - kid: api-2026-10
      key: <02 で表示された公開鍵>
```

例専用の鍵（`z2wDYLx…`）は消す。残すと、シードが公開されている鍵で承認を偽造できてしまう。

## 6. ペアリングとデーモン

新しい Pi なら、Web/API にログインして「デバイスを追加」を押し、画面に出る1行（`curl -fsSL …/install.sh | sudo bash -s -- <コード>`）を Pi で実行すれば、以下の 1〜3 とマニフェストの作成（手順 3）がまとめて済む（README の「新しい Pi に入れる」）。手で行うなら:

1. Web/API（`https://mugemuge-web-…run.app`）にログインし、「デバイスを追加」でコードを出す
2. Pi で `python3 -m mugemuge_agentd pair <コード> --manifest <マニフェスト> [--token-file <パス>]`
3. `python3 -m mugemuge_agentd run --manifest <マニフェスト>`（本番は systemd。README の「本番での動かし方」）

## 7. 承認の確認（M2 の完了条件）

Agent（M3）の代役として `tools/agent_cli.py` を使う。ID トークンは `mugemuge-agent@` になりすまして取る（自分のアカウントに、その SA の `roles/iam.serviceAccountTokenCreator` が要る）:

```sh
gcloud iam service-accounts add-iam-policy-binding "mugemuge-agent@${PROJECT_ID}.iam.gserviceaccount.com" \
    --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountTokenCreator

python3 tools/agent_cli.py --gateway "$GATEWAY_URL" --web "$WEB_URL" --device <device.id> \
    --impersonate "mugemuge-agent@${PROJECT_ID}.iam.gserviceaccount.com" \
    i2c.bind '{"bus":1,"addr":118,"part":"BMP280","fingerprint":{"reg":208,"len":1,"hex":"58"},"write_allow_regs":[244,245]}'
```

ブラウザに承認カードが出るので承認する → CLI に `ok [approved …] {"bound": true}` が出れば完了。

## ローカルで全部を動かす（クラウドなし）

`tests/test_web.py` が、Gateway・Web/API・デーモン（模擬ハード）・Agent の代役を1つのプロセスで動かし、ブラウザでの承認まで通している。手で触るなら:

```sh
export MUGEMUGE_DEV_INSECURE=1 MUGEMUGE_STORE=memory PYTHONPATH=.
SERVICE=gateway uvicorn --factory services.gateway.app:app_from_env --port 8080 --ws-max-size 65536
```

（`MUGEMUGE_STORE=memory` は1プロセス内だけの保存先なので、Gateway と Web/API を別プロセスにすると状態を共有できない。ローカルで両方を手で触るのはテストの形で行う）
