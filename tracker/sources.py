"""출처 신뢰도 — 정부·지자체·공공기관의 공식 발표만 '근거'로 쓰고 링크한다.

공식으로 인정하는 곳
  · 정부·지자체: *.go.kr (국토교통부, 금융위, 서울시, 경기도, 각 구청, 국가법령정보센터, 공공데이터포털 …)
  · 정책브리핑: korea.kr
  · 공공기관: 청약홈(한국부동산원 운영), 한국부동산원, LH, SH, GH, HUG, 통계청 KOSIS
그 밖의 언론·블로그·민간 앱은 근거로 쓰지 않고 링크도 달지 않는다.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

OFFICIAL_SUFFIX = (".go.kr",)
OFFICIAL_HOSTS = {
    "korea.kr": "정책브리핑",
    "applyhome.co.kr": "청약홈",
    "reb.or.kr": "한국부동산원",
    "lh.or.kr": "LH",
    "i-sh.co.kr": "SH",
    "gh.or.kr": "GH",
    "khug.or.kr": "HUG",
    "kosis.kr": "통계청 KOSIS",
}
GOV_NAMES = {
    "molit.go.kr": "국토교통부", "fsc.go.kr": "금융위원회", "law.go.kr": "국가법령정보센터",
    "data.go.kr": "공공데이터포털", "seoul.go.kr": "서울시", "gg.go.kr": "경기도", "myhome.go.kr": "마이홈(국토부)",
}


def host(url: str | None) -> str:
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _match(h: str, base: str) -> bool:
    return h == base or h.endswith("." + base)


def is_official(url: str | None) -> bool:
    h = host(url)
    if not h:
        return False
    return h.endswith(OFFICIAL_SUFFIX) or any(_match(h, b) for b in OFFICIAL_HOSTS)


def org(url: str | None) -> str | None:
    """공식 출처면 기관 이름."""
    h = host(url)
    for b, name in {**GOV_NAMES, **OFFICIAL_HOSTS}.items():
        if _match(h, b):
            return name
    if h.endswith(".go.kr"):
        return h
    return None


# 공식 링크가 아니어도 '근거'가 아닌 기능 링크로 허용하는 키 (사업주체 홈페이지는 청약홈 공고에 적힌 주소)
FUNCTIONAL_KEYS = {"homepage"}
LINK_KEYS = {"url", "source_url", "map_url", "homepage", "link"}
OFFICIAL_SITES_QUERY = "(site:korea.kr OR site:go.kr OR site:applyhome.co.kr OR site:lh.or.kr OR site:i-sh.co.kr OR site:gh.or.kr OR site:reb.or.kr)"


def link_ok(d: dict, key: str, v: str) -> bool:
    if key in FUNCTIONAL_KEYS or is_official(v):
        return True
    # 구글 뉴스 링크는 원문(공식 기관)으로 넘어가는 중계 주소 → 원문 출처가 공식일 때만 허용
    return host(v) == "news.google.com" and is_official(d.get("source_url"))


def sanitize(obj, path: str = "", dropped: list | None = None):
    """data.json 전체를 훑어 비공식 링크를 없앤다.
    - 근거 목록(값 + 출처 url)의 항목이 비공식이면 항목째 제외
    - 그 밖의 비공식 url 필드는 지움
    """
    if dropped is None:
        dropped = []
    if isinstance(obj, list):
        out = []
        for i, x in enumerate(obj):
            if isinstance(x, dict) and x.get("url") and ("value" in x or "title" in x) and not link_ok(x, "url", x["url"]):
                dropped.append(f"{path}[{i}] {host(x['url'])}")
                continue
            out.append(sanitize(x, f"{path}[{i}]", dropped))
        return out
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in LINK_KEYS and isinstance(v, str) and v.startswith("http"):
                if link_ok(obj, k, v):
                    out[k] = v
                else:
                    dropped.append(f"{path}.{k} {host(v)}")
                continue
            out[k] = sanitize(v, f"{path}.{k}", dropped)
        return out
    return obj


ARTICLE_RE = re.compile(r"제\d+조(의\d+)?")
