# 排程模組接入指南

> **這份文件給誰看**：要跟排程模組整合的隊友 — 訂單 CRUD、前端、Ops。
> **想看內部細節**（演算法、線段樹、score 編碼、race fix 等）：請看 [`scheduling.md`](./scheduling.md)。

排程模組做了什麼，一句話：**接收訂單事件 → EDF 排程到未來 30 天 → 寫回 DB → WebSocket 推前端**。你不用管演算法怎麼跑，只要知道下面這些怎麼用。

---

## 1. 找你的角色

| 你是誰 | 會用到什麼 | 跳到哪一節 |
|---|---|---|
| 寫 Order CRUD（create / update / delete 訂單的人） | 推 op 到排程 queue | [§2](#2-訂單-crud-接入) |
| 寫前端 / dashboard | REST endpoints + WebSocket 即時通知 | [§3](#3-前端接入) |
| 部署 / Ops | Celery 設定、env vars、Redis 觀察、災難復原 | [§4](#4-ops--部署) |

---

## 2. 訂單 CRUD 接入

訂單在 DB 寫成功之後，**必須**告訴排程模組「有訂單變了」。怎麼說：把一筆 op 推進排程 queue。

### 2.1 兩種推 op 的方法

| 方法 | 適用情境 | 優點 | 缺點 |
|---|---|---|---|
| **A. HTTP** `POST /api/v1/schedule/operations` | 訂單 service 跟排程在不同進程 / 不同 container | 標準介面、走 auth、log 完整 | 多一次 HTTP round-trip |
| **B. 直連 Redis** | 訂單 service 跟排程在**同一個進程** | 沒網路成本 | service 多耦合一個 Redis client |

**強烈建議用 A**，除非你確定要省那一次 RTT 而且能接受耦合。

> **Phase 2 變更**：endpoint shape 從「一次接一筆 op」改成「一次接一個 compound」。一個 compound = 一組 1-4 筆 leaf ops，worker 端 **atomic 執行 + snapshot rollback**（任何一筆 op 失敗就把整個 compound 連已成功的部分一起 undo）。Order CRUD service 內部已自動 build 對應 compound（見 `app/services/order.py::_build_*_compound`），多數情況**前端 / 第三方服務只在「手動 pin / unpin」**時需要直接戳這支 endpoint。

### 2.2 方法 A — HTTP（推薦）

```python
import httpx
import uuid

# 建立訂單後（如果 backend Order CRUD 已自動處理，這段可以略）
def on_order_created(order, actor):
    ops = [
        {
            "op": "add",
            "order_id": str(order.id),
            "order_number": order.order_number,
            "wafer_quantity": order.wafer_quantity,
            "deadline": order.requested_delivery_date.isoformat(),
        },
    ]
    httpx.post(
        "http://backend/api/v1/schedule/operations",
        json={
            "compound_id": str(uuid.uuid4()),
            "group": "grow",
            "op_count": len(ops),  # 必須等於 len(ops)，schema 會驗
            "requested_by": str(actor.id),
            "ops": ops,
        },
        headers={"Authorization": f"Bearer {service_token}"},
    )

# 手動把訂單 pin 到 5/12
def on_user_pinned(order, actor, pin_day):
    ops = [
        {
            "op": "pin",
            "order_id": str(order.id),
            "order_number": order.order_number,
            "wafer_quantity": order.wafer_quantity,
            "deadline": order.requested_delivery_date.isoformat(),
            "fake_deadline": pin_day.isoformat(),
        },
    ]
    httpx.post(
        "http://backend/api/v1/schedule/operations",
        json={
            "compound_id": str(uuid.uuid4()),
            "group": "grow",
            "op_count": len(ops),
            "requested_by": str(actor.id),
            "ops": ops,
        },
        headers={"Authorization": f"Bearer {service_token}"},
    )
```

**Response 202**：`{"compound_id": "...", "message": "Compound queued"}`。實際結果走 WS：
- 成功 → `schedule.updated` broadcast。
- 失敗 → `schedule.compound_failed` notify\_user 給 `requested_by`，state 已 rollback。

### 2.3 方法 B — 直連 Redis（不推薦，僅在無法走 HTTP 時用）

```python
from app.services.schedule_queue import enqueue_compound
from app.schemas.schedule import ScheduleCompoundRequest, ScheduleOpInCompound
# ↑ 這條路徑只有同進程 Python 才走得通；微服務之間請用 HTTP。
```

直接戳 Redis 不會 self-validate（schema 跳過），請避免。

### 2.4 修改訂單 — Order CRUD 已自動處理

Phase 2 之後，**只要呼叫 backend 的 Order CRUD endpoint，scheduler compound 會自動建好推進佇列**，不需要第三方服務 / 前端額外打 `/schedule/operations`：

| Order CRUD | service 自動 build 的 compound |
|---|---|
| `POST /api/v1/orders` | `[add(新)]` |
| `PATCH /api/v1/orders/{id}` 改 qty / deadline（非 pinned） | `[remove(舊), add(新)]` |
| `PATCH /api/v1/orders/{id}`（pinned，新 deadline ≥ pin 日 AND 新 qty ≤ 舊 qty） | `[unpin, remove, add, pin(原 pin 日)]` — 自動 re-pin |
| `PATCH /api/v1/orders/{id}`（pinned，其他情況） | `[unpin, remove, add]` — silent drop pin |
| `PATCH /orders/batch-update` | 每筆訂單獨立 compound，內部規則同上 |
| `DELETE /api/v1/orders/{id}`（非 pinned） | `[remove]` |
| `DELETE /api/v1/orders/{id}`（pinned） | `[unpin, remove]` |

「`group` 是 shrink 還是 grow」由 service 端按 `defer / qty 變小 → shrink`、`advance / qty 變大 → grow` 自動推導，所有 ops 都標同一個 group 進 compound。

**直接戳 `/schedule/operations` 的場景**只剩兩種：
1. **手動 pin / unpin**（user 在 UI 上按「鎖到 5/12」按鈕） — 前端送 1-op compound 帶 `pin` 或 `unpin`。
2. **第三方非標準業務 op**（譬如另一條 service 想直接動排程）。

### 2.5 注意事項

- **`requested_by` 一定要填**：compound 失敗時用這個 user id 推 `schedule.compound_failed` 通知。
- **`op_count` 必須等於 `len(ops)`**：schema 跟 worker 都會驗，不一致直接 422 / `schedule.compound_failed`。這是 producer 對 payload 自我宣告長度的契約，網路截斷或人工改 payload 漏掉一筆 op 都能立刻被偵測到。
- **`ops` 沒上限**：compound 可以是 1 筆 op（pin 單筆訂單）也可以是 30 筆 op（複雜的 batch 改動）— 業務動作要做幾步就放幾步。worker atomic 處理整個 compound。
- **不需要等排程跑完**：endpoint 回 202 就可以接著做事，排程是 async。
- **同 compound 內 ops 必須同 `order_id`**：schema 自帶 validator，多 order 一次直接 422。
- **Compound 內 ops 順序由 producer 決定**（worker 不重排）；service 內建 builder 已經把順序排好。手動戳的話注意 `unpin → remove → add → pin`（如有）的拓樸。
- **權限**：`POST /schedule/operations` 要 `scheduler+`。從 Order CRUD 內部呼叫時要帶 scheduler 等級的 service token。

### 2.6 Pin / Unpin 手動操作

```python
import httpx
import uuid

# UI 按「把訂單 pin 到 5/12」按鈕
def pin_order(order, actor, pin_day):
    ops = [{
        "op": "pin",
        "order_id": str(order.id),
        "order_number": order.order_number,
        "wafer_quantity": order.wafer_quantity,
        "deadline": order.requested_delivery_date.isoformat(),
        "fake_deadline": pin_day.isoformat(),
    }]
    httpx.post(URL, json={
        "compound_id": str(uuid.uuid4()),
        "group": "grow",
        "op_count": len(ops),
        "requested_by": str(actor.id),
        "ops": ops,
    })

# 解 pin
def unpin_order(order, actor):
    ops = [{
        "op": "unpin",
        "order_id": str(order.id),
        "order_number": order.order_number,
        "wafer_quantity": order.wafer_quantity,
        "deadline": order.requested_delivery_date.isoformat(),
    }]
    httpx.post(URL, json={
        "compound_id": str(uuid.uuid4()),
        "group": "shrink",
        "op_count": len(ops),
        "requested_by": str(actor.id),
        "ops": ops,
    })
```

失敗時拿到 `schedule.compound_failed`（`failed_op="pin"` 或 `"unpin"`），**state 已 rollback** — pin 失敗的訂單仍保持原樣（未 pinned）；unpin 失敗的訂單仍是原 pin 狀態。

`is_pinned` / `pinned_production_date` 由 worker 處理完 compound 之後 `apply_schedule` 寫回 DB；compound 失敗就不會寫，DB 維持舊值。

`is_processing_locked` 是獨立的「UI 編輯鎖」flag — Order CRUD 時設 true、`apply_schedule` 跑完設 false。前端拿來在那段時間 disable 該列的 inline edit。

---

## 3. 前端接入

排程模組對前端只暴露兩件事：**REST endpoints**（讀資料 + 主動操作）跟 **WebSocket**（即時通知）。

### 3.1 REST endpoints

全部以 `/api/v1/schedule` 為前綴。

| Method | Path | 權限 | 用途 |
|---|---|---|---|
| `POST` | `/trigger` | scheduler+ | 手動補觸發排程任務 |
| `GET` | `/status` | order_manager+ | 排程 worker 的 lifecycle snapshot（`idle`/`running`/`failed`） |
| `GET` | `/result` | order_manager+ | 目前已排定 / 進行中的訂單清單（含每筆訂單的逐日數量 `daily_breakdown`，包含 `scheduled` 跟 `in_production` 兩種 status） |
| `POST` | `/rebuild` | scheduler+ | 從 DB 重建排程 state（async；不會 block） |
| `POST` | `/operations` | scheduler+ | 推 compound 進佇列（Phase 2 後 Order CRUD 自動處理大部分情況，前端只在「手動 pin / unpin」時直接打） |
| `DELETE` | `/operations/{compound_id}` | scheduler+ | 取消尚未被 worker 處理的 compound（前端「取消」按鈕）。200 = 取消成功；409 = worker 已開始處理，無法取消；404 = compound id 未知 |

錯誤回應一律走 unified envelope：
```json
{ "error": { "code": 404, "message": "Order not found.", "details": [] } }
```

### 3.2 主要拿來用的 endpoints

#### 3.2.1 `GET /api/v1/schedule/result` — 取得當前排程

```ts
const res = await fetch("/api/v1/schedule/result", {
    headers: { Authorization: `Bearer ${token}` },
});
const orders = await res.json();
// [
//   {
//     id: "uuid",
//     order_number: "ORD-20260505-0001",
//     customer_name: "...",
//     wafer_quantity: 15000,
//     requested_delivery_date: "2026-06-15",
//     scheduled_production_date: "2026-05-08",  // 最早開始日
//     expected_delivery_date: "2026-05-09",     // 最晚完成日
//     status: "scheduled",
//     daily_breakdown: [                         // 逐日切分（畫 timeline 用）
//       { date: "2026-05-08", quantity: 10000 },
//       { date: "2026-05-09", quantity: 5000  }
//     ]
//   },
//   ...
// ]
```

訂單按 `scheduled_production_date` 升冪排序。`daily_breakdown` 來自 DB 的 `orders.daily_breakdown` JSONB 欄位（由 `materialize_schedule_task` 寫入），不再即時從 Redis 算 — 所以 Redis 被清過不影響這個欄位的內容。`daily_breakdown` 為空表示這筆訂單還沒被 materializer 寫過（首次部署、或欄位是 NULL）。

#### 3.2.2 `GET /api/v1/schedule/status` — 顯示排程狀態

```ts
const res = await fetch("/api/v1/schedule/status", {
    headers: { Authorization: `Bearer ${token}` },
});
const status = await res.json();
// { state: "idle" | "running" | "failed", started_at, finished_at, task_id, error }
```

通常拿來在 dashboard 顯示「排程中⋯」/「上次跑於 XX」/「失敗了，error 是 …」。

#### 3.2.3 `POST /api/v1/schedule/rebuild` — 災難復原按鈕

當懷疑排程跟現實不同步（例如 DB 跟 Redis state 對不起來，或者 `daily_breakdown` 看起來明顯錯誤）時，叫管理員按這個按鈕：rebuild 會從 DB 重建 Redis state，再跑一輪 materializer 把 `orders.daily_breakdown` 改寫回正確值。

```ts
const res = await fetch("/api/v1/schedule/rebuild", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
});
const { task_id, message } = await res.json();
// 202 Accepted, async 執行；結果透過 WebSocket schedule.updated + schedule.rebuild_skipped 通知
```

### 3.3 WebSocket 即時通知

連線：`GET /api/v1/ws?token=<jwt>`（用同一把 REST 用的 JWT）

```ts
const token = await getAccessToken();
const ws = new WebSocket(`wss://${host}/api/v1/ws?token=${token}`);

ws.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
        case "schedule.updated":
            // 系統動作（換天 advance_day / 重建 rebuild）廣播給所有連線 client
            queryClient.invalidateQueries(["schedule", "result"]);
            break;
        case "schedule.compound_accepted":
            // Phase 4 fast-path：你的 compound 通過了，但 DB 還在 deferred materializer 排隊。
            // 通常拿來把 UI 上的 "處理中" badge 換成 "已接受、等 DB 寫入"。
            toast.success(`操作已接受，正在更新排程資料`);
            break;
        case "schedule.materialized":
            // Phase 4 slow-path：materializer 已經把你提交的修改寫進 DB。
            // 觸發 refetch 看到最新數字。
            queryClient.invalidateQueries(["schedule", "result"]);
            queryClient.invalidateQueries(["orders"]);
            break;
        case "schedule.compound_failed":
            // 自己送的 compound 失敗 + 已 saga-rollback。
            // failed_op 可以是 "add" / "remove" / "pin" / "unpin"，按需要分流。
            switch (msg.failed_op) {
                case "add":
                    toast.error(`訂單 ${msg.order_number} 排不進去：${msg.reason}`);
                    break;
                case "pin":
                    toast.error(`訂單 ${msg.order_number} 鎖定生產日失敗：${msg.reason}`);
                    break;
                case "remove":
                    toast.warning(`訂單 ${msg.order_number} 移除失敗：${msg.reason}（狀態可能已不一致，建議刷新）`);
                    break;
                case "unpin":
                    toast.warning(`訂單 ${msg.order_number} 解除鎖定失敗：${msg.reason}`);
                    break;
            }
            // state 已 rollback，前端不用主動還原 UI；下次刷 /schedule/result 看到的就是失敗前的狀態。
            break;
        case "schedule.compound_cancelled":
            // 自己按了取消、後端確認成功。可以收回 optimistic UI 的「取消中」標記。
            toast.success(`已取消排隊中的操作`);
            queryClient.invalidateQueries(["schedule", "result"]);
            break;
        case "schedule.rebuild_skipped":
            // 自己創的訂單在 rebuild 時被跳過（通常是 deadline 已過期）
            toast.warning(`重建時 ${msg.order_number} 無法排入（${msg.reason}），請確認`);
            break;
    }
});

