#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-Ship (Jexactyl/Pterodactyl 系) 自动续约 —— 已迁移到 renew-kit。

公共部分（重试 / 结果分类 / Telegram / 报告排版）交给 renewkit，
本文件只保留 Host-Ship 的业务逻辑：CSRF 登录 + 续期补点。

迁移带来的行为变化：
    · 面板 5xx / 超时 / 连不上 -> 自动退避重试（5s/15s/30s），
      重试耗尽记为 TRANSIENT 并 exit 0，不再把上游故障当脚本失败。
    · 4xx 不重试（确定性错误，重试没意义）。
    · 通知失败不再影响退出码。

用法（环境变量）：
    PANEL_URL / PANEL_USER / PANEL_PASS / SERVER_IDS
    TG_BOT_TOKEN / TG_CHAT_ID
    DRY_RUN=1 时只检查不续期
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse

from renewkit import Outcome, RenewReport
from renewkit import env
from renewkit.http import TransientError, build_session, request
from renewkit.timeutil import days_left

PANEL = env.get("PANEL_URL", "https://panel.host-ship.com").rstrip("/")
USER = env.get("PANEL_USER")
PASS = env.get("PANEL_PASS")
SERVER_IDS = env.get_list("SERVER_IDS", default="3dee8360")
DRY_RUN = env.dry_run()

# 续期补点：面板一次 POST 约 +7 日、总上限 30 日
TOPUP_MAX_EXTRA = env.get_int("TOPUP_MAX_EXTRA", 3)
TOPUP_INTERVAL_S = env.get_int("TOPUP_INTERVAL_S", 10)
TOPUP_GOAL_DAYS = env.get_int("TOPUP_GOAL_DAYS", 28)

SERVICE = "Host-Ship"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 登录

def login():
    """sanctum/csrf-cookie → POST /auth/login（带 URL-decode 后的 X-XSRF-TOKEN）。"""
    s = build_session()
    r = request(s, "GET", f"{PANEL}/sanctum/csrf-cookie")
    if r.status_code not in (200, 204):
        raise RuntimeError(f"sanctum/csrf-cookie → HTTP {r.status_code}")

    xsrf = urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", ""))
    if not xsrf:
        raise RuntimeError("没有拿到 XSRF-TOKEN")

    r = request(s, "POST", f"{PANEL}/auth/login",
                json={"user": USER, "password": PASS},
                headers={"X-XSRF-TOKEN": xsrf, "Referer": f"{PANEL}/auth/login"},
                allow_redirects=False)
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 → HTTP {r.status_code}: {(r.text or '')[:200]}")

    data = (r.json().get("data") or {}) if r.text else {}
    if not data.get("complete") and not data.get("user"):
        raise RuntimeError(f"登录未完成: {str(data)[:200]}")

    request(s, "GET", f"{PANEL}/")           # 走一趟首页，拿齐 session cookie
    s.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", "") or xsrf)
    log(f"✅ 登录成功: {(data.get('user') or {}).get('username', '')}")
    return s


# ---------------------------------------------------------------- 续期

def get_server(s, server_id: str):
    r = request(s, "GET", f"{PANEL}/api/client/servers/{server_id}")
    if r.status_code != 200:
        return None, f"GET server → HTTP {r.status_code}: {(r.text or '')[:150]}"
    return (r.json().get("attributes") or {}), ""


def renew_server(s, server_id: str) -> tuple[Outcome, str]:
    """发一次续期请求，返回 (Outcome, 说明)。"""
    r = request(s, "POST", f"{PANEL}/api/client/servers/{server_id}/renew",
                headers={"Referer": f"{PANEL}/server/{server_id}",
                         "Content-Type": "application/json"})
    if r.status_code in (200, 204):
        return Outcome.RENEWED, f"✅ 续约成功 (HTTP {r.status_code})"

    detail = ""
    try:
        detail = json.loads((r.text or "").strip())["errors"][0]["detail"]
    except Exception:
        detail = (r.text or "").strip()[:200]

    # 已达上限 30 日 → 不是失败，是「已满」状态
    if r.status_code in (400, 422) and "cannot add more than 30 days" in detail.lower():
        return Outcome.ALREADY_MAX, f"⏭️ 已达续期上限 (30 日)，无需再续 (HTTP {r.status_code})"
    return Outcome.FAILED, f"❌ 续约失败 (HTTP {r.status_code}): {detail}"


def read_renewal(s, server_id: str, delay: float = 2):
    """隔 delay 秒读回 renewal（读不到返回 None）。"""
    time.sleep(delay)
    attrs, err = get_server(s, server_id)
    if not attrs:
        log(f"  ⚠️ 续后读取失败: {err}")
        return None
    log(f"  续后 renewal: {attrs.get('renewal')} | renewable: {attrs.get('renewable')}")
    return attrs.get("renewal")


