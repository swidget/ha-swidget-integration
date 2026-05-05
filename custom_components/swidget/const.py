"""Constants for the Swidget integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "swidget"

PLATFORMS: Final[list[Platform]] = [Platform.SENSOR, Platform.SWITCH]

CONF_TOKEN_NAME: Final = "token_name"
CONF_SECRET_KEY: Final = "secret_key"
CONF_USE_HTTPS: Final = "use_https"

DEFAULT_TOKEN_NAME: Final = "x-secret-key"
DEFAULT_USE_HTTPS: Final = True
