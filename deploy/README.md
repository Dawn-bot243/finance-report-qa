# 部署说明（Docker Compose + Nginx 反代 + 鉴权 + 限流）

## 组成

| 服务 | 作用 | 端口 |
|------|------|------|
| `backend` | FastAPI（问答 / Agent / 指标 / 对比） | 容器内 8000 |
| `frontend` | Streamlit 四页签界面 | 容器内 8501 |
| `nginx` | 反向代理、限流、统一入口 | 宿主机 8080 |

访问方式：

- `http://localhost:8080/` → Streamlit 界面
- `http://localhost:8080/api/health` → 后端健康检查
- `http://localhost:8080/api/ask`、`/api/agent` 等 → 后端接口

## 前置条件

1. Docker 24+ 与 Docker Compose v2
2. 项目根目录已有：
   - `db/finance.db`（SQLite，含页面文本与指标）
   - `raw/pdf/`（PDF 原文，用于出处页码）
   - `.env`（`DEEPSEEK_API_KEY`、`SILICONFLOW_API_KEY`、`DATASINK_API_KEY`）

数据不进镜像，由 compose 挂载（`db/` 103MB、`raw/` 212MB）。

## 启动

```bash
cd D:/金融项目
cp .env.example .env          # 若无 .env，先填好各 API key
export API_KEY=$(openssl rand -hex 16)      # 设置访问密钥（可选，见下）
export RATE_LIMIT_PER_MINUTE=60             # 每 IP 每分钟上限（0 = 关闭）
docker compose up -d --build
docker compose logs -f backend
```

首次构建约 5–10 分钟（安装依赖）。之后启动秒级。

停止 / 清理：

```bash
docker compose down            # 停止并删除容器（数据卷为挂载，不受影响）
docker compose up -d --build   # 改了代码后重建
```

## 鉴权

- 环境变量 `API_KEY` 非空时启用：所有非公开接口需带 `X-API-Key` 或 `Authorization: Bearer <key>`
- 公开路径：`/health`、`/docs`、`/openapi.json`（便于探活与调试）
- Streamlit 容器读取同一个 `API_KEY`，自动在请求头中携带
- 未设置 `API_KEY` 则不启用鉴权（仅适合本地开发）

验证：

```bash
curl -s http://localhost:8080/api/health                      # 200（公开）
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/companies   # 401（未带 key）
curl -s -H "X-API-Key: $API_KEY" http://localhost:8080/api/companies           # 200
```

## 限流

两层：

1. **Nginx 层**（`deploy/nginx.conf`）：
   - `/api/`：5 req/s，burst 10（Agent 单次请求内部耗时不影响该限流）
   - `/`：20 req/s，burst 40
   - 超限返回 429
2. **应用层**（`src/api/security.py`）：`RATE_LIMIT_PER_MINUTE` 每 IP 每分钟固定窗口，
   默认 60；用于绕过反代直连后端时兜底

调整：

```bash
# 应用层：改环境变量
RATE_LIMIT_PER_MINUTE=120 docker compose up -d backend
# Nginx 层：改 deploy/nginx.conf 的 rate= 后
docker compose restart nginx
```

## 反代要点

- `location /api/` 重写去掉 `/api` 前缀后转发到 FastAPI
- Streamlit 需要 WebSocket，`location /` 已配置 `Upgrade` / `Connection` 头
- `proxy_read_timeout` 设为 600s：Agent 多轮工具调用、辩论报告可能持续数分钟
- 真实 IP 通过 `X-Real-IP` 传给后端，应用层限流据此分桶

## 常见问题

| 现象 | 原因 / 处理 |
|------|------------|
| 容器启动报 `db/finance.db` 不存在 | 先在宿主机跑完阶段一流水线，生成数据库 |
| `/api/agent` 返回 502 且日志超时 | 模型 API（DeepSeek/硅基流动）慢或不可达；检查 `.env` 与网络 |
| Streamlit 页面空白 | 检查 nginx 是否转发 WebSocket；`docker compose logs nginx` |
| 401 但已设置 API_KEY | 前端容器未重启，未读到新环境变量：`docker compose up -d frontend` |
| 429 频繁 | 调大 `RATE_LIMIT_PER_MINUTE` 或 nginx `rate`/`burst` |
