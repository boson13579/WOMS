# 把 Repo 推上 GitHub + 設定 CI + 看 PR 測試結果

這份是端到端的教學，從一個本機資料夾走到「同學提 PR 我能在網頁上看到測試有沒有過」。整個流程跑完約 15–30 分鐘。

---

## 0. 前置

- 已照 [README.md](../README.md) 跑過 first-clone walkthrough，本機可以 `make test` 全綠
- 有 GitHub 帳號
- （推薦）安裝 [GitHub CLI](https://cli.github.com/)：可以從 terminal 直接看 CI 狀態，不用一直開瀏覽器

```bash
# Windows (PowerShell)
winget install --id GitHub.cli
# 或下載 .msi: https://github.com/cli/cli/releases

# 確認
gh --version

# 第一次用：登入
gh auth login
# 選：GitHub.com → HTTPS → Yes (auth git) → Login with a web browser
```

---

## 1. 初始化 git repo（本機）

```bash
cd c:/NYCU/Cloud/Final

git init
git branch -M main
```

確認 `.gitignore` 有把該擋的擋掉（已經寫好了，但驗證一下）：

```bash
git status                       # 應該看不到 .env、.venv/、node_modules/、__pycache__/
git check-ignore -v .env         # 應該顯示 .gitignore:1:.env  .env
```

⚠️ **檢查 `.env` 有沒有不小心 stage 到**：

```bash
git status | grep -i env
# 應該只看到 .env.example、.env.example.md 之類，絕對不能看到 .env
```

如果看到 `.env`，立刻：
```bash
git rm --cached .env 2>/dev/null
echo ".env" >> .gitignore
```

---

## 2. 第一個 commit

```bash
git add .
git status                       # 再次確認 staged 的東西沒問題
git commit -m "feat: phase 1 scaffolding (config, base entity, error envelope, audit log, CI)"
```

---

## 3. 在 GitHub 建立 repo

### 方法 A：用 gh CLI（推薦）

```bash
gh repo create smart-order-management \
  --private \
  --source=. \
  --remote=origin \
  --push \
  --description="NYCU Cloud Final — Smart Order Management System"
```

一行搞定：建 repo、設 remote、push。

### 方法 B：用網頁

1. 打開 [https://github.com/new](https://github.com/new)
2. **Repository name：** `smart-order-management`
3. **Visibility：** Private（如果是課程作業）或 Public
4. **不要** 勾任何 "Initialize this repository with..." 選項（README/gitignore/license） — 我們本機已經有了
5. 按 **Create repository**
6. 把畫面上「…or push an existing repository from the command line」那塊指令複製貼上 terminal 跑：

```bash
git remote add origin https://github.com/<你>/smart-order-management.git
git push -u origin main
```

---

## 4. 確認 CI 自動跑起來

GitHub Actions workflow 已經寫好在 `.github/workflows/ci.yml`，**push 上去就會自動觸發**，不需要任何額外設定。

### 4.1 從網頁看

1. 打開你的 repo → **Actions** 分頁
2. 應該看到一個正在跑 / 剛跑完的 workflow run，叫 `CI` 或顯示你的 commit message
3. 點進去 → 看到兩個 job：
   - `backend (lint + test)`
   - `frontend (lint + test)`
4. 點任一個 job 進去看詳細 log（每一步的輸出都在）

### 4.2 從 terminal 看（gh CLI）

```bash
# 列出最近的 run
gh run list --workflow=ci.yml --limit 5

# 看最新一筆的 detail
gh run view --log

# 即時 watch（會等到跑完）
gh run watch
```

---

## 5. 設定 branch protection（保護 main）

這一步避免「測試還沒跑完就 merge」、「直接 push main 跳過 review」。

### 網頁路徑

1. Repo → **Settings** → **Branches**
2. **Branch protection rules** → **Add branch ruleset**（新版 GitHub）或 **Add rule**（舊版）
3. **Branch name pattern：** `main`
4. 勾以下（建議最小組合）：
   - ☑ **Require a pull request before merging** → **Require approvals**（個人專案可設 0；團隊建議 1）
   - ☑ **Require status checks to pass before merging**
     - 在搜尋框打 `backend` → 勾 `backend (lint + test)`
     - 同樣勾 `frontend (lint + test)`
     - ☑ **Require branches to be up to date before merging**
   - ☑ **Require conversation resolution before merging**（review comment 要解決完才能 merge）
5. **Save changes** / **Create**

> 🟡 必須先有過至少一次成功的 CI run，status check 名稱才會出現在搜尋框可選。所以順序：第一次 push → 等 CI 跑完 → 再來設 branch protection。

---

## 6. 完整模擬：開分支 → PR → CI → Review → Merge

下面是一次完整的流程演練。

### 6.1 開分支

```bash
git checkout -b feat/test-pr-flow
```

### 6.2 改一點東西

```bash
echo "" >> README.md
echo "<!-- testing PR flow -->" >> README.md
git add README.md
git commit -m "docs: test PR flow comment"
```

### 6.3 推上去

```bash
git push -u origin feat/test-pr-flow
```

terminal 會印出一個 `https://github.com/.../pull/new/feat/test-pr-flow` 的連結 — 點它直接到開 PR 的頁面。

### 6.4 開 PR

#### 用 gh CLI（最快）

```bash
gh pr create --base main --head feat/test-pr-flow \
  --title "docs: test PR flow" \
  --body "Verifying CI runs and PR checks display correctly."
```

#### 或網頁

1. 點剛剛 terminal 印出的連結
2. **base：** `main`、**compare：** `feat/test-pr-flow`
3. 寫 title + description
4. **Create pull request**

### 6.5 看 CI 在 PR 頁面跑

打開 PR 頁面，往下捲到底部會看到一塊「Some checks haven't completed yet」或「All checks have passed」：

```
┌─────────────────────────────────────────────┐
│ ⏱ Some checks haven't completed yet         │
│                                             │
│ ⏱ backend (lint + test)        Details     │
│ ⏱ frontend (lint + test)       Details     │
└─────────────────────────────────────────────┘
```

跑中是黃色圓圈、過了是綠勾、失敗是紅叉。點 **Details** 可以直接看那個 job 的 log。

從 terminal 看：

```bash
gh pr checks                     # 即時看當前 PR 的 CI 狀態
gh pr view --web                 # 在瀏覽器開 PR 頁面
```

### 6.6 假如 CI 紅了

點失敗 job 的 **Details** → 找紅色標記的 step → 看 log。常見原因：

| log 訊息 | 原因 | 修法 |
|---|---|---|
| `ruff check` 失敗 | code 不合 lint | 本機跑 `make lint`，再修；或 `make format` 自動修 |
| `mypy` 失敗 | 型別錯 | 看錯誤行號，加正確型別註記 |
| `pytest` 失敗 | 測試掛了 | 本機 `cd backend && uv run pytest -v` 重現 |
| `eslint` 失敗 | 前端 lint | `cd frontend && pnpm lint:fix` |
| `tsc` 失敗 | TypeScript 型別錯 | `cd frontend && pnpm typecheck` 看錯誤 |
| `psql: connection refused` | postgres service 還沒 ready | 看 `.github/workflows/ci.yml` 的 `health-cmd` 設定 |

修完 push 同分支會自動再觸發一次 CI：

```bash
git add ...
git commit -m "fix: ..."
git push                         # CI 自動 re-run
```

### 6.7 review

請隊友打開 PR：
- **Files changed** 分頁可以行內留言
- **Review changes** → 選 **Approve / Request changes / Comment** → **Submit review**

### 6.8 merge

當「All checks passed」+「至少一個 approval」+「conversation resolved」三個都打勾，**Merge pull request** 按鈕才會變綠。

選擇 merge 方式：
- **Merge commit** — 保留分支 history（適合多人協作）
- **Squash and merge** — 把 PR 內所有 commit 壓成一個（推薦個人 / 小團隊，main 更乾淨）
- **Rebase and merge** — 不產生 merge commit（main 是直線 history）

按下去 → 刪掉 source branch（GitHub 會問）→ 完成。

```bash
# 本機同步
git checkout main
git pull
git branch -d feat/test-pr-flow
```

---

## 7. 平常每天的工作流（Cheat sheet）

```bash
# 每天開工前
git checkout main && git pull

# 開新功能
git checkout -b feat/order-crud
# ... 寫程式 ...
make test                        # 本機先過再 push
git add .
git commit -m "feat(order): ..."
git push -u origin feat/order-crud

# 開 PR
gh pr create

# 看 CI
gh pr checks --watch             # 等到跑完

# CI 失敗 → 修 → push
make lint                        # 本機檢查
git add . && git commit -m "fix: lint"
git push

# 通過 + approve 後 merge
gh pr merge --squash --delete-branch
git checkout main && git pull
```

---

## 8. 進階：在 PR 裡加 status badge

把這段加到 `README.md` 最頂端：

```markdown
[![CI](https://github.com/<你>/smart-order-management/actions/workflows/ci.yml/badge.svg)](https://github.com/<你>/smart-order-management/actions/workflows/ci.yml)
```

每次有人逛到你的 repo 首頁都能一眼看到 CI 是綠是紅。

---

## 9. 補充：CI 跑多久？貴不貴？

- 公開 repo：**免費無限 minutes**
- Private repo：每個帳號每月 **2000 minutes** 免費（GitHub Free 方案）
- 我們的 CI：backend 約 60–90 秒，frontend 約 30–60 秒，**總計每次 push 約 1.5–2.5 分鐘**
- 一個月平均 50 次 push → 約用掉 100 分鐘，遠低於 free quota

如果之後 CI 跑太久（例如 Phase 2 加了大量 e2e test），可以：
- 用 `concurrency.cancel-in-progress: true`（已設）→ 同分支新 push 自動取消舊 run
- 加 `paths` filter → backend 改動不觸發 frontend job、反之亦然
- 把 testcontainers 換成 service container → 省 docker pull 時間

---

## 10. 出問題清單

| 症狀 | 解法 |
|---|---|
| Push 被擋 `! [rejected] main -> main (non-fast-forward)` | `git pull --rebase` 後再 push |
| Branch protection 說 status check 找不到 | 先 push 一次讓 CI 跑過、再回來設定 |
| `gh: command not found` | 裝 GitHub CLI（見上方第 0 節） |
| CI 在 PR 頁面顯示「Expected — Waiting for status to be reported」 | 通常是 Actions 還在排隊（GitHub 流量高峰會慢），等 1–2 分鐘 |
| 想重跑 CI（不想 commit 空白變更）| `gh run rerun --failed`，或網頁 Actions → 該 run → Re-run jobs |
| Token 過期、push 失敗 | `gh auth login` 重新登入；或 `gh auth refresh -s repo,workflow` |

---

更多開發規範見 [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md)。
