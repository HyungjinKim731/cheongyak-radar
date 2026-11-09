"""공고마다 '조건 쉽게 보기'를 만든다.

청약홈 공고의 투기과열·조정·분양가상한제 표기와 공고 유형(국민/민영/무순위/임의공급), 면적대를 바탕으로
초보자가 먼저 확인할 항목을 문장으로 정리한다. 최종 기준은 항상 입주자모집공고문이다.
"""
from __future__ import annotations

import csv

from . import sources
from .guide import REGULATED
from .util import ROOT


# 같은 시·군·구의 최근 청약홈 공고에 적힌 규제 표기 {(시도, 시군구): 단계} — run 에서 채움.
# 10·15 대책 이후 추가 지정된 곳도 공식 공고 표기로 자동 반영된다.
LEARNED: dict[tuple, str] = {}


def learn_from(schedules: list[dict]) -> None:
    LEARNED.clear()
    for s in sorted(schedules, key=lambda x: x.get("notice_date") or ""):   # 최신 공고가 마지막에 덮어씀
        f = s.get("flags") or {}
        if f.get("overheat") in ("Y", "N") and s.get("sigungu"):
            lvl = "투기과열" if f.get("overheat") == "Y" else "조정" if f.get("adjust") == "Y" else "비규제"
            LEARNED[(s.get("region"), s["sigungu"])] = lvl


def regulation(n: dict) -> tuple[str, str]:
    """(규제 단계, 판단 근거). 단계: 투기과열 / 조정 / 비규제 / 미확인."""
    f = n.get("flags") or {}
    if f.get("overheat") == "Y":
        return "투기과열", "청약홈 공고 표기"
    if f.get("adjust") == "Y":
        return "조정", "청약홈 공고 표기"
    if f.get("overheat") == "N" and f.get("adjust") in ("N", None):
        return "비규제", "청약홈 공고 표기"
    reg, sgg = n.get("region"), n.get("sigungu") or ""
    if (reg, sgg) in LEARNED:
        return LEARNED[(reg, sgg)], "같은 시·군·구 최근 청약홈 공고 표기"
    lst = REGULATED.get(reg or "", [])
    if "*" in lst or (sgg and any(sgg.startswith(x) for x in lst)):
        return "투기과열", "10·15 대책 지역 목록으로 추정"
    if reg in ("서울", "경기", "인천"):
        return "비규제", "10·15 대책 지역 목록으로 추정"
    return "미확인", "정보 없음"


def is_core(n: dict, focus: dict | None = None) -> bool:
    """핵심지역 = 정부가 지정한 규제지역(투기과열지구·조정대상지역, 10·15 대책: 서울 전역 + 경기 12곳).
    config [focus] include / exclude 로 시·군·구를 더하거나 뺄 수 있음."""
    focus = focus or {}
    sgg = f"{n.get('region') or ''} {n.get('sigungu') or ''}"
    if any(x and x in sgg for x in focus.get("exclude", [])):
        return False
    if any(x and x in sgg for x in focus.get("include", [])):
        return True
    return regulation(n)[0] in ("투기과열", "조정")


def _deposit_line(band: int | None) -> str:
    if band is None:
        return "서울 거주자 300만원~, 경기 거주자 200만원~ (면적에 따라 증가)"
    if band <= 85:
        return "전용 85㎡ 이하: 서울 거주 300만원, 인천 250만원, 경기 200만원"
    if band <= 102:
        return "전용 102㎡ 이하: 서울 600만원, 인천 400만원, 경기 300만원"
    if band <= 135:
        return "전용 135㎡ 이하: 서울 1,000만원, 인천 700만원, 경기 400만원"
    return "모든 면적: 서울 1,500만원, 인천 1,000만원, 경기 500만원"


def _ratio_line(level: str, bands: list[int]) -> str:
    parts = []
    for b in sorted(set(bands)):
        if level in ("투기과열", "조정"):
            if b <= 60:
                parts.append(f"{b}㎡: 가점 40% · 추첨 60%")
            elif b <= 85:
                parts.append(f"{b}㎡: 가점 70% · 추첨 30%")
            else:
                parts.append(f"{b}㎡: 가점 {80 if level == '투기과열' else 50}% · 추첨 {20 if level == '투기과열' else 50}%")
        else:
            parts.append(f"{b}㎡: " + ("가점 최대 40%, 나머지 추첨" if b <= 85 else "100% 추첨"))
    return " / ".join(parts) or "주택형 정보 없음"


