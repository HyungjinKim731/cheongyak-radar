"""포트폴리오 정리 트래커 — 시세 조회·매도 기록 (개인 정보라 로컬 전용).

보유 정보: portfolio.local.json (gitignore)
시세 캐시: data/private/quotes.json (gitignore)
시세: Yahoo Finance 차트 API (키 불필요, 비공식). 실패하면 마지막 캐시를 그대로 씀.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

import requests

from .util import KST, ROOT, now_kst_iso, read_json, write_json

log = logging.getLogger(__name__)
FILE = ROOT / "portfolio.local.json"
QUOTES = ROOT / "data" / "private" / "quotes.json"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_lock = threading.Lock()


def load() -> dict | None:
    return read_json(FILE, None) if FILE.exists() else None


def save(doc: dict) -> None:
    with _lock:
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(FILE)


def _quote(session: requests.Session, sym: str) -> dict:
    r = session.get(CHART.format(sym=sym), params={"range": "3mo", "interval": "1d"}, timeout=12)
    js = r.json()["chart"]
    if not js.get("result"):
        raise RuntimeError((js.get("error") or {}).get("description") or "결과 없음")
    res = js["result"][0]
    meta = res["meta"]
    raw = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    ts = res.get("timestamp") or []
    off = dt.timedelta(seconds=meta.get("gmtoffset") or 0)        # 거래소 현지 날짜로 비교
    day = lambda t: (dt.datetime.fromtimestamp(t, dt.timezone.utc) + off).date()
    pairs = [(day(t), c) for t, c in zip(ts, raw) if c is not None]
    closes = [c for _, c in pairs]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    # 전일 종가 = 시세 시각(거래소 날짜)보다 앞선 날의 마지막 종가
    prev = None
    if pairs and meta.get("regularMarketTime"):
        mday = day(meta["regularMarketTime"])
        before = [c for d, c in pairs if d < mday]
        prev = before[-1] if before else None
    return {
        "price": price, "prev": prev,
        "chg_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
        "currency": meta.get("currency"),
        "name": meta.get("longName") or meta.get("shortName"),
        "market_time": dt.datetime.fromtimestamp(meta["regularMarketTime"], KST).isoformat(timespec="minutes") if meta.get("regularMarketTime") else None,
        "spark": [round(c, 4) for c in closes[-60:]],
        "high_3m": round(max(closes + ([price] if price else [])), 4) if closes else None,
        "low_3m": round(min(closes + ([price] if price else [])), 4) if closes else None,
    }


def refresh_quotes(doc: dict | None = None, max_age_s: int = 60) -> dict:
    doc = doc or load() or {"holdings": []}
    cache = read_json(QUOTES, {"quotes": {}, "fetched_at": None})
    if cache.get("fetched_at"):
        try:
            age = (dt.datetime.now(KST) - dt.datetime.fromisoformat(cache["fetched_at"])).total_seconds()
            if age < max_age_s:
                return cache
        except ValueError:
            pass
    syms = [h["ticker"] for h in doc.get("holdings", [])] + ["USDKRW=X"]
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (cheongyak-tracker personal)"
    errors = {}
    for sym in dict.fromkeys(syms):
        try:
            cache["quotes"][sym] = _quote(s, sym) | {"ok": True}
        except Exception as e:
            errors[sym] = str(e)[:120]
            if sym in cache["quotes"]:
                cache["quotes"][sym]["ok"] = False
        time.sleep(0.15)
    cache["fetched_at"] = now_kst_iso()
    cache["errors"] = errors
    QUOTES.parent.mkdir(parents=True, exist_ok=True)
    write_json(QUOTES, cache, compact=True)
    return cache


def bundle(max_age_s: int = 60) -> dict | None:
    doc = load()
    if doc is None:
        return None
    try:
        q = refresh_quotes(doc, max_age_s)
    except Exception as e:
        q = read_json(QUOTES, {"quotes": {}}) | {"errors": {"_": str(e)}}
    return {"portfolio": doc, "quotes": q, "server_time": now_kst_iso()}


def add_trade(t: dict) -> dict:
    """매도(또는 매수) 기록 추가. 보유 수량을 알면 자동으로 줄인다."""
    doc = load()
    if doc is None:
        raise ValueError("portfolio.local.json 없음")
    tk = str(t.get("ticker") or "")
    h = next((x for x in doc["holdings"] if x["ticker"] == tk), None)
    if not h:
        raise ValueError(f"종목 없음: {tk}")
    qty = float(t["qty"])
    price = float(t["price"])
    side = t.get("side") or "sell"
    if qty <= 0 or price <= 0:
        raise ValueError("수량·가격은 0보다 커야 함")
    fx = t.get("fx")
    if fx in (None, "") and h.get("currency") == "USD":
        fx = (read_json(QUOTES, {"quotes": {}})["quotes"].get("USDKRW=X") or {}).get("price")
    rec = {"id": f"{int(time.time() * 1000)}", "date": t.get("date") or now_kst_iso()[:10], "ticker": tk, "side": side,
           "qty": qty, "price": price, "avg": h.get("avg"), "fee": float(t.get("fee") or 0),
           "fx": float(fx) if fx not in (None, "") else None, "memo": (t.get("memo") or "")[:200]}
    if side == "sell" and h.get("shares") is not None:
        h["shares"] = max(0.0, round(float(h["shares"]) - qty, 6))
    if side == "buy" and h.get("shares") is not None:
        tot = float(h["shares"]) + qty
        h["avg"] = round((float(h["avg"]) * float(h["shares"]) + price * qty) / tot, 4)
        h["shares"] = tot
    doc.setdefault("trades", []).append(rec)
    save(doc)
    return rec


def delete_trade(trade_id: str) -> bool:
    """기록만 지움(보유 수량은 자동 복원하지 않음 — 화면에서 수량을 직접 고침)."""
    doc = load()
    if not doc:
        return False
    n = len(doc.get("trades", []))
    doc["trades"] = [t for t in doc.get("trades", []) if t.get("id") != trade_id]
    save(doc)
    return len(doc["trades"]) < n
