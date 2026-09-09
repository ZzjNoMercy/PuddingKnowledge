#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

API_PORT="${KNOWLEDGE_API_PORT:-8889}"
CONSOLE_PORT="${KNOWLEDGE_CONSOLE_PORT:-8090}"
API_PORT_EXPLICIT=0
CONSOLE_PORT_EXPLICIT=0
if [[ -n "${KNOWLEDGE_API_PORT+x}" ]]; then
    API_PORT_EXPLICIT=1
fi
if [[ -n "${KNOWLEDGE_CONSOLE_PORT+x}" ]]; then
    CONSOLE_PORT_EXPLICIT=1
fi
CATALOG_INPUT="${KNOWLEDGE_CATALOG:-artifacts/phase0b-local-catalog/knowledge-platform.sqlite3}"
WIKI_INPUT="${KNOWLEDGE_WIKI_ROOT:-}"
REVIEW_QUEUE_INPUT="${KNOWLEDGE_REVIEW_QUEUE:-}"
DATABASE_CONFIG_INPUT="${KNOWLEDGE_DATABASE_CONFIG:-}"
STARTUP_TIMEOUT="${KNOWLEDGE_STARTUP_TIMEOUT:-60}"
BUILD_CONSOLE=1
CHECK_ONLY=0
OPEN_BROWSER=0
KEEP_TEMP="${KNOWLEDGE_KEEP_TEMP:-0}"

usage() {
    cat <<'EOF'
Usage: ./scripts/start-knowledge-local.sh [options]

Start the local-only Knowledge Platform API and product Web UI together.

Options:
  --catalog PATH         staged local Catalog SQLite file
  --wiki-root PATH       local Wiki directory (defaults to the existing local Wiki)
  --database-config PATH explicit local PostgreSQL/Vanna configuration
  --review-queue PATH    optional path-free Asset binding review queue
  --api-port PORT        require this loopback API port (default starts at 8889)
  --console-port PORT    require this loopback Web UI port (default starts at 8090)
  --no-build             reuse the existing Next production build
  --open                 open the product UI in the system browser after readiness
  --check                validate inputs and ports, then exit
  -h, --help             show this help

Environment equivalents:
  KNOWLEDGE_CATALOG, KNOWLEDGE_WIKI_ROOT, KNOWLEDGE_REVIEW_QUEUE,
  KNOWLEDGE_API_PORT, KNOWLEDGE_CONSOLE_PORT, KNOWLEDGE_STARTUP_TIMEOUT,
  KNOWLEDGE_DATABASE_CONFIG, KNOWLEDGE_KEEP_TEMP=1

This launcher is local shadow-only. It never activates a deployment or changes
the canonical staged Catalog. An occupied default port advances to the next
free port; an occupied explicitly requested port fails without killing it.
EOF
}

fail() {
    echo "[错误] $*" >&2
    exit 1
}

require_value() {
    if [[ $# -lt 2 || -z "$2" ]]; then
        fail "$1 需要一个值"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --catalog)
            require_value "$@"
            CATALOG_INPUT="$2"
            shift 2
            ;;
        --wiki-root)
            require_value "$@"
            WIKI_INPUT="$2"
            shift 2
            ;;
        --database-config)
            require_value "$@"
            DATABASE_CONFIG_INPUT="$2"
            shift 2
            ;;
        --review-queue)
            require_value "$@"
            REVIEW_QUEUE_INPUT="$2"
            shift 2
            ;;
        --api-port)
            require_value "$@"
            API_PORT="$2"
            API_PORT_EXPLICIT=1
            shift 2
            ;;
        --console-port)
            require_value "$@"
            CONSOLE_PORT="$2"
            CONSOLE_PORT_EXPLICIT=1
            shift 2
            ;;
        --no-build)
            BUILD_CONSOLE=0
            shift
            ;;
        --open)
            OPEN_BROWSER=1
            shift
            ;;
        --check)
            CHECK_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数: $1"
            ;;
    esac
done

absolute_from_repo() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$REPO_ROOT" "$1" ;;
    esac
}

