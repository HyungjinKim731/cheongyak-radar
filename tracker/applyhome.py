"""한국부동산원 청약홈 분양정보 조회 서비스 (data.go.kr 15098547) 수집기.

엔드포인트: https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/<operation>
- 응답: {"currentCount", "data": [...], "matchCount", "page", "perPage", "totalCount"}
- 오류: {"code": -401, "msg": "..."}
필드명은 엔드포인트마다 조금씩 다르므로(예: 접수일 RCEPT_* / SUBSCRPT_RCEPT_* / GNRL_RCEPT_*)
후보 필드를 순서대로 찾는다.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

from .util import ApiError, clean, http_get_json, to_int, to_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    detail_ops: tuple[str, ...]  # 대소문자 표기가 문서마다 달라 후보를 둔다
    model_ops: tuple[str, ...]


SOURCES = {
    "apt": Source("apt", "APT 분양", ("getAPTLttotPblancDetail",), ("getAPTLttotPblancMdl",)),
    "remndr": Source("remndr", "무순위·잔여", ("getRemndrLttotPblancDetail",), ("getRemndrLttotPblancMdl",)),
    "opt": Source("opt", "임의공급", ("getOPTLttotPblancDetail", "getOptLttotPblancDetail"),
                  ("getOPTLttotPblancMdl", "getOptLttotPblancMdl")),
}

# (라벨, 시작 후보필드, 종료 후보필드)
EVENT_FIELDS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("특별공급", ("SPSPLY_RCEPT_BGNDE",), ("SPSPLY_RCEPT_ENDDE",)),
    ("1순위 해당", ("GNRL_RNK1_CRSPAREA_RCPTDE", "GNRL_RNK1_CRSPAREA_RCEPT_BGNDE"),
     ("GNRL_RNK1_CRSPAREA_ENDDE", "GNRL_RNK1_CRSPAREA_RCEPT_ENDDE")),
    ("1순위 경기", ("GNRL_RNK1_ETC_GG_RCPTDE",), ("GNRL_RNK1_ETC_GG_ENDDE",)),
    ("1순위 기타", ("GNRL_RNK1_ETC_AREA_RCPTDE",), ("GNRL_RNK1_ETC_AREA_ENDDE",)),
    ("2순위", ("GNRL_RNK2_CRSPAREA_RCPTDE",), ("GNRL_RNK2_CRSPAREA_ENDDE", "GNRL_RNK2_ETC_AREA_ENDDE")),
    ("청약접수", ("RCEPT_BGNDE", "SUBSCRPT_RCEPT_BGNDE", "GNRL_RCEPT_BGNDE"),
     ("RCEPT_ENDDE", "SUBSCRPT_RCEPT_ENDDE", "GNRL_RCEPT_ENDDE")),
    ("당첨자 발표", ("PRZWNER_PRESNATN_DE",), ("PRZWNER_PRESNATN_DE",)),
    ("계약", ("CNTRCT_CNCLS_BGNDE",), ("CNTRCT_CNCLS_ENDDE",)),
]


# 청약홈 공고의 Y/N 표기 (없으면 비어 있음)
FLAG_FIELDS = {
    "overheat": "SPECLT_RDN_EARTH_AT",          # 투기과열지구
    "adjust": "MDAT_TRGET_AREA_SECD",           # 조정대상지역
    "price_cap": "PARCPRC_ULS_AT",              # 분양가상한제
    "redevelop": "IMPRMN_BSNS_AT",              # 정비사업
    "public_zone": "PUBLIC_HOUSE_EARTH_AT",     # 공공주택지구
    "large_site": "LRSCL_BLDLND_AT",            # 대규모 택지개발지구
    "capital_public": "NPLN_PRVOPR_PUBLIC_HOUSE_AT",  # 수도권 내 민영 공공주택지구
}


def _pick(row: dict, fields: Iterable[str]) -> Any:
    for f in fields:
        v = row.get(f)
        if v not in (None, "", " "):
            return v
    return None


def build_events(row: dict) -> list[dict]:
    events = []
    detail_rank = False
    for label, start_f, end_f in EVENT_FIELDS:
        start = to_iso(_pick(row, start_f))
        end = to_iso(_pick(row, end_f)) or start
        if not start:
            continue
        if label.startswith(("1순위", "2순위")):
            detail_rank = True
        # 순위별 일정이 있으면 '청약접수'(전체 범위)는 중복이라 생략
        if label == "청약접수" and detail_rank:
            continue
        events.append({"label": label, "start": start, "end": end})
    events.sort(key=lambda e: e["start"])
    return events


def normalize(row: dict, source: Source) -> dict | None:
    name = clean(row.get("HOUSE_NM"))
    if not name:
        return None
    hmno = clean(row.get("HOUSE_MANAGE_NO")) or ""
    pbno = clean(row.get("PBLANC_NO")) or hmno
    kind = " · ".join(x for x in (
        clean(row.get("HOUSE_SECD_NM")),
        clean(row.get("HOUSE_DTL_SECD_NM")),
        clean(row.get("RENT_SECD_NM")),
    ) if x and x not in {"분양주택"}) or source.label
    events = build_events(row)
    return {
        "id": f"{source.key}:{hmno}:{pbno}",
        "source": source.key,
        "source_label": source.label,
        "house_manage_no": hmno,
        "pblanc_no": pbno,
        "name": name,
        "kind": kind,
        "region": clean(row.get("SUBSCRPT_AREA_CODE_NM")) or _region_from_address(row.get("HSSPLY_ADRES")),
        "address": clean(row.get("HSSPLY_ADRES")),
        "units": to_int(row.get("TOT_SUPLY_HSHLDCO")),
        "builder": clean(row.get("CNSTRCT_ENTRPS_NM")) or clean(row.get("BSNS_MBY_NM")),
        "notice_date": to_iso(row.get("RCRIT_PBLANC_DE")),
        "move_in": clean(row.get("MVN_PREARNGE_YM")),
        "url": clean(row.get("PBLANC_URL")),
        "homepage": clean(row.get("HMPG_ADRES")),
        "speculative": clean(row.get("SPECLT_RDN_EARTH_AT")),   # 투기과열지구 여부(Y/N)
        "price_cap": clean(row.get("PARCPRC_ULS_AT")),          # 분양가상한제 여부(Y/N)
        "flags": {k: clean(row.get(f)) for k, f in FLAG_FIELDS.items() if clean(row.get(f))},
        "events": events,
        "first_date": events[0]["start"] if events else to_iso(row.get("RCRIT_PBLANC_DE")),
        "last_date": max((e["end"] for e in events), default=None),
    }


_REGION_PREFIX = {
    "서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산", "대구": "대구", "광주": "광주",
    "대전": "대전", "울산": "울산", "세종": "세종", "강원": "강원", "충청북": "충북", "충북": "충북",
    "충청남": "충남", "충남": "충남", "전라북": "전북", "전북": "전북", "전라남": "전남", "전남": "전남",
    "경상북": "경북", "경북": "경북", "경상남": "경남", "경남": "경남", "제주": "제주",
}


def _region_from_address(addr: Any) -> str | None:
    s = clean(addr)
    if not s:
        return None
    for k, v in _REGION_PREFIX.items():
        if s.startswith(k):
            return v
    return None


def _is_auth_error(e: Exception) -> bool:
    # odcloud 인증 오류: -4(미등록 키), -401(키 누락) 등. 메시지에 '인증키'가 들어간다
    return isinstance(e, ApiError) and ("인증키" in str(e) or "-401" in str(e) or "-4:" in str(e))


class ApplyhomeClient:
    def __init__(self, service_key: str, base_url: str, per_page: int = 500, max_pages: int = 20,
                 session: requests.Session | None = None,
                 getter: Callable[[requests.Session, str, dict], dict] | None = None):
        self.key = service_key
        self.base = base_url.rstrip("/")
        self.per_page = per_page
        self.max_pages = max_pages
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "cheongyak-tracker/1.0"
        self._get = getter or http_get_json

    def _call(self, ops: tuple[str, ...], params: dict) -> tuple[str, dict]:
        last: Exception | None = None
        for op in ops:
            try:
                data = self._get(self.session, f"{self.base}/{op}", {"serviceKey": self.key, **params})
                return op, data
            except Exception as e:
                last = e
                # 인증키 오류는 다른 이름으로 바꿔도 같으므로 바로 중단
                if _is_auth_error(e):
                    raise
        raise last or RuntimeError("no operation")

    def fetch_rows(self, source: Source, since: dt.date) -> list[dict]:
        cond = {"cond[RCRIT_PBLANC_DE::GTE]": since.isoformat()}
        try:
            rows = self._paginate(source.detail_ops, cond)
        except ApiError as e:
            if _is_auth_error(e):
                raise
            log.warning("%s: 날짜 조건 조회 실패(%s) → 조건 없이 재시도", source.label, e)
            rows = self._paginate(source.detail_ops, {})
        # 서버 필터가 무시되는 경우에 대비해 클라이언트에서도 거른다
        out = []
        for r in rows:
            d = to_iso(r.get("RCRIT_PBLANC_DE"))
            if d is None or d >= since.isoformat():
                out.append(r)
        return out

    def _paginate(self, ops: tuple[str, ...], extra: dict) -> list[dict]:
        rows: list[dict] = []
        total = None
        for page in range(1, self.max_pages + 1):
            _, data = self._call(ops, {"page": page, "perPage": self.per_page, **extra})
            batch = data.get("data") or []
            total = total if total is not None else data.get("matchCount", data.get("totalCount"))
            rows.extend(batch)
            if not batch or (total is not None and len(rows) >= int(total)):
                break
        return rows

    def fetch_models(self, source: Source, house_manage_no: str, pblanc_no: str) -> list[dict]:
        params = {"page": 1, "perPage": 100,
                  "cond[HOUSE_MANAGE_NO::EQ]": house_manage_no,
                  "cond[PBLANC_NO::EQ]": pblanc_no}
        _, data = self._call(source.model_ops, params)
        return data.get("data") or []


CMPET_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoCmpetRtSvc/v1"
CMPET_OPS = {"apt": ("getAPTLttotPblancCmpet",), "remndr": ("getRemndrLttotPblancCmpet",),
             "opt": ("getOPTLttotPblancCmpet",)}
SCORE_OP = ("getAptLttotPblancScore",)


def fetch_competition(client: "ApplyhomeClient", source_key: str, house_manage_no: str) -> dict:
    """청약홈 경쟁률·당첨가점 (data.go.kr 15098905, 같은 키로 별도 활용신청 필요)."""
    params = {"page": 1, "perPage": 500, "cond[HOUSE_MANAGE_NO::EQ]": house_manage_no}
    base = client.base
    client.base = CMPET_BASE
    try:
        _, data = client._call(CMPET_OPS[source_key], params)
        rows = data.get("data") or []
        scores = []
        if source_key == "apt":
            try:
                _, sd = client._call(SCORE_OP, params)
                scores = sd.get("data") or []
            except Exception:
                scores = []
    finally:
        client.base = base
    return summarize_competition(rows, scores)


def _rate(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None  # 미달은 '(△3)' 같은 표기라 숫자가 아님


def summarize_competition(rows: list[dict], scores: list[dict]) -> dict:
    by_type: dict[str, dict] = {}
    rank1_rates = []
    for r in rows:
        rank = str(r.get("SUBSCRPT_RANK_CODE") or "")
        ty = clean(r.get("HOUSE_TY")) or "-"
        d = by_type.setdefault(ty, {"type": ty, "supply": to_int(r.get("SUPLY_HSHLDCO")), "req": 0, "rates": {}})
        if rank == "1":
            d["req"] += to_int(r.get("REQ_CNT")) or 0
            rate = _rate(r.get("CMPET_RATE"))
            d["rates"][clean(r.get("RESIDE_SENM")) or "전체"] = r.get("CMPET_RATE")
            if rate is not None:
                rank1_rates.append(rate)
    supply = sum(d["supply"] or 0 for d in by_type.values())
    req = sum(d["req"] for d in by_type.values())
    sc = [s for s in scores if to_int(s.get("LWET_SCORE")) is not None]
    return {
        "types": list(by_type.values()),
        "rank1_supply": supply or None,
        "rank1_req": req or None,
        "rank1_avg": round(req / supply, 2) if supply and req else None,
        "rank1_max": max(rank1_rates) if rank1_rates else None,
        "score_min": min((to_int(s.get("LWET_SCORE")) for s in sc), default=None),
        "score_max": max((to_int(s.get("TOP_SCORE")) or 0 for s in sc), default=None),
        "score_avg": round(sum(float(s.get("AVRG_SCORE") or 0) for s in sc) / len(sc), 1) if sc else None,
        "available": bool(rows),
    }


_AREA_RE = re.compile(r"(\d+(?:\.\d+)?)")


BANDS = [49, 59, 74, 84, 102, 114, 135, 170]


def area_band(area: float | None) -> int | None:
    if not area:
        return None
    return min(BANDS, key=lambda b: abs(b - area))


SUPPLY_RATIO = 1.33   # 공급면적 정보가 없을 때 전용면적 × 1.33 으로 추정(수도권 신축 평균 수준)
MODEL_KEEP = ("HOUSE_TY", "SUPLY_AR", "LTTOT_TOP_AMOUNT", "SUPLY_HSHLDCO", "SPSPLY_HSHLDCO")


def _f(v) -> float | None:
    try:
        x = float(str(v).replace(",", ""))
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def summarize_models(models: list[dict]) -> dict:
    """주택형 목록 → 전체 가격대 + 평형(전용면적 구간)별 가격·평당가·세대수."""
    import statistics as _st
    prices, areas, per_pyeong = [], [], []
    bands: dict[int, dict] = {}
    for m in models:
        p = to_int(m.get("LTTOT_TOP_AMOUNT"))  # 만원 단위(주택형 최고가)
        mt = _AREA_RE.search(str(m.get("HOUSE_TY") or ""))
        area = float(mt.group(1)) if mt else None
        if area:
            areas.append(area)
        b = area_band(area)
        if b is None:
            continue
        sup = _f(m.get("SUPLY_AR"))
        est = sup is None
        sup = sup or round(area * SUPPLY_RATIO, 2)
        g = bands.setdefault(b, {"prices": [], "pp": [], "areas": [], "sups": [], "est": False, "types": set(), "units": 0, "sp_units": 0})
        g["areas"].append(area)
        g["sups"].append(sup)
        g["est"] |= est
        g["types"].add(str(m.get("HOUSE_TY") or "").strip().lstrip("0"))
        g["units"] += to_int(m.get("SUPLY_HSHLDCO")) or 0
        g["sp_units"] += to_int(m.get("SPSPLY_HSHLDCO")) or 0
        if p:
            prices.append(p)
            g["prices"].append(p)
            pp = p / (sup / 3.3058)
            g["pp"].append(pp)
            if not est:
                per_pyeong.append(pp)
    out_bands = {}
    for b, g in sorted(bands.items()):
        sup_avg = sum(g["sups"]) / len(g["sups"])
        out_bands[str(b)] = {
            "min": min(g["prices"]) if g["prices"] else None,
            "max": max(g["prices"]) if g["prices"] else None,
            "median": int(_st.median(g["prices"])) if g["prices"] else None,
            "n": len(g["prices"]),
            "area_min": round(min(g["areas"]), 2), "area_max": round(max(g["areas"]), 2),
            "supply_m2": round(sup_avg, 1), "pyeong": round(sup_avg / 3.3058),   # 공급면적 기준 '평형'
            "supply_estimated": g["est"],
            "pyeong_price": round(_st.median(g["pp"])) if g["pp"] else None,     # 3.3㎡당(만원)
            "types": sorted(t for t in g["types"] if t),
            "units": g["units"] or None, "sp_units": g["sp_units"] or None,
        }
    return {
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "area_min": round(min(areas), 1) if areas else None,
        "area_max": round(max(areas), 1) if areas else None,
        "types": len(models),
        "pyeong_price": round(sum(per_pyeong) / len(per_pyeong)) if per_pyeong else (
            round(_st.median([v["pyeong_price"] for v in out_bands.values() if v["pyeong_price"]]))
            if any(v["pyeong_price"] for v in out_bands.values()) else None),
        "pyeong_price_estimated": not per_pyeong,
        "bands": out_bands,
    }


def collect(client: ApplyhomeClient, source_keys: list[str], regions: list[str], since: dt.date,
            price_cache: dict, fetch_prices: bool = True) -> tuple[list[dict], list[str]]:
    """반환: (정규화된 공고 목록, 경고 메시지)."""
    items: list[dict] = []
    warnings: list[str] = []
    for key in source_keys:
        src = SOURCES[key]
        try:
            rows = client.fetch_rows(src, since)
        except ApiError:
            raise
        except Exception as e:  # 한 유형이 실패해도 나머지는 계속
            warnings.append(f"{src.label} 수집 실패: {e}")
            continue
        for r in rows:
            n = normalize(r, src)
            if n and (not regions or n["region"] in regions):
                items.append(n)
        log.info("%s: %d건 중 대상 지역 %d건", src.label, len(rows),
                 sum(1 for i in items if i["source"] == key))

    if fetch_prices:
        for it in items:
            ck = it["id"]
            c = price_cache.get(ck)
            if not (isinstance(c, dict) and c.get("v") == 2):     # 예전 캐시(요약만 저장)는 다시 받음
                try:
                    models = client.fetch_models(SOURCES[it["source"]], it["house_manage_no"], it["pblanc_no"])
                    c = price_cache[ck] = {"v": 2, "models": [{k: m.get(k) for k in MODEL_KEEP if m.get(k) not in (None, "")}
                                                              for m in models]}
                except Exception as e:
                    warnings.append(f"분양가 조회 실패 {it['name']}: {e}")
                    continue
            it.update(summarize_models(c["models"]))
    # 같은 공고가 중복으로 내려오는 경우 제거
    uniq = {i["id"]: i for i in items}
    return sorted(uniq.values(), key=lambda x: (x["first_date"] or "9999")), warnings
