#!/usr/bin/env bash
set -euo pipefail

if [ -x "${ES_HOME}/bin/elasticsearch" ]; then
  exit 0
fi
: "${ES_VERSION:?ES_VERSION is required}"
: "${ES_SHA512:?ES_SHA512 is required}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
archive="${tmp_dir}/elasticsearch.tar.gz"
curl --http1.1 --retry 5 --retry-all-errors --retry-delay 3 -fsSL \
  "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-${ES_VERSION}-linux-x86_64.tar.gz" \
  -o "${archive}"
printf '%s  %s\n' "${ES_SHA512}" "${archive}" | sha512sum -c -
mkdir -p /opt
tar -xzf "${archive}" -C /opt
mv "/opt/elasticsearch-${ES_VERSION}" "${ES_HOME}"
