# deploy/cloud/*.sh が読む設定。PROJECT_ID と REGION を埋めてから使う。
export PROJECT_ID="${PROJECT_ID:-}"          # 例: mugemuge-hackathon
export REGION="${REGION:-asia-northeast1}"
export APPROVAL_KID="${APPROVAL_KID:-api-2026-10}"
# 承認の公開鍵（秘密ではない）。02_secrets_cloudshell.sh が表示したもの。仮想デバイスのマニフェストに入る
export APPROVAL_PUBLIC_KEY="${APPROVAL_PUBLIC_KEY:-9Fip5FtbCIaB1LROMkt2qHYClTqHv5OUgvxql3Jyk48}"

if [ -z "$PROJECT_ID" ]; then echo "PROJECT_ID を設定してください" >&2; return 1 2>/dev/null || exit 1; fi
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export PROJECT_NUMBER
export GATEWAY_SA="mugemuge-gateway@${PROJECT_ID}.iam.gserviceaccount.com"
export WEB_SA="mugemuge-web@${PROJECT_ID}.iam.gserviceaccount.com"
export AGENT_SA="mugemuge-agent@${PROJECT_ID}.iam.gserviceaccount.com"
export BUILD_SA="mugemuge-build@${PROJECT_ID}.iam.gserviceaccount.com"
# Cloud Run の決定的な URL（https://<サービス>-<プロジェクト番号>.<リージョン>.run.app）
export GATEWAY_URL="https://mugemuge-gateway-${PROJECT_NUMBER}.${REGION}.run.app"
export WEB_URL="https://mugemuge-web-${PROJECT_NUMBER}.${REGION}.run.app"
export AGENT_URL="https://mugemuge-agent-${PROJECT_NUMBER}.${REGION}.run.app"
export MEDIA_BUCKET="${MEDIA_BUCKET:-${PROJECT_ID}-media}"   # 写真（90日で削除）
export VIRTUAL_URL="https://mugemuge-virtual-${PROJECT_NUMBER}.${REGION}.run.app"
export VIRTUAL_SA="mugemuge-virtual@${PROJECT_ID}.iam.gserviceaccount.com"
export VIRTUAL_TTL_S="${VIRTUAL_TTL_S:-3600}"          # 仮想デバイスの寿命（秒）
export VIRTUAL_MAX_DEVICES="${VIRTUAL_MAX_DEVICES:-10}"
export MUGEMUGE_MODEL="${MUGEMUGE_MODEL:-gemini-3.5-flash}"
