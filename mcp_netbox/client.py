"""Async NetBox REST API client.

Wraps ``httpx.AsyncClient`` with authentication, timeout, TLS-verification
control, pagination helpers, and consistent error handling.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from .config import NetBoxConfig


class NetBoxAPIError(Exception):
    """Raised when the NetBox API returns an error or an unexpected response."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class NetBoxClient:
    """A thin async client for the NetBox REST API."""

    def __init__(self, config: NetBoxConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_url,
            headers={
                "Authorization": config.authorization,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(config.timeout_ms / 1000.0),
            verify=config.verify_ssl,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NetBoxClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # Core request helpers
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, path: str,
                       params: Optional[Dict[str, Any]] = None,
                       json: Optional[Any] = None) -> Any:
        """Perform a request and return the decoded JSON body.

        ``path`` is relative to the API root, e.g. ``/dcim/devices/``.
        """
        # Drop None values so optional filters are not sent as "None".
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        try:
            response = await self._client.request(method, path, params=params,
                                                  json=json)
        except httpx.TimeoutException as exc:
            raise NetBoxAPIError(
                f"Request to NetBox timed out after "
                f"{self._config.timeout_ms} ms. Try again or increase "
                f"netbox.timeout in config.yaml."
            ) from exc
        except httpx.HTTPError as exc:
            raise NetBoxAPIError(
                f"Network error contacting NetBox at {self._config.url}: "
                f"{exc.__class__.__name__}. Check that the URL is reachable."
            ) from exc

        return self._raise_for_status(response)

    def _raise_for_status(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            detail = self._safe_json(response)
            raise NetBoxAPIError(
                self._friendly_error(response.status_code, detail),
                status_code=response.status_code,
                detail=detail,
            )
        if response.status_code == 204 or not response.content:
            return None
        return self._safe_json(response)

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _friendly_error(status_code: int, detail: Any) -> str:
        if status_code == 401:
            return ("Authentication failed (401). The API token is invalid or "
                    "expired. Check 'netbox.token' in config.yaml.")
        if status_code == 403:
            return ("Permission denied (403). The token does not have access "
                    "to this resource.")
        if status_code == 404:
            return ("Resource not found (404). Verify the app, resource, and "
                    "ID are correct (see netbox_list_apps for valid "
                    "endpoints).")
        if status_code == 429:
            return "Rate limit exceeded (429). Wait a moment and retry."
        if status_code == 400:
            # NetBox returns validation errors as a dict of field -> messages.
            if isinstance(detail, dict):
                parts = []
                for field, msgs in detail.items():
                    if isinstance(msgs, list):
                        parts.append(f"{field}: {'; '.join(str(m) for m in msgs)}")
                    else:
                        parts.append(f"{field}: {msgs}")
                return "Invalid request (400). " + " | ".join(parts)
            return f"Invalid request (400). {detail}"
        return f"NetBox API error (HTTP {status_code}). {detail}"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def get(self, path: str,
                  params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def list_objects(self, app: str, resource: str,
                           params: Optional[Dict[str, Any]] = None,
                           limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """List objects from ``/<app>/<resource>/`` with pagination.

        Returns a dict with ``count``, ``results``, ``next``, ``previous``.
        """
        query: Dict[str, Any] = dict(params or {})
        query["limit"] = limit
        query["offset"] = offset
        data = await self.get(f"/{app}/{resource}/", params=query)
        if not isinstance(data, dict) or "results" not in data:
            raise NetBoxAPIError(
                f"Unexpected response from /{app}/{resource}/. The endpoint "
                f"may not exist. Use netbox_list_apps to discover valid "
                f"endpoints."
            )
        return data

    async def get_object(self, app: str, resource: str,
                         object_id: int) -> Dict[str, Any]:
        """Fetch a single object by ID from ``/<app>/<resource>/<id>/``."""
        return await self.get(f"/{app}/{resource}/{object_id}/")

    async def status(self) -> Dict[str, Any]:
        return await self.get("/status/")

    async def api_root(self) -> Dict[str, Any]:
        """Return the top-level API map (app -> endpoint URL)."""
        return await self.get("/")

    async def app_endpoints(self, app: str) -> Dict[str, Any]:
        """Return the endpoint map for a single app (e.g. ``dcim``)."""
        return await self.get(f"/{app}/")

    async def list_all(self, app: str, resource: str,
                       params: Optional[Dict[str, Any]] = None,
                       page_size: int = 100,
                       max_items: Optional[int] = None) -> List[Dict[str, Any]]:
        """Iterate through all pages of a list endpoint.

        Useful for small collections. Avoid for very large ones (e.g. all
        IP addresses) — prefer a filtered ``list_objects`` call.
        """
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            data = await self.list_objects(app, resource, params=params,
                                           limit=page_size, offset=offset)
            results = data.get("results", [])
            items.extend(results)
            if data.get("next") is None:
                break
            if max_items is not None and len(items) >= max_items:
                items = items[:max_items]
                break
            offset += page_size
        return items