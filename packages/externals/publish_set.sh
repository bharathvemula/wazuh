#!/bin/bash
#
# Publish a set built by 5_builderpackage_externals.yml or
# 5_builderpackage_embedded-python.yml to s3://<bucket>/deps/<set>/, the
# bucket behind packages.wazuh.com/deps/.
#
#   publish_set.sh <tarball> <bucket> <set>
#
# <tarball> is externals-all.tar.gz or python-all.tar.gz (manifest.json plus
# libraries/); <set> is 5/externals/<n> or 5/python/<n> and must be the
# deps_version its manifest names. Credentials come from FEED_AWS_KEY_ID and
# FEED_AWS_SECRET_ACCESS_KEY; AWS_REGION defaults to us-west-1.
#
# A published set is never modified: every file is written with
# --if-none-match '*', and manifest.json goes last, so a set without its
# manifest is incomplete. A key left by an interrupted run is accepted only if
# its content has the sha256 the manifest records.

set -euo pipefail

log() { echo "[publish] $*"; }
err() { echo "[publish][ERROR] $*" >&2; }

if [ $# -ne 3 ]; then
    err "usage: $0 <tarball> <bucket> <set>"
    exit 2
fi
TARBALL="$1" BUCKET="$2" SET="$3"

if ! [[ "${SET}" =~ ^5/(externals|python)/[1-9][0-9]*$ ]]; then
    err "set must be 5/externals/<n> or 5/python/<n>, got '${SET}'"
    exit 2
fi
if [ -z "${FEED_AWS_KEY_ID:-}" ] || [ -z "${FEED_AWS_SECRET_ACCESS_KEY:-}" ]; then
    err "FEED_AWS_KEY_ID and FEED_AWS_SECRET_ACCESS_KEY are required"
    exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
tar -xzf "${TARBALL}" -C "${WORK}"

manifest_set="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["deps_version"])' "${WORK}/manifest.json")"
if [ "${manifest_set}" != "${SET}" ]; then
    err "the manifest describes set '${manifest_set}', not '${SET}'"
    exit 1
fi

# The artifact download left OIDC credentials for the internal CI bucket in the
# environment; they must not mix with the publishing ones.
unset AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_PROFILE
export AWS_ACCESS_KEY_ID="${FEED_AWS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${FEED_AWS_SECRET_ACCESS_KEY}"
export AWS_REGION="${AWS_REGION:-us-west-1}" AWS_DEFAULT_REGION="${AWS_REGION:-us-west-1}"

if ! aws s3api put-object help 2>/dev/null | grep -q -- '--if-none-match'; then
    err "this AWS CLI does not support put-object --if-none-match"
    exit 1
fi

PREFIX="deps/${SET}"
if aws s3api head-object --bucket "${BUCKET}" --key "${PREFIX}/manifest.json" >/dev/null 2>&1; then
    err "${PREFIX}/manifest.json already exists: the set is published"
    exit 1
fi

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# Write $1 to key $2; an existing key is accepted only with sha256 $3.
put() {
    local file="$1" key="$2" want="$3" out
    if out="$(aws s3api put-object --bucket "${BUCKET}" --key "${key}" --body "${file}" --if-none-match '*' 2>&1)"; then
        return 0
    fi
    if ! grep -q 'PreconditionFailed' <<< "${out}"; then
        err "upload of ${key} failed: ${out}"
        return 1
    fi
    aws s3api get-object --bucket "${BUCKET}" --key "${key}" "${WORK}/existing" >/dev/null
    if [ "$(sha256_of "${WORK}/existing")" != "${want}" ]; then
        err "${key} already exists with other content"
        return 1
    fi
    log "${key} already uploaded by an earlier run, same content"
}

count=0
while IFS= read -r -d '' file; do
    rel="${file#"${WORK}/"}"
    want="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["files"].get(sys.argv[2], ""))' "${WORK}/manifest.json" "${rel}")"
    if [ "${want}" != "$(sha256_of "${file}")" ]; then
        err "${rel} does not match the manifest"
        exit 1
    fi
    put "${file}" "${PREFIX}/${rel}" "${want}"
    count=$((count + 1))
done < <(find "${WORK}/libraries" -type f -print0 | sort -z)

listed="$(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["files"]))' "${WORK}/manifest.json")"
if [ "${count}" -ne "${listed}" ]; then
    err "the tarball has ${count} files under libraries/, the manifest lists ${listed}"
    exit 1
fi
put "${WORK}/manifest.json" "${PREFIX}/manifest.json" "$(sha256_of "${WORK}/manifest.json")"
log "published ${count} files and manifest.json to s3://${BUCKET}/${PREFIX}/"