def conditions(n: dict) -> list[dict]:
    level, basis = regulation(n)
    kind = n.get("kind") or ""
    src = n.get("source")
    name = n.get("name") or ""
    bands = [int(b) for b in (n.get("bands") or {}).keys()]
    out: list[dict] = [{"title": "규제지역", "tone": "warn" if level in ("투기과열", "조정") else "info",
                        "body": f"{level} ({basis})", "facts": ["gg_regulated_count"]}]
    if src == "remndr":
        out += [
            {"title": "누가 넣나", "tone": "info",
             "body": "청약통장 없이 신청. 2025년 개편 이후 무주택 세대구성원만 가능하고, 거주 지역 요건은 공고마다 다릅니다(전국·수도권·해당 시).",
             "facts": []},
            {"title": "주의", "tone": "warn",
             "body": "당첨되면 이후 청약에서 당첨자로 관리되고, 규제지역 단지는 재당첨 제한(최장 10년)이 붙을 수 있습니다. 계약금 현금을 먼저 확인하세요."
             if level in ("투기과열", "조정") else
             "당첨 후 계약하면 분양권 보유자가 되어 이후 청약에서 무주택 자격을 잃습니다. 계약금 현금을 먼저 확인하세요.",
             "facts": ["rewin_years"]},
        ]
        return out
    if src == "opt":
        return out + [{"title": "누가 넣나", "tone": "info",
                       "body": "임의공급은 사업자가 남은 물량을 선착순·추첨으로 공급합니다. 통장이 필요 없는 경우가 많지만 자격은 공고마다 다릅니다.",
                       "facts": []}]

    public = "국민" in kind or any(k in name for k in ("공공분양", "신혼희망", "토지임대부"))
    if level in ("투기과열", "조정"):
        out.append({"title": "1순위 조건", "tone": "warn",
                    "body": "청약통장 2년·24회 이상, 세대주만, 최근 5년 안에 세대원 누구도 당첨된 적 없어야 함"
                            + ("" if public else ", 2주택 이상 세대는 1순위 불가"),
                    "facts": ["rank1_regulated_months"]})
    else:
        out.append({"title": "1순위 조건", "tone": "info",
                    "body": "청약통장 1년·12회 이상(수도권). 세대원도 신청 가능. 공고에서 기간 연장 여부만 확인",
                    "facts": ["rank1_capital_months"]})
    if public:
        out.append({"title": "공공분양 방식", "tone": "info",
                    "body": "세대 전원이 무주택이어야 하고 소득·자산 기준이 있습니다. 일반공급은 통장 납입 인정금액(회차당 최대 25만원) 많은 순으로 뽑습니다.",
                    "facts": ["monthly_cap"]})
        if "신혼희망" in name:
            out.append({"title": "신혼희망타운", "tone": "info",
                        "body": "혼인 7년 이내·예비 신혼부부·6세 이하 자녀를 둔 한부모 가구 대상. 자산·소득 기준은 공고문 확인.",
                        "facts": []})
        if "토지임대부" in name:
            out.append({"title": "토지임대부", "tone": "warn",
                        "body": "땅은 공공 소유라 분양가가 낮은 대신 매달 토지임대료를 냅니다. 전매제한이 길고 대출 조건이 일반 분양과 다를 수 있습니다.",
                        "facts": []})
    else:
        out.append({"title": "통장 예치금", "tone": "info", "body": "모집공고일까지 잔액 기준(내 주소지 기준). " +
                    _deposit_line(max(bands) if bands else None), "facts": ["deposit_seoul_85", "deposit_etc_85"]})
        out.append({"title": "가점 · 추첨 비율", "tone": "info", "body": _ratio_line(level, bands),
                    "facts": ["ratio_reg_60", "ratio_reg_85", "ratio_overheat_85p"]})
    r = restrictions(n)
    if "resale_years" in r:
        rs = years_txt(r["resale_years"])
        rd = (years_txt(r["reside_max"]) if r["reside_min"] == r["reside_max"]
              else f"{years_txt(r['reside_min'])}~{years_txt(r['reside_max'])}")
        out.append({"title": "당첨 후 제한", "tone": "warn" if (r["resale_years"] >= 1 or r["reside_max"]) else "info",
                    "body": f"전매제한 {rs}({r['resale_basis']}) · 실거주의무 {rd if r['reside_max'] else '없음'}({r['reside_basis']})"
                            f" · 재당첨 제한 {years_txt(r['rewin_years'])}. 법령 기준 추정이며 최종은 공고문",
                    "facts": ["resale_years", "rewin_years"]})
    if (n.get("flags") or {}).get("price_cap") == "Y" or n.get("price_cap") == "Y":
        out.append({"title": "분양가상한제", "tone": "info",
                    "body": "주변보다 싸게 나오는 대신 전매제한·실거주의무가 붙을 수 있습니다.", "facts": []})
    if n.get("price_min"):
        lo, hi = n["price_min"] * 0.1, (n.get("price_max") or n["price_min"]) * 0.2
        ltv = "규제지역이라 주담대 LTV는 40%" if level in ("투기과열", "조정") else "비규제지역 주담대 LTV는 무주택자 70% 안팎(은행·소득에 따라 다름)"
        out.append({"title": "필요한 현금(대략)", "tone": "info",
                    "body": f"계약금 10~20% 가정 시 약 {_eok(lo)}~{_eok(hi)}. {ltv}.",
                    "facts": []})
    return out


