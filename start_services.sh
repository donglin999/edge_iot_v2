#!/bin/bash
# 工业采集控制平台 - 服务启动脚本
# 用途：一键启动 Docker 服务和前后端应用

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

# PID 文件
PID_DIR="$PROJECT_ROOT/.pids"
DJANGO_PID="$PID_DIR/django.pid"
CELERY_PID="$PID_DIR/celery.pid"
FRONTEND_PID="$PID_DIR/frontend.pid"

# 日志目录
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$PID_DIR" "$LOG_DIR"

# 环境变量配置 - 从 backend/.env 文件加载
if [ -f "$BACKEND_DIR/.env" ]; then
    log_info "加载环境变量从 backend/.env"
    export $(grep -v '^#' "$BACKEND_DIR/.env" | xargs)
else
    # 默认配置（如果没有.env文件）
    log_warning "未找到 backend/.env 文件，使用默认配置"
    export DATABASE_URL="postgresql://iot_user:iot_password@localhost:5432/edge_iot"
    export REDIS_HOST="localhost"
    export REDIS_PORT="6379"
    export INFLUXDB_HOST="localhost"
    export INFLUXDB_PORT="8086"
    export INFLUXDB_TOKEN="my-super-secret-auth-token"
    export INFLUXDB_ORG="edge-iot"
    export INFLUXDB_BUCKET="iot-data"
fi

# 打印带颜色的消息
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 退出 conda 环境
deactivate_conda() {
    if [ -n "$CONDA_DEFAULT_ENV" ]; then
        log_info "检测到 Conda 环境: $CONDA_DEFAULT_ENV"
        log_info "正在退出 Conda 环境..."

        # 尝试多种方式退出 conda
        if command -v conda &> /dev/null; then
            # 方法1: 使用 conda deactivate
            for i in {1..10}; do
                if [ -n "$CONDA_DEFAULT_ENV" ]; then
                    conda deactivate 2>/dev/null || true
                else
                    break
                fi
            done
        fi

        # 方法2: 直接 unset 环境变量
        unset CONDA_DEFAULT_ENV
        unset CONDA_PREFIX
        unset CONDA_SHLVL

        log_success "已退出 Conda 环境"
    else
        log_info "未检测到 Conda 环境"
    fi
}

# 检查进程是否运行
is_running() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0  # 运行中
        else
            rm -f "$pid_file"
            return 1  # 未运行
        fi
    fi
    return 1  # 未运行
}

# 停止服务 - 改进版本
stop_service() {
    local service_name=$1
    local pid_file=$2
    local port=$3  # 可选参数：要检查的端口

    log_info "正在停止 $service_name..."

    # 1. 首先尝试从PID文件停止
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
            log_info "停止主进程 (PID: $pid)..."
            
            # 杀死进程组（包括子进程）
            kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
            
            # 等待进程退出
            local count=0
            while ps -p "$pid" > /dev/null 2>&1 && [ $count -lt 10 ]; do
                sleep 1
                count=$((count + 1))
            done

            # 如果还在运行，强制杀掉进程组
            if ps -p "$pid" > /dev/null 2>&1; then
                log_warning "强制终止 $service_name 进程组..."
                kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
                sleep 2
            fi
        fi
        rm -f "$pid_file"
    fi

    # 2. 根据服务类型查找并杀死相关进程
    case "$service_name" in
        *Django*|*django*)
            # 查找所有Django runserver进程
            local django_pids=$(ps aux | grep "manage.py runserver" | grep -v grep | awk '{print $2}')
            if [ -n "$django_pids" ]; then
                log_info "发现遗留的Django进程，正在清理..."
                echo "$django_pids" | xargs -r kill -9 2>/dev/null || true
            fi
            ;;
        *Celery*|*celery*)
            # 查找所有Celery worker进程
            local celery_pids=$(ps aux | grep "celery.*worker" | grep -v grep | awk '{print $2}')
            if [ -n "$celery_pids" ]; then
                log_info "发现遗留的Celery进程，正在清理..."
                echo "$celery_pids" | xargs -r kill -9 2>/dev/null || true
            fi
            ;;
        *前端*|*Frontend*|*frontend*)
            # 查找所有前端开发服务器进程
            local frontend_pids=$(ps aux | grep "vite\|npm run dev" | grep -v grep | awk '{print $2}')
            if [ -n "$frontend_pids" ]; then
                log_info "发现遗留的前端进程，正在清理..."
                echo "$frontend_pids" | xargs -r kill -9 2>/dev/null || true
            fi
            ;;
    esac

    # 3. 如果指定了端口，检查端口占用并清理
    if [ -n "$port" ]; then
        local port_pids=$(lsof -ti :$port 2>/dev/null || true)
        if [ -n "$port_pids" ]; then
            log_warning "端口 $port 仍被占用，正在强制清理..."
            echo "$port_pids" | xargs -r kill -9 2>/dev/null || true
            sleep 1
            
            # 再次检查
            port_pids=$(lsof -ti :$port 2>/dev/null || true)
            if [ -n "$port_pids" ]; then
                log_error "无法释放端口 $port，请手动检查"
            else
                log_success "端口 $port 已释放"
            fi
        fi
    fi

    log_success "$service_name 已停止"
}

