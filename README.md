# Swidget — Home Assistant Custom Integration

Home Assistant custom integration for [Swidget](https://swidget.com) smart home devices (outlets, switches, dimmers, timer switches). Built on top of the [`python-swidget`](https://pypi.org/project/python-swidget/) SDK for local control over HTTP and websockets.

## Features

- Local control — no cloud required
- Realtime state updates via websockets
- SSDP autodiscovery
- Supports the full Swidget device lineup:
  - Outlets
  - Switches
  - Dimmers
  - Timer switches

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** → **⋮** → **Custom repositories**.
3. Add this repository's URL with category **Integration**.
4. Install **Swidget** from the HACS integrations list.
5. Restart Home Assistant.

### Manual

1. Copy `custom_components/swidget/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

After installation, add the integration via **Settings → Devices & Services → Add Integration → Swidget**. Discovered devices will be offered automatically; otherwise enter the device's host and credentials manually.

You will need:
- Device host/IP
- Token name (header)
- Secret key

## Development

This integration depends on [`python-swidget`](https://github.com/swidget/python-swidget). The library lives at `../python-swidget` relative to this repo during local development.

## License

GPL-3.0-or-later
