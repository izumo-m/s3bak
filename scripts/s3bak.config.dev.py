# Dev s3bak config for the local MinIO stack.
#
# Selected via S3BAK_CONFIG (set by scripts/minio-env.sh). The profile's
# endpoint and credentials live in scripts/aws-config.dev.

profile = "s3bak-minio"
prefix = "s3://s3bak-test/dev"

# Entries for manual play. The unit/e2e tests supply their own configs (via
# S3BAK_CONFIG) pointing at temporary directories, so this stays minimal.
entries = {
    "sample": {"path": "/tmp/s3bak-sample"},
}
