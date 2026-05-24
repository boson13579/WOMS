# E2E Test Next Steps and Working Rules

## Current Status

目前已建立 Playwright E2E 測試架構，測試放在 `frontend/e2e/specs/`，共用操作放在 `frontend/e2e/helpers/`。

目前完整 E2E suite 已可執行：

```bash
cd frontend
pnpm test:e2e
```

最近一次完整測試結果：

```text
35 passed
```

## Covered Areas

目前已涵蓋的主要流程：

- Auth flow
  - 未登入進入受保護頁面會導回登入頁
  - 正確帳密登入
  - 錯誤密碼顯示錯誤
  - 已登入使用者進入登入頁會被導回 dashboard
  - 註冊後可登入

- Order lifecycle
  - viewer 可看訂單但不能寫入
  - order manager 可建立訂單
  - order manager 可編輯自己的訂單
  - order manager 可刪除自己的訂單
  - 建立訂單時 wafer quantity 需介於 25 到 2500

- RBAC
  - scheduler 看得到排程按鈕
  - order manager 看不到排程按鈕
  - viewer 看不到寫入與排程操作
  - order manager 不能編輯其他 manager 的訂單
  - 非 root 不能進 user management
  - root 可進 audit log
  - scheduler 不能進 audit log
  - scheduler 可進 observability，viewer 不可

- Scheduling / calendar
  - scheduler 可觸發排程
  - scheduler 建立 pending order 後，可在 calendar 的 unscheduled list 看到該訂單

- Search / filter
  - 用 customer name 搜尋訂單
  - 用 pending status 過濾訂單
  - reset search 與 filter
  - customer name 排序
  - 用 order number 搜尋
  - 搜尋無結果時顯示 empty state

- User management
  - root 可搜尋帳號
  - root 可用 email 搜尋帳號
  - root 可修改帳號角色
  - root 可停用帳號，且該帳號無法登入
  - root 不能停用或修改自己的帳號
  - 搜尋無結果時顯示 empty state

- Notifications
  - 使用者可開啟通知中心並切換 unread/all tabs
  - 使用者可將單筆通知標成已讀
  - 使用者可將全部通知標成已讀

## Remaining Work

接下來可以繼續補的測試，依建議優先順序：

1. Calendar deeper behavior
   - 測試 calendar search。
   - 測試 selected date 切換後右側清單更新。
   - 暫時不優先測 drag and drop，因為它比較脆弱，也會真的改排程資料。

2. Notification edge cases
   - 沒有 unread notifications 時，mark all read button 的 disabled 狀態。
   - all tab 裡已讀通知仍然看得到。
   - notification badge/count 是否正確更新。

3. Audit log
   - root 執行關鍵操作後，audit log 可搜尋或看到該操作。
   - 例如 role change、deactivate user、order update。

4. Observability
   - scheduler/root 可進 observability。
   - viewer 被 redirect。
   - 若頁面有明確 metrics cards，可補 smoke assertion。

5. Error states
   - 表單 validation 顯示。
   - API 失敗時 UI 是否保留在目前頁面並顯示錯誤。
   - 這類測試需要小心，不要依賴不穩定的後端狀態。

6. Cleanup / maintainability
   - 若某些 helper 開始變長，再考慮抽成 page object。
   - 目前不需要為了形式硬做 POM。

## Working Rules Requested by User

後續寫 E2E 時請遵守以下規則：

- 回覆與教學用中文。
- 程式碼、檔名、變數名稱維持英文。
- 測試名稱可以用中文，方便閱讀。
- 每次新增測試時，要一邊簡短解釋：
  - 這個測試在驗證什麼。
  - 為什麼這樣寫。
  - `page`、`locator`、`expect`、helper 的用途。

- 不要一次講太多理論，優先用目前寫到的測試解釋。
- 不要只提出計畫；如果方向明確，就直接實作。
- 每次改 component 時，要說明是不是只加 `data-testid`，還是有改產品邏輯。
- 除了 `data-testid` 或修明確 bug，不要隨便改 component 行為。
- 如果測試失敗，要先分辨：
  - assertion failure：通常是測試或產品行為不一致。
  - worker/browser crash：可能是環境或 Playwright worker 問題。
  - timeout：可能是 selector 不穩、資料沒出現、或後端處理太慢。

- 每補一批 E2E 後，要跑相關 spec。
- 若有改 component 或 helper，至少跑 TypeScript check：

```bash
cd frontend
pnpm exec tsc --noEmit
```

- 重要變更後要跑完整 E2E：

```bash
cd frontend
pnpm test:e2e
```

- 不要在 final response 裡暴露帳號密碼。
- 測試需要帳密時使用環境變數：

```bash
E2E_ADMIN_USERNAME
E2E_ADMIN_PASSWORD
```

## Helper Usage Rules

helper 的用途是把重複的測試操作包起來，不是把後端搬到前端。

目前 helper 分工：

- `auth.ts`
  - 登入 UI。
  - 透過 API 建立不同 role 的測試使用者。

- `orders.ts`
  - 建立訂單 UI 操作。
  - 找到訂單 row。

- `notifications.ts`
  - 建立通知測試資料。
  - 用 API 做 setup，再用 UI 驗證使用者行為。

- `data.ts`
  - 產生唯一 suffix，避免測試資料互相撞名。

原則：

- 真正要驗證的行為用 UI 做。
- 測試前置資料可以用 API 做，讓測試更快、更穩。
- 如果同一段 UI 操作重複兩次以上，可以考慮抽 helper。
- 不要過早抽 POM；等登入表單、訂單頁或使用者管理頁操作開始大量重複，再抽 page object。

## Selector Rules

E2E selector 優先順序：

1. `getByRole`
2. `getByLabel`
3. `getByText`
4. `getByTestId`

但如果畫面文字會變、中文可能亂碼、或 UI 結構容易調整，優先補 `data-testid`。

`data-testid` 應該描述使用者看得到的功能區塊或操作，例如：

- `orders-calendar-dialog`
- `orders-calendar-grid`
- `orders-calendar-unscheduled-list`
- `notification-card`
- `notifications-mark-all-read-button`

不要用太實作細節的名稱，例如：

- `div-1`
- `blue-card`
- `button-wrapper`

## Current Caution

Calendar drag and drop、order lock、部分 scheduling materialization 流程目前先不要急著測。

原因：

- 這些流程牽涉後端非同步狀態。
- 容易讓測試變慢或 flaky。
- 目前先測穩定且高價值的讀取、權限、搜尋、建立、更新、刪除與通知流程。

