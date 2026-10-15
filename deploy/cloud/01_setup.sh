#!/bin/bash
# 1回だけ: API の有効化、Firestore、サービスアカウント、IAM。Pi でも Cloud Shell でも実行できる（秘密は扱わない）。
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
gcloud config set project "$PROJECT_ID"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    firestore.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com

gcloud firestore databases describe --database='(default)' >/dev/null 2>&1 || \
    gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native

for sa in mugemuge-gateway mugemuge-web mugemuge-agent mugemuge-virtual; do
    gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
        gcloud iam service-accounts create "$sa" --display-name="$sa"
done

# ビルド専用の SA（既定の compute SA に広い権限を足さない）。gcloud run deploy --build-service-account で使う
gcloud iam service-accounts describe "$BUILD_SA" >/dev/null 2>&1 || \
    gcloud iam service-accounts create mugemuge-build --display-name="mugemuge-build (Cloud Build)"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILD_SA" \
    --role=roles/run.builder --condition=None >/dev/null

# Firestore は Gateway と Web/API だけ。Agent には付けない（M3 で必要になったら読み取りだけ足す）
for sa in "$GATEWAY_SA" "$WEB_SA"; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$sa" \
        --role=roles/datastore.user --condition=None >/dev/null
done
# Agent: Gemini（Vertex AI）を呼ぶ権限とトレースの書き込みだけ。Secret Manager・Firestore には触らせない
for role in roles/aiplatform.user roles/cloudtrace.agent; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$AGENT_SA" \
        --role="$role" --condition=None >/dev/null
done
# Gateway はデバイスとの往復のスパンを書く
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$GATEWAY_SA" \
    --role=roles/cloudtrace.agent --condition=None >/dev/null

# 写真のバケット: 公開を禁止、90日で削除。Gateway は新規作成だけ（上書き・削除はできない）、Web/API は読むだけ
if ! gcloud storage buckets describe "gs://${MEDIA_BUCKET}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${MEDIA_BUCKET}" --location="$REGION" --uniform-bucket-level-access \
        --public-access-prevention
fi
LIFECYCLE="$(mktemp)"
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}' > "$LIFECYCLE"
gcloud storage buckets update "gs://${MEDIA_BUCKET}" --lifecycle-file="$LIFECYCLE" >/dev/null
rm -f "$LIFECYCLE"
gcloud storage buckets add-iam-policy-binding "gs://${MEDIA_BUCKET}" --member="serviceAccount:$GATEWAY_SA" \
    --role=roles/storage.objectCreator >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${MEDIA_BUCKET}" --member="serviceAccount:$WEB_SA" \
    --role=roles/storage.objectViewer >/dev/null

# 仮想デバイス（mugemuge-virtual@）にはロールを付けない。公開の Gateway に wss でつなぐだけ

echo "setup done. 次は Cloud Shell で 02_secrets_cloudshell.sh を実行する"
