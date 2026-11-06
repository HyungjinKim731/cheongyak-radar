"""공고마다 '조건 쉽게 보기'를 만든다.

청약홈 공고의 투기과열·조정·분양가상한제 표기와 공고 유형(국민/민영/무순위/임의공급), 면적대를 바탕으로
초보자가 먼저 확인할 항목을 문장으로 정리한다. 최종 기준은 항상 입주자모집공고문이다.
"""
from __future__ import annotations

from .guide import REGULATED


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
    lst = REGULATED.get(reg or "", [])
    if "*" in lst or (sgg and any(sgg.startswith(x) for x in lst)):
        return "투기과열", "10·15 대책 지역 목록으로 추정"
    if reg in ("서울", "경기", "인천"):
        return "비규제", "10·15 대책 지역 목록으로 추정"
    return "미확인", "정보 없음"


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
    if level in ("투기과열", "조정"):
        out.append({"title": "당첨 후 제한", "tone": "warn",
                    "body": "전매제한 3년(수도권), 재당첨 제한 최장 10년. 실거주의무는 공고문 확인", "facts": ["resale_years", "rewin_years"]})
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
