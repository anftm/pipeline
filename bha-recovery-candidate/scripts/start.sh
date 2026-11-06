#!/usr/bin/env bash
set -euo pipefail
export ES_URL="${ES_URL:-http://127.0.0.1:9200}"
export PORT="${PORT:-7860}"
export ES_JAVA_OPTS="${ES_JAVA_OPTS:--Xms1g -Xmx1g}"
data_root="/data/elasticsearch"
app_data_root="/app/data"
bucket_dir="${BHA_BUCKET_DIR:-}"

if [ ! -d /data ]; then
  data_root="/app/es-data"
fi
export DATA_ROOT="${DATA_ROOT:-${app_data_root}}"
snapshot_repo_root="${DATA_ROOT}/es-repositories"
mkdir -p "${data_root}" /app/logs "${DATA_ROOT}" "${snapshot_repo_root}/production" "${snapshot_repo_root}/probe"
if [ -n "${bucket_dir}" ]; then
  mkdir -p "${bucket_dir}/generations"
fi
index_version="$(python -c 'from app.config import INDEX_VERSION; print(INDEX_VERSION)')"
version_file="${DATA_ROOT}/es-index-version"
status_file="${DATA_ROOT}/index-status"
convergence_plan="${DATA_ROOT}/bucket-snapshot-convergence.json"

printf 'starting' > "${status_file}"

ownership_marker="${DATA_ROOT}/.ownership-ready"
if [ "${BHA_FORCE_OWNERSHIP:-0}" = "1" ] || [ ! -f "${ownership_marker}" ]; then
  chown -R elasticsearch:elasticsearch "${data_root}" /app/logs "${snapshot_repo_root}"
  touch "${ownership_marker}"
else
  chown elasticsearch:elasticsearch "${data_root}" /app/logs "${snapshot_repo_root}"
fi
cat > "${ES_HOME}/config/elasticsearch.yml" <<EOF
cluster.name: bha-lite
node.name: bha-lite-node
discovery.type: single-node
network.host: 127.0.0.1
http.port: 9200
xpack.security.enabled: false
xpack.security.http.ssl.enabled: false
path.data: ${data_root}
path.logs: /app/logs
path.repo: ${snapshot_repo_root}
EOF
chown elasticsearch:elasticsearch "${ES_HOME}/config/elasticsearch.yml"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --no-access-log &
api_pid="$!"
su -s /bin/bash elasticsearch -c "${ES_HOME}/bin/elasticsearch" &
es_pid="$!"
(
  trap 'rc=$?; printf "failed" > "${status_file}"; kill "${api_pid}" "${es_pid}" 2>/dev/null || true; exit "${rc}"' ERR
  /app/scripts/wait-for-es.sh
  if [ "${BHA_BUCKET_PROBE:-0}" = "1" ]; then
    python -m app.bucket_probe "${bucket_dir}"
    exit $?
  fi
  if [ -n "${bucket_dir}" ] && [ "${RESET_INDEX:-0}" != "1" ]; then
    printf 'restoring' > "${status_file}"
    if python -m app.bucket_snapshot restore "${bucket_dir}"; then
      source_stale="$(python -c 'import json; from app.bucket_snapshot import snapshot_status; print("1" if (snapshot_status() or {}).get("source_stale") else "0")')"
      if [ "${source_stale}" != "1" ]; then
        printf '%s' "${index_version}" > "${version_file}"
        printf 'ready' > "${status_file}"
        exit 0
      fi
      printf 'stale snapshot restored; continuing with index convergence\n' >&2
      printf '%s' "${index_version}" > "${version_file}"
      printf 'ready' > "${status_file}"
      if [ ! -f "${convergence_plan}" ]; then
        printf 'snapshot source could not be verified; waiting for an explicit revision update\n' >&2
        exit 0
      fi
    fi
  fi
  if [ "$(<"${status_file}")" != "ready" ]; then
    printf 'indexing' > "${status_file}"
  fi

  indexer_args=(--ensure)
  if [ -f "${convergence_plan}" ]; then
    indexer_args+=(--plan "${convergence_plan}")
  fi
  if python -m app.indexer "${indexer_args[@]}"; then
    printf '%s' "${index_version}" > "${version_file}"
    printf 'ready' > "${status_file}"
    if [ -n "${bucket_dir}" ]; then
      if ! python -m app.bucket_snapshot publish "${bucket_dir}"; then
        printf 'bha_bucket_snapshot=publish_failed\n' >&2
      fi
    else
      rm -f "${convergence_plan}"
    fi
  else
    if [ "$(<"${status_file}")" = "ready" ]; then
      printf 'background index convergence failed; continuing with restored snapshot\n' >&2
    else
      printf 'failed' > "${status_file}"
      kill "${api_pid}" "${es_pid}" 2>/dev/null || true
      exit 1
    fi
  fi
) &
indexer_pid="$!"
trap 'kill ${api_pid} ${indexer_pid} ${es_pid} 2>/dev/null || true; wait || true' TERM INT
set +e
wait -n "${api_pid}" "${es_pid}" "${indexer_pid}"
first_exit="$?"
set -e
if ! kill -0 "${indexer_pid}" 2>/dev/null; then
  lifecycle_status=""
  if [ -f "${status_file}" ]; then
    lifecycle_status="$(<"${status_file}")"
  fi
  if [ "${first_exit}" -ne 0 ] || { [ "${BHA_BUCKET_PROBE:-0}" != "1" ] && [ "${lifecycle_status}" != "ready" ]; }; then
    printf 'failed' > "${status_file}"
    kill "${api_pid}" "${es_pid}" 2>/dev/null || true
    wait || true
    exit 1
  fi
  wait -n "${api_pid}" "${es_pid}"
else
  kill "${api_pid}" "${indexer_pid}" "${es_pid}" 2>/dev/null || true
  wait || true
  if [ "${first_exit}" -ne 0 ]; then
    exit "${first_exit}"
  fi
  exit 1
fi
