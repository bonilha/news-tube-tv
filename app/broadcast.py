"""Transmission length and the local-clock start schedule.

With the schedule off, the five-file window stays open so the operator can
start by hand. A daily or one-shot slot keeps downloads closed until the
cycle is running or the next start is inside the fifteen minutes before it
(or already due and waiting for an MP4).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.database import get_db

log = logging.getLogger(__name__)

PREP_MINUTES = 15
DEFAULT_LIMIT_SECONDS = 12 * 60 * 60

_gate_holding = False


def note_cycle_started() -> None:
    """The cycle is on, so the five-file window may download."""
    global _gate_holding
    if not _gate_holding:
        return
    from app.queue import download as queue_download
    queue_download.release_downloads()
    _gate_holding = False


def hold_until_needed() -> None:
    """Close downloads after a graceful stop until the cycle or the schedule needs them."""
    global _gate_holding
    if _gate_holding:
        return
    from app.queue import download as queue_download
    queue_download.hold_downloads()
    _gate_holding = True


def _clock(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now()


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    parts = (text or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute


def _on_day(day, hhmm: str) -> datetime | None:
    parsed = _parse_hhmm(hhmm)
    if parsed is None:
        return None
    hour, minute = parsed
    return datetime(day.year, day.month, day.day, hour, minute)


def _valid_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _catchable(start: datetime, now: datetime, armed_for: str) -> bool:
    """Inside the 15 minutes around the start, or already armed and still waiting."""
    delta = (now - start).total_seconds()
    if -PREP_MINUTES * 60 <= delta <= PREP_MINUTES * 60:
        return True
    key = start.isoformat(timespec="minutes")
    return bool(armed_for) and armed_for == key and delta > 0


def scheduled_start(row: dict, now: datetime) -> datetime | None:
    """The start this process may still catch. None when the slot was missed or already used."""
    mode = row.get("schedule_mode") or "off"
    if mode == "daily":
        start = _on_day(now.date(), row.get("schedule_time") or "")
        if start is None:
            return None
        if (row.get("last_fired_on") or "") == now.date().isoformat():
            return None
        if _catchable(start, now, row.get("armed_for") or ""):
            return start
        return None
    if mode == "once":
        try:
            day = datetime.strptime(row.get("schedule_date") or "", "%Y-%m-%d").date()
        except ValueError:
            return None
        start = _on_day(day, row.get("schedule_time") or "")
        if start is None:
            return None
        if (row.get("last_fired_on") or "") == start.date().isoformat():
            return None
        if _catchable(start, now, row.get("armed_for") or ""):
            return start
        return None
    return None


def decide(
    now: datetime,
    row: dict,
    *,
    cycle_on: bool,
    streaming: bool,
    ready: bool,
) -> dict:
    """What the 15s tick should do. Pure: no clock and no database."""
    start = scheduled_start(row, now)
    # Off means manual operation. The start button needs an MP4, so the
    # window has to fill before the cycle can turn on.
    mode = row.get("schedule_mode") or "off"
    open_downloads = bool(cycle_on) or mode == "off"
    start_now = False
    armed_for = row.get("armed_for") or ""
    last_fired_on = None
    schedule_mode = None
    if start is not None:
        key = start.isoformat(timespec="minutes")
        open_downloads = True
        if now >= start and ready and not streaming:
            start_now = True
            last_fired_on = start.date().isoformat()
            armed_for = ""
            if (row.get("schedule_mode") or "") == "once":
                schedule_mode = "off"
        else:
            armed_for = key
    elif (row.get("schedule_mode") or "") == "once":
        missed = _on_day(
            datetime.strptime(row["schedule_date"], "%Y-%m-%d").date(),
            row.get("schedule_time") or "",
        ) if _valid_date(row.get("schedule_date") or "") else None
        if missed is not None and now > missed + timedelta(minutes=PREP_MINUTES):
            schedule_mode = "off"
            armed_for = ""
    return {
        "open_downloads": open_downloads,
        "start": start_now,
        "armed_for": armed_for,
        "last_fired_on": last_fired_on,
        "schedule_mode": schedule_mode,
    }


def validate_schedule(mode: str, time_text: str, date_text: str, now: datetime) -> dict:
    """Raise ValueError with a Portuguese message, or return the columns to store."""
    mode = (mode or "off").strip()
    if mode == "off":
        return {
            "schedule_mode": "off",
            "schedule_time": "",
            "schedule_date": "",
            "armed_for": "",
        }
    if _parse_hhmm(time_text) is None:
        raise ValueError("Informe a hora no formato HH:MM.")
    if mode == "daily":
        return {
            "schedule_mode": "daily",
            "schedule_time": time_text.strip(),
            "schedule_date": "",
            "armed_for": "",
            "last_fired_on": "",
        }
    if mode == "once":
        try:
            day = datetime.strptime((date_text or "").strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Informe a data no formato AAAA-MM-DD.") from exc
        start = _on_day(day.date(), time_text)
        if start is None or start <= now:
            raise ValueError("A data e a hora precisam estar no futuro.")
        return {
            "schedule_mode": "once",
            "schedule_time": time_text.strip(),
            "schedule_date": day.date().isoformat(),
            "armed_for": "",
            "last_fired_on": "",
        }
    raise ValueError("Modo de agendamento desconhecido.")


async def current_limit_seconds() -> int:
    row = await get_control()
    return int(row.get("limit_seconds") or DEFAULT_LIMIT_SECONDS)


async def get_control() -> dict:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM broadcast_control WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO broadcast_control (id, limit_seconds) VALUES (1, ?)",
                (DEFAULT_LIMIT_SECONDS,),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM broadcast_control WHERE id = 1")
            row = await cursor.fetchone()
    return dict(row)


async def save_limit_hours(hours: float) -> None:
    if hours <= 0:
        raise ValueError("A duração tem de ser maior que zero.")
    seconds = int(round(float(hours) * 3600))
    async with get_db() as db:
        await db.execute(
            "UPDATE broadcast_control SET limit_seconds = ? WHERE id = 1",
            (seconds,),
        )
        await db.commit()


async def save_schedule(mode: str, time_text: str, date_text: str, now: datetime | None = None) -> None:
    fields = validate_schedule(mode, time_text, date_text, _clock(now))
    columns = ", ".join(f"{name} = ?" for name in fields)
    async with get_db() as db:
        await db.execute(
            f"UPDATE broadcast_control SET {columns} WHERE id = 1",
            tuple(fields.values()),
        )
        await db.commit()


async def _store_decision(decision: dict) -> None:
    sets = []
    values = []
    for name in ("armed_for", "last_fired_on", "schedule_mode"):
        if decision.get(name) is not None:
            sets.append(f"{name} = ?")
            values.append(decision[name])
    if not sets:
        return
    async with get_db() as db:
        await db.execute(
            f"UPDATE broadcast_control SET {', '.join(sets)} WHERE id = 1",
            tuple(values),
        )
        await db.commit()


def apply_download_choice(open_downloads: bool, cycle_on: bool) -> None:
    global _gate_holding
    from app.queue import download as queue_download
    if open_downloads or cycle_on:
        if _gate_holding:
            queue_download.release_downloads()
            _gate_holding = False
        return
    hold_until_needed()


async def start_transmission() -> dict:
    """Same path as the dashboard button Transmissão ao vivo."""
    from app.cycle import cycle_manager
    from app.obs.manager import obs_manager

    armed = await cycle_manager.enable()
    if not armed.get("ok"):
        return {
            "ok": False,
            "error": armed.get("error") or "A sequência não pode começar.",
        }
    ok, error = await obs_manager.start_streaming()
    if not ok and error:
        cycle_manager.set_message(error, error=True)
    elif ok:
        cycle_manager.set_message("Transmissão no ar, na mesma sequência do preview.")
    return {"ok": ok, "error": error}


async def tick(now: datetime | None = None, *, ready: bool | None = None) -> dict:
    """One scheduler pass. Returns the decision so tests can read it."""
    from app.cycle import cycle_manager
    from app.obs.manager import obs_manager
    from app.queue import service as queue_svc

    moment = _clock(now)
    row = await get_control()
    if ready is None:
        ready = await queue_svc.broadcast_ready()
    decision = decide(
        moment,
        row,
        cycle_on=cycle_manager.is_enabled,
        streaming=bool(obs_manager.is_streaming),
        ready=ready,
    )
    apply_download_choice(decision["open_downloads"], cycle_manager.is_enabled)
    await _store_decision(decision)
    if decision["start"]:
        result = await start_transmission()
        decision["started"] = bool(result.get("ok"))
        if not result.get("ok"):
            log.warning("Scheduled start did not go on air: %s", result.get("error"))
    return decision


def format_hm(seconds: int) -> str:
    total = max(0, int(seconds))
    hours, minutes = divmod(total // 60, 60)
    return f"{hours} h {minutes:02d} min"
