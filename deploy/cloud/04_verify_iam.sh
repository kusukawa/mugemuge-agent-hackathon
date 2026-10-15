#!/bin/bash
# M2 の完了条件「Agent サービスのサービスアカウントに Secret Manager の権限がない」を確かめる。
# 署名鍵を読めるのが mugemuge-web@ だけであることも合わせて確かめる。読むだけで何も変えない。
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh

fail=0
echo "== プロジェクトの IAM に、Agent の SA の secretmanager 系ロールがないこと"
roles="$(gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members:serviceAccount:${AGENT_SA}" --format='value(bindings.role)')"
echo "  agent roles: ${roles:-（なし）}"
if echo "$roles" | grep -qiE 'secretmanager|roles/owner|roles/editor'; then echo "  NG"; fail=1; else echo "  OK"; fi

for s in mugemuge-approval-key mugemuge-session-secret mugemuge-users; do
    echo "== Secret ${s} を読めるメンバー"
    members="$(gcloud secrets get-iam-policy "$s" --flatten='bindings[].members' --format='value(bindings.members)')"
    echo "$members" | sed 's/^/  /'
    if echo "$members" | grep -q "$AGENT_SA"; then echo "  NG: agent が読める"; fail=1; fi
    if [ "$(echo "$members" | grep -vc "^serviceAccount:${WEB_SA}$" || true)" != "0" ]; then
        echo "  注意: mugemuge-web@ 以外のメンバーがいる"; fail=1
    fi
done

echo "== Agent の SA として署名鍵を読もうとすると拒否されること（なりすましの権限がある場合だけ確認できる）"
if gcloud secrets versions access latest --secret=mugemuge-approval-key \
        --impersonate-service-account="$AGENT_SA" >/dev/null 2>&1; then
    echo "  NG: 読めてしまった"; fail=1
else
    echo "  OK（読めない。なりすましの権限自体がない場合もここに来る）"
fi
exit $fail