# 停止所有服务
stop_all() {
    log_info "======================================="
    log_info "停止所有服务..."
    log_info "======================================="

    # 停止服务时指定端口进行彻底清理
    stop_service "Django 后端" "$DJANGO_PID" "8000"
    stop_service "Celery Worker" "$CELERY_PID"
    stop_service "前端开发服务器" "$FRONTEND_PID" "5173"

    log_info "停止 Docker 容器..."
    docker stop influxdb postgres-iot redis-iot 2>/dev/null || true

    # 额外清理：杀死所有可能遗留的相关进程
    log_info "清理所有相关进程..."
    pkill -f "manage.py runserver" 2>/dev/null || true
    pkill -f "celery.*worker" 2>/dev/null || true
    pkill -f "npm run dev" 2>/dev/null || true
    pkill -f "vite" 2>/dev/null || true

    log_success "所有服务已停止"
}

# 检查并启动 Docker 容器
start_docker_services() {
    log_info "======================================="
    log_info "启动 Docker 服务..."
    log_info "======================================="

    # 检查 Docker 是否可用
    if ! command -v docker &> /dev/null; then
        log_error "Docker 命令未找到，请安装 Docker"
        return 1
    fi

    # 检查并创建 Docker 网络
    if ! docker network inspect iot-network &> /dev/null; then
        log_info "创建 Docker 网络 iot-network..."
        docker network create iot-network
    fi

    # 启动 InfluxDB
    if docker ps -a --format "{{.Names}}" | grep -q "^influxdb$"; then
        if ! docker ps --format "{{.Names}}" | grep -q "^influxdb$"; then
            log_info "启动 InfluxDB 容器..."
            docker start influxdb
        else
            log_success "InfluxDB 容器已运行"
        fi
    else
        log_info "创建并启动 InfluxDB 容器..."
        docker run -d --name influxdb \
            --network iot-network \
            -p 8086:8086 \
            -e DOCKER_INFLUXDB_INIT_MODE=setup \
            -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
            -e DOCKER_INFLUXDB_INIT_PASSWORD=admin123456 \
            -e DOCKER_INFLUXDB_INIT_ORG=edge-iot \
            -e DOCKER_INFLUXDB_INIT_BUCKET=iot-data \
            -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=my-super-secret-auth-token \
            influxdb:2.7
    fi

    # 启动 PostgreSQL
    if docker ps -a --format "{{.Names}}" | grep -q "^postgres-iot$"; then
        if ! docker ps --format "{{.Names}}" | grep -q "^postgres-iot$"; then
            log_info "启动 PostgreSQL 容器..."
            docker start postgres-iot
        else
            log_success "PostgreSQL 容器已运行"
        fi
    else
        log_info "创建并启动 PostgreSQL 容器..."
        docker run -d --name postgres-iot \
            --network iot-network \
            -p 5432:5432 \
            -e POSTGRES_DB=edge_iot \
            -e POSTGRES_USER=iot_user \
            -e POSTGRES_PASSWORD=iot_password \
            postgres:15-alpine
    fi

    # 启动 Redis
    if docker ps -a --format "{{.Names}}" | grep -q "^redis-iot$"; then
        if ! docker ps --format "{{.Names}}" | grep -q "^redis-iot$"; then
            log_info "启动 Redis 容器..."
            docker start redis-iot
        else
            log_success "Redis 容器已运行"
        fi
    else
        log_info "创建并启动 Redis 容器..."
        docker run -d --name redis-iot \
            --network iot-network \
            -p 6379:6379 \
            redis:7-alpine
    fi

    # 等待服务启动
    log_info "等待 Docker 服务启动..."
    sleep 5

    # 验证服务
    if docker exec postgres-iot pg_isready -U iot_user &> /dev/null; then
        log_success "PostgreSQL 已就绪"
    else
        log_warning "PostgreSQL 可能未就绪"
    fi

    if docker exec redis-iot redis-cli ping &> /dev/null; then
        log_success "Redis 已就绪"
    else
        log_warning "Redis 可能未就绪"
    fi

    if docker exec influxdb influx ping &> /dev/null; then
        log_success "InfluxDB 已就绪"
    else
        log_warning "InfluxDB 可能未就绪"
    fi

    log_success "Docker 服务启动完成"
}

