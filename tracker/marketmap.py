"""시세 지도 — 서울·경기 상급지 아파트 실거래 통계 (docs/market/ 화면용 데이터).

데이터: 국토교통부 아파트 매매 실거래가 상세 자료(공식) — 청약 레이더와 같은 키·같은 캐시(data/state/trades)를 씀.
정제:
  · 해제된 거래 제외(수집 단계), 직거래 제외
  · 단지 안에서 전용㎡당 가격이 단지 중앙값의 0.5배 미만·2배 초과인 거래는 이상치로 빼고 개수만 기록
산출 (기간: 최근 24개월):
  · 단지 × 면적대: 거래 수(6·12개월), 중앙값·평균(6·12개월), 최근 거래, 최저·최고(12개월), 전고점(24개월)과 대비율,
                   전용 3.3㎡당 중앙값, 1년 변동률(최근 6개월 vs 1년 전 같은 6개월, 전용㎡당 중앙값 기준, 양쪽 3건 이상일 때만)
  · 단지: 위 값 + 대표 면적대, 84㎡ 환산가(전용㎡당 중앙값 × 84.9)
  · 동·구: 월별 거래 수와 전용 3.3㎡당 중앙값(24개월), 84㎡ 환산 중앙값, 1년 변동률, 거래량 변동
출력:
  · docs/data/market/summary.json        지도·목록용 요약(단지·동·구)
  · docs/data/market/t_<시군구코드>.json  단지별 거래 목록(단지를 눌렀을 때만 받음)
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics as st
from collections import defaultdict

from . import geo as geo_mod
from .market import TradeClient
from .util import ROOT, now_kst_iso, today_kst, write_json

log = logging.getLogger(__name__)
OUT = ROOT / "docs" / "data" / "market"
PYEONG = 3.3058
STD_AREA = 84.9

# 기본 대상: 서울 25개 구 + 경기 상급지. config [marketmap] areas 로 바꿀 수 있음.
SEOUL = ["종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구", "강북구", "도봉구", "노원구", "은평구",
         "서대문구", "마포구", "양천구", "강서구", "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구"]
DEFAULT_AREAS = [{"region": "서울", "sigungu": g} for g in SEOUL] + [
    {"region": "경기", "sigungu": "성남시 분당구", "label": "분당·판교"},
    {"region": "경기", "sigungu": "과천시", "label": "과천"},
    {"region": "경기", "sigungu": "수원시 영통구", "label": "광교·영통"},
    {"region": "경기", "sigungu": "용인시 수지구", "label": "수지·광교(상현)"},
    {"region": "경기", "sigungu": "화성시 동탄구", "label": "동탄"},
]
# 면적대 통계는 배열로 줄여 저장(화면 docs/market 에서 같은 순서로 풂)
BAND_FIELDS = ["area", "n6", "n12", "n24", "med6", "med12", "avg12", "min12", "max12", "ppa12",
               "last_date", "last_price", "last_floor", "peak_date", "peak_price", "from_peak", "yoy"]
BANDS = [("소형", 0, 50), ("59", 50, 65), ("74", 65, 80), ("84", 80, 92), ("중대형", 92, 120), ("대형", 120, 1e9)]
QUICK = [  # 지도 바로가기 (중심 lat, lng, 줌)
    {"name": "강남3구", "c": [37.497, 127.06, 13]}, {"name": "마용성", "c": [37.548, 126.97, 13]},
    {"name": "여의도·목동", "c": [37.53, 126.89, 13]}, {"name": "분당·판교", "c": [37.375, 127.11, 13]},
    {"name": "과천", "c": [37.43, 126.99, 14]}, {"name": "광교", "c": [37.29, 127.05, 14]},
    {"name": "동탄", "c": [37.2, 127.1, 13]}, {"name": "잠실·위례", "c": [37.5, 127.11, 13]},
]


def band_of(area: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= area < hi:
            return name
    return "대형"


def _ym(d: dt.date, back: int) -> str:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}{m:02d}"


def _med(xs):
    return st.median(xs) if xs else None


def _r(x, nd=0):
    return None if x is None else (round(x) if nd == 0 else round(x, nd))


def clean_trades(rows: list[dict]) -> tuple[list[dict], int, int]:
    """직거래 제외 + 단지별 전용㎡당 가격 이상치 제외. (남은 거래, 직거래 수, 이상치 수)"""
    direct = [r for r in rows if r.get("deal") == "직거래"]
    rows = [r for r in rows if r.get("deal") != "직거래" and r.get("area") and r.get("price")]
    by: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by[(ckey(r), r["date"][:4])].append(r)       # 같은 단지·같은 해 안에서 비교(오랜 기간의 가격 상승을 이상치로 보지 않도록)
    out, outl = [], 0
    for ts in by.values():
        med = st.median(t["price"] / t["area"] for t in ts)
        for t in ts:
            ratio = (t["price"] / t["area"]) / med
            if 0.5 <= ratio <= 2.0:
                out.append(t)
            else:
                outl += 1
    return out, len(direct), outl


def ckey(r: dict) -> str:
    return r.get("seq") or f"{r.get('dong')}|{r.get('jibun') or r.get('apt')}"


def _yoy(ts: list[dict], today: dt.date) -> tuple[float | None, int, int]:
    """최근 6개월 vs 1년 전 같은 6개월의 전용㎡당 중앙값 변동률(%)."""
    a0, a1 = (today - dt.timedelta(days=183)).isoformat(), today.isoformat()
    b0, b1 = (today - dt.timedelta(days=548)).isoformat(), (today - dt.timedelta(days=365)).isoformat()
    now = [t["price"] / t["area"] for t in ts if a0 <= t["date"] <= a1]
    ago = [t["price"] / t["area"] for t in ts if b0 <= t["date"] <= b1]
    if len(now) >= 3 and len(ago) >= 3:
        return round((st.median(now) / st.median(ago) - 1) * 100, 1), len(now), len(ago)
    return None, len(now), len(ago)


def band_stats(ts: list[dict], today: dt.date) -> dict:
    d6, d12 = (today - dt.timedelta(days=183)).isoformat(), (today - dt.timedelta(days=365)).isoformat()
    t6 = [t for t in ts if t["date"] >= d6]
    t12 = [t for t in ts if t["date"] >= d12]
    last = max(ts, key=lambda t: (t["date"], t["price"]))
    # 전고점: 거래 한 건(특수 거래·큰 타입)에 끌리지 않도록 시간순 '연속 3건 중앙값'의 최고치(3건 미만이면 최고 거래가)
    seq = sorted(ts, key=lambda t: t["date"])
    if len(seq) >= 3:
        roll = [(seq[i + 1]["date"], st.median([seq[i]["price"], seq[i + 1]["price"], seq[i + 2]["price"]])) for i in range(len(seq) - 2)]
        pk = max(roll, key=lambda x: x[1])
        peak = {"date": pk[0], "price": round(pk[1])}
    else:
        peak = max(ts, key=lambda t: t["price"])
    p12 = [t["price"] for t in t12]
    ppa12 = [t["price"] / t["area"] * PYEONG for t in t12]
    yoy, n_now, n_ago = _yoy(ts, today)
    area = _med([t["area"] for t in ts])
    ref = _med([t["price"] for t in t6]) or _med(p12) or last["price"]
    return {
        "area": round(area, 1), "n6": len(t6), "n12": len(t12), "n24": len(ts),
        "med6": _r(_med([t["price"] for t in t6])), "med12": _r(_med(p12)),
        "avg12": _r(st.mean(p12)) if p12 else None,
        "min12": min(p12) if p12 else None, "max12": max(p12) if p12 else None,
        "ppa12": _r(_med(ppa12)),                                  # 전용 3.3㎡당 중앙값(만원)
        "last": [last["date"], last["price"], last.get("floor")],
        "peak": [peak["date"], peak["price"]],
        "from_peak": round((ref / peak["price"] - 1) * 100, 1) if peak["price"] else None,
        "yoy": yoy, "yoy_n": [n_now, n_ago],
    }


def monthly(ts: list[dict], yms: list[str]) -> dict:
    """월별 거래 수, 전용 3.3㎡당 중앙값, 그리고 '같은 단지 기준 지수'.

    단순 중앙값은 그달에 어떤 단지가 팔렸는지(비싼 단지 거래가 몰렸는지)에 따라 크게 출렁인다.
    그래서 거래마다 '그 단지의 전체 기간 전용㎡당 중앙값' 대비 비율을 구하고, 월별로 그 비율의 중앙값을 낸다.
    처음 6개월 평균을 100으로 맞춘 값이 idx (같은 단지끼리 비교한 가격 흐름).
    """
    base: dict[str, list] = defaultdict(list)
    for t in ts:
        base[ckey(t)].append(t["price"] / t["area"])
    bmed = {k: st.median(v) for k, v in base.items() if len(v) >= 3}
    by: dict[str, list] = defaultdict(list)
    rel: dict[str, list] = defaultdict(list)
    for t in ts:
        ym = t["date"][:7].replace("-", "")
        by[ym].append(t["price"] / t["area"] * PYEONG)
        b = bmed.get(ckey(t))
        if b:
            rel[ym].append((t["price"] / t["area"]) / b)
    raw = [(_med(rel.get(y)) if len(rel.get(y, [])) >= 5 else None) for y in yms]
    head = [x for x in raw[:6] if x]
    k = 100 / (sum(head) / len(head)) if head else None
    return {"ym": yms, "n": [len(by.get(y, [])) for y in yms], "ppa": [_r(_med(by.get(y))) for y in yms],
            "idx": [round(x * k, 1) if (x and k) else None for x in raw]}


def same_complex_yoy(ts: list[dict], today: dt.date) -> tuple[float | None, int]:
    """지역 1년 변동률 — 같은 단지끼리만 비교: 단지마다 (최근 6개월 ÷ 1년 전 6개월) 전용㎡당 중앙값 비율 → 그 비율들의 중앙값.
    양쪽 기간에 각각 2건 이상 거래된 단지가 5곳 이상일 때만 계산(비싼 단지 거래가 몰린 달의 착시 제거)."""
    a0 = (today - dt.timedelta(days=183)).isoformat()
    b0, b1 = (today - dt.timedelta(days=548)).isoformat(), (today - dt.timedelta(days=365)).isoformat()
    now, ago = defaultdict(list), defaultdict(list)
    for t in ts:
        v = t["price"] / t["area"]
        if t["date"] >= a0:
            now[ckey(t)].append(v)
        elif b0 <= t["date"] <= b1:
            ago[ckey(t)].append(v)
    ratios = [st.median(now[k]) / st.median(ago[k]) for k in now if len(now[k]) >= 2 and len(ago.get(k, [])) >= 2]
    if len(ratios) >= 5:
        return round((st.median(ratios) - 1) * 100, 1), len(ratios)
    return None, len(ratios)


def area_summary(ts: list[dict], today: dt.date, yms: list[str]) -> dict:
    d12 = (today - dt.timedelta(days=365)).isoformat()
    t12 = [t for t in ts if t["date"] >= d12]
    yoy, yoy_cx = same_complex_yoy(ts, today)
    n_now = sum(1 for t in t12)
    n_prev = sum(1 for t in ts if (today - dt.timedelta(days=730)).isoformat() <= t["date"] < d12)
    covered = bool(ts) and min(t["date"] for t in ts) <= (today - dt.timedelta(days=700)).isoformat()
    if not covered:            # 직전 12개월 자료가 다 모이지 않았으면 거래량 변동을 계산하지 않음
        n_prev = 0
    ppa = [t["price"] / t["area"] for t in t12]
    return {"n12": n_now, "n_prev12": n_prev,
            "vol_chg": round((n_now / n_prev - 1) * 100, 1) if n_prev >= 10 else None,
            "ppa12": _r(_med([x * PYEONG for x in ppa])), "std84": _r(_med(ppa) * STD_AREA) if ppa else None,
            "yoy": yoy, "yoy_cx": yoy_cx, "series": monthly(ts, yms)}


HIST_START = (2006, 1)      # 실거래가 공개가 시작된 때
RECENT_RAW_MONTHS = 36      # 단지 상세에 개별 거래를 그대로 싣는 기간(그 이전은 분기 중앙값)


def all_yms(today: dt.date) -> list[str]:
    out, (y, m) = [], HIST_START
    while (y, m) <= (today.year, today.month):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _quarter(date: str) -> str:
    return f"{date[:4]}Q{(int(date[5:7]) - 1) // 3 + 1}"


def history_stats(cts: list[dict], build_year: int | None, today: dt.date) -> dict:
    """준공(또는 첫 거래) 이후 전체 기간 통계 — 전용㎡당 가격 기준이라 면적대가 섞여도 비교 가능.

    y0      첫 기준 연도(준공 연도 이후, 거래 2건 이상인 첫 해)
    since   y0 대비 최근 12개월 누적 상승률(%)
    cagr    연평균 상승률(%, 복리)
    ath_dd  역대 최고(분기 중앙값, 거래 2건 이상) 대비 최근 6개월 중앙값(%)
    mdd     기간 중 최대 낙폭(분기 중앙값 기준 고점→저점, %)과 그 시기
    """
    by_y, by_q = defaultdict(list), defaultdict(list)
    for t in cts:
        v = t["price"] / t["area"]
        by_y[int(t["date"][:4])].append(v)
        by_q[_quarter(t["date"])].append(v)
    years = sorted(y for y, v in by_y.items() if len(v) >= 2 and (not build_year or y >= build_year))
    d12, d6 = (today - dt.timedelta(days=365)).isoformat(), (today - dt.timedelta(days=183)).isoformat()
    now12 = [t["price"] / t["area"] for t in cts if t["date"] >= d12]
    now6 = [t["price"] / t["area"] for t in cts if t["date"] >= d6] or now12
    out = {"y0": None, "since": None, "cagr": None, "ath_dd": None, "mdd": None, "n_all": len(cts)}
    if years and len(now12) >= 2 and years[0] < today.year - 1:
        y0 = years[0]
        base, cur = st.median(by_y[y0]), st.median(now12)
        span = (today.year + (today.month - 6.5) / 12) - (y0 + 0.5)      # 기준 연도 중간 ~ 최근 12개월 중간
        out.update(y0=y0, since=round((cur / base - 1) * 100, 1),
                   cagr=round(((cur / base) ** (1 / span) - 1) * 100, 1) if span > 1 else None)
    qs = sorted((q, st.median(v)) for q, v in by_q.items() if len(v) >= 2)
    if len(qs) >= 4:
        ath = max(v for _, v in qs)
        if now6:
            out["ath_dd"] = round((st.median(now6) / ath - 1) * 100, 1)
        peak_v, peak_q, worst = qs[0][1], qs[0][0], (0.0, None, None)
        for q, v in qs:
            if v > peak_v:
                peak_v, peak_q = v, q
            dd = v / peak_v - 1
            if dd < worst[0]:
                worst = (dd, peak_q, q)
        if worst[1]:
            out["mdd"] = [round(worst[0] * 100, 1), worst[1], worst[2]]
    return out


def band_history(cts: list[dict]) -> dict:
    """면적대별 분기 중앙값 [[분기, 중앙값(만원), 거래 수], …] — 준공 이후 전체 흐름 그래프용."""
    by: dict[tuple, list] = defaultdict(list)
    for t in cts:
        by[(band_of(t["area"]), _quarter(t["date"]))].append(t["price"])
    out: dict[str, list] = defaultdict(list)
    for (b, q), v in sorted(by.items()):
        out[b].append([q, round(st.median(v)), len(v)])
    return out


def chain_index(ts: list[dict]) -> list[list]:
    """연도별 연쇄 지수(같은 단지 기준): 이웃한 두 해에 모두 2건 이상 거래된 단지들의 전용㎡당 중앙값 비율 → 그 중앙값을 이어 붙임.
    반복매매지수와 같은 생각(같은 물건끼리 비교)으로, 어떤 단지가 팔렸는지에 따른 착시가 없다. [[연도, 지수(첫 해=100), 비교 단지 수, 거래 수], …]"""
    by: dict[tuple, list] = defaultdict(list)
    n_y: dict[int, int] = defaultdict(int)
    for t in ts:
        y = int(t["date"][:4])
        by[(ckey(t), y)].append(t["price"] / t["area"])
        n_y[y] += 1
    med = {k: st.median(v) for k, v in by.items() if len(v) >= 2}
    years = sorted(n_y)
    out, idx = [], 100.0
    for i, y in enumerate(years):
        if i == 0:
            out.append([y, 100.0, 0, n_y[y]])
            continue
        ratios = [med[(c, y)] / med[(c, y - 1)] for (c, yy) in med if yy == y and (c, y - 1) in med]
        if len(ratios) >= 5:
            idx *= st.median(ratios)
            out.append([y, round(idx, 1), len(ratios), n_y[y]])
        else:
            out.append([y, None, len(ratios), n_y[y]])
    return out


def build(cfg: dict, key: str | None, geo, months: int = 24, pipeline: list | None = None) -> dict | None:
    mcfg = cfg.get("marketmap", {})
    if not mcfg.get("enabled", True):
        return None
    if not key:
        log.info("시세 지도: DATA_GO_KR_KEY 없음 → 캐시만 사용")
    areas = mcfg.get("areas") or DEFAULT_AREAS
    months = mcfg.get("months", months)
    today = today_kst()
    yms = [_ym(today, i) for i in range(months - 1, -1, -1)]
    client = TradeClient(key or "")
    OUT.mkdir(parents=True, exist_ok=True)
    complexes, gus, dongs = [], [], []
    stats_total = {"trades": 0, "direct": 0, "outliers": 0}
    from .util import read_json
    old_yms = [y for y in all_yms(today) if y < yms[0]]                 # 24개월보다 오래된 달
    budget = {"calls": mcfg.get("backfill_calls_per_run", 300) if key else 0, "fails": 0}
    preps = []                                                           # 시나리오 예측용(지역별 지수·사건 표)

    def cached(code: str, ym: str):
        path = client.cache_dir / f"{code}_{ym}.json"
        if not path.exists():
            return None
        c = read_json(path, [])
        return c["rows"] if isinstance(c, dict) else c
    for ai, a in enumerate(areas):
        sgg = a["sigungu"]
        from .market import SIGUNGU
        codes = a.get("codes") or ([SIGUNGU[sgg]] if sgg in SIGUNGU else client.lawd_codes(sgg))
        raw = []
        for code in codes:
            for ym in yms:
                path = client.cache_dir / f"{code}_{ym}.json"
                try:
                    if key:
                        raw.extend(client.month(code, ym))
                    elif path.exists():
                        from .util import read_json
                        c = read_json(path, [])
                        raw.extend(c["rows"] if isinstance(c, dict) else c)
                except Exception as e:
                    log.warning("실거래 %s %s 실패: %s", code, ym, e)
        # 과거(준공 이후 전체) — 한 번 받은 달은 캐시에 남으므로, 없는 달만 최근 것부터 한 번에 정해진 횟수만큼 채움
        hist_raw, have_from = [], None
        for code in codes:
            for ym in reversed(old_yms):
                rows = cached(code, ym)
                if rows is None and budget["calls"] > 0 and budget["fails"] < 3:
                    try:
                        budget["calls"] -= 1
                        rows = client.month(code, ym)
                        budget["fails"] = 0
                    except Exception as e:
                        budget["fails"] += 1
                        log.warning("과거 실거래 %s %s 실패: %s", code, ym, e)
                if rows is None:
                    break                       # 여기서부터 더 과거는 아직 없음 → 이어진 구간까지만 사용
                hist_raw.extend(rows)
                have_from = ym
        if a.get("dongs"):
            raw = [r for r in raw if r.get("dong") in a["dongs"]]
            hist_raw = [r for r in hist_raw if r.get("dong") in a["dongs"]]
        ts, n_direct, n_out = clean_trades(raw)
        hist_ts, _, _ = clean_trades(hist_raw)
        hist_by: dict[str, list] = defaultdict(list)
        for t in hist_ts:
            hist_by[ckey(t)].append(t)
        raw_from = _ym(today, RECENT_RAW_MONTHS)
        stats_total["trades"] += len(ts)
        stats_total["direct"] += n_direct
        stats_total["outliers"] += n_out
        if not ts:
            continue
        code0 = codes[0] if codes else str(ai)
        # ---- 단지
        by: dict[str, list] = defaultdict(list)
        for t in ts:
            by[ckey(t)].append(t)
        detail = {}
        for k, cts in by.items():
            t0 = max(cts, key=lambda t: t["date"])
            bands = defaultdict(list)
            for t in cts:
                bands[band_of(t["area"])].append(t)
            full = {b: band_stats(v, today) for b, v in bands.items()}
            rep = "84" if "84" in full else max(full, key=lambda b: full[b]["n24"])
            bstats = {b: [x["area"], x["n6"], x["n12"], x["n24"], x["med6"], x["med12"], x["avg12"], x["min12"], x["max12"],
                          x["ppa12"], x["last"][0], x["last"][1], x["last"][2], x["peak"][0], x["peak"][1], x["from_peak"], x["yoy"]]
                      for b, x in full.items()}
            d12 = (today - dt.timedelta(days=365)).isoformat()
            ppa = [t["price"] / t["area"] for t in cts if t["date"] >= d12] or [t["price"] / t["area"] for t in cts]
            yoy, _, _ = _yoy(cts, today)
            cid = f"{code0}:{k}"
            all_cts = hist_by.get(k, []) + cts
            hs = history_stats(all_cts, t0.get("build_year"), today)
            g = geo.locate(f"cx:{sgg}:{t0.get('dong')}:{t0.get('jibun') or t0.get('apt')}",
                           f"{a['region']} {sgg} {t0.get('dong')} {t0.get('jibun')}" if t0.get("jibun") else None,
                           a["region"], sgg, t0.get("apt")) if geo else None
            complexes.append({
                "id": cid, "name": t0.get("apt"), "gu": sgg, "region": a["region"], "dong": t0.get("dong"),
                "jibun": t0.get("jibun"), "by": t0.get("build_year"),
                "lat": round(g["lat"], 5) if g else None, "lng": round(g["lng"], 5) if g else None,
                "geo": g["precision"] if g else None,
                "rep": rep, "n12": sum(1 for t in cts if t["date"] >= d12), "n24": len(cts),
                "ppa12": _r(st.median(ppa) * PYEONG), "std84": _r(st.median(ppa) * STD_AREA), "yoy": yoy,
                "hist": [hs["y0"], hs["since"], hs["cagr"], hs["ath_dd"], hs["mdd"], hs["n_all"]],
                "bands": bstats,
            })
            recent = [t for t in all_cts if t["date"].replace("-", "")[:6] >= raw_from]
            detail[cid] = {"t": [[t["date"].replace("-", ""), round(t["area"], 1), t["price"], t.get("floor")]
                                 for t in sorted(recent, key=lambda t: t["date"])],
                           "h": band_history(all_cts)}
        write_json(OUT / f"t_{code0}.json", detail, compact=True)
        # ---- 구 / 동
        try:
            from . import forecast
            preps.append(forecast.prepare(code0, sgg, a.get("label") or sgg, a["region"], hist_ts + ts,
                                          [z for z in (pipeline or []) if z.get("sigungu") == sgg and z.get("milestones")],
                                          have_from or yms[0]))
        except Exception as e:
            log.warning("시나리오 예측 준비 실패 %s: %s", sgg, e)
        gs = area_summary(ts, today, yms)
        gus.append({"gu": sgg, "region": a["region"], "label": a.get("label") or sgg, "code": code0, **gs,
                    "long": chain_index(hist_ts + ts), "hist_from": have_from or yms[0]})
        dts = defaultdict(list)
        for t in ts:
            dts[t["dong"]].append(t)
        for dn, v in dts.items():
            ds = area_summary(v, today, yms)
            ds.pop("series")
            dongs.append({"gu": sgg, "dong": dn, **ds})
        log.info("시세 지도 %s: 거래 %d건(직거래 %d·이상치 %d 제외), 단지 %d", sgg, len(ts), n_direct, n_out, len(by))
    # 구·동 위치: 소속 단지 좌표(번지 단위)의 중앙값, 없으면 구 중심
    def place(x, pts):
        if pts:
            x["lat"], x["lng"] = round(st.median(p[0] for p in pts), 5), round(st.median(p[1] for p in pts), 5)
        else:
            cen = geo_mod.centroid(x.get("region") or ("서울" if x["gu"] in SEOUL else "경기"), x["gu"])
            x["lat"], x["lng"] = (cen[0], cen[1]) if cen else (None, None)
    exact = [c for c in complexes if c["lat"] and c["geo"] == "exact"]
    for x in gus:
        place(x, [(c["lat"], c["lng"]) for c in exact if c["gu"] == x["gu"]])
    for x in dongs:
        x["region"] = next((g["region"] for g in gus if g["gu"] == x["gu"]), None)
        place(x, [(c["lat"], c["lng"]) for c in exact if c["gu"] == x["gu"] and c["dong"] == x["dong"]])
    summary = {
        "generated_at": now_kst_iso(), "months": months, "yms": yms, "bands": [b[0] for b in BANDS],
        "band_fields": BAND_FIELDS, "hist_fields": ["y0", "since", "cagr", "ath_dd", "mdd", "n_all"],
        "backfill_left": sum(1 for g in gus if g["hist_from"] > all_yms(today)[0]),
        "quick": QUICK, "totals": stats_total, "gus": gus, "dongs": dongs, "complexes": complexes,
        "method": {
            "source": "국토교통부 아파트 매매 실거래가 상세 자료 (공공데이터포털)",
            "source_url": "https://www.data.go.kr/data/15126468/openapi.do",
            "check_url": "https://rt.molit.go.kr/",
            "rules": ["해제된 거래 제외", "직거래 제외",
                      "단지 안에서 전용㎡당 가격이 단지 중앙값의 0.5배 미만·2배 초과인 거래는 이상치로 제외",
                      "가격 단위: 만원, 3.3㎡당 가격은 전용면적 기준",
                      "84㎡ 환산 = 전용㎡당 중앙값 × 84.9",
                      "단지 1년 변동 = 최근 6개월 vs 1년 전 같은 6개월의 전용㎡당 중앙값 (양쪽 3건 이상일 때만)",
                      "구·동 1년 변동 = 양쪽 기간에 2건 이상 거래된 같은 단지들의 변동률 중앙값 (단지 5곳 이상일 때만)",
                      "가격 지수 = 거래마다 그 단지 중앙값 대비 비율의 월별 중앙값, 처음 6개월 = 100 (어떤 단지가 팔렸는지에 따른 착시 제거)",
                      "전고점 = 최근 24개월 시간순 '연속 3건 중앙값'의 최고치(한 건짜리 특수 거래에 끌리지 않게), 대비율은 최근 6개월 중앙값 기준",
                      "거래량 변동 = 최근 12개월 vs 직전 12개월 (24개월 자료가 다 모였을 때만)",
                      "준공 이후 누적·연평균 상승률 = 준공 연도 이후 거래 2건 이상인 첫 해의 전용㎡당 중앙값 대비 최근 12개월 (복리 연율)",
                      "역대 최고 대비·최대 낙폭 = 분기 중앙값(거래 2건 이상) 기준",
                      "장기 지수 = 이웃한 두 해에 모두 거래된 같은 단지들의 가격 비율 중앙값을 이어 붙인 연쇄 지수(단지 5곳 이상인 해만)",
                      "이상치 판정은 같은 단지·같은 해 안에서만(오랜 기간의 상승을 이상치로 보지 않음)"],
        },
    }
    write_json(OUT / "summary.json", summary, compact=True)
    try:
        from . import forecast
        forecast.finalize(preps, summary["backfill_left"])
    except Exception as e:
        log.warning("시나리오 예측 실패: %s", e)
    located = sum(1 for c in complexes if c["geo"] == "exact")
    log.info("시세 지도: 단지 %d (번지 위치 %d), 구 %d, 동 %d", len(complexes), located, len(gus), len(dongs))
    return summary
