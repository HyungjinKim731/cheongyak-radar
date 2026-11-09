"""줍줍(무순위·잔여세대·임의공급·계약취소주택) 알림.

언제 보내나
  · 새 줍줍 공고가 청약홈에 올라왔을 때 (서울·경기, config 의 지역)
  · 접수 하루 전(D-1) 아침 갱신 때 한 번 더
어디로 보내나 (설정된 것 모두)
  · 텔레그램: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  → 휴대폰 푸시, 가장 빠름
  · GitHub 이슈: GitHub Actions 안에서 GITHUB_TOKEN 으로 저장소에 이슈를 만듦
                → 저장소 주인에게 GitHub 앱 푸시·메일 알림 (추가 설정 없음)
중복 방지: data/state/notified.json. 처음 실행 때는 이미 있는 공고를 '본 것'으로만 기록하고 보내지 않음.
기본은 GitHub Actions 에서만 보냄(PC에서 serve.py 와 동시에 돌 때 두 번 오지 않도록). config [notify] local = true 로 PC에서도 보냄.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

import requests

from .util import ROOT, read_json, today_kst, write_json

log = logging.getLogger(__name__)
STATE = ROOT / "data" / "state" / "notified.json"
ZUP_SOURCES = ("remndr", "opt")


def _eok(man: int | None) -> str:
    if not man:
        return "-"
    return f"{man / 10000:.2f}".rstrip("0").rstrip(".") + "억" if man >= 10000 else f"{man:,}만"


def _first(s: dict, label: str = "청약접수") -> dict | None:
    return next((e for e in s.get("events", []) if e["label"] == label), None) or (s.get("events") or [None])[0]


def focus_ok(s: dict, focus: dict) -> bool:
    """화면 기본값과 같은 기준: 핵심지역만 + 주변 시세(실거래 거리 기반) N억 이상. 시세 근거가 없으면 알림은 보냄."""
    from . import rules
    if focus.get("core_only", True) and not rules.is_core(s, focus):
        return False
    m = s.get("market") or {}
    lim = (focus.get("min_expected_eok", 10) or 0) * 10000
    if lim and m.get("method") == "distance" and m.get("expected") and m["expected"] < lim:
        return False
    return True


def message(s: dict, kind: str, site: str | None) -> str:
    ev = _first(s)
    price = (_eok(s.get("price_min")) + ("" if s.get("price_min") == s.get("price_max") else f"~{_eok(s.get('price_max'))}")
             if s.get("price_min") else "공고문 확인")
    head = "🆕 줍줍 공고" if kind == "new" else "⏰ 내일 줍줍 접수"
    m = s.get("market") or {}
    lines = [
        f"{head} · {s.get('source_label') or s.get('kind')}",
        f"{s['name']} ({s.get('region')} {s.get('sigungu') or ''}, {s.get('regulation') or ''})",
        f"접수 {ev['start'] if ev else '-'}" + (f"~{ev['end']}" if ev and ev['end'] != ev['start'] else "")
        + f" · {s.get('units') or '-'}세대 · {price}",
    ]
    if m.get("method") == "distance" and m.get("margin_pct") is not None:
        lines.append(f"주변 시세 대비 {m['margin_pct']:+.0f}% (실거래 {m.get('nearby', {}).get('radius_km')}km 비교)")
    lines.append("자격: 성년·무주택세대구성원 / 거주요건은 공고문")
    lines.append(f"공고 원문: {s.get('url')}")
    if site:
        lines.append(f"레이더: {site}#zupzup")
    return "\n".join(lines)


def telegram(text: str) -> bool:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (tok and chat):
        return False
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
    if r.status_code != 200:
        log.warning("텔레그램 전송 실패 %s: %s", r.status_code, r.text[:200])
        return False
    return True


def github_issue(title: str, body: str) -> bool:
    tok, repo = os.environ.get("GITHUB_TOKEN", "").strip(), os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not (tok and repo):
        return False
    owner = repo.split("/")[0]
    r = requests.post(f"https://api.github.com/repos/{repo}/issues",
                      headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body + f"\n\n@{owner}", "labels": ["줍줍"]}, timeout=15)
    if r.status_code == 422:      # 라벨 문제면 라벨 없이 다시
        r = requests.post(f"https://api.github.com/repos/{repo}/issues",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                          json={"title": title, "body": body + f"\n\n@{owner}"}, timeout=15)
    if r.status_code not in (200, 201):
        log.warning("GitHub 이슈 생성 실패 %s: %s", r.status_code, r.text[:200])
        return False
    return True


def run(schedules: list[dict], cfg: dict, regions: list[str]) -> list[str]:
    """보낸 알림 요약 목록을 반환."""
    ncfg = cfg.get("notify", {})
    if not ncfg.get("enabled", True):
        return []
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if not in_actions and not ncfg.get("local", False):
        return []
    state = read_json(STATE, None)
    first_run = state is None
    state = state or {"new": [], "d1": []}
    today = today_kst()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    site = ncfg.get("site_url") or (
        f"https://{os.environ['GITHUB_REPOSITORY'].split('/')[0].lower()}.github.io/{os.environ['GITHUB_REPOSITORY'].split('/')[1]}/"
        if os.environ.get("GITHUB_REPOSITORY") else None)
    sent = []
    for s in schedules:
        if s.get("source") not in ZUP_SOURCES or (regions and s.get("region") not in regions):
            continue
        if not focus_ok(s, cfg.get("focus", {})):
            continue
        ev = _first(s)
        if not ev or ev["end"] < today.isoformat():
            continue
        jobs = []
        if s["id"] not in state["new"]:
            jobs.append("new")
        if ncfg.get("remind_d1", True) and ev["start"] == tomorrow and s["id"] not in state["d1"]:
            jobs.append("d1")
        for kind in jobs:
            state["new" if kind == "new" else "d1"].append(s["id"])
            if first_run:
                continue
            text = message(s, kind, site)
            ok_tg = telegram(text)
            ok_gh = ncfg.get("github_issue", True) and github_issue(text.splitlines()[0] + " " + s["name"], text)
            if ok_tg or ok_gh:
                sent.append(f"{kind}:{s['name']}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["new"], state["d1"] = state["new"][-500:], state["d1"][-500:]
    write_json(STATE, state)
    if first_run:
        log.info("알림: 첫 실행이라 지금 있는 줍줍 공고는 기록만 하고 보내지 않음")
    elif sent:
        log.info("알림 %d건 보냄: %s", len(sent), ", ".join(sent[:5]))
    return sent