# 启动 Django 后端
start_django() {
    log_info "======================================="
    log_info "启动 Django 后端服务..."
    log_info "======================================="

    if is_running "$DJANGO_PID"; then
        log_warning "Django 后端已在运行中"
        return 0
    fi

    # 检查 Python3 是否可用
    if ! command -v python3 &> /dev/null; then
        log_error "python3 命令未找到，请安装 Python 3"
        return 1
    fi

    cd "$BACKEND_DIR"

    # 检查 manage.py
    if [ ! -f "manage.py" ]; then
        log_error "未找到 Django manage.py 文件"
        return 1
    fi

    # 运行数据库迁移
    log_info "检查数据库迁移..."
    python3 manage.py migrate --noinput &> /dev/null || log_warning "数据库迁移失败，继续启动..."

    # 启动服务（后台运行）
    log_info "启动 Django 服务器 (端口 8000)..."
    nohup python3 manage.py runserver 0.0.0.0:8000 \
        > "$LOG_DIR/django.log" 2>&1 &

    echo $! > "$DJANGO_PID"

    # 等待启动
    sleep 3

    if is_running "$DJANGO_PID"; then
        log_success "Django 后端已启动 (PID: $(cat $DJANGO_PID))"
        log_info "访问地址: http://localhost:8000"
        log_info "日志文件: $LOG_DIR/django.log"
    else
        log_error "Django 后端启动失败，请查看日志: $LOG_DIR/django.log"
        return 1
    fi

    cd "$PROJECT_ROOT"
}

# 启动 Celery Worker
start_celery() {
    log_info "======================================="
    log_info "启动 Celery Worker..."
    log_info "======================================="

    if is_running "$CELERY_PID"; then
        log_warning "Celery Worker 已在运行中"
        return 0
    fi

    cd "$BACKEND_DIR"

    # 检查 Celery 是否可用
    if ! command -v celery &> /dev/null; then
        log_error "celery 命令未找到"
        log_info "请运行: pip3 install celery"
        return 1
    fi

    log_info "启动 Celery Worker (solo pool)..."
    nohup celery -A control_plane worker -l info --pool=solo \
        > "$LOG_DIR/celery.log" 2>&1 &

    echo $! > "$CELERY_PID"

    # 等待启动
    sleep 5

    if is_running "$CELERY_PID"; then
        log_success "Celery Worker 已启动 (PID: $(cat $CELERY_PID))"
        log_info "日志文件: $LOG_DIR/celery.log"
    else
        log_error "Celery Worker 启动失败，请查看日志: $LOG_DIR/celery.log"
        return 1
    fi

    cd "$PROJECT_ROOT"
}

# 启动前端开发服务器
start_frontend() {
    log_info "======================================="
    log_info "启动前端开发服务器..."
    log_info "======================================="

    if is_running "$FRONTEND_PID"; then
        log_warning "前端开发服务器已在运行中"
        return 0
    fi

    cd "$FRONTEND_DIR"

    # 检查 npm 是否可用
    if ! command -v npm &> /dev/null; then
        log_error "npm 命令未找到，请安装 Node.js"
        return 1
    fi

    # 检查 node_modules
    if [ ! -d "node_modules" ]; then
        log_warning "未找到 node_modules，正在安装依赖..."
        npm install
    fi

    log_info "启动 Vite 开发服务器 (端口 5173)..."
    nohup npm run dev \
        > "$LOG_DIR/frontend.log" 2>&1 &

    echo $! > "$FRONTEND_PID"

    # 等待启动
    sleep 5

    if is_running "$FRONTEND_PID"; then
        log_success "前端开发服务器已启动 (PID: $(cat $FRONTEND_PID))"
        log_info "访问地址: http://localhost:5173"
        log_info "日志文件: $LOG_DIR/frontend.log"
    else
        log_error "前端开发服务器启动失败，请查看日志: $LOG_DIR/frontend.log"
        return 1
    fi

    cd "$PROJECT_ROOT"
}

