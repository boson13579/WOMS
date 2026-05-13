# 排程模組整合說明

本文件給 **要呼叫 / 串接排程功能** 的後端隊友看，由抽象到具體：先把模組做什麼講清楚，再講對外 contract，最後才講「你要寫哪幾行程式」。
排程演算法本身的細節（線段樹、EDF、advance_day）請參考 [`backend/CLAUDE.md`](../backend/CLAUDE.md) §業務規則。

> 文中所有路徑相對於 repo 根。

---

## 1. 模組是什麼

排程模組把 Order CRUD 產生的「加入 / 移除訂單」事件丟到 Celery worker，依 EDF 演算法排到未來 30 天的產能格子裡，把結果寫回 `orders` 資料表，再廣播 WebSocket 通知前端重抓。每天凌晨自動把時間軸前進一天。

```
                                  ┌──────────────────────┐
   POST /api/v1/orders ─────▶     │ Order CRUD           │
                                  │ (api/v1/orders.py)   │
                                  └──────────┬───────────┘
                                             │ ZADD op
                                             │ (score = group + seq)
                                             ▼
                                  ┌──────────────────────┐         ┌──────────────────┐
                                  │ Redis                │         │ Celery Beat      │
                                  │  schedule:pending_ops│         │  每天 00:00 UTC  │
                                  │   (sorted set)       │         └────────┬─────────┘
                                  │  schedule:pending_ops│                  │
                                  │   :seq (INCR)        │                  │
                                  │  schedule:state      │                  │
                                  │  schedule:status     │                  │
                                  └──────────┬───────────┘                  │
                                             │ ZRANGE                       ▼
                                             ▼                  ┌──────────────────────┐
                                  ┌──────────────────────┐      │ advance_day_task     │
                                  │ run_scheduling_task  │◀─────┤ (rolls horizon, then │
                                  │  batch admission：    │      │  re-fires scheduling)│
                                  │  1. read all pending │      └──────────────────────┘
                                  │  2. binary search    │
                                  │     largest feasible │
                                  │     prefix [1..k]    │
                                  │  3. batch tree update│
                                  │     + per-compound pq│
                                  │  4. ZREM accepted    │
                                  │  5. save state once  │
                                  │  6. loop until empty │
                                  │  7. status = idle    │
                                  │  8. self.delay() 若  │
                                  │     queue 還有東西   │
                                  └──────┬───────────────┘
                                         │
                            ┌────────────┴────────────┐
                            ▼                         ▼
                  ┌──────────────────┐      ┌──────────────────────────┐
                  │ Postgres orders  │      │ Redis pub/sub channel    │
                  │ (status,         │      │  schedule:ws:events      │
                  │  scheduled_dates)│      └─────────┬────────────────┘
                  └──────────────────┘                │ subscribe
                                                      ▼
                                            ┌──────────────────────────┐
                                            │ /api/v1/ws (FastAPI)     │
                                            │  ConnectionManager       │
                                            │  → ws.send_json(...) to  │
                                            │    connected clients     │
                                            └──────────────────────────┘
```

關鍵概念三個：

- **pending_ops queue**：所有 compound（想排 / 想取消 / 想 pin 等的事件包成一組）先進這個 Redis sorted set，score 編碼 shrink-優先 + 組內 FIFO。worker 用 batch admission 一次處理 — `ZRANGE` 讀全部、二分搜尋找最大可行 prefix `[1..k]`、整 batch tree 一次更新後 `ZREM` 那 k 個 member。可行性 check 在 tree 變動前就做，所以不需要 snapshot rollback。詳見 §4.2。
- **scheduler state**：序列化在 Redis 的線段樹 + pq + `pinned_orders` + `base_date`，跨次持久保存。
- **schedule status**：`idle` / `running` / `failed`，由 worker 維護，API 跟 advance\_day 拿來判斷有沒有任務在跑。
- **訂單生命週期 status**（DB 上的 `Order.status` 欄位，跟上面的 schedule status 是不同的東西）：`pending` → `scheduled` → `in_production` → `completed`。`pending` 是剛建立 / 剛 PATCH 的；scheduler 把它排進排程後變 `scheduled`；advance_day 在生產日當天 00:00 UTC 把它變 `in_production`、生產完那天 00:00 UTC 變 `completed`。`cancelled` 是 PATCH 軟刪除走的旁路。

WebSocket fan-out 走另一條 Redis pub/sub 通道（`schedule:ws:events`）：worker 用同步 `publish()`，FastAPI 進程在 lifespan 起 async subscriber，把訊息推到連線在 `/api/v1/ws` 的客戶端（§4.5 細講）。

主要入口分四種，對應後面四個章節：
- 對外 HTTP / WebSocket → §2
- 接入步驟（`celery_app.py` 註冊、Order CRUD 推 op、前端連 WebSocket）→ §3
- 內部實作（演算法、worker 細節、DB 寫回、WebSocket transport）→ §4
- 開發 / 測試 / 限制 → §5、§6、§8

---

## 2. 對外 contract

### 2.1 HTTP API

全部以 `/api/v1/schedule` 為 prefix，定義在 `backend/app/api/v1/schedule.py`。錯誤一律走 unified envelope `{"error": {"code": <int>, "message": "...", "details": [...]}}`。

五個 endpoint 一覽（細節見下方各節）：

| Method | Path | 權限 | 一句話 |
|---|---|---|---|
| `POST` | `/trigger` | scheduler+ | 手動補觸發排程任務 |
| `POST` | `/operations` | scheduler+ | 推一筆訂單操作進 `pending_ops` |
| `GET`  | `/status` | order_manager+ | 排程 worker 的 lifecycle snapshot |
| `GET`  | `/result` | order_manager+ | 目前已排定的訂單清單 |
| `POST` | `/rebuild` | scheduler+ | 從 DB 重建線段樹與 pq（async；任務自己等 in-flight 結束） |

#### `POST /schedule/trigger` — 手動補觸發排程

**功能**：發送 `run_scheduling_task.delay()`，回傳這次 Celery task 的 id。如果 `schedule:status.state == "running"`，直接 409 不重複觸發。

**權限**：`scheduler+`

**Request**：無 body。

**Response 202**：
```json
{ "task_id": "celery-task-uuid", "message": "Scheduling started" }
```

**Response 409**：
```json
{ "error": { "code": 409, "message": "Scheduling already in progress", "details": [] } }
```

**什麼時候會被叫**：
- 前端排程 dashboard 上的「重新排程」按鈕（scheduler / admin 想 force 一次完整重算）。
- 自動觸發鏈異常 — 例如 worker crash 後 `pending_ops` 還有殘留沒被消化，運維用這條補一次。
- 演算法 / state schema 升版後想 reset 一次完整跑。
- QA、開發本機除錯重跑驗證。

> 正常運作下根本不會被呼叫 — Order CRUD 推 op 時 `POST /operations` 自己就會 `.delay()`，worker 跑完還會自我 re-trigger 直到 `pending_ops` 清空。`/trigger` 是「自動鏈斷掉」或「不經 op 想強制重排」的逃生口。

---

#### `POST /schedule/operations` — 推訂單 compound 進 pending_ops

> **Phase 2 變更**：endpoint 已從「一次接一筆 op」改成「一次接一個 compound」。Compound 是一組 N 筆 leaf ops（沒上限，常見的 PATCH-pinned-order 是 3-4 筆、batch 動作可以更多）。**worker 端走 batch admission**：一次撈全部 pending compound、二分搜尋最大可行 prefix `[1..k]`、整批接受。可行性檢查在 state 變動前就做，所以單筆 compound 內部不需要 saga / rollback。第一筆 compound 自己塞不下時走 reject + WS `schedule.compound_failed`。詳見 §4.2。Order CRUD 內部已自動 build 對應 compound（見下表），多數 producer 不必直接戳這支 endpoint。

**功能**：接收一筆 `ScheduleCompoundRequest`，透過 `services.schedule_queue.enqueue_compound` 推進 sorted set（一個 compound = 一個 member）。`schedule:status` 是 `idle` / `failed` 就 `celery_app.send_task("scheduling.run")` 觸發 worker；`running` 就讓 in-flight task 自己 re-trigger 撿。

> sorted-set 的 score 同時編碼了「shrink compound 先於 grow compound」跟「組內 FIFO」（seq），所以 worker 端只是 `ZPOPMIN` 拿下一個 compound 處理。Compound 內 ops 順序由 producer 安排，worker 不重排。

**權限**：`scheduler+`。

**Request body** (`ScheduleCompoundRequest`)：
```json
{
  "compound_id": "uuid",          // 系統會 default_factory 產生；cancel 時要用
  "group": "shrink" | "grow",
  "op_count": 3,                  // 必須等於 len(ops)，tamper / truncation 守門
  "requested_by": "uuid",
  "ops": [
    {
      "op": "add" | "remove" | "pin" | "unpin",
      "order_id": "uuid",            // 同 compound 內所有 ops 必須同一個 order_id
      "order_number": "ORD-...",
      "wafer_quantity": 200,
      "deadline": "2026-06-15",
      "fake_deadline": "2026-06-12"  // 只 op="pin" 才填，其他 op 必須省略
    }
  ]
}
```

Schema-level validation：
- `ops` 至少 1 筆（沒上限 — 想塞 3 筆、30 筆都行，看業務動作要做幾步）。
- `op_count` 必須等於 `len(ops)`。不一致就 422。這是 producer 端對 payload「自我宣告長度」的契約，網路截斷或人工改 payload 漏掉一筆 op 時，後端能立刻偵測到。
- 所有 ops 必須對同一個 `order_id`（多 order 一次 = 業務 bug）。
- `op="pin"` 必須帶 `fake_deadline`，其他 op 不得帶。

**Worker 端 double-check**：worker pop 出 compound member 後 也驗一次 `op_count == len(ops)`，不對就直接 `schedule.compound_failed`（`failed_op_index=-1` 表示「不是哪一筆 op 的錯、而是整個 payload 壞掉」），不執行任何 op。這層是給 Redis member 在 enqueue 之後被改壞 / 人工 surgery 弄錯時做的兜底。

**Response 202** (`ScheduleCompoundResponse`)：
```json
{ "compound_id": "uuid", "message": "Compound queued" }
```
永遠 202 — 結果經 WebSocket 通知。成功 → `schedule.updated` broadcast；失敗 → `schedule.compound_failed` notify\_user（envelope 含 `compound_id` / `failed_op_index` / `failed_op` / `reason` / `rolled_back: true`）。

**Order CRUD 自動 build 的 compound**（service 層的 case-8 smart routing）：

| Order CRUD 動作 | 自動 build 的 compound 內容 | Group |
|---|---|---|
| `POST /api/v1/orders` 新增訂單 | `[add(新)]` | grow |
| `DELETE /api/v1/orders/{id}` 軟刪除（非 pinned） | `[remove(舊)]` | shrink |
| `DELETE /api/v1/orders/{id}` 軟刪除（**pinned**） | `[unpin, remove(舊)]` | shrink |
| `PATCH` 改 `wafer_quantity` / `requested_delivery_date`（非 pinned） | `[remove(舊), add(新)]` | shrink（defer / qty 變小）或 grow（advance / qty 變大） |
| `PATCH` 改（**pinned**，新 deadline ≥ pin 日 AND 新 qty ≤ 舊 qty） | `[unpin, remove(舊), add(新), pin(原 pin 日)]` — **自動 re-pin** | 同上 |
| `PATCH` 改（**pinned**，其他情況） | `[unpin, remove(舊), add(新)]` — **silent drop pin** | 同上 |
| `PATCH` 只改 `notes` / `assigned_to` / `customer_name` 等 | 不推 compound | — |
| `PATCH /orders/batch-update` | **每筆訂單獨立 1 個 compound**，內部規則同上 | 每筆獨立判斷 |

Auto-re-pin 條件（case 14）：**兩個都要成立**才會在 compound 末尾加上 `pin(舊 pin 日)`：
1. 新 deadline ≥ 舊 pin 日（否則 pin 日落到 deadline 之後，pin 在物理上不可能滿足）
2. 新 qty ≤ 舊 qty（否則 pin 那天的 capacity 可能不夠，pin 會在 worker 那邊 fail，整個 compound rollback）

「把訂單 pin 到某天」或「解除 pin」這兩個獨立 user action 目前**沒有專屬 endpoint**，前端直接打 `POST /schedule/operations` 帶單筆 `[pin]` 或 `[unpin]` 的 compound 就好。

---

#### `DELETE /schedule/operations/{compound_id}` — 取消尚未處理的 compound

> **Phase 3** 新增。前端按下「取消」按鈕觸發；後端從 sorted set 把這個 compound 移掉，worker 不會再看到它。

**功能**：透過 `schedule:pending_ops:by_compound_id` secondary index（`enqueue_compound` 一邊維護）O(1) 查到 sorted set member 字串 → `ZREM` 移除 + `HDEL` 清掉 index entry → WebSocket `schedule.compound_cancelled` 給 compound 的 `requested_by`。

**Response 200** (`ScheduleCompoundResponse`)：
```json
{ "compound_id": "uuid", "message": "Compound cancelled" }
```

**錯誤碼**：

| Code | 何時 | 含意 |
|---|---|---|
| **409** | secondary index 有這支 compound、但 `ZREM` 回 0 | worker 已經把它 ZPOPMIN 走、cancellation 輸了 race。已經在處理，無法取消。前端應該等 `schedule.updated` / `schedule.compound_failed`。 |
| **404** | secondary index 完全沒這 compound_id | compound 從沒被 enqueue、或老早已經處理掉 index entry 也被清乾淨了。 |

**權限**：`scheduler+`。

---

#### `GET /schedule/status` — 排程器當前狀態

**功能**：讀 `schedule:status` Redis key 回傳 lifecycle snapshot；沒有資料時回 idle 預設值（首次部署或 Redis 被清空）。

**權限**：`order_manager+`

**Response 200（有資料）**：
```json
{
  "state": "running",
  "started_at": "2026-05-06T00:13:42+00:00",
  "finished_at": null,
  "task_id": "celery-task-uuid",
  "error": null,
  "message": null
}
```

**Response 200（沒資料）**：
```json
{
  "state": "idle",
  "started_at": null,
  "finished_at": null,
  "task_id": null,
  "error": null,
  "message": "No scheduling has been run yet"
}
```

`state` 三值：
- `"idle"` — 上一輪成功跑完、目前沒任務在跑
- `"running"` — 有任務在跑（`started_at` 是這一輪開始時間，`finished_at` 為 null）
- `"failed"` — 上一輪丟例外，`error` 欄會有 exception message

**什麼時候會被叫**：
- 前端排程 dashboard 顯示狀態徽章（idle / 跑中 / 失敗）。
- 前端 / 操作者按過 `POST /trigger` 後輪詢這條 endpoint 看任務跑完沒。
- 監控 / Grafana scrape，把狀態變化（特別是 `failed`）轉警報。
- 運維 incident 排查 — 哪一輪 task 是哪個 id、什麼時候失敗、錯什麼。

> `advance_day_task` 內部也讀同一個 key（直接走 Redis client 不走 HTTP），用來判斷要不要等 in-flight run 結束。

---

#### `GET /schedule/result` — 已排定的訂單清單

**功能**：從 DB 撈所有 `is_deleted = false AND status = 'scheduled'` 的訂單，按 `scheduled_production_date` 升冪排序，序列化成 `list[ScheduleResultResponse]`。

**權限**：`order_manager+`

**Response 200**：
```json
[
  {
    "id": "uuid",
    "order_number": "ORD-20260505-0001",
    "customer_name": "ACME",
    "wafer_quantity": 15000,
    "requested_delivery_date": "2026-06-15",
    "scheduled_production_date": "2026-05-06",
    "expected_delivery_date": "2026-05-07",
    "status": "scheduled",
    "daily_breakdown": [
      {"date": "2026-05-06", "quantity": 10000},
      {"date": "2026-05-07", "quantity": 5000}
    ]
  }
]
```

**什麼時候會被叫**：
- 前端排程 dashboard 主畫面載入。
- 前端收到 `{"type": "schedule.updated"}` WebSocket 通知後 invalidate 快取重抓。
- scheduler / order\_manager 換班、開站前看當前計劃。
- 匯出報表、產製當日生產單。

**行為細節**：
- 軟刪除（`is_deleted = true`）不會出現。
- 每筆訂單一個 row。`scheduled_production_date` 是最早被排到的那天、`expected_delivery_date` 是最後一天，這兩個欄位由 `apply_schedule` 折疊後寫進 DB。
- `daily_breakdown` 列每一天分配多少 wafer，按日期升冪。**來源是 DB 的 `orders.daily_breakdown` JSONB 欄位**，由 `materialize_schedule_task` 在每次排程被 accept 後（slow path）跑 `compute_schedule(state)` 算出來寫進去。`GET /schedule/result` 這條讀路徑**完全不碰 Redis state**，純粹從 Postgres 取資料；Redis state 只負責演算法本身，DB 才是「最新排程結果」的 source of truth。
- 沒有 `daily_breakdown` 資料（首次部署、訂單還沒被 materializer 寫過，或欄位是 NULL）時回 `[]`，summary 日期欄仍正常回傳。
- 沒分頁 — 30 天 horizon 下訂單量目前看不會大到要分頁；未來真要分頁請另開 endpoint 或加 query params，**不要直接動這條**讓既有客戶端壞掉。
- `status` 欄理論上一定是 `"scheduled"`（這是查詢條件），但仍照 schema 回傳給前端做 type guard。

---

#### `GET /schedule/pending-ops` — 排隊中 compound 的順位快照

**功能**：把 `schedule:pending_ops`（Redis sorted set）的所有 compound 攤出來，每筆附上 1-indexed 的 `rank`，順位完全跟 worker `ZPOPMIN` 的順序對齊。

**權限**：`order_manager+`

**Response 200**：
```json
[
  {
    "compound_id": "uuid",
    "rank": 1,
    "group": "shrink",
    "op_count": 2,
    "ops": [
      {"op": "unpin", "order_id": "uuid-a", "order_number": "ORD-20260505-0001"},
      {"op": "remove", "order_id": "uuid-a", "order_number": "ORD-20260505-0001"}
    ],
    "requested_by": "uuid"
  },
  ...
]
```

**行為細節**：
- **一個 compound 可以跨多筆訂單**：典型 single-order 流（PATCH / DELETE / CREATE）一個 compound 對應一筆訂單；但 batch 業務動作完全可以把多筆訂單塞進同一個 compound。`ops[]` 每筆 leaf 都帶自己的 `order_id` / `order_number`，前端要查「某訂單排第幾」就掃 `ops` 找出含該 `order_id` 的 compound、讀它的 `rank`。
- 一筆訂單**可能會出現在 list 中多次**（連續快速 PATCH 在 worker 還沒消化前可以堆兩個 compound、或同時出現在不同 compound 的 ops 裡）。前端要計算「某訂單的下一個排程動作排第幾」就取所有出現處 `rank` 的最小值。
- `group` + 同 group 的 FIFO 是底層 `score_for_op(group, seq)` 排出來的，shrink-group 永遠排在 grow-group 之前。Worker 也是按 ZPOPMIN 拿，所以 `rank=1` = 下一個被處理。
- 佇列空（沒人排隊）時回 `[]`，**不**是 404 — dashboard 可以無腦輪詢。
- `ops[].op` 是 `"add"` / `"remove"` / `"pin"` / `"unpin"` 之一；不暴露 `wafer_quantity` / `deadline` 之類的 leaf 細節，dashboard 真要那些資訊請改打 `/schedule/result`。

**什麼時候會被叫**：
- Dashboard 顯示「目前佇列裡有 N 個動作等著被處理」的徽章。
- 收到 `schedule.updated` / `schedule.compound_accepted` / `schedule.compound_cancelled` WebSocket 時 refetch 一次。
- 工程師 debug：看 worker 為什麼還沒處理到某個 compound。

---

#### `GET /schedule/capacity` — 30 天剩餘產能前綴和

**功能**：讀 Redis 中現有的 `SchedulerState`，把 `capacity_tree` 投影到絕對日期、回傳 30 天的「累計剩餘產能」序列給 dashboard 畫圖用。Redis 沒有 state 時用 `SchedulerState.initial(today)` 當 fallback，這樣 dashboard 永遠拿得到 30 筆 entry，不會吃到 500 或空陣列。

**權限**：`order_manager+`

**Response 200**：
```json
{
  "base_date": "2026-05-12",
  "daily_capacity": 10000,
  "entries": [
    {"date": "2026-05-12", "cumulative_remaining": 6000},
    {"date": "2026-05-13", "cumulative_remaining": 16000},
    {"date": "2026-05-14", "cumulative_remaining": 26000}
    // ... 共 30 筆
  ]
}
```

**行為細節**：
- `entries[i].cumulative_remaining = capacity_tree.query(i + 1)` — 也就是從 `base_date` 到 `entries[i].date` 為止「總共還剩多少 wafer 產能」。要某一天**單日**剩餘的話前端做差 (`entries[i] - entries[i-1]`) 即可。
- 來源就是排程演算法用來做 feasibility 判斷的同一棵 segment tree，所以 dashboard 看到的數字跟「scheduler 自己認為當前還能不能塞進新訂單」永遠對齊。
- **不碰 DB**：剩餘產能是演算法內部量，只活在 Redis；要 dashboard 跟 DB 對齊請看 `/schedule/result`。Trade-off：Redis 被清掉時 capacity 就會看起來「全空」（fallback 走 `SchedulerState.initial`），不像 `/schedule/result` 從 DB 拿那麼穩。要永久解決就按 `POST /schedule/rebuild` 把 state 從 DB 拉回來。
- 列表固定長度 30；`daily_capacity` 欄位帶出來讓前端不用 hard-code 常數（將來改 `SCHEDULER_DAILY_CAPACITY` 不會讓前端錯算）。

**什麼時候會被叫**：
- Dashboard 首頁的「未來 30 天產能利用率」圖表初始載入。
- 收到 `schedule.materialized` / `schedule.updated` WebSocket 通知後 refetch 一次（前端可以跟 `/schedule/result` 並行打）。

---

#### `POST /schedule/rebuild` — 從 DB 重建排程狀態（async）

**功能**：dispatch `rebuild_schedule_task` 並立刻回 202。**rebuild 本身是 async**，endpoint 不會 block 等待結果，跟 `advance_day_task` 同一個 pattern。

**task 的執行流程**：
1. **Poll `schedule:status` 等 in-flight `run_scheduling_task` 結束**（最多 5 分鐘，2 秒 polling 一次；超時就 log warning 後繼續）。這一步用 `_wait_for_idle_run` 完成，跟 `advance_day_task` 共用同一個 helper。
2. 讀取 Redis 中現有 `schedule:state` 的 `base_date`（沒有就用今天）。
3. 從 DB 撈所有 `status='scheduled'` 的訂單，轉換成 `SchedulingOrder`，並帶出 `order_id → created_by` map（給 step 5 通知用）。
4. 依 `sort_key()`（deadline 早 → qty 大 → order\_number 字母）排序後逐一呼叫 `add_order`，從全空的 `SchedulerState.initial(base_date)` 開始重填。`add_order` 失敗的訂單（主要是 `deadline_too_far`）收進 `skipped` 清單。把重建後的 state 存回 `schedule:state`。
5. **針對每筆被 skip 的訂單，依 `created_by` 透過 WebSocket 推 `schedule.rebuild_skipped` 訊息給原 requester**，讓他們知道這筆訂單需要人工調整。
6. 觸發一次 `run_scheduling_task.delay()` — 在重建後的 state 上消化「等待期間累積的 pending\_ops」並廣播 WebSocket `schedule.updated`。

