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

訂單模組告訴排程模組「有訂單變了」的方式是把一個 **compound**（一組 1~N 筆 leaf ops）推進 Redis sorted set，worker 端用 batch admission 把可行的 prefix 一次接受。

> **唯一入口：Order CRUD**。**沒有對外的 raw 排程操作 endpoint**。`POST /api/v1/schedule/operations` 已在 P1-2 後移除（pin / unpin 改用 PATCH 的 `pinned_production_date` 欄位）。所有 compound 都由 backend Order CRUD service 內部 build 並 enqueue — 前端只要正常呼叫 `POST/PATCH/DELETE /api/v1/orders/...`，scheduler 就會跟著動。

### 2.1 Order CRUD → compound 自動對照

| Order CRUD 動作 | 自動 build 的 compound | Group |
|---|---|---|
| `POST /api/v1/orders` | `[add(新)]` | grow |
| `DELETE /api/v1/orders/{id}`（非 pinned） | `[remove]` | shrink |
| `DELETE /api/v1/orders/{id}`（pinned） | `[unpin, remove]` | shrink |
| `PATCH /api/v1/orders/{id}` 改 qty / deadline（非 pinned） | `[remove(舊), add(新)]` | qty 不增 AND deadline 不前 → shrink；否則 grow |
| `PATCH` 改 qty / deadline（pinned，沒明示 `pinned_production_date`） | 自動 re-pin（新 deadline ≥ 舊 pin 日 AND 新 qty ≤ 舊 qty）→ `[unpin, remove, add, pin(原 pin 日)]`；不滿足條件 → `[unpin, remove, add]`（silent drop pin） | 同上 |
| `PATCH` 帶 `pinned_production_date: "YYYY-MM-DD"`（pin / 改 pin 日） | unpinned → `[(remove,add 如有), pin(新)]`；已 pinned 改日 → `[unpin, (remove,add 如有), pin(新)]` | 任一 axis（qty / deadline / pin 日）變緊 → grow；全部不變緊才 shrink |
| `PATCH` 帶 `pinned_production_date: null`（unpin） | `[unpin, (remove,add 如有)]` | unpin 本身屬 shrink；同時 qty/deadline 變緊則 grow |
| `PATCH` 只改 `notes` / `assigned_to` | 不推 compound，producer 直接 commit + audit | — |
| `PATCH /orders/batch-update` | 每筆訂單獨立 1 個 compound，規則同上 | 每筆獨立判斷 |

**Strict-AND group classification**：`shrink` 只在 **qty 不增 AND deadline 不前 AND pin 日不前** 全部成立時標；否則 `grow`。Newly-pinning 視為「pin 日往前移」（把 demand 從 EDF 分布拉到單一天 → 屬於 grow）。這個保證讓 worker batch-admission halving 的不變式成立（shrink compound 的每天 cumulative delta 必非正，drop 尾巴 prefix 不會把可行性翻盤）。

### 2.2 Pin / Unpin — PATCH 的一個欄位（**不再有獨立 endpoint**）

```ts
// UI 按「把訂單 pin 到 5/12」按鈕
await fetch(`/api/v1/orders/${order.id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
        pinned_production_date: "2026-05-12",  // ← 想 pin 的日期
        version_id: order.version_id,           // 樂觀鎖
    }),
});

