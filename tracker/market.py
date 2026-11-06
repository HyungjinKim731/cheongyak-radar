"""주변 시세(연식별)·최근 상승폭 분석과 청약 후보 순위.

데이터
  1) 국토교통부 아파트 매매 실거래가 상세 API (data.go.kr 15126469, 같은 키로 활용신청)
     https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev
  2) data/comps.csv         직접 찾은 비교 단지(국토부 실거래가 공개시스템 등 공식 url만 사용)
  3) data/region_trends.csv 한국부동산원 등 공식 통계로 확인한 지역별 상승률과 출처 (공식 url만 사용)

계산 (모든 수치는 출처 목록과 함께 data.json에 남는다)
  기대 시세    = 같은 면적대 비교 거래의 중앙값 (실거래 API 신축 ≥3건이면 그것, 아니면 수동 비교값)
  시세 여유율  = (기대 시세 − 분양가) ÷ 분양가
  분양가 비교  = (주변 최근 분양가 중앙값 − 분양가) ÷ 분양가   (약한 근거, 가중치 0.5)
  가치 점수    = 여유율(%) + 최근 3개월 지역 상승률(±5%p로 제한)
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
import statistics
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
    "화성시": "41590", "광주시": "41610", "양주시": "41630",
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
            return read_json(path, [])
        rows: list[dict] = []
        page = 1
        while True:
            r = self.s.get(TRADE_URL, params={"serviceKey": self.key, "LAWD_CD": lawd, "DEAL_YMD": ym,
                                              "pageNo": page, "numOfRows": 1000}, timeout=30)
            items, total = _parse_xml(r.text)
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
                        "build_year": to_int(it.get("buildYear")), "area": area, "price": price,
                        "date": f"{it.get('dealYear')}-{int(it.get('dealMonth') or 0):02d}-{int(it.get('dealDay') or 0):02d}",
                        "floor": to_int(it.get("floor"))})
        write_json(path, out, compact=True)
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
def analyze(notice: dict, trades: list[dict] | None, comps: list[dict], trends: list[dict], cfg: dict) -> dict:
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
        if notice.get("dong"):
            local = [t for t in same if t["dong"] == notice["dong"]]
            if len(local) >= 10:
                same = local
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
    expected, basis = None, None
    new_g = groups.get("신축(5년 이내)") or []
    if len(new_g) >= 3:
        expected, basis = statistics.median([t["price"] for t in new_g]), f"실거래 신축 {len(new_g)}건 중앙값"
    elif market_vals:
        expected = statistics.median([c["value"] for c in market_vals])
        basis = f"비교 단지 {len(market_vals)}곳 중앙값"
        if all(c["group"] in ("구축(10년 초과)", "연식 미상") for c in market_vals):
            out["notes"].append("비교 단지가 구축·연식 미상뿐이라 신축 시세보다 낮게 잡혔을 수 있음")
    elif groups.get("준신축(6~10년)"):
        g = groups["준신축(6~10년)"]
        expected, basis = statistics.median([t["price"] for t in g]), f"실거래 준신축 {len(g)}건 중앙값(신축 부족)"
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
    n_used = len(new_g) if len(new_g) >= 3 else len(market_vals) or len(sale_vals)
    year_ago = (today_kst() - dt.timedelta(days=365)).isoformat()
    stale_ratio = (sum((c.get("date") or "") < year_ago for c in market_vals) / len(market_vals)
                   if market_vals and len(new_g) < 3 else 0.0)
    if out.get("score") is None:
        out["confidence"] = "데이터 부족"
    elif n_used >= 5 and stale_ratio < 0.5:
        out["confidence"] = "높음"
    elif n_used >= 3:
        out["confidence"] = "보통"
    else:
        out["confidence"] = "낮음"
    if stale_ratio >= 0.5:
        out["notes"].append(f"비교 거래 {round(stale_ratio * 100)}%가 1년 이상 지난 값")
    if price_rep and band and band in bands:
        out["notes"].append("분양가는 주택형별 최고가(공고 기준) 중앙값이라 저층·비선호 타입은 더 쌉니다")
    return out


def _comp_value(c: dict) -> dict:
    return {"value": to_int(c["price_manwon"]), "source": f"{c.get('comp_name')} ({c.get('source')})", "url": c.get("url"),
            "date": c.get("date"), "kind": c.get("kind"), "build_year": c.get("build_year"), "area": c.get("area"),
            "note": c.get("note")}


def rank(schedules: list[dict]) -> list[dict]:
    """아직 신청할 수 있는 공고만 가치 점수 순으로."""
    today = today_kst().isoformat()
    open_ = [s for s in schedules if any(e["end"] >= today and e["label"] not in ("당첨자 발표", "계약")
                                         for e in s.get("events", []))]
    scored = [s for s in open_ if s.get("market", {}).get("score") is not None]
    rest = [s for s in open_ if s not in scored]
    conf = {"높음": 0, "보통": 1, "낮음": 2}
    scored.sort(key=lambda s: -s["market"]["score"])
    return ([{"id": s["id"], "name": s["name"], "score": s["market"]["score"], "confidence": s["market"]["confidence"],
              "margin_pct": s["market"].get("margin_pct"), "momentum_3m_pct": s["market"].get("momentum_3m_pct")}
             for s in scored]
            + [{"id": s["id"], "name": s["name"], "score": None, "confidence": "데이터 부족"} for s in rest])


def enrich(schedules: list[dict], key: str | None, cfg: dict, warnings: list[str]) -> None:
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
        s["market"] = analyze(s, trades, comps, trends, cfg)