def _eok(man: float) -> str:
    return f"{man / 10000:.1f}억" if man >= 10000 else f"{round(man):,}만원"


# ---------------------------------------------------------------- 전매제한 · 실거주의무 · 재당첨 제한
# 근거(공식): 주택법 시행령 제73조·별표 3(전매제한), 주택법 제57조의2·시행령 제60조의2(거주의무),
#            주택공급에 관한 규칙 제54조(재당첨 제한). 최종 기준은 항상 입주자모집공고문.
LAW_RESALE = "https://www.law.go.kr/법령/주택법시행령/제73조"
LAW_RESIDE = "https://www.law.go.kr/법령/주택법/제57조의2"
LAW_REWIN = "https://www.law.go.kr/법령/주택공급에관한규칙/제54조"

# 수도권정비계획법상 과밀억제권역 (경기는 시 단위, 남양주·시흥은 일부만 해당)
OVERCROWDED_GG = ("의정부시", "구리시", "하남시", "고양시", "수원시", "성남시", "안양시", "부천시", "광명시",
                  "과천시", "의왕시", "군포시")
OVERCROWDED_GG_PART = ("남양주시", "시흥시")
PUBLIC_DEVELOPERS = ("LH", "한국토지주택공사", "SH", "서울주택도시공사", "GH", "경기주택도시공사", "공공분양", "신혼희망", "뉴:홈", "뉴홈")


def _is_public_land(n: dict) -> bool | None:
    f = n.get("flags") or {}
    if any(f.get(k) == "Y" for k in ("public_zone", "large_site", "capital_public")):
        return True
    if f.get("redevelop") == "Y":
        return False
    text = f"{n.get('name') or ''} {n.get('builder') or ''} {n.get('kind') or ''}"
    if any(k in text for k in PUBLIC_DEVELOPERS) or "국민" in (n.get("kind") or ""):
        return True
    if any(k in f for k in ("public_zone", "large_site")):
        return False
    return None


def _overcrowded(n: dict) -> bool | None:
    reg, sgg = n.get("region"), n.get("sigungu") or ""
    if reg == "서울":
        return True
    if reg == "경기":
        if any(sgg.startswith(x) for x in OVERCROWDED_GG):
            return True
        if any(sgg.startswith(x) for x in OVERCROWDED_GG_PART):
            return None
        return False if sgg else None
    return None


NOTICE_RULES = ROOT / "data" / "notice_rules.csv"
_rules_cache: dict | None = None