valid_decimal() {
    local value="$1"
    local max_digits="$2"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] && (( ${#value} <= max_digits ))
}

valid_port() {
    valid_decimal "$1" 5 && (( $1 <= 65535 ))
}

valid_timeout() {
    valid_decimal "$1" 3 && (( $1 <= 300 ))
}

port_in_use() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

next_available_port() {
    local candidate="$1"
    local excluded="$2"
    while (( candidate <= 65535 )); do
        if [[ "$candidate" != "$excluded" ]] && ! port_in_use "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
        candidate=$((candidate + 1))
    done
    return 1
}

PYTHON_BIN="$REPO_ROOT/backend/.venv/bin/python"
SERVER_SCRIPT="$REPO_ROOT/backend/knowledge_platform/local/__main__.py"
CONSOLE_PACKAGE="$REPO_ROOT/packages/knowledge-platform-web"
CONSOLE_BUILD_ID="$CONSOLE_PACKAGE/.next/BUILD_ID"
CATALOG_PATH="$(absolute_from_repo "$CATALOG_INPUT")"

if [[ -z "$WIKI_INPUT" ]]; then
    if [[ -n "${HOME:-}" && -d "$HOME/Documents/knowledge/llm-wiki/wiki" ]]; then
        WIKI_INPUT="$HOME/Documents/knowledge/llm-wiki/wiki"
    else
        WIKI_INPUT="$REPO_ROOT/docs"
    fi
fi
WIKI_ROOT="$(absolute_from_repo "$WIKI_INPUT")"

REVIEW_QUEUE=""
if [[ -n "$REVIEW_QUEUE_INPUT" ]]; then
    REVIEW_QUEUE="$(absolute_from_repo "$REVIEW_QUEUE_INPUT")"
fi

command -v npm >/dev/null 2>&1 || fail "未找到 npm"
command -v curl >/dev/null 2>&1 || fail "未找到 curl"
command -v lsof >/dev/null 2>&1 || fail "未找到 lsof"
[[ -x "$PYTHON_BIN" ]] || fail "缺少 backend/.venv；请先准备后端开发环境"
[[ -f "$SERVER_SCRIPT" && ! -L "$SERVER_SCRIPT" ]] || fail "Knowledge API 启动器缺失或是 symlink"
[[ -f "$CATALOG_PATH" && ! -L "$CATALOG_PATH" ]] || fail "staged Catalog 缺失或是 symlink: $CATALOG_PATH"
[[ -d "$WIKI_ROOT" && ! -L "$WIKI_ROOT" ]] || fail "Wiki root 缺失或是 symlink: $WIKI_ROOT"
[[ -d "$CONSOLE_PACKAGE" && ! -L "$CONSOLE_PACKAGE" ]] || fail "Knowledge Web package 缺失或是 symlink"
[[ -d "$CONSOLE_PACKAGE/node_modules" && ! -L "$CONSOLE_PACKAGE/node_modules" ]] \
    || fail "Knowledge Web 依赖缺失；请先运行 npm --prefix packages/knowledge-platform-web install"
if [[ -n "$REVIEW_QUEUE" ]]; then
    [[ -f "$REVIEW_QUEUE" && ! -L "$REVIEW_QUEUE" ]] || fail "review queue 缺失或是 symlink: $REVIEW_QUEUE"
fi

DATABASE_CONFIG=""
DATABASE_PASSWORD_ENV=""
if [[ -n "$DATABASE_CONFIG_INPUT" ]]; then
    DATABASE_CONFIG="$(absolute_from_repo "$DATABASE_CONFIG_INPUT")"
    DATABASE_PASSWORD_ENV="$(PYTHONPATH="$REPO_ROOT/backend" "$PYTHON_BIN" - "$DATABASE_CONFIG" <<'PYCONFIG'
import os
import sys
from pathlib import Path
from knowledge_platform.local.database import load_database_config
try:
    config = load_database_config(Path(sys.argv[1]))
    if config.password_env and config.password_env not in os.environ:
        raise ValueError("missing explicit password variable")
    import asyncpg  # optional runtime dependency must be installed
except Exception:
    raise SystemExit("Invalid database configuration, missing explicit password variable, or missing asyncpg")
print(config.password_env or "")
PYCONFIG
)" || fail "Database 配置预检失败"
fi

PATHS_TO_VALIDATE=("$SERVER_SCRIPT" "$CATALOG_PATH" "$WIKI_ROOT" "$CONSOLE_PACKAGE")
if [[ -n "$REVIEW_QUEUE" ]]; then
    PATHS_TO_VALIDATE+=("$REVIEW_QUEUE")
fi
if ! "$PYTHON_BIN" - "${PATHS_TO_VALIDATE[@]}" <<'PY'
import os
import sys
from pathlib import Path

system_aliases = {
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}
for raw_path in sys.argv[1:]:
    absolute = Path(os.path.abspath(raw_path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if not current.is_symlink():
            continue
        target = Path(os.path.realpath(current))
        if system_aliases.get(current) == target:
            continue
        raise SystemExit("input path contains a symlink component")
PY
then
    fail "本地输入路径包含不允许的 symlink 组件"
fi

valid_port "$API_PORT" || fail "API 端口无效: $API_PORT"
valid_port "$CONSOLE_PORT" || fail "Console 端口无效: $CONSOLE_PORT"
valid_timeout "$STARTUP_TIMEOUT" || fail "KNOWLEDGE_STARTUP_TIMEOUT 必须是无前导零的 1-300 秒"
if port_in_use "$API_PORT"; then
    if (( API_PORT_EXPLICIT )); then
        fail "显式指定的 API 端口 $API_PORT 已被占用；不会自动终止现有进程"
    fi
    ORIGINAL_API_PORT="$API_PORT"
    API_PORT="$(next_available_port "$((API_PORT + 1))" "$CONSOLE_PORT")" \
        || fail "找不到可用的 loopback API 端口"
    echo "[提示] API 默认端口 $ORIGINAL_API_PORT 已占用，改用 $API_PORT"
fi
if port_in_use "$CONSOLE_PORT" || [[ "$CONSOLE_PORT" == "$API_PORT" ]]; then
    if (( CONSOLE_PORT_EXPLICIT )); then
        fail "显式指定的 Console 端口 $CONSOLE_PORT 已被占用或与 API 冲突"
    fi
    ORIGINAL_CONSOLE_PORT="$CONSOLE_PORT"
    CONSOLE_PORT="$(next_available_port "$((CONSOLE_PORT + 1))" "$API_PORT")" \
        || fail "找不到可用的 loopback Console 端口"
    echo "[提示] Console 默认端口 $ORIGINAL_CONSOLE_PORT 不可用，改用 $CONSOLE_PORT"
fi

CATALOG_COUNTS="$($PYTHON_BIN - "$CATALOG_PATH" <<'PY'
import sqlite3
import sys
from pathlib import Path

catalog = Path(sys.argv[1])
connection = sqlite3.connect(f"file:{catalog.as_posix()}?mode=ro&immutable=1", uri=True)
try:
    assets = connection.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0]
    collections = connection.execute("SELECT COUNT(*) FROM knowledge_datasets").fetchone()[0]
finally:
    connection.close()
print(f"{collections} Collection(s), {assets} Catalog Asset(s)")
PY
)" || fail "staged Catalog 不是可读的 Knowledge Catalog"

if (( CHECK_ONLY )); then
    echo "[通过] Knowledge 本地启动预检通过：$CATALOG_COUNTS"
    echo "[边界] local shadow only；activation_allowed=false"
    exit 0
fi

if [[ -d /private/tmp ]]; then
    RUN_TEMP_PARENT="/private/tmp"
else
    RUN_TEMP_PARENT="${TMPDIR:-/tmp}"
fi
RUN_DIR="$(mktemp -d "$RUN_TEMP_PARENT/puddingknowledge-local.XXXXXX")"
READY_FILE="$RUN_DIR/ready.json"
API_LOG="$RUN_DIR/api.log"
CONSOLE_LOG="$RUN_DIR/console.log"
BUILD_LOG="$RUN_DIR/build.log"
API_PID=""
CONSOLE_PID=""

show_log_tail() {
    local label="$1"
    local path="$2"
    if [[ -s "$path" ]]; then
        echo "--- $label 最近日志 ---" >&2
        tail -n 40 "$path" >&2
    fi
}

stop_child() {
    local pid="$1"
    [[ -n "$pid" ]] || return 0
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        local attempt
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    fi
    wait "$pid" 2>/dev/null || true
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    stop_child "$CONSOLE_PID"
    stop_child "$API_PID"
    if [[ "$KEEP_TEMP" == "1" ]]; then
        echo "[信息] 临时运行目录已保留: $RUN_DIR"
    elif [[ "$RUN_DIR" == "$RUN_TEMP_PARENT"/puddingknowledge-local.* ]]; then
        rm -rf -- "$RUN_DIR"
    fi
    if (( status == 0 )); then
        echo "[完成] Knowledge 本地服务已停止"
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'exit 0' INT TERM

echo "[1/4] 本地输入预检通过：$CATALOG_COUNTS"

if (( BUILD_CONSOLE )); then
    echo "[2/4] 构建 Knowledge Web..."
    if ! npm --prefix "$CONSOLE_PACKAGE" run build >"$BUILD_LOG" 2>&1; then
        show_log_tail "Knowledge Web build" "$BUILD_LOG"
        fail "Knowledge Web 构建失败"
    fi
else
    echo "[2/4] 跳过构建，复用现有 Knowledge Web production build"
fi
[[ -f "$CONSOLE_BUILD_ID" && ! -L "$CONSOLE_BUILD_ID" ]] \
    || fail "Knowledge Web build 不完整；请移除 --no-build 后重试"

CONSOLE_ORIGIN="http://127.0.0.1:$CONSOLE_PORT"
API_URL="http://127.0.0.1:$API_PORT"
CONSOLE_URL="$CONSOLE_ORIGIN/knowledge"

API_ARGS=(
    "$PYTHON_BIN"
    -m knowledge_platform.local
    --catalog "$CATALOG_PATH"
    --wiki-root "$WIKI_ROOT"
    --temp-dir "$RUN_DIR/server-data"
    --port "$API_PORT"
    --ready-file "$READY_FILE"
    --console-origin "$CONSOLE_ORIGIN"
)
if [[ -n "$REVIEW_QUEUE" ]]; then
    API_ARGS+=(--asset-binding-review-queue "$REVIEW_QUEUE")
fi

API_ENV=("PATH=$PATH" "PYTHONPATH=$REPO_ROOT/backend")
if [[ -n "$DATABASE_CONFIG" ]]; then
    API_ARGS+=(--database-config "$DATABASE_CONFIG")
    if [[ -n "$DATABASE_PASSWORD_ENV" ]]; then
        API_ENV+=("$DATABASE_PASSWORD_ENV=${!DATABASE_PASSWORD_ENV}")
    fi
fi

echo "[3/4] 启动 Knowledge API..."
env -i "${API_ENV[@]}" \
    "${API_ARGS[@]}" >"$API_LOG" 2>&1 &
API_PID=$!

api_ready=0
for (( second=1; second<=STARTUP_TIMEOUT; second++ )); do
    if [[ -f "$READY_FILE" ]] && grep -q '"status": "failed"' "$READY_FILE"; then
        show_log_tail "Knowledge API" "$API_LOG"
        fail "Knowledge API 初始化失败"
    fi
    if curl -fsS --max-time 1 "$API_URL/v1/spaces" >/dev/null 2>&1; then
        api_ready=1
        break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        show_log_tail "Knowledge API" "$API_LOG"
        fail "Knowledge API 提前退出"
    fi
    sleep 1
done
if (( ! api_ready )); then
    show_log_tail "Knowledge API" "$API_LOG"
    fail "Knowledge API 未在 ${STARTUP_TIMEOUT} 秒内就绪"
fi

READY_WIKI_PAGES="$($PYTHON_BIN - "$READY_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
pages = payload.get("pages", 0)
if not isinstance(pages, int) or pages < 0:
    raise SystemExit("invalid readiness page count")
print(pages)
PY
)" || fail "Knowledge API readiness 数据无效"

echo "[4/4] 启动 Knowledge Web..."
env -i PATH="$PATH" HOME="${HOME:-$RUN_DIR}" NODE_ENV=production \
    PLATFORM_API_URL="$API_URL" PORT="$CONSOLE_PORT" \
    npm --prefix "$CONSOLE_PACKAGE" run start >"$CONSOLE_LOG" 2>&1 &
CONSOLE_PID=$!

web_ready() {
    curl -fsS --max-time 1 "$CONSOLE_ORIGIN/knowledge" >/dev/null 2>&1 || return 1
    curl -fsS --max-time 1 "$CONSOLE_ORIGIN/v1/spaces" 2>/dev/null \
        | "$PYTHON_BIN" -c 'import json, sys; value = json.load(sys.stdin); raise SystemExit(0 if value.get("status") == "ok" else 1)' \
        >/dev/null 2>&1
}

console_ready=0
for (( second=1; second<=STARTUP_TIMEOUT; second++ )); do
    if web_ready; then
        console_ready=1
        break
    fi
    if ! kill -0 "$CONSOLE_PID" 2>/dev/null; then
        show_log_tail "Knowledge Web" "$CONSOLE_LOG"
        fail "Knowledge Web 提前退出"
    fi
    sleep 1
done
if (( ! console_ready )); then
    show_log_tail "Knowledge Web" "$CONSOLE_LOG"
    fail "Knowledge Web 未在 ${STARTUP_TIMEOUT} 秒内就绪"
fi

echo ""
echo "Knowledge Platform 本地测试环境已就绪"
echo "Web UI:  $CONSOLE_URL"
echo "API:     $API_URL"
echo "数据:    $CATALOG_COUNTS + $READY_WIKI_PAGES materialized Wiki Page(s)"
echo "边界:    local shadow only；activation_allowed=false"
echo "停止:    Ctrl+C"
echo ""

if (( OPEN_BROWSER )); then
    if command -v open >/dev/null 2>&1; then
        open "$CONSOLE_URL"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$CONSOLE_URL"
    else
        echo "[提示] 未找到系统浏览器打开命令，请手动访问 Web UI 地址"
    fi
fi

while true; do
    if ! kill -0 "$API_PID" 2>/dev/null; then
        show_log_tail "Knowledge API" "$API_LOG"
        fail "Knowledge API 已退出"
    fi
    if ! kill -0 "$CONSOLE_PID" 2>/dev/null; then
        show_log_tail "Knowledge Web" "$CONSOLE_LOG"
        fail "Knowledge Web 已退出"
    fi
    sleep 1
done
