#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Host-Ship (Jexactyl/Pterodactyl 系) 自動續約
- 登入: GET /sanctum/csrf-cookie → POST /auth/login (X-XSRF-TOKEN: URL-decode 後)
- 續期: POST /api/client/servers/{id}/renew (面板一次約 +7 日、總上限 30 日
  → 成功後讀返 renewal，未到頂就隔 10 秒補點，見 renew_until_full())
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

# 續期補點 (面板一次 POST 約 +7 日、總上限 30 日)
# 成功後讀返 renewal（剩餘日數），未夠 TOPUP_GOAL_DAYS 就隔 TOPUP_INTERVAL_S 再點，
# 最多補 TOPUP_MAX_EXTRA 次；面板報「已滿」或剩餘日數無進展即停。
TOPUP_MAX_EXTRA = 3
TOPUP_INTERVAL_S = 10
TOPUP_GOAL_DAYS = 28


def log(msg):
    print(msg, flush=True)


def now_local():
    """UTC+8 當地時間 MM-DD HH:MM (runner 係 UTC)"""
    return time.strftime("%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


def fmt_renewal(v):
    """renewal 可能係 timestamp(秒/毫秒) 或 ISO/日期字串 → MM-DD HH:MM 或 MM-DD"""
    if v in (None, "", 0):
        return ""
    try:
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        if n > 1e9:
            return time.strftime("%m-%d %H:%M", time.gmtime(n + 8 * 3600))
    except (TypeError, ValueError):
        pass
    t = str(v)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", t)
    if m:
        return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(2)}-{m.group(3)}"
    return t[:19]


