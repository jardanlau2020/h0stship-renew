# Host-Ship 自動續約

Jexactyl 面板 (panel.host-ship.com) API 自動續約, GitHub Actions 每週 1 次 (週日 UTC 04:00 = 北京 12:00)。

續期步長: 一次 POST 約 +7 日、面板總上限 30 日; 成功後讀返 `renewal`, 未夠 28 日就隔 10 秒補點 (最多 3 次), 面板報「已滿 30 日」或剩餘日數無進展即停。

## Secrets
- `PANEL_USER` / `PANEL_PASS` — 面板帳密
- `PANEL_URL` — 預設 https://panel.host-ship.com
- `SERVER_IDS` — 逗號分隔, 預設 3dee8360
- `TG_BOT_TOKEN` / `TG_CHAT_ID` — 通知

## 登入方式
Pterodactyl/Jexactyl 系: GET /sanctum/csrf-cookie → POST /auth/login (X-XSRF-TOKEN, URL-decode 後) → POST /api/client/servers/{id}/renew
