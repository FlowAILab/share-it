#!/bin/bash
# One-time AWS setup for share-it. Run with an admin-ish profile:
#   ./setup-aws.sh <admin-profile> [bucket-name]
# Creates (idempotently): the bucket (default shareit-links-<account>, pass a short
# custom name for prettier URLs), public-read policy on p/*, tag-based lifecycle
# expiry, a scoped upload-only IAM user + access key stored as the [shareit]
# profile, and ~/.shareit/config.json pointing share-it at all of it.
set -euo pipefail
PROFILE="${1:?usage: ./setup-aws.sh <admin-profile> [bucket-name]}"
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --output text --query Account)
B="${2:-shareit-links-$ACCOUNT}"
REGION=$(aws configure get region --profile "$PROFILE" || true); REGION="${REGION:-us-east-1}"

if ! aws s3api head-bucket --bucket "$B" --profile "$PROFILE" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$B" --profile "$PROFILE"
  else
    aws s3api create-bucket --bucket "$B" --profile "$PROFILE" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
  echo "created bucket $B"
fi

aws s3api put-public-access-block --bucket "$B" --profile "$PROFILE" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false

aws s3api put-bucket-policy --bucket "$B" --profile "$PROFILE" --policy '{
  "Version":"2012-10-17",
  "Statement":[{"Sid":"PublicReadP","Effect":"Allow","Principal":"*",
    "Action":"s3:GetObject","Resource":"arn:aws:s3:::'"$B"'/p/*"}]}'

aws s3api put-bucket-lifecycle-configuration --bucket "$B" --profile "$PROFILE" \
  --lifecycle-configuration '{"Rules":[
    {"ID":"presigned-purge","Status":"Enabled","Filter":{"Prefix":"shareit/"},"Expiration":{"Days":30}},
    {"ID":"tag-1d","Status":"Enabled","Filter":{"And":{"Prefix":"p/","Tags":[{"Key":"expiry","Value":"1d"}]}},"Expiration":{"Days":1}},
    {"ID":"tag-3d","Status":"Enabled","Filter":{"And":{"Prefix":"p/","Tags":[{"Key":"expiry","Value":"3d"}]}},"Expiration":{"Days":3}},
    {"ID":"tag-7d","Status":"Enabled","Filter":{"And":{"Prefix":"p/","Tags":[{"Key":"expiry","Value":"7d"}]}},"Expiration":{"Days":7}}]}'

aws iam get-user --user-name shareit-uploader --profile "$PROFILE" >/dev/null 2>&1 \
  || aws iam create-user --user-name shareit-uploader --profile "$PROFILE" >/dev/null

aws iam put-user-policy --user-name shareit-uploader --profile "$PROFILE" \
  --policy-name shareit-minimal --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:PutObject","s3:PutObjectTagging","s3:GetObject","s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::'"$B"'/shareit/*","arn:aws:s3:::'"$B"'/p/*"]},
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::'"$B"'"}
  ]}'

# an existing [shareit] profile must actually be the scoped uploader in THIS account
if aws configure get aws_access_key_id --profile shareit >/dev/null 2>&1; then
  ARN=$(aws sts get-caller-identity --profile shareit --output text --query Arn 2>/dev/null || true)
  case "$ARN" in
    "arn:aws:iam::$ACCOUNT:user/shareit-uploader") : ;;  # valid
    *) echo "existing [shareit] profile is stale ($ARN) — recreating key"
       aws configure set aws_access_key_id "" --profile shareit ;;
  esac
fi
if [ -z "$(aws configure get aws_access_key_id --profile shareit 2>/dev/null)" ]; then
  KEY=$(aws iam create-access-key --user-name shareit-uploader --profile "$PROFILE" --output json)
  aws configure set aws_access_key_id \
    "$(echo "$KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')" --profile shareit
  aws configure set aws_secret_access_key \
    "$(echo "$KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')" --profile shareit
  echo "created scoped access key → [shareit] profile"
fi
aws configure set region "$REGION" --profile shareit

mkdir -p ~/.shareit && chmod 700 ~/.shareit
python3 - "$B" "$REGION" <<'EOF'
import json, os, sys
p = os.path.expanduser("~/.shareit/config.json")
cfg = {}
try: cfg = json.load(open(p))
except Exception: pass
cfg["aws"] = {"profile": "shareit", "bucket": sys.argv[1], "region": sys.argv[2]}
cfg.pop("public_probe", None)  # force re-detection of the public prefix
json.dump(cfg, open(p, "w"), indent=1)
os.chmod(p, 0o600)
print("wrote ~/.shareit/config.json →", cfg["aws"])
EOF

echo "done — restart share-it (or wait <10 min) to pick up short public links"
