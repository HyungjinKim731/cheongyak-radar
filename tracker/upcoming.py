"""'곧 나올 분양' 통합: 공고 전 분양예정 + 정비사업 막바지 구역 + 분양 예정 기사.

- planned: data/watchlist.csv 의 stage=분양예정 항목(기사·LH 계획 등). 청약홈 공고가 뜨면 이름으로 짝지어 '공고 나옴'으로 표시
- candidates: 서울 정비사업 중 관리처분 또는 최근 착공 구역(아직 공고와 짝이 없는 것) — 일반분양이 가까운 후보
- news: 정부·지자체·LH/SH/GH 등 공식 발표 중 분양·입주자모집 관련 글 (매 갱신)
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import requests

from . import news as news_mod
from . import sources
from . import rules
from .util import today_kst

log = logging.getLogger(__name__)


def norm(s: str | None) -> str:
    s = re.sub(r"\(.*?\)", "", s or "")
    s = re.sub(r"[\s·,\-_/]|아파트|단지$", "", s)
    return s.lower()


def match_notice(name: str, schedules: list[dict]) -> dict | None:
    a = norm(name)
    if len(a) < 3:
        return None
    for s in schedules:
        b = norm(s["name"])
        if a in b or b in a or (len(a) >= 5 and a[:5] in b):
            return s
    return None


def expected_key(text: str | None) -> str:
    """'2026년 11월' → '2026-11', '2026년' → '2026-12', 없으면 '9999'."""
    t = text or ""
    m = re.search(r"(20\d\d)\D{0,3}(\d{1,2})\s*월", t) or re.search(r"(20\d\d)-(\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d\d)\s*년?\s*(상반기|하반기)?", t)
    if m:
        return f"{m.group(1)}-{'06' if m.group(2) == '상반기' else '12'}"
    return "9999"


def planned(pipeline: list[dict], schedules: list[dict]) -> list[dict]:
    out = []
    for p in pipeline:
        if p.get("source") == "seoul_xlsx" and not p.get("expected"):
            continue
        if p.get("stage") != "분양예정" and not p.get("expected"):
            continue
        it = {k: p.get(k) for k in ("id", "name", "region", "sigungu", "type", "stage", "households", "general_supply",
                                    "expected", "builder", "source_url", "basis", "memo", "address", "lat", "lng", "geo")}
        it["expected_key"] = expected_key(p.get("expected"))
        it["regulation"] = rules.regulation({"region": p.get("region"), "sigungu": p.get("sigungu")})[0]
        m = match_notice(p["name"], schedules) or (match_notice(p.get("memo") or "", schedules) if p.get("memo") else None)
        if m:
            it["notice_id"] = m["id"]
            it["notice_name"] = m["name"]
        out.append(it)
    out.sort(key=lambda x: (x["expected_key"], -(x.get("general_supply") or x.get("households") or 0)))
    return out


def candidates(pipeline: list[dict], schedules: list[dict], planned_ids: set[str], limit: int = 60) -> list[dict]:
    today = today_kst()
    cut = (today - dt.timedelta(days=730)).isoformat()
    out = []
    for p in pipeline:
        if p.get("source") != "seoul_xlsx" or p["id"] in planned_ids:
            continue
        ms = p.get("milestones") or {}
        start = ms.get("d_start")
        if p.get("stage") == "관리처분" or (p.get("stage") == "착공" and start and start >= cut):
            if match_notice(p["name"], schedules):
                continue
            mg = ms.get("d_mgmt2") or ms.get("d_mgmt")
            if p["stage"] == "착공":
                est = "착공 후 — 선분양이면 곧 공고(이미 분양했을 수도 있음)"
            elif mg:
                yrs = (today - dt.date.fromisoformat(mg)).days / 365
                est = f"관리처분 {mg} ({yrs:.1f}년 전) — 보통 이주·철거 후 1~3년 안에 일반분양"
            else:
                est = "관리처분 단계"
            out.append({k: p.get(k) for k in ("id", "name", "region", "sigungu", "type", "type_detail", "stage", "households",
                                                "sale_households", "address", "lat", "lng", "geo", "watched", "memo")}
                       | {"estimate": est, "d_start": start, "d_mgmt": mg})
    out.sort(key=lambda x: (0 if x["stage"] == "착공" else 1, -(x.get("sale_households") or 0)))
    return out[:limit]


def news_feed(session: requests.Session | None = None) -> list[dict]:
    today = today_kst()
    months = [today.month, today.month % 12 + 1]
    seen, out = set(), []
    for m in months:
        for q in (f"{m}월 분양 공고", f"{m}월 입주자모집", f"{m}월 공공분양"):
            try:
                arts = news_mod.fetch_query(f"{q} {sources.OFFICIAL_SITES_QUERY} when:45d", session, limit=10)
            except Exception as e:
                log.warning("분양 예정 기사 조회 실패 %s: %s", q, e)
                continue
            for a in arts:
                if a["title"] in seen or not any(k in a["title"] for k in ("분양", "입주자", "청약", "공급")):
                    continue
                seen.add(a["title"])
                out.append({k: a.get(k) for k in ("title", "source", "date", "url", "source_url")})
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:20]


def build(pipeline: list[dict], schedules: list[dict], with_news: bool = True) -> dict:
    pl = planned(pipeline, schedules)
    ids = {p["id"] for p in pl}
    return {"planned": pl, "candidates": candidates(pipeline, schedules, ids),
            "news": news_feed() if with_news else []}