**為什麼設計成 async（不再 409）**：rebuild 跟 in-flight `run_scheduling_task` 衝突的本質是「兩個都要寫 `schedule:state`」。原本 409 設計把責任丟給呼叫方（叫他「等一下再試」），但實務上呼叫者沒辦法精確知道 in-flight 任務什麼時候結束，會變成 polling retry loop。改 async 之後：
- 呼叫者只要 POST 一次，rebuild 一定會被執行
- 任務自己 serialize（poll status 直到 idle），不會跟 in-flight 任務搶寫
- rebuild 完還會自動再觸發 `run_scheduling_task` 把等待期間進來的 pending\_ops 消化掉，**rebuild 後的 state + 新 ops** 一起在新基礎上重算
- 流程跟 `advance_day_task` 完全一致，模型統一好維護

**權限**：`scheduler+`

**Request**：無 body。

**Response 202**：
```json
{
  "task_id": "f2c1a78c-…",
  "message": "Rebuild queued; will run after any in-flight scheduling completes."
}
```
- `task_id`：Celery task 的 ID，可選用於日誌追蹤。
- `message`：固定字串，告知呼叫者 rebuild 已排入。
- 真正的「重建結果 + 被 skip 的訂單」會透過 WebSocket 抵達：
  - `schedule.rebuild_skipped`（每筆 skip 各一次，送給該訂單的 `created_by`）
  - `schedule.updated`（broadcast，rebuild + drain pending\_ops 後）

**什麼時候會被叫**：
- **資料 migration 後**：批次直接寫 DB 繞過 pending\_ops，導致 Redis state 跟 DB 脫節 — 打這條重同步。
- **Redis 被清空 / 崩潰**：`schedule:state` 不見了，重建讓排程從 DB 真值恢復，而不是等下次 Order CRUD 才慢慢補回。
- **線段樹算法升版**：schema 或算法邏輯有 breaking change，需要用新版演算法把現有訂單跑一遍以確保 state 格式正確。
- **懷疑 state 損壞**：`capacity_tree` 或 `deadline_tree` 的前綴和跟 pq 不一致時（例如 mid-run crash）。

> 這條 endpoint 會讓重建後的結果**覆蓋** `schedule:state`，但寫入時機是 task 執行到 step 4 時才發生，不是 endpoint 回 202 那一刻。等待期間打的 CRUD 會 push 進 `pending_ops`，rebuild 完後 step 6 觸發的 `run_scheduling_task` 會把它們消化掉。  
> 多次連按 rebuild 不會出錯：第二個 task 會等第一個 task 結束的 `run_scheduling_task` 跑完才 rebuild，結果一樣 — 從 DB 重建是 idempotent 的。

---

### 2.2 Pydantic schemas

定義在 `backend/app/schemas/schedule.py`：

| Schema | 對應欄位 |
|---|---|
| `ScheduleOperationRequest` | `op` (`"add"`\|`"remove"`)、**`group`** (`"shrink"`\|`"grow"`，可省略 — 詳見 §4.3 處理順序)、`order_id`、`order_number`、`wafer_quantity`、`deadline`、`requested_by` |
| `ScheduleTriggerResponse` | `task_id`、`message` |
| `ScheduleStatusResponse` | `state` (`"idle"`\|`"running"`\|`"failed"`)、`started_at`、`finished_at`、`task_id`、`error`、`message` |
| `ScheduleRebuildResponse` | `task_id`、`message`（rebuild 已改 async；skipped 訂單透過 WebSocket `schedule.rebuild_skipped` 抵達） |
| `ScheduleResultResponse` | `id`、`order_number`、`customer_name`、`wafer_quantity`、`requested_delivery_date`、`scheduled_production_date`、`expected_delivery_date`、`status`、**`daily_breakdown`** (list of `DailyAssignment`) |
| `DailyAssignment` | `date`、`quantity`（>0） |

### 2.3 WebSocket endpoint 與 message payloads

**Endpoint**：`GET /api/v1/ws?token=<jwt>` — server-driven channel，client 連上後等 server push，自己不需要送任何 frame。token 用任何已登入 user 的 JWT（跟 REST 一樣那把），驗證走 `app.core.security.decode_access_token`。

**連線生命週期**：

| 事件 | server 行為 |
|---|---|
| 帶有效 token 連線 | `accept()`，把 socket 註冊到 `ConnectionManager[user_id]` |
| token 缺失 / 過期 / 無效 | `close(code=4401)`（自訂 application close code，對應 HTTP 401 語義） |
| client `disconnect` | `ConnectionManager` 自動清掉這條 socket，user 沒其他 socket 時連 user key 一起拔掉 |
| 訊息有錯字 / Redis 暫斷 | `_handle_event` 跟 publisher 都會 `try/except` 後 log warning 繼續，不會中斷整條連線 |

**訊息 payloads**：worker 送的兩種 type，每筆都用 `send_json` 送一個 JSON object：

| 場景 | 走哪個 publisher | payload |
|---|---|---|
| advance_day / rebuild 完成（system-initiated 才 broadcast） | `broadcast` | `{"type": "schedule.updated"}` |
| **Compound 通過 batch admission**（Phase 4：in-memory state 已接受、DB 寫入排到 materializer 上） | `notify_user` | `{"type": "schedule.compound_accepted", "compound_id": "..."}` |
| Compound 在 batch 二分搜尋裡單獨塞不下（連 `[1..1]` 都 infeasible） | `notify_user` | `{"type": "schedule.compound_failed", "compound_id": "...", "failed_op_index": 0, "failed_op": "add"\|"remove"\|"pin"\|"unpin", "order_id": "...", "order_number": "...", "reason": "capacity_exceeded", "detail": "Batch admission could not fit this compound.", "rolled_back": true}` |
| **Materializer 一批 DB 寫完**（Phase 4：要求 user 重新 fetch） | `notify_user` | `{"type": "schedule.materialized"}` — 只送給那批裡有 compound 的 user，其他 user 不收 |
| Compound 被 `DELETE /operations/{compound_id}` 取消（Phase 3） | `notify_user` | `{"type": "schedule.compound_cancelled", "compound_id": "..."}` |
| rebuild 時某筆 scheduled 訂單塞不回去（通常是 deadline 已被 `base_date` 越過） | `notify_user` | `{"type": "schedule.rebuild_skipped", "order_id": "...", "order_number": "...", "reason": "deadline_too_far"\|"capacity_exceeded"}` |

**前端怎麼接**：

```ts
const token = await getAccessToken();          // 跟 REST 用同一把 JWT
const ws = new WebSocket(`wss://${host}/api/v1/ws?token=${token}`);

ws.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
        case "schedule.updated":
            queryClient.invalidateQueries(["schedule", "result"]);
            break;
        case "schedule.add_failed":
            toast.error(`Order ${msg.order_number} 排不進去：${msg.reason}`);
            break;
        case "schedule.rebuild_skipped":
            toast.warning(`重建時 ${msg.order_number} 無法排入（${msg.reason}），請確認`);
            break;
    }
});