ws.addEventListener("close", (e) => {
    if (e.code === 4401) {
        // ✱ 重要：4401 = token 失效，刷新 token 後重連
        await refreshToken();
        reconnect();
    } else {
        // 其他 close code：指數退避重連
        reconnect(backoffDelay);
    }
});
```

#### 三種 message type 詳細

| `type` | 觸發時機 | 收件對象 | payload |
|---|---|---|---|
| `schedule.updated` | 任何排程結果有變動（單筆 op 處理完、換天、rebuild） | **所有連線的 client**（broadcast） | `{ type: "schedule.updated" }` |
| `schedule.compound_accepted` | Phase 4 fast-path：compound 通過 saga，trees / pq / pinned 都更新好，accept/reject 結果出來。**DB 還沒寫入**（materializer 排隊中） | compound 的 `requested_by` user | `{ type, compound_id }` |
| `schedule.compound_failed` | Compound 內任一 op 失敗（add / remove / pin / unpin），整個 compound 已 saga-rollback 至 pre-compound 狀態 | compound 的 `requested_by` user | `{ type, compound_id, failed_op_index, failed_op: "add"\|"remove"\|"pin"\|"unpin", order_id, order_number, reason: "capacity_exceeded"\|"deadline_too_far", detail, rolled_back: true }` |
| `schedule.materialized` | Phase 4 slow-path：materializer 一批 DB 寫完，這個 user 有 compound 在這批裡 | 那批 compound 對應的 `requested_by` user | `{ type }` — payload 沒額外欄位，前端收到就 invalidate cache + refetch `/schedule/result` |
| `schedule.compound_cancelled` | `DELETE /operations/{compound_id}` 取消成功 | compound 的 `requested_by` user | `{ type, compound_id }` |
| `schedule.rebuild_skipped` | rebuild 時某筆 scheduled 訂單塞不回去（通常 deadline 已被 base_date 越過） | 訂單的 `created_by` user | `{ type, order_id, order_number, reason: "deadline_too_far"\|"capacity_exceeded" }` |

### 3.4 前端注意事項

- **WebSocket 是 best-effort**：Redis pub/sub 短暫中斷時訊息會掉。所以**不能**只靠 WebSocket 同步資料 — 連線重連後一定要主動 `GET /schedule/result` 對齊一次。
- **多 tab 同步**：同一個 user 開多個 tab 都會收到自己的 `notify_user` 訊息（每個 tab 一份），這是設計如此。
- **自己跟伺服器看到的時間不一樣**：`scheduled_production_date` 是排程器算出來的，不是訂單的 `requested_delivery_date`。前端要兩個都顯示。
- **加新 message type 不要改舊的**：前端是用 `msg.type` 做 routing，舊名改掉所有版本的 client 都會壞。

---

## 4. Ops / 部署

### 4.1 Celery 設定

`backend/app/workers/celery_app.py` 必須加上 imports + beat schedule：

```python
from celery.schedules import crontab

