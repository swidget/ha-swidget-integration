"""Constants for the Swidget integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "swidget"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONF_TOKEN_NAME: Final = "token_name"
CONF_SECRET_KEY: Final = "secret_key"
CONF_USE_HTTPS: Final = "use_https"

DEFAULT_TOKEN_NAME: Final = "x-secret-key"
DEFAULT_USE_HTTPS: Final = True


# Friendly host-type labels for hosts that aren't WiFi inserts. WiFi
# inserts are the default — they share the standard outlet/switch/
# dimmer chassis with every other Swidget product, including video and
# UPNP, which are distinguished by their *insert* rather than the host.
_FAN_HOST_MODELS: Final[dict[str, str]] = {
    "pesna_fv05": "Swidget Fan Control — Panasonic FV05",
    "pesna_fv15": "Swidget Fan Control — Panasonic FV15",
    "pesna_fv20": "Swidget Fan Control — Panasonic FV20",
    "pesna_IB150": "Swidget Fan Control — Panasonic IB150",
    "pesna_IB160": "Swidget Fan Control — Panasonic IB160",
    "pesna_fv05_G5": "Swidget Fan Control — Panasonic FV05 G5",
    "pesna_fv05_wrong_slot": "Swidget Fan Control (wrong slot)",
    "pesna_unrecognized": "Swidget Fan Control",
    "pesna_error": "Swidget Fan Control (error)",
}


def friendly_host_model(host_type: object, insert_type: object = None) -> str:
    """Return the display label for a Swidget device.

    The product family is determined by host *and* insert: a video
    device is a normal WiFi-insert chassis with a video insert, so we
    check the insert first. Fan controllers are distinguished by their
    host type (Pesna*). Everything else is a WiFi insert — that's the
    canonical product name for the outlet/switch/dimmer chassis on its
    own and with non-video inserts.
    """
    insert_raw = getattr(insert_type, "value", insert_type)
    if isinstance(insert_raw, str) and insert_raw == "video":
        return "Swidget Video"
    host_raw = getattr(host_type, "value", host_type)
    if isinstance(host_raw, str):
        fan_label = _FAN_HOST_MODELS.get(host_raw)
        if fan_label is not None:
            return fan_label
    return "Swidget WiFi Insert"
