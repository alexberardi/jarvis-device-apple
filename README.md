# jarvis-device-apple

Apple TV and HomePod protocol adapter via AirPlay/Companion for [Jarvis](https://github.com/alexberardi/jarvis-node-setup).

## Install

```bash
python scripts/command_store.py install --url https://github.com/alexberardi/jarvis-device-apple
```

## Supported Devices

- Apple TV (all generations with tvOS)
- HomePod
- HomePod mini

## Secrets

No secrets required — works over LAN.

## Structure

```
device_families/apple/protocol.py   # Device protocol adapter
```