def days_left(v):
    """renewal → 約剩幾日 (int)；判斷唔到返 None（保守：唔會亂補點）
    面板直接俾日數 (e.g. 30) / 秒或毫秒 timestamp / ISO 日期字串都食得住。"""
    if v in (None, "", 0):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        from datetime import datetime
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ]?(\d{2})?:?(\d{2})?", str(v))
        if not m:
            return None
        try:
            dt = datetime(*[int(x) if x else 0 for x in m.groups()])
        except (TypeError, ValueError):
            return None
        return int((dt.timestamp() - time.time()) // 86400)
    if n < 1e6:                 # 面板直接俾「剩幾日」
        return int(n)
    if n > 1e11:                # 毫秒 timestamp
        n /= 1000.0
    return int((n - time.time()) // 86400)


def build_summary(results):
    """方案 B (極致精簡人話版): 每台精準兩行，徹底消滅頂部計數器"""
    blocks = []
    for r in results:
        name = r.get("name", "Host-Ship")
        act = r.get("action")
        exp = fmt_renewal(r.get("expire"))
        days = days_left(r.get("expire"))
        rem_str = f"（剩 {days} 天）" if days is not None else ""

        if act in ("renewed", "done"):
            l1 = f"✅ {name} · 成功續期" + (f"至 {exp}" if exp else "")
            l2 = "ℹ️ " + (f"剩餘 {days} 天 · " if days is not None else "") + "服務已自動展期"
            blocks.append([l1, l2])
        elif act == "failed":
            l1 = f"🚨 {name} · 續期未完成{rem_str}"
            reason = r.get("detail") or "執行失敗"
            l2 = f"⚠️ {reason} · 請登入面板手動處理"
            blocks.append([l1, l2])
        else: # skip / dry
            l1 = f"🟢 {name} · 狀態良好{rem_str}"
            info_parts = []
            if exp:
                info_parts.append(f"{exp} 到期")
            info_parts.append("未到續期窗口")
            l2 = "ℹ️ " + " · ".join(info_parts)
            blocks.append([l1, l2])

    if not blocks:
        return "🟢 Host-Ship · 檢查完成（未發現伺服器實例）"
    return "\n\n".join("\n".join(b) for b in blocks)


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


def read_renewal(s, server_id, delay=2):
    """隔 delay 秒讀返 /api/client/servers/{id}，回傳 renewal（讀唔到返 None）"""
    time.sleep(delay)
    attrs, err = get_server(s, server_id)
    if not attrs:
        log(f"  ⚠️ 續後讀取失敗: {err}")
        return None
    log(f"  續後 renewal: {attrs.get('renewal')} | renewable: {attrs.get('renewable')}")
    return attrs.get("renewal")


def renew_until_full(s, server_id):
    """首次 POST（連面板 CD 重試），之後剩餘日數未夠 TOPUP_GOAL_DAYS 就補點。
    返 (ok, msg, new_exp)；ok=False 時 msg 係首次失敗原因（供判「已滿 30 日」）。"""
    ok, msg = renew_server(s, server_id)
    log(f"  {msg}")
    for _ in range(3):          # 面板 CD: "You can renew again in N seconds"
        m = re.search(r"renew again in (\d+) seconds", msg, re.IGNORECASE)
        if ok or not m:
            break
        wait = min(int(m.group(1)) + 3, 300)
        log(f"  ⏳ 面板 CD: 等 {wait} 秒再重試...")
        time.sleep(wait)
        ok, msg = renew_server(s, server_id)
        log(f"  {msg}")
    if not ok:
        return False, msg, None
    new_exp = read_renewal(s, server_id)
    extra, prev_left = 0, None
    while True:
        left = days_left(new_exp)
        if left is None:
            log("  ⏭️ 剩餘日數讀唔到，唔補點")
            break
        if left >= TOPUP_GOAL_DAYS:
            log(f"  ✅ 剩 {left} 日，已近面板上限 (30 日)")
            break
        if prev_left is not None and left <= prev_left:
            log(f"  ⏭️ 補點後無進展 ({prev_left} → {left})，停")
            break
        if extra >= TOPUP_MAX_EXTRA:
            log(f"  ⏭️ 已補 {extra} 次，停 (剩 {left} 日)")
            break
        extra += 1
        prev_left = left
        time.sleep(TOPUP_INTERVAL_S)
        ok2, msg2 = renew_server(s, server_id)
        log(f"  🔁 補點 {extra}/{TOPUP_MAX_EXTRA}: {msg2}")
        if not ok2:
            if "30 days" in msg2 or "已達續期上限" in msg2:
                log("  ✅ 面板報已滿，當成功")
            else:
                log(f"  ⚠️ 補點失敗，收手: {(msg2 or '')[:120]}")
            break
        again = read_renewal(s, server_id)
        if again is not None:
            new_exp = again
    return True, msg, new_exp


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
                results.append({"name": sid, "action": "failed", "detail": err})
                continue
            name = attrs.get("name", sid)
            renewable = attrs.get("renewable")
            renewal = attrs.get("renewal")
            cur = {"name": name, "expire": renewal}
            log(f"  名稱: {name} | renewable={renewable} | renewal={renewal}")
            if not renewable:
                msg = f"⏭️ 而家唔可以續 (renewable=false, renewal={renewal})"
                log(f"  {msg}")
                results.append({**cur, "action": "skip", "tag": "未可續 (renewable=false)"})
                continue
            if DRY_RUN:
                msg = "dry-run: 唔真正續約"
                log(f"  {msg}")
                results.append({**cur, "action": "dry"})
                continue
            ok, msg, new_exp = renew_until_full(s, sid)
            at_cap = (not ok) and ("已達續期上限" in msg or "30 days" in msg)
            if ok and new_exp is not None:
                msg += f" | 新 renewal={new_exp}"
            if ok:
                results.append({"name": name, "expire": new_exp or renewal, "action": "renewed"})
            elif at_cap:
                results.append({"name": name, "expire": renewal, "action": "skip", "tag": "已滿 30 日"})
            else:
                results.append({"name": name, "action": "failed",
                                "detail": re.sub(r"^[❌⏭️]\s*", "", msg)[:120]})
    except Exception as e:
        log(f"💥 錯誤: {e}")
        send_tg(f"🔧 Host-Ship 續約異常: {e}")
        sys.exit(1)

    # 匯總 + 通知 (瘦身版)
    all_ok = all(r.get("action") != "failed" for r in results)
    summary = build_summary(results)
    log("\n" + summary)
    send_tg(summary)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
