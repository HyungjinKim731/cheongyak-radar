"""공통 유틸: 날짜 정규화, KST 기준 오늘, 재시도 HTTP."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any

import requests

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def now_kst_iso() -> str:
    return dt.datetime.now(KST).replace(microsecond=0).isoformat()


_DATE_RE = re.compile(r"(\d{4})[-./]?(\d{1,2})[-./]?(\d{1,2})")


def to_iso(value: Any) -> str | None:
    """'2026-08-24', '20260824', datetime, '2026.8.24' → '2026-08-24'. 실패 시 None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    s = str(value).strip()
    if not s or s in {"-", "0", "null", "None"}:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return None


def http_get_json(session: requests.Session, url: str, params: dict, attempts: int = 3,
                  timeout: int = 30) -> dict:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            res = session.get(url, params=params, timeout=timeout)
            data = res.json()
            # odcloud는 인증/파라미터 오류를 {"code": -401, "msg": ...}로 돌려준다
            if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
                raise ApiError(f"API 오류 {data['code']}: {data.get('msg', '')}")
            res.raise_for_status()
            return data
        except ApiError:
            raise
        except Exception as e:  # 네트워크·JSON 오류는 재시도
            last = e
            time.sleep(2 * i)
    raise RuntimeError(f"요청 실패 {url}: {last}")


class ApiError(RuntimeError):
    pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
            else json.dumps(data, ensure_ascii=False, indent=1))
    path.write_text(text, encoding="utf-8")
