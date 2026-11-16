#!/usr/bin/env bash
# Deploy the stack and/or publish the site.
#
#   scripts/deploy.sh plan      # create a change set and show what would change (no changes made)
#   scripts/deploy.sh infra     # create/update the CloudFormation stack
#   scripts/deploy.sh site      # export WordPress, sync to S3, invalidate CloudFront
#   scripts/deploy.sh status    # stack outputs and Free plan status
#
# Settings come from deploy.env (copy deploy.env.example). Credentials come from the
# usual AWS CLI chain, e.g. `aws login` or AWS_PROFILE.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$here/deploy.env" ]] || { echo "Missing deploy.env (copy deploy.env.example)" >&2; exit 1; }
# shellcheck source=/dev/null
source "$here/deploy.env"
: "${STACK_NAME:?}" "${DOMAIN_NAME:?}" "${HOSTED_ZONE_ID:?}" "${BUDGET_ALERT_EMAIL:?}" "${SOURCE_URL:?}"
export AWS_REGION=us-east-1 AWS_PAGER=""

params=(
  "DomainName=$DOMAIN_NAME"
  "HostedZoneId=$HOSTED_ZONE_ID"
  "BudgetAlertEmail=$BUDGET_ALERT_EMAIL"
  "AttachHostedZoneToPlan=${ATTACH_HOSTED_ZONE:-false}"
  "MonthlyBudgetUsd=${MONTHLY_BUDGET_USD:-1}"
)

output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

deploy_stack() {
  aws cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$here/template.yaml" \
    --parameter-overrides "${params[@]}" \
    --tags "Project=$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    "$@"
}

case "${1:-}" in
  plan)
    deploy_stack --no-execute-changeset
    ;;
  infra)
    deploy_stack --no-fail-on-empty-changeset
    "$0" status
    ;;
  site)
    bucket="$(output BucketName)"
    dist="$(output DistributionId)"
    out="$here/dist"
    python3 -I "$here/scripts/export.py" --source "$SOURCE_URL" --site "https://$DOMAIN_NAME" --out "$out"

    # Long-lived assets first, then pages (always revalidated) so a page never points at a missing asset.
    aws s3 sync "$out" "s3://$bucket" --delete --only-show-errors \
      --exclude "*.html" --exclude "*.xml" --exclude "*.txt" \
      --cache-control "public, max-age=86400"
    aws s3 sync "$out" "s3://$bucket" --delete --only-show-errors \
      --exclude "*" --include "*.html" --include "*.xml" --include "*.txt" \
      --cache-control "public, max-age=0, must-revalidate"

    id="$(aws cloudfront create-invalidation --distribution-id "$dist" --paths '/*' --query Invalidation.Id --output text)"
    echo "Invalidation $id started; waiting..."
    aws cloudfront wait invalidation-completed --distribution-id "$dist" --id "$id"
    echo "Published https://$DOMAIN_NAME/"
    ;;
  status)
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query 'Stacks[0].[StackStatus, Outputs[].[OutputKey, OutputValue]]' --output text
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
