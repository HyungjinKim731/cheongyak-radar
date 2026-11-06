"""여러 출처에서 모은 같은 수치의 통계량.

같은 항목(예: '아크메르 동탄 1순위 최고 경쟁률')에 대해 출처별 값을 넣으면
출처 수, 최솟값·최댓값·평균·중앙값·표준편차, 최빈값과 그 값에 동의한 출처 수, 일치 여부를 돌려준다.
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any


def summarize(values: list[dict], tolerance: float = 0.05) -> dict:
    """values: [{"value": 16.88, "source": "헤럴드경제", "url": "...", "date": "2026-10-28", "note": "..."}]

    tolerance: 최댓값/최솟값 차이가 이 비율 이하면 '일치'로 본다(반올림 차이 흡수).
    """
    nums = [float(v["value"]) for v in values if isinstance(v.get("value"), (int, float))]
    out: dict[str, Any] = {"n": len(values), "n_numeric": len(nums)}
    if not values:
        out["agreement"] = "없음"
        return out
    keyed = Counter(_key(v["value"]) for v in values)
    mode, mode_n = keyed.most_common(1)[0]
    out.update({"mode": mode, "mode_n": mode_n})
    if nums:
        lo, hi = min(nums), max(nums)
        out.update({
            "min": round(lo, 4), "max": round(hi, 4),
            "mean": round(statistics.fmean(nums), 4),
            "median": round(statistics.median(nums), 4),
            "stdev": round(statistics.stdev(nums), 4) if len(nums) > 1 else 0.0,
        })
        spread = (hi - lo) / abs(lo) if lo else (0.0 if hi == lo else 1.0)
        out["spread_pct"] = round(spread * 100, 1)
        agree = spread <= tolerance
    else:
        agree = len(keyed) == 1
    if len(values) == 1:
        out["agreement"] = "단일 출처"
    elif agree:
        out["agreement"] = "일치"
    elif mode_n / len(values) >= 0.6:
        out["agreement"] = "대체로 일치"
    else:
        out["agreement"] = "불일치"
    out["outliers"] = [v.get("source") for v in values if _key(v["value"]) != mode] if out["agreement"] != "일치" else []
    return out


def _key(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2)
    return v


def fact(key: str, label: str, unit: str, values: list[dict], note: str | None = None,
         tolerance: float = 0.05) -> dict:
    return {"key": key, "label": label, "unit": unit, "note": note,
            "values": values, "stats": summarize(values, tolerance)}
