#!/usr/bin/env bash

# Platform-owned infrastructure supervisor boundary.
# This script is a release asset: it never sources legacy application config
# and never delegates to an application-owned infrastructure launcher.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/compose.platform.yml"
PROJECT_NAME="${PUDDINGKNOWLEDGE_PROJECT_NAME:-puddingknowledge}"
if [[ ! "${PROJECT_NAME}" =~ ^puddingknowledge(-[a-z0-9][a-z0-9_-]*)?$ ]]; then
    echo "PUDDINGKNOWLEDGE_PROJECT_NAME must be puddingknowledge or puddingknowledge-<suffix>" >&2
    exit 2
fi

usage() {
    cat <<'EOF'
Knowledge Platform infrastructure supervisor

  platform-infra.sh plan
  platform-infra.sh up
  platform-infra.sh down
  platform-infra.sh status

PUDDINGKNOWLEDGE_HOME and all required Compose secret/image inputs must be
provided by the operator. No defaults contain credentials.
Use PUDDINGKNOWLEDGE_PROJECT_NAME=puddingknowledge-<suffix> with a distinct
Home and ports for a separate instance. Reuse the same name for its lifecycle.
EOF
}

require_home() {
    if [[ -z "${PUDDINGKNOWLEDGE_HOME:-}" || "${PUDDINGKNOWLEDGE_HOME}" != /* ]]; then
        echo "PUDDINGKNOWLEDGE_HOME must be an absolute path" >&2
        exit 2
    fi
    if [[ "${PUDDINGKNOWLEDGE_HOME}" == "/" || "${PUDDINGKNOWLEDGE_HOME}" == */ ]]; then
        echo "PUDDINGKNOWLEDGE_HOME must name a dedicated directory without a trailing slash" >&2
        exit 2
    fi
    if [[ -L "${PUDDINGKNOWLEDGE_HOME}" ]]; then
        echo "PUDDINGKNOWLEDGE_HOME must not be a symlink" >&2
        exit 2
    fi
    require_real_directory "${PUDDINGKNOWLEDGE_HOME}"
    require_real_directory "${PUDDINGKNOWLEDGE_HOME}/infrastructure/postgres"
    require_real_directory "${PUDDINGKNOWLEDGE_HOME}/infrastructure/milvus/etcd"
    require_real_directory "${PUDDINGKNOWLEDGE_HOME}/infrastructure/milvus/minio"
    require_real_directory "${PUDDINGKNOWLEDGE_HOME}/infrastructure/milvus/data"
}

require_real_directory() {
    local path_value="$1"
    if [[ "${path_value}" != "${PUDDINGKNOWLEDGE_HOME}" && "${path_value}" != "${PUDDINGKNOWLEDGE_HOME}/"* ]]; then
        echo "Platform infrastructure directory is outside Platform Home: ${path_value}" >&2
        exit 2
    fi
    local current="${PUDDINGKNOWLEDGE_HOME}"
    local component
    local components=()
    local relative="${path_value#"${PUDDINGKNOWLEDGE_HOME}"}"
    IFS='/' read -r -a components <<< "${relative#/}"
    if [[ -z "${relative#/}" ]]; then
        if [[ -L "${current}" || ! -d "${current}" ]]; then
            echo "Platform infrastructure directory must be a real directory: ${path_value}" >&2
            exit 2
        fi
        return
    fi
    for component in "${components[@]}"; do
        [[ -z "${component}" ]] && continue
        current="${current%/}/${component}"
        if [[ -L "${current}" || ! -d "${current}" ]]; then
            echo "Platform infrastructure directory must be a real directory: ${path_value}" >&2
            exit 2
        fi
    done
}

require_project_owner() {
    local ids id owner
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
    for id in ${ids}; do
        owner="$(docker inspect --format '{{ index .Config.Labels "io.puddingknowledge.home" }}' "${id}")"
        if [[ "${owner}" != "${PUDDINGKNOWLEDGE_HOME}" ]]; then
            echo "Compose project contains a container not owned by this Platform Home; refusing lifecycle action" >&2
            exit 2
        fi
    done
}

compose() {
    docker compose --project-name "${PROJECT_NAME}" --file "${COMPOSE_FILE}" "$@"
}

command="${1:-}"
case "${command}" in
    plan)
        require_home
        compose config --quiet
        ;;
    up)
        require_home
        require_project_owner
        compose up -d
        ;;
    down)
        require_home
        require_project_owner
        compose down
        ;;
    status)
        require_home
        compose ps
        ;;
    -h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
