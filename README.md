# Smart Order Management System

訂單管理 + 自動排程平台。Phase 1 已完成基礎建設（config、base entity、unified error envelope、ECS audit log、TDD demo、Bulletproof React、Docker Compose、GitHub Actions CI、開發規範）。

> **狀態：** Phase 1 完成；商業邏輯（Order CRUD / Scheduling / Auth）將在 Phase 2 加入。

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | React 18 + TypeScript + Vite + Tailwind v3 + shadcn/ui (Bulletproof React) |
| Backend  | Python 3.11 + FastAPI + SQLAlchemy 2.0 + Alembic |
| Worker   | Celery (Redis broker) |
| DB       | PostgreSQL 15 |
| Logging  | structlog (ECS-compatible JSON, correlation IDs) |
| Tooling  | uv (Python), pnpm (JS), Ruff, mypy --strict, ESLint Airbnb, pre-commit |
| Infra    | Docker Compose; GitHub Actions CI |

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
# 系統 Python 不會被動到；如果你機器上沒有 3.11.x，uv 會自己抓一份到 ~/.local/share/uv
cd backend && uv sync && cd ..

# Frontend
cd frontend && pnpm install && cd ..
```

### 步驟 5：啟動 db + redis

```bash
docker compose up -d db redis

# 等到兩個都 healthy（約 10 秒）
docker compose ps
```

### 步驟 6：跑 alembic（驗證 ORM 接到 DB）

```bash
cd backend && uv run alembic upgrade head
```

Phase 1 還沒任何 entity，所以沒 migration 會跑，但 `alembic_version` 表會被建立 — 這就是接通的證明：
```bash
docker compose exec db psql -U postgres -d smart_order -c "\dt"
```

### 步驟 7：開 backend API server

```bash
# 在 backend/ 目錄下
uv run uvicorn app.main:app --reload --port 8000
```

開另一個 terminal 測：
```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok"}
```

或直接瀏覽器開 [http://localhost:8000/docs](http://localhost:8000/docs) 看 Swagger UI。

### 步驟 8：開 frontend dev server

```bash
# 在 frontend/ 目錄下
pnpm dev
```

瀏覽器開 [http://localhost:5173](http://localhost:5173) 看 LoginForm（Phase 1 是 mock，按下去只會在 console 印假 token）。

### 步驟 9：跑測試

```bash
# Backend (Testcontainers 會起一個臨時 Postgres，需要 Docker Desktop 在跑)
cd backend && uv run pytest

# Frontend
cd frontend && pnpm test
```

---

## VS Code 設定（推薦）

按 `Ctrl+Shift+P` → **Python: Select Interpreter** → 選 `backend\.venv\Scripts\python.exe`。

不選的話，VS Code 會用系統 Python 找不到專案套件，編輯器到處紅線提示 — 但實際 lint/test 不受影響。

---

## 常用指令（用 make 一行搞定）

如果在 Git Bash 裡跑，可以用 Makefile 簡化：

| 目的 | Make 命令 | 等價直接命令 |
|---|---|---|
| 全部裝起來 | `make setup` | `cd backend && uv sync && cd ../frontend && pnpm install` |
| 啟動全部 | `make up` | `docker compose up -d` |
| 停止 | `make down` | `docker compose down` |
| 砍光 DB（DANGER） | `make nuke` | `docker compose down -v` |
| 看 logs | `make logs` | `docker compose logs -f --tail=100` |
| 看狀態 | `make ps` | `docker compose ps` |
| 跑 migration | `make migrate` | `docker compose exec backend alembic upgrade head` |
| 新增 migration | `make revision m="add orders"` | `docker compose exec backend alembic revision --autogenerate -m "add orders"` |
| 跑全部測試 | `make test` | 見下 |
| 全部 lint | `make lint` | 見下 |
| 自動 format | `make format` | 見下 |
| 全部目標 | `make help` | — |

> `make up` 會 build backend + worker + frontend container，第一次大約 3–5 分鐘（之後會 cache）。

如果不想用 make，直接跑：

```bash
# Backend lint + typecheck + test
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest

# Frontend lint + typecheck + test
cd frontend
pnpm lint
pnpm typecheck
pnpm test
```

---

## Repository layout

```
.
├── backend/        FastAPI service — see docs/DEVELOPMENT_GUIDELINES.md §Backend
├── frontend/       React/Vite SPA — Bulletproof React structure
├── docs/           架構與貢獻指南
│   ├── DEVELOPMENT_GUIDELINES.md   開發規範（命名 / TDD / API 錯誤格式 / 故障排除）
│   ├── HOW_TO_TEST.md              Phase 1 可以手動測試什麼、怎麼測
│   └── GITHUB_SETUP.md             把 repo 放上 GitHub + 設 CI + PR 流程
├── .github/        GitHub Actions CI workflow
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
| Alembic autogenerate 產生空 diff | 新加 entity 忘了在 `app/models/__init__.py` re-export | 補 `from app.models.order import Order` |
| 500 回 `{"detail": "..."}`（舊格式） | route 直接回 `JSONResponse` 沒走 handler | 改用 `raise HTTPException(...)` |
| Frontend HMR 在 Windows 偶爾沒反應 | Bind mount 在容器內變慢 | `docker compose up -d --force-recreate frontend` |
| VS Code 紅線「套件未安裝」 | Python 解譯器選錯 | Ctrl+Shift+P → Python: Select Interpreter → `backend\.venv\Scripts\python.exe` |

---

## 文件導引

- **[Project rules](docs/RULES.md)** ⚠️ — 架構與編碼**強制規範**（12-Factor、Bulletproof React、FastAPI Best Practices）。所有 PR 必須符合，不得違反。
- **[Development guidelines](docs/DEVELOPMENT_GUIDELINES.md)** — 開發 SOP、命名、TDD 流程、API 錯誤格式。
- **[How to test](docs/HOW_TO_TEST.md)** — Phase 1 你可以手動測試什麼、怎麼測。
- **[GitHub setup](docs/GITHUB_SETUP.md)** — 怎麼把 repo 放上 GitHub、CI 自動跑、PR 上看測試結果。
