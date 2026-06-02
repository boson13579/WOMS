# Smart Order Management System

晶圓代工訂單管理 + 智慧排程平台。前後端、worker、即時通知、可觀測性、AWS 部署全套打通。

> **Live demo**：[https://d2eo1ky9o3esf3.cloudfront.net](https://d2eo1ky9o3esf3.cloudfront.net)（隨 EKS deploy 啟停）
> **Demo 帳號**：見 `terraform output -raw admin_password`（或 ops 那邊問）。

---

## What's built

| 區塊 | 範圍 |
|---|---|
| **認證** | JWT (HttpOnly cookie) + 4 角色 (root / scheduler / order_manager / viewer) + RBAC 中介層 |
| **訂單** | CRUD + 取消 + 軟刪 + 樂觀鎖 (`version_id`) + 每日 atomic seq 取號 (`order_daily_seq`) |
| **排程** | Celery worker + Redis queue + 兩棵 segment tree (capacity / deadline) + EDF + batch admission 二分搜 + materializer/rebuild + 跨日 `advance_day` (Celery Beat, Asia/Taipei) |
| **WebSocket** | 訂單 / 排程事件即時推播 (`/api/v1/ws`)，多 replica 跨 pod 透過 Redis pub/sub |
| **Audit log** | 每筆 user 觸發狀態變動寫 `audit_logs`（actor / before / after），root 可篩 actor / action / resource / time |
| **Dashboard** | Scheduler 狀態 / 30 天 capacity 折線 / Pending Ops queue / Orders by status，全部 WS 即時更新 |
| **Observability** | RED (rate/error/duration) + Schedule Lag P95 業務 SLI + USE (DB pool / Redis / Live WS connections) + Top endpoints；DB pool 跨 backend pod 聚合 |
| **AWS 部署** | EKS (multi-AZ) + ALB + CloudFront + RDS Postgres + ElastiCache + ECR + GitHub Actions CD（push main → image build → roll deployments）|
| **CI** | 每 PR 過 `ruff` + `mypy --strict` + `pytest` (testcontainers Postgres) + `eslint` + `tsc` + `vitest` 才能 merge |

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | React 18 + TypeScript + Vite + Tailwind v3 + shadcn/ui (Bulletproof React layout) |
| Backend  | Python 3.11 + FastAPI + SQLAlchemy 2.0 + Alembic + Pydantic v2 |
| Worker   | Celery + Celery Beat (Redis broker / result backend) |
| DB       | PostgreSQL 15 |
| Cache / Queue | Redis 7 (queue + pub/sub + sorted-set metrics) |
| Logging  | structlog (ECS JSON, X-Request-Id correlation) |
| Tooling  | uv (Python), pnpm (JS), Ruff, mypy --strict, ESLint Airbnb, pre-commit |
| Infra (local) | Docker Compose + nginx LB (2 backend replicas) |
| Infra (prod) | Terraform → AWS EKS + ALB + CloudFront + RDS + ElastiCache + ECR |

---

## Prerequisites

需要：**Docker Desktop**、**Node.js ≥20**、**git**。Python / uv / pnpm 等其他工具會在下面的步驟自動裝起來，不用先準備。

| Tool | Version | 用途 |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | 24+ | 跑 Postgres / Redis / 整套 stack |
| [Node.js](https://nodejs.org) | ≥ 20 (推薦 LTS) | 前端 + 安裝 pnpm |
| git | 任何版本 | 版控；Git Bash 自帶 `make` |

> **Windows 11 使用者注意：** PowerShell 打 `bash` 會進 Git Bash（MSYS）。所有指令都在 Git Bash 裡跑沒問題；**不需要 WSL2**。

---

## First-clone walkthrough（Windows 11 + Git Bash 實測流程）

### 步驟 0：clone

```bash
git clone <repo-url> smart-order
cd smart-order
```

### 步驟 1：裝 pnpm

```bash
npm install -g pnpm@9.12.3
pnpm --version    # 應該顯示 9.12.3
```

> ❌ 不要用 `corepack enable` — 它會嘗試寫入 `C:\Program Files\nodejs\` 需要 admin 權限。

### 步驟 2：裝 uv（Python 套件管理工具）

PowerShell 一行裝好：
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

裝完後 `uv` 在 `C:\Users\<你>\.local\bin\`，**Git Bash 預設找不到**。把以下加進 `~/.bashrc`：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
echo 'export PYTHONUTF8=1' >> ~/.bashrc          # Windows cp950 locale 防呆
echo 'unset VIRTUAL_ENV' >> ~/.bashrc            # 避免 uv 警告
source ~/.bashrc
uv --version    # 應該顯示 0.5+
```

### 步驟 3：複製 .env

```bash
cp .env.example .env
```

`.env.example` 裡的密碼是隨機產生的開發用值，**正式部署前一定要重產**：
```bash
python -c "import secrets; print(secrets.token_hex(24))"      # POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"  # JWT_SECRET
```

### 步驟 4：裝 backend + frontend 依賴

```bash
# Backend：uv 依 backend/.python-version（鎖在 3.11.9）自動下載對應直譯器
cd backend && uv sync && cd ..

# Frontend
cd frontend && pnpm install && cd ..
```

### 步驟 5：一鍵啟動全套

```bash
docker compose up -d

# 等待全部 healthy（約 30-60 秒第一次 build）
docker compose ps
```

跑起來會有：
- `db` (postgres:15) — :5432
- `redis` (redis:7) — :6379
- `backend` × 2 (FastAPI + uvicorn) — 透過 nginx LB 對外
- `nginx` (load balancer 給兩個 backend) — :8000
- `worker` (Celery) — 處理排程
- `beat` (Celery Beat) — 每日 00:00 Asia/Taipei 觸發 `advance_day_task`
- `frontend` (Vite dev server) — :5173

### 步驟 6：跑 alembic migration

```bash
docker compose exec backend alembic upgrade head
```

### 步驟 7：建管理員（demo 用）

```bash
docker compose exec backend python -c "
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole
db = SessionLocal()
db.add(User(
    username='admin', email='admin@example.com',
    password_hash=hash_password('Password123'),
    role=UserRole.root, is_active=True,
))
db.commit()
"
```

### 步驟 8：玩

| URL | 內容 |
|---|---|
| [http://localhost:5173](http://localhost:5173) | 前端 SPA — 用 `admin / Password123` 登入 |
| [http://localhost:8000/docs](http://localhost:8000/docs) | Swagger UI（API 對照） |
| [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health) | health check |

登入後可以：
- 建 / 改 / 刪 / 取消 訂單
- 看 Dashboard（30 天 capacity、Pending Ops、Orders by status）
- 看 Observability（RED / USE / Schedule Lag P95 / Live Connections）
- 看 Audit Log（root 角色）
- 看 Calendar 視圖（含 pin/unpin、today 高亮、訂單顏色標）

---

## 跑測試

```bash
# Backend (Testcontainers 起臨時 Postgres + Redis，需要 Docker)
cd backend && uv run pytest

# Frontend
cd frontend && pnpm test
```

預期數量約：backend ~500 tests、frontend ~400 tests，全綠。

---

## 常用指令（Makefile）

| 目的 | Make 命令 |
|---|---|
| 全部裝起來 | `make setup` |
| 啟動全部 | `make up` |
| 停止 | `make down` |
| 砍光 DB（DANGER） | `make nuke` |
| 看 logs | `make logs` |
| 看狀態 | `make ps` |
| 跑 migration | `make migrate` |
| 新增 migration | `make revision m="add orders"` |
| 跑全部測試 | `make test` |
| 全部 lint | `make lint` |
| 自動 format | `make format` |
| 全部目標 | `make help` |

---

## Repository layout

```
.
├── backend/                    FastAPI + Celery worker
│   ├── app/
│   │   ├── api/v1/             REST + WebSocket endpoints (auth, orders, schedule, system, ws, audit, notifications, users)
│   │   ├── core/               config / db / security / audit helper / logging / RED middleware
│   │   ├── models/             SQLAlchemy entities (Order, User, AuditLog, OrderDailySeq, ScheduleDailyCapacity, ...)
│   │   ├── repositories/       純 DB 存取
│   │   ├── services/           商業邏輯（order / scheduling / schedule_queue / compound_finalize / system / red_metrics / schedule_lag / pod_stats / ...）
│   │   ├── schemas/            Pydantic DTOs
│   │   └── workers/            Celery tasks（scheduling.run / advance_day / materialize / rebuild / audit_cleanup）
│   ├── alembic/                migration 歷史
│   └── tests/                  ~500 tests (testcontainers Postgres)
├── frontend/                   React + Vite SPA (Bulletproof React)
│   └── src/features/           auth / orders / dashboard / observability / audit / notifications / users
├── infra/                      Terraform — EKS / ALB / CloudFront / RDS / ElastiCache / IAM
├── k8s/                        Kubernetes manifests — configmap / deployment / service / ingress / migration-job
├── docker/                     local-only docker assets (nginx config 給 backend LB)
├── docs/                       架構與貢獻指南
│   ├── ARCHITECTURE.md         ⭐ 從這裡開始 — 整個專案的 mental model
│   ├── RULES.md                ⚠️ 強制編碼規範（12-Factor / Bulletproof React / FastAPI Best Practices）
│   ├── DEVELOPMENT_GUIDELINES.md  開發 SOP、命名、TDD、API 錯誤格式
│   ├── scheduling-integration.md  排程 API 怎麼用（隊友看的）
│   ├── scheduling.md            排程內部實作（要改 worker code 才需要看）
│   ├── DEPLOY.md                AWS 部署 SOP
│   ├── FRONTEND_SPEC.md
│   ├── GITHUB_SETUP.md
│   └── HOW_TO_TEST.md
├── .github/workflows/          CI (ci.yml) + CD (deploy.yml, GitHub Actions → ECR → EKS rollout)
└── docker-compose.yml
```

---

## 故障排除（Windows 上常見坑）

| 症狀 | 原因 | 解法 |
|---|---|---|
| `uv: command not found` | `~/.local/bin` 不在 PATH | `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc` |
| `'cp950' codec can't decode byte` | Windows 中文版預設 locale 解 UTF-8 文字爆掉 | `export PYTHONUTF8=1`（或寫進 `~/.bashrc`） |
| `corepack: EPERM operation not permitted` | Corepack 想寫 `C:\Program Files\nodejs\` | 改用 `npm install -g pnpm@9.12.3` |
| `VIRTUAL_ENV does not match` 警告 | 系統環境變數殘留 | `unset VIRTUAL_ENV`（或 `~/.bashrc`） |
| `make up` 失敗 `POSTGRES_PASSWORD must be set` | `.env` 沒建 | `cp .env.example .env` |
| `pytest` 卡在 `Pulling postgres:15-alpine` | Docker daemon 沒開 | 開 Docker Desktop |
| Alembic autogenerate 產生空 diff | 新加 entity 忘了在 `app/models/__init__.py` re-export | 補 `from app.models.<x> import <X>` |
| 500 回 `{"detail": "..."}`（舊格式） | route 直接回 `JSONResponse` 沒走 unified handler | 改用 `raise HTTPException(...)` |
| Frontend HMR 在 Windows 偶爾沒反應 | Bind mount 在容器內變慢 | `docker compose up -d --force-recreate frontend` |
| Nginx 卡舊 backend IP（502） | Local nginx upstream 是 static IP，backend 重啟換 IP 後沒重 resolve | `docker compose restart nginx` |
| Observability 只看到一個 pod 的 stats | Pod 是 publish-on-request — 等 ~6 秒讓 LB 輪到另一 pod 就會兩個都出現 | 等或刷新 |
| VS Code 紅線「套件未安裝」 | Python 解譯器選錯 | Ctrl+Shift+P → Python: Select Interpreter → `backend\.venv\Scripts\python.exe` |

---

## 文件導引

從 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 開始 — 拿到整個專案的 mental map。再依需要：

- **[Project rules](docs/RULES.md)** ⚠️ — 12-Factor / Bulletproof React / FastAPI 嚴格分層 — **所有 PR 必須符合**。
- **[Development guidelines](docs/DEVELOPMENT_GUIDELINES.md)** — 命名、TDD、API 錯誤格式、新增 endpoint 的 SOP。
- **[Scheduling integration guide](docs/scheduling-integration.md)** ⭐ — 跟排程模組整合的隊友（Order CRUD / 前端 / Ops）看這份就夠。
- **[Scheduling internals](docs/scheduling.md)** — 排程模組內部實作（segment tree、EDF、batch admission、race fix）。要動 worker code 才需要看。
- **[Deploy](docs/DEPLOY.md)** — Terraform → AWS EKS 的部署 SOP。
- **[How to test](docs/HOW_TO_TEST.md)** — 怎麼手動測完整 user flow。
- **[Frontend spec](docs/FRONTEND_SPEC.md)** — 前端各頁面的設計規格。
- **[GitHub setup](docs/GITHUB_SETUP.md)** — repo 上 GitHub、CI / CD、PR 流程。

---

## 開發 / Production 環境差異

| 維度 | Local (docker compose) | Production (AWS) |
|---|---|---|
| 入口 | `localhost:8000` → nginx → 2× backend | CloudFront → ALB → 2× backend pod |
| DB | Postgres container | RDS Postgres `db.t3.micro` (single-AZ) |
| Redis | Redis container | ElastiCache (single node) |
| Worker | 1 worker container | 1 worker pod |
| Beat | 1 beat container | 1 beat pod (singleton, Recreate strategy) |
| Frontend | Vite dev server (`:5173`) | nginx 靜態 build，從 CloudFront 提供 |
| Secrets | `.env` | k8s Secret (`woms-secrets`)，從 Terraform output 灌入 |
| Pool 設定 | `DB_POOL_SIZE=50 / OVERFLOW=30` | `DB_POOL_SIZE=20 / OVERFLOW=10`（受 RDS `max_connections=85` 限制） |
| CD 觸發 | `docker compose up -d` 手動 | push to `main` → GitHub Actions → ECR → `kubectl set image` |

詳見 [`docs/DEPLOY.md`](docs/DEPLOY.md)。
