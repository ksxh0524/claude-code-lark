#!/bin/bash
# Claude Code Lark Bot - 一键启动/停止脚本
# 双击即可运行，支持 start / stop / restart / status

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 确保 poetry 可用：自动搜索常见安装路径
find_poetry() {
    if command -v poetry &>/dev/null; then
        return 0
    fi
    for p in \
        "$HOME/Library/Python/"*/bin \
        "$HOME/.local/bin" \
        "$HOME/.poetry/bin" \
        "/opt/homebrew/bin" \
        "/usr/local/bin"; do
        if [ -x "$p/poetry" ]; then
            export PATH="$p:$PATH"
            return 0
        fi
    done
    return 1
}

if ! find_poetry; then
    echo "[ERROR] 未找到 poetry，请先安装: https://python-poetry.org/docs/#installation"
    echo ""
    echo "按回车键退出..."
    read -r
    exit 1
fi
BOT_NAME="claude-bot"
PID_FILE="$PROJECT_DIR/.bot.pid"
LOG_FILE="$PROJECT_DIR/bot.log"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

cd "$PROJECT_DIR"

# 检查 poetry 是否可用
check_poetry() {
    if ! command -v poetry &>/dev/null; then
        error "未找到 poetry，请先安装: https://python-poetry.org/docs/#installation"
        exit 1
    fi
}

# 获取运行中的 PID
get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

# 启动
do_start() {
    local pid
    if pid=$(get_pid); then
        warn "Bot 已在运行 (PID: $pid)"
        return 0
    fi

    check_poetry

    info "正在启动 $BOT_NAME ..."
    nohup poetry run "$BOT_NAME" >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # 等待一下确认进程存活
    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        ok "$BOT_NAME 已启动 (PID: $new_pid)"
        ok "日志文件: $LOG_FILE"
    else
        error "启动失败，请查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# 停止
do_stop() {
    local pid
    if ! pid=$(get_pid); then
        warn "$BOT_NAME 未在运行"
        return 0
    fi

    info "正在停止 $BOT_NAME (PID: $pid) ..."
    kill "$pid" 2>/dev/null || true

    # 等待进程退出
    local i=0
    while [ $i -lt 10 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            ok "$BOT_NAME 已停止"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done

    # 超时强制 kill
    warn "进程未响应，强制终止 ..."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    ok "$BOT_NAME 已强制停止"
}

# 查看状态
do_status() {
    local pid
    if pid=$(get_pid); then
        ok "$BOT_NAME 正在运行 (PID: $pid)"
    else
        info "$BOT_NAME 未在运行"
    fi
}

# 查看日志
do_log() {
    if [ -f "$LOG_FILE" ]; then
        tail -50 "$LOG_FILE"
    else
        info "暂无日志"
    fi
}

# 交互菜单（无参数时显示）
show_menu() {
    echo ""
    echo -e "${CYAN}=== Claude Code Lark Bot ===${NC}"
    echo ""
    echo "  1) 启动 Bot"
    echo "  2) 停止 Bot"
    echo "  3) 重启 Bot"
    echo "  4) 查看状态"
    echo "  5) 查看日志"
    echo "  0) 退出"
    echo ""
    read -rp "请选择 [0-5]: " choice
    case "$choice" in
        1) do_start ;;
        2) do_stop ;;
        3) do_stop; echo; do_start ;;
        4) do_status ;;
        5) do_log ;;
        0) exit 0 ;;
        *) error "无效选择"; exit 1 ;;
    esac
}

# 主入口
case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; echo; do_start ;;
    status)  do_status ;;
    log)     do_log ;;
    *)       show_menu ;;
esac
