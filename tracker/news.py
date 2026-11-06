"""단지별 공식 발표(Google 뉴스 RSS로 찾되 정부·지자체·공공기관 출처만 남김)와 그 속 수치 추출.
언론·블로그 기사는 근거로 쓰지 않는다(sources.py).

기사 제목·요약에서 '16.88대 1', '평균 1.02대 1', '3.3㎡당 1575만원' 같은 수치를 뽑아
출처별 값으로 모으고 stats.summarize로 통계량을 낸다. 청약홈 경쟁률 API 값이 있으면 '청약홈(공식)'으로 함께 넣는다.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from . import sources
from .stats import summarize
from .util import KST, now_kst_iso, today_kst

log = logging.getLogger(__name__)
RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

RATE_RE = re.compile(r"(평균|최고|최대)?\s*(?:경쟁률\s*)?(?:은|이|도)?\s*(\d{1,4}(?:\.\d{1,2})?)\s*(?:대|:)\s*1(?![\d.])")
PYEONG_RE = re.compile(r"(?:3\.3㎡|평)당\s*(?:평균\s*)?(?:약\s*)?([\d,]{3,6})\s*만\s*원")


def search_query(name: str) -> str:
    # 괄호 속 부가 설명(본청약·후속사업 등)은 검색을 좁히므로 뺀다
    base = re.sub(r"\(.*?\)", "", name).strip()
    return f"\"{base}\" {sources.OFFICIAL_SITES_QUERY} when:180d"


def fetch(name: str, session: requests.Session | None = None, limit: int = 8) -> list[dict]:
    return fetch_query(search_query(name), session, limit)


def fetch_query(query: str, session: requests.Session | None = None, limit: int = 8) -> list[dict]:
    s = session or requests.Session()
    r = s.get(RSS.format(q=quote(query)), timeout=20,
              headers={"User-Agent": "Mozilla/5.0 cheongyak-tracker/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "")
        src = it.findtext("source") or ""
        se = it.find("source")
        src_url = se.get("url") if se is not None else None
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            date = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).date().isoformat()
        except Exception:
            date = None
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        if not sources.is_official(src_url):      # 정부·지자체·공공기관 발표만 사용
            continue
        out.append({"title": title.strip(), "source": sources.org(src_url) or src, "date": date,
                    "url": it.findtext("link") or src_url, "source_url": src_url, "text": f"{title} {desc}"})
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out[:limit]


def extract(articles: list[dict]) -> dict[str, list[dict]]:
    metrics: dict[str, list[dict]] = {"rate_max": [], "rate_avg": [], "pyeong": []}
    seen = set()
    for a in articles:
        for m in RATE_RE.finditer(a["title"]):  # 경쟁률은 제목 기준(요약은 다른 단지 수치가 섞임)
            label, val = m.group(1), float(m.group(2))
            if val <= 0 or val > 3000:
                continue
            key = "rate_avg" if label == "평균" else "rate_max" if label in ("최고", "최대") else None
            if not key:
                continue
            k = (key, a["source"], val)
            if k not in seen:
                seen.add(k)
                metrics[key].append({"value": val, "source": a["source"], "url": a["url"], "source_url": a.get("source_url"), "official": True, "date": a["date"],
                                     "note": a["title"]})
        for m in PYEONG_RE.finditer(a["text"]):
            val = int(m.group(1).replace(",", ""))
            if 300 <= val <= 20000:
                k = ("pyeong", a["source"], val)
                if k not in seen:
                    seen.add(k)
                    metrics["pyeong"].append({"value": val, "source": a["source"], "url": a["url"], "source_url": a.get("source_url"), "official": True, "date": a["date"],
                                              "note": a["title"]})
    return metrics


LABELS = {"rate_max": ("1순위 최고 경쟁률", "대 1"), "rate_avg": ("1순위 평균 경쟁률", "대 1"),
          "pyeong": ("3.3㎡당 분양가", "만원")}


def cross_check(notice: dict, metrics: dict[str, list[dict]]) -> list[dict]:
    """공식 값(청약홈)과 기사 값을 합쳐 항목별 통계량."""
    comp = notice.get("competition") or {}
    official = {"rate_max": comp.get("rank1_max"), "rate_avg": comp.get("rank1_avg"),
                "pyeong": notice.get("pyeong_price")}
    out = []
    for key, (label, unit) in LABELS.items():
        vals = list(metrics.get(key, []))
        if official.get(key):
            vals.insert(0, {"value": official[key], "source": "청약홈(공식)", "url": notice.get("url"),
                            "date": None, "note": "API 집계값", "official": True})
        if vals:
            out.append({"key": key, "label": label, "unit": unit, "values": vals,
                        "stats": summarize(vals, tolerance=0.03)})
    return out


def enrich(items: list[dict], cache: dict, warnings: list[str], max_age_hours: int = 20,
           session: requests.Session | None = None) -> None:
    now = dt.datetime.now(KST)
    failed = 0
    for it in items:
        key = it["name"]
        c = cache.get(key)
        fresh = False
        if c and c.get("fetched_at"):
            try:
                fresh = now - dt.datetime.fromisoformat(c["fetched_at"]) < dt.timedelta(hours=max_age_hours)
            except ValueError:
                pass
        if not fresh and failed < 3:
            time.sleep(0.6)  # Google 뉴스 RSS 503 방지
            try:
                cache[key] = {"fetched_at": now_kst_iso(), "articles": fetch(key, session)}
            except Exception as e:
                failed += 1
                log.warning("뉴스 조회 실패 %s: %s", key, e)
        arts = (cache.get(key) or {}).get("articles", [])
        arts = [a for a in arts if sources.is_official(a.get("source_url"))]   # 예전 캐시의 언론 기사 제외
        it["news"] = [{k: a.get(k) for k in ("title", "source", "date", "url", "source_url")} for a in arts]
        it["crosscheck"] = cross_check(it, extract(arts))
    if failed >= 3:
        warnings.append("뉴스 RSS 조회가 여러 번 실패해 일부 공고는 지난 뉴스로 표시됩니다.")
