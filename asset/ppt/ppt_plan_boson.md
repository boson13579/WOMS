# Boson PPT 規劃 — Smart Order Management System v4

> 5 張 slide、視覺驅動。
> 故事線：**紀律 → 三個 dashboard 各自一張全圖呈現**。
> 講稿用評分標準的關鍵字暗示對應、不明示打分。

## 時間分配（依 [`speechdraft.md`](speechdraft.md) 實算）
| Slide | 時間 | 段落 |
|---|---|---|
| 1 | ~15 sec | Outline |
| 2 | ~41 sec | 工程紀律（規範 + CI） |
| 3 | ~17 sec | Dashboard — 業務面儀表板 |
| 4 | ~37 sec | Observability — 工程面三層指標 + 業務 SLI |
| 5 | ~12 sec | Audit Log — 合規取證 |
| **合計** | **~123 sec ≈ 2.0 min** | 對應 2.5 min 預算（10 sec buffer）/ 1 min 預算還要再砍 60 sec |

---

## Slide 1 — Outline (~15 sec)

### 螢幕呈現
- **標題**：Smart Order Management System (WOMS)
- **副標**：晶圓代工訂單管理 + 智慧排程系統
- **議程**：
  1. 開發規範與 CI
  2. User Story 與整體架構
  3. 三大功能區塊（認證 / 訂單與排程 / 監控）
  4. 測試與排程設計
  5. AWS 部署
  6. Demo

### 講稿 (~12 sec, ~45 字)
> 「我們做的是晶圓代工的訂單管理 + 智慧排程系統。
> 簡報分六段，包含開發規範跟 CI、架構、功能、測試、部署、還有最後的 demo。」

---

## Slide 2 — 工程紀律 (~30 sec)

### 螢幕呈現（兩欄、無 footer）

**標題**：工程紀律 (RULE.md)

Audit Log 留給 Slide 5 講，這張不再 footer 重複。

```
┌──────────────────────┬──────────────────────────┐
│  📐 規範              │  ⚙️ CI                    │
│                      │                          │
│  • 12-Factor App     │      PR                  │
│  • Bulletproof       │       │                  │
│    React             │       ▼                  │
│  • Google API /      │  ┌─────────────┐         │
│    Python 規範        │  │  Backend    │         │
│  • FastAPI 嚴格分層   │  │  ruff       │         │
│                      │  │  mypy --strict│        │
│                      │  │  pytest     │         │
│                      │  └──────┬──────┘         │
│                      │  ┌──────▼──────┐         │
│                      │  │  Frontend   │         │
│                      │  │  eslint     │         │
│                      │  │  tsc        │         │
│                      │  │  vitest     │         │
│                      │  └──────┬──────┘         │
│                      │         ▼                │
│                      │      merge 🟢            │
└──────────────────────┴──────────────────────────┘
```

### 講稿 (~30 sec, ~110 字)
> 「我們的開發規範參考業界做法 — 包含 12-Factor、Bulletproof React、Google 的 API 跟 Python 規範、還有 FastAPI Best Practice 的嚴格分層架構。
> 規範由 CI 強制執行 — 每個 PR 都要過 ruff、mypy strict、跟全套 pytest 才能 merge。前端則是 eslint、tsc、跟 vitest。」

---

## Slide 3 — Dashboard：業務面儀表板 (~25 sec)

### 螢幕呈現
- **標題**：Dashboard — Real-time scheduler operations
- **滿版截圖**：[`notes/ppt/dashboard.jpeg`](dashboard.jpeg)

**畫面要點**：
- 頂部 Scheduler 狀態卡片（state / task ID / started / finished）
- 兩顆按鈕：`Trigger scheduling` / `Rebuild`
- 中段：**Capacity (next 30 days)** 累積剩餘 wafer slot 折線圖
- 底部：左 Pending operations queue、右 Orders by status（pending / scheduled / in production / completed）

### 講稿 (~25 sec, ~95 字)
> 「第一個是 Dashboard、給排程操作者用的業務面儀表板。
> 最上面是 scheduler 狀態跟操作按鈕；中間 30 天的累積產能折線圖，可以一眼看出未來哪幾天還能接單；下面 Pending Ops 是 worker queue 即時深度，旁邊是訂單依狀態分布。
> 所有資料訂單事件用 WebSocket push、整體秒級反映。」

---

## Slide 4 — Observability：工程面三層指標 (~35 sec)

### 螢幕呈現
- **標題**：Observability — RED + USE + 業務 SLI
- **滿版截圖**：[`notes/ppt/observability.png`](observability.png)

**畫面要點**（從上而下四段）：
1. **Services row**：API / Postgres / Redis / Celery Worker 4 顆健康燈
2. **RED row**：Rate / Error rate / P95 latency / **Schedule lag P95**（最右是業務 SLI、不是教科書 RED）
3. **USE row**：DB connections（aggregate 多 replica）/ Redis memory / Live connections
4. **Top endpoints 表**：哪條 API 最熱、各自 P50/P95/P99

### 講稿 (~35 sec, ~140 字)
> 「第二個是 Observability、給工程跟 ops 用的。
> 最上一排是服務存活四顆燈。下面套兩套業界方法：
> RED 看 API 請求流的速率、錯誤率、跟延遲。USE 看資源層的 DB connection pool、Redis 記憶體、跟即時 WS 連線數。
> 再額外加一個 Schedule Lag P95，就是 user 觸發一個操作到 worker 真的寫進 DB 的時間 — 這直接回答『排程管線跟不跟得上』這個業務問題，比通用 SLO 有意義。
> 多 replica 部署的話、DB pool 卡會跨 pod 聚合、不會被 load balancer 騙到。」

---

## Slide 5 — Audit Log：合規取證 (~20 sec)

### 螢幕呈現
- **標題**：Audit Log — Cross-resource activity feed
- **滿版截圖**：[`notes/ppt/audit_log.png`](audit_log.png)

**畫面要點**：
- 頂部 4 個 filter：**Actor**（user 下拉）/ **Action** / **Resource type** / **From / To** 日期區間
- 下方 timeline：每列 `timestamp / actor / action / resource ID`
- 點任一列展開 → 看 before / after diff

### 講稿 (~20 sec, ~80 字)
> 「第三個是 Audit Log、root 才看得到。
> 上面 4 個維度 filter — 誰、做了什麼、改了哪種資源、時間範圍。下面是完整的 activity feed、按時間排。每一筆都點得開、可以看 before / after 改了什麼。
> 出事的時候直接拿這個查、不用翻 log。」

---

## 字數總計（依 [`speechdraft.md`](speechdraft.md) 實算）
| Slide | 字數 | 對應秒數 (220 字/min) |
|---|---|---|
| 1 | 55 | 15 |
| 2 | 152 | 41 |
| 3 | 62 | 17 |
| 4 | 137 | 37 |
| 5 | 45 | 12 |
| **合計** | **451** | **~123 sec ≈ 2.0 min** |

---