# 启动所有服务
start_all() {
    log_info "======================================="
    log_info "工业采集控制平台 - 启动所有服务"
    log_info "======================================="

    # 退出 Conda 环境
    deactivate_conda

    # 启动 Docker 服务
    start_docker_services

    # 依次启动应用服务
    start_django
    start_celery
    start_frontend

    echo ""
    log_success "======================================="
    log_success "所有服务启动完成！"
    log_success "======================================="
    echo ""
    log_info "服务地址："
    echo "  - 前端界面: http://localhost:5173"
    echo "  - 后端 API: http://localhost:8000"
    echo "  - API 文档: http://localhost:8000/api/schema/swagger-ui/"
    echo ""
    log_info "Docker 服务："
    echo "  - PostgreSQL: localhost:5432 (DB: edge_iot)"
    echo "  - Redis:      localhost:6379"
    echo "  - InfluxDB:   localhost:8086 (Org: edge-iot, Bucket: iot-data)"
    echo ""
    log_info "日志文件："
    echo "  - Django:   $LOG_DIR/django.log"
    echo "  - Celery:   $LOG_DIR/celery.log"
    echo "  - Frontend: $LOG_DIR/frontend.log"
    echo ""
    log_info "管理命令："
    echo "  - 停止服务:  $0 stop"
    echo "  - 重启服务:  $0 restart"
    echo "  - 查看状态:  $0 status"
    echo "  - 查看日志:  $0 logs [django|celery|frontend]"
    echo ""
}

# 重启所有服务
restart_all() {
    log_info "======================================="
    log_info "重启所有服务..."
    log_info "======================================="

    stop_all
    sleep 2
    start_all
}

# 查看服务状态
show_status() {
    log_info "======================================="
    log_info "服务运行状态"
    log_info "======================================="

    echo ""

    # Docker 服务
    log_info "Docker 容器："
    for container in influxdb postgres-iot redis-iot; do
        if docker ps --format "{{.Names}}" | grep -q "^${container}$"; then
            log_success "  - $container: 运行中"
        else
            log_error "  - $container: 未运行"
        fi
    done

    echo ""

    # Django
    if is_running "$DJANGO_PID"; then
        log_success "Django 后端: 运行中 (PID: $(cat $DJANGO_PID))"
    else
        log_error "Django 后端: 未运行"
    fi

    # Celery
    if is_running "$CELERY_PID"; then
        log_success "Celery Worker: 运行中 (PID: $(cat $CELERY_PID))"
    else
        log_error "Celery Worker: 未运行"
    fi

    # Frontend
    if is_running "$FRONTEND_PID"; then
        log_success "前端开发服务器: 运行中 (PID: $(cat $FRONTEND_PID))"
    else
        log_error "前端开发服务器: 未运行"
    fi

    echo ""
}

# 查看日志
show_logs() {
    local service=$1

    case $service in
        django)
            log_info "Django 后端日志 (最后 50 行):"
            echo "======================================="
            tail -n 50 "$LOG_DIR/django.log" 2>/dev/null || log_error "日志文件不存在"
            ;;
        celery)
            log_info "Celery Worker 日志 (最后 50 行):"
            echo "======================================="
            tail -n 50 "$LOG_DIR/celery.log" 2>/dev/null || log_error "日志文件不存在"
            ;;
        frontend)
            log_info "前端开发服务器日志 (最后 50 行):"
            echo "======================================="
            tail -n 50 "$LOG_DIR/frontend.log" 2>/dev/null || log_error "日志文件不存在"
            ;;
        *)
            log_error "未知服务: $service"
            log_info "可用选项: django, celery, frontend"
            ;;
    esac
}

# 主程序
main() {
    case "${1:-start}" in
        start)
            start_all
            ;;
        stop)
            stop_all
            ;;
        restart)
            restart_all
            ;;
        status)
            show_status
            ;;
        logs)
            if [ -z "$2" ]; then
                log_error "请指定服务名: django, celery, frontend"
                exit 1
            fi
            show_logs "$2"
            ;;
        *)
            echo "用法: $0 {start|stop|restart|status|logs [service]}"
            echo ""
            echo "命令说明："
            echo "  start    - 启动所有服务"
            echo "  stop     - 停止所有服务"
            echo "  restart  - 重启所有服务"
            echo "  status   - 查看服务状态"
            echo "  logs     - 查看服务日志"
            echo ""
            echo "示例："
            echo "  $0 start              # 启动所有服务"
            echo "  $0 restart            # 重启所有服务"
            echo "  $0 status             # 查看状态"
            echo "  $0 logs django        # 查看 Django 日志"
            echo "  $0 logs celery        # 查看 Celery 日志"
            exit 1
            ;;
    esac
}

# 执行主程序
main "$@"