// 解 pin
await fetch(`/api/v1/orders/${order.id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
        pinned_production_date: null,           // ← 明示 null = unpin
        version_id: order.version_id,
    }),
});
```

關鍵差異「欄位省略」vs「明示 null」：
- **欄位缺席**（body 沒有 `pinned_production_date` 這個 key）：pin 狀態維持原樣，僅在 qty/deadline 變動時觸發 case-14 auto-re-pin / silent-drop。
- **明示 `null`**：unpin。
- **明示日期**：pin 到那一天（如果已 pinned 就是改 pin 日）。

Pydantic `model_fields_set` 區分這兩種「`None`」— body 沒寫 vs explicit null 在 schema 層是不同事件，service 拿來決定要不要走 pin transition 邏輯。

**同步驗證**：`pinned_production_date > requested_delivery_date` 在 PATCH 同步回 422，user 立刻看到錯誤、不用等 WebSocket。

**Pin 失敗（worker 端）**：因為改用 batch admission，**沒有 compound-level rollback**。Producer 端 PATCH 進場前已做兩件事：(1) `SELECT FOR UPDATE` row lock 序列化同訂單的併發 PATCH（避免兩個 producer 各自讀到 stale row 後 enqueue 互斥 compound、worker trip `SegmentTreeInvariantError`）、(2) `is_processing_locked=True` + `status=pending` 預先寫到 DB。Worker 接受該 compound 後才把 `is_pinned` / `pinned_production_date` 寫進 DB；compound 被 batch 二分搜尋 reject 時，worker 走 `_apply_db_action_reject` 清掉 lock、不寫業務欄位 — DB 仍是 pre-PATCH 狀態。

### 2.3 注意事項

- **不再戳 `/schedule/operations`**：raw compound endpoint 已撤掉，前端不會也不該直接打到排程 queue。所有 schema-level 驗證、權限檢查、row lock、`db_action` 都長在 Order CRUD endpoint 裡。
- **`is_processing_locked` 是 UI 編輯鎖**：Order CRUD 進場時 producer 寫 true、worker accept / reject 後寫 false。前端在這段時間 disable 該列的 inline edit；第二次 PATCH/DELETE 撞到 locked row 會被 producer 直接 409。
- **`version_id` 是樂觀鎖**：每次寫入自動 bump；client 拿到 409「stale version」要重抓 row 再試。bombard 模式內建 `call_with_retry` 在 409 時自動 re-fetch `version_id` 重試。
- **`requested_by` 自動帶**：scheduler compound 失敗時 `schedule.compound_failed` 通知該 user，producer 從 JWT 帶 actor_id 過去，**前端不用自己塞**。
- **非 scheduling 的 PATCH**（只改 notes / assigned_to）：producer 直接 commit + audit，**不推 compound、不過 worker**，沒有 latency penalty。
- **Internal `enqueue_compound`**：backend 同 process 內寫測試 / scripts 時可以 `from app.services.schedule_queue import enqueue_compound`；但**跨 process / 第三方 service 沒有對外 endpoint 可走**。

### 2.x 給 Order CRUD service 開發者（內部實作 reference）

下面這段給維護 `app/services/order.py` 的人看 — 一般前端 / 第三方使用者跳過。

```python
# app/services/order.py — 內部 builder
from app.services.schedule_queue import enqueue_compound

# create_order: producer 寫 is_processing_locked=True + commit → 自動推 [add]
# update_order: get_by_id_for_update 取 row lock → 預先寫 status=pending /
#   is_processing_locked=True + commit → _build_patch_compound 依 qty / deadline /
#   pin 三軸 build ops + db_action（new_pinned_production_date_set 標 worker 該不該
#   寫 pin columns）→ enqueue_compound
# delete_order: 同 update_order 走 row lock，build [unpin?, remove]
```

`get_by_id_for_update` 是 `app/repositories/order.py` 新加的 helper（`.with_for_update()`）— 對同一筆訂單的併發 PATCH/DELETE 在 SELECT 階段就序列化。沒有這把 row lock，兩個快速連續 PATCH 可以各自從同一份 row state 出發 build compound、雙雙 enqueue 進 Redis，worker 處理第二個時觸發 `SegmentTreeInvariantError`。

**Worker 端互補防禦（`SegmentTreeInvariantError` per-leaf 隔離）**：萬一 invariant break 還是發生（producer 沒走 lock 的歷史路徑、或真的 state corruption），`_commit_accepted_batch` 對每筆 leaf op 包 try/except — 受影響的 compound 收進 `failed` bucket、走 reject path（清 lock、不寫業務欄位）、WS 推 `schedule.compound_failed`；同 batch 其他 compound 繼續處理，不會被毒到。原本的設計是 raise 冒到 task 外層 → 整支 task fail → 整批 N 個 healthy compound 一起死，bombard pin race 偶爾炸一個會把整輪 drain 雪崩。

**Materializer 端互補防禦（`apply_schedule` SAVEPOINT）**：`rebuild_schedule_task` 跟 `materialize_schedule_task` 跑完整 `apply_schedule` 時，逐筆 `set_schedule_dates` 在 `db.begin_nested()`（PostgreSQL SAVEPOINT）內執行。某筆撞到 `StaleDataError`（producer race bump version_id）只 rollback 該筆 SAVEPOINT、不毒到 outer session，繼續處理下一筆 — 不然 `PendingRollbackError` 會卡死整批寫入跟 audit log。

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
| `GET` | `/capacity` | order_manager+ | 未來 30 天剩餘產能的**前綴和**序列，dashboard 畫產能圖用 |
| `GET` | `/pending-ops` | order_manager+ | 排隊中 compound 的 drain 順位快照（rank=1 = 下一個會被 worker 處理） |
| `POST` | `/rebuild` | scheduler+ | 從 DB 重建排程 state（async；不會 block） |
| `DELETE` | `/operations/{compound_id}` | scheduler+ | 取消尚未被 worker 處理的 compound（前端「取消」按鈕）。200 = 取消成功；409 = worker 已開始處理，無法取消；404 = compound id 未知 |

> Raw `POST /schedule/operations` 已移除。pin / unpin 改用 PATCH 的 `pinned_production_date` 欄位，所有 compound 由 Order CRUD service 內部 enqueue（§2.1）。`DELETE /operations/{compound_id}` 保留，給前端「取消尚未處理的動作」按鈕用 — secondary index `schedule:pending_ops:by_compound_id` 由 `enqueue_compound` 一邊維護。

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

#### 3.2.2 `GET /api/v1/schedule/capacity` — 30 天剩餘產能（dashboard 用）

```ts
const res = await fetch("/api/v1/schedule/capacity", {
    headers: { Authorization: `Bearer ${token}` },
});
const cap = await res.json();
// {
//   base_date: "2026-05-12",
//   daily_capacity: 10000,
//   entries: [
//     { date: "2026-05-12", cumulative_remaining: 6000 },   // day 1: 還剩 6,000 (今天用了 4,000)
//     { date: "2026-05-13", cumulative_remaining: 16000 },  // day 1+2 累計
//     // ... 共 30 筆
//   ]
// }
```

`cumulative_remaining` 是**前綴和**（從 `base_date` 累計到該天），不是當日剩餘。要單日剩餘自己做差就好：

```ts
const dailyRemaining = cap.entries.map((e, i) =>
    i === 0 ? e.cumulative_remaining
            : e.cumulative_remaining - cap.entries[i - 1].cumulative_remaining
);
// 單日已用 = daily_capacity - dailyRemaining[i]
```

跟 `/schedule/result` 不同，這條 endpoint **讀的是 Redis 中的 `SchedulerState`**（不是 DB），所以反映的是 scheduler 演算法當前的 in-memory 視角；Redis 被清掉時會 fallback 到「30 天都還是空的」（每天 10,000 全可用）。要重新對齊就按 `POST /rebuild`。

收到 WebSocket `schedule.materialized` / `schedule.updated` 時前端可以跟 `/result` 並行 refetch 這條，把產能圖一起更新。

#### 3.2.3 `GET /api/v1/schedule/pending-ops` — 排隊中 compound 順位

```ts
const res = await fetch("/api/v1/schedule/pending-ops", {
    headers: { Authorization: `Bearer ${token}` },
});
const queued = await res.json();
// [
//   { compound_id, rank: 1, group: "shrink", op_count: 2,
//     ops: [
//       { op: "unpin",  order_id: "uuid-a", order_number: "ORD-A" },
//       { op: "remove", order_id: "uuid-a", order_number: "ORD-A" }
//     ],
//     requested_by },
//   { compound_id, rank: 2, ... },
//   ...
// ]
```

`rank=1` 代表 worker 下一個會處理的 compound。**一個 compound 可能跨多筆訂單**（batch 業務動作的合法形狀），所以 `ops[]` 每筆 leaf 都各自帶 `order_id` / `order_number`。同一筆訂單在 list 中也可能出現多次（連續 PATCH 快過 worker 消化速度、或多個 compound 都碰到它）；前端通常掃 `ops` 找出含該 `order_id` 的 compound、取最小 rank：

```ts
const rankByOrder = new Map<string, number>();
for (const it of queued) {
    for (const o of it.ops) {
        const cur = rankByOrder.get(o.order_id);
        if (cur === undefined || it.rank < cur) {
            rankByOrder.set(o.order_id, it.rank);
        }
    }
}
// rankByOrder.get(orderId) → 該訂單下一個動作排第幾、或 undefined 代表沒在排隊
```

跟 `/capacity` 一樣讀的是 Redis（不是 DB），所以這個視角是「scheduler 的 in-flight 工作清單」。佇列空時回 `[]`。

收到 `schedule.compound_accepted` / `schedule.compound_cancelled` / `schedule.updated` 時都可以順便 refetch 這條更新「N 個動作等著被處理」的徽章。

#### 3.2.4 `GET /api/v1/schedule/status` — 顯示排程狀態

```ts
const res = await fetch("/api/v1/schedule/status", {
    headers: { Authorization: `Bearer ${token}` },
});
const status = await res.json();
// { state: "idle" | "running" | "failed", started_at, finished_at, task_id, error }
```

通常拿來在 dashboard 顯示「排程中⋯」/「上次跑於 XX」/「失敗了，error 是 …」。

#### 3.2.5 `POST /api/v1/schedule/rebuild` — 災難復原按鈕

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

⚠️ **沒設 beat 換天作業不會跑**，每天 00:00 UTC 應該推進 `base_date` 但實際上會卡住。Beat 漏跑的場景（worker 重啟跨日、整個 stack 關過夜）由 FastAPI startup recovery 偵測 + 自動補；但 beat 還是必須設好，不然每天都要靠重啟才會 advance。

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

> **跨日重啟 / Redis 被清 / orphan pending_ops 都不需要人工介入**：FastAPI lifespan startup 會自動跑 `run_startup_recovery()`（`app/services/startup_recovery.py`），依現況派 `rebuild_schedule_task` / `advance_day_task` × N / `run_scheduling_task` 之一或多個。Multi-replica 用 `SET NX EX 60` mutex 確保只有一個 replica 派工。詳見 [`scheduling.md`](./scheduling.md) §4.7。

| 症狀 | 怎麼辦 |
|---|---|
| 重啟跨過一個或多個 00:00 UTC | **自動處理**：startup recovery 偵測 `base_date < today` → 派 `advance_day_task` × (today - base_date)；差超過 30 天直接 rebuild。 |
| `schedule:state` key 不見 / Redis 被 flush | **自動處理**：startup recovery 偵測缺 state → 派 `rebuild_schedule_task.delay()`。手動觸發仍可用 `POST /api/v1/schedule/rebuild`。 |
| `pending_ops` 有 compound 但 worker 沒在動 | **自動處理**：startup recovery 偵測 `zcard > 0` AND `status != running` → 派 `run_scheduling_task.delay()`。 |
| 前端 `daily_breakdown` 一直是空 | 表示 `orders.daily_breakdown` 欄位是 NULL — 通常代表 materializer 還沒跑過或寫入失敗。觸發一次 `POST /api/v1/schedule/trigger` 讓 worker 跑完整流程；如果還是空就 `POST /api/v1/schedule/rebuild` 強制重建。 |
| `schedule:status` 卡在 `running` 但 worker 已經死了 | startup recovery **不會自動清**這個（race-prone）；重啟 worker 後若還是卡，手動 `redis-cli set schedule:status '{"state":"idle"}'`。 |
| `schedule:status.state == "failed"`、`error` 欄位有訊息 | 三支 task（`run_scheduling` / `advance_day` / `rebuild_schedule`）任一條失敗都會留這個記錄，先看 `error` + Celery traceback 找根因。`failed` 不會擋 `/trigger`（409 只擋 `running`），下次成功的 task 會把 status 蓋回 `idle`，不需要先手動清。 |
| `schedule:waiter_pending` 卡住超過 10 分鐘 | TTL 會自己過期；如果 TTL 被改大可以手動 `redis-cli del schedule:waiter_pending` |
| `is_processing_locked=true` 的 orphan row（worker 在 compound 處理中途 crash） | startup recovery **不自動清**（race-prone — 真的 worker 在處理中時會誤清），需手動 SQL：`UPDATE orders SET is_processing_locked=false WHERE is_processing_locked=true AND id NOT IN (...)` |
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

**Q: `schedule.compound_failed` 的訊息收不到？**
A: 通常是 (1) 該 user 沒連 WebSocket，或 (2) Redis pub/sub 中斷。`requested_by` 是後端從 JWT 帶的，前端不用自己填。

**Q: 改 deadline 之後排程結果不對？**
A: 後端 service 已經自動拆 `remove + add` 並標 group；如果結果還是不對，先看 `GET /schedule/pending-ops` 確認 compound 進佇列了沒，再看 worker log 有沒有 `schedule.compound_failed`。手動改前端組 compound 已沒有意義（raw endpoint 已撤）。

**Q: 我能不能直接讀 / 寫 Redis state？**
A: **不要**。state 是序列化的 `SchedulerState`，外人改它幾乎一定會破壞線段樹的不變式。要寫 state 就走 `POST /schedule/rebuild`。要讀的話可以 `from app.services.scheduling import SchedulerState; SchedulerState.from_json(raw)`。

**Q: 部署到 K8s 要怎麼開 worker？**
A: 額外開 worker deployment + beat deployment（beat 全 cluster 只能一個 replica）。env vars 從 ConfigMap 帶過去。

**Q: 想看內部運作細節**
A: 去看 [`scheduling.md`](./scheduling.md)，有完整的線段樹推導、score 編碼、race fix 的時序分析、測試矩陣等。
