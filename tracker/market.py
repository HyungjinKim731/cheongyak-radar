"""주변 시세(연식별)·최근 상승폭 분석과 청약 후보 순위.

데이터
  1) 국토교통부 아파트 매매 실거래가 상세 API (data.go.kr 15126468, 같은 키로 활용신청)
     https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev
  2) data/comps.csv         직접 찾은 비교 단지(국토부 실거래가 공개시스템 등 공식 url만 사용)
  3) data/region_trends.csv 한국부동산원 등 공식 통계로 확인한 지역별 상승률과 출처 (공식 url만 사용)

계산 (모든 수치는 출처 목록과 함께 data.json에 남는다)
  기대 시세    = 같은 면적대 비교 거래의 중앙값 (실거래 API 신축 ≥3건이면 그것, 아니면 수동 비교값)
  시세 여유율  = (기대 시세 − 분양가) ÷ 분양가
  분양가 비교  = (주변 최근 분양가 중앙값 − 분양가) ÷ 분양가   (약한 근거, 가중치 0.5)
  투자 점수    = 시세 여유율 ÷ 묶이는 기간 (invest 참고). 최근 3개월 상승률은 참고 표시만
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
import statistics
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

from .stats import summarize
from . import sources
from .util import ROOT, clean, read_json, to_int, today_kst, write_json

log = logging.getLogger(__name__)

TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
REGION_CODE_URL = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
STATE = ROOT / "data" / "state"

SIGUNGU = {
    "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200", "광진구": "11215", "동대문구": "11230",
    "중랑구": "11260", "성북구": "11290", "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
    "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500", "구로구": "11530", "금천구": "11545",
    "영등포구": "11560", "동작구": "11590", "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710",
    "강동구": "11740",
    "수원시 장안구": "41111", "수원시 권선구": "41113", "수원시 팔달구": "41115", "수원시 영통구": "41117",
    "성남시 수정구": "41131", "성남시 중원구": "41133", "성남시 분당구": "41135", "의정부시": "41150",
    "안양시 만안구": "41171", "안양시 동안구": "41173", "부천시": "41190", "광명시": "41210", "평택시": "41220",
    "안산시 상록구": "41271", "안산시 단원구": "41273", "고양시 덕양구": "41281", "고양시 일산동구": "41285",
    "고양시 일산서구": "41287", "과천시": "41290", "구리시": "41310", "남양주시": "41360", "오산시": "41370",
    "시흥시": "41390", "군포시": "41410", "의왕시": "41430", "하남시": "41450", "용인시 처인구": "41461",
    "용인시 기흥구": "41463", "용인시 수지구": "41465", "파주시": "41480", "이천시": "41500", "김포시": "41570",
    "화성시": "41590",
    # 2026.2.1 화성특례시 일반구 출범 — 새 시군구 코드(실거래가 조회는 새 코드 기준)
    "화성시 만세구": "41591", "화성시 효행구": "41593", "화성시 병점구": "41595", "화성시 동탄구": "41597", "광주시": "41610", "양주시": "41630",
}
# 2025~2026년에 구가 새로 생긴 곳: 옛 코드와 새 구 코드를 함께 조회한다
SPLIT = {"화성시": ["화성시 만세구", "화성시 효행구", "화성시 병점구", "화성시 동탄구"],
         "부천시": ["부천시 원미구", "부천시 소사구", "부천시 오정구"]}
HWASEONG_TOWN = {"만세구": ["향남", "남양", "우정", "송산", "서신", "마도", "장안", "팔탄", "양감", "새솔"],
                 "효행구": ["봉담", "매송", "비봉"],
                 "병점구": ["병점", "진안", "반월", "기배", "화산", "능동"],
                 "동탄구": ["동탄", "오산동", "청계동", "영천동", "석우동", "반송동", "능동"]}


# ---------------------------------------------------------------- 주소 → 시군구
def parse_sigungu(address: str | None, name: str = "") -> str | None:
    s = f"{address or ''} {name}"
    m = re.search(r"(?:서울특별시|서울)\s*(\S+구)", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:경기도|경기)\s*(\S+시)\s*(\S+구)?", s)
    if m:
        city, gu = m.group(1), m.group(2)
        if gu and f"{city} {gu}" in SIGUNGU | {x: 1 for v in SPLIT.values() for x in v}:
            return f"{city} {gu}"
        if city == "화성시":
            for g, towns in HWASEONG_TOWN.items():
                if any(t in s for t in towns):
                    return f"화성시 {g}"
        return city
    return None


def region_chain(region: str | None, sigungu: str | None) -> list[str]:
    """지역 동향을 찾을 순서: 구 → 시 → 시도."""
    chain = []
    if sigungu:
        chain.append(sigungu)
        if " " in sigungu:
            chain.append(sigungu.split()[0])
    if region:
        chain.append(region)
    return chain


# ---------------------------------------------------------------- 실거래 API
class TradeClient:
    def __init__(self, key: str, session: requests.Session | None = None):
        self.key = key
        self.s = session or requests.Session()
        self.cache_dir = STATE / "trades"
        self.code_cache = read_json(STATE / "lawd_cache.json", {})

    def lawd_codes(self, sigungu: str) -> list[str]:
        city = sigungu.split()[0]
        codes = []
        if sigungu in SIGUNGU:
            codes.append(SIGUNGU[sigungu])
        if city in SPLIT:
            if SIGUNGU.get(city):
                codes.append(SIGUNGU[city])
            targets = [sigungu] if sigungu in SPLIT[city] else SPLIT[city]
            for t in targets:
                c = self._resolve(f"경기도 {t}")
                if c:
                    codes.append(c)
        elif not codes and city in SIGUNGU:
            codes.append(SIGUNGU[city])
        return list(dict.fromkeys(codes))

    def _resolve(self, full_name: str) -> str | None:
        if full_name in self.code_cache:
            return self.code_cache[full_name]
        try:
            r = self.s.get(REGION_CODE_URL, params={"serviceKey": self.key, "type": "json", "pageNo": 1,
                                                    "numOfRows": 5, "flag": "Y", "locatadd_nm": full_name}, timeout=20)
            rows = r.json()["StanReginCd"][1]["row"]
            code = str(rows[0]["region_cd"])[:5]
        except Exception as e:
            log.warning("법정동코드 조회 실패 %s: %s", full_name, e)
            code = None
        self.code_cache[full_name] = code
        write_json(STATE / "lawd_cache.json", self.code_cache)
        return code

    def month(self, lawd: str, ym: str) -> list[dict]:
        path = self.cache_dir / f"{lawd}_{ym}.json"
        recent = ym >= (today_kst().replace(day=1) - dt.timedelta(days=40)).strftime("%Y%m")
        if path.exists() and not recent:
            cached = read_json(path, [])
            if isinstance(cached, dict) and cached.get("v") == 2:   # 예전 캐시(지번 없음)는 다시 받음
                return cached["rows"]
        rows: list[dict] = []
        page = 1
        while True:
            for attempt in range(3):            # 공공데이터포털 초당 한도·일시 오류는 잠깐 쉬고 다시
                try:
                    time.sleep(0.06)
                    r = self.s.get(TRADE_URL, params={"serviceKey": self.key, "LAWD_CD": lawd, "DEAL_YMD": ym,
                                                      "pageNo": page, "numOfRows": 1000}, timeout=30)
                    items, total = _parse_xml(r.text)
                    break
                except (requests.RequestException, RuntimeError) as e:
                    if attempt == 2 or "SERVICE_KEY" in str(e) or "등록되지 않은" in str(e):
                        raise
                    time.sleep(1.5 * (attempt + 1))
            rows.extend(items)
            if len(rows) >= total or not items:
                break
            page += 1
        out = []
        for it in rows:
            if (it.get("cdealType") or "").strip() == "O":  # 해제된 거래 제외
                continue
            price = to_int(it.get("dealAmount"))
            area = _f(it.get("excluUseAr"))
            if not price or not area:
                continue
            out.append({"apt": clean(it.get("aptNm")), "dong": clean(it.get("umdNm")),
                        "jibun": clean(it.get("jibun")), "seq": clean(it.get("aptSeq")),
                        "deal": clean(it.get("dealingGbn")),          # 중개거래 / 직거래
                        "build_year": to_int(it.get("buildYear")), "area": area, "price": price,
                        "date": f"{it.get('dealYear')}-{int(it.get('dealMonth') or 0):02d}-{int(it.get('dealDay') or 0):02d}",
                        "floor": to_int(it.get("floor"))})
        write_json(path, {"v": 2, "rows": out}, compact=True)
        return out

    def recent(self, sigungu: str, months: int = 13) -> list[dict]:
        today = today_kst()
        yms = []
        y, m = today.year, today.month
        for _ in range(months):
            yms.append(f"{y}{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        out = []
        for code in self.lawd_codes(sigungu):
            for ym in yms:
                try:
                    out.extend(self.month(code, ym))
                except Exception as e:
                    log.warning("실거래 조회 실패 %s %s: %s", code, ym, e)
        return out


def _f(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_xml(text: str) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise RuntimeError(f"실거래 API 응답 오류: {text[:200]}")
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    if code and code not in ("00", "000"):
        raise RuntimeError(f"실거래 API {code}: {root.findtext('.//resultMsg') or root.findtext('.//returnAuthMsg')}")
    items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
    return items, int(root.findtext(".//totalCount") or 0)


# ---------------------------------------------------------------- 통계
def _q(vals: list[float], q: float) -> float:
    vals = sorted(vals)
    k = (len(vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def describe(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {"n": len(vals), "median": round(statistics.median(vals)), "mean": round(statistics.fmean(vals)),
            "p25": round(_q(vals, .25)), "p75": round(_q(vals, .75)), "min": round(min(vals)), "max": round(max(vals)),
            "stdev": round(statistics.stdev(vals)) if len(vals) > 1 else 0}


def age_group(build_year: int | None, this_year: int) -> str:
    if not build_year:
        return "연식 미상"
    age = this_year - build_year
    if age < 0:
        return "분양·입주 전"
    if age <= 5:
        return "신축(5년 이내)"
    if age <= 10:
        return "준신축(6~10년)"
    return "구축(10년 초과)"


AGE_ORDER = ["분양·입주 전", "신축(5년 이내)", "준신축(6~10년)", "구축(10년 초과)", "연식 미상"]


def trade_trends(trades: list[dict]) -> dict | None:
    """전용 40~135㎡ 거래의 ㎡당 가격 중앙값으로 월별 추이와 3개월·1년 변화율."""
    by_m: dict[str, list[float]] = {}
    for t in trades:
        if 40 <= t["area"] <= 135:
            by_m.setdefault(t["date"][:7], []).append(t["price"] / t["area"])
    if len(by_m) < 6:
        return None
    months = sorted(by_m)
    series = [{"month": m, "per_m2": round(statistics.median(by_m[m]), 1), "n": len(by_m[m])} for m in months]

    def med(ms):
        vals = [v for m in ms for v in by_m.get(m, [])]
        return statistics.median(vals) if vals else None
    last3, prev3 = months[-3:], months[-6:-3]
    last_year3 = [f"{int(m[:4]) - 1}{m[4:]}" for m in last3]
    a, b, c = med(last3), med(prev3), med(last_year3)
    return {"series": series,
            "chg_3m_pct": round((a / b - 1) * 100, 1) if a and b else None,
            "chg_1y_pct": round((a / c - 1) * 100, 1) if a and c else None,
            "n_trades": sum(len(v) for v in by_m.values())}


# ---------------------------------------------------------------- 수동 CSV
def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [{k: clean(v) for k, v in r.items() if k} for r in csv.DictReader(f)]
    # 정부·지자체·공공기관 자료만 근거로 사용(언론·블로그·민간 추정치는 제외)
    keep = [r for r in rows if sources.is_official(r.get("url"))]
    if len(keep) < len(rows):
        log.info("%s: 공식 출처가 아닌 %d줄은 제외", path.name, len(rows) - len(keep))
    return keep


MARKET_KINDS = {"실거래", "KB시세", "AI추정", "시세범위", "시세"}


# ---------------------------------------------------------------- 공고별 분석
RADII_KM = (1.0, 2.0, 3.0)      # 가까운 곳부터 넓혀 가며 비교 단지를 찾음
MIN_COMPLEXES = 3
MIN_TRADES_PER_COMPLEX = 2      # 거래 1건뿐인 단지는 비교에서 뺌
MIN_TRADES_TOTAL = 8            # 비교 거래가 이보다 적으면 근거 부족
MAX_SPREAD = 0.5                # 비교 단지 값의 퍼짐(범위 또는 사분위범위 ÷ 중앙값)이 이보다 크면 신뢰도 '낮음'(순위 제외)


def nearby(notice: dict, trades: list[dict], geo, band_area: float, this_year: int) -> dict:
    """공고 위치에서 실제 거리로 가까운 '10년 이내' 아파트 단지들의 최근 실거래로 기대 시세를 낸다.

    - 국토부 실거래가(공식)만 사용, 해제 거래·직거래 제외
    - 같은 면적대(전용 ±8㎡)의 전용㎡당 가격 × 공고 주택형 전용면적으로 환산
    - 단지마다 중앙값을 낸 뒤, 반경 1km → 2km → 3km 순으로 넓혀 단지 3곳 이상이 되는 첫 반경의 '단지 중앙값들의 중앙값'
    - 공고와 비교 단지 모두 번지·단지 단위 좌표가 있어야 함(동 단위 좌표는 거리 계산에 쓰지 않음)
    반환 {ok, reason, …}
    """
    from . import geo as geo_mod
    loc = geo.locate(notice["id"], notice.get("address"), notice.get("region"), notice.get("sigungu"), None)
    if loc and loc.get("precision") == "dong" and hasattr(geo, "keyword_near"):
        # 번지 없는 택지지구 블록 공고: 동 중심 근처에서 단지 이름으로 다시 찾음
        kw = geo.keyword_near(notice["id"], notice.get("name") or "", loc["lat"], loc["lng"])
        if kw:
            loc = kw
    if not loc or loc.get("precision") != "exact":
        return {"ok": False, "reason": "공고 위치를 번지·단지 단위로 못 찾음 (택지지구 블록 공고는 주소에 번지가 없는 경우가 많음)"}
    today = today_kst()
    out_n = 0
    for months in (6, 12):
        cutoff = (today - dt.timedelta(days=months * 31)).isoformat()
        pool = [t for t in trades if t["date"] >= cutoff and abs(t["area"] - band_area) <= 8
                and (t.get("build_year") or 0) >= this_year - 10 and t.get("deal") != "직거래"]
        if len({(t.get("dong"), t.get("apt")) for t in pool}) >= MIN_COMPLEXES:
            break
    cx: dict[tuple, list] = {}
    for t in pool:
        cx.setdefault((t.get("dong"), t.get("jibun") or "", t.get("apt")), []).append(t)
    rows = []
    for (dong, jibun, apt), ts in cx.items():
        addr = f"{notice.get('sigungu') or ''} {dong or ''} {jibun}".strip()
        g = geo.locate(f"cx:{notice.get('sigungu')}:{dong}:{jibun or apt}", addr if jibun else None,
                       notice.get("region"), notice.get("sigungu"), apt)
        if not g or g.get("precision") != "exact":
            out_n += 1
            continue
        km = geo_mod._km(loc["lat"], loc["lng"], g["lat"], g["lng"])
        per_m2 = statistics.median([t["price"] / t["area"] for t in ts])
        rows.append({"apt": apt, "dong": dong, "jibun": jibun, "build_year": ts[0].get("build_year"),
                     "km": round(km, 2), "n": len(ts), "value": round(per_m2 * band_area),
                     "min": min(t["price"] for t in ts), "max": max(t["price"] for t in ts),
                     "last": max(t["date"] for t in ts), "lat": g["lat"], "lng": g["lng"]})
    rows.sort(key=lambda r: r["km"])
    thin = [r for r in rows if r["n"] < MIN_TRADES_PER_COMPLEX]          # 거래 1건짜리 단지는 대표성 없음
    rows = [r for r in rows if r["n"] >= MIN_TRADES_PER_COMPLEX]
    for radius in RADII_KM:
        near = [r for r in rows if r["km"] <= radius]
        if len(near) >= MIN_COMPLEXES and sum(r["n"] for r in near) >= MIN_TRADES_TOTAL:
            vals = sorted(r["value"] for r in near)
            med = statistics.median(vals)
            if len(vals) >= 5:
                q = statistics.quantiles(vals, n=4)
                spread = (q[2] - q[0]) / med
            else:
                spread = (vals[-1] - vals[0]) / med
            return {"ok": True, "radius_km": radius, "months": months, "complexes": near,
                    "expected": round(med), "low": vals[0], "high": vals[-1], "spread": round(spread, 2),
                    "trades": sum(r["n"] for r in near), "unlocated": out_n, "thin": len(thin),
                    "basis": f"반경 {radius:g}km 안 10년 이내 {len(near)}개 단지, 최근 {months}개월 실거래 {sum(r['n'] for r in near)}건"}
    n3 = [r for r in rows if r["km"] <= 3]
    return {"ok": False, "reason": f"반경 3km 안 10년 이내 비교 단지(거래 2건 이상)가 {len(n3)}곳·거래 {sum(r['n'] for r in n3)}건뿐 — "
                                   f"단지 {MIN_COMPLEXES}곳·거래 {MIN_TRADES_TOTAL}건 이상 필요",
            "complexes": rows[:8], "unlocated": out_n, "thin": len(thin)}


def analyze(notice: dict, trades: list[dict] | None, comps: list[dict], trends: list[dict], cfg: dict,
            geo=None) -> dict:
    this_year = today_kst().year
    bands = notice.get("bands") or {}
    band = "84" if "84" in bands else ("59" if "59" in bands else (next(iter(bands), None)))
    mine = [c for c in comps if c.get("target") and c["target"] in notice["name"]]
    if not band and mine:
        band = mine[0].get("band")
    price_rep = None
    if band and band in bands:
        price_rep = bands[band]["median"]
    elif notice.get("price_min"):
        price_rep = (notice["price_min"] + notice["price_max"]) // 2

    out: dict[str, Any] = {"band": band, "price_rep": price_rep, "notes": []}

    # 토지임대부: 월 토지임대료를 연 수익률로 나눠 '땅값 상당액'을 실질 비용에 더한다
    for key, rents in (cfg.get("land_lease") or {}).items():
        if key in notice["name"] and band and str(band) in rents and price_rep:
            rent = float(rents[str(band)])
            land = round(rent * 12 / cfg.get("land_lease_yield", 0.04))
            out["land_equiv"] = land
            out["price_effective"] = price_rep + land
            out["notes"].append(f"토지임대부: 월 임대료 {rent:g}만원을 연 {cfg.get('land_lease_yield', 0.04) * 100:g}%로 "
                                f"환산한 {land:,}만원을 실질 비용에 더함")
    cost = out.get("price_effective") or price_rep

    # 1) 실거래 API 비교군
    groups: dict[str, dict] = {}
    if trades and band:
        b = float(band)
        cutoff = (today_kst() - dt.timedelta(days=365)).isoformat()
        same = [t for t in trades if abs(t["area"] - b) <= 6 and t["date"] >= cutoff]
        out["scope"] = "시군구"
        if notice.get("dong"):
            local = [t for t in same if t["dong"] == notice["dong"]]
            if len(local) >= 10:
                same = local
                out["scope"] = notice["dong"]
        for t in same:
            groups.setdefault(age_group(t["build_year"], this_year), []).append(t)
        out["api_groups"] = [{"group": g, "stats": describe([t["price"] for t in groups[g]]),
                              "examples": sorted(groups[g], key=lambda t: t["date"], reverse=True)[:3]}
                             for g in AGE_ORDER if g in groups]
        out["trade_trend"] = trade_trends(trades)

    # 2) 수동 비교값
    market_vals = [_comp_value(c) for c in mine if c.get("kind") in MARKET_KINDS and to_int(c.get("price_manwon"))]
    sale_vals = [_comp_value(c) for c in mine if c.get("kind") == "분양가" and to_int(c.get("price_manwon"))]
    for c in market_vals:
        c["group"] = age_group(to_int(c.get("build_year")), this_year)
    out["manual_market"] = {"values": market_vals, "stats": summarize(market_vals, tolerance=0.1) if market_vals else None}
    out["manual_newsale"] = {"values": sale_vals, "stats": summarize(sale_vals, tolerance=0.1) if sale_vals else None}

    # 기대 시세
    # 0) 거리 기반 비교(정확한 근거) — 이 값이 있을 때만 추천 순위에 들어감
    out["method"] = None
    if geo is not None and trades and band:
        b = bands.get(band) or {}
        band_area = ((b.get("area_min") or float(band)) + (b.get("area_max") or float(band))) / 2
        nb = nearby(notice, trades, geo, band_area, this_year)
        out["nearby"] = {k: v for k, v in nb.items() if k != "ok"}
        if nb["ok"]:
            out["method"] = "distance"

    # (참고용) 같은 동/시군구 연식별 비교. 한 단지 쏠림을 막으려 '단지별 중앙값'의 중앙값.
    expected, basis = None, None
    new_g = groups.get("신축(5년 이내)") or []
    semi_g = groups.get("준신축(6~10년)") or []

    def by_complex(ts):
        cx: dict[str, list] = {}
        for t in ts:
            cx.setdefault(t.get("apt") or "?", []).append(t["price"])
        return {k: statistics.median(v) for k, v in cx.items()}

    new_cx, ten_cx = by_complex(new_g), by_complex(new_g + semi_g)
    out["complexes"] = [{"apt": k, "median": round(v), "n": sum(1 for t in new_g + semi_g if (t.get("apt") or "?") == k)}
                        for k, v in sorted(ten_cx.items(), key=lambda kv: -kv[1])][:12]
    n_cx = 0
    scope = out.get("scope", "시군구")
    if len(new_cx) >= 3:
        expected, n_cx = statistics.median(new_cx.values()), len(new_cx)
        basis = f"{scope} 신축(5년 이내) {n_cx}개 단지 중앙값 (거래 {len(new_g)}건)"
    elif len(ten_cx) >= 2:
        expected, n_cx = statistics.median(ten_cx.values()), len(ten_cx)
        basis = f"{scope} 10년 이내 {n_cx}개 단지 중앙값 (신축 단지가 {len(new_cx)}곳뿐이라 준신축 포함)"
    elif len(new_g) >= 3:
        expected, n_cx = statistics.median([t["price"] for t in new_g]), 1
        basis = f"{scope} 신축 1개 단지 {len(new_g)}건 중앙값"
        out["notes"].append("비교 신축이 한 단지뿐이라 대표성이 낮음")
    elif market_vals:
        expected = statistics.median([c["value"] for c in market_vals])
        basis = f"비교 단지 {len(market_vals)}곳 중앙값"
        if all(c["group"] in ("구축(10년 초과)", "연식 미상") for c in market_vals):
            out["notes"].append("비교 단지가 구축·연식 미상뿐이라 신축 시세보다 낮게 잡혔을 수 있음")
    elif groups.get("준신축(6~10년)"):
        g = groups["준신축(6~10년)"]
        expected, basis = statistics.median([t["price"] for t in g]), f"실거래 준신축 {len(g)}건 중앙값(신축 부족)"
    if out["method"] == "distance":
        expected, basis = out["nearby"]["expected"], out["nearby"]["basis"]
    elif expected:
        out["method"] = "reference"
        out["notes"].append("거리 기반 비교가 안 돼 같은 동·시군구 참고값만 표시 — 추천 순위에서는 제외")
    out["expected"] = round(expected) if expected else None
    out["expected_basis"] = basis
    out["margin_pct"] = round((expected / cost - 1) * 100, 1) if expected and cost else None
    if sale_vals and cost:
        out["newsale_margin_pct"] = round((statistics.median([c["value"] for c in sale_vals]) / cost - 1) * 100, 1)

    # 3) 지역 동향 (구 → 시 → 시도 순으로 첫 매칭)
    chain = region_chain(notice.get("region"), notice.get("sigungu"))
    for reg in chain:
        rows = [r for r in trends if r.get("region") == reg]
        if rows:
            metrics = {}
            for r in rows:
                metrics.setdefault(r["metric"], []).append(
                    {"value": float(r["change_pct"]), "source": r["source"], "url": r["url"], "date": r["date"],
                     "note": r.get("note") or r.get("period")})
            out["region_trend"] = {"region": reg, "exact": reg == chain[0],
                                   "metrics": {k: {"values": v, "stats": summarize(v, tolerance=0.15)}
                                               for k, v in metrics.items()}}
            break

    # 4) 점수와 신뢰도
    momentum = None
    tt = out.get("trade_trend")
    if tt and tt.get("chg_3m_pct") is not None:
        momentum, mbasis = tt["chg_3m_pct"], "실거래 3개월"
    elif out.get("region_trend"):
        m = out["region_trend"]["metrics"]
        for k in ("3개월", "월간", "주간"):
            if k in m:
                val = m[k]["stats"]["median"]
                momentum = val if k == "3개월" else (val * 3 if k == "월간" else val * 13)
                mbasis = f"{out['region_trend']['region']} {k} 중앙값 {val:g}%" + ("×3" if k == "월간" else ("×13" if k == "주간" else ""))
                break
    if momentum is not None:
        out["momentum_3m_pct"] = round(momentum, 1)
        out["momentum_basis"] = mbasis
    margin = out.get("margin_pct")
    if margin is None and out.get("newsale_margin_pct") is not None:
        margin = out["newsale_margin_pct"] * 0.5
        out["notes"].append("주변 시세가 없어 인근 분양가 비교값을 절반 가중치로 사용")
    if margin is not None:
        out["score"] = round(margin + max(-5.0, min(5.0, momentum or 0.0)), 1)
    n_used = n_cx if n_cx else (len(market_vals) or len(sale_vals))
    year_ago = (today_kst() - dt.timedelta(days=365)).isoformat()
    stale_ratio = (sum((c.get("date") or "") < year_ago for c in market_vals) / len(market_vals)
                   if market_vals and len(new_g) < 3 else 0.0)
    if out.get("score") is None:
        out["confidence"] = "데이터 부족"
    elif out["method"] == "distance":
        nbh = out["nearby"]
        if nbh.get("spread", 0) > MAX_SPREAD:
            out["confidence"] = "낮음"
            out["notes"].append(f"비교 단지 값의 퍼짐이 {nbh['spread'] * 100:.0f}%로 커서(단지 규모·브랜드 차이 가능) 순위에서 뺌")
        else:
            out["confidence"] = ("높음" if nbh["radius_km"] <= 1.0 and len(nbh["complexes"]) >= 4 and nbh["trades"] >= 15
                                 else "보통" if nbh["radius_km"] <= 2.0 else "낮음")
    elif out["method"] == "reference":
        out["confidence"] = "참고용"
    elif n_used >= 4 and stale_ratio < 0.5 and out.get("scope", "시군구") != "시군구":
        out["confidence"] = "높음"
    elif n_used >= 2:
        out["confidence"] = "보통"
    else:
        out["confidence"] = "낮음"
    if stale_ratio >= 0.5:
        out["notes"].append(f"비교 거래 {round(stale_ratio * 100)}%가 1년 이상 지난 값")
    if out.get("scope") == "시군구" and n_cx:
        out["notes"].append("같은 동 거래가 적어 시·군·구 전체와 비교 — 입지 차이로 오차가 클 수 있음")
    if price_rep and band and band in bands:
        out["notes"].append("분양가는 주택형별 최고가(공고 기준) 중앙값이라 저층·비선호 타입은 더 쌉니다")
    return out


def _comp_value(c: dict) -> dict:
    return {"value": to_int(c["price_manwon"]), "source": f"{c.get('comp_name')} ({c.get('source')})", "url": c.get("url"),
            "date": c.get("date"), "kind": c.get("kind"), "build_year": c.get("build_year"), "area": c.get("area"),
            "note": c.get("note")}


DEFAULT_YEARS_TO_MOVE_IN = 2.5   # 입주예정월이 없을 때 가정(일반적인 착공 후 분양 → 입주 기간)


TAX_HOLD_YEARS = 2.0   # 주택은 취득(잔금) 후 2년 안에 팔면 양도세율 60~70%(소득세법 제104조) → 최소 2년 보유 가정


def invest(s: dict) -> dict | None:
    """투자 관점 지표 — 차익을 '세금 폭탄 없이' 손에 쥘 수 있을 때까지 묶이는 기간으로 비교한다.

    보유 기간  = max(실거주의무, 2년)                       ← 입주(잔금) 뒤 최소 보유. 2년 미만 매도는 양도세 60~70%
    묶이는 기간 = max(전매제한, 입주까지 남은 기간 + 보유 기간)  ← 전매제한은 당첨일부터, 보유는 입주부터 셈
    투자 점수   = 시세 여유율 ÷ 묶이는 기간   (시세 여유율이 0 이하이면 나누지 않고 그대로 — 손실을 기간으로 희석하지 않음)
    최근 상승률은 점수에 넣지 않고 참고로만 표시(동네 거래 구성에 따라 출렁여 신뢰도가 낮음).
    """
    m = s.get("market") or {}
    r = s.get("restrict") or {}
    s.pop("invest_reason", None)
    if m.get("method") != "distance":       # 실측(거리 기반) 근거가 없으면 순위에 넣지 않음
        return None
    if any(k in (s.get("name") or "") for k in ("토지임대부", "지분적립")):
        s["invest_reason"] = "토지임대부·지분적립형은 땅·지분을 다 갖지 않아 주변 아파트 시세와 같은 선에서 비교할 수 없음"
        return None
    if m.get("confidence") not in ("높음", "보통"):
        s["invest_reason"] = "비교 근거가 약함(신뢰도 낮음): " + "; ".join(n for n in m.get("notes", []) if "퍼짐" in n or "반경" in n)[:120]
        return None
    margin = m.get("margin_pct")
    if margin is None:
        return None
    today = today_kst()
    ytm, ytm_basis = DEFAULT_YEARS_TO_MOVE_IN, "입주예정월 없음 → 2.5년 가정"
    mv = re.sub(r"\D", "", str(s.get("move_in") or ""))
    if len(mv) >= 6:
        y, mo = int(mv[:4]), int(mv[4:6])
        ytm = max(0.0, (y - today.year) + (mo - today.month) / 12)
        ytm_basis = f"입주예정 {y}.{mo:02d}"
    resale = r.get("resale_years") or 0.0
    reside = r.get("reside_max") or 0.0
    hold = max(reside, TAX_HOLD_YEARS)
    lock = round(max(resale, ytm + hold), 1)
    out = {
        "margin_pct": round(margin, 1),
        "lock_years": lock, "years_to_move_in": round(ytm, 1), "move_in_basis": ytm_basis,
        "resale_years": resale, "reside_years": reside, "hold_years": hold,
        "lock_formula": f"max(전매 {resale:g}년, 입주까지 {ytm:.1f}년 + 보유 {hold:g}년{'(실거주의무)' if reside >= TAX_HOLD_YEARS else '(양도세 2년)'})",
        "score": round(margin / lock, 1) if margin > 0 else round(margin, 1),
        "momentum_3m_pct": m.get("momentum_3m_pct"),
    }
    if m.get("expected") and m.get("price_rep"):
        out["gain_manwon"] = round(m["expected"] - m["price_rep"])
        out["down_payment"] = [round(m["price_rep"] * 0.1), round(m["price_rep"] * 0.2)]
    return out


def rank(schedules: list[dict]) -> list[dict]:
    """아직 신청할 수 있는 공고만 투자 점수(연 환산) 순으로."""
    today = today_kst().isoformat()
    open_ = [s for s in schedules if any(e["end"] >= today and e["label"] not in ("당첨자 발표", "계약")
                                         for e in s.get("events", []))]
    for s in open_:
        s["invest"] = invest(s)
    scored = [s for s in open_ if s.get("invest")]
    rest = [s for s in open_ if not s.get("invest")]
    scored.sort(key=lambda s: (-s["invest"]["score"], -(s["invest"].get("gain_manwon") or 0)))
    return ([{"id": s["id"], "name": s["name"], "score": s["invest"]["score"], "confidence": s["market"].get("confidence"),
              "margin_pct": s["invest"]["margin_pct"], "momentum_3m_pct": s["market"].get("momentum_3m_pct"),
              "gain_manwon": s["invest"].get("gain_manwon"), "lock_years": s["invest"]["lock_years"],
              "value_score": s["market"].get("score")}
             for s in scored]
            + [{"id": s["id"], "name": s["name"], "score": None, "confidence": "데이터 부족",
                "reason": s.get("invest_reason") or ((s.get("market") or {}).get("nearby") or {}).get("reason")
                          or "주변 실거래 근거 부족"} for s in rest])


def enrich(schedules: list[dict], key: str | None, cfg: dict, warnings: list[str], geo=None) -> None:
    comps = load_csv(ROOT / "data" / "comps.csv")
    trends = load_csv(ROOT / "data" / "region_trends.csv")
    client = TradeClient(key) if key and cfg.get("fetch_trades", True) else None
    trade_cache: dict[str, list[dict]] = {}
    api_failed = False
    for s in schedules:
        s["sigungu"] = parse_sigungu(s.get("address"), s.get("name"))
        m = re.search(r"(\S+[동읍면리])(?:\s|$|\d)", s.get("address") or "")
        s["dong"] = m.group(1) if m else None
        trades = None
        if client and s["sigungu"] and not api_failed:
            if s["sigungu"] not in trade_cache:
                try:
                    trade_cache[s["sigungu"]] = client.recent(s["sigungu"])
                except Exception as e:
                    api_failed = True
                    warnings.append(f"실거래가 API 실패({e}) — data.go.kr에서 '아파트 매매 실거래가 상세 자료' 활용신청을 확인하세요.")
            trades = trade_cache.get(s["sigungu"])
        s["market"] = analyze(s, trades, comps, trends, cfg, geo)
