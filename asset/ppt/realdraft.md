
「大家好，我們是做晶圓代工訂單，智慧排程系統。簡報分為六段，包含CI 跟開發規範、接下來是架構、功能、測試、部署、還有最後的 demo。」
---

「我們的開發規範參考業界做法 — 包含 Bulletproof React、Google 的 API 跟 Python 規範、還有 FastAPI Best Practice 的嚴格分層架構。

規範由 CI 強制執行 — 每個 PR 都要過 ruff、mypy strict、跟全套 pytest 才能 merge。前端則是 eslint、tsc、跟 vitest。」

---

「 Dashboard、是業務面儀表板。

包含 scheduler 狀態；30 天累積產能折線圖 worker queue 即時深度，還有訂單狀態分布。」

---

「 Observability 利用兩套業界方法 — RED 看 API 請求速率、錯誤率、跟延遲。USE 看 DB connection pool、Redis 記憶體、跟即時 WebSocket 連線數量。

還有新增一個 Schedule Lag，就是 user 觸發一個操作到 worker 寫進 DB 的時間，比通用 SLO 有意義。

---

「最後是 Audit Log。任何訂單操作都完整記錄誰，甚麼時間，改動前後狀態，可以直接查閱，不用翻 log。」