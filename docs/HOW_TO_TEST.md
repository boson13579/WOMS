# 怎麼測試 Phase 1

Phase 1 還沒任何商業邏輯（沒有 Order、Auth、Scheduling），但**基礎建設已完備**。這份文件列出你目前可以實際驗證的功能，從快到慢、從輕到重。

> 前提：照 [README.md](../README.md) 的「First-clone walkthrough」走完步驟 1–6（裝完依賴、`.env` 已建立、db + redis 在跑）。

---

## 0. 一鍵跑全部測試（最快）

```bash
cd backend && uv run ruff check . && uv run mypy app && uv run pytest
cd ../frontend && pnpm lint && pnpm typecheck && pnpm test
```

預期：

- **backend ruff** → `All checks passed!`
- **backend mypy** → `Success: no issues found in 14 source files`
- **backend pytest** → `3 passed`、coverage ≥ 85%
- **frontend lint/typecheck** → 無輸出（即無錯）
- **frontend test** → `No test files found, exiting with code 0`（Phase 1 還沒前端測試）

如果以上全綠，整個 toolchain 是健康的。

---

## 1. 自動化測試（CI 等價）

### 1.1 Backend pytest（含 Testcontainers）

```bash
cd backend
uv run pytest -v
```

預期看到三個測試通過：

```
tests/api/test_health.py::test_health_returns_200 PASSED
tests/api/test_health.py::test_health_returns_ok_payload PASSED
tests/api/test_health.py::test_unknown_route_uses_unified_error_envelope PASSED
```

第三個測試特別重要 — 它驗證即使是 Starlette 自動產生的 404（不是你手動 raise 的），也會被統一錯誤格式包起來。

> **第一次跑會比較久（10–30 秒）**：testcontainers 要拉 postgres:15-alpine。第二次起就快了。

### 1.2 Backend type check

```bash
cd backend
uv run mypy app
```

任何型別錯誤都會 fail。`--strict` 模式很嚴 — 試試把 `app/api/v1/health.py` 裡 `def get_health() -> HealthResponse:` 的回傳註記拿掉就會看到失敗。

### 1.3 Backend lint + format

```bash
cd backend
uv run ruff check .              # lint
uv run ruff format --check .     # format
uv run ruff check --fix .        # 自動修可修的
uv run ruff format .             # 套用 format
```

### 1.4 Frontend test

```bash
cd frontend
pnpm test          # vitest
pnpm lint          # ESLint Airbnb + strict TS
pnpm typecheck     # tsc --noEmit
```

### 1.5 一次跑完全部（用 Make）

```bash
make test          # backend + frontend tests
make lint          # backend + frontend lint
```

---

## 2. 手動驗證 API（最有趣的部分）

開兩個 terminal：

```bash
# Terminal 1：開 db + redis
docker compose up -d db redis

# Terminal 2：開 backend
cd backend && uv run uvicorn app.main:app --reload --port 8000
```

然後測：

### 2.1 Health endpoint

```bash
curl -i http://localhost:8000/api/v1/health
```

預期回應：
```
HTTP/1.1 200 OK
x-correlation-id: <一個隨機 UUID>
content-type: application/json

{"status":"ok"}
```

**重點看 `X-Correlation-ID` header** — 這是 logger 中介層自動產生的請求追蹤 ID。

### 2.2 自帶 Correlation ID（驗證跨服務追蹤）

```bash
curl -i -H "X-Correlation-ID: my-test-trace-123" http://localhost:8000/api/v1/health
```

回應 header 應該回 `x-correlation-id: my-test-trace-123` — 證明系統優先使用上游傳來的 ID（用於從 frontend → backend → Celery worker 的全鏈路追蹤）。

同時你會在 backend log 裡看到一行 JSON 含 `"trace.id":"my-test-trace-123"`。

### 2.3 統一錯誤格式（404）

```bash
curl http://localhost:8000/api/v1/this-route-does-not-exist
```

預期：
```json
{"error":{"code":404,"message":"Not Found","details":[]}}
```

不是 FastAPI 預設的 `{"detail":"Not Found"}` — 證明統一錯誤 envelope 蓋到 Starlette 的「無路由」情境。

### 2.4 統一錯誤格式（422 validation）

Phase 1 的 `/health` 沒參數能玩，但你可以暫時加一個 demo endpoint 試。或等 Phase 2 真有 endpoint 接收 request body 再試。

### 2.5 OpenAPI / Swagger UI

