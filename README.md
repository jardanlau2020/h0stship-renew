# Host-Ship 自動續約

Jexactyl 面板 (panel.host-ship.com) API 自動續約, GitHub Actions 每日 2 次 (北京 12:00 / 00:00)。

## Secrets
- `PANEL_USER` / `PANEL_PASS` — 面板帳密
- `PANEL_URL` — 預設 https://panel.host-ship.com
- `SERVER_IDS` — 逗號分隔, 預設 3dee8360
- `TG_BOT_TOKEN` / `TG_CHAT_ID` — 通知

## 登入方式
Pterodactyl/Jexactyl 系: GET /sanctum/csrf-cookie → POST /auth/login (X-XSRF-TOKEN, URL-decode 後) → POST /api/client/servers/{id}/renew
