"""Cycle manager — bumper → video → bumper playback loop driven by OBS events."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from enum import Enum
from pathlib import Path

from app.config import settings
from app.obs.manager import SCENE_BUMPER, SCENE_PROGRAM, obs_manager
from app.overlay import service as overlay
from app.queue import cookies as yt_cookies
from app.queue import download as queue_download
from app.queue.download import ready_wait_seconds
from app.queue import service as queue_svc

log = logging.getLogger(__name__)

PLAYER_SOURCE = "NewsTube Fila"
_MEDIA_RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
_MEDIA_KINDS = {"ffmpeg_source", "vlc_source"}
_NO_FILE = "Não há arquivo para tocar."
_OBS_STALE = "O OBS não responde. O player da fila vai tentar reconectar."
_STALLED = "A mídia parou de avançar no OBS. Ciclo rearmado."
_NO_MEDIA_GRACE = 10.0
_NO_PROGRESS_SEC = 90.0
_BLIND_POLLS = 3
_RETRY_WAIT_SEC = 5
# While OBS is down, do not burn through the pass marking every video aired.
_OBS_BACKOFF_SEC = 30
_POLL_SEC = 1.0
# Playing, opening, buffering. A paused media is not active: it never ends itself.
_ACTIVE_STATES = {
    "OBS_MEDIA_STATE_PLAYING",
    "OBS_MEDIA_STATE_OPENING",
    "OBS_MEDIA_STATE_BUFFERING",
}
_NO_MEDIA_STATES = {
    "",
    "OBS_MEDIA_STATE_NONE",
    "OBS_MEDIA_STATE_EMPTY",
}


class _State(Enum):
    STOPPED = "stopped"
    BUMPER = "bumper"
    VIDEO = "video"


def _playable(row: dict) -> bool:
    path = row.get("local_path") or ""
    return bool(path) and Path(path).is_file()


def _pass_rows(rows: list[dict], aired: set[str], playing_id: str = "") -> list[dict]:
    """Rows still ahead this pass that can be downloaded. The rest do not block.

    A disabled channel stays out. The id already on air is kept so that video
    can finish; the next pick after it ends does not use this exception.
    """
    return [
        row for row in rows
        if (row.get("video_id") or "") not in aired
        and not queue_download.download_blocked(row)
        and (
            not queue_download.channel_disabled(row)
            or (playing_id and (row.get("video_id") or "") == playing_id)
        )
    ]


def next_in_pass(
    rows: list[dict], aired: set[str], playing_id: str = "",
) -> tuple[dict | None, bool]:
    """Next row this pass, or the first row when every downloadable row already aired.

    The second value is true when the choice is the start of the next lap.
    A downloadable row without an MP4 is still the choice. The caller waits for that file.
    A row that cannot be downloaded is skipped.
    """
    unplayed = _pass_rows(rows, aired, playing_id)
    if unplayed:
        return unplayed[0], False
    lap = _pass_rows(rows, set(), playing_id)
    if not lap:
        return None, False
    return lap[0], True


def _now() -> float:
    return time.monotonic()


def tail_scan_due(unplayed: int, limit: int, already: bool) -> bool:
    """One channel scan when this pass has limit videos left, or fewer."""
    return unplayed > 0 and limit > 0 and unplayed <= limit and not already


class CycleManager:
    """Singleton. Controls the bumper↔video playback cycle."""

    _instance: CycleManager | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._enabled = False
            cls._instance._state = _State.STOPPED
            cls._instance._task = None
            cls._instance._media_ended = None
            cls._instance._current_video_title = ""
            cls._instance._message = ""
            cls._instance._bumper_input = ""
            cls._instance._wait_input = ""
            cls._instance._pass_items = []
            cls._instance._current_id = ""
            cls._instance._aired = set()
            cls._instance._tail_scanned = False
            cls._instance._ready_count = 0
            cls._instance._retry_all_errors = False
            cls._instance._error = False
            cls._instance._wait_budget = 0.0
            cls._instance._unready_since = {}
            cls._instance._listening = False
        return cls._instance

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> str:
        return self._state.value

    @property
    def current_video_title(self) -> str:
        return self._current_video_title

    def status_dict(self) -> dict:
        return {
            "enabled": self._enabled,
            "state": self._state.value,
            "current_video_title": self._current_video_title,
            "current_video_id": self._current_id,
            "aired_ids": sorted(self._aired),
            "message": self._message,
            "error": self._error,
            "finish_requested": bool(getattr(self, "_finish_requested", False)),
            "on_air_seconds": int(getattr(self, "_on_air_seconds", 0)),
            "limit_seconds": int(getattr(self, "_limit_seconds", 0) or 0),
        }

    def broadcast_marks(self) -> dict:
        return {"on_air": self._current_id, "aired": set(self._aired)}

    async def marks_for(self, items: list[dict]) -> dict:
        """Row badges from the cycle, corrected by the file OBS is actually playing."""
        on_air = self._current_id
        aired = set(self._aired)
        try:
            settings_resp = await obs_manager.get_input_settings(PLAYER_SOURCE)
            media = await obs_manager.get_media_input_status(PLAYER_SOURCE)
        except Exception:
            log.debug("Could not read player state from OBS")
            return {"on_air": on_air, "aired": aired}
        local = str((settings_resp.get("inputSettings") or {}).get("local_file") or "")
        state = str(media.get("mediaState") or "")
        match_id = ""
        if local:
            try:
                playing_path = str(Path(local).resolve())
            except OSError:
                playing_path = local
            for row in items:
                path = row.get("local_path") or ""
                if not path:
                    continue
                try:
                    row_path = str(Path(path).resolve())
                except OSError:
                    row_path = path
                if row_path == playing_path:
                    match_id = row.get("video_id") or ""
                    break
        if match_id and state in _ACTIVE_STATES:
            on_air = match_id
        elif match_id and state == "OBS_MEDIA_STATE_ENDED":
            aired.add(match_id)
            if on_air == match_id:
                on_air = ""
        return {"on_air": on_air, "aired": aired}

    def set_message(self, message: str, error: bool = False) -> None:
        self._message = message
        self._error = error

    def _blocked_reason(self) -> str:
        """Why no MP4 is arriving, for the operator message. '' when unclear."""
        cookie = yt_cookies.status()
        if not cookie["ok"] and cookie["message"]:
            return cookie["message"]
        if not queue_download.selected_encoder():
            return "Nenhum codificador H.264 encontrado para o yt-dlp."
        return ""

    def _protect(self) -> None:
        """Video ids still needed in this pass, including the one on air."""
        ids = {row["video_id"] for row in self._pass_items if row.get("video_id")}
        if self._current_id:
            ids.add(self._current_id)
        queue_download.protect_video_ids(ids)

    def _publish_keep_skip(self) -> None:
        """Aired videos fill the window only after the unplayed ones run out."""
        queue_download.note_on_air(self._current_id)
        queue_download.skip_keep_ids(set(self._aired))
        queue_download.schedule_sync()

    async def enable(self) -> dict:
        """Preview the queue in OBS: bumper, video, bumper. Does not start RTMP."""
        if self._enabled and self._task and not self._task.done():
            return {
                "ok": True,
                "scene": SCENE_PROGRAM,
                "source": PLAYER_SOURCE,
                "message": "Preview da fila já está no OBS.",
            }
        bumper = await self._bumper_source()
        if bumper is None:
            return {"ok": False, "error": self._message}
        file_path = await self._first_playable_path()
        if not file_path:
            self._message = _NO_FILE
            self._error = True
            return {"ok": False, "error": self._message}
        if not await self._ensure_player_source(file_path):
            self._message = "Não foi possível criar a fonte NewsTube Fila em SCENE_PROGRAM."
            self._error = True
            return {"ok": False, "error": self._message}
        try:
            await overlay.ensure_overlay()
        except Exception:
            log.exception("Lower third was not created")

        self._bumper_input = bumper
        # A looping bumper never fires MediaInputPlaybackEnded. Without the event
        # the only exit is sampling the cursor, which is not deterministic.
        await obs_manager.set_input_settings(bumper, {"looping": False})
        # A new run starts a new pass, not the tail of the last one.
        self._aired = set()
        self._tail_scanned = False
        self._retry_all_errors = False
        self._ready_count = 0
        self._wait_budget = 0.0
        self._unready_since = {}
        self._pass_items = []
        self._current_id = ""
        if self._media_ended is None:
            self._media_ended = asyncio.Event()
        if not self._listening:
            obs_manager.on_event(self._on_obs_event)
            self._listening = True

        self._enabled = True
        self._finish_requested = False
        self._on_air_seconds = 0.0
        self._limit_seconds = None

        async def _load_limit() -> int:
            from app.broadcast import current_limit_seconds
            return await current_limit_seconds()

        self._limit_loader = _load_limit
        self._message = "Preview da fila no OBS: bumper, vídeo, bumper."
        self._error = False
        self._start_task()
        from app.broadcast import note_cycle_started
        note_cycle_started()
        log.info("Queue player preview started")
        return {
            "ok": True,
            "scene": SCENE_PROGRAM,
            "source": PLAYER_SOURCE,
            "message": "Preview da fila no OBS: bumper, vídeo, bumper.",
        }

    async def _bumper_source(self) -> str | None:
        items = await obs_manager.get_scene_items(SCENE_BUMPER)
        inputs = await obs_manager.get_input_list()
        kinds = {item.get("inputName"): item.get("inputKind") for item in inputs}
        media_items = [
            item for item in items
            if kinds.get(item.get("sourceName")) in _MEDIA_KINDS
        ]
        if len(media_items) == 0:
            self._message = "SCENE_BUMPER não tem nenhuma mídia. Coloque um vídeo nessa cena."
            self._error = True
            return None
        if len(media_items) > 1:
            names = [item.get("sourceName", "?") for item in media_items]
            self._message = (
                f"SCENE_BUMPER tem {len(media_items)} mídias ({', '.join(names)}). "
                "Deixe só uma."
            )
            self._error = True
            return None
        name = media_items[0].get("sourceName") or ""
        if not name:
            self._message = "A mídia de SCENE_BUMPER não tem nome. Recrie a fonte."
            self._error = True
            return None
        return name

    def _start_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run_supervised())

    async def _run_supervised(self) -> None:
        """An internal error must not leave 'enabled' true with no loop behind it."""
        while self._enabled:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Cycle crashed; arming again in 5s")
                self.set_message("Erro interno no ciclo. Nova tentativa automática.", error=True)
            if not self._enabled:
                return
            # The cycle gave up its turn, usually no file to play. Try again on a
            # cadence instead of spinning.
            await asyncio.sleep(_RETRY_WAIT_SEC)

    def request_finish(self) -> None:
        """After the current video, play the bumper and then stop. Never cuts mid-media."""
        self._finish_requested = True
        self.set_message(
            "Encerramento pedido: o vídeo atual termina, a vinheta toca e a transmissão para."
        )

    def _add_airtime(self, milliseconds: int) -> None:
        """Count bumper and video time only while RTMP is up."""
        if not obs_manager.is_streaming:
            return
        self._on_air_seconds = float(getattr(self, "_on_air_seconds", 0) or 0)
        self._on_air_seconds += max(0, int(milliseconds or 0)) / 1000.0
        limit = int(getattr(self, "_limit_seconds", 0) or 0)
        if limit > 0 and self._on_air_seconds >= limit:
            self.request_finish()

    async def _refresh_limit(self) -> None:
        loader = getattr(self, "_limit_loader", None)
        if loader is None:
            return
        self._limit_seconds = int(await loader() or 0)

    async def _finish_broadcast(self) -> None:
        """Stop RTMP if it is up, stop the cycle, and close downloads."""
        self._finish_requested = False
        self._enabled = False
        if obs_manager.is_streaming:
            await obs_manager.stop_streaming()
        from app.broadcast import hold_until_needed
        hold_until_needed()
        self.set_message("Transmissão encerrada depois da vinheta.")
        self._error = False

    async def _closing_bumper(self) -> None:
        """One bumper after the video that crossed the stop, then no other video."""
        await obs_manager.switch_scene(SCENE_BUMPER)
        self._state = _State.BUMPER
        self._current_video_title = ""
        self._current_id = ""
        await overlay.hide()
        queue_download.hold_player_release(True)
        try:
            await self._wait_end(self._bumper_input)
        finally:
            queue_download.hold_player_release(False)
        self._add_airtime(getattr(self, "_last_media_ms", 0))

    def disable(self):
        """Disable the queue player. Stops the cycle if running."""
        self._enabled = False
        if self._listening:
            obs_manager.off_event(self._on_obs_event)
            self._listening = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._state = _State.STOPPED
        self._current_video_title = ""
        self._current_id = ""
        self._aired = set()
        self._tail_scanned = False
        self._message = ""
        self._pass_items = []
        self._protect()
        queue_download.hold_player_release(False)
        self._publish_keep_skip()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(overlay.hide())
        except RuntimeError:
            pass
        log.info("Queue player disabled")

    async def _on_obs_event(self, event_name: str, event_data: dict):
        if event_name != "MediaInputPlaybackEnded":
            return
        if event_data.get("inputName") != self._wait_input:
            return
        if self._media_ended is not None:
            self._media_ended.set()

    async def _first_playable_path(self) -> str:
        for row in await queue_svc.get_queue_full():
            if _playable(row):
                return row.get("local_path") or ""
        return ""

    async def _ensure_player_source(self, file_path: str) -> bool:
        """Create 'NewsTube Fila' in SCENE_PROGRAM and load a real file.

        An empty local_file has no size, so position and scale cannot be applied.
        SetInputSettings does not reset an existing transform.
        """
        items = await obs_manager.get_scene_items(SCENE_PROGRAM)
        names = {item.get("sourceName") for item in items}
        if PLAYER_SOURCE not in names:
            created = await obs_manager.create_input(
                PLAYER_SOURCE, "ffmpeg_source",
                {"is_local_file": True, "local_file": file_path, "looping": False},
                scene_name=SCENE_PROGRAM,
            )
            if created is None:
                inputs = await obs_manager.get_input_list()
                exists = any(item.get("inputName") == PLAYER_SOURCE for item in inputs)
                if not exists:
                    return False
                placed = await obs_manager.create_scene_item(SCENE_PROGRAM, PLAYER_SOURCE)
                if placed is None:
                    return False
            log.info("Created player source '%s' in SCENE_PROGRAM", PLAYER_SOURCE)
        ok = await obs_manager.set_input_settings(PLAYER_SOURCE, {
            "is_local_file": True,
            "local_file": file_path,
            "looping": False,
        })
        if not ok:
            return False
        await obs_manager.trigger_media(PLAYER_SOURCE, _MEDIA_RESTART)
        return True

    async def _wait_end(self, input_name: str) -> bool:
        """Wait until this media finishes. The websocket event is easy to miss.

        A short clip can already be ENDED on the first poll. That still counts
        after a brief grace, once a stale end from before the restart has had
        one chance to start again.

        True when the media played to its end. False when it could not be
        read or never started — the caller re-arms the bumper instead of
        sitting on the same scene forever.
        """
        if not input_name:
            self.set_message("Nenhuma fonte de mídia para tocar.", error=True)
            return False
        self._wait_input = input_name
        self._media_ended.clear()
        await obs_manager.trigger_media(input_name, _MEDIA_RESTART)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = (
            started + settings.CYCLE_MAX_WAIT_MINUTES * 60
            if settings.CYCLE_MAX_WAIT_MINUTES > 0 else math.inf
        )
        saw_active = False
        retried = False
        grace = 2.0
        blind = 0
        cursor = -1
        duration = 0
        progress_at = started
        while self._enabled:
            media = await obs_manager.get_media_input_status(input_name)
            if not media:
                # OBS is not answering: the socket died or the request failed.
                blind += 1
                if blind >= _BLIND_POLLS:
                    self.set_message(_OBS_STALE, error=True)
                    return False
                await asyncio.sleep(_POLL_SEC)
                continue
            blind = 0
            state = str(media.get("mediaState") or "")
            if state in _ACTIVE_STATES:
                saw_active = True
            new_cursor = int(media.get("mediaCursor") or 0)
            duration = int(media.get("mediaDuration") or 0) or duration
            if duration > 0:
                self._last_media_ms = duration
            if new_cursor != cursor:
                cursor, progress_at = new_cursor, loop.time()
            at_end = duration > 0 and cursor >= duration - 200
            ended = state in {"OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_STOPPED"}
            if saw_active and (ended or at_end):
                return True
            # Nothing is loaded: a missing file, a deleted bumper. ENDED with no
            # duration is the same thing, not a clip that finished.
            if not saw_active and (state in _NO_MEDIA_STATES or (ended and duration <= 0)):
                if loop.time() >= started + _NO_MEDIA_GRACE:
                    self.set_message(
                        f"A mídia '{input_name}' não abre no OBS. Verifique o arquivo.",
                        error=True,
                    )
                    return False
            if not saw_active and ended and at_end and loop.time() >= started + grace:
                if not retried:
                    retried = True
                    saw_active = False
                    started = loop.time()
                    progress_at = started
                    cursor = -1
                    self._media_ended.clear()
                    await obs_manager.trigger_media(input_name, _MEDIA_RESTART)
                    continue
                return True
            if loop.time() > deadline:
                self.set_message(_STALLED, error=True)
                return False
            if saw_active and loop.time() - progress_at > _NO_PROGRESS_SEC:
                # Paused or frozen. It is not going to finish on its own.
                self.set_message(_STALLED, error=True)
                return False
            if self._media_ended.is_set() and saw_active:
                return True
            self._media_ended.clear()
            try:
                await asyncio.wait_for(self._media_ended.wait(), timeout=_POLL_SEC)
            except asyncio.TimeoutError:
                continue
            # A stale ended event from the previous lap is not this media's end.
            if saw_active:
                return True
            self._media_ended.clear()
        return False

    async def _load_pass(self) -> None:
        self._pass_items = _pass_rows(list(await queue_svc.get_queue_full()), set())
        self._current_id = ""
        self._protect()

    def _unready_expired(self, video_id: str) -> bool:
        """True once this id has used up half the ready buffer."""
        budget = self._wait_budget
        if budget <= 0 or not video_id:
            return False
        now = _now()
        started = self._unready_since.get(video_id)
        if started is None:
            self._unready_since[video_id] = now
            return False
        return now - started >= budget

    async def _take_next(self) -> dict | None:
        """First video that has not aired this pass. Waits on a missing MP4.

        With five or fewer still waiting, scan channels once so a new video
        can join the end before anything repeats. An empty unplayed list
        starts the same order again.
        """
        rows = await queue_svc.get_queue_full()
        aired = set(self._aired)
        unplayed = _pass_rows(rows, aired, self._current_id)
        limit = settings.QUEUE_DOWNLOAD_KEEP
        if len(unplayed) > limit:
            self._tail_scanned = False
        elif tail_scan_due(len(unplayed), limit, self._tail_scanned):
            await queue_svc.maintain_queue()
            self._tail_scanned = True
            rows = await queue_svc.get_queue_full()
            unplayed = _pass_rows(rows, aired, self._current_id)
        video, wrapped = next_in_pass(rows, aired, self._current_id)
        if wrapped:
            self._aired = set()
            self._tail_scanned = False
            self._publish_keep_skip()
            await queue_download.retry_error_videos()
            rows = await queue_svc.get_queue_full()
            video, _wrapped = next_in_pass(rows, self._aired, self._current_id)
            unplayed = _pass_rows(rows, set(), self._current_id)
        self._pass_items = unplayed
        self._ready_count = sum(1 for row in unplayed if _playable(row))
        self._wait_budget = ready_wait_seconds(rows, self._current_id, _playable)
        self._protect()
        return video

    async def _run_cycle(self):
        """Main loop: bumper → video → bumper → ..."""
        log.info("Cycle started")
        try:
            await self._load_pass()
            while self._enabled:
                await self._refresh_limit()
                await obs_manager.switch_scene(SCENE_BUMPER)
                self._state = _State.BUMPER
                self._current_video_title = ""
                self._current_id = ""
                await overlay.hide()
                queue_download.hold_player_release(True)
                try:
                    bumper_done = await self._wait_end(self._bumper_input)
                finally:
                    queue_download.hold_player_release(False)

                if not self._enabled:
                    return

                if not bumper_done:
                    # The bumper did not play to its end. The scene is already the
                    # bumper, so wait a beat and arm it again instead of parking here.
                    # With OBS down, the longer wait keeps the pass from burning videos.
                    wait = _OBS_BACKOFF_SEC if self._message == _OBS_STALE else _RETRY_WAIT_SEC
                    await asyncio.sleep(wait)
                    continue

                self._add_airtime(getattr(self, "_last_media_ms", 0))
                if self._finish_requested:
                    await self._finish_broadcast()
                    return

                video = await self._take_next()
                if video is None:
                    if not self._enabled:
                        return
                    self._message = self._blocked_reason() or _NO_FILE
                    self._error = True
                    log.info("No MP4 available — staying on the bumper")
                    if not self._retry_all_errors:
                        self._retry_all_errors = True
                        await queue_download.retry_error_videos()
                    await asyncio.sleep(_RETRY_WAIT_SEC)
                    continue
                if not _playable(video):
                    skip_id = video.get("video_id") or ""
                    # A known failure skips at once. Otherwise the wait is half
                    # the ready buffer. An empty buffer is not a reason to burn
                    # the pass: the download is still allowed to finish.
                    if video.get("download_error") or self._unready_expired(skip_id):
                        log.info("Skipping %s: not ready in time", skip_id)
                        self.set_message(
                            f"Pulei {video.get('title') or skip_id}: não ficou pronto a tempo."
                        )
                        if skip_id:
                            self._aired.add(skip_id)
                            self._unready_since.pop(skip_id, None)
                        self._publish_keep_skip()
                        continue
                    log.info("Waiting for MP4: %s", video.get("title"))
                    self.set_message(
                        "Aguardando o arquivo do próximo vídeo. "
                        + self._blocked_reason()
                    )
                    queue_download.schedule_sync()
                    await asyncio.sleep(_RETRY_WAIT_SEC)
                    continue

                file_path = video.get("local_path") or ""
                self._current_id = video.get("video_id") or ""
                self._protect()
                self._publish_keep_skip()
                await queue_svc.mark_status_by_video_id(self._current_id, "playing")
                try:
                    await obs_manager.set_input_settings(PLAYER_SOURCE, {
                        "is_local_file": True,
                        "local_file": file_path,
                        "looping": False,
                    })
                    await obs_manager.switch_scene(SCENE_PROGRAM)
                    self._state = _State.VIDEO
                    self._current_video_title = video.get("title") or ""
                    self._message = ""
                    self._error = False
                    await overlay.show_video(self._current_id)
                    log.info("Playing: %s (%s)", self._current_video_title, file_path)
                    video_done = await self._wait_end(PLAYER_SOURCE)
                except Exception:
                    log.exception("Could not play %s", self._current_id)
                    self.set_message(
                        f"Pulei {self._current_video_title or self._current_id}: o OBS não abriu o arquivo."
                    )
                    video_done = False
                if self._current_id:
                    # Whether it finished or would not play, this file had its turn.
                    # Marking it aired is what stops the loop from replaying it now.
                    self._aired.add(self._current_id)
                    if video_done:
                        await queue_svc.rotate_video_to_end(self._current_id)
                self._current_id = ""
                self._protect()
                self._publish_keep_skip()
                await overlay.hide()
                self._add_airtime(getattr(self, "_last_media_ms", 0))
                if self._finish_requested:
                    await self._closing_bumper()
                    await self._finish_broadcast()
                    return

        finally:
            self._state = _State.STOPPED
            self._current_video_title = ""
            self._current_id = ""
            self._pass_items = []
            self._protect()
            queue_download.note_on_air("")
            queue_download.hold_player_release(False)
            await queue_svc.release_stuck_playing()
            await overlay.hide()
            log.info("Cycle stopped")


cycle_manager = CycleManager()
