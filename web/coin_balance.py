"""Account-scoped coin snapshots, adjusted only by recorded coin spending."""
from __future__ import annotations

import math
import threading
import time

import httpx

from src.bilibili_auth import get_login_uid
from src.bilibili_client import BilibiliClient
from src.participation_log import load_action_entries_for_uid
from web.account_service import NAV_URL

REFRESH_SECONDS = 12 * 3600
RETRY_SECONDS = 900
_coin_lock = threading.Lock()


def confirmed_spending(uid, since):
    total = 0
    if not since:
        return total
    for entry in load_action_entries_for_uid(uid):
        if entry["recorded_at"] <= since or entry["status"] == "dry_run":
            continue
        for action in entry.get("actions") or []:
            count = action.get("coins_used")
            if action.get("action") == "coin" and action.get("ok") is True and type(count) is int and count > 0:
                total += count
    return total


def coin_balance(store, *, refresh_after_coin=None):
    uid = get_login_uid()
    if not uid:
        return {"balance": None, "checked_at": None, "estimated": False, "refresh_after_coin": False}
    uid = str(uid)
    with _coin_lock:
        record = store.get("coin_balance", uid) or {}
        if refresh_after_coin is not None:
            record["refresh_after_coin"] = refresh_after_coin
            store.put("coin_balance", uid, record)
        now = int(time.time())
        checked = record.get("checked_at") or 0
        spent = confirmed_spending(uid, checked)
        due = now - checked >= REFRESH_SECONDS or (record.get("refresh_after_coin") and spent > 0)
        if due and now - record.get("failed_at", 0) >= RETRY_SECONDS:
            record["failed_at"] = now
            try:
                with BilibiliClient(timeout=8, warmup=False) as client:
                    payload = client.request_json(NAV_URL, referer="https://www.bilibili.com", retries=0)
                data = payload.get("data") or {}
                amount = data.get("money")
                if (payload.get("code") == 0 and data.get("isLogin") and str(data.get("mid")) == uid
                        and type(amount) in (int, float) and math.isfinite(amount) and amount >= 0):
                    record.update(balance=amount, checked_at=now, failed_at=0)
                    checked = now
                    spent = 0
            except (httpx.HTTPError, RuntimeError, ValueError):
                pass
            store.put("coin_balance", uid, record)
        balance = record.get("balance")
        return {"balance": None if balance is None else max(0, balance - spent), "checked_at": checked or None,
                "estimated": bool(spent or (checked and now - checked >= REFRESH_SECONDS)),
                "refresh_after_coin": bool(record.get("refresh_after_coin"))}