def renew_until_full(s, server_id: str) -> tuple[Outcome, str, object]:
    """首次 POST（含面板 CD 重试），之后剩余天数不够就补点。"""
    outcome, msg = renew_server(s, server_id)
    log(f"  {msg}")

    # 面板 CD: "You can renew again in N seconds"
    for _ in range(3):
        m = re.search(r"renew again in (\d+) seconds", msg, re.IGNORECASE)
        if outcome is Outcome.RENEWED or not m:
            break
        wait = min(int(m.group(1)) + 3, 300)
        log(f"  ⏳ 面板 CD: 等 {wait} 秒再重试...")
        time.sleep(wait)
        outcome, msg = renew_server(s, server_id)
        log(f"  {msg}")

    if outcome is not Outcome.RENEWED:
        return outcome, msg, None

    new_exp = read_renewal(s, server_id)
    extra, prev_left = 0, None
    while True:
        left = days_left(new_exp)
        if left is None:
            log("  ⏭️ 剩余天数读不到，不补点")
            break
        if left >= TOPUP_GOAL_DAYS:
            log(f"  ✅ 剩 {left} 日，已近面板上限 (30 日)")
            break
        if prev_left is not None and left <= prev_left:
            log(f"  ⏭️ 补点后无进展 ({prev_left} → {left})，停")
            break
        if extra >= TOPUP_MAX_EXTRA:
            log(f"  ⏭️ 已补 {extra} 次，停 (剩 {left} 日)")
            break

        extra += 1
        prev_left = left
        time.sleep(TOPUP_INTERVAL_S)
        o2, msg2 = renew_server(s, server_id)
        log(f"  🔁 补点 {extra}/{TOPUP_MAX_EXTRA}: {msg2}")
        if o2 is Outcome.ALREADY_MAX:
            log("  ✅ 面板报已满，当成功")
            break
        if o2 is not Outcome.RENEWED:
            log(f"  ⚠️ 补点失败，收手: {(msg2 or '')[:120]}")
            break
        again = read_renewal(s, server_id)
        if again is not None:
            new_exp = again

    return Outcome.RENEWED, msg, new_exp


# ---------------------------------------------------------------- 主流程

def main() -> int:
    if not USER or not PASS:
        report = RenewReport(SERVICE)
        report.add("凭证", Outcome.FAILED, detail="缺少 PANEL_USER / PANEL_PASS")
        return report.finish()

    log(f"🚀 {SERVICE} 续约 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"面板: {PANEL} | 服务器: {SERVER_IDS} | dry_run={DRY_RUN}")

    report = RenewReport(SERVICE)

    try:
        session = login()
    except TransientError as exc:
        report.add("登录", Outcome.TRANSIENT, detail=str(exc))
        return report.finish()
    except Exception as exc:
        report.add("登录", Outcome.FAILED, detail=str(exc)[:150])
        return report.finish()

    for sid in SERVER_IDS:
        log(f"── 服务器 {sid} ──")
        try:
            attrs, err = get_server(session, sid)
        except TransientError as exc:
            report.add(sid, Outcome.TRANSIENT, detail=str(exc))
            continue

        if err:
            log(f"  {err}")
            report.add(sid, Outcome.FAILED, detail=err[:150])
            continue

        name = attrs.get("name", sid)
        renewal = attrs.get("renewal")
        renewable = attrs.get("renewable")
        log(f"  名称: {name} | renewable={renewable} | renewal={renewal}")

        if not renewable:
            report.add(name, Outcome.SKIPPED, expire=renewal, detail="暂不可续 (renewable=false)")
            continue

        if DRY_RUN:
            report.add(name, Outcome.SKIPPED, expire=renewal, detail="dry-run：未真正续约")
            continue

        try:
            outcome, msg, new_exp = renew_until_full(session, sid)
        except TransientError as exc:
            report.add(name, Outcome.TRANSIENT, expire=renewal, detail=str(exc))
            continue

        if outcome is Outcome.RENEWED:
            log(f"  {msg} | 新 renewal={new_exp}")
            report.add(name, Outcome.RENEWED, expire=new_exp or renewal)
        elif outcome is Outcome.ALREADY_MAX:
            report.add(name, Outcome.ALREADY_MAX, expire=renewal)
        else:
            clean = re.sub(r"^[❌⏭️]\s*", "", msg or "")[:120]
            report.add(name, Outcome.FAILED, expire=renewal, detail=clean)

    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