ws.addEventListener("close", (e) => {
    if (e.code === 4401) {
        // token 過期 / 無效：刷新 token 後重連
    } else {
        // 一般斷線：指數退避重連
    }
});
```

> 加新 `type` 不要改舊 `type` 字串。前端是用 `msg.type` 做 routing，舊名改掉會打到所有版本的 client。

### 2.4 Redis keys

| Key | 型別 | 內容 | 由誰寫 / 誰讀 |
|---|---|---|---|
| `schedule:pending_ops` | **Sorted Set** | member = op JSON 字串；score = `score_for_op(group, seq)`（shrink 群 score < grow 群，組內 seq 小的先出）。producer ZADD，worker ZPOPMIN，兩端都是 O(log n)。 | CRUD / API 寫；worker 讀 |
| `schedule:pending_ops:seq` | String (Integer) | 全域單調遞增 INCR 計數器，給每筆 op 配一個唯一的 `seq`，讓 sorted-set member 內含 `_seq` 欄位達成「相同 payload 也視為不同筆」。 | producer INCR；不會被讀（單純當 counter） |
| `schedule:state` | String (JSON) | `SchedulerState` 序列化（兩棵線段樹 raw values + pq + base\_date） | worker 寫；演算法讀 |
| `schedule:status` | String (JSON) | `{state, started_at, finished_at, task_id, error}` | worker 寫；API / 監控讀 |
| `schedule:waiter_pending` | String (`"1"`，TTL 10 分鐘) | 一支 advance\_day / rebuild waiter 進入 `_wait_for_idle_run` 之前 SET，task body 結束時在 `finally` 裡 DELETE。`run_scheduling_task` 在結尾 retrigger 之前先 `GET` 這把 key — 有就**讓位**（不 retrigger），由 waiter 結尾自己呼 `.delay()`。TTL 是 crash-safety：如果 waiter 在 finally 之前死掉，10 分鐘後 flag 自動消失，系統不會永遠卡在「讓位」狀態。 | waiter 寫 / 清；run_scheduling_task 讀 |
| `schedule:state_writer_lock` | String (task_id，TTL 5 分鐘) | **P0-2/P0-3 並行保護**：任何要寫 `schedule:state` 的 task（run/advance_day/rebuild）進入 body 前 SETNX 自己的 task_id；finally 用 Lua CAS-delete（只刪自己持有的）。`run_scheduling_task` 拿不到就直接 return（持有人 retrigger 時會接手）；`advance_day_task` / `rebuild_schedule_task` 退而 polling，最多等 5 分鐘（仍拿不到 → status=failed）。TTL 跟 lock-hold 時間是同一個 5 分鐘安全窗口。 | 三支 state-writer task 寫 / 清 |
| `schedule:pending_ops:by_compound_id` | **Hash** | secondary index: `compound_id → sorted-set member 字串`，給 `DELETE /schedule/operations/{compound_id}` O(1) 查到該 compound 的 member 後 ZREM。enqueue 時用 pipeline 跟 ZADD 一起 atomic 寫；worker 在 ZPOPMIN 後 best-effort HDEL 清掉 stale entry。 | producer 寫；cancel-endpoint 讀；worker 清 |
| `schedule:pending_ops:dlq` | **List** | **P1-5 dead-letter queue**：`_pop_next_compound` 從 sorted set 拿到的 member 若 JSON 解析失敗，RPUSH 進這條 list + ERROR log；不再 silently 丟掉。ops 看到這條 list 有東西就要去人工處理（撈出原始 bytes、找到被卡住的 order_id、unlock）。 | worker 寫；ops / 監控讀 |
| `schedule:materialize_running` | String (`"1"`，TTL 5 分鐘) | self-coalescing flag：materializer 進 body 前 SETNX，沒拿到就 return（已有 materializer 在跑）；finally 在最後 DEL。讓 N 個 fast-path 成功只觸發 1 次 DB 重寫。 | materializer 寫 / 清 |
| `schedule:materialize_notify_pending` | **Set** | 等著被 materializer 通知的 user_id 集合：每次 fast-path 接受 compound 後 SADD `requested_by`。Set 語義天然 dedupe — 同一 user 多筆 compound 在一個 materialize window 內只通知一次。 | run_scheduling_task SADD；materializer RENAME 走 |
| `schedule:materialize_notify_processing` | **Set** | materializer 透過 atomic RENAME 從 pending swap 過來的 in-flight 批次。drain 期間新進 SADD 落在新的 pending；crash 恢復時 SUNIONSTORE 把 processing 合回 pending。 | materializer 寫 / 清 |
| `schedule:compound_reject_rate` | String (float as text) | `run_scheduling_task` batch admission 用的 reject-rate adaptive cap：EWMA 估計每筆 compound 被 reject 的機率 p（初值 0.01、bounds `[1e-4, 1.0]`、alpha 0.05）。drain loop 用 `min(N, ceil(1/p))` 當 candidate 窗口，避免在「下一筆就是 reject」的情境下 halve 整個 N。每筆 compound 接受/拒絕都 EWMA 更新；持久化在 Redis 跨 worker / restart 共享（last-writer-wins on concurrent writes，OK for tuning heuristic）。 | run_scheduling_task 讀 / 寫 |

`pending_ops` 每筆 JSON：

```json
{
  "op": "add" | "remove",
  "group": "shrink" | "grow",
  "order_id": "uuid-string",
  "order_number": "ORD-20260505-0001",
  "wafer_quantity": 200,
  "deadline": "2026-06-15",
  "requested_by": "uuid-string",
  "_seq": 42
}
```

`group` 是處理順序欄位（§4.3 詳述）：
- 省略時 schema validator 依 `op` 推導（`remove → shrink`、`add → grow`），給單純的 delete / add 用。
- **複合更新（defer / shrink-qty / advance / grow-qty）的 remove + add 兩筆必須由 producer 明確帶上同一個 group**，不然 add 那半會掉到 grow phase 而拿不到 shrink phase 已經釋放的產能。

`_seq` 是 producer 端從 `INCR schedule:pending_ops:seq` 拿到的單調序號：
- 寫進 payload 是為了讓即使兩筆 op 的內容欄位完全一樣（極少見的重送 / 重試），sorted-set member 的 JSON 字串也不同，避免 ZADD 把後一筆 silent 覆蓋成同一個 member。
- worker 端把它當 metadata 忽略（`_op_to_scheduling_order` 只挑出 `order_id` / `order_number` / `wafer_quantity` / `deadline`）。

**為什麼從 List 換成 Sorted Set**：原本用 `LPUSH` + `LRANGE 0 -1` + `LREM` 的寫法，每次 pop 要把整條 queue 拉出來掃一遍才能挑出 shrink-優先 + 組內 FIFO 的 winner，是 O(n) 而且每個 op 都會這樣做一次（總共 O(n²)）。改成在 producer 端把 group 跟 seq 編成 score，worker 直接 `ZPOPMIN` 拿最小 score 那筆 — ZADD / ZPOPMIN 都是 O(log n)，總成本 O(n log n)。

**為什麼有 `schedule:state_writer_lock`（不靠 `schedule:status` 就好嗎）**：`status` 是 advisory observability key — 多個 reader 看到 `idle` 都會繼續、`_set_status(running)` 跟下一個 `_save_state` 之間的 gap 就是 race window。在 prod `--concurrency >= 2` 或多 worker container 的場景下，兩個 `run_scheduling_task` 可以同時讀同一份 V1 state、各自 mutate、各自 save — 後 save 者蓋掉前者，一個 compound 的影響徹底蒸發。`state_writer_lock` 是 atomic mutex，補上正確性層的缺口：
- **只有真的會寫 `schedule:state` 的三支 task 拿這把 lock**：`run_scheduling_task` / `advance_day_task` / `rebuild_schedule_task`。
- 進 body 前 `SET NX EX 300 task_id`；finally 用 Lua CAS-delete（只刪 value == 自己 task_id 的 lock）防止「TTL 過期 → 別人接手 → 我們的 finally 誤刪別人 lock → race window 重開」這個二次 race。
- `run_scheduling_task` 拿不到就 return（holder 在自己 retrigger 時會接手）；`advance_day_task` / `rebuild_schedule_task` polling 等 lock，超時 → raise `RuntimeError`，**Celery autoretry 自動 backoff 重試 3 次**（60s/120s/240s）— 因為他們是「必須跑」的 task，單次拿不到 lock 不該讓整天 finalize 沒做（沒 autoretry 的話 advance_day 失敗一次，下次 Beat 觸發要等 24 小時）。

選 SETNX + CAS 而不是 Redlock：單一 Redis 實例的場景用單一 SETNX 就夠，Redlock 是給多 Redis 副本的，會引入不必要的複雜度。也不選「靠 Celery `--concurrency=1` 約束」 — 把正確性壓在 ops 紀律上太脆。`status` + `waiter_pending` 仍保留下來給 observability + cooperative retrigger 用。

**為什麼 `materialize_schedule_task` 不在 lock 裡** — 早期 round-2 review 為了 advance_day × materializer 的「stale daily_breakdown」race 把 materializer 也加進 lock，但這違反了 `status`（idle / running）的設計初衷：**status 是 `run_scheduling_task` 的 lifecycle key**，讓 slow path（materializer 每筆訂單 ~4ms × N，N=500 跑 2 秒）排在 lock 後面 = 把 user-facing PATCH/DELETE 延遲堆在 materializer 上面，背離 Phase-4 fast/slow split 的核心目的。所以 round-3 拿掉了。`materialize_running` flag 還在用來 self-coalesce 防止兩個 materializer 同時跑。

代價：advance_day（每天 00:00）跟 in-flight materializer 仍可能 race，advance_day `_save_state(V')` 之後 materializer 用 pre-advance_day V 寫 DB → `daily_breakdown` / `scheduled_production_date` 變成 stale 一個 materializer cycle。**`mark_in_production` / `mark_completed_outside_set` 寫的 status 欄不受影響**（materializer 的 `set_schedule_dates` 有 P0-1 防禦不會 demote in_production、也根本不寫 completed），所以 status workflow 安全。

stale 也是有界的 — `advance_day_task` / `rebuild_schedule_task` finally 走完最後 `materialize_schedule_task.delay()` 顯式再派一支 materializer，讀 fresh state 後把 stale 覆寫掉。stale window ≈ 一個 materializer 週期（50ms-2s），不會擴散到「下次 user 動作」。

**Corner case — follow-up dispatch 撞 `skip_concurrent`**：advance_day 派的 M2 醒來時，造成 stale 的那支 in-flight M1 還握著 `materialize_running` 沒釋放 → M2 SETNX 失敗、走 `skip_concurrent` 直接 return。M1 跑完之後的 post-release re-trigger 檢查 (`materialize_schedule_task` 結尾 `if rds.exists(MATERIALIZE_NOTIFY_PENDING_KEY)`) 若看到 `notify_pending` 是空的就不會再派下一支 → stale 一路 leak 到下次 user PATCH 才覆寫。為了堵這個漏洞，advance_day / rebuild 在 `.delay()` 前先 `SADD` 一個系統 sentinel 字串（`__system_advance_day__`）進 `notify_pending`：M1 的 post-release 檢查保證會看到 pending 有東西、再派一支 M3 讀 fresh state 寫 DB。Materializer 在 per-member 通知時走 `uuid.UUID(member)` → 撞到 sentinel 會 `ValueError` 走 log-and-skip 分支（`schedule.materialize.bad_user_id`），不會 propagate。代價是每個 advance_day / rebuild 週期最多一條 warning log。

**為什麼 `schedule:pending_ops:dlq` 存在**：`_pop_next_compound` 從 sorted set 拿到的 member 若 JSON 解析失敗（極罕見：手動 redis-cli 改錯、persistence 半寫入、bit flip），ZPOPMIN 已經把 member 從 queue 拿走 → 沒有 DLQ 的話 compound 就**永遠消失**，對應訂單的 `is_processing_locked=True` 永遠不會被清、`requested_by` 也救不出來 → 訂單卡在「處理中」轉圈圈。改 RPUSH 進 DLQ + ERROR log，ops 看到 DLQ 有東西就介入手動 decode、找到 order_id、unlock。用 List 而不是 Set 是為了保留到達順序（forensic analysis 容易），同一個壞 member 出現多次也都保留。

**為什麼 `enqueue_compound` 用 pipeline 寫 ZADD + HSET**：原本 INCR → ZADD → HSET 三步是分開的 Redis call。任何一步失敗會留下不一致狀態 — 最常見 case：HSET 前 Redis 連線中斷 → compound 在 queue 但 secondary index (`by_compound_id`) 沒有 → `DELETE /operations/{compound_id}` 拿不到 member 回 404，即便那個 compound 還在 queue 裡（worker 會吃掉，但 cancel 窗口失效）。改成把 ZADD + HSET 包 `pipeline(transaction=True)` → MULTI/EXEC 原子寫，partial outcome 不可能。INCR 留在 pipeline 外是因為要先有 seq 才能算 score；INCR 失敗的後果是 seq 短少一格、沒實際傷害（seq 只要 monotonic increasing 不要求連續），不值得進 pipeline。

**為什麼 materializer 用三把 key（running / notify_pending / notify_processing）而不是一把**：self-coalescing 設計需要分離「誰在跑」跟「誰要被通知」兩個資料。`materialize_running` 是 SETNX 互斥鎖讓 N 個 fast-path 成功只觸發 1 次 DB 重寫；`notify_pending` 是 set 語義天然 dedupe（同 user 多 compound 在一個 window 內只通知一次）；`notify_processing` 是 RENAME atomic swap 過來的 in-flight 批次，drain 期間新 SADD 落在新的 pending、crash 恢復時 SUNIONSTORE 把 processing 合回 pending — 不掉訊息。

### 2.5 環境變數（scheduler 相關）

排程模組可調的常數都從 `app.core.config.Settings` 讀，定義在 `.env` / `.env.example`，預設值跟 production 用的數字一致。改動要重新啟動 worker / API 進程才生效（`get_settings()` 是 `@lru_cache`、模組載入時 snapshot）。**改 `SCHEDULER_DAILY_CAPACITY` 或 `SCHEDULER_HORIZON_DAYS` 的話必須打 `POST /api/v1/schedule/rebuild`** — 否則既有 Redis 裡 `schedule:state` 的線段樹尺寸跟新的不一致，反序列化會 raise。

| 變數 | 預設 | 用途 | 哪裡讀 |
|---|---|---|---|
| `SCHEDULER_DAILY_CAPACITY` | `10000` | 每日 wafer 產能上限；`capacity_tree` 每天 cap 值都用這個 | `app/services/scheduling.py::DAILY_CAPACITY` |
| `SCHEDULER_HORIZON_DAYS` | `30` | 線段樹的天數；超過這個天數的 deadline → `add_order` 回 `deadline_too_far` | `app/services/scheduling.py::HORIZON_DAYS` |
| `SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS` | `300` | `advance_day_task` / `rebuild_schedule_task` 的 `_wait_for_idle_run` 上限 | `app/workers/scheduling.py::_RUN_WAIT_TIMEOUT_SECONDS` |
| `SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS` | `2` | `_wait_for_idle_run` 每次 poll 之間的 `time.sleep` 秒數 | `app/workers/scheduling.py::_RUN_WAIT_POLL_INTERVAL_SECONDS` |
| `SCHEDULER_WAITER_FLAG_TTL_SECONDS` | `600` | `schedule:waiter_pending` 的 TTL — crashed waiter 自我復原用 | `app/workers/scheduling.py::_WAITER_FLAG_TTL_SECONDS` |

> **不放進 env 的常數**：`GROUP_OFFSET = 10**12` 是 sorted-set score 的編碼內部常數，跟線段樹的 float64 精度綁在一起改了會直接破壞 ZPOPMIN 的排序行為，沒有合理的 deployment 會想動它。留在 code 裡。

---

## 3. 接入步驟（後端要寫 2 段、前端接 1 段 WebSocket）

### 3.1 在 `backend/app/workers/celery_app.py` 註冊 task module 與 Beat

`autodiscover_tasks(packages=["app.workers"])` 預設只找 `tasks.py`，**抓不到 `scheduling.py`**。

這個陷阱在測試環境發現不出來 — pytest 直接 `import app.workers.scheduling`，`@celery_app.task` decorator 立刻執行、task 也就註冊了；但 prod worker 用 `celery -A app.workers.celery_app worker` 起來時不會 transitively import scheduling.py，沒有 task 註冊。後續 `.delay()` 進 broker 後 worker 找不到 handler → silent dead-letter，整個排程系統靜默死亡，**前端只會看到 compound 一直停在 pending-ops 不被消化**。這是「測試全綠 staging deploy 直接死」的最丟臉雷區。

不選改檔名為 `tasks.py` 因為 `scheduling.py` 跟 module purpose 對齊（這個檔是 scheduling 相關的所有 task），重命名只是為了滿足 autodiscover 的預設規則、反而模糊化語義。顯式 `imports=` 更清楚。配套的 smoke test `test_celery_registers_all_scheduling_tasks` assert 四個 task 都註冊 — refactor 不小心動到時立刻紅燈。

在 `celery_app.conf.update(...)` 加 `imports`，並補 `beat_schedule`：

```python
from celery.schedules import crontab

celery_app.conf.update(
    # ... 既有設定 ...
    imports=("app.workers.scheduling",),
)

celery_app.conf.beat_schedule = {
    "scheduling.advance_day": {
        "task": "scheduling.advance_day",
        "schedule": crontab(hour=0, minute=0),  # 每天 00:00 UTC
    },
}
```

啟動指令（在 `backend/` 下）：

```
uv run celery -A app.workers.celery_app worker --loglevel=INFO
uv run celery -A app.workers.celery_app beat   --loglevel=INFO   # 換天作業需要 beat
```

### 3.2 WebSocket — 已就緒，前端怎麼連

WebSocket 整套已經實作完成，**你不用寫 backend code**。前端只要：

1. **登入拿 JWT**（跟 REST 完全同一把，`POST /api/v1/auth/login`）。
2. **連到 `/api/v1/ws?token=<jwt>`**（協議用 `ws://` 或 `wss://`）。
3. **註冊 message 處理**：`schedule.updated` 觸發 React Query invalidate；`schedule.add_failed` 顯示 toast。
4. **斷線重連**：close code `4401` 表示 token 失效要刷新 + 重連；其他 close code 就指數退避重試。

範例 JS / TS 已在 §2.3 給。

> 如果你之後要在 backend 加新的 WebSocket 訊息類型（例如「訂單即將到期」之類），只要在 worker / service 那邊呼叫 `app.services.websocket.broadcast(...)` 或 `notify_user(...)`，傳一個帶獨立 `type` 字串的 payload，前端自動會收到。後端的 endpoint / connection manager / Redis 訂閱端一律不用改。

### 3.3 在 Order CRUD 把操作推進 pending_ops

> ⚠ **重要：使用者欄位的 DB 寫入由 worker 負責，不是 producer**。
>
> Producer (`create_order` / `update_order` / `delete_order` / `batch_update_orders`) 只做三件事：(1) 驗證 status + optimistic-lock `version_id`，(2) 把 `is_processing_locked=True`（+ `status=pending` for update）寫進 DB 並 commit（這是 OL 邊界），(3) 帶著 new + old values 把 compound enqueue 進去（schema 新欄位 `db_action: CompoundDbAction`）。`wafer_quantity` / `requested_delivery_date` / `notes` / `is_deleted` / audit log **都在 worker 接受 compound 後才寫**。
>
> **為什麼**：原本 producer 是「先 commit DB 新值 → 再 enqueue compound」。compound 在 worker 端失敗（capacity_exceeded / deadline_too_far）時 saga rollback 把 state 還原成舊值，但 DB 已經寫了新值 → 永久脫節。例：PATCH qty 1000→9999，worker 拒絕，state 回 1000，DB 留 9999，前端看到 `wafer_quantity=9999` 但 `daily_breakdown` 加總 = 1000，8999 wafer 憑空消失。改成 worker single-owner：DB 只在 state 接受時寫，永遠跟 state 一致；失敗時 producer 從沒寫過 → 零補償、零脫節。
>
> 不選「producer 先 commit、worker 失敗時補償寫回舊值」是因為補償方案需要兩處 ownership（每加一個 column 就要兩邊同步維護），而且 producer commit 跟 compound enqueue 之間若 process crash，DB 已寫新值但 compound 沒進 queue → 永久脫節、沒地方補償。
>
> **API 契約變化**（前端要知道）：
> - `PATCH /orders/{id}` 仍回 200，但 body 是 **locked + pre-PATCH values**；新值要等 `schedule.compound_accepted` WebSocket 後 refetch。
> - `DELETE /orders/{id}` 回 204，但 DB 還沒軟刪除；同樣 await WebSocket。
> - audit log 時間戳對應 worker accept 那刻，不是 user 按按鈕那刻。
>
> 純改 `notes` / `assigned_to`（不影響排程）的 PATCH 不走 compound，producer 直接 commit + audit；不為了一致性付額外延遲。`is_processing_locked` 仍由 producer 寫，因為它只是 UI lock hint（避免 user 連按兩下）、不是 source of truth，原子寫 lock + `version_id` bump 就達到 PATCH-twice 防呆。
>
> **Producer 端還有一道 explicit guard：`is_processing_locked=True` 的 row 第二次 PATCH/DELETE 直接 409**（`update_order` / `delete_order` 開頭檢查；`batch_update_orders` 把 locked row 加進 skipped）。原本只靠 `version_id` bump 偷防 — 兩個快速 PATCH 落地時，第二個會因為 stale version_id 失敗。但這個保護不夠：如果第二個 PATCH **沒**帶 version_id（例如 batch update 或 future API 變動），就會繞過防呆、enqueue 第二個 compound 疊在第一個之上，audit log 順序變混亂、前端 lock UI 失效。所以多加一條顯式 409 既快又清楚。
>
> **`db_action.old_*` 欄位在 delete compound 內被 worker 用於 audit**：`_build_delete_compound` 把 pre-delete 的 `wafer_quantity` / `requested_delivery_date` / `notes` / `assigned_to` 都塞進 `db_action`；worker accept 時把這四個欄位填進 `audit_logs.old_value`。沒有這個 snapshot 的話，`order.cancelled` 那筆 audit 只記 `status: scheduled → cancelled` + `is_deleted: false → true`，要回答「這張訂單被取消時 qty / deadline 是多少」就得 cross-reference 別的 audit 紀錄 — self-contained 比較好查。
>
> **`_apply_db_action_reject` 防禦 `in_production` status**：reject path 預設「DB 沒被產品端寫過 → 清 lock + 用 `scheduled_production_date` 推導 status」。但若未來放寬 `MUTABLE_STATUSES` 讓 in_production row 也能 PATCH（例如改 notes），這條 reject 路徑會把 in_production 訂單默默 demote 回 `scheduled` — 跟 §4.4 P0-1 為 `set_schedule_dates` 加的防禦對稱。所以 reject path 顯式 `if status == in_production: return`，今天不可達、明天放寬時也不會踩雷。

訂單**送進 producer service** 後推 compound + 觸發排程。**操作對應**（每個 compound 都要帶 `group` 標記，省略時 worker 退回 `op`-based 預設，單純 add / delete 沒問題、複合更新就會錯）：

| Order 動作 | 推進 `pending_ops` 的 op | `group` |
|---|---|---|
| 新增訂單 | 1 筆 `add` | `grow` |
| 取消 / 軟刪除 | 1 筆 `remove`（用刪除前的 quantity / deadline） | `shrink` |
| 延後 deadline（更晚的 `requested_delivery_date`） | `remove`（舊 deadline）+ `add`（新 deadline） | 兩筆都 `shrink` |
| 縮減 quantity（更小的 `wafer_quantity`） | `remove`（舊 qty）+ `add`（新 qty） | 兩筆都 `shrink` |
| 提前 deadline | `remove`（舊）+ `add`（新） | 兩筆都 `grow` |
| 增加 quantity | `remove`（舊）+ `add`（新） | 兩筆都 `grow` |
| 同時改：qty 跟 deadline 反向變化（一邊鬆一邊緊） | `remove` + `add` | **任一邊變鬆就標 `shrink`**（讓所有 demand 變鬆的先跑完，整個 horizon 騰出空間，再讓 grow phase 動）。`_build_patch_compound` 的實作：`group = "shrink" if (qty_smaller or deadline_later) else "grow"`。 |

> 演算法不認得 modify，由呼叫方拆成 `remove` + `add`，且**兩筆必須打進同一個 group**（worker 是「shrink 全跑完才開始 grow」、組內 FIFO，§4.3 解釋為什麼）。
> `requested_by` 必填 — `add` 失敗時 worker 用它呼叫 `websocket.notify_user(...)`。

兩種接法擇一：

**3.3.A（推薦）打 `POST /schedule/operations`**

```python
import httpx

httpx.post(
    "http://backend/api/v1/schedule/operations",
    json={
        "op": "add",
        "group": "grow",            # 新增 / 提前 / 增量 → grow；刪除 / 延後 / 縮減 → shrink
        "order_id": str(order.id),
        "order_number": order.order_number,
        "wafer_quantity": order.wafer_quantity,
        "deadline": order.requested_delivery_date.isoformat(),
        "requested_by": str(actor.id),
    },
    headers={"Authorization": f"Bearer {service_token}"},
)
```

`POST /schedule/operations` 自動處理 INCR + ZADD 與條件式 `.delay()`（status 不為 `running` 才觸發）。權限 `scheduler+`。

**3.3.B 程序內直連 Redis**

省 HTTP round-trip，但 service 層多 Redis / Celery 耦合：

```python
import json
from redis import Redis
from app.core.config import get_settings
from app.services.scheduling import (
    PENDING_OPS_KEY,
    PENDING_OPS_SEQ_KEY,
    score_for_op,
)
from app.workers.scheduling import run_scheduling_task

_redis = Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)
seq = _redis.incr(PENDING_OPS_SEQ_KEY)
payload = {"op": "add", "group": "grow", "_seq": seq, ...}
_redis.zadd(PENDING_OPS_KEY, {json.dumps(payload): score_for_op(group="grow", seq=seq)})
run_scheduling_task.delay()
```

> Redis key 常數跟 `score_for_op` 編碼住在 `app/services/scheduling.py`（producer ↔ consumer 的契約屬於 services 層），不在 `app/workers/scheduling.py`。Celery task object（`run_scheduling_task` / `rebuild_schedule_task`）才從 `workers/` 進來 — 這是 api → workers 唯一允許的依賴方向。

> 排程執行中又有新操作進來不需要特別處理 — task 結束時會自動檢查 `pending_ops` 並再次 `.delay()` 自己。

---

## 4. 內部運作

### 4.1 演算法層（pure，無 IO）

`backend/app/services/scheduling.py` 是純算法核心，沒有 DB、Redis、FastAPI：

- 兩棵 30 天線段樹：`capacity_tree`（每日剩餘產能）、`deadline_tree`（每天 deadline 上的訂單總和）
- `priority_queue`：deadline 早 → wafer\_quantity 大 → order\_number 字母順序
- `pinned_orders`：**強制日期**（PinnedOrder = order\_id, order\_number, qty, deadline, fake\_deadline）。pq 跟 pinned\_orders 互斥 — 一筆訂單同時間只能在其中一邊。
- `add_order` / `remove_order` / `pin_order` / `unpin_order` / `compute_schedule` / `advance_day` / **`rebuild_state`** 七個入口
- **Producer ↔ consumer 契約**：Redis key 常數（`STATE_KEY` / `STATUS_KEY` / `PENDING_OPS_KEY` / `PENDING_OPS_SEQ_KEY`）跟 `score_for_op` 編碼也住在這個檔，因為兩端（API 跟 worker）都要對得上，把契約放在共同上游避免 api → workers 反向依賴（RULES.md §3）。
- 演算法詳細推導見 [`backend/CLAUDE.md`](../backend/CLAUDE.md) §業務規則

**Admission control invariant — 不可能有「pinned 滿載 10000 + pq 有 dl=today 訂單」這種 state**

`pin_order` 跟 `add_order` 的容量檢查都用 `capacity_tree.query(rel)` — 它回傳的是「day 1 到 day `rel` 的累計剩餘產能」。配合 backward-fill 的 reservation：
- pin Y(qty=10000) 到 today 之後，day 1 prefix sum = 0，任何後續 `add_order` with dl=today 必 reject。
- 反過來：先 add X(qty=2000, dl=today)，day 1 prefix sum = 8000；後續 pin Y(qty=10000, fake=today) 看到 8000 < 10000 → reject；只有 pin Y(qty=8000, fake=today) 剛好用滿才會通過，此時 X 跟 Y 加總正好 10000，仍可行。

所以「pinned 把今天吃滿但 pq 還有 dl=today 訂單」這個 state 在 admission 正確的前提下**不可達**。reviewer 提出的 P1-1 場景（advance_day 之後該 pq 訂單卡死）的前提條件因此排除掉。`tests/services/test_scheduling.py::test_p1_1_invariant_*` 三個測試把這個 invariant 鎖住 — 如果後續有人改 admission control 把這個保證打破，test 立刻紅燈。

**Invariant break 時的處理：`_apply_remove_to_trees` 殘餘 → raise**

remove 的 forward give-back 走完還有 `remaining > 0`，代表線段樹的 obligation 跟訂單實際 quantity 對不上 — state 已腐化。如果只 log warning 然後讓演算法繼續跑，後續所有 `compute_schedule` 結果都會被污染，materializer 還會把錯誤 schedule 寫進 DB，前端看到不對的數字、完全沒人會發現出錯。

選擇 raise `RuntimeError("invariant broken")` 而不是 return 一個 `status="capacity_exceeded"` 的 `ScheduleResult`：invariant break 是 programming bug / state corruption，不是業務 input 錯誤。用 exception 是要讓 stack trace 進 Sentry / log shipper，讓 ops 知道「這次失敗不是 user 的問題」。`status` 是業務語意失敗的 channel，混用會誤導。

Batch admission 改寫之後，這個 raise 會冒到 `run_scheduling_task` 的外層 except，把整支 task 寫 `status=failed` 並 re-raise。注意這跟舊 saga 設計差異很大：以前一個 compound 的 invariant break 只壞那一個 compound、其他 compound 繼續；現在 batch tree update 是一次性的，一旦 invariant 出問題代表整個 state 已經污染，整支 task 失敗是正確姿勢（state 不該繼續被汙染的演算法操作，ops 介入手動 rebuild）。實務上不會發生（`is_batch_feasible` 已經先驗過容量），這條 path 純粹是 defensive — 如果觸發了，就是真有 state corruption 要查。

#### 4.1.2 PQ / pinned 資料結構與複雜度

| 集合 | 內部結構 | 為何 |
|---|---|---|
| `pq_index` | `dict[order_id, SchedulingOrder]` | 唯一的 pq 容器。EDF 順序不在這裡維護 — 需要 ordered iteration 的 caller (`compute_schedule` / `advance_day`) 在呼叫當下透過 `_iter_pq_edf_sorted` 做 bucket sort。先前用 `sortedcontainers.SortedKeyList` 每次 insert / remove 都要 O(log n) 排序；改成 dict 之後 insert / remove / contains 全 O(1)。 |
| `pinned_orders` | `dict[order_id, PinnedOrder]` | Python 3.7+ dict 保留插入序，所以 iteration 順序對 compute_schedule / log replay 都是穩定的；同時 contains / remove 也是 O(1)。 |

**`_iter_pq_edf_sorted(state)`**：把 `pq_index.values()` 依 `(deadline_rel, -qty, order_number)` 排好回傳 list。實作是 30 個 bucket（每天 deadline 一個）+ 桶內 `sorted` tie-break：

- bucket placement pass：O(n)
- 桶內排序：每桶 K_d 筆 → O(K_d log K_d)；總和 worst case（全擠同一天）O(n log n)，typical case（30 天均勻分布）O(n + n log(n/30))。
- 對 n = 1000 來說 typical case 約 5000 ops，比舊 SortedKeyList「每次 mutation 都 O(log n)」累積成本（K_mutation × log n）划算很多。

每個 op 的複雜度（n = pq 大小，p = pinned\_orders 大小）：

| 操作 | 複雜度 | 主要 cost |
|---|---|---|
| `add_order` | **O(D log D)** | tree backward-fill；pq 本身 O(1) |
| `remove_order` | **O(D log D)** | tree forward give-back；pq 本身 O(1) |
| `pin_order` | **O(D log D)** | tree swap；pq + pinned dict 都 O(1) |
| `unpin_order` | **O(D log D)** | tree swap；pinned + pq dict 都 O(1) |
| 「is in pq」/「is pinned」guard | **O(1)** | dict contains |
| `_pq_add` / `_pq_remove_by_id` | **O(1)** | pure dict op |
| `compute_schedule` | **O(n)** bucket + O(Σ K_d log K_d) tie-break + O(p) pinned pass | 一個 `_iter_pq_edf_sorted` 加上 daily fill loop |
| `advance_day` | 同上 | 主要是 `_iter_pq_edf_sorted` 一次 |
| batch admission 的 K-leaf-op 結構化 pass | **O(K)** | 全是 pq dict op |

**advance\_day boundary 語意調整**：以前 boundary order（今天做了一部分、明天還要接著做）會被「保留在原本的 pq 位置」。Phase-3 重構之後，boundary 訂單 qty 變小，sort\_key (deadline, -qty, name) 改變 — 新的 dict-backed pq 沒有「位置」可言，反正 `_iter_pq_edf_sorted` 下次叫到就會把它擺到新的 EDF 位置。例如同 deadline 下原本 f (qty=2000) g (qty=2000) 是 f 先（"f" < "g"），advance\_day 之後 f' (qty=1000) g (qty=2000)，因為 -1000 > -2000，f' 變成排在 g 後面。這比舊的「位置鎖死」更 EDF-correct，spec 文件已經跟著改了。對應測試 `test_advance_day_processes_pq_and_shifts_trees` 的 assertion 也用 `_iter_pq_edf_sorted` 拿排序結果再 index。

#### 4.1.1 Pin 機制：把訂單鎖到特定生產日

「pin」分兩種，DB 用兩支獨立 boolean 表示，演算法只認其中一種：

| DB 欄位 | 由誰寫 | 對 scheduler state 的影響 | 對前端的意義 |
|---|---|---|---|
| `is_pinned` + `pinned_production_date` | worker 透過 `apply_schedule` | 訂單從 pq 搬到 `pinned_orders`，trees 改用 `fake_deadline` 索引 | 該訂單的實際生產日是 `pinned_production_date`，不會被 EDF 推遲 |
| `is_processing_locked` | order CRUD service（create / update / batch-update 設 true）+ scheduler `apply_schedule`（清 false） | **無**（這個 flag 不影響演算法） | 「目前有 op 在排程器佇列裡」，前端據此 disable 該列的 inline edit |

**Production pin 接受條件**：跟 add\_order 一樣 — 把 `fake_deadline` 當成新 deadline 看，問「現在的 trees 容得下嗎？」更精確：

1. 把訂單從 pq 跟 trees 暫時移除（先 free 掉它在 real deadline 的占用），這樣等於把「需要 X wafers」這件事重新放到桌上。
2. 看 `capacity_tree.query(fake_deadline) >= wafer_quantity`。若不通過 → undo（把訂單還回 pq + trees 還原）→ 回 `capacity_exceeded`。
3. 通過 → 把訂單以 `fake_deadline` 當 deadline 寫進 trees、加進 `pinned_orders`。Real deadline 跟 fake deadline 都記在 `PinnedOrder` 上以便日後 unpin。

**為什麼接受條件這樣設**：因為 trees 一律以「目前每筆訂單的有效 deadline」為索引（pq 訂單是 real deadline，pinned 訂單是 fake deadline），`add_order` 用的容量檢查邏輯就能不變地套用到 pin 上 — 同一個 `capacity_tree.query(rel) >= qty` 公式。實作上甚至直接重用 `_apply_remove_to_trees` / `_apply_add_to_trees`。

**Compute schedule 的兩階段填法**：

1. **先放 pinned**：每筆 `PinnedOrder` 在 `fake_deadline` 那天直接吃 `wafer_quantity` 的容量。沒有跨日切分。
2. **再 EDF 填 pq**：用「pinned 扣完之後」的剩餘容量，按 pq 順序逐筆從第 1 天往後 forward fill 到 real deadline 那天為止。

**Advance\_day 跟 pinned 的關係**：base\_date 推進時，`fake_deadline == 今天` 的 pinned 訂單視同「今天就會做掉」 —
- 從 `pinned_orders` 移走、tree 上的占用也撤掉。
- 它們吃掉的 wafer 數量算進 day-1 的 10000 額度裡，所以 pq 累加器的上限變成 `DAILY_CAPACITY - sum(pinned_today.wafer_quantity)`，不是 10000。
- 處理完 pinned\_today 之後 pq 用剩下的額度走原本的「累加到上限就停」邏輯。

**Unpin**：基本上是 pin 的反向 — 先以 `fake_deadline` 當 deadline 把訂單從 trees 移除，從 `pinned_orders` 拿走；再以 real deadline 重新 add\_order 進 pq + trees。

**Rebuild\_state 跟 pinned 的關係**：DB 列裡 `is_pinned=true` 的訂單在 `list_for_scheduler` 回傳時會把 `pinned_production_date` 填進 `SchedulingOrder.pinned_production_date`，`rebuild_state` 看到不是 None 的就走 add+pin 雙步驟，把該訂單最終放進 `pinned_orders`。pin 失敗（典型原因：`fake_deadline` 已經被 base\_date 越過）會列在 `skipped`，訂單仍會留在 pq 當 fallback。

**範例**（呼應 spec 用的數字）：

設 base\_date = day 1，已存在訂單 a (qty=9000, dl=day3)、b (qty=1000, dl=day3)、c (qty=1000, dl=day3)。

- Pin b、c 到 day 1 都成功之後：
  - `capacity_tree` prefix sum = `[8000, 18000, 19000]`（day 1 因為 b+c 占了 2000，剩 8000；day 3 因為 a 占了 9000，剩 1000，prefix = 8000+10000+1000）
  - `deadline_tree` prefix sum = `[2000, 2000, 11000]`（day 1 上有 b+c=2000、day 3 上 a 加 b+c 全部累積 11000）
  - `compute_schedule` 出來：day 1 做 b1000+c1000+a8000、day 2 做 a1000
- 接著 unpin c：
  - `capacity_tree` prefix sum = `[9000, 19000, 19000]`、`deadline_tree` prefix sum = `[1000, 1000, 11000]`
  - `compute_schedule` 出來：day 1 做 b1000+a9000、day 2 做 c1000

### 4.2 Worker 一輪在做什麼（`backend/app/workers/scheduling.py`）

#### 共用 helper：`_finalize_run(state)`

三個 task 都在某個時點需要把「state 改完」這件事「物化」出去：算出排程 → 寫 DB scheduled_dates → 存回 Redis state → 廣播 `schedule.updated`。這四步驟抽成 `_finalize_run(state)`：

1. `compute_schedule(state)` 算出每筆訂單跨哪幾天
2. `order_service.apply_schedule(db, scheduled)` 寫回 DB（細節 §4.4）
3. `_save_state(state)` 序列化回 `schedule:state`
4. `websocket.broadcast({"type": "schedule.updated"})`

#### `run_scheduling_task` — **batch admission 模式**（後 Phase-2 saga 改寫）

每次 task 呼叫**一次 drain pending queue 直到清空**。一輪 task 內部跑一個 while loop，每圈做：

1. `_read_pending_compounds()` 用 `ZRANGE` **不 pop** 把目前所有 pending 抓出來、按 priority 排序（shrink-group 先、grow-group 後，組內 FIFO）。malformed JSON 或 `op_count` mismatch 的 member 在這裡 ZREM + RPUSH 到 DLQ + ERROR log，drain loop 繼續。
2. **Reject-rate adaptive cap**：用 `_take_count_from_rate` 把 candidate 數壓到 `min(len(pending), ceil(1/p))`，p 是持久化在 Redis (`schedule:compound_reject_rate`) 的 EWMA 每筆 compound 被 reject 機率估計（初值 0.01、bounds `[1e-4, 1.0]`、alpha 0.05）。動機：當 N=1000 但其實第一筆就會 reject，halve 1000→500→250→...→1 會白做 log N 輪 `compute_batch_capacity_delta`；用「預期下一筆 reject 在第 ceil(1/p) 筆」直接把窗口壓到 ceil(1/p)，後面那些反正 infeasible 的 prefix 連算都不算。後面步驟 4/5 接受時 EWMA 推 p 往 0，步驟 3 reject 時 EWMA 推 p 往 1，所以這個窗口會自動隨工作負載 adapt。
3. 對抓到的 `compounds = pending[:take]` 做 **halving 二分搜尋找最大可行 prefix**：
   - 試 `[1..take]` → 用 `compute_batch_capacity_delta` 折成 per-day 表 → `is_batch_feasible(state, delta)` 比較 prefix sum 跟 `capacity_tree`。可行就接受。
   - 不可行 → 試 `[1..take//2]`，再 `[1..take//4]`...，到 `[1..1]` 仍不可行就 return `0`。
4. **`k == 0`**（連第一筆 compound 都塞不下）：`_reject_first_compound` ZREM 那筆 + drop secondary index + `_perform_compound_db_action(rejected)` + WS `schedule.compound_failed`。`_update_reject_rate(rejected=1)` 把 p 往 1 推一個 EWMA 步。再 continue 外圈 while。
5. **`k > 0`**：`_commit_accepted_batch` 一口氣處理 `compounds[:k]`：
   - 用 batch delta 一次更新兩棵樹（`apply_batch_to_capacity` 用 carry-back distribution，`apply_batch_to_deadline` 直接 point_update）。**樹只動一次，不論 batch 裡有 k 個還是 1 個 compound**。
   - 逐 compound 逐 leaf 跑 `_apply_compound_leaf_structural`：`add` / `remove` 只動 pq（樹的部分剛剛 batch 寫完了）；`pin` / `unpin` 呼 既有的 `pin_order` / `unpin_order`（它們有自己的 tree swap，不在 batch delta 內）。
   - ZREM 所有接受的 member（單一 round-trip）。
   - `_save_state(state)` 把更新後的 state 序列化回 Redis（整批一次，不是 per-compound）。
   - 逐 compound 跑 `_perform_compound_db_action(accepted)` + WS `schedule.compound_accepted` + SADD 進 `schedule:materialize_notify_pending`。
   - `_update_reject_rate(accepted=k)` 把 p 往 0 推 k 個 EWMA 步。
6. 外圈 while 再從步驟 1 重來（新 compound 可能在 batch 處理期間進來；下一輪用更新後的 p 重算 take）。
7. drain 結束才 `materialize_schedule_task.delay()` 一次（**整輪 drain 共用一個 materialize**，不是每 batch 一次）。
8. status 翻回 `idle`，最後檢查 `zcard pending_ops > 0` — 若有（drain 結束跟 status 翻完之間又有 compound 進來），看 `schedule:waiter_pending`：沒設就 self-`.delay()`；有設就 yield 給 waiter。

**幾個跟舊 per-compound 設計的重大行為差異**：

- **沒有 saga rollback**：`is_batch_feasible` 是先檢查的；一旦樹開始動，所有 compound 都會被接受。Compound 內部沒有「半套狀態」這種事 — batch 接受前就排除了。原本的 snapshot + restore 整段刪除。
- **`compound_failed` 只有一條 path**：「binary search 找到 k=0」。其他失敗模式都不會 emit 這個 WS event：
  - 樹層 invariant break（`RuntimeError` from `_apply_remove_to_trees` residual）→ 走外層 except，整支 task 寫 `status=failed`、re-raise（這次不會 mid-compound 出現了，因為 batch 預先檢查過了；如果還是發生，代表整支系統 invariant 出問題，不是 single compound 問題）。
  - Pin / unpin 在 `_apply_compound_leaf_structural` 裡失敗：**只 log warning、batch 繼續**。理由是 producer 端的 admission control 應該已經過濾 capacity 不夠的 fake_deadline；worker 看到失敗代表 producer / 線下手動操作出 bug，整個 compound 已經半執行（add 部分樹已動、pq 已 insert），rollback 不切實際 — 留下 warning 讓 ops 注意就好。
  - op_count mismatch / JSON parse 失敗：**走 DLQ**（ZREM + RPUSH 到 `schedule:pending_ops:dlq` + ERROR log）。不 emit `compound_failed` 因為 compound 的 `requested_by` 本身就不可信。
- **`schedule.materialized` 通知頻率變高**：以前 per-compound 一個 SADD；現在 batch 內每個 compound 都 SADD 自己的 `requested_by`，但因為 set 語意自然 dedupe，同個 user 多筆 compound 還是只通知一次。
- **`schedule:status` 維持 `running` 整個 drain 期間**：drain 沒結束、status 不會翻 `idle`，所以 `advance_day` / `rebuild` 不會在 drain 中間插隊。代價：drain 可能持有 lock 很久（high 流量場景）；好處：不需要 saga / atomicity 的細緻處理。
- **`compute_schedule` + DB write 不在這條 path**：跟 Phase 4 一樣，這些是 `materialize_schedule_task` 的工作，跟 fast-path 解耦。

**batch admission 的複雜度分析**：

對 K 個 compound 的 batch，舊設計成本：
- 每個 compound 一個 `SchedulerState.to_json()` snapshot（大 dump，幾百 bytes ~ 幾 KB），共 K 次
- 每個 leaf op 一個 `_apply_add_to_trees`（O(log D × D) 後向填充）或 `_apply_remove_to_trees`（O(D × log D) 前向恢復），共 K × avg(ops) 次
- 每個 compound 一個 `_save_state`、一個 `compute_schedule + apply_schedule`（Phase 4 之後變每 compound 一次 SADD + 一次 delay）

新設計成本：
- `_read_pending_compounds` ZRANGE 一次 O(K log K)
- binary search 最多 log₂ K 輪，每輪 `compute_batch_capacity_delta` O(K + D)、`is_batch_feasible` O(D log D)
- `apply_batch_to_capacity` 一次 O(D log D)（不論 batch 多大）、`apply_batch_to_deadline` 一次 O(D log D)
- `_apply_compound_leaf_structural` per leaf 只動 pq（pin/unpin 還是 O(D log D)，但通常很少）
- `_save_state` 一次（整 batch 共用）

實質節省：樹的後向填充從 K 次降到 1 次；snapshot 從 K 次降到 0 次。對 K=500 的 batch，差異是肉眼可見的 throughput 提升（benchmark 在 §5.3）。

**`_commit_accepted_batch` 的 step 順序為什麼是「樹 → pq → ZREM → save → DB / WS」**：
- 樹 + pq 先：state 一致性最重要、決定下一輪 drain 的 feasibility check 是否正確。
- ZREM 在 save 之前：如果 save 失敗，至少 pending queue 已經清乾淨，重啟後不會把已經套用的 compound 重跑（雖然 save 失敗本身會把 task 標 failed 並 re-raise）。
- DB / WS 在 save 之後：成功的最後一哩；萬一 DB 寫一半 crash，state 已經是 post-batch、queue 已清，重啟看到的是「state 已套用但 db_action 沒落地」的少數 user 通知缺失，可以靠 materializer 重新 dispatch 補上。

#### `materialize_schedule_task` — Phase 4 deferred DB writer

Self-coalescing celery task，把多個 compound 的累積 state 一次寫進 DB、再 per-user 通知。

**為什麼要 coalesce**：fast path 每個 compound 都 `.delay()` 一支 materializer。如果 5 秒內進來 50 個 compound，舊設計會跑 50 次完整的 DB rewrite；coalescing 後最多跑幾次（每次處理當下所有累積 user）。實際上 user spec 點到「上次寫入db還未完成，要等完成後自動去看有沒有新的狀態要寫入，再一次寫入到最新狀態」— 就是這個模式。

**三個 Redis key 協調**（常數定義在 `services.scheduling`）：

| Key | 角色 | 由誰寫 |
|---|---|---|
| `schedule:materialize_running` | `SET NX EX 300` 的單行 flag — 一次只允許一個 materializer 在跑 | materializer 進門 / 離開 |
| `schedule:materialize_notify_pending` | Set of `requested_by` UUIDs — 等著被通知「DB 已更新、快 refetch」的 user | fast path 每次 compound 成功就 SADD；materializer drain 取走 |
| `schedule:materialize_notify_processing` | Set — 目前 materializer 正在處理的批次 | materializer 透過 `RENAME` 原子 swap 過來；DB 寫完才 `DEL` |

**Loop body 每一輪**：

1. `RENAME notify_pending → notify_processing`（原子 swap：併發 fast task 之後的 SADD 落到新生成的 notify_pending；不會影響本批次）。如果 notify_pending 不存在，rename 會 raise `ResponseError`，迴圈結束。
2. `SMEMBERS notify_processing` 拿到這批要通知的 user 集合。
3. `_load_state()` → `compute_schedule(state)` → `apply_schedule(db, scheduled, pinned_map)` 把最新 in-memory state 寫進 DB。
4. 對每位 user `notify_user("schedule.materialized")`，他們的前端收到就會 invalidate cache + refetch `GET /schedule/result`。
5. `DEL notify_processing` 收尾。
6. 回去 step 1 看看有沒有新 SADD 進來。

**Crash 復原**：每輪開頭先檢查 notify_processing 是否殘留（前一次 materializer 在 step 3-5 之間死掉留下的）。如果有就 `SUNIONSTORE` merge 回 notify_pending 再 `DEL processing`。下一輪會正常處理這些 user，**不會 silently 漏通知**。

**空 state 防禦**：step 3 前先 `rds.exists(STATE_KEY)` 檢查。為什麼要這個檢查 — 在極端 race（fast-path SADD notify 後、`_save_state` 前 materializer 就被排程）或 Redis 被 flush 的場景，`STATE_KEY` 可能不存在；`_load_state()` 會 fallback 回 `SchedulerState.initial(today)`（空 state），`compute_schedule` 回 `[]`，`apply_schedule(db, [], ...)` 走 `clear_scheduled_dates` 把**每個 scheduled 訂單的 daily_breakdown / scheduled_production_date 全部清空**。空 state 跑 apply_schedule 是無聲的資料清空操作，比拋例外更可怕（沒人會立刻發現）。沒有這個檢查的話，罕見但發生時 = 全套 daily_breakdown 被清光、前端整個排程看起來消失、要 rebuild 才救得回。

檢測到空 state 時不能直接 break — pending_processing 的 user 還沒被通知，掉訊息也是 bug。改成 `SUNIONSTORE` 把 processing 合回 pending（保住訊息）+ `DEL processing` + break loop + ERROR log。下次有 task 真的寫了 state 之後 materializer 再被觸發時會正常處理。把這條 path 從「無聲掉資料」改成「可觀測的 short-circuit」。

**post-release 重新派工**：迴圈跳出來、`DEL materialize_running` 之後，再 `EXISTS notify_pending` 看看有沒有「在我們最後一次 empty rename 跟釋放 flag 之間」進來的新 user。有就 `materialize_schedule_task.delay()` 把自己再派一次。這條保證沒有 user 會被卡在 idle window 裡。

**Race-with-advance_day**：materializer 是 read-only 對 state（讀 state、寫 DB），不參與 `state_writer_lock`（理由見 §2.4）。advance_day / rebuild 是 read-write 對 state，會持 lock。兩者 DB write 都會通過 `apply_schedule.clear_scheduled_dates` 然後寫新值；如果 concurrent，PostgreSQL transaction 序列化、後 commit 者贏。worst case：materializer 用 pre-advance_day state 寫 stale `daily_breakdown`、覆蓋 advance_day 剛寫的新值；status 欄（`in_production` / `completed`）不受影響因為 materializer 的 `set_schedule_dates` 有 P0-1 防禦不會 demote、也根本不寫 `completed`。

stale window 由 **advance_day / rebuild 在 finally 結尾 `materialize_schedule_task.delay()` 顯式再派一支**收尾 — 那支 materializer 讀 fresh state、用 fresh daily_breakdown 把 stale 覆蓋掉，整個 stale 時段 ≈ 一個 materializer cycle（50ms-2s）。沒有這個 finally retrigger 的話 stale 會撐到「下次 user PATCH」，最糟在連假期間整天 stale。注意 `.delay()` 前要先 SADD 一個 system sentinel 進 `notify_pending`，否則 in-flight materializer 還沒釋放 `materialize_running` 時新派的那支會 `skip_concurrent` 退出、in-flight 跑完的 post-release re-trigger 又看到 pending 空的 → stale leak 回「下次 user PATCH」。詳見 §2.4 末段「Corner case — follow-up dispatch 撞 `skip_concurrent`」。

**WS event 對照**：

| 事件 | 何時發 | 誰收到 |
|---|---|---|
| `schedule.compound_accepted` | batch admission 接受該 compound 之後 | compound 的 `requested_by` |
| `schedule.compound_failed` | binary search 連 `[1..1]` 都 infeasible，該 compound 被 drop | compound 的 `requested_by` |
| `schedule.materialized` | materializer 一批寫完 DB | 那批裡每個 `requested_by` |
| `schedule.updated` (broadcast) | advance_day / rebuild 完成 | 所有連線的 client（system-initiated 才 broadcast） |

**對前端的影響**：以前 fast task 完成就 broadcast，所有連線 client 都會 invalidate；現在只有「該 user 自己改的訂單已寫進 DB」才收到 notify。其他 user 看到的 `GET /schedule/result` 可能落後一個 materializer 週期，user spec 接受這個 trade-off（「其他人則是不會接受到broadcast，改成可能是定時或是要手動刷新」）。要降低延遲可以前端加 polling 或 advance_day 之類 system 動作的 broadcast 觸發 refetch。

#### Waiter-flag race fix（為什麼 `run_scheduling_task` 收尾要查旗標）

Drain loop 結束之後、`status=idle` 翻完、`state_writer_lock` 釋放之間的最後一哩窗口會開給 waiter 插隊（`advance_day` / `rebuild` 在 `_wait_for_idle_run` 看到 `status=idle` 就會跳出 wait）。這個窗口本身會造成**新的 race**：

1. `run_scheduling_task #1` 整輪 drain 結束、`_set_status("idle")`
2. waiter（之前已經在 `_wait_for_idle_run` polling）剛好下一輪 poll 看到 idle，跳出 wait
3. waiter 開始它自己的工作（讀 state、改 state）
4. **同時間 run_task #1 繼續往下走**，看到 `zcard > 0`（drain 結束跟 status 翻完之間有新 compound 進來），呼 `.delay()`
5. 新的 `run_scheduling_task #2` 被 Celery 撿起來開跑
6. **#2 跟 waiter 同時都在寫 `schedule:state`** — 後寫者覆蓋前寫者，state 被破壞

修法：waiter 在進 `_wait_for_idle_run` 之前先 SET `schedule:waiter_pending`（帶 10 分鐘 TTL），離開 task body 時在 `finally` 裡 DELETE。`run_scheduling_task` 在收尾 `zcard` 檢查時看到旗標就**讓位**（不 retrigger），把 retrigger 的責任交給 waiter 結尾去做。

時序保證：
- 旗標的生命週期完全包住 waiter 的 wait + work + retrigger，所以從 run_task #1 開始 finalize 到 waiter 真的結束之間，旗標一直是設的
- run_task #1 step 7 的 `GET` 看到旗標還在 → 讓位
- waiter 結束（含已經 `.delay()`）才 DELETE 旗標 → 之後再進來的 run_task 看不到旗標，恢復正常 retrigger
- TTL 10 分鐘是 crash safety — 如果 waiter 在 finally 之前死掉（例如 worker container 被 kill），10 分鐘後旗標自動過期，系統不會永遠卡在「讓位」

#### Status-claim race fix（為什麼 waiter body 也要 claim `schedule:status`）

waiter flag 解掉了「run_task #1 retrigger 跟 waiter 同時跑」這條 race，但留下另一條：**waiter 自己在做事的期間 `schedule:status` 仍是 `idle`**。`POST /schedule/trigger` 的 409 邏輯就是去看這支 key，看到 `idle` 就會直接 `run_scheduling_task.delay()` —

1. `advance_day_task`（或 `rebuild_schedule_task`）走過 `_wait_for_idle_run`，準備呼 `_load_state()`
2. 此時 `schedule:status` = `idle`（前一支 run_task 已經把它寫回去了）
3. 有人手動戳 `POST /schedule/trigger`：endpoint 看 `idle` → 不 409、`run_scheduling_task.delay()`
4. 新的 `run_scheduling_task` 跟 waiter **同時讀寫 `schedule:state`**，又是一次後寫者覆蓋前寫者；waiter 已經 ZPOPMIN 走的 op 還會永久消失

修法：waiter 在 `_wait_for_idle_run` 結束之後、開始動 state 之前先 `_set_status("running")`，body 正常結束時在 inner success path 清回 `idle`。從這時起：

- `POST /schedule/trigger` 看到 `running` → 直接回 409，不會在背後偷偷打另一支 run_task
- `POST /schedule/operations` 也只會把新 op ZADD 進佇列，**不會** `run_scheduling_task.delay()`（同一支 endpoint 也是 status != running 才 delay），等到 waiter 結束、status 回 idle、且 zcard > 0 時 waiter 自己會 `.delay()`，所以也不會丟 op
- inner exit 跟 outer waiter-flag finally 是兩層，先還 status 再清 flag，順序剛好讓「有人剛好在 status 還沒翻 idle 之前 retrigger」這個邊界永遠看到 status=running

例外處理（PR-review 補強之後的契約）：waiter body 任何一步丟例外時，inner `except` 會把 status 寫成 **`failed`**（不是 `idle`），帶著 `error=str(exc)` 跟 `finished_at`，然後 re-raise。這跟舊版「不論成敗都還 idle」是有意的差異：原本的設計把 `failed` 當成 run_task 專屬的 sentinel，但實務上 `GET /schedule/status` 是 ops 唯一能看到「排程是否健康」的地方，把 advance_day / rebuild 失敗藏成 idle 等於是在掩蓋錯誤。改成 `failed` 之後：

- ops dashboard / 監控 grep `state == "failed"` 才能即時抓到 advance_day 沒換天、rebuild 沒重建的事故
- 沒有「`failed` 永久卡住 /trigger」的副作用 — `/trigger` 的 409 判斷只看 `running`，看到 `failed` 一樣會 dispatch；下次任何 `run_scheduling_task` / `advance_day_task` / `rebuild_schedule_task` 跑成功就會把 status 蓋回 `idle`，不需要人工介入
- Celery 的 traceback 仍然會自己記（因為我們 re-raise）

#### State-writer Redis SETNX lock（為什麼 status + waiter 還不夠）

waiter flag + status claim 兩層解決了「同類型 task 串接 race」（advance_day 等 run_task、run_task 讓位給 waiter），但**沒解決**「兩個 `run_scheduling_task` 同時起來」這條 race。在 prod `--concurrency >= 2` 或多 worker container 場景下：

1. Task A 讀 state V1、mutate 成 V1'，準備 save
2. Task B 讀 state V1（A 還沒 save）、mutate 成 V2'
3. B save V2'，A save V1' — 後寫者覆蓋前寫者，**其中一個 compound 的影響徹底蒸發**

`schedule:status` 解決不了這個：它是 advisory observability key，多個 reader 看到 `idle` 都會繼續、`_set_status(running)` 跟下一個 `_save_state` 之間的 gap 就是 race window。`enqueue_compound` 端也一樣 — 兩個 producer 幾乎同時觀察到 `status != running`，都會 dispatch task，於是同類型 task 並發。

**修法**：新 Redis key `schedule:state_writer_lock`（§2.4），用 atomic `SET NX EX` 當 hard mutex：

- 三支 state-writing task（`run_scheduling_task` / `advance_day_task` / `rebuild_schedule_task`）進 body 前先 `_try_acquire_state_lock(task_id)`
- finally 用 Lua CAS-delete（只刪 value == 自己 task_id 的 lock）防止「TTL 過期 → 別人接手 → 我們的 finally 誤刪別人 lock → race window 重開」這個二次 race
- `run_scheduling_task` 拿不到就直接 return（不動 status、不 pop queue）— 持有 lock 的那支結束時會 retrigger，下一個 compound 自然會被處理
- `advance_day_task` / `rebuild_schedule_task` polling 等 lock（`_acquire_state_lock_blocking`），最多等 `SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS`；超時 → raise `RuntimeError`。`@celery_app.task` decorator 用 `autoretry_for=(RuntimeError,)` + `max_retries=3` + `retry_backoff=60`（exponential，cap 600s）+ jitter 自動重試 — 必須這樣做因為 advance_day 是「每天 00:00 一次」的硬性 schedule，單次失敗讓那整天的 `mark_in_production` / `mark_completed_outside_set` 都不會跑、訂單 status 工作流停滯 24 小時要等下一個 Beat tick。三次仍失敗才走 status=`failed`，讓 ops 透過 `GET /schedule/status` 看見、手動介入。
- **materializer 不拿 lock**：跑 slow path（compute_schedule + per-order DB writes，N=500 約 2 秒）把它鎖進 mutex 等於是把 user-facing PATCH 延遲堆在 materializer 上面 — 跟 Phase-4 fast/slow split 的核心目的相反。改成讓 materializer 自由跑，唯一 trade-off 是跟 advance_day / rebuild 並行時可能寫 stale `daily_breakdown`（status 欄不受影響）；advance_day / rebuild 在 release lock 後**主動再 dispatch 一支 materializer**（`materialize_schedule_task.delay()` 前要先 `SADD __system_advance_day__` 進 `notify_pending`，理由詳見 §2.4 末段「Corner case — follow-up dispatch 撞 `skip_concurrent`」），讀 fresh state 把 stale 覆寫，所以 stale window 有界 ≈ 一個 materializer cycle。

選 SETNX + CAS-delete 而不是 Redlock：單一 Redis 實例的場景用單一 SETNX 就夠，Redlock 是給多 Redis 副本的、會引入不必要複雜度。也不選「靠 Celery `--concurrency=1` 約束」把正確性壓在 ops 紀律上太脆，文件講過很容易忘記。`status` + `waiter_pending` 仍然保留，因為它們提供 observability（`GET /schedule/status`）跟 cooperative retrigger（讓 waiter 知道誰要接手 delay）— lock 補的是正確性層的洞，這兩層是 UX / ergonomics 層、互補不互斥。

#### `advance_day_task`（每天 00:00 UTC，由 Beat 觸發）

整個 task body 包在 `try / finally` 裡 — 進入時 `_set_waiter_flag()`，離開時 `_clear_waiter_flag()`。等 in-flight run 結束之後，再用一層 inner `try / except` claim `schedule:status`：成功走完寫 `idle`，body 任何一步 raise 寫 `failed` 並 re-raise。

**Phase 3 加入了 status workflow**：advance_day 觸發時做兩支額外 DB UPDATE — 把「昨天的 in_production」訂單改成 `completed`、把「今天的 d0 production」訂單改成 `in_production`。

1. **`_set_waiter_flag()`**（waiter-flag race fix，見下面 "Waiter-flag race fix" 段）
2. `_wait_for_idle_run(...)` 輪詢 `schedule:status` 等 `running` 結束（最多 5 分鐘，超時就警告繼續）
3. **`_set_status("running")`**（status-claim race fix，見下面 "Status-claim race fix" 段）
4. `_load_state()` → 先 `compute_schedule(state)` 在 **OLD state** 上算「今天的 d0 production」（filter `scheduled_date == base_date`），把這些 order_id 存成 `today_locked_in_ids`
5. `new_state = advance_day(state)` → 演算法把 day-1 從 state 移走、shift trees、base_date++
6. 一個 DB session 裡做三件事（**順序很重要**）：
   - `order_service.apply_schedule(db, compute_schedule(new_state), pinned_map)` 寫 `scheduled_production_date` / `expected_delivery_date` / `status='scheduled'`（針對 new state 還在的訂單）
   - `order_repo.mark_completed_outside_set(db, new_alive_ids)` 把 `status='in_production' AND id NOT IN new_alive_ids` 全部改成 `'completed'`
   - `order_repo.mark_in_production(db, today_locked_in_ids)` 覆蓋 apply_schedule 剛寫的 `'scheduled'`，把今天 d0 的訂單 status 升級成 `'in_production'`
7. `_save_state(new_state)` + `websocket.broadcast({"type": "schedule.updated"})`
8. 只有 `ZCARD pending_ops > 0` 才 `run_scheduling_task.delay()` 把等待期間累積的 compounds 消化掉
9. **success：`_set_status("idle")`**
10. **except：`_set_status("failed", error=str(exc))` 然後 re-raise**
11. **outer `finally: _clear_waiter_flag()`** — 不論 body 正常或 raise 都會跑

**為什麼 step 6 的順序是「先 apply_schedule、再 mark_in_production」**：apply_schedule 對 new state 還在的訂單（包含 boundary — 今天做了一部分 + 明天還要繼續做）一律寫 `status='scheduled'`。但 boundary 也屬於「今天 d0」一部分，需要 `status='in_production'`。把 mark_in_production 放在 apply_schedule 之後，就是讓它對 boundary 做覆寫升級（`scheduled` → `in_production`），這樣 boundary 訂單最終 status 是 `in_production`、`scheduled_production_date` 是 new state 算出來的下個生產日（= 明天）— UI 用 status 判斷「正在做」比用 date 判斷可靠。

**為什麼 `mark_completed_outside_set` 用 set-difference 而不是 date 判斷**：boundary 訂單跨多個 calendar day 是常態。用 date 比較很容易在邊界搞錯；用「new state 還在不在」當訊號最乾淨：訂單 `in_production` 但已經不在 state 的 pq / pinned_orders 任一邊 = 做完了。

#### `rebuild_schedule_task`（由 `POST /schedule/rebuild` 觸發）

跟 advance_day 一樣套兩層保護：外層 `try / finally` 持有 waiter flag、內層 `try / except` claim status，成功寫 `idle`、失敗寫 `failed` + re-raise。

1. **`_set_waiter_flag()`**
2. `_wait_for_idle_run(...)` — 跟 `advance_day_task` 共用同一個等待 helper
3. **`_set_status("running")`**
4. 從 Redis 讀現有 `schedule:state.base_date`（沒有就用今天）
5. `order_service.list_for_scheduler(db)` 拿出 `(orders, creators)`
6. `rebuild_state(orders, base_date)` 得到 `(new_state, skipped)`
7. `_finalize_run(new_state)` — 同樣自己 finalize（理由同 advance_day）
8. 對每筆 `skipped` 查 `creators` map，`websocket.notify_user(creator, "schedule.rebuild_skipped")`
9. 只有 `ZCARD pending_ops > 0` 才 `run_scheduling_task.delay()` 消化等待期間累積的 ops
10. **success：`_set_status("idle")`**
11. **except：`_set_status("failed", error=str(exc))` 然後 re-raise**
12. **outer `finally: _clear_waiter_flag()`**

例外路徑統一行為：三支 task（`run_scheduling_task` / `advance_day_task` / `rebuild_schedule_task`）body 任何步驟 raise 時，worker 把 status 標 `failed`、寫進 `error` 欄位、re-raise，Celery 也會記錄 traceback。`failed` 在 `/trigger` 的 409 邏輯下不會卡住下一輪 — 只有 `running` 才會 409，`failed` 視同 idle 可以再 dispatch；下次成功的 task 會把 status 蓋回 `idle`，不必人工介入 Redis。

### 4.3 為什麼把 pending\_ops 拆成 shrink / grow 兩個 phase

排程演算法本身只認得 `add_order` / `remove_order` 兩個原子操作，所以 update 必須由 producer 拆成 `remove`（舊值）+ `add`（新值）兩筆 op 進佇列。但「兩筆同屬一個 update」這個資訊一旦進了 FIFO 就遺失了 — 如果 worker 只簡單地「先處理所有 remove 再處理所有 add」，跨 update 的 ops 會被打散：

```
producer 推（依時間序）：
  defer X：remove_X(舊 deadline 5/10)、add_X(新 deadline 5/15)
  advance Y：remove_Y(舊 deadline 5/20)、add_Y(新 deadline 5/05)

舊邏輯（remove-then-add）processing order：
  remove_X、remove_Y、add_X、add_Y

問題：
- remove_Y 釋放 5/20 之前的產能。
- add_X 先跑，可能把 5/01–5/15 的早期格子吃掉。
- add_Y 想塞 5/05 deadline 時，5/01–5/05 已被 X 占走 → 可能 capacity_exceeded。
```

實際上 X 的 defer 是「demand 變寬鬆」、Y 的 advance 是「demand 變嚴格」，後者本來就更該先拿到剛被釋放的早期產能。把兩個 update 的 remove + add 各自綁回原本的 update（atomicity）並按「demand 變鬆 → demand 變緊」的方向排序，才會得到合理結果。

**規則**：

- **shrink group（demand 變鬆 / 變沒）**：delete、defer（更晚的 deadline）、qty 變小。
- **grow group（demand 變緊 / 新增）**：add、advance（更早的 deadline）、qty 變大。
- shrink group 內所有 op 處理完，才開始 grow group。
- 兩個 group 內各自 FIFO（producer LPUSH 的順序，等同 update 發生的時間序）。
- 一個複合 update（例如 defer = remove(舊)+add(新)）的兩筆 op 必須由 producer 標記為**同一個** group — 這樣 add(新) 才會跟 remove(舊) 緊鄰執行，不會穿越到下一個 phase。

worker 端的實作是「**每跑完一筆就重新挑下一筆**」而不是「一次撈光全跑完再 fetch」。`_pop_next_op()` 直接做 `ZPOPMIN schedule:pending_ops`：sorted-set 已經依 `score = group_priority * GROUP_OFFSET + seq` 排好序，最小 score 那筆就是「shrink 群有的話先出，否則挑 grow 群裡最早的」。外層 `while True` loop 反覆呼叫直到佇列空。

這樣設計的好處是：mid-task 進來的 shrink op 不會被「還沒輪到」的 grow op 卡住。例如佇列原本是 `[grow_X1, grow_X2]`（score 都在 grow 區），worker 跑完 `grow_X1` 之後若 producer 剛好 ZADD 一筆 `shrink_Y`（score 在 shrink 區、< 任何 grow score），下一次 `ZPOPMIN` 會直接拿到 `shrink_Y` → 先處理，再回頭做 `grow_X2`。如果是「drain-all 再做完」的舊版設計，`shrink_Y` 要等到下一個 task invocation 才會被看到。

`group` 欄位省略時退回 `op`-based 預設（remove → shrink、add → grow），這個 default 由 schema validator `_default_group_from_op` 在 producer 端套上；複合更新若忘了顯式標 group，add 那半會用 default 掉到 grow phase，行為就是上面那段「舊邏輯」的失敗 case。

> Producer 端的責任：在拆 `remove` + `add` 時就要根據「這個 update 是讓 demand 變鬆還是變緊」決定 group。同時改 qty 跟 deadline 的混合 case（例如 qty 變小 + deadline 變早，一邊鬆一邊緊）：實作上以「**任一邊變鬆就標 `shrink`**」為原則 — shrink phase 先跑能把整個 horizon 的 demand 騰鬆，grow phase 再進去就比較容易過。對應 `_build_patch_compound` 的 `group = "shrink" if (qty_smaller or deadline_later) else "grow"`。

> **Score 編碼細節**：`GROUP_OFFSET = 10**12` 是 shrink/grow 兩個 score 區間的中間隔。shrink 群的 score = seq（< 10^12），grow 群的 score = 10^12 + seq。seq 從 `INCR schedule:pending_ops:seq` 拿，全域單調。要相撞需要 shrink 群在某次重啟內累積到 10^12 筆，遠超過任何實際工作量；同時 10^12 在 float64（線段樹 score 用的型態）的 exact-integer 範圍（2^53）內，ZPOPMIN 的排序行為不會因浮點誤差而錯亂。

---

### 4.4 DB 寫回 — 為什麼動到既有的 `services/order.py` / `repositories/order.py`

`docs/RULES.md §3` 與 `docs/DEVELOPMENT_GUIDELINES.md §1` 訂死的分層：

- `api/` 不能直接打 ORM，必須走 `services/`
- `services/` 接收與回傳 Pydantic schemas，**不回傳 ORM 物件**
- `repositories/` 是唯一可以寫 SQL 的層，**純 CRUD、零業務邏輯**

排程模組有兩段必須進入 backend 的 DB I/O：

1. `GET /schedule/result` — 列出所有 `status='scheduled'` 訂單
2. worker 跑完算法 — 清空舊排程日期 → 寫入新排程 → 改 status → 發 audit log → commit

兩段都在操作 `Order` entity。依專案 SOP（`docs/DEVELOPMENT_GUIDELINES.md §2`）每個 entity 各**一份** service / repo，所以這些函式必須加進 `Order` 既有檔，不能另開 `services/schedule.py` 把同個 entity 的 CRUD 切兩半。

**`backend/app/repositories/order.py` 新增三個純 CRUD：**

| 函式 | 用途 | 被誰呼叫 |
|---|---|---|
| `get_scheduled(db) -> list[Order]` | `select(Order).where(status='scheduled', is_deleted=False).order_by(scheduled_production_date asc)` | `services.order.list_scheduled_orders` |
| `clear_scheduled_dates(db) -> int` | bulk `UPDATE` 把所有 scheduled 訂單的兩個日期欄清成 `None`，一次往返；回傳影響列數 | `services.order.apply_schedule` 第 1 步 |
| `set_schedule_dates(db, *, order_id, scheduled_production_date, expected_delivery_date) -> Order \| None` | 單筆 select → mutate 兩個日期 + status → `flush()` + `refresh()`；訂單不存在或軟刪除回 `None` | `services.order.apply_schedule` 對每筆排程結果呼叫一次 |

**`backend/app/services/order.py` 新增三個編排函式：**

| 函式 | 用途 | 被誰呼叫 |
|---|---|---|
| `list_scheduled_orders(db) -> list[ScheduleResultResponse]` | 包 `repo.get_scheduled` 並把 ORM 物件 `model_validate` 成 schema | `GET /api/v1/schedule/result` |
| `list_for_scheduler(db) -> list[SchedulingOrder]` | 把 `status='scheduled'` 訂單轉換成 `SchedulingOrder`（`deadline = requested_delivery_date`），供 `rebuild_state` 重建 state 用 | `POST /api/v1/schedule/rebuild` |
| `apply_schedule(db, scheduled: list[ScheduledResult]) -> int` | 編排 5 個步驟（清空 → 聚合 earliest/latest → 逐筆 set → 寫 audit_logs DB row + 發 audit stdout log → commit）；回傳被排程的訂單數 | `workers.scheduling.run_scheduling_task` |

`apply_schedule` 詳細流程：

1. `order_repo.clear_scheduled_dates(db)` 一次性清空所有 `status='scheduled'` 訂單的 `scheduled_production_date` 與 `expected_delivery_date`
2. 把同一張 order 跨多天的 `ScheduledResult` 折成 `(earliest, latest)`
3. 對每筆訂單呼叫 `order_repo.set_schedule_dates(db, ...)` 寫回兩個日期、把 `status` 改為 `scheduled`
4. 對每筆訂單**雙寫**稽核：
   - `audit_log_repo.create(db, ..., user_id=None, resource_type="order", resource_id=order_id, new_value={…})` 把一筆 row 寫進 `audit_logs` 資料表（與 commit 同 transaction），確保「這張訂單什麼時候被排到哪天」可以從 DB 直接查出（`docs/DEVELOPMENT_GUIDELINES.md §6` 對 user-visible mutation 的稽核要求 — 只寫 stdout 在 log shipper 掉資料時就找不到了）
   - `emit_audit_log(action="order.scheduled", actor_id=None, ...)` 額外發一筆 ECS-compliant stdout log 給 Kibana / Elastic
5. `db.commit()`（同時 flush DB 上面新增的 audit_log row）

**為什麼 `set_schedule_dates` 不能無條件覆寫 `status`**：`advance_day_task` 會把今天要做的訂單 status 標成 `in_production`；接下來任何 compound 被 accept、materializer 跑 `apply_schedule` 時，如果 `set_schedule_dates` 無條件把 status 寫回 `scheduled`，前端「正在做」就會突然變回「排隊中」 — 更嚴重的是 `mark_completed_outside_set` 只抓 `status='in_production'`，被 demote 後永遠收不到尾，訂單卡死在 `scheduled` 不會被歸檔成 `completed`。所以 `set_schedule_dates` 寫入前讀目前 status，是 `in_production` 就只更新排程欄位（dates / daily_breakdown / pin cols），status 不動。`apply_schedule` audit log 的 status 也從 hard-coded `"scheduled"` 改成讀 refreshed row 的真實狀態。

修在 repo 層而不是 service 層，是因為 `set_schedule_dates` 是寫 status 的唯一入口；加一道條件 cheap、語義清楚，比把判斷散在每個 caller 好。`mark_in_production` 仍然是 `advance_day_task` 唯一寫 `in_production` 的地方 — ownership 不交叉。

worker 只負責 session 生命週期：

```python
db: Session = SessionLocal()
try:
    order_service.apply_schedule(db, scheduled)
finally:
    db.close()
```

> **改動之前**：`api/v1/schedule.py::get_schedule_result` 直接 `select(Order)`、`workers/scheduling.py::_persist_schedule` 直接 `db.query(Order).update(...)`，兩段都繞過 service / repo 層，違反 RULES §3。新增上面五個函式後，`api` 與 `workers` 都統一走 service → repo，`_persist_schedule` 整段被刪除。

---

### 4.5 rebuild_state — 從頭重建 scheduler state

`rebuild_state(orders: list[SchedulingOrder], base_date: date) -> tuple[SchedulerState, list[SkippedOrder]]`（`backend/app/services/scheduling.py`）是純函式，沒有 IO，邏輯如下：

1. 呼叫 `SchedulerState.initial(base_date)`：兩棵樹全部初始化到「全空產能」（capacity=10000 每天、deadline=0），pq 清空。
2. 把傳入的 `orders` 依 `sort_key()`（deadline 早 → qty 大 → order\_number 字母）升冪排序。
3. 依排序後的順序逐一呼叫 `add_order(state, order)`：
   - `"success"` → 加入 pq + 更新兩棵樹，繼續下一筆。
   - `"capacity_exceeded"` / `"deadline_too_far"` → 把該訂單加進 `skipped` 清單（含 `order_id` / `order_number` / `reason`）並記 `logger.warning("schedule.rebuild.skip", ...)`；**不中斷整個重建**。
4. 回傳 `(new_state, skipped)`。

**為什麼回傳 `skipped` 而不是只 log？**

被 skip 的訂單通常代表「這筆訂單需要人工處理」（典型場景：訂單長期 stuck 在 `scheduled` 狀態而 deadline 已被 `base_date` 越過）。光記 log 沒辦法主動通知到原 requester，所以 `rebuild_state` 把結構化資訊回傳給呼叫方（`rebuild_schedule_task`），由 task 依 `created_by` 透過 WebSocket `notify_user` 推 `schedule.rebuild_skipped` 訊息給原 requester。

**為什麼要重排序而不直接照 DB 的原始順序？**

`SchedulerState.initial` 是全空的，`capacity_tree` 的 backward-fill 行為跟「一張白紙上逐一加訂單」完全一致。若訂單以 deadline 早的先加，每次 backward-fill 都是在「後面還有容量」的前提下進行，結果與從未有 state 損壞時正常累積的樹相同。若順序不對（例如先加後面的訂單），早期的格子不會被正確消耗，state 會跟現實脫節。

**`list_for_scheduler`（`backend/app/services/order.py`）**

這是 `rebuild_state` 的資料來源，把 DB 的 `Order` entity 轉成純演算法層的 `SchedulingOrder`，並同時回傳 `order_id → created_by` 的 map（給 endpoint 通知 skipped 訂單時用）。**注意它用的是 `get_scheduled_for_rebuild`，只回 `status='scheduled'`**：

```python
def list_for_scheduler(db) -> tuple[list[SchedulingOrder], dict[uuid.UUID, uuid.UUID]]:
    rows = order_repo.get_scheduled_for_rebuild(db)  # 不含 in_production
    orders = [
        SchedulingOrder(
            order_id=r.id,
            order_number=r.order_number,
            wafer_quantity=r.wafer_quantity,
            deadline=r.requested_delivery_date,
            pinned_production_date=(
                r.pinned_production_date if r.is_pinned and r.pinned_production_date else None
            ),
        )
        for r in rows
    ]
    creators = {r.id: r.created_by for r in rows}
    return orders, creators
```

**為什麼 `in_production` 訂單要被排除掉**：in_production 訂單**今天已經做了一部分** — DB 沒記「還剩多少」這個 in-memory 量。用原始 `wafer_quantity` 重排 = 把已經做過的 wafer 在線段樹裡再扣一次；加上 deadline 可能已被 `base_date` 越過、進不去 30 天 horizon → 列為 skipped → 下次 advance_day 觸發 `mark_completed_outside_set`、訂單不在 new state 的 pq 也不在 pinned_orders → 直接 completed。**生產進度蒸發成 completed**。

修法是讓 rebuild 完全不碰 in_production 訂單，保持它們現有 DB state 不動，等下次 `advance_day_task::mark_completed_outside_set` 自然處理。分界線：演算法只該追蹤未來（scheduled），今天的物理生產是 DB-owned reality，rebuild 沒辦法精確 reconstruct 它的剩餘量。Rebuild 是 incident recovery 工具、使用情境少，這個 trade-off 可接受。

> 邊界 case：rebuild 發生在 boundary 訂單的中間日（今天做一半、明天還要做一半）、且在物理生產還沒收尾時打 rebuild → 明天那半的演算法 tracking 會掉。這個情境罕見、且 rebuild 本來就是「破壞 state 後人工救援」的逃生口、不是日常流程。徹底解決需要把 in-production 剩餘量也存 DB，目前評估 cost / value 不划算。

> 為什麼 `get_scheduled_for_rebuild` 跟 `get_scheduled` 是兩支 function（不用一個 boolean param）：`get_scheduled` 給 `GET /schedule/result` 用，要回所有 user-visible 排程相關訂單（scheduled + in_production），讓前端 timeline UI 看得到「正在做」的部分。Rebuild 跟 endpoint 對 in_production 的需求剛好相反 — 拆成兩支命名比 boolean flag 清楚，呼叫方一看 function 名就知道意圖、不會誤用。

---

### 4.6 WebSocket transport — sync publisher、async subscriber、in-process registry

排程的 worker 是同步的 Celery task、WebSocket 連線住在 async FastAPI 進程，而且兩者通常是**不同的 OS 進程**（worker container vs API container）。沒辦法直接 `await socket.send_json(...)`，所以拆成三段：

```
Celery worker (sync)          Redis pub/sub          FastAPI process (async)
───────────────────────       ───────────────        ──────────────────────────
broadcast({...})  ──PUBLISH──▶ schedule:ws:events ──SUBSCRIBE──▶ event_consumer_loop
                                                                   │
                                                                   ▼
                                                          ConnectionManager
                                                          (per-process registry)
                                                                   │
                                                                   ▼
                                                            ws.send_json(...)
                                                          → connected client
```

三層的責任：

1. **Publisher（`backend/app/services/websocket.py`）** — 同步函式 `broadcast(message)` / `notify_user(user_id, message)`。每次呼叫就用 sync `redis.publish(EVENT_CHANNEL, json.dumps(envelope))`。Redis 故障時 catch 起來只 log，不影響 caller 的 transaction。worker / 任何 sync 程式碼都能直接呼叫，不必 `asyncio.run` 或 `loop.run_in_executor`。

2. **Redis channel（`schedule:ws:events`）** — 單一 pub/sub channel，envelope 自帶 `kind` 欄位（`"broadcast"` 或 `"notify_user"`）做 dispatch。pub/sub 是 fire-and-forget 沒有 ack 也不重送 — best-effort 對 schedule 通知夠用，要嚴格一致時請改用 Streams。

3. **Subscriber（`backend/app/api/v1/websocket.py::event_consumer_loop`）** — async task，在 `app/main.py` 的 lifespan 起來：`AsyncRedis.from_url(...).pubsub().subscribe(...)`，`async for raw in pubsub.listen()` 拿訊息、`_handle_event` 解 envelope、依 `kind` 呼叫 `ConnectionManager.broadcast(payload)` 或 `send_to_user(user_id, payload)`。連不到 Redis、解析錯都只 log warning 不會把 loop 弄掛。lifespan shutdown 會 `task.cancel()`。

4. **`ConnectionManager`** — `dict[uuid.UUID, set[WebSocket]]` + `asyncio.Lock`。WebSocket endpoint 在驗 token 完 `accept()` 後 `manager.connect(user_id, ws)`，`receive_text()` block 住直到 client 斷線；`finally` 區塊 `manager.disconnect(...)` 確保一定會清掉註冊表。一個 user 多個 socket 是支援的（同個 user 開好幾個 tab）。

5. **WebSocket endpoint（`/api/v1/ws`）** — 解析 `?token=` 走既有的 `decode_access_token`，token 無效就 `close(code=4401)` 直接結束。此 close code 是 RFC6455 application-defined 區段（4000–4999），對應 HTTP 401 語義。

**多 worker 部署**：uvicorn 起 N 個 worker process 時每個 process 都會跑自己的 lifespan、各自 subscribe Redis channel、各自有 `ConnectionManager`。一筆訊息 publish 後會被 N 個 process 都收到，但每個 process 只會送給自己手上連線的 socket — Redis 自然 fan-out，不需要 sticky session。

---

## 5. 開發 / 除錯

### 5.1 手動觸發

```python
from app.workers.scheduling import run_scheduling_task, advance_day_task

run_scheduling_task.delay()   # 跟 CRUD 推 op 後做的事一樣
advance_day_task.delay()      # 手動跑換天，正常情況交給 Beat
```

或 HTTP（需要 scheduler 權限的 token）：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://backend/api/v1/schedule/trigger
curl -H "Authorization: Bearer $TOKEN" http://backend/api/v1/schedule/status

# Redis state 跟 DB 脫節時重建（migration / 故障復原）
curl -X POST -H "Authorization: Bearer $TOKEN" http://backend/api/v1/schedule/rebuild
```

### 5.2 觀察 Redis 狀態

```bash
$ uv run python -c "from redis import Redis; from app.core.config import get_settings; \
    r = Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True); \
    print(r.get('schedule:status')); print('queue len:', r.zcard('schedule:pending_ops'))"
```

### 5.3 偷看 WebSocket 流量

直接訂閱 pub/sub channel 看 worker 真的有沒有送訊息：

```bash
$ uv run python -c "from redis import Redis; from app.core.config import get_settings; \
    r = Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True); \
    ps = r.pubsub(); ps.subscribe('schedule:ws:events'); \
    [print(m) for m in ps.listen()]"
```

或用任何 WebSocket 客戶端（瀏覽器 DevTools / `websocat` / Postman）連 `ws://localhost:8000/api/v1/ws?token=$TOKEN`，然後 `POST /api/v1/schedule/trigger` 看 `{"type": "schedule.updated"}` 有沒有回來。

---

## 6. 測試

### 6.1 測試檔

| 檔案 | 範圍 | 依賴 |
|---|---|---|
| `backend/tests/services/test_scheduling.py` | 純演算法 | 無 — 不需 Postgres、Redis、Celery |
| `backend/tests/services/test_websocket.py` | WS publisher | 無 — `MagicMock` 假 Redis 客戶端 |
| `backend/tests/services/test_order.py` | `apply_schedule` 稽核 DB 寫入 | `db_session` fixture（真實 Postgres via testcontainers） |
| `backend/tests/workers/test_scheduling_task.py` | Celery task body | 全 mock — `_FakeRedis` + `monkeypatch` |
| `backend/tests/api/test_schedule.py` | HTTP layer | `client` fixture（真實 Postgres via testcontainers）+ mock Redis / Celery |
| `backend/tests/api/test_websocket.py` | WS endpoint + manager | `client` fixture + `pytest-asyncio` 跑 `ConnectionManager` async 單元測 |

### 6.2 各檔案測試案例

測試的設計哲學是：**每一個 case 保護一個具體的不變量（invariant）或邊界行為**，測試案例失敗時，要能直接對應到「哪段業務規則被破壞了」，而不只是「某個 assertion 跑錯」。以下說明每個測試「在測什麼」以及「為什麼這樣設計」。

---

#### `backend/tests/services/test_scheduling.py` — 純演算法（22 個）

這組測試完全沒有 DB、Redis、FastAPI，只對 `app/services/scheduling.py` 裡的函式呼叫。這樣設計的原因是：演算法是整個模組最難推理的部分，若它本身有 bug 而且只能透過整合測試才能發現，除錯成本會很高。把純邏輯隔離出來、毫秒就能跑完，讓每次修演算法都能快速得到回饋。

---

**日期轉換**

- **`test_abs_to_rel_and_rel_to_abs_roundtrip`**

  **在測什麼**：`abs_to_rel` 與 `rel_to_abs` 這兩個函式是日期與線段樹 index 之間的橋樑，所有跟樹有關的操作都要先過這道轉換。如果轉換有差一天的 off-by-one，整個排程的 deadline 都會算錯。

  **為什麼這樣設計**：對 delta=0..29 全部跑完（而不只是幾個點）是因為 off-by-one bug 的本質就是「靠近邊界才出現」，把整個 30 天 window 每個 index 都驗一遍，等同窮舉。先驗 `abs_to_rel → rel`，再從 rel 反推回日期，確認兩個方向都無損，沒有任何捨入或偏移。

- **`test_abs_to_rel_outside_horizon_returns_none`**

  **在測什麼**：`add_order` 跟 `remove_order` 都依賴 `abs_to_rel` 回 `None` 來判斷「deadline 超出 30 天 horizon，不可排」。如果這個邊界判斷失效（例如多算了一天），超期訂單就會被靜默地排進去，線段樹的 index 會越界造成不可預期的計算錯誤。

  **為什麼要驗三個 case**：`base-1`（昨天，過期）、`base+30`（第 31 天，剛好超界）、`base+29`（第 30 天，最後一個合法日）三個點釘住「開閉區間的哪端」。光驗一個點猜不出到底是 `<` 還是 `<=` 的問題在哪裡。

---

**`add_order`**

- **`test_add_order_success_updates_both_trees`**

  **在測什麼**：`add_order` 是整個排程的核心寫入操作，它必須同時更新兩棵線段樹（`capacity_tree` 和 `deadline_tree`）以及 `priority_queue`。任何一棵樹沒更新，後續的 feasibility check 或 `compute_schedule` 就會用到過時的數據。

  **初始狀態（fresh state）**：
  - `capacity_tree`：每天剩餘產能 10,000，所有前綴和均為 `d × 10,000`，即 `query(1)=10,000`、`query(2)=20,000`、`query(3)=30,000`、…、`query(30)=300,000`
  - `deadline_tree`：沒有任何訂單，`query(d)=0` for all d
  - `priority_queue`：空

  **操作**：呼叫 `add_order(state, order)` 加入一筆 qty=2,000、deadline=base+2（rel=3）的訂單。

  **backward-fill 推導**（`_apply_add_to_trees` 內部計算）：
  - b = `capacity_tree.query(3)` = 30,000（rel=3 範圍內的當前總剩餘量）
  - x = 2,000
  - target_prefix = b − x = 28,000（加了這筆訂單後，rel=3 的 prefix sum 應該變成多少）
  - 從 index 1 往右找第一個 `query(p) >= 28,000`：query(1)=10,000 ✗、query(2)=20,000 ✗、query(3)=30,000 ✓ → p=3
  - upper+1=4 > rel=3，不需要 `range_set`（沒有中間的天要清空）
  - `point_update(3, 28,000 − 30,000)` = `point_update(3, −2,000)` → 第 3 天從 10,000 降為 8,000

  **操作後的狀態**：
  - `capacity_tree`：day1=10,000、day2=10,000、day3=**8,000**、day4..30=10,000
    - `query(3)` = 10,000+10,000+8,000 = **28,000** = 3×10,000 − 2,000 ✓
    - `query(30)` = 28,000 + 27×10,000 = **298,000** = 30×10,000 − 2,000 ✓
  - `deadline_tree`：day3 += 2,000 → `query(3)` = **2,000** ✓
  - `priority_queue`：含該訂單

  **為什麼同時驗 `query(3)` 和 `query(30)`**：`capacity_tree` 儲存的是前綴和，任何包含 day3 的查詢都應該少 2,000。只驗 `query(3)` 不夠，因為理論上 bug 可能出現在「day3 改了，但後面的 prefix 沒有跟著更新」的情況。驗 `query(30)` 確認前綴和的級聯更新沒有斷掉。

- **`test_add_order_capacity_exceeded`**

  **在測什麼**：當訂單的 `wafer_quantity` 超過 deadline 以內的剩餘總產能時，`add_order` 必須拒絕這筆訂單且**完全不修改任何 state**。這個「失敗不留副作用」是正確性的關鍵：如果 add 失敗但樹已經被部分修改，之後的 remove 就沒辦法還原，整個 state 會損壞。

  **為什麼驗樹沒被動**：在 `result.status == "capacity_exceeded"` 之後，再驗 `capacity_tree.query(1) == 10000` 和 `deadline_tree.query(1) == 0`，確認「失敗就是完全沒動」這個原子性保證。只看 status 不看樹，沒辦法發現「樹改了一半但最後 rollback 不正確」的 bug。

- **`test_add_order_deadline_too_far`**

  **在測什麼**：`deadline_too_far` 是另一個拒絕路徑，但原因不同 — 不是「有空間但 deadline 不夠早」，而是「deadline 本身就超出 30 天 horizon，根本不能排」。這兩個錯誤碼對呼叫方有不同語義（前者可以縮短訂單或等產能釋放，後者客戶的交期要求本身就無法滿足），所以要分開測試確認 status 字串沒被搞混。

---

**`remove_order`**

- **`test_remove_order_restores_capacity_after_single_add`**

  **在測什麼**：`remove_order` 是 `add_order` 的完整逆操作，add 之後再 remove，state 必須完全恢復成 fresh state。這個「round-trip 等冪性」是系統正確性的基礎 — 訂單修改（update = remove 舊值 + add 新值）的正確性完全依賴 remove 能精確還原 add 造成的影響。

  **操作一：`add_order(state, order)`，qty=15,000，deadline=base+2（rel=3）**

  backward-fill 推導（跨兩天）：
  - b = `capacity_tree.query(3)` = 30,000
  - x = 15,000；target = 30,000 − 15,000 = 15,000
  - 找 p：query(1)=10,000 < 15,000 ✗、query(2)=20,000 ≥ 15,000 ✓ → **p=2**
  - p+1=3 ≤ rel=3：需要 `range_set(3, 3, 0)` → day3 點值清空
  - `point_update(2, 15,000 − 20,000)` = `point_update(2, −5,000)` → day2：10,000 → 5,000

  **add 後的狀態**：
  - `capacity_tree`：day1=10,000、day2=**5,000**、day3=**0**、day4..30=10,000
    - `query(2)` = 10,000+5,000 = 15,000；`query(3)` = 15,000+0 = 15,000
  - `deadline_tree`：day3 += 15,000 → `query(3)` = 15,000
  - `priority_queue`：含該訂單

  **操作二：`remove_order(state, order)`**

  remove 是 add 的逆過程：找出 add 時「壓扁」的那些格子，把值還回去。
  - 在 deadline 範圍內找到第一個「未被完全填滿」的天：day2 點值=5,000（< 10,000）→ **tight=day2**
  - 往 deadline 方向走，把清空的格子（day3=0）補回 10,000，把 tight day 加回差額（10,000−5,000=5,000）
  - `deadline_tree` 在 day3 減 15,000

  **remove 後的狀態（= fresh state）**：
  - `capacity_tree`：day1=10,000、day2=**10,000**、day3=**10,000**、day4..30=10,000
    - 對所有 d：`query(d)` = d × 10,000 ✓
  - `deadline_tree`：所有 `query(d)` = 0 ✓
  - `priority_queue`：空 ✓

  **為什麼要對 1..30 全部驗**：如果 remove 的 restore 邏輯只在某幾天正確還原、其他天有殘留，只驗少數幾個 index 可能正好跳過問題。全部 30 天的 prefix sum 都驗，確保沒有任何「漏掉還原」的格子。qty=15,000 故意用跨兩天（10,000+5,000）的量，讓 backward-fill 真的走到不同天的邏輯，force restore 路徑也要同時處理「清空的 day3」和「部分佔用的 day2」。

- **`test_remove_order_leaves_other_orders_intact`**

  **在測什麼**：remove 只應該還原**被移除的那筆**訂單所佔用的產能，其他訂單的格子不能被動到。這個「局部性」保證在多訂單環境中至關重要；如果 remove 算法對「哪些格子是這筆訂單佔的」判斷不精確，就可能偷還了不該還的容量，讓後來的 add 超收而實際上已超產。

  **加入 a、b、c 之後的狀態**（三筆都是 qty=2,000、deadline=base，rel=1）：

  各筆的 backward-fill（都落在 day1）：
  - 加 a：b=10,000，target=8,000，p=1；`point_update(1, 8,000−10,000)` → day1：10,000→8,000
  - 加 b：b=8,000，target=6,000，p=1；`point_update(1, 6,000−8,000)` → day1：8,000→6,000
  - 加 c：b=6,000，target=4,000，p=1；`point_update(1, 4,000−6,000)` → day1：6,000→4,000

  三筆加完後：
  - `capacity_tree`：day1=**4,000**、day2..30=10,000；`query(1)` = 4,000 ✓
  - `deadline_tree`：day1=**6,000**；`query(1)` = 6,000 ✓

  **`remove_order(state, b)` 之後**：

  remove b（qty=2,000、rel=1）：tight=day1（唯一的非滿天），還回 2,000：day1：4,000→6,000；dl_day1：6,000→4,000

  - `capacity_tree`：day1=**6,000**；`query(1)` = 6,000 ✓（只還了 b 的 2,000）
  - `deadline_tree`：day1=**4,000**；`query(1)` = 4,000 ✓（a+c 的義務還在）
  - `priority_queue`：只剩 a 和 c

  **為什麼要先加三筆再移中間那筆**：a、b、c 同 deadline、同 qty，讓它們在同一個 deadline 格子裡競爭。同一個格子才能暴露「remove 有沒有多還」的問題：如果 remove b 誤還了 4,000（全部），query(1) 就會變 8,000；如果誤還了 0，query(1) 就還是 4,000。精確驗 6,000，確認只還了 b 的份。

- **`test_remove_order_restores_when_later_add_overlaps_earlier_one`**

  **regression test，守的 bug 是「逐天 give-back 時 slack 沒重算」**。場景如下：

  - Step 1：add `first`（qty=10,000、deadline=base+1，rel=2）→ backward-fill 全部塞進 day 2 → cap day values `[10000, 0, 10000, ...]`、cap prefix `[10000, 10000, 20000, 30000, ...]`
  - Step 2：add `second`（qty=15,000、deadline=base+2，rel=3）→ backward-fill 把 day 2、day 3 都歸零再用 point_update 把 day 1 扣 5,000 → cap day values `[5000, 0, 0, 10000, ...]`、cap prefix `[5000, 5000, 5000, 15000, 25000, ...]`
  - Step 3：remove `second`。**正確結果**是樹回到 step 1 的狀態（day 2 還是 0，因為它本來就被 `first` 的 deadline 義務佔住）：cap day values `[10000, 0, 10000, ...]`、cap prefix `[10000, 10000, 20000, 30000, ...]`

  **這個 bug 長什麼樣**：如果 remove 在 give-back 迴圈外把 prefix slack 一次算完然後逐天扣（而不是每次 `query(d)` 重算），會在 day 1 補了 5,000 之後，day 2 還誤以為自己有 5,000 slack 可以補，導致最後變成 `[10000, 5000, 5000, 10000, ...]`（cap prefix `[10000, 15000, 20000, 30000, ...]`）— day 2 多出來那 5,000 其實是 `first` 的 deadline 義務，不該被還給 `second`。

  **為什麼這個 case 必要**：既有的 `test_remove_order_restores_capacity_after_single_add` 只有單一筆 add 後 remove，沒辦法驗「多筆訂單有交疊時 remove 的局部性」。這個 case 把兩筆訂單故意設成 deadline 不同、後加的 backward-fill 會覆蓋前面格子，是現有邏輯最容易出錯的形狀；同時也驗 `deadline_tree` 只剩 `first` 的 10,000 義務（query(2..30) 都是 10,000）。

---

**`compute_schedule`**

- **`test_compute_schedule_splits_orders_across_days`**

  **在測什麼**：`compute_schedule` 是從 pq 推導「前端每天要做多少」的 forward-fill，它的輸出直接決定前端看到的 `daily_breakdown`。這個測試驗的是跨天切分的正確性：一筆訂單的 wafer 量超過單天產能時，應該把「第一天填滿，剩的填到下一天」，並且按照 pq 的 priority order 讓高優先訂單先用早期的格子。

  **為什麼這個測試要驗三筆訂單的互動**：如果只驗一筆，無法確認「第一筆訂單占走的容量有沒有正確反映在第二筆的起始點」。a(qty=15000, deadline=base+1) 先跑，佔走 day1 的 10000 和 day2 的 5000；b(qty=8000, deadline=base+2) 接著跑，day2 只剩 5000，b 要延到 day3 拿 3000；c 最後拿 day3 的 2000。這個三筆的互動把「產能不夠時自動延到後一天」跟「前一筆訂單的剩餘容量正確傳遞給後一筆」兩個行為一起驗了。

---

**`advance_day`**

- **`test_advance_day_processes_pq_and_shifts_trees`**

  **在測什麼**：`advance_day` 是整個模組中邏輯最複雜的函式，涉及「確定完成的訂單從 pq 移除並從兩棵樹 remove」、「跨天訂單的剩餘量用新 qty 重新 add_order」、「兩棵樹整體左移一格 + 最後一格補滿產能」、「base_date +1 day」四個步驟，任何一步做錯都會讓後續所有排程都跑在錯誤的基礎上。

  **完整的 abc/de/fg 例子**

  訂單清單（qty、deadline、rel）：
  - a: qty=2,000、deadline=base（rel=1）
  - b: qty=2,000、deadline=base（rel=1）
  - c: qty=2,000、deadline=base（rel=1）
  - d: qty=1,000、deadline=base+1（rel=2）
  - e: qty=2,000、deadline=base+1（rel=2）
  - f: qty=2,000、deadline=base+2（rel=3）
  - g: qty=2,000、deadline=base+2（rel=3）

  **PQ 排序（sort key = deadline asc → −qty asc → order_number asc）**：
  - a, b, c 同 deadline=base、同 qty=2,000 → 按字母 → a, b, c
  - e vs d：同 deadline=base+1，e qty=2,000 > d qty=1,000 → e 優先
  - f, g 同 deadline=base+2、同 qty=2,000 → 按字母 → f, g
  - **最終順序：a → b → c → e → d → f → g**

  **七筆全加完後的樹狀態**（backward-fill 累計）：
  - a,b,c 各 2,000 全落在 day1：day1 點值 10,000−6,000=4,000
  - e(2,000)、d(1,000) 全落在 day2：day2 點值 10,000−3,000=7,000
  - f(2,000)、g(2,000) 全落在 day3：day3 點值 10,000−4,000=6,000
  - `capacity_tree.query(1)` = **4,000** ✓
  - `capacity_tree.query(2)` = 4,000+7,000 = **11,000** ✓
  - `capacity_tree.query(3)` = 11,000+6,000 = **17,000** ✓
  - `deadline_tree`：dl_day1=6,000、dl_day2=3,000、dl_day3=4,000

  **`advance_day(state)` 內部步驟**

  *步驟 1 — 依 PQ 順序累加，找出邊界*：
  | 訂單 | qty | 累計 | ≤ 10,000？ |
  |---|---|---|---|
  | a | 2,000 | 2,000 | ✓ 完整完成 |
  | b | 2,000 | 4,000 | ✓ 完整完成 |
  | c | 2,000 | 6,000 | ✓ 完整完成 |
  | e | 2,000 | 8,000 | ✓ 完整完成 |
  | d | 1,000 | 9,000 | ✓ 完整完成 |
  | f | 2,000 | **11,000** | ✗ 跨天邊界！昨天完成量=1,000，剩餘=1,000 |
  | g | — | — | 未到達，原封不動 |

  *步驟 2 — 從兩棵樹 remove 已完成的訂單，並修改邊界訂單 f*：
  - remove a,b,c：day1 點值 4,000→10,000；dl_day1 6,000→0
  - remove e,d：day2 點值 7,000→10,000；dl_day2 3,000→0
  - remove 舊 f（qty=2,000）：day3 點值 6,000→8,000；dl_day3 4,000→2,000
  - add 新 f（qty=1,000，同 rel=3）：backward-fill → target=query(3)−1,000=27,000；p=3；point_update(3,−1,000) → day3 8,000→7,000；dl_day3 2,000→3,000

  *移除前的最終樹點值*：cap: day1=10,000, day2=10,000, day3=7,000, day4..30=10,000；dl: day1=0, day2=0, day3=3,000, day4..30=0

  *步驟 3 — 樹整體左移（去掉 day1，days 2..30 遞補為 1..29，day30 補滿）*：
  - 新的前綴和 = 舊前綴和(k+1) − 舊 day1 點值（cap: −10,000；dl: −0）
  - `new_capacity_tree.query(1)` = old_query(2)−10,000 = 20,000−10,000 = **10,000** ✓
  - `new_capacity_tree.query(2)` = old_query(3)−10,000 = 27,000−10,000 = **17,000** ✓
  - `new_deadline_tree.query(1)` = old_dl_query(2)−0 = 0 ✓（de 都完成了）
  - `new_deadline_tree.query(2)` = old_dl_query(3)−0 = **3,000** ✓（f′ 1,000 + g 2,000）

  **advance_day 後的 PQ**：
  - 存活的訂單：f（qty 改為 1,000）和 g（qty=2,000 不變）
  - **f 在 index 0，g 在 index 1**：advance_day 不重新排序，保留原本相對位置（f 在 g 前面是因為加入時兩者 qty 相同且 "f" < "g"，f_new qty 雖然變小但不觸發重排）

  **為什麼這個例子能完整覆蓋三種情況**：(1) 完整完成的訂單（a,b,c,e,d）有沒有被正確移除，(2) 邊界訂單（f）的 remove 舊量＋add 新量有沒有正確，(3) 未到達的訂單（g）有沒有保持不動。驗原 state 沒被 mutate（`state.base_date == _BASE`、`len(state.priority_queue) == 7`）確認函式是 pure function — 不能修改傳入的 state。

---

**`rebuild_state`**

- **`test_rebuild_state_empty_orders_returns_empty_state`**

  **在測什麼**：`rebuild_state` 的語義是「從零開始、以給定訂單清單為輸入、重建一個等價的 state」。空的訂單清單輸入時，結果必須完全等同 `SchedulerState.initial(base_date)`。這個 case 驗的是「`rebuild_state` 開頭有沒有正確初始化」，避免萬一 initial 邏輯有問題（例如殘留上次 state 的資料），而不是靠 rebuild 邏輯去補救。

- **`test_rebuild_state_single_order_matches_fresh_add`**

  **在測什麼**：`rebuild_state` 的核心正確性保證是「對同一份訂單清單，rebuild 得到的 state 跟從頭一筆一筆 add_order 得到的 state 完全相同」。如果這個等價性不成立，那麼 rebuild 後的 state 跟原本 worker 累積出來的 state 就會有差異，排程結果也會不一樣。

  **為什麼要先建一個 fresh reference state 做比對**：直接比兩棵樹的 prefix sum（而不只是比 pq），是因為 pq 只有 order 的 metadata，樹才是決定「產能格子怎麼被消耗」的數據。兩棵樹在所有 30 個 index 都對，才能確信 backward-fill 的行為完全一致。

- **`test_rebuild_state_multiple_orders_adds_in_priority_order`**

  **在測什麼**：`rebuild_state` 收到的 orders 清單順序是 DB 查詢的結果，不一定已經按照 priority 排好。`rebuild_state` 內部必須先對 orders 按 `sort_key()` 排序才 add，否則 backward-fill 的分配結果跟原本 worker 依 priority 逐一 add 的結果就會不同。

  **為什麼故意把 a 放在 b 前面傳入**：這是在驗「函式有沒有自己做排序」，而不是依賴呼叫方準備好順序。如果 rebuild_state 直接照傳入順序 add，這個測試就會失敗（因為 a deadline 更晚、先 add a 的 backward-fill 跟先 add b 的結果不同）。

- **`test_rebuild_state_skips_orders_past_horizon`**

  **在測什麼**：DB 裡可能有訂單的 `requested_delivery_date` 落在 30 天 horizon 之外。`rebuild_state` 對這些無法排入的訂單必須 skip，不能 raise exception 中斷整個重建流程；同時，被 skip 的訂單必須以結構化資訊回傳給呼叫方（包含 `order_id`、`order_number`、`reason`），這樣呼叫方（API endpoint）才能逐一通知原 requester。

  **為什麼這個情況會在「正常運作」下出現**：產品邏輯保證未排入的訂單不會被標 `scheduled`（而是會被取消或留在 `pending`），所以 capacity_exceeded 這條路徑在 rebuild 時理論上不應該出現。但 `deadline_too_far` 不一樣 — 它**會**在正常運作下發生，原因是：
  1. **`base_date` 會隨時間推進，但訂單的 `requested_delivery_date` 是絕對日期、不會跟著動**。一筆長期 stuck 在 `scheduled` 狀態的訂單（出貨延誤、漏跑等），deadline 會被 `base_date` 越甩越遠。當 `base_date` 已經超過該訂單的 deadline，`abs_to_rel` 回 `None`、`add_order` 回 `deadline_too_far`。
  2. **migration / disaster recovery**：從備份或舊 DB 撈訂單時，那些訂單在原系統可能還在 horizon 內，但匯入時新系統的 `base_date` 跟原系統不同，相對位置就跑出 horizon 了。

  **驗的具體欄位**：除了驗 `outside.order_id not in pq_ids`（被 skip 的訂單真的沒進 PQ）之外，還驗 `skipped[0].order_id == outside.order_id`、`skipped[0].order_number == "outside"`、`skipped[0].reason == "deadline_too_far"`，確認結構化資訊完整。endpoint 才能依此打 WebSocket 通知。

  **為什麼不需要 `skips_orders_exceeding_capacity` 對應的測試**：產品邏輯規定「訂單只有在能被排進產能時才會被標 SCHEDULED，否則被取消」。所以 DB 裡 `status=scheduled` 的訂單，在 rebuild 時理論上一定能塞回去。capacity_exceeded 這條 skip 路徑是 `add_order` 自己的契約（被 `test_add_order_capacity_exceeded` 蓋住了），不需要 rebuild 層再額外驗一次。`rebuild_state` 對這條路徑仍然有 fallback（萬一資料異常），但不在 unit test 矩陣裡刻意製造。

---

#### `backend/tests/workers/test_scheduling_task.py` — Celery task body

這組測試在驗「Celery task 的編排邏輯」，也就是 `run_scheduling_task`、`advance_day_task`、`rebuild_schedule_task`、`materialize_schedule_task` 這幾個函式的「主體行為」，而不是演算法本身（演算法已在 services 那組測了）。

> **註**：`run_scheduling_task` 的測試案例在 batch admission 改寫之後被重新組織，下面個別 case 的描述可能跟舊版本（per-op saga rollback 設計）對不上。實際內容請以 source 為準；保留下面舊描述供「過去設計動機」參考。新行為的概要：drain pending 一次到底、binary search 找最大可行 prefix、整 batch 一次 tree update、第一筆 compound 自己塞不下就 reject + WS notify。

**測試基礎建設的設計動機**：

Worker 有三個外部依賴 — Redis、DB、純演算法函式。如果跑整合測試（帶著真實 Redis 和 Postgres），每個 case 就需要幾秒甚至幾十秒，而且 race condition 和 timing 問題也會讓測試變得不穩定。因此採取「全部 mock、只讓 task body 的編排邏輯跑真實程式碼」的策略：
- **`_FakeRedis`**：純 Python dict 實作 `get / set / incr` 跟 sorted-set 的 `zadd / zpopmin / zcard`，不需要真實 Redis 連線，也讓測試能精確控制「佇列裡有什麼」。配合的 `_enqueue(fake_redis, op)` helper 模仿 producer 端的「INCR seq → 計算 score → ZADD」流程，把 op JSON 推進 sorted set，這樣 worker 的 `_pop_next_op` 才能用同樣的 score 排序拿出最高優先序的 op。
- **`monkeypatch` 換掉所有副作用**：`add_order / remove_order / compute_schedule / apply_schedule / broadcast / notify_user` 全部換成 `MagicMock`，這樣 task 的行為完全由測試控制，不會被演算法自身的邏輯干擾（演算法邏輯在 services 測試那組已經驗過）。
- **`task.apply()` 同步跑**：讓測試不需要真正的 Celery broker，而且能在同一個 Python thread 裡同步等到 task 跑完，直接斷言結果。
- **`_install_auto_retrigger_delay(monkeypatch)` helper**：per-op 設計下每次 `apply()` 只處理一筆 op，要驗多筆 op 的流程（順序、count）需要 task 結尾的 `.delay()` 真的把下一輪 task 跑起來。這個 helper 把 `run_scheduling_task.delay()` 換成 side_effect = `apply()`，讓 `.delay()` 直接同步遞迴回 `apply()`，整條 queue 在一次 test-driven `apply()` 裡跑完。內建 50 層深度 cap 抓無限迴圈 bug（例如 fake_compute 每次注入新 op 而沒有停止條件）。

---

- **`test_run_scheduling_processes_two_adds`**

  **在測什麼**：最基本的正向路徑 — 有兩筆 op 在佇列，per-op 設計下要兩次 task invocation 才能處理完。auto-retrigger helper 讓 `apply()` 一次跑完整個 queue。最後驗 `add_order` 跑了 2 次、`apply_schedule` 跟 `broadcast` 各 2 次（per-op refresh signal）、`delay()` 在第一筆與第二筆中間被呼叫一次、queue 空後沒再 retrigger、最終 status 是 idle。

  **為什麼斷言 `apply_schedule` / `broadcast` 都是 2 次**：這是 per-op 設計的核心可觀察性 — 每筆 op 處理完都會 finalize 一次。如果 worker 改回「一次 drain 整批 ops 才 finalize」的舊模式，這個斷言會 fail。把它釘住，重構時誤改回去會立刻被抓到。

  **`delay.call_count == 1`**：第一筆 op 處理完看到 queue 還有第二筆，所以呼叫一次 delay；第二筆處理完 queue 空，不再 delay。如果是 0 表示 retrigger 機制壞了；如果 > 1 表示 queue 空了還在 retrigger（無限迴圈風險）。

- **`test_run_scheduling_notifies_user_on_capacity_exceeded`**

  **在測什麼**：當某筆訂單 `add_order` 失敗時，task 必須做兩件事：（1）透過 WebSocket `notify_user` 通知那筆訂單的 `requested_by` 使用者；（2）**繼續處理佇列裡的下一筆**，不能因為一筆失敗就中斷整個 task。

  **為什麼要驗 `add_order.call_count == 2`**：如果 task 在遇到失敗時直接 raise 或 break，第二筆 ORD-OK 就不會被處理。這個數字確認兩筆都走過了 `_process_one`，也確認失敗不是「靜默吃掉」而是真的有呼叫 notify_user。

- **`test_run_scheduling_notifies_user_on_remove_failure`**（PR-review 補強）

  **在測什麼**：`remove_order` 失敗時 — 典型場景是訂單已不在 pq、deadline 落出 horizon、或 CRUD 端送了一筆對舊狀態才有意義的 remove op — task 必須對 `requested_by` 推 `schedule.remove_failed`，envelope 結構與 `schedule.add_failed` 對稱（`type / order_id / order_number / reason / detail`）。

  **為什麼要補這條**：原本 add 失敗會 notify、remove 失敗只 logger.warning，行為不對稱。前端如果按 `type` 開 switch，就會發現 add 路徑有訊號、remove 路徑悄悄消失；維運看 log 才能知道使用者的 remove op 沒生效。補這條測試把「兩條失敗路徑都會主動通知」的契約鎖死，重構時誤刪掉 remove 分支的 notify 會立刻被抓。

  **手法**：mock `remove_order` 回 `ScheduleResult(status="deadline_too_far", ...)`，跑 `apply()` 後驗 `notify_mock.call_count == 1`、`message["type"] == "schedule.remove_failed"`、`reason == "deadline_too_far"`、`user_id == failing_user`。`status` 用真的列在 `Literal[...]` 裡的值，避免 Pydantic validation 自己擋掉測試假資料。

- **`test_run_scheduling_writes_status_failed_on_exception_and_reraises`**（review 第二輪補強）

  **在測什麼**：`run_scheduling_task` body 任何步驟 raise（典型：segment tree 邏輯壞掉、`compute_schedule` 出錯）必須做三件事：(1) `schedule:status.state` 寫成 `"failed"` 並把 `str(exc)` 放進 `error` 欄位；(2) status 不能卡在 `"running"`（會讓所有 `/trigger` 永遠 409）；(3) re-raise 讓 Celery result backend 收到 traceback。

  **為什麼這條測試是必要的**：這條 except 路徑沒被覆蓋過，重構時把 try/except 拿掉不會有任何測試紅燈，但實際後果是：worker 跑炸 → status 卡 running → `/trigger` 永遠回 409 → 唯一解套是 ops 進 Redis 手動 `SET schedule:status '{"state":"idle"}'`。鎖死這條 invariant 之後，未來無論誰重構這段都會被 CI 擋住。

  **為什麼也要驗「沒打 retrigger」**：失敗的 task 不應該觸發下一輪 — 同樣的 input 會再炸一次，只會在 Celery 上留下一連串相同 traceback 的失敗任務。`assert not mocks["delay"].called` 就是把這條鎖住。

  **手法**：把 `add_order` mock 成 `side_effect=RuntimeError("segment tree corrupted")`，佇列裡放一筆 op，跑 `apply()` 後 `assert not result.successful()`、`"segment tree corrupted" in result.traceback`，再讀 fake_redis 的 `STATUS_KEY` 驗 JSON payload `state == "failed"` / `error == "segment tree corrupted"` / `state != "running"`。

- **`test_run_scheduling_processes_shrink_group_before_grow`**

  **在測什麼**：shrink 優先的業務規則（§4.3 解釋了為什麼這樣設計）。這個測試把「一個 defer 操作（remove+add，group=shrink）」跟「一個 advance 操作（remove+add，group=grow）」同時放進佇列，驗 shrink 的兩筆（remove+add）全跑完後，grow 的兩筆才開始。

  **為什麼用 `call_order` list 記呼叫順序而不是只看 call_count**：只看 count 沒辦法知道順序。把每次 add/remove 呼叫的 order_number 追加到 `call_order`，最後斷言整個 list 的順序，才能精確驗「shrink 兩筆 → grow 兩筆」這個 interleave 行為。

- **`test_run_scheduling_lets_late_shrink_jump_pending_grow`**

  **在測什麼**：`_pop_next_op` 的「每跑完一筆就重新 peek 整個佇列」語義。這個語義讓「task 執行到一半時才進來的 shrink op」能插到還沒跑的 grow op 前面，而不是等到下次 task invocation。

  **為什麼用 `side_effect` 在 `GROW-1` 被處理時 LPUSH `LATE-SHRINK`**：這是能在「task 內部執行過程中」精確注入一個新 op 的唯一方法，模擬了現實中「Order CRUD 在 task 跑到一半時觸發了一筆取消操作」的 race condition。如果用舊的「drain-all 再做完」邏輯，LATE-SHRINK 要等到下一個 task invocation 才會被看到，這個測試就會失敗（call_order 的順序會是 `GROW-1, GROW-2, LATE-SHRINK`）。

- **`test_run_scheduling_retriggers_when_more_ops_arrive`**

  **在測什麼**：task 結尾的「自我 re-trigger」邏輯 — 主迴圈把初始佇列跑完後，在 `compute_schedule / apply_schedule / save_state / broadcast` 這段期間若又有新 op 進來，task 結尾偵測到佇列不空就立刻再次 `.delay()` 自己，避免新 op 無限期等待下一個外部觸發。

  **為什麼用 `compute_schedule` 的 side_effect 注入新 op**：`compute_schedule` 在「pop 迴圈結束後、task 結尾 check 佇列前」被呼叫，是能精確模擬「主迴圈跑完但後處理還沒完成時就有新 op 進來」這個 timing window 的注入點。

- **`test_run_scheduling_skips_retrigger_when_queue_drained`**

  **在測什麼**：佇列本來就是空的時候，task 結尾必須不觸發 re-trigger。這是為了防止「空跑任務無限 re-trigger 自己」造成 Celery worker 被佔滿。這個 case 也順帶驗了「佇列空時 `add_order` 不會被呼叫`」，確認 pop 迴圈是「佇列空就立刻結束」而非「固定跑幾輪」。

- **`test_run_scheduling_yields_retrigger_to_waiter`**

  **在測什麼**：waiter-flag race fix 的核心契約 — 當 `schedule:waiter_pending` 是 `"1"` 時，即使 `zcard > 0`，`run_scheduling_task` 也**不能**呼 `.delay()` retrigger。retrigger 的責任這時候在 waiter 身上。

  **為什麼這條測試是必要的**：per-op 設計把 status 翻 idle 跟 retrigger 拆成兩步，中間開出一個窗口讓 waiter 觀察到 idle 而跳出 wait（這就是 per-op 想要的效果）。但同時也讓 run_task 的 retrigger 可能跟 waiter 並行，兩者搶寫 `schedule:state`。waiter-flag 是這條 race 的修法 — 這個 test 把「flag 設了就絕不 retrigger」這個 invariant 鎖死，重構時誤改回去會立刻被抓。

  **手法**：用 plain delay mock（不裝 auto-retrigger），佇列裡放兩筆 op、預先 SET flag、跑 `apply()` 一次。第一筆 op 會被處理（`add_order.call_count == 1`），第二筆留在佇列（`zcard == 1`），但 `delay.call_count == 0` — 證明就算還有 op 也讓位給 waiter。

- **`test_advance_day_sets_waiter_flag_then_clears_it`**

  **在測什麼**：`advance_day_task` 的 try/finally 結構正確 — flag 在 body 執行期間是設的（用 `observe_advance` 在 advance_day 被呼叫時讀 flag 確認是 `"1"`），task 結束之後 flag 被清掉（讀 Redis 確認 `None`）。

  **為什麼要驗中間時刻而不只驗最終狀態**：只驗 final state 沒辦法確認 flag 真的有撐過整個 body（萬一 set 之後立刻被 clear，run_task 那邊就讀不到 flag 了）。把 advance_day 換成 spy 在被呼叫的當下讀 Redis，可以精確確認「執行期間 flag 是 set 的」。

- **`test_advance_day_clears_waiter_flag_even_on_exception`** / **`test_rebuild_clears_waiter_flag_even_on_exception`**

  **在測什麼**：crash safety — 如果 waiter body 半路 raise（例如 advance_day 演算法 bug、rebuild 撈 DB 失敗），`finally` 仍要清掉 flag。否則 flag 卡住直到 10 分鐘 TTL 到期，這 10 分鐘內所有 `run_scheduling_task` 都會讓位給「已經死掉的 waiter」，pending ops 完全卡住。

  **手法**：把 `advance_day` / `list_for_scheduler` mock 成 raise `RuntimeError`，跑 `apply()` 後 `assert not result.successful()`（task 確實失敗），再驗 `fake_redis.get("schedule:waiter_pending") is None`（flag 已清）。如果有人重構時不小心把 try/finally 改成 try/except 並吃掉 exception 而忘了清 flag，這條會抓到。

- **`test_advance_day_claims_status_running_during_body_and_clears_to_idle`**（PR-review 補強）

  **在測什麼**：Status-claim race fix（§4.2）的核心契約 — `advance_day_task` 在 `_wait_for_idle_run` 結束後、`advance_day(state)` 開跑前必須先把 `schedule:status` 寫成 `running`，body 跑完後 inner finally 必須清回 `idle`（包含寫好 `finished_at` 時間戳）。

  **為什麼要驗中間時刻**：waiter flag 解掉了「run_task #1 retrigger 跟 waiter 同時跑」這條 race，但留下另一條 — waiter 自己在做事的期間 `schedule:status` 還是 `idle`，`POST /schedule/trigger` 看到 idle 就會打另一支 `run_scheduling_task`。修法是把 status 在 body 期間 claim 成 running，409 就會自然擋掉並行請求。光驗最後狀態 `idle` 不夠，因為一開始就是 idle、最後也是 idle，看不出來 body 中間有沒有真的 claim 過。所以用 `observe_advance` spy 在 `advance_day` 被呼叫的當下從 fake_redis 直接讀 `schedule:status`，斷言這個瞬間是 `"running"`。

  **手法**：`_get_status` mock 成永遠回 `idle`（避免 wait loop 多輪 polling 干擾），`_load_state` 餵一份固定 state，`advance_day` 換成 spy `observe_advance` — 它真的去讀 fake_redis 裡的 status JSON、把 `state` 欄位 append 到 list。task 跑完後驗 `status_during_body == ["running"]` 跟 `final["state"] == "idle" and final["finished_at"] is not None`。

- **`test_advance_day_writes_status_failed_on_exception`**（review 第二輪修法後重命名）

  **在測什麼**：inner except 把 status 寫成 **`failed`**（含 `error` 欄位、`finished_at`），不是 `idle`、不是 `running`。原本的契約是 raise 之後寫回 `idle`，但實務上等於把 advance_day / rebuild 的事故藏起來：`GET /schedule/status` 是 ops 唯一能看到「排程是否健康」的窗口，寫 idle 等於監控顯示綠燈但實際上換天根本沒成功。

  **為什麼這條跟 waiter-flag-on-exception 是不同的 invariant**：waiter flag 對應「不要 retrigger 撞到 waiter」；status 對應「ops 看得到失敗 + 不要在 waiter 做事時又開 trigger」。兩條保護寫在不同的 try 層、保護的 key 也不同（`schedule:waiter_pending` vs `schedule:status`），所以要各自有獨立的 crash-safety 測試。重構若把 status 寫回改回 `idle`，這條會立刻紅燈。

  **為什麼可以放心把 status 寫成 `failed` 不會卡死系統**：`/trigger` 的 409 判斷只看 `running`，看到 `failed` 一樣會 dispatch；下次任何成功的 task 都會把 status 蓋回 `idle`。所以「failed 永久卡住 /trigger」不是真的會發生的副作用，把錯誤暴露給 ops 才是更高優先序。

  **手法**：`advance_day` mock 成 `side_effect=RuntimeError("boom")`、`apply()` 確認 `not result.successful()`、讀 fake_redis 的 `schedule:status`，斷言 `state == "failed"`、`error == "boom"`、`finished_at is not None`。

- **`test_advance_day_waits_then_advances_finalizes_and_retriggers`**

  **在測什麼**：`advance_day_task` 的四個行為：（1）若有 `run_scheduling_task` 在跑中，先輪詢等它結束再繼續；（2）呼叫 `advance_day(state)` 產生新 state；（3）**自己呼叫 `_finalize_run(new_state)`**（compute / apply / save / broadcast 一次到位） — 這是 per-op 化之後的關鍵改動，不再依賴 `run_scheduling_task` 幫它 broadcast；（4）只有當 `pending_ops` 還有未處理的 op 時才 `run_scheduling_task.delay()`。

  **為什麼預先塞一筆 POST-ADVANCE op 到 fake_redis**：要驗第（4）個行為的「有 pending 才 retrigger」分支，最直接的方式就是讓 queue 不空。如果不預先塞，`zcard == 0` 路徑會被觸發，看不出來 retrigger 的條件邏輯有沒有走對。

  **為什麼 `compute_mock.assert_called_once_with(advanced)`**：`_finalize_run` 把 advance 後的 state 餵給 compute_schedule，這條斷言驗「自己 finalize」這個契約 — 從前是靠 `run_scheduling_task` 順便做的，現在 advance_day_task 必須自己做才能在 queue 為空時也廣播 schedule.updated。

  **為什麼要 mock `time.sleep` 跟 `time.monotonic`**：這個 case 的核心行為涉及「等待 running 結束」的 polling loop。如果不 mock 時間函式，測試必須真的 sleep 幾秒才能走到「狀態從 running 變 idle」的分支，讓測試慢且脆弱。把 `monotonic` 換成「每次 +0.1s」的 fake，讓 polling loop 能在毫秒內跑完多次，精確驗「第一次 running → sleep → 第二次 idle → 繼續」這個 transition。

- **`test_rebuild_task_waits_for_running_then_rebuilds_and_retriggers`**

  **在測什麼**：`rebuild_schedule_task` 跟 `advance_day_task` 同樣的「等 running 結束 → 改 state → 自己 finalize → 條件 retrigger」骨架，只是中間做的事不同：rebuild 從 DB 撈 `(orders, creators)` → 呼叫 `rebuild_state` → `_finalize_run(new_state)` → 對 skipped 訂單推 WebSocket → 條件 retrigger。

  **為什麼這條測試是核心**：rebuild 改 async 之後，所有「endpoint 不再 block」的承諾都靠這個 task body 兌現。如果 task 內部沒乖乖 poll status 就先動手寫 state，會跟 in-flight `run_scheduling_task` 搶 `schedule:state`，rebuild 後的結果會被 in-flight task 結尾的 `_save_state` 覆蓋掉。這個 case 用「status 第一次 running、第二次 idle」的 fake 序列驗 sleep 真的有發生過一次（`len(sleep_calls) == 1`）才繼續，鎖死「先等再做」這個順序。同樣預先塞 POST-REBUILD op + 斷言 `compute_mock.assert_called_once_with(rebuilt_state)` 來鎖定 per-op 化後「rebuild 自己 finalize」的契約。

- **`test_rebuild_task_notifies_each_skipped_orders_creator`**

  **在測什麼**：rebuild 的「skip 通知」鏈路 — 兩筆訂單都因 `deadline_too_far` 被 skip，task 必須對每筆的 `creators[order_id]` 各打一次 `websocket.notify_user`，envelope `type` 是 `"schedule.rebuild_skipped"`、`reason` 對得上。

  **為什麼要驗兩筆（不是只一筆）**：一筆只能驗「有送出」，兩筆才能驗「每筆都各送一次、不會漏、不會合併」— 對應到實作裡的 `for skip in skipped` 迴圈是否正確 iterate。

- **`test_rebuild_task_uses_today_when_no_existing_state`**

  **在測什麼**：「Redis 沒有 `schedule:state`」的 fallback — task 必須用 `datetime.now(tz=UTC).date()` 當 base_date，而不是 raise exception 或用 `None`。這個 case 是首次部署 / Redis 完全清空後第一次打 rebuild 的場景。用 `capture_base` 攔截 `rebuild_state` 的第二個參數，確認真的是今天。

---

#### `backend/tests/api/test_schedule.py` — HTTP endpoint × 權限矩陣（21 個）

這組測試的目的是驗「HTTP 路由層的行為」：request 有沒有被正確解析、狀態判斷有沒有走對分支、權限有沒有正確執行、response schema 有沒有符合 contract。DB 用真實 Postgres（testcontainers），確保 SQL query 能真的跑通；Redis 和 Celery 換成 mock，把測試速度和隔離性拉到合理水準。

每個 endpoint 的測試矩陣覆蓋：正向路徑、關鍵的負向/邊界路徑（如佇列已有任務在跑）、以及「沒有正確角色 → 403」、「沒有 token → 401」這兩個固定的 auth guard case。

**auth guard case 的設計哲學**：每個需要 auth 的 endpoint 都必須測 403 跟 401，原因是「security regression 是最昂貴的 bug」— 如果某次重構不小心把 `require_roles` 拿掉了，只有 happy path 的測試不會抓到這個問題。403/401 的斷言 pattern 統一用 `res.json()["error"]["code"] == <status_code>`，驗的是 unified error envelope 的格式，而不只是 HTTP status code。

---

**`POST /schedule/trigger`**

- **`test_trigger_success_returns_202`**

  驗正向路徑：排程器 idle、scheduler 打 trigger，應該回 202 且 `delay()` 真的有被呼叫。斷言 `body["task_id"]` 和 `body["message"]` 確認 response schema 符合前端期待。這個 case 是「功能基本可用」的最低標準。

- **`test_trigger_returns_409_when_already_running`**

  驗「雙重觸發」的保護。`schedule:status` 裡放了 `{"state": "running"}` 代表已有一個 task 在跑。這時再打 trigger 必須回 409 且**不再 `.delay()`**，否則兩個 task 同時跑可能造成 state 競爭寫入 Redis。

- **`test_trigger_by_viewer_returns_403` / `test_trigger_without_token_returns_401`**

  這兩個是**不同的**拒絕路徑，對應 JWT 驗證的兩個獨立層次：

  - **403（Forbidden）**：請求帶了合法的 JWT token，token 解碼成功、能拿到 User 物件，但這個 user 的 role 是 `viewer`，而 endpoint 要求 `scheduler+`。`require_roles` 檢查通過 JWT 但擋下 role，所以是「有身份、沒有權限」→ 403。
  - **401（Unauthorized）**：請求根本沒有帶 token（`Authorization` header 缺失或空白）。FastAPI 的 `OAuth2PasswordBearer` 在找不到 token 時直接回 401，連解碼 JWT、查 User 這步都沒走到，是「沒有身份」→ 401。

  這兩個 case 每次 endpoint 改動後都一起跑，分別確認「role guard」和「auth guard」這兩層各自還在位，不能只靠一個代替另一個。因為 security regression 是最昂貴的 bug：如果重構時不小心把 `Depends(_WRITE_ROLES)` 拿掉，只有這兩個測試才能立刻抓到。斷言用 `res.json()["error"]["code"]`（unified error envelope）而非只看 HTTP status code，是為了同時驗「error 格式也是對的，不只是狀態碼湊巧正確」。

---

**`POST /schedule/operations`**

- **`test_operations_enqueues_and_triggers_when_idle`**

  驗完整的「CRUD → 排程」鏈路：op 有進 `pending_ops` 佇列（`llen == 1`）、idle 狀態下有觸發 `delay()`。這兩個斷言一起才完整 — 只驗佇列長度但不驗 delay 被呼叫，沒辦法確認訂單的排程請求真的送出去了。

- **`test_operations_skips_trigger_while_running`**

  驗「task 在跑的時候 CRUD 推 op 不重複觸發」的保護。op 仍然要進佇列（這樣 in-flight task 結尾才能撿到），但不重新 delay。斷言 `llen == 1` 且 `delay` 沒被呼叫，確認「enqueue」和「trigger」是兩個獨立的判斷，不能因為不 trigger 就連 enqueue 也跳過。

---

**`GET /schedule/status`**

- **`test_status_returns_redis_doc_when_present`**

  驗 API 有正確把 Redis key 的 JSON 反序列化成 response。同時驗三個欄位（`state / task_id / started_at`），確認整個 schema 的 mapping 都對，不只是 status 200。

- **`test_status_returns_idle_default_when_empty`**

  驗「Redis 沒有 status key 時的首次部署預設值」。這個 case 模擬的是全新部署後第一次打 status，必須回一個有意義的 response 而不是 500 或 null。斷言 `body["message"]` 確認這個 default message 字串沒有被其他人改掉。

---

**`GET /schedule/result`**

- **`test_result_returns_scheduled_orders_sorted_by_production_date`**

  驗三件事同時成立：（1）只有 `status=scheduled` 的訂單出現，`pending` 的訂單被過濾掉；（2）排序是 `scheduled_production_date` 升冪；（3）DB 裡 `daily_breakdown` 欄位是 NULL 時 endpoint 回空 list 而非 null 或 500。這個 case 故意建「排序相反」的兩筆訂單，讓 sorted-wrong 的 bug 立刻可見。

- **`test_result_includes_daily_breakdown_from_db_column`**

  **在測什麼**：驗 `GET /schedule/result` 把 `orders.daily_breakdown` JSONB 欄位的內容**忠實**轉成 response 裡的 `daily_breakdown` 陣列。讀路徑現在不碰 Redis、不跑 `compute_schedule`，純粹從 Postgres 取資料：

  | 來源 | 提供的資訊 | 由誰寫入 |
  |---|---|---|
  | **DB（Postgres）— summary 欄位** | `status=scheduled` 過濾、`scheduled_production_date` / `expected_delivery_date` | `apply_schedule` 在排程結束後寫入 |
  | **DB（Postgres）— `orders.daily_breakdown` JSONB** | 該訂單在哪幾天各做多少 wafer 的逐日切分 | `materialize_schedule_task`：跑 `compute_schedule(state)` 後把每筆訂單的逐日結果寫進 JSONB |

  **目的**：把 `compute_schedule` 從讀路徑搬到寫路徑後，這個 case 變成 endpoint 層的 contract 守門員 — 確保（a）endpoint 真的有把 JSONB 欄位翻成 response；（b）JSON 內部的 `date` 字串能被 Pydantic 正確 parse 回 `date` 物件；（c）沒漏掉欄位導致前端拿到的是 `[]`。

  **測試手法**：建一筆 `status=scheduled` 的訂單，直接把 `order.daily_breakdown` 賦值成 `[{"date": base, "quantity": 10000}, {"date": base+1, "quantity": 5000}]` 後 `db.commit()`，打 `GET /schedule/result` 並驗 response 的 `daily_breakdown` 跟塞進去的 JSON 完全一致。**不再**需要建 SchedulerState 或調 fake_redis — 那部分的 forward-fill 邏輯已經移到 `materialize_schedule_task` 的測試裡驗。

  **為什麼這樣設計**：把 `daily_breakdown` 從「即時計算」改成「持久化欄位」之後，讀路徑只剩 SELECT，效能可預期、語意單純；trade-off 是寫路徑多了一個 JSONB 寫入，但因為 materializer 本來就要 commit DB，這個成本基本是免費的。

- **`test_result_excludes_soft_deleted_orders`**

  驗軟刪除的 filter。`is_deleted=True` 的訂單即使 `status=scheduled` 也不能出現在結果裡，這是 `get_scheduled` repo function 的職責。

  **為什麼 `is_deleted=True` 但 `status=scheduled` 可以同時存在**：正常的 `delete_order` 業務邏輯會把 `is_deleted=True` 和 `status=cancelled` 一起設。但這個測試**刻意繞過業務邏輯，直接用 ORM 把 `is_deleted` 設成 True，同時讓 `status` 維持 `scheduled`**，模擬「非正常路徑」。這類情況在真實部署中確實可能發生，例如：
  - 直接在 DB console 或 migration script 裡修改資料
  - 一個 bug 讓 `is_deleted` 和 `status` 的更新有 race condition 而沒有 atomic
  - 未來加了新的 soft-delete 路徑但漏了同步更新 status

  這個測試的目的是確認「`get_scheduled` 的篩選條件是 `is_deleted.is_(False)`，不是 `status != cancelled`」。兩個條件在正常情況下等效，但如果只靠 status 來篩，以上這些異常情況就會讓「已刪除的訂單」漏進結果。用真實 Postgres 跑，確認 SQL WHERE 條件真的有效（如果 ORM 的 `is_deleted.is_(False)` 拼錯了，用 SQLite 的 loose typing 可能不會發現）。

---

**`POST /schedule/rebuild`**（rebuild 已改 async，API 層只驗「dispatch 行為」；rebuild 本身的 wait + rebuild + notify + retrigger 在 worker 那組測）

- **`test_rebuild_returns_202_and_dispatches_task`**

  **在測什麼**：rebuild 的核心 happy path — endpoint 收到 POST 後 `rebuild_schedule_task.delay()` 真的有被呼叫、回 202、response body 有 `task_id` 和「queued」message。**不再驗 Redis 的 `schedule:state` 變化**，因為 endpoint 只 dispatch、不直接寫 state（state 寫入發生在 worker 的 task body 內）。

- **`test_rebuild_dispatches_even_when_run_scheduling_is_running`**

  **在測什麼**：rebuild 在 `schedule:status.state == "running"` 時**不再 409**，而是同樣 dispatch task。task 自己會 poll status 直到 idle 才動手。這個 case 把舊的 `test_rebuild_returns_409_when_already_running` 替換掉，反映 async 化後的行為差異。

  **為什麼這個 case 重要**：行為改變很容易因為 refactor 不小心退回舊邏輯（在 endpoint 端讀 status 然後 raise 409）。這個測試把「endpoint 收到 running status 時也必須 dispatch」這條 invariant 釘住。

---

#### `backend/tests/services/test_websocket.py` — WS publisher（3 個）

Publisher 是 worker 送訊息給前端的「出口」，它的職責只有一件事：把呼叫者給的 payload 包成正確格式的 envelope 並 PUBLISH 到 Redis channel。測試只需驗「envelope 格式對」和「Redis 故障時不往上拋例外」，不需要真實 Redis 或前端接收端。

- **`test_broadcast_publishes_envelope_with_kind_broadcast`**

  驗 envelope 的 JSON 結構嚴格符合 `{"kind": "broadcast", "payload": {...}}`。

  **`kind` 是什麼，為什麼不叫 `type`**：整個訊息路徑分兩層：
  1. **Redis pub/sub 層 envelope**（publisher → subscriber/ConnectionManager）：用 `"kind"` 欄位做 dispatch，`"broadcast"` 代表送給所有連線的 client，`"notify_user"` 代表只送給特定 user_id 的連線。
  2. **WebSocket payload 層**（ConnectionManager → 前端 JS）：`payload` 裡面可能有 `"type"` 欄位（例如 `"schedule.updated"`），前端 JS 依這個 `type` 決定要做什麼（例如刷新 API、顯示 toast）。

  之所以分開命名是為了避免混淆：`kind` 是「傳遞機制的 routing 指令」（誰要收到），`type` 是「前端的業務事件名稱」（收到後做什麼）。如果 key 被打錯（例如誤用 `"type"` 代替 `"kind"`），subscriber 就找不到 dispatch key，所有廣播都會被靜默丟棄，前端永遠收不到 `schedule.updated`。

- **`test_notify_user_publishes_envelope_with_user_id`**

  驗 `user_id` 欄位是字串而不是 UUID 物件。Redis pub/sub 傳的是 JSON 字串，`uuid.UUID` 物件不能直接 JSON serialize。subscriber 收到後需要 `uuid.UUID(envelope["user_id"])` 才能查找 `ConnectionManager` 裡的連線，如果 publisher 傳的 `user_id` 格式不對，subscriber 這邊就會解析失敗。

- **`test_publisher_swallows_redis_errors`**

  驗「best-effort delivery」的降級行為。`PUBLISH` 如果 raise `ConnectionError`，publisher 必須 catch 並只 log，**不能讓例外傳播到 caller**。

  **為什麼不能傳播**：publisher 的 caller 是 `run_scheduling_task`（Celery task）。如果 publisher 把 `ConnectionError` 往上丟，Celery task 就會 FAIL，而 `schedule:status` 就會被 worker 寫成 `{"state": "failed"}`。這會造成兩個問題：(1) 之後的排程觸發都會被 409 擋掉（因為偵測到 status=failed 的邏輯如果沒有特別處理，可能讓排程器卡死）；(2) 明明排程計算和 DB 寫入都成功了，只是 WebSocket 通知沒送出，卻讓整個任務被標記為失敗，這是過度懲罰的設計。

  WebSocket 通知是「讓前端即時刷新的便利功能」，不是排程的核心保障。如果 Redis pub/sub 短暫中斷，前端下次主動 poll `GET /schedule/result` 就能拿到最新結果。所以 publisher 的設計原則是：**「能送就送，不能送就 log 警告，絕不影響主流程」**。同時驗兩個函式（`broadcast` + `notify_user`）都有正確的 exception handling，確保沒有人漏寫。

---

#### `backend/tests/api/test_websocket.py` — WS endpoint + manager（12 個）

WebSocket 的測試分三個層次，每個層次的目的不同：

**`ConnectionManager` — 純 async 單元測試**（使用 `AsyncMock`，沒有真實 network）

- **`test_manager_connects_and_routes_send_to_user_only_to_target`**

  驗 `send_to_user` 的精確 routing。同一個 user 開了兩個 socket（模擬同一帳號在兩個 tab 都連著），另一個 user 有一個 socket。對 user A `send_to_user` 後，A 的兩個 socket 都要收到（多 tab 同步），B 的 socket 絕對不能收到（不能洩漏給其他人）。

  **為什麼用 AsyncMock 而不是真實 WebSocket**：真實 WebSocket 需要 HTTP server + client 建立連線，整個 async lifecycle 很難在 unit test 裡控制。`AsyncMock` 讓我們可以用 `socket.send_json.assert_awaited_once_with(...)` 精確驗「這個 socket 有沒有被呼叫到、呼叫幾次、傳了什麼內容」，而不需要真的送出 network frame。

- **`test_manager_disconnect_removes_socket_and_cleans_empty_user`**

  驗 disconnect 的清理行為：socket 走後要從 set 裡移除，如果這個 user 的所有 socket 都走了，整個 user key 要從 `_connections` dict 拿掉（避免 memory leak）。還有「disconnect 同一個 socket 兩次是 no-op 而非 crash」，因為 endpoint 的 finally block 可能在 exception 路徑下被呼叫多次。

- **`test_manager_send_failure_does_not_remove_socket`**

  驗一個微妙的設計決定：`send_json` 拋例外時，socket 不應該被踢出 `_connections`。原因是「能不能 send」不等於「這條 WebSocket 連線還在不在」 — 可能只是暫時的 backpressure 或 transport buffer 問題。真正的斷線信號來自 `WebSocketDisconnect`，由 endpoint 的 receive_text 迴圈負責觸發 disconnect 清理。如果 send 失敗就自動踢掉，一個暫時的 send error 就會讓 user 後續的通知都收不到。

- **`test_broadcast_continues_past_unexpected_exception`**（PR-review 補強）

  **在測什麼**：`_send_all` 內的 except 範圍 — 任何例外（典型來源是 TCP-reset 的 `OSError`、奇怪的 transport-layer error）都不能讓整個廣播迴圈中止。原本只 catch `(WebSocketDisconnect, RuntimeError)`，TCP 層斷掉的客戶端會丟 `OSError`，迴圈一旦在中間 unwind，**iteration 順序排在那條壞 socket 後面的所有 client 都收不到這則訊息**，但前面的 client 收得到 — 這是最難察覺的 bug 之一，因為部分使用者拿到通知、部分沒有，沒人會察覺資料不一致。

  **為什麼修法是 `except Exception` 而不是列舉所有類別**：HTTP/WebSocket transport 在不同 ASGI server / OS 下可以丟出的 exception 類太多（`OSError`、`anyio.EndOfStream`、`h11._util.RemoteProtocolError` 等等），列舉永遠列不完。這條測試用 `OSError` 當代表，鎖死「broadcast 對任何 send 失敗都是 log + 繼續」這個契約。

  **手法**：建一個 `bad` socket（`send_json.side_effect = OSError(...)`）夾在兩個 `good` socket 中間，呼叫 `manager.broadcast(...)`，斷言 `delivered == 2`（壞的那條沒成功、沒被算入）跟兩個 good socket 都有被 `assert_awaited_once_with` 收到訊息。修法之前這條會 fail：`OSError` 沒被 catch、迴圈 unwind、第二個 good socket 的 `assert_awaited_once_with` 會抓到「沒被 await」。

**`_handle_event` — async 單元測試**（測 Redis 訊息進來後怎麼被 dispatch）

- **`test_handle_event_dispatches_broadcast` / `test_handle_event_dispatches_notify_user`**

  驗 subscriber 收到 envelope 之後有正確根據 `kind` 欄位呼叫 `manager.broadcast` 或 `manager.send_to_user`。把真實的 `ConnectionManager`（帶著 mock socket）monkeypatch 進去，確認端對端的 message routing 邏輯正確。

- **`test_handle_event_drops_malformed_json` / `test_handle_event_drops_unknown_kind`**

  驗錯誤的 envelope 不會讓 consumer loop crash。consumer loop 是一個長期存活的 async task，一旦 crash 就要等到下次 FastAPI 重啟才恢復，期間所有 WebSocket 通知都沉默。這兩個 case 確認 handle_event 有足夠的 defensive parsing。

**WebSocket endpoint — 整合路徑**（走完 ASGI handshake，需要真實 Postgres 驗 JWT）

- **`test_websocket_connects_with_valid_token`**

  驗最基本的 happy path：有效 JWT + 正確 `?token=` 參數 → 連線被 accept，`with client.websocket_connect(...)` 進去不丟 exception。這個測試的斷言很簡單（「沒 throw 就過」），但它驗的是整個「token parse → `decode_access_token` → `manager.connect` → `accept()`」這條 chain 沒有問題。

- **`test_websocket_rejects_invalid_token`**

  驗 token 驗失敗時用 close code `4401` 而非標準的 4000 或 1008。`4401` 是 RFC6455 application-defined 區段（4000–4999）中我們自己定義的 code，對應 HTTP 401 語義，讓前端能夠區分「server 主動關掉」（4401 → 刷 token + 重連）和「server 故障」（其他 code → 指數退避重連）。

---

#### `backend/tests/services/test_order.py` — `apply_schedule` 稽核 + case-8 smart routing（10 個）

包括 PR-review 補強的 2 個 `apply_schedule` audit DB 測試，再加 Phase 2 新增的 8 個 compound build 測試 — 涵蓋 create / delete（pinned / 非 pinned）/ update（純 modify / pinned auto-re-pin / pinned silent-drop / notes-only skip）。每個都驗證 `enqueue_compound` 被呼叫時帶的是哪一種 ops 序列、哪一個 group。

這份檔案的範圍刻意收窄 — 只圍繞 PR review 第 3 點要求的「`apply_schedule` 必須把 `order.scheduled` 事件落到 `audit_logs` DB table，而不是只發 stdout audit log」這個契約。`services/order.py` 的其他路徑（CRUD、batch update 等）的測試覆蓋不在這個檔案的範圍內，靠 `tests/api/test_orders.py` 的 endpoint 測試從上層帶到。

**為什麼必須用真實 DB**：worker / API 層的 `apply_schedule` 過去都是 mock 掉的，那種測試對「audit row 有沒有真的寫進 DB」這條契約完全沒幫助 — 即便有人不小心把 `audit_log_repo.create(...)` 整段刪掉只留下 stdout，mock 測試還是會綠燈。所以這個檔案直接走 `db_session` fixture（real Postgres via testcontainers），跑完 `apply_schedule(db_session, scheduled)` 後再 `select(AuditLog).where(action == "order.scheduled")` 把 row 撈出來核對。

- **`test_apply_schedule_persists_audit_row_per_order`**

  **在測什麼**：每筆被排程的訂單都要在 `audit_logs` 留下一行，欄位包含 `action="order.scheduled"`、`user_id=None`（系統觸發）、`resource_type="order"`、`resource_id` 是訂單 UUID、`new_value` 是 `{scheduled_production_date, expected_delivery_date, status}` 三鍵 JSON。

  **為什麼跨日 + 單日各塞一筆**：`apply_schedule` 內部會把同一張訂單跨多天的 `ScheduledResult` 折疊成 `(earliest, latest)`。光驗單日 schedule 沒辦法區分「真的有跑 fold」跟「直接拿 first 當 earliest, last 當 latest」這兩種實作，得用一張多日訂單（拆成兩筆 ScheduledResult）才能驗 fold 邏輯把 5/12 跟 5/13 收成 `earliest=5/12, latest=5/13`，並把這兩個值寫進 `new_value`。同時搭配一張單日訂單驗 `earliest == latest` 的退化情況也是對的。

  **斷言為什麼不只看 row count**：count 只能驗「有寫」，不驗「寫對」。把 row 用 `resource_id` 拆成 `by_order` dict 後逐欄位斷言 `new_value`，能抓到「日期 stringify 成錯的格式」「status 沒寫進去」「user_id 不小心填成 fallback default」這類 silent-corruption bug。

- **`test_apply_schedule_with_no_results_writes_no_audit_rows`**

  **在測什麼**：`apply_schedule(db, [])` 必須是 noop — 0 筆 audit row 寫入、`applied == 0`。

  **為什麼這個邊界很容易踩雷**：實作裡 `clear_scheduled_dates` 是「先清掉所有舊 scheduled 訂單的日期欄」，然後對 `per_order` dict 逐筆呼叫 `set_schedule_dates` + 寫 audit。如果有人把 audit 寫入放錯位置（例如不小心放進 `clear_scheduled_dates` 的 loop 裡，或對「被清空的訂單」也寫一筆 `order.scheduled`），空 list 進來時就會多出一堆「其實沒有被排程到」的 audit row 出來。這條測試先建一張 `pending` 訂單，跑空 list 進去，最後驗 `audit_logs` 裡完全沒有 `order.scheduled` row，鎖死「audit 寫入只跟 applied 訂單同步、不跟 cleared 訂單同步」這個語意。

### 6.3 跑測試

在 `backend/` 目錄下：

```bash
uv run pytest tests/services/test_scheduling.py -v        # 純算法，秒過
uv run pytest tests/services/test_websocket.py -v         # WS publisher，秒過
uv run pytest tests/services/test_order.py -v             # apply_schedule audit DB 寫入，起 testcontainer
uv run pytest tests/workers/test_scheduling_task.py -v    # mock 全包，秒過
uv run pytest tests/api/test_schedule.py -v               # 起 testcontainer Postgres，較慢
uv run pytest tests/api/test_websocket.py -v              # 起 testcontainer Postgres + asyncio 子集，較慢
uv run pytest tests/services tests/workers tests/api      # 全部
```

> **如果 worker 測試獨立跑炸了**：通常是 `tests/conftest.py` 沒走到（沒人 demand `postgres_container`），環境變數沒 set。`tests/workers/conftest.py` 已用模組層 `os.environ.setdefault` 補上 fallback。

### 6.4 測試慣例（與 [`backend/CLAUDE.md`](../backend/CLAUDE.md) 一致）

- 模組層級 helper（`_make_user` / `_make_order` / `_FakeRedis` 等），**不另外定義 pytest fixture**
- 每個測試的 username 必須唯一以避開 unique constraint
- 錯誤回應斷言用 `res.json()["error"]["code"] == <status_code>`
- Celery task 用 `task.apply()` 同步跑（`bind=True` 的 `self.request` 會被正確注入）

---

## 7. 檔案總覽（reference）

### 新增 / 修改

| 路徑 | 類型 | 摘要 |
|---|---|---|
| `backend/app/services/scheduling.py` | 演算法核心 | `SegmentTree`、`SchedulerState`、`add_order` / `remove_order` / `compute_schedule` / `advance_day` / `rebuild_state`、日期轉換 helper |
| `backend/app/services/websocket.py` | WS publisher | 同步 `notify_user` / `broadcast`，把 envelope `PUBLISH` 到 Redis pub/sub channel `schedule:ws:events`；Redis 故障時靜默降級不影響 caller |
| `backend/app/services/order.py` | 既有 service | 新增 `list_scheduled_orders`、`list_for_scheduler`、`apply_schedule`（內含 audit log emission） |
| `backend/app/repositories/order.py` | 既有 repo | 新增 `get_scheduled`、`clear_scheduled_dates`、`set_schedule_dates` |
| `backend/app/workers/scheduling.py` | Celery task | `run_scheduling_task`、`advance_day_task`、`rebuild_schedule_task` |
| `backend/app/api/v1/schedule.py` | HTTP router | 5 個 endpoints；已在 `__init__.py` 註冊 prefix `/schedule` |
| `backend/app/api/v1/websocket.py` | WebSocket endpoint | `GET /api/v1/ws?token=<jwt>`、`ConnectionManager` 連線註冊表、`event_consumer_loop` 訂閱 Redis 把訊息 fan-out 給連線的 client |
| `backend/app/main.py` | 既有 entrypoint | lifespan 多起一個 `event_consumer_loop()` background task；shutdown 時 cancel |
| `backend/app/api/v1/__init__.py` | 既有 aggregate router | 多註冊 `websocket.router`（無 prefix，最終路徑 `/api/v1/ws`） |
| `backend/app/schemas/schedule.py` | Pydantic DTO | 6 個 schemas（含 `ScheduleRebuildResponse`、`DailyAssignment`） |
| `backend/tests/services/test_scheduling.py` | 單元測試 | 純算法 |
| `backend/tests/services/test_websocket.py` | 單元測試 | WS publisher（mock Redis 驗 envelope 格式 / 故障降級） |
| `backend/tests/workers/conftest.py` | 測試 bootstrap | env var setdefault |
| `backend/tests/workers/test_scheduling_task.py` | 單元測試 | mock 全包 |
| `backend/tests/api/test_schedule.py` | 整合測試 | 真 Postgres + mock Redis/Celery |
| `backend/tests/api/test_websocket.py` | 整合測試 | `ConnectionManager` 純 async 單元 + `_handle_event` 路由 + `TestClient.websocket_connect` 跑 connect / 4401 close |
| `docs/scheduling.md` | 文件 | **本檔** |

`backend/tests/services/__init__.py` 與 `backend/tests/workers/__init__.py` 是空的 package marker，未列入。

### 我刻意沒動

`backend/app/workers/celery_app.py`、`backend/app/api/v1/orders.py`、`backend/app/models/`、Alembic migrations、`backend/tests/conftest.py`、`README.md`（除新增本文連結）。串接點由你們依 §3 操作。

---

## 8. 已知限制 / 後續工作

- `celery_app.py` 已照 §3.1 顯式加 `imports=("app.workers.scheduling",)`，但 Beat schedule（換天 task 每天 00:00 UTC 觸發）仍需要部署環境另外註冊 `beat_schedule=`，沒寫的話 advance_day 不會自動跑。
- WebSocket 是 in-memory `ConnectionManager`：每個 FastAPI worker 進程各自持有自己的連線，靠 Redis pub/sub fan-out 同步事件。橫向擴展（uvicorn `--workers N` 或多台機器）時是 fan-out 模式（每個 worker 都收訊息但只送給自己手上的連線），不需要 sticky session；要把 `ConnectionManager` 的 metrics 暴露給 Prometheus 之類的監控時要 per-process aggregate。
- WebSocket 是 best-effort：worker `publish` 失敗會被 `services/websocket.py` 內部 catch 起來只 log warning，不會擋住 caller 的 transaction。如果 Redis pub/sub 中斷時段較長要保證訊息不漏，請另外加持久化（例如 Redis Streams + consumer group），目前的 `pub/sub` 不重送。
- `apply_schedule` 用 ORM session 更新每筆訂單，會 bump `version_id` 但不檢查它，理論上會跟同時的人工 PATCH 撞到 — 演算法有最終決定權，前端讀到時請以 server 值為準。
- `POST /schedule/operations` 的權限是 `scheduler+`，從另一個 backend 服務（例如 Order CRUD）打過來時要用 scheduler 權限的 token；如果 Order CRUD 跟排程跑在同一個程序內，建議用 §3.3.B 直連 Redis 省一次 HTTP。
- `compute_schedule` 是 forward-fill 結果（給前端看的時間表）；`capacity_tree` 的內部分布是 backward-fill（feasibility 檢查用），兩者語義不同，不要混用。
- `GET /schedule/result` 的 `daily_breakdown` 現在**直接讀 `orders.daily_breakdown` JSONB 欄位**，由 `materialize_schedule_task` 在每次排程被 accept 後寫入。讀路徑不再碰 Redis，所以 Redis state 被清也不影響 `daily_breakdown` 內容；但反過來說，如果 DB 跟 Redis state 脫節（極少見、通常代表 materializer 失敗或 DB rollback），這時 `POST /schedule/rebuild` 可以把 Redis state 拉回 DB 一致，再跑一次 materializer 把 `daily_breakdown` 改寫回正確值。
- `pending_ops` 的 `group` 欄位是 producer 的責任。複合更新（defer / shrink-qty / advance / grow-qty）兩筆 op 沒標到同一個 group 時 worker 不會擋，但會走錯 phase 影響可排程性。
- `docker-compose.yml` 目前還沒列 worker / beat 服務；本地開發要手動 `uv run celery ...`。