瀏覽器開：
- **Swagger UI：** [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI JSON：** [http://localhost:8000/api/v1/openapi.json](http://localhost:8000/api/v1/openapi.json)

可以從 Swagger UI 直接點 "Try it out" 觸發 `/api/v1/health`。

### 2.6 結構化 JSON log

backend terminal 應該看到每個 request 都印出一行 JSON，類似：

```json
{"level":"info","@timestamp":"2026-04-27T14:37:05.123Z","service.name":"smart-order-backend","service.version":"0.1.0","service.environment":"dev","trace.id":"b3a6c10c-...","message":"HTTP Request: GET http://localhost:8000/api/v1/health \"HTTP/1.1 200 OK\""}
```

這就是 ECS 格式 — 之後丟到 Elasticsearch / Datadog / CloudWatch 都能直接吃。

---

## 3. 手動驗證 Frontend

```bash
cd frontend && pnpm dev
```

瀏覽器開 [http://localhost:5173](http://localhost:5173)。

### 3.1 LoginForm（mock）

- 看到 **「Smart Order Management」** 標題 + 帳密表單
- 留空提交 → 看到「Username is required」「Password is required」錯誤訊息（zod schema 在前端先擋）
- 隨便填字按 Sign in → 按鈕變「Signing in...」約 400ms → 開 DevTools console 應該看到 `[Phase 1 mock] login succeeded {access_token: 'mock-jwt-token-...', ...}`
- 不會真的呼叫 backend（Phase 1 是純 mock）

### 3.2 視覺檢查

- Tailwind 跑起來 → 樣式有套上去（按鈕黑底白字、表單有圓角邊框）
- shadcn `Button` 元件能用 → variant 切換看起來合理（但 Phase 1 只用了 `default`）

### 3.3 Hot Module Reload

改 `frontend/src/App.tsx` 把 `Phase 1 — scaffolding only` 改成別的字，存檔，瀏覽器應該秒換不需重整。

---

## 4. 驗證 DB Schema 工作流（Alembic）

雖然 Phase 1 沒有 entity，但可以演一遍流程：

### 4.1 跑現有 migration（其實沒東西可跑）

```bash
cd backend && uv run alembic upgrade head
docker compose exec db psql -U postgres -d smart_order -c "\dt"
```

應該看到只有 `alembic_version` 表存在（這代表 Alembic 接通了 DB）。

### 4.2 寫一個假 entity 試 autogenerate

把以下檔案存進 `backend/app/models/order_demo.py`：

```python
"""暫時的 demo entity，跑完試用就刪掉。"""
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class OrderDemo(Base):
    __tablename__ = "order_demo"
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
```

在 `app/models/__init__.py` 裡加：

```python
from app.models.order_demo import OrderDemo  # noqa: F401
```

然後：

```bash
cd backend && uv run alembic revision --autogenerate -m "add order demo"
```

打開 `backend/alembic/versions/2026_*_add_order_demo.py`，應該看到：

```python
def upgrade() -> None:
    op.create_table(
        "order_demo",
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("id", ...),
        sa.Column("created_at", ...),
        sa.Column("updated_at", ...),
        sa.Column("is_deleted", ...),
        sa.Column("version_id", ...),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(...)
```

**重點看：** `id`、`created_at`、`updated_at`、`is_deleted`、`version_id` 五個 column 都自動加上 — 證明 `Base` 的繼承生效。

跑完別忘了 `alembic downgrade base` 回滾、刪掉 demo 檔案、清掉 `alembic/versions/` 下的 revision。

---

## 5. 驗證 Audit log 與 Optimistic lock 雛形

這兩個是 Phase 2 才會真用到，但 helper 已經在了。

### 5.1 Audit log helper

打開 `python` REPL：

```bash
cd backend && uv run python
```

```python
from app.core.logger import configure_logging, audit_log

configure_logging()
audit_log(
    action="order.created",
    actor_id="user-123",
    resource_type="order",
    resource_id="order-456",
    changes={"status": {"from": None, "to": "pending"}},
)
```

會在 stdout 印出一行 ECS-compliant JSON：

```json
{"level":"info","@timestamp":"...","service.name":"smart-order-backend","event.action":"order.created","event.category":"audit","user.id":"user-123","resource.type":"order","resource.id":"order-456","changes":{"status":{"from":null,"to":"pending"}},"message":"audit"}
```

### 5.2 Optimistic lock 行為

Phase 1 還沒 entity 可玩，但邏輯就在 `Base.__mapper_args__` 裡。Phase 2 會加 e2e test 驗證兩個 session 同時 update 時會丟 `StaleDataError`。

---

## 6. Pre-commit hooks（可選）

```bash
uv tool install pre-commit       # 一次性
pre-commit install               # 每個 clone 一次
```

之後每次 `git commit` 會自動跑 ruff + mypy + prettier + eslint。試試：

```bash
echo "x=1" >> backend/app/main.py    # 加一行不合 style 的
git add backend/app/main.py
git commit -m "test"
# 預期：ruff 修掉 + 中止 commit，要你重 stage
```

手動全跑一次：

```bash
pre-commit run --all-files
```

---

## 7. 還沒實作（Phase 2 才有）

下面這些項目目前**沒辦法測**，因為 entity / endpoint 還沒寫：

- 真的登入（目前是 mock）
- 建立、修改、刪除訂單
- Excel 批次匯入
- 自動排程演算法
- Email / In-app 通知
- 衝突偵測 / 警示
- Audit log 的「實際被某個 endpoint 呼叫」流程
- Optimistic lock 的 409 衝突處理
- 軟刪除過濾查詢

這些都會在 Phase 2 時搭配 TDD（先寫測試再實作）逐項加入。

---

## 8. 出問題時看哪裡

| 症狀 | 看這裡 |
|---|---|
| pytest 失敗，testcontainers 起不來 | 檢查 Docker Desktop 是否在跑 |
| uvicorn 起不來，pydantic validation error | 檢查 `.env` 是否存在、是否有 `DATABASE_URL` 等三個必填欄位 |
| Frontend HMR 不動 | `docker compose up -d --force-recreate frontend` |
| Alembic 抱怨 cp950 編碼 | `export PYTHONUTF8=1` |
| 其他 | 看 [README.md 的故障排除表](../README.md#故障排除windows-上常見坑) |

更多開發規範與命名見 [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md)。