celery_app.conf.update(
    # ... 既有設定 ...
    imports=("app.workers.scheduling",),  # ← autodiscover 抓不到，要顯式 import
)

celery_app.conf.beat_schedule = {
    "scheduling.advance_day": {
        "task": "scheduling.advance_day",
        "schedule": crontab(hour=0, minute=0),  # 每天 00:00 UTC
    },
}
```

啟動指令（在 `backend/` 下）：
```bash
uv run celery -A app.workers.celery_app worker --loglevel=INFO
uv run celery -A app.workers.celery_app beat   --loglevel=INFO   # 換天作業需要 beat
```

⚠️ **沒設 beat 換天作業不會跑**，每天 00:00 UTC 應該推進 `base_date` 但實際上會卡住。

### 4.2 環境變數

5 個 `SCHEDULER_*` 變數，定義在 `.env`，預設值已經是 production 用的數字。

| 變數 | 預設 | 什麼時候要動 |
|---|---|---|
| `SCHEDULER_DAILY_CAPACITY` | `10000` | 產線產能改了（**改完一定要打 `POST /schedule/rebuild`**） |
| `SCHEDULER_HORIZON_DAYS` | `30` | 接受訂單的時間跨度改了（同上，**必須 rebuild**） |
| `SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS` | `300` | advance_day / rebuild 等 in-flight 任務的上限。在大量 op 排隊的環境可能需要調大 |
| `SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS` | `2` | 等待時的 polling 頻率。Redis 流量太多可調大 |
| `SCHEDULER_WAITER_FLAG_TTL_SECONDS` | `600` | crashed-waiter 自我復原時間。改了 wait timeout 記得這個也要 ≥ wait timeout × 2 |

> **改 `DAILY_CAPACITY` 或 `HORIZON_DAYS` 的部署 SOP**：
> 1. 改 `.env`
> 2. **重啟所有 worker + API 進程**（`get_settings()` 是 `@lru_cache`）
> 3. **必須**呼叫 `POST /api/v1/schedule/rebuild`，否則 Redis 裡舊 state 的線段樹大小跟新值不一致，反序列化會 raise

### 4.3 Redis keys 一覽

| Key | 型別 | 用途 |
|---|---|---|
| `schedule:state` | String (JSON) | 排程器主 state（兩棵線段樹 + pq + base_date） |
| `schedule:pending_ops` | Sorted Set | 待處理的訂單 op |
| `schedule:pending_ops:seq` | Integer (INCR) | 給每筆 op 配序號的計數器 |
| `schedule:status` | String (JSON) | worker 跑到哪了（`idle`/`running`/`failed`） |
| `schedule:waiter_pending` | String (TTL 600s) | advance_day / rebuild 占用旗標 |
| `schedule:ws:events` | Pub/Sub channel | worker → API 進程的 WebSocket fan-out 通道 |

### 4.4 觀察 Redis 狀態

```bash
$ uv run python -c "from redis import Redis; from app.core.config import get_settings; \
    r = Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True); \
    print('status:    ', r.get('schedule:status')); \
    print('queue len: ', r.zcard('schedule:pending_ops')); \
    print('seq:       ', r.get('schedule:pending_ops:seq')); \
    print('waiter:    ', r.get('schedule:waiter_pending'))"
