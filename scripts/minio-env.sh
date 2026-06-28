# scripts/minio-env.sh - point aws / s3bak at the local MinIO stack.
#
# Source this file (it only exports environment variables, no side effects):
#
#   scripts/compose-up.sh         # start MinIO first (separate concern)
#   source scripts/minio-env.sh
#   uv run s3bak ls-remote        # uses the dev config + profile below
#   aws s3 ls s3://s3bak-test/    # or poke at MinIO manually
#
# s3bak always passes `--profile <profile>` (from its config), so the endpoint
# and credentials are provided through a repo-local AWS config file
# (scripts/aws-config.dev) selected with AWS_CONFIG_FILE. AWS_ENDPOINT_URL_S3
# and AWS_* are also exported so plain `aws` (without --profile) and botocore
# reach MinIO too.
#
# Windows: this file is POSIX; set the variables manually in PowerShell.

_s3bak_scriptdir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

export AWS_CONFIG_FILE="$_s3bak_scriptdir/aws-config.dev"
export AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_REGION=us-east-1

# Dev s3bak config (profile = s3bak-minio, prefix = s3://s3bak-test/dev).
export S3BAK_CONFIG="$_s3bak_scriptdir/s3bak.config.dev.py"

# Gates the e2e suite (added with the test work); the bucket is created by
# mc-init and must stay empty between runs.
export S3BAK_E2E_BUCKET=s3bak-e2e

unset _s3bak_scriptdir
