# Playwright E2E Test Suite

## PPT 顯示內容（一頁）

### Playwright E2E Test Suite

**Goal**

- 建立可在完整 app 上執行的 E2E 測試
- 用真實瀏覽器驗證使用者流程
- 補足 unit / integration test 較難覆蓋的 UI、權限與跨頁流程

**Coverage**

- **Auth flow**
  - 未登入 redirect
  - 登入成功 / 錯誤密碼
  - 註冊後登入

- **Orders**
  - 建立 / 編輯 / 刪除訂單
  - 搜尋 / 篩選 / reset filter / 排序
  - 不合法數量驗證

- **RBAC & User Management**
  - 不同角色看到不同操作
  - order manager 不能編輯別人的訂單
  - 非 root 不能進 user management
  - root 可搜尋帳號、改角色、停用帳號

**Design Approach**

- 使用 `data-testid` 建立穩定 selector
- 使用 helper 封裝重複流程：`loginViaUi()`、`createOrderViaUi()`、`orderRow()`
- 測試資料使用唯一 suffix，避免和既有資料衝突

**Value**

- 驗證真實使用者流程，不只驗證單一 component
- 保護 RBAC 與管理操作等高風險流程
- 可整合到 CI pipeline，作為 merge 前的回歸測試

---

## 1.5 分鐘講稿

這次我針對 Smart Order 系統建立 Playwright E2E test suite。目標不是單純測 component，而是用真實瀏覽器驗證使用者流程，例如登入、建立訂單、搜尋篩選，以及不同角色看到的操作權限。

這套測試主要涵蓋三塊。第一是 Auth flow，包含未登入 redirect、登入成功、錯誤密碼與註冊後登入。第二是 Orders，包含建立、編輯、刪除、搜尋、狀態篩選、reset filter、排序，以及不合法數量驗證。第三是 RBAC 和 User management，例如 viewer 看不到寫入操作、order manager 不能編輯別人的訂單、非 root 不能進 user management，root 可以搜尋帳號、修改角色與停用帳號。

測試設計上，我們加入穩定的 `data-testid`，避免依賴 CSS 或容易變動的文字；也把重複操作抽成 helper，例如登入、建立訂單、找表格 row。測試資料會加唯一 suffix，避免跟舊資料衝突。

整體價值是讓測試更接近真實使用者操作，特別是 RBAC、帳號管理、訂單流程這些跨頁且高風險的情境。後續可以把這套 E2E 測試接到 CI pipeline，作為 merge 前的回歸檢查。

---

## 建議投影片版面

### Layout

- 左側：`Goal` + `Coverage`
- 右側：`Design Approach` + `Value`
- 中間或右上角用大字突出：`E2E coverage for critical user flows`

### 建議視覺重點

- 不要把所有 test case 全列出來，會太密。
- 每個區塊最多 3 到 4 個 bullets。
- 強調「測試的是使用者流程」和「保護高風險權限流程」。