def notice_rules() -> dict:
    """data/notice_rules.csv — 공고문에서 직접 확인한 전매·거주의무·재당첨 값 (공식 url인 줄만 사용)."""
    global _rules_cache
    if _rules_cache is None:
        _rules_cache = {}
        if NOTICE_RULES.exists():
            with NOTICE_RULES.open(encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if sources.is_official(r.get("source_url")) and r.get("house_manage_no"):
                        _rules_cache[r["house_manage_no"].strip()] = r
    return _rules_cache


def restrictions(n: dict) -> dict:
    """당첨 후 묶이는 조건을 추정한다. {resale_years, reside_years, rewin_years, …, basis/notes}.
    값이 범위로만 정해지면 *_min/*_max 와 함께 확정되지 않았음을 표시한다."""
    if n.get("source") in ("remndr", "opt"):
        return {"note": "무순위·임의공급은 원래 분양 공고의 전매제한·실거주의무를 그대로 따릅니다. 공고문 원문에서 확인하세요", "confirmed": False}
    level, _ = regulation(n)
    f = n.get("flags") or {}
    capital = n.get("region") in ("서울", "경기", "인천")
    public_land = _is_public_land(n)
    cap = f.get("price_cap")
    notes: list[str] = []

    # 전매제한 (주택법 시행령 별표 3, 2023.4 개정)
    if capital:
        if public_land or level in ("투기과열", "조정"):
            resale, why = 3.0, "수도권 공공택지·규제지역"
        elif _overcrowded(n):
            resale, why = 1.0, "수도권 과밀억제권역"
        elif _overcrowded(n) is None:
            resale, why = 0.5, "수도권 그 밖의 지역(과밀억제권역이면 1년)"
        else:
            resale, why = 0.5, "수도권 그 밖의 지역"
    else:
        resale, why = (1.0, "비수도권 공공택지·규제지역") if (public_land or level in ("투기과열", "조정")) else (0.5, "비수도권(지역에 따라 없음)")

    # 거주의무 (주택법 제57조의2) — 수도권 분양가상한제 주택
    reside_min = reside_max = 0.0
    ratio = None
    m = n.get("market") or {}
    if m.get("expected") and m.get("price_rep"):
        ratio = m["price_rep"] / m["expected"]
    if capital and (cap == "Y" or (cap is None and public_land)):
        hi, lo = (5.0, 3.0) if public_land else (3.0, 2.0)
        reside_min, reside_max = lo, hi      # 80% 기준은 공식 감정 시세로 정해져 공고문에서만 확정됨
        if ratio is not None:
            notes.append(f"참고: 분양가가 추정 시세의 {ratio * 100:.0f}% (80% 미만이면 {hi:g}년, 이상이면 {lo:g}년)")
        reside_why = ("공공택지" if public_land else "민간택지") + " 분양가상한제 주택"
        if cap is None:
            reside_why += "(상한제 여부 미표기 → 공공분양으로 추정)"
        notes.append("거주 시작 시점은 법 개정으로 입주 후 일정 기간 미룰 수 있는 경우가 있음 — 공고문 확인")
    else:
        reside_why = "분양가상한제 미적용" if cap == "N" else ("비수도권" if not capital else "상한제 표기 없음")

    # 재당첨 제한 (주택공급에 관한 규칙 제54조)
    if level == "투기과열" or cap == "Y":
        rewin = 10
    elif level == "조정":
        rewin = 7
    else:
        rewin = 0

    out = {
        "resale_years": resale, "resale_basis": why,
        "reside_min": reside_min, "reside_max": reside_max, "reside_basis": reside_why,
        "rewin_years": rewin,
        "public_land": public_land, "price_cap": cap,
        "notes": notes, "confirmed": False,
        "law": {"resale": LAW_RESALE, "reside": LAW_RESIDE, "rewin": LAW_REWIN},
    }
    ov = notice_rules().get(str(n.get("house_manage_no") or ""))
    if ov:
        def num(k):
            try:
                return float(ov[k])
            except (TypeError, ValueError, KeyError):
                return None
        if num("resale_years") is not None:
            out["resale_years"], out["resale_basis"] = num("resale_years"), "공고문 기재"
        if num("reside_years") is not None:
            out["reside_min"] = out["reside_max"] = num("reside_years")
            out["reside_basis"] = "공고문 기재"
        if num("rewin_years") is not None:
            out["rewin_years"] = int(num("rewin_years"))
        out["confirmed"], out["source_url"] = True, ov["source_url"]
        out["notes"] = [ov.get("memo")] if ov.get("memo") else []
    return out


def years_txt(y: float) -> str:
    return "없음" if not y else (f"{int(y * 12)}개월" if y < 1 else f"{y:g}년")
