"""Apple TV and HomePod protocol adapter."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from jarvis_command_sdk import (
    IJarvisDeviceProtocol,
    DiscoveredDevice,
    DeviceControlResult,
    IJarvisButton,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug(msg)


logger = JarvisLogger(service="device.apple")

_SUPPORTED_MODELS: set[str] = {
    "AppleTV",
    "AppleTV4",
    "AppleTV4K",
    "AppleTV4KGen2",
    "AppleTV4KGen3",
    "HomePod",
    "HomePodMini",
}

_SUPPORTED_RAW_PREFIXES: tuple[str, ...] = (
    "appletv",
    "apple tv",
    "homepod",
)

_MODEL_FRIENDLY_NAMES: dict[str, str] = {
    "AppleTV": "Apple TV",
    "AppleTV4": "Apple TV (4th gen)",
    "AppleTV4K": "Apple TV 4K",
    "AppleTV4KGen2": "Apple TV 4K (2nd gen)",
    "AppleTV4KGen3": "Apple TV 4K (3rd gen)",
    "HomePod": "HomePod",
    "HomePodMini": "HomePod mini",
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _is_supported_device(raw_model: str) -> bool:
    if raw_model in _SUPPORTED_MODELS:
        return True
    lower: str = raw_model.lower()
    for prefix in _SUPPORTED_RAW_PREFIXES:
        if lower.startswith(prefix):
            return True
    return False


def _model_to_device_class(raw_model: str) -> str:
    lower: str = raw_model.lower()
    if "homepod" in lower:
        return "homepod"
    return "appletv"


def _model_friendly(raw_model: str) -> str:
    if raw_model in _MODEL_FRIENDLY_NAMES:
        return _MODEL_FRIENDLY_NAMES[raw_model]
    return raw_model


class AppleProtocol(IJarvisDeviceProtocol):
    """Apple TV and HomePod LAN protocol adapter."""

    protocol_name: str = "apple"
    friendly_name: str = "Apple"
    supported_domains: list[str] = ["media_player"]
    connection_type: str = "lan"

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Play", button_action="play", button_type="primary", button_icon="play"),
            IJarvisButton(button_text="Pause", button_action="pause", button_type="secondary", button_icon="pause"),
            IJarvisButton(button_text="Power On", button_action="turn_on", button_type="primary", button_icon="power"),
            IJarvisButton(button_text="Power Off", button_action="turn_off", button_type="secondary", button_icon="power-off"),
            IJarvisButton(button_text="Vol Up", button_action="volume_up", button_type="secondary", button_icon="volume-plus"),
            IJarvisButton(button_text="Vol Down", button_action="volume_down", button_type="secondary", button_icon="volume-minus"),
        ]

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        try:
            import pyatv
        except ImportError:
            logger.error("pyatv is not installed. Run: pip install pyatv")
            return []

        devices: list[DiscoveredDevice] = []
        seen_macs: set[str] = set()

        try:
            loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
            configs = await pyatv.scan(loop, timeout=timeout)
        except Exception as e:
            logger.error(f"Apple device discovery failed: {e}")
            return []

        for config in configs:
            name: str = config.name or ""
            raw_model: str = ""
            mac: str = ""

            for service in config.services:
                if hasattr(service, "properties"):
                    props: dict[str, str] = service.properties or {}
                    if "model" in props:
                        raw_model = props["model"]
                    if "macAddress" in props:
                        mac = props["macAddress"]
                    elif "deviceid" in props:
                        mac = props["deviceid"]

            if not raw_model:
                raw_model = str(getattr(config, "model", "")) or ""

            if raw_model and not _is_supported_device(raw_model):
                continue

            if mac and mac in seen_macs:
                continue
            if mac:
                seen_macs.add(mac)

            address: str = str(config.address) if config.address else ""
            device_class: str = _model_to_device_class(raw_model)
            friendly_model: str = _model_friendly(raw_model)
            device_id: str = _slugify(name) if name else _slugify(mac or address)

            devices.append(
                DiscoveredDevice(
                    id=device_id,
                    name=name or friendly_model or "Apple Device",
                    domain="media_player",
                    protocol=self.protocol_name,
                    ip=address,
                    mac=mac,
                    model=friendly_model,
                    manufacturer="Apple",
                    metadata={"device_class": device_class, "raw_model": raw_model},
                )
            )

        logger.info(f"Apple discovery found {len(devices)} device(s)")
        return devices

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None
    ) -> DeviceControlResult:
        try:
            import pyatv
        except ImportError:
            return DeviceControlResult(
                success=False,
                message="pyatv is not installed. Run: pip install pyatv",
            )

        params = params or {}
        ip: str = device.ip or ""
        if not ip:
            return DeviceControlResult(success=False, message="No IP address for device")

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

        try:
            configs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not configs:
                return DeviceControlResult(
                    success=False, message=f"Could not find Apple device at {ip}"
                )
            config = configs[0]
        except Exception as e:
            return DeviceControlResult(success=False, message=f"Scan failed for {ip}: {e}")

        atv = None
        try:
            atv = await pyatv.connect(config, loop)
        except Exception as e:
            return DeviceControlResult(success=False, message=f"Failed to connect to {ip}: {e}")

        try:
            if action == "turn_on":
                await atv.power.turn_on()
                return DeviceControlResult(success=True, message=f"{device.name} powered on")

            elif action == "turn_off":
                await atv.power.turn_off()
                return DeviceControlResult(success=True, message=f"{device.name} powered off")

            elif action == "play":
                await atv.remote_control.play()
                return DeviceControlResult(success=True, message=f"{device.name} playing")

            elif action == "pause":
                await atv.remote_control.pause()
                return DeviceControlResult(success=True, message=f"{device.name} paused")

            elif action == "stop":
                await atv.remote_control.stop()
                return DeviceControlResult(success=True, message=f"{device.name} stopped")

            elif action == "next":
                await atv.remote_control.next()
                return DeviceControlResult(success=True, message=f"{device.name} skipped to next")

            elif action == "previous":
                await atv.remote_control.previous()
                return DeviceControlResult(
                    success=True, message=f"{device.name} skipped to previous"
                )

            elif action == "volume_up":
                await atv.remote_control.volume_up()
                return DeviceControlResult(success=True, message=f"{device.name} volume up")

            elif action == "volume_down":
                await atv.remote_control.volume_down()
                return DeviceControlResult(success=True, message=f"{device.name} volume down")

            else:
                return DeviceControlResult(success=False, message=f"Unsupported action: {action}")

        except Exception as e:
            return DeviceControlResult(success=False, message=f"Control failed: {e}")
        finally:
            atv.close()

    async def get_state(self, device: DiscoveredDevice) -> dict[str, Any]:
        try:
            import pyatv
            from pyatv.const import DeviceState
        except ImportError:
            return {"error": "pyatv is not installed"}

        ip: str = device.ip or ""
        if not ip:
            return {"error": "No IP address for device"}

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

        try:
            configs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not configs:
                return {"error": f"Could not find Apple device at {ip}"}
            config = configs[0]
        except Exception as e:
            return {"error": f"Scan failed: {e}"}

        atv = None
        try:
            atv = await pyatv.connect(config, loop)
            playing = await atv.metadata.playing()

            state_map: dict[Any, str] = {
                DeviceState.Idle: "idle",
                DeviceState.Loading: "on",
                DeviceState.Paused: "paused",
                DeviceState.Playing: "playing",
                DeviceState.Seeking: "playing",
                DeviceState.Stopped: "off",
            }

            device_state: str = state_map.get(playing.device_state, "on")

            result: dict[str, Any] = {
                "state": device_state,
            }

            if playing.title:
                result["media_title"] = playing.title
            if playing.artist:
                result["media_artist"] = playing.artist
            if playing.album:
                result["media_album"] = playing.album
            if playing.media_type:
                result["media_type"] = str(playing.media_type)

            return result

        except Exception as e:
            return {"error": f"Failed to get state: {e}"}
        finally:
            if atv:
                atv.close()
