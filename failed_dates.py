"""실패한 TARGET_DATE 추적 — cron 실행 시 소급 재시도용.

저장 위치: state/failed_dates.json
형식:
    {
      "YYYY-MM-DD": {
        "first_failed_at": ISO8601,
        "last_attempted_at": ISO8601,
        "attempts": int,
        "last_error": str
      }
    }

7일 룰: first_failed_at 기준 GIVE_UP_AFTER_DAYS 경과 시 목록에서 제거 + 경고.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
STATE_FILE = Path("state") / "failed_dates.json"
GIVE_UP_AFTER_DAYS = 7


def _load():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[failed_dates] 로드 실패 (빈 상태로 시작): {e}")
        return {}


def _save(data):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def record(target_date, error_message):
    """실패 날짜 추가/업데이트."""
    data = _load()
    now = datetime.now(KST).isoformat()
    entry = data.get(target_date) or {"first_failed_at": now, "attempts": 0}
    entry["last_attempted_at"] = now
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = (str(error_message)[:500] if error_message else "")
    data[target_date] = entry
    _save(data)
    logger.info(f"[failed_dates] 기록: {target_date} (attempts={entry['attempts']})")


def clear(target_date):
    """성공한 날짜를 목록에서 제거."""
    data = _load()
    if target_date in data:
        del data[target_date]
        _save(data)
        logger.info(f"[failed_dates] 성공으로 제거: {target_date}")


def pending_for_retry(exclude_date):
    """소급 재시도 대상 (정렬된 날짜 리스트). 7일 경과분은 포기 처리."""
    data = _load()
    now = datetime.now(KST)
    cutoff = now - timedelta(days=GIVE_UP_AFTER_DAYS)

    eligible = []
    expired = []
    for date_str, info in list(data.items()):
        if date_str == exclude_date:
            continue
        try:
            first = datetime.fromisoformat(info.get("first_failed_at", ""))
        except ValueError:
            first = now
        if first < cutoff:
            expired.append(date_str)
        else:
            eligible.append(date_str)

    if expired:
        for d in expired:
            logger.warning(
                f"[failed_dates] {GIVE_UP_AFTER_DAYS}일 경과 포기: {d} (info={data[d]})"
            )
            del data[d]
        _save(data)

    return sorted(eligible)
