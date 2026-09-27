"""Cycle manager — bumper → video → bumper playback loop driven by OBS events."""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path

from app.obs.manager import SCENE_BUMPER, SCENE_PROGRAM, obs_manager
from app.overlay import service as overlay
from app.queue import download as queue_download
from app.queue import service as queue_svc

log = logging.getLogger(__name__)

PLAYER_SOURCE = "NewsTube Fila"
_MEDIA_RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
_MEDIA_KINDS = {"ffmpeg_source", "vlc_source"}
_NO_FILE = "Não há arquivo para tocar."


class _State(Enum):
    STOPPED = "stopped"
    BUMPER = "bumper"
    VIDEO = "video"


def _playable(row: dict) -> bool:
    path = row.get("local_path") or ""
    return bool(path) and Path(path).is_file()


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
            cls._instance._error = False
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
        active = {
            "OBS_MEDIA_STATE_PLAYING",
            "OBS_MEDIA_STATE_OPENING",
            "OBS_MEDIA_STATE_BUFFERING",
            "OBS_MEDIA_STATE_PAUSED",
        }
        if match_id and state in active:
            on_air = match_id
        elif match_id and state == "OBS_MEDIA_STATE_ENDED":
            aired.add(match_id)
            if on_air == match_id:
                on_air = ""
        return {"on_air": on_air, "aired": aired}

    def set_message(self, message: str, error: bool = False) -> None:
        self._message = message
        self._error = error

    def _protect(self) -> None:
        """Video ids still needed in this pass, including the one on air."""
        ids = {row["video_id"] for row in self._pass_items if row.get("video_id")}
        if self._current_id:
            ids.add(self._current_id)
        queue_download.protect_video_ids(ids)

    def _publish_keep_skip(self) -> None:
        """Aired videos and the one on air do not count toward the five MP4s."""
        skip = set(self._aired)
        if self._current_id:
            skip.add(self._current_id)
        queue_download.skip_keep_ids(skip)
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
        if self._media_ended is None:
            self._media_ended = asyncio.Event()
        if not self._listening:
            obs_manager.on_event(self._on_obs_event)
            self._listening = True

        self._enabled = True
        self._message = "Preview da fila no OBS: bumper, vídeo, bumper."
        self._error = False
        self._start_task()
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
        return media_items[0].get("sourceName") or ""

    def _start_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run_cycle())

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

    async def _wait_end(self, input_name: str):
        """Wait until this media finishes. The websocket event is easy to miss.

        A short clip can already be ENDED on the first poll. That still counts
        after a brief grace, once a stale end from before the restart has had
        one chance to start again.
        """
        self._wait_input = input_name
        self._media_ended.clear()
        await obs_manager.trigger_media(input_name, _MEDIA_RESTART)
        loop = asyncio.get_running_loop()
        started = loop.time()
        saw_active = False
        retried = False
        grace = 2.0
        while self._enabled:
            media = await obs_manager.get_media_input_status(input_name)
            state = str(media.get("mediaState") or "")
            if state in {
                "OBS_MEDIA_STATE_PLAYING",
                "OBS_MEDIA_STATE_OPENING",
                "OBS_MEDIA_STATE_BUFFERING",
                "OBS_MEDIA_STATE_PAUSED",
            }:
                saw_active = True
            cursor = int(media.get("mediaCursor") or 0)
            duration = int(media.get("mediaDuration") or 0)
            at_end = duration > 0 and cursor >= duration - 200
            ended = state in {"OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_STOPPED"}
            if saw_active and (ended or at_end):
                return
            if not saw_active and ended and at_end and loop.time() >= started + grace:
                if not retried:
                    retried = True
                    saw_active = False
                    started = loop.time()
                    self._media_ended.clear()
                    await obs_manager.trigger_media(input_name, _MEDIA_RESTART)
                    continue
                return
            if self._media_ended.is_set() and saw_active:
                return
            self._media_ended.clear()
            try:
                await asyncio.wait_for(self._media_ended.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue
            if saw_active:
                return
            self._media_ended.clear()

    async def _load_pass(self) -> None:
        self._pass_items = list(await queue_svc.get_queue_full())
        self._current_id = ""
        self._protect()

    def _take_next(self) -> dict | None:
        while self._pass_items:
            video = self._pass_items.pop(0)
            self._protect()
            if _playable(video):
                return video
            log.info("Skipping item without MP4: %s", video.get("title"))
        return None

    async def _run_cycle(self):
        """Main loop: bumper → video → bumper → ..."""
        log.info("Cycle started")
        played_any = False
        try:
            await self._load_pass()
            while self._enabled:
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

                if not self._enabled:
                    return

                video = self._take_next()
                if video is None:
                    if not self._enabled:
                        return
                    if not played_any:
                        self._message = _NO_FILE
                        self._error = True
                        log.info("No MP4 available — stopping cycle")
                        return
                    self._aired = set()
                    self._publish_keep_skip()
                    await queue_svc.shuffle_play_order(reset_files=False)
                    await self._load_pass()
                    played_any = False
                    if not any(_playable(row) for row in self._pass_items):
                        self._message = _NO_FILE
                        self._error = True
                        log.info("No MP4 after shuffle — waiting for the buffer")
                        await asyncio.sleep(2)
                    continue

                file_path = video.get("local_path") or ""
                self._current_id = video.get("video_id") or ""
                self._protect()
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
                await self._wait_end(PLAYER_SOURCE)
                if self._current_id:
                    self._aired.add(self._current_id)
                self._current_id = ""
                self._protect()
                self._publish_keep_skip()
                await overlay.hide()
                played_any = True

        except Exception as exc:
            if exc.__class__.__name__ != "CancelledError":
                log.exception("Cycle error")
        finally:
            self._state = _State.STOPPED
            self._current_video_title = ""
            self._current_id = ""
            self._pass_items = []
            self._protect()
            queue_download.hold_player_release(False)
            await overlay.hide()
            log.info("Cycle stopped")


cycle_manager = CycleManager()
