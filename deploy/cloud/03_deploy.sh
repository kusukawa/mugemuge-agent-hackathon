#!/bin/bash
# Gateway と Web/API をデプロイする（Cloud Build でビルド。手元に Docker は要らない）。何度実行してもよい。
set -euo pipefail
cd "$(dirname "$0")/../.."; source deploy/cloud/env.sh

# ビルドは専用の SA で（01_setup.sh で作成）
BUILD="--build-service-account=projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}"

# EXEC_FULL_CALLERS: /internal/exec で何でも送れるのは Agent だけ。Web/API は非常停止の2つと記録の停止だけ（CLOUD.md 1章）
# --set-env-vars の値にカンマを含むので、gcloud の区切り文字を ";" に変える（^;^ 記法）
gcloud run deploy mugemuge-gateway --source . --region="$REGION" "$BUILD" --quiet \
    --service-account="$GATEWAY_SA" --allow-unauthenticated \
    --max-instances=1 --min-instances=1 --timeout=3600 --session-affinity --concurrency=200 \
    --set-env-vars="^;^SERVICE=gateway;MEDIA_BUCKET=${MEDIA_BUCKET};GOOGLE_CLOUD_PROJECT=${PROJECT_ID};GATEWAY_AUDIENCE=${GATEWAY_URL};INTERNAL_CALLERS=${WEB_SA},${AGENT_SA};EXEC_FULL_CALLERS=${AGENT_SA}"

# チャットの送信はエージェントが動き終わるまで待つので、時間切れは Agent と同じ 900 秒
gcloud run deploy mugemuge-web --source . --region="$REGION" "$BUILD" --quiet \
    --service-account="$WEB_SA" --allow-unauthenticated \
    --max-instances=2 --timeout=900 \
    --set-env-vars="^;^SERVICE=web;MEDIA_BUCKET=${MEDIA_BUCKET};GOOGLE_CLOUD_PROJECT=${PROJECT_ID};APPROVAL_KID=${APPROVAL_KID};GATEWAY_URL=${GATEWAY_URL};AGENT_URL=${AGENT_URL};VIRTUAL_URL=${VIRTUAL_URL};VIRTUAL_TTL_S=${VIRTUAL_TTL_S};WEB_AUDIENCE=${WEB_URL};API_CALLERS=${AGENT_SA}" \
    --set-secrets="APPROVAL_SIGNING_KEY=mugemuge-approval-key:latest,SESSION_SECRET=mugemuge-session-secret:latest,MUGEMUGE_USERS=mugemuge-users:latest"

# Agent: 認証必須（呼べるのは IAM の起動元だけ）。セッションと承認待ちはメモリ上なので1台。
# CPU は常時割り当てない（承認の再開は Web/API からの通知と、画面の問い合わせで行う）
gcloud run deploy mugemuge-agent --source . --region="$REGION" "$BUILD" --quiet \
    --service-account="$AGENT_SA" --no-allow-unauthenticated \
    --max-instances=1 --timeout=900 --memory=1Gi \
    --set-env-vars="^;^SERVICE=agent;GOOGLE_CLOUD_PROJECT=${PROJECT_ID};GATEWAY_URL=${GATEWAY_URL};WEB_URL=${WEB_URL};MUGEMUGE_MODEL=${MUGEMUGE_MODEL}"
gcloud run services add-iam-policy-binding mugemuge-agent --region="$REGION" \
    --member="serviceAccount:${WEB_SA}" --role=roles/run.invoker >/dev/null

# 仮想デバイス: 外向きの WebSocket を保つので CPU は常時割り当て（--no-cpu-throttling）。最小0台＝使う人がいなければ止まる。
# 呼べるのは Web/API の SA だけ。デーモンは本物の Pi と同じく公開の Gateway に wss でつなぐ
gcloud run deploy mugemuge-virtual --source . --region="$REGION" "$BUILD" --quiet \
    --service-account="$VIRTUAL_SA" --no-allow-unauthenticated \
    --no-cpu-throttling --min-instances=0 --max-instances=1 --timeout=3600 --cpu=1 --memory=512Mi \
    --set-env-vars="^;^SERVICE=virtual;GATEWAY_URL=${GATEWAY_URL};APPROVAL_KID=${APPROVAL_KID};APPROVAL_PUBLIC_KEY=${APPROVAL_PUBLIC_KEY};VIRTUAL_TTL_S=${VIRTUAL_TTL_S};VIRTUAL_MAX_DEVICES=${VIRTUAL_MAX_DEVICES}"
gcloud run services add-iam-policy-binding mugemuge-virtual --region="$REGION" \
    --member="serviceAccount:${WEB_SA}" --role=roles/run.invoker >/dev/null

echo
echo "Gateway: ${GATEWAY_URL}   （デバイスの gateway_url は wss://${GATEWAY_URL#https://}/v1/device）"
echo "Web/API: ${WEB_URL}"
echo "Agent:   ${AGENT_URL}（認証必須）"
echo "Virtual: ${VIRTUAL_URL}（認証必須）"
curl -fsS "${GATEWAY_URL}/health" && echo
curl -fsS "${WEB_URL}/health" && echo
