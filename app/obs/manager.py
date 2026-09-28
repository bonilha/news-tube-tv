"""OBS control via obs-websocket v5 (no DLL — cross-platform).

Connects to the OBS WebSocket server (built-in since OBS 28, port 4455).
All scene/streaming operations go through JSON WebSocket requests.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

import websockets

from app.config import settings

log = logging.getLogger(__name__)

# Canonical scene names
SCENE_PROGRAM = "SCENE_PROGRAM"
SCENE_BUMPER = "SCENE_BUMPER"
SCENE_EMPTY_SLATE = "SCENE_EMPTY_SLATE"
SCENE_SHUTDOWN = "SCENE_SHUTDOWN"
SCENE_OFFLINE = "SCENE_OFFLINE"
ALL_SCENES = [SCENE_PROGRAM, SCENE_BUMPER, SCENE_EMPTY_SLATE, SCENE_SHUTDOWN, SCENE_OFFLINE]
SCENE_DESCRIPTIONS = {
    SCENE_PROGRAM: "Conteúdo principal — vídeos da fila",
    SCENE_BUMPER: "Vinheta de transição",
    SCENE_EMPTY_SLATE: "Tela estática placeholder",
    SCENE_OFFLINE: "Canal indisponível",
    SCENE_SHUTDOWN: "Fade para preto",
}


def _visible_source_size(transform: dict, axis: str) -> float:
    """Source pixels that remain after crop. axis is 'Width' or 'Height'."""
    source = float(transform.get(f"source{axis}") or 0)
    if axis == "Width":
        source -= float(transform.get("cropLeft") or 0) + float(transform.get("cropRight") or 0)
    else:
        source -= float(transform.get("cropTop") or 0) + float(transform.get("cropBottom") or 0)
    return source if source > 0 else float(transform.get(f"source{axis}") or 0)


def _compute_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 auth per official spec:

    1. Concatenate password with salt string:  password + salt
    2. SHA256 binary hash → base64 encode  →  base64_secret
    3. Concatenate base64_secret with challenge string:  base64_secret + challenge
    4. SHA256 binary hash → base64 encode  →  authentication string

    Note: salt and challenge are used as raw strings (NOT base64-decoded).
    """
    base64_secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode()
    return base64.b64encode(
        hashlib.sha256((base64_secret + challenge).encode("utf-8")).digest()
    ).decode()


