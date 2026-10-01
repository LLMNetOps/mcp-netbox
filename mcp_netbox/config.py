"""Configuration loading for the NetBox MCP server.

Configuration is read from a ``config.yaml`` file with the following shape::

    netbox:
      url: https://netbox.example.com
      token: Token <api-token>
      timeout: 30000        # milliseconds
      verify_ssl: false

Environment variables override the file values (useful for secrets):

* ``NETBOX_CONFIG``     - explicit path to the config file
* ``NETBOX_URL``        - NetBox base URL
* ``NETBOX_TOKEN``      - API token (with or without the ``Token `` prefix)
* ``NETBOX_TIMEOUT``    - request timeout in milliseconds
* ``NETBOX_VERIFY_SSL`` - "true"/"false" to control TLS verification
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class NetBoxConfig:
    """Resolved NetBox connection settings."""

    url: str
    token: str
    timeout_ms: int = 30000
    verify_ssl: bool = True

    @property
    def base_url(self) -> str:
        """Base URL with any trailing slash removed."""
        return self.url.rstrip("/")

    @property
    def api_url(self) -> str:
        """The NetBox REST API root (``<base>/api``)."""
        return f"{self.base_url}/api"

    @property
    def authorization(self) -> str:
        """The value for the ``Authorization`` header.

        NetBox expects ``Token <token>``. If the configured token already
        carries the ``Token `` prefix it is used as-is.
        """
        token = self.token.strip()
        if token.lower().startswith("token "):
            return token
        return f"Token {token}"


def _find_config_file() -> Optional[Path]:
    """Locate ``config.yaml``.

    Search order:
    1. ``NETBOX_CONFIG`` environment variable (explicit path).
    2. ``config.yaml`` in the current working directory.
    3. ``config.yaml`` next to this package (the project root).
    """
    env_path = os.environ.get("NETBOX_CONFIG")
    if env_path:
        p = Path(env_path)
        return p if p.is_file() else None

    cwd = Path.cwd() / "config.yaml"
    if cwd.is_file():
        return cwd

    pkg_parent = Path(__file__).resolve().parent.parent / "config.yaml"
    if pkg_parent.is_file():
        return pkg_parent

    return None


def _parse_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(config_path: Optional[str] = None) -> NetBoxConfig:
    """Load and resolve the NetBox configuration.

    Args:
        config_path: Optional explicit path to a config file. When omitted,
            the file is auto-discovered (see ``_find_config_file``).

    Raises:
        RuntimeError: if the explicit path does not exist, or no configuration
            can be found, or required values (url, token) are missing.
    """
    if config_path:
        cfg_file = Path(config_path)
        if not cfg_file.is_file():
            raise RuntimeError(f"Config file not found: {cfg_file}")
    else:
        cfg_file = _find_config_file()

    data: dict = {}
    if cfg_file is not None:
        with open(cfg_file, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        data = loaded.get("netbox", {}) or {}

    url = os.environ.get("NETBOX_URL") or data.get("url")
    token = os.environ.get("NETBOX_TOKEN") or data.get("token")
    timeout = os.environ.get("NETBOX_TIMEOUT") or data.get("timeout", 30000)
    verify_ssl_env = os.environ.get("NETBOX_VERIFY_SSL")
    verify_ssl = (
        _parse_bool(verify_ssl_env, _parse_bool(data.get("verify_ssl"), True))
        if verify_ssl_env is not None
        else _parse_bool(data.get("verify_ssl"), True)
    )

    if not url:
        raise RuntimeError(
            "NetBox URL is not configured. Set 'netbox.url' in config.yaml "
            "or the NETBOX_URL environment variable."
        )
    if not token:
        raise RuntimeError(
            "NetBox API token is not configured. Set 'netbox.token' in "
            "config.yaml or the NETBOX_TOKEN environment variable."
        )

    try:
        timeout_ms = int(timeout)
    except (TypeError, ValueError):
        timeout_ms = 30000

    return NetBoxConfig(
        url=str(url),
        token=str(token),
        timeout_ms=timeout_ms,
        verify_ssl=verify_ssl,
    )