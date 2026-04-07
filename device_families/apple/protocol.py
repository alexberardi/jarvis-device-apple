"""Apple TV and HomePod protocol adapter."""

from __future__ import annotations

import asyncio
import re
import socket
import time
from typing import Any

from jarvis_command_sdk import (
    IJarvisDeviceProtocol,
    IJarvisSecret,
    DiscoveredDevice,
    DeviceControlResult,
    InputRequest,
    IJarvisButton,
    JarvisSecret,
    JarvisStorage,
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

    # Pairing sessions expire after 2 minutes (Apple TV PIN display timeout)
    _PAIRING_SESSION_TTL: int = 120

    def __init__(self) -> None:
        self._storage = JarvisStorage("apple_device")
        self._pairing_sessions: dict[str, dict[str, Any]] = {}

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "LAN_SUBNET", "LAN subnet prefix for device discovery (e.g. 10.0.0)",
                "node", "string", required=False, is_sensitive=False,
                friendly_name="LAN Subnet",
            ),
        ]

    setup_guide: str = """## Setup

1. Set **LAN Subnet** to your network prefix (e.g. `10.0.0`) — required for Docker nodes
2. Tap **Scan for Devices** to discover Apple TVs and HomePods
3. For each device, tap **Pair** — a PIN will appear on your TV
4. Enter the PIN to complete pairing

Native Pi nodes discover via mDNS automatically (no subnet needed)."""

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Pair", button_action="pair_start", button_type="primary", button_icon="link"),
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

            # Try mDNS broadcast first (works on native Pi nodes)
            configs = await pyatv.scan(loop, timeout=timeout)

            # If mDNS finds nothing (e.g. Docker), try unicast subnet scan.
            # Reads LAN_SUBNET secret (e.g. "10.0.0") for the network to scan.
            if not configs:
                lan_subnet = self._storage.get_secret("LAN_SUBNET", scope="node") or ""
                if lan_subnet:
                    logger.info(f"mDNS scan empty, trying unicast on {lan_subnet}.x")
                    for batch_start in range(1, 255, 25):
                        batch_hosts = [f"{lan_subnet}.{i}" for i in range(batch_start, min(batch_start + 25, 255))]
                        try:
                            batch_configs = await pyatv.scan(loop, hosts=batch_hosts, timeout=2)
                            configs.extend(batch_configs)
                        except Exception:
                            pass
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
                    entity_id=device_id,
                    name=name or friendly_model or "Apple Device",
                    domain="media_player",
                    protocol=self.protocol_name,
                    local_ip=address,
                    mac_address=mac,
                    model=friendly_model,
                    manufacturer="Apple",
                    extra={"device_class": device_class, "raw_model": raw_model},
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
                success=False, entity_id=device.entity_id, action=action,
                error="pyatv is not installed. Run: pip install pyatv",
            )

        params = params or {}
        ip: str = device.local_ip or ""
        if not ip:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error="No IP address for device")

        # Handle pairing flow
        if action == "pair_start":
            return await self._pair_start(device, ip, pyatv)
        if action == "pair_finish":
            return await self._pair_finish(device, params, pyatv)

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

        # Load stored credentials if available
        creds = self._storage.get(f"credentials:{device.entity_id}")
        try:
            configs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not configs:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action=action,
                    error=f"Could not find Apple device at {ip}"
                )
            config = configs[0]
        except Exception as e:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Scan failed for {ip}: {e}")

        # Apply stored pairing credentials so pyatv connects as a paired device
        if creds:
            try:
                from pyatv.const import Protocol
                proto_str: str = creds.get("protocol", "")
                cred_str: str = creds.get("credentials", "")
                if proto_str and cred_str:
                    proto_map: dict[str, Any] = {
                        "Protocol.AirPlay": Protocol.AirPlay,
                        "Protocol.Companion": Protocol.Companion,
                        "Protocol.RAOP": Protocol.RAOP,
                    }
                    proto = proto_map.get(proto_str)
                    if proto:
                        config.set_credentials(proto, cred_str)
                        logger.info(f"Loaded {proto_str} credentials for {device.name}")
            except Exception as e:
                logger.warning(f"Failed to load credentials for {device.name}: {e}")

        atv = None
        try:
            atv = await pyatv.connect(config, loop)
        except Exception as e:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Failed to connect to {ip}: {e}")

        try:
            if action == "turn_on":
                # Send Wake-on-LAN magic packet first — Apple TVs in deep sleep
                # don't respond to pyatv's power.turn_on() alone.
                mac: str = device.mac_address or ""
                if mac:
                    self._send_wol(mac)
                    await asyncio.sleep(1)
                await atv.power.turn_on()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "turn_off":
                await atv.power.turn_off()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "play":
                await atv.remote_control.play()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "pause":
                await atv.remote_control.pause()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "stop":
                await atv.remote_control.stop()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "next":
                await atv.remote_control.next()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "previous":
                await atv.remote_control.previous()
                return DeviceControlResult(
                    success=True, entity_id=device.entity_id, action=action
                )

            elif action == "volume_up":
                await atv.remote_control.volume_up()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            elif action == "volume_down":
                await atv.remote_control.volume_down()
                return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

            else:
                return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Unsupported action: {action}")

        except Exception as e:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Control failed: {e}")
        finally:
            atv.close()

    def _send_wol(self, mac: str) -> None:
        """Send a Wake-on-LAN magic packet to the given MAC address.

        Sends to both 255.255.255.255 and the LAN subnet broadcast address
        (e.g. 10.0.0.255) so it works from inside Docker containers.
        """
        mac_clean: str = mac.replace(":", "").replace("-", "").replace(".", "")
        if len(mac_clean) != 12:
            logger.warning(f"Invalid MAC for WoL: {mac}")
            return
        mac_bytes: bytes = bytes.fromhex(mac_clean)
        magic: bytes = b"\xff" * 6 + mac_bytes * 16

        targets: list[str] = ["255.255.255.255"]
        lan_subnet: str = self._storage.get_secret("LAN_SUBNET", scope="node") or ""
        if lan_subnet:
            targets.append(f"{lan_subnet}.255")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in targets:
                try:
                    sock.sendto(magic, (target, 9))
                except OSError:
                    pass
        logger.info(f"Sent WoL packet to {mac} via {targets}")

    def _cleanup_expired_sessions(self) -> None:
        """Remove pairing sessions older than _PAIRING_SESSION_TTL seconds."""
        now: float = time.monotonic()
        expired: list[str] = [
            sid for sid, sess in self._pairing_sessions.items()
            if now - sess.get("created_at", 0) > self._PAIRING_SESSION_TTL
        ]
        for sid in expired:
            session = self._pairing_sessions.pop(sid, None)
            if session:
                logger.info(f"Expired pairing session {sid[:8]}")
                try:
                    pairing = session.get("pairing")
                    if pairing:
                        asyncio.ensure_future(pairing.close())
                except Exception:
                    pass

    async def _pair_start(self, device: DiscoveredDevice, ip: str, pyatv: Any) -> DeviceControlResult:
        """Begin pairing — Apple TV shows PIN on screen."""
        import uuid

        self._cleanup_expired_sessions()

        loop = asyncio.get_event_loop()
        try:
            configs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not configs:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action="pair_start",
                    error=f"Could not find Apple device at {ip}",
                )

            config = configs[0]
            # Companion is the reliable pairing protocol on modern Apple TVs
            # (tvOS 15+). AirPlay pairing often times out with
            # "no response to POST /pair-setup". Try Companion first, then
            # fall back to AirPlay if Companion isn't advertised.
            from pyatv.const import Protocol
            pairing_order: list[Any] = [Protocol.Companion, Protocol.AirPlay]
            available: list[Any] = [
                p for p in pairing_order if config.get_service(p) is not None
            ]
            if not available:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action="pair_start",
                    error="No pairable protocol found on device",
                )

            pairing = None
            pairing_protocol = None
            last_error: str = ""
            for proto in available:
                try:
                    logger.info(f"Trying {proto.name} pairing for {device.name}")
                    pairing = await pyatv.pair(config, proto, loop)
                    await pairing.begin()
                    pairing_protocol = proto
                    break
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"{proto.name} pairing failed for {device.name}: {e}")
                    if pairing is not None:
                        try:
                            await pairing.close()
                        except Exception:
                            pass
                        pairing = None

            if pairing is None:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action="pair_start",
                    error=f"All pairing protocols failed. Last error: {last_error}",
                )

            session_id = str(uuid.uuid4())
            self._pairing_sessions[session_id] = {
                "pairing": pairing,
                "device": device,
                "protocol": pairing_protocol,
                "created_at": time.monotonic(),
            }

            logger.info(f"Apple pairing started for {device.name}, session={session_id[:8]}")

            return DeviceControlResult(
                success=True, entity_id=device.entity_id, action="pair_start",
                input_required=InputRequest(
                    type="pin",
                    prompt=f"Enter the PIN shown on {device.name}",
                    session_id=session_id,
                    follow_up_action="pair_finish",
                ),
            )
        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="pair_start",
                error=f"Failed to start pairing: {e}",
            )

    async def _pair_finish(self, device: DiscoveredDevice, params: dict[str, Any], pyatv: Any) -> DeviceControlResult:
        """Complete pairing with the PIN entered by the user."""
        session_id = params.get("session_id", "")
        pin = params.get("pin", "")

        session = self._pairing_sessions.pop(session_id, None)
        if not session:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="pair_finish",
                error="Pairing session not found or expired. Try pairing again.",
            )

        pairing = session["pairing"]

        try:
            pairing.pin(int(pin))
            await pairing.finish()

            # Store credentials for future connections
            if pairing.has_paired:
                credentials = pairing.service.credentials
                if credentials:
                    self._storage.save(f"credentials:{device.entity_id}", {
                        "protocol": str(session["protocol"]),
                        "credentials": str(credentials),
                    })
                    logger.info(f"Apple pairing complete for {device.name}, credentials stored")

            await pairing.close()

            return DeviceControlResult(
                success=True, entity_id=device.entity_id, action="pair_finish",
            )
        except Exception as e:
            try:
                await pairing.close()
            except Exception:
                pass
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="pair_finish",
                error=f"Pairing failed: {e}",
            )

    async def get_state(self, device: DiscoveredDevice) -> dict[str, Any]:
        try:
            import pyatv
            from pyatv.const import DeviceState
        except ImportError:
            return {"error": "pyatv is not installed"}

        ip: str = device.local_ip or ""
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