class OBSManager:
    """Async OBS controller via WebSocket. Singleton."""

    _instance: OBSManager | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._ws = None
            cls._instance._current_scene = SCENE_OFFLINE
            cls._instance._is_streaming = False
            cls._instance._obs_version = ""
            cls._instance._pending_requests: dict[str, asyncio.Future] = {}
            cls._instance._event_handlers: list[Callable[[str, dict], Awaitable[None]]] = []
            cls._instance._reader_task: asyncio.Task | None = None
            cls._instance._send_lock = asyncio.Lock()
        return cls._instance

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def current_scene(self) -> str:
        return self._current_scene

    # -- WebSocket helpers ---------------------------------------------------

    async def _connect(self) -> bool:
        """Connect to OBS WebSocket and authenticate."""
        try:
            # Cancel any existing reader from a prior connection
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                self._reader_task = None

            # Preview frames can exceed the library default of 1 MiB and
            # close the socket (1009). Keep headroom for a full-scene JPEG.
            self._ws = await websockets.connect(
                settings.OBS_WS_URL, max_size=8 * 1024 * 1024,
            )
            hello = json.loads(await self._ws.recv())
            d = hello.get("d", {})
            self._obs_version = d.get("obsWebSocketVersion", "?")

            # Identify is required with or without a password. MediaInputs
            # (1 << 8) is what carries MediaInputPlaybackEnded.
            identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 1 << 8}
            auth_info = d.get("authentication")
            if auth_info:
                password = settings.OBS_WS_PASSWORD.strip()
                if not password:
                    log.error("OBS WebSocket requires auth but OBS_WS_PASSWORD is empty")
                    await self._ws.close()
                    self._ws = None
                    return False
                identify["authentication"] = _compute_auth(
                    password,
                    auth_info["salt"],
                    auth_info["challenge"],
                )
            await self._ws.send(json.dumps({"op": 1, "d": identify}))
            ident = json.loads(await self._ws.recv())
            if ident.get("op") != 2:
                log.error("OBS WebSocket auth failed: %s", ident)
                await self._ws.close()
                self._ws = None
                return False

            self._reader_task = asyncio.ensure_future(self._reader_loop())
            log.info("Connected to OBS WebSocket v%s", self._obs_version)
            return True
        except Exception as e:
            log.warning("Cannot connect to OBS WebSocket: %s", e)
            self._ws = None
            return False

    async def _reader_loop(self):
        """Read websocket frames and dispatch to request futures or event handlers."""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                op = msg.get("op")
                d = msg.get("d", {})
                if op == 7:
                    # Request response
                    req_id = d.get("requestId", "")
                    future = self._pending_requests.get(req_id)
                    if future and not future.done():
                        future.set_result(d)
                    else:
                        log.debug("Response for unknown request %s", req_id)
                elif op == 5:
                    # Event — forward to handlers
                    event_name = d.get("eventType", "")
                    event_data = d.get("eventData", {})
                    for handler in self._event_handlers:
                        try:
                            await handler(event_name, event_data)
                        except Exception:
                            log.exception("Event handler error for %s", event_name)
        except websockets.ConnectionClosed:
            log.info("OBS WebSocket connection closed")
        except Exception as e:
            log.error("OBS reader error: %s", e)
        finally:
            self._ws = None
            # Cancel any pending requests
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_result(None)
            self._pending_requests.clear()

    async def _request(self, req_type: str, req_data: dict | None = None) -> dict | None:
        if not self._ws:
            return None
        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "op": 6,
                    "d": {"requestId": req_id, "requestType": req_type, "requestData": req_data or {}},
                }))
            return await asyncio.wait_for(future, timeout=30)
        except (asyncio.TimeoutError, Exception) as e:
            log.error("WebSocket request '%s' failed: %s", req_type, e)
            return None
        finally:
            self._pending_requests.pop(req_id, None)

    def on_event(self, handler: Callable[[str, dict], Awaitable[None]]):
        if handler not in self._event_handlers:
            self._event_handlers.append(handler)

    def off_event(self, handler: Callable[[str, dict], Awaitable[None]]):
        self._event_handlers = [item for item in self._event_handlers if item is not handler]

    async def trigger_media(self, input_name: str, action: str) -> bool:
        resp = await self._request("TriggerMediaInputAction", {
            "inputName": input_name,
            "mediaAction": action,
        })
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    # -- Public API ----------------------------------------------------------

    async def connect(self) -> bool:
        """Connect and optionally create missing scenes."""
        ok = await self._connect()
        if ok:
            await self._ensure_scenes()
            await self._refresh_state()
        return ok

    async def _ensure_scenes(self):
        """Create any missing canonical scenes in OBS."""
        resp = await self._request("GetSceneList")
        if not resp:
            return
        existing = {s["sceneName"] for s in resp.get("responseData", {}).get("scenes", [])}
        for name in ALL_SCENES:
            if name not in existing:
                await self._request("CreateScene", {"sceneName": name})
                log.info("Created scene: %s", name)

    async def _refresh_state(self):
        """Sync current_scene and is_streaming from OBS."""
        scene_resp = await self._request("GetSceneList")
        if scene_resp:
            self._current_scene = scene_resp.get("responseData", {}).get("currentProgramSceneName", SCENE_OFFLINE)

        stream_resp = await self._request("GetStreamStatus")
        if stream_resp:
            self._is_streaming = stream_resp.get("responseData", {}).get("outputActive", False)

    async def switch_scene(self, scene_name: str):
        """Switch the program scene."""
        if not self._ws:
            return
        await self._request("SetCurrentProgramScene", {"sceneName": scene_name})
        self._current_scene = scene_name

    def _request_ok(self, resp: dict | None) -> tuple[bool, str]:
        status = (resp or {}).get("requestStatus", {})
        if resp and status.get("result", False):
            return True, ""
        return False, status.get("comment") or "OBS recusou a configuração."

    async def _set_profile(self, category: str, name: str, value: str) -> tuple[bool, str]:
        resp = await self._request("SetProfileParameter", {
            "parameterCategory": category,
            "parameterName": name,
            "parameterValue": value,
        })
        return self._request_ok(resp)

    async def apply_stream_settings(self) -> tuple[bool, str]:
        """Push canvas, bitrate, and simple-output mode. OBS fixes keyint at 2 s."""
        if settings.OBS_KEYFRAME_SEC != 2:
            return False, (
                "OBS_KEYFRAME_SEC precisa ser 2. "
                "O modo Simple do OBS fixa o keyframe em 2 segundos."
            )
        stream_resp = await self._request("GetStreamStatus")
        if stream_resp and stream_resp.get("responseData", {}).get("outputActive"):
            return True, ""

        video = await self._request("SetVideoSettings", {
            "baseWidth": settings.OBS_BASE_WIDTH,
            "baseHeight": settings.OBS_BASE_HEIGHT,
            "outputWidth": settings.OBS_BASE_WIDTH,
            "outputHeight": settings.OBS_BASE_HEIGHT,
            "fpsNumerator": settings.OBS_FPS_NUM,
            "fpsDenominator": settings.OBS_FPS_DEN,
        })
        ok, comment = self._request_ok(video)
        if not ok:
            log.warning("SetVideoSettings failed: %s", comment)
            return False, comment

        for category, name, value in (
            ("Output", "Mode", "Simple"),
            ("SimpleOutput", "VBitrate", str(settings.OBS_STREAM_BITRATE_KBPS)),
            ("SimpleOutput", "ABitrate", str(settings.OBS_AUDIO_BITRATE_KBPS)),
        ):
            ok, comment = await self._set_profile(category, name, value)
            if not ok:
                log.warning("SetProfileParameter %s/%s failed: %s", category, name, comment)
                return False, comment
        return True, ""

    async def start_streaming(self) -> tuple[bool, str]:
        """Write the configured RTMP target into OBS, then start the output."""
        if not self._ws:
            return False, "OBS não está conectado."
        ready, error = await self.apply_stream_settings()
        if not ready:
            return False, error
        service = await self._request("SetStreamServiceSettings", {
            "streamServiceType": "rtmp_custom",
            "streamServiceSettings": {
                "server": settings.RTMP_URL,
                "key": settings.RTMP_KEY,
                "use_auth": False,
            },
        })
        service_status = (service or {}).get("requestStatus", {})
        if not service or not service_status.get("result", False):
            comment = service_status.get("comment") or "OBS recusou a configuração RTMP."
            log.warning("SetStreamServiceSettings failed: %s", comment)
            return False, comment
        resp = await self._request("StartStream")
        status = (resp or {}).get("requestStatus", {})
        if resp and status.get("result", False):
            self._is_streaming = True
            return True, ""
        await self._refresh_state()
        if self._is_streaming:
            return True, ""
        comment = status.get("comment") or "OBS não iniciou a transmissão."
        log.warning("StartStream failed: %s", comment)
        return False, comment

    async def stop_streaming(self):
        if not self._ws:
            return
        await self._request("StopStream")
        self._is_streaming = False

    async def get_status(self) -> dict[str, Any]:
        """Full status dict for the dashboard API. Auto-reconnects if disconnected."""
        if not self._ws:
            # Attempt a silent reconnect before returning disconnected state
            await self.connect()

        if not self._ws:
            return {
                "connected": False,
                "is_initialized": False,
                "is_streaming": False,
                "current_scene": SCENE_OFFLINE,
                "resolution": "N/A",
                "fps": "N/A",
                "scenes": [
                    {"name": n, "description": SCENE_DESCRIPTIONS.get(n, ""), "active": False}
                    for n in ALL_SCENES
                ],
            }

        await self._refresh_state()

        raw_scenes = await self.get_scene_list()
        scenes = [
            {
                "name": s.get("sceneName") or "",
                "description": SCENE_DESCRIPTIONS.get(s.get("sceneName") or "", ""),
                "active": s.get("sceneName") == self._current_scene,
            }
            for s in raw_scenes
            if isinstance(s, dict) and s.get("sceneName")
        ]
        if not scenes:
            scenes = [
                {"name": n, "description": SCENE_DESCRIPTIONS.get(n, ""), "active": n == self._current_scene}
                for n in ALL_SCENES
            ]

        return {
            "connected": True,
            "is_initialized": True,
            "is_streaming": self._is_streaming,
            "current_scene": self._current_scene,
            "resolution": f"{settings.OBS_BASE_WIDTH}x{settings.OBS_BASE_HEIGHT}",
            "fps": f"{settings.OBS_FPS_NUM / settings.OBS_FPS_DEN:.2f}",
            "obs_version": self._obs_version,
            "scenes": scenes,
        }

    async def disconnect(self):
        """Close the WebSocket connection."""
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        # Cancel pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.set_result(None)
        self._pending_requests.clear()
        self._initialized = False
        log.info("Disconnected from OBS WebSocket")

    # -- Scene item manipulation (editor) ------------------------------------

    async def get_scene_list(self) -> list[dict]:
        """Return list of scenes from OBS."""
        if not self._ws:
            await self.connect()
        resp = await self._request("GetSceneList")
        if not resp:
            return []
        data = resp.get("responseData") or {}
        return data.get("scenes") or []

    async def get_scene_items(self, scene_name: str) -> list[dict]:
        """Return ordered list of scene items in a scene."""
        if not self._ws:
            await self.connect()
        resp = await self._request("GetSceneItemList", {"sceneName": scene_name})
        status = (resp or {}).get("requestStatus", {})
        if not resp or not status.get("result", True):
            log.warning(
                "GetSceneItemList %s failed: %s",
                scene_name, status.get("comment") or status.get("code") or "no response",
            )
            return []
        items = list(resp.get("responseData", {}).get("sceneItems", []))
        # A group is one item. Pull its children so every source in the scene is listed.
        # _transformScene is the scene name GetSceneItemTransform expects (the group, for a child).
        expanded: list[dict] = []
        for item in items:
            tagged = dict(item)
            tagged["_transformScene"] = scene_name
            expanded.append(tagged)
            if not item.get("isGroup"):
                continue
            group = await self._request("GetGroupSceneItemList", {"sceneName": item.get("sourceName")})
            children = (group or {}).get("responseData", {}).get("sceneItems", [])
            for child in children:
                tagged_child = dict(child)
                tagged_child["_transformScene"] = item.get("sourceName") or scene_name
                expanded.append(tagged_child)
        return expanded

    async def get_scene_item_transform(self, scene_name: str, scene_item_id: int) -> dict:
        """Transform of one item. List requests do not include it."""
        resp = await self._request("GetSceneItemTransform", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
        })
        status = (resp or {}).get("requestStatus", {})
        if not status.get("result", False):
            return {}
        tf = dict(((resp or {}).get("responseData") or {}).get("sceneItemTransform") or {})
        width = float(tf.get("width") or 0)
        height = float(tf.get("height") or 0)
        if width <= 0:
            width = float(tf.get("sourceWidth") or 0) * float(tf.get("scaleX") or 1)
        if height <= 0:
            height = float(tf.get("sourceHeight") or 0) * float(tf.get("scaleY") or 1)
        tf["width"] = width
        tf["height"] = height
        return tf

    async def get_scene_input_names(self, scene_name: str) -> list[str]:
        """Return list of source names in scene."""
        items = await self.get_scene_items(scene_name)
        return [i.get("sourceName", "") for i in items]

    async def create_scene_item(self, scene_name: str, source_name: str) -> dict | None:
        """Add a source to a scene."""
        resp = await self._request("CreateSceneItem", {
            "sceneName": scene_name,
            "sourceName": source_name,
        })
        if not resp or not resp.get("requestStatus", {}).get("result"):
            return None
        return resp.get("responseData")

    async def remove_scene_item(self, scene_name: str, scene_item_id: int) -> bool:
        resp = await self._request("RemoveSceneItem", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
        })
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    async def set_scene_item_index(self, scene_name: str, scene_item_id: int, index: int) -> bool:
        resp = await self._request("SetSceneItemIndex", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
            "sceneItemIndex": index,
        })
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    async def get_canvas_size(self) -> tuple[int, int]:
        """OBS base (canvas) size. Position and 100% scale use this area."""
        if not self._ws:
            await self.connect()
        resp = await self._request("GetVideoSettings")
        data = (resp or {}).get("responseData") or {}
        width = int(data.get("baseWidth") or settings.OBS_BASE_WIDTH)
        height = int(data.get("baseHeight") or settings.OBS_BASE_HEIGHT)
        return width, height

    async def set_scene_item_transform(
        self, scene_name: str, scene_item_id: int,
        position_x: float | None = None,
        position_y: float | None = None,
        scaleX: float | None = None,
        scaleY: float | None = None,
        width: float | None = None,
        height: float | None = None,
        rotation: float | None = None,
    ) -> bool:
        current = await self._request("GetSceneItemTransform", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
        })
        current_tf = ((current or {}).get("responseData") or {}).get("sceneItemTransform") or {}
        src_w = _visible_source_size(current_tf, "Width")
        src_h = _visible_source_size(current_tf, "Height")

        transform: dict[str, Any] = {}
        if position_x is not None:
            transform["positionX"] = position_x
        if position_y is not None:
            transform["positionY"] = position_y
        # OBS only stores scale. width/height on the request are rejected,
        # so a pixel size is converted with the source's visible size.
        if width is not None and src_w > 0:
            transform["scaleX"] = float(width) / src_w
        elif scaleX is not None:
            transform["scaleX"] = scaleX
        if height is not None and src_h > 0:
            transform["scaleY"] = float(height) / src_h
        elif scaleY is not None:
            transform["scaleY"] = scaleY
        if rotation is not None:
            transform["rotation"] = rotation
        if not transform:
            return True
        resp = await self._request("SetSceneItemTransform", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
            "sceneItemTransform": transform,
        })
        status = (resp or {}).get("requestStatus", {})
        if not status.get("result"):
            log.warning(
                "SetSceneItemTransform %s/%s failed: %s",
                scene_name, scene_item_id, status.get("comment") or status.get("code"),
            )
            return False
        return True

    async def set_scene_item_enabled(self, scene_name: str, scene_item_id: int, enabled: bool) -> bool:
        resp = await self._request("SetSceneItemEnabled", {
            "sceneName": scene_name,
            "sceneItemId": scene_item_id,
            "sceneItemEnabled": enabled,
        })
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    async def get_input_settings(self, input_name: str) -> dict:
        resp = await self._request("GetInputSettings", {"inputName": input_name})
        if not resp or not resp.get("requestStatus", {}).get("result"):
            return {}
        return resp.get("responseData") or {}

    async def get_media_input_status(self, input_name: str) -> dict:
        resp = await self._request("GetMediaInputStatus", {"inputName": input_name})
        if not resp or not resp.get("requestStatus", {}).get("result"):
            return {}
        return resp.get("responseData") or {}

    async def set_input_settings(self, input_name: str, settings: dict) -> bool:
        resp = await self._request("SetInputSettings", {
            "inputName": input_name,
            "inputSettings": settings,
        })
        return bool(resp and resp.get("requestStatus", {}).get("result"))

    async def get_input_list(self) -> list[dict]:
        """List all available inputs/sources in OBS."""
        if not self._ws:
            await self.connect()
        resp = await self._request("GetInputList")
        if not resp:
            return []
        return resp.get("responseData", {}).get("inputs", [])

    async def create_input(
        self, input_name: str, input_kind: str, input_settings: dict | None = None,
        scene_name: str | None = None,
    ) -> dict | None:
        """Create an OBS input and attach it to scene_name.

        obs-websocket v5 requires sceneName on CreateInput; without it the
        request is rejected and nothing shows up in the scene.
        Returns responseData on success, or None when the input already exists
        or the request fails.
        """
        data: dict[str, Any] = {
            "inputName": input_name,
            "inputKind": input_kind,
            "inputSettings": input_settings or {},
            "sceneItemEnabled": True,
        }
        if scene_name:
            data["sceneName"] = scene_name
        resp = await self._request("CreateInput", data)
        status = (resp or {}).get("requestStatus", {})
        if not status.get("result"):
            log.warning(
                "CreateInput %s in %s failed: %s",
                input_name, scene_name, status.get("comment") or status.get("code"),
            )
            return None
        return resp.get("responseData") or {}

    async def get_source_screenshot(
        self, source_name: str, fmt: str = "png", quality: int = 80,
    ) -> str | None:
        """Return base64-encoded screenshot data (without prefix)."""
        if not self._ws:
            await self.connect()
        # Scale down. A 1080p PNG is larger than the websocket frame limit
        # and the preview only needs a monitor-sized JPEG.
        resp = await self._request("GetSourceScreenshot", {
            "sourceName": source_name,
            "imageFormat": fmt,
            "imageWidth": 960,
            "imageCompressionQuality": quality,
        })
        if not resp or not resp.get("requestStatus", {}).get("result"):
            return None
        img = resp.get("responseData", {}).get("imageData", "")
        if "," in img:
            img = img.split(",", 1)[1]
        return img

    # -- Sync wrappers for non-async callers (routes) ------------------------

    def switch_scene_sync(self, scene_name: str):
        asyncio.get_event_loop().create_task(self.switch_scene(scene_name))

    def start_streaming_sync(self) -> bool:
        fut = asyncio.get_event_loop().create_task(self.start_streaming())
        # Can't block in async context — return pending
        return False  # caller should poll get_status

    def stop_streaming_sync(self):
        asyncio.get_event_loop().create_task(self.stop_streaming())

    def get_status_sync(self) -> dict[str, Any]:
        """Immediate status without fresh OBS query (for sync routes)."""
        return {
            "connected": self._ws is not None,
            "is_initialized": self._ws is not None,
            "is_streaming": self._is_streaming,
            "current_scene": self._current_scene,
            "resolution": f"{settings.OBS_BASE_WIDTH}x{settings.OBS_BASE_HEIGHT}",
            "fps": f"{settings.OBS_FPS_NUM / settings.OBS_FPS_DEN:.2f}",
            "scenes": [
                {"name": n, "description": SCENE_DESCRIPTIONS.get(n, ""), "active": n == self._current_scene}
                for n in ALL_SCENES
            ],
        }


obs_manager = OBSManager()