```

### 4.5 偷看 WebSocket 流量

```bash
$ uv run python -c "from redis import Redis; from app.core.config import get_settings; \
    r = Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True); \
    p = r.pubsub(); p.subscribe('schedule:ws:events'); \
    [print(m) for m in p.listen()]"
```

### 4.6 災難復原

| 症狀 | 怎麼辦 |
|---|---|
| `schedule:state` key 不見 / Redis 被 flush | `POST /api/v1/schedule/rebuild` |
| 前端 `daily_breakdown` 一直是空 | 表示 `orders.daily_breakdown` 欄位是 NULL — 通常代表 materializer 還沒跑過或寫入失敗。觸發一次 `POST /api/v1/schedule/trigger` 讓 worker 跑完整流程；如果還是空就 `POST /api/v1/schedule/rebuild` 強制重建。 |
| `schedule:status` 卡在 `running` 但 worker 已經死了 | 重啟 worker；如果還是卡，手動 `redis-cli set schedule:status '{"state":"idle"}'` |
| `schedule:status.state == "failed"`、`error` 欄位有訊息 | 三支 task（`run_scheduling` / `advance_day` / `rebuild_schedule`）任一條失敗都會留這個記錄，先看 `error` + Celery traceback 找根因。`failed` 不會擋 `/trigger`（409 只擋 `running`），下次成功的 task 會把 status 蓋回 `idle`，不需要先手動清。 |
| `schedule:waiter_pending` 卡住超過 10 分鐘 | TTL 會自己過期；如果 TTL 被改大可以手動 `redis-cli del schedule:waiter_pending` |
| 排程結果跟 DB 不同步 | `POST /api/v1/schedule/rebuild` |
| 前端 WebSocket 通知突然全停 | 看 backend log 有沒有 `websocket.consumer.failed`（ERROR）— 這代表 Redis pub/sub 中斷或訂閱失敗，consumer 已退出且**不會自我重啟**。重啟 FastAPI process 即可（lifespan 會重新建一個 consumer task）。 |

### 4.7 Ops 注意事項

- **生產 deploy 不要清 `schedule:pending_ops:seq`**：清掉的話新進來的 op 會跟舊的同 score 撞 member。要清的話**也要一起清 `schedule:pending_ops`**。
- **scaling**：`run_scheduling_task` 設計成同時只能跑一個（靠 `schedule:status` 守）。即使開多 worker container，concurrent 的這個 task 也只會有一個在做事。pending_ops 自然 serialize。
- **logs**：worker 的關鍵事件用 `structlog` 寫，可以 grep `schedule.run.start` / `schedule.run.success` / `schedule.advance_day.success` / `schedule.rebuild.success` / `schedule.run.yield_to_waiter`（最後這個代表 race fix 起作用了）
- **alert-worthy log lines**（建議在 log shipper 設告警）：`schedule.run.failed` / `schedule.advance_day.failed` / `schedule.rebuild.failed` / `websocket.consumer.failed` — 這四個都是 ERROR 級別，前三個對應 `schedule:status.state == "failed"`，最後一個代表 WebSocket 通知通道斷掉（需要重啟 FastAPI）
- **WebSocket 在多 instance 部署下**：每個 FastAPI worker 各自持有自己的連線，靠 Redis pub/sub fan-out 同步事件。橫向擴展不需要 sticky session。

---

## 5. 常見問題

**Q: 我推了 op 但前端沒收到 `schedule.updated`？**
A: 檢查 (1) Celery worker 在跑嗎、(2) `schedule:status` 卡在 `running` 嗎、(3) WebSocket 連線還在嗎、(4) Redis pub/sub 通的嗎（用 §4.5 偷看）。

**Q: `schedule.add_failed` 的訊息收不到？**
A: 通常是推 op 時 `requested_by` 沒填，或填的 user_id 沒連 WebSocket。

**Q: 改 deadline 之後排程結果不對？**
A: 確認你拆成 `remove` + `add` 兩筆，且兩筆 `group` 一致。詳見 §2.4。

**Q: 我能不能直接讀 / 寫 Redis state？**
A: **不要**。state 是序列化的 `SchedulerState`，外人改它幾乎一定會破壞線段樹的不變式。要寫 state 就走 `POST /schedule/rebuild`。要讀的話可以 `from app.services.scheduling import SchedulerState; SchedulerState.from_json(raw)`。

**Q: 部署到 K8s 要怎麼開 worker？**
A: 額外開 worker deployment + beat deployment（beat 全 cluster 只能一個 replica）。env vars 從 ConfigMap 帶過去。

**Q: 想看內部運作細節**
A: 去看 [`scheduling.md`](./scheduling.md)，有完整的線段樹推導、score 編碼、race fix 的時序分析、測試矩陣等。
