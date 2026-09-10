#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Host-Ship (Jexactyl/Pterodactyl 系) 自動續約
- 登入: GET /sanctum/csrf-cookie → POST /auth/login (X-XSRF-TOKEN: URL-decode 後)
- 續期: POST /api/client/servers/{id}/renew
- 通知: Telegram
用法(環境變數): PANEL_USER / PANEL_PASS / SERVER_IDS(逗號分隔) / TG_BOT_TOKEN / TG_CHAT_ID
"""
import json
import os
import re
import sys
import time
import urllib.parse

import requests

PANEL = (os.environ.get("PANEL_URL") or "https://panel.host-ship.com").rstrip("/")
USER = os.environ.get("PANEL_USER", "").strip()
PASS = os.environ.get("PANEL_PASS", "").strip()
SERVER_IDS = [x.strip() for x in (os.environ.get("SERVER_IDS") or "3dee8360").split(",") if x.strip()]
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    print(msg, flush=True)


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log("(冇設定 TG, 跳過通知)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=20,
        )
        log(f"TG 通知 → HTTP {r.status_code}")
    except Exception as e:
        log(f"TG 通知失敗: {e}")


def login():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    # 1) CSRF cookie
    r = s.get(f"{PANEL}/sanctum/csrf-cookie", timeout=20)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"sanctum/csrf-cookie → HTTP {r.status_code}")
    xsrf = urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", ""))
    if not xsrf:
        raise RuntimeError("冇攞到 XSRF-TOKEN")
    # 2) 登入
    r = s.post(f"{PANEL}/auth/login",
               json={"user": USER, "password": PASS},
               headers={"X-XSRF-TOKEN": xsrf, "Referer": f"{PANEL}/auth/login"},
               timeout=20, allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f"登入失敗 → HTTP {r.status_code}: {(r.text or '')[:200]}")
    data = r.json().get("data", {}) if r.text else {}
    if not data.get("complete") and not data.get("user"):
        raise RuntimeError(f"登入未完成: {str(data)[:200]}")
    # 3) 行埋 / 攞齊 session cookie
    s.get(f"{PANEL}/", timeout=20)
    # 存返最新 XSRF, renew POST 都要帶 (URL-decode 後)
    s.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", "") or xsrf)
    log(f"✅ 登入成功: {data.get('user', {}).get('username', '')}")
    return s


def get_server(s, server_id):
    r = s.get(f"{PANEL}/api/client/servers/{server_id}", timeout=20)
    if r.status_code != 200:
        return None, f"GET server → HTTP {r.status_code}: {(r.text or '')[:150]}"
    return r.json().get("attributes", {}), ""


def renew_server(s, server_id):
    r = s.post(f"{PANEL}/api/client/servers/{server_id}/renew", timeout=25,
               headers={"Referer": f"{PANEL}/server/{server_id}", "Content-Type": "application/json"})
    body = (r.text or "").strip()
    if r.status_code in (200, 204):
        return True, f"✅ 續約成功 (HTTP {r.status_code})"
    # Jexactyl 常見: CD 期間會回 400 + detail 文字
    detail = ""
    try:
        detail = json.loads(body)["errors"][0]["detail"] if body else ""
    except Exception:
        detail = body[:200]
    # 已達上限 30 日 → 唔算失敗, 係「已滿」狀態 (唔使再續)
    if r.status_code in (400, 422) and "cannot add more than 30 days" in detail.lower():
        return True, f"⏭️ 已達續期上限 (30 日), 唔使再續 (HTTP {r.status_code})"
    return False, f"❌ 續約失敗 (HTTP {r.status_code}): {detail}"


def main():
    if not USER or not PASS:
        log("❌ 缺少 PANEL_USER / PANEL_PASS")
        send_tg("🔧 Host-Ship 續約: 缺少憑證 (PANEL_USER/PANEL_PASS)")
        sys.exit(1)

    log(f"🚀 Host-Ship 續約 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"面板: {PANEL} | 伺服器: {SERVER_IDS} | dry_run={DRY_RUN}")

    results = []
    try:
        s = login()
        for sid in SERVER_IDS:
            log(f"── 伺服器 {sid} ──")
            attrs, err = get_server(s, sid)
            if err:
                log(f"  {err}")
                results.append((sid, False, err))
                continue
            name = attrs.get("name", sid)
            renewable = attrs.get("renewable")
            renewal = attrs.get("renewal")
            log(f"  名稱: {name} | renewable={renewable} | renewal={renewal}")
            if not renewable:
                msg = f"⏭️ 而家唔可以續 (renewable=false, renewal={renewal})"
                log(f"  {msg}")
                results.append((sid, True, msg))
                continue
            if DRY_RUN:
                msg = "dry-run: 唔真正續約"
                log(f"  {msg}")
                results.append((sid, True, msg))
                continue
            ok, msg = renew_server(s, sid)
            # 面板 CD: "You can renew again in N seconds" → 等完再試 (最多 3 次)
            for _ in range(3):
                m = re.search(r"renew again in (\d+) seconds", msg, re.IGNORECASE)
                if ok or not m:
                    break
                wait = min(int(m.group(1)) + 3, 300)
                log(f"  ⏳ 面板 CD: 等 {wait} 秒再重試...")
                time.sleep(wait)
                ok, msg = renew_server(s, sid)
                log(f"  {msg}")
            if ok:
                time.sleep(2)
                attrs2, _ = get_server(s, sid)
                if attrs2:
                    log(f"  續後 renewal: {attrs2.get('renewal')} | renewable: {attrs2.get('renewable')}")
                    msg += f" | 新 renewal={attrs2.get('renewal')}"
            results.append((sid, ok, msg))
    except Exception as e:
        log(f"💥 錯誤: {e}")
        send_tg(f"🔧 Host-Ship 續約異常: {e}")
        sys.exit(1)

    # 匯總 + 通知
    lines = ["🎮 Host-Ship 自動續約", ""]
    all_ok = True
    for sid, ok, msg in results:
        lines.append(f"{'✅' if ok else '❌'} {sid}: {msg}")
        all_ok = all_ok and ok
    summary = "\n".join(lines)
    log("\n" + summary)
    send_tg(summary)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
