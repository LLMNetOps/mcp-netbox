"""NetBox MCP server.

Exposes a NetBox DCIM/IPAM instance as a set of **read-only** MCP tools so an
LLM can explore sites, devices, IPAM, circuits, virtualization, tenancy, and
the netbox-dns plugin.

The server is served over **Streamable HTTP** at ``http://<host>:<port>/mcp``.

Run with::

    python mcp-netbox/server.py

Useful options::

    python mcp-netbox/server.py --host 0.0.0.0 --port 5756
    python mcp-netbox/server.py --config /path/to/config.yaml
    python mcp-netbox/server.py --daemon          # run in the background
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Annotated, Any, Dict, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import NetBoxAPIError, NetBoxClient
from .config import NetBoxConfig, load_config
from .formatting import (
    format_list_json,
    format_list_markdown,
    format_object_json,
    format_object_markdown,
    to_json,
)

# --------------------------------------------------------------------------- #
# Server instance
# --------------------------------------------------------------------------- #
mcp = FastMCP(
    name="mcp-netbox",
    instructions=(
        "Read-only tools for exploring a NetBox DCIM/IPAM instance. "
        "Explore a NetBox instance (sites, regions, locations, devices, "
        "interfaces, racks, IP prefixes, IP addresses, VLANs, VRFs, ASNs, "
        "circuits, providers, clusters, virtual machines, tenants, and DNS "
        "zones/records). All tools are read-only. Use netbox_status to verify "
        "connectivity, netbox_list_apps to discover endpoints, and the "
        "generic netbox_list_objects / netbox_get_object tools for any "
        "resource not covered by a dedicated tool. List tools support "
        "pagination via limit/offset and return markdown tables or JSON."
    ),
)

# Shared annotations: every tool is a safe, read-only, idempotent query.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# --------------------------------------------------------------------------- #
# Lazy client / config
# --------------------------------------------------------------------------- #
_config: Optional[NetBoxConfig] = None
_config_error: Optional[str] = None
_config_path: Optional[str] = None
_client: Optional[NetBoxClient] = None


def _get_client() -> NetBoxClient:
    """Return a shared NetBoxClient, loading config on first use."""
    global _config, _config_error, _client
    if _client is None:
        if _config is None and _config_error is None:
            try:
                _config = load_config(_config_path)
            except Exception as exc:  # noqa: BLE001 - report any config issue
                _config_error = str(exc)
        if _config_error is not None:
            raise NetBoxAPIError(f"Configuration error: {_config_error}")
        _client = NetBoxClient(_config)
    return _client


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _params(**filters: Any) -> Dict[str, Any]:
    """Build a query dict, dropping None values."""
    return {k: v for k, v in filters.items() if v is not None}


def _err(exc: Exception) -> str:
    if isinstance(exc, NetBoxAPIError):
        return f"⚠️ {exc}"
    return f"❌ Unexpected error: {exc.__class__.__name__}: {exc}"


async def _safe_list(
    app: str,
    resource: str,
    params: Dict[str, Any],
    limit: int,
    offset: int,
    columns: list[str],
    title: str,
    response_format: str,
) -> str:
    """List objects and format the result, converting errors to text."""
    try:
        data = await _get_client().list_objects(
            app, resource, params=params, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if response_format == "json":
        return format_list_json(data, offset)
    return format_list_markdown(data, columns, title, offset)


async def _safe_get(
    app: str,
    resource: str,
    object_id: int,
    title: str,
    response_format: str,
) -> str:
    """Fetch a single object and format the result."""
    try:
        obj = await _get_client().get_object(app, resource, object_id)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if response_format == "json":
        return format_object_json(obj)
    return format_object_markdown(obj, title)


# Common parameter annotations (reused across tools).
Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of "
                       "results to return (1-100). Default 20.")]
Offset = Annotated[int, Field(ge=0, description="Number of results to skip "
                        "for pagination. Default 0.")]
Fmt = Annotated[str, Field(description="Output format: 'markdown' "
                     "(human-readable table) or 'json' (machine-readable).")]
Q = Annotated[Optional[str], Field(description="Global search string matched "
        "against most fields of the object.")]


# =========================================================================== #
# Status / discovery
# =========================================================================== #
@mcp.tool(name="netbox_status", title="NetBox Status",
          description="Check connectivity to the NetBox instance and return "
                      "its version, Python version, and installed plugins. "
                      "Call this first to verify the server is reachable.",
          annotations=_READ_ONLY)
async def netbox_status(
    response_format: Fmt = "markdown",
) -> str:
    try:
        data = await _get_client().status()
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if response_format == "json":
        return to_json(data)
    lines = ["## NetBox Status", ""]
    if data.get("version"):
        lines.append(f"- **NetBox version:** {data['version']}")
    if data.get("ninja_version"):
        lines.append(f"- **Ninja version:** {data['ninja_version']}")
    if data.get("python_version"):
        lines.append(f"- **Python version:** {data['python_version']}")
    if data.get("django_version"):
        lines.append(f"- **Django version:** {data['django_version']}")
    plugins = data.get("plugins") or {}
    if plugins:
        lines.append(f"- **Plugins ({len(plugins)}):**")
        for name, info in plugins.items():
            ver = info.get("version", "?") if isinstance(info, dict) else "?"
            lines.append(f"  - {name} {ver}")
    return "\n".join(lines)


@mcp.tool(name="netbox_list_apps", title="List API Apps",
          description="List the NetBox API apps and their endpoints. Without "
                      "an 'app' argument, returns a summary (name, version, "
                      "endpoint count) of every app. Pass 'app' (e.g. 'dcim') "
                      "to list that app's endpoint names in detail. Use this "
                      "to discover valid resource names for "
                      "netbox_list_objects.",
          annotations=_READ_ONLY)
async def netbox_list_apps(
    app: Annotated[Optional[str], Field(description="Optional app name "
        "(e.g. 'dcim', 'ipam') to list its endpoints in detail.")] = None,
    response_format: Fmt = "markdown",
) -> str:
    try:
        apps = await _get_client().get_apps()
    except Exception as exc:  # noqa: BLE001
        return _err(exc)

    if app:
        match = next((a for a in apps if a.get("name") == app), None)
        if match is None:
            names = ", ".join(sorted(a.get("name", "?") for a in apps))
            return f"⚠️ App '{app}' not found. Available apps: {names}"
        endpoints = match.get("endpoints") or {}
        if response_format == "json":
            return to_json(match)
        lines = [f"## App: {app} ({match.get('version', '?')})", ""]
        lines.append(f"**Endpoints ({len(endpoints)}):**")
        lines.append("")
        for ep in sorted(endpoints):
            lines.append(f"- `{ep}`")
        return "\n".join(lines)

    # Summary of all apps.
    if response_format == "json":
        return to_json(apps)
    lines = ["## NetBox API Apps", ""]
    lines.append("| App | Version | Endpoints |")
    lines.append("| --- | --- | --- |")
    for a in sorted(apps, key=lambda x: x.get("name", "")):
        eps = a.get("endpoints") or {}
        lines.append(f"| {a.get('name', '?')} | {a.get('version', '?')} "
                     f"| {len(eps)} |")
    lines.append("")
    lines.append("_Pass `app` to list an app's endpoints in detail._")
    return "\n".join(lines)


# =========================================================================== #
# Generic (comprehensive API coverage)
# =========================================================================== #
@mcp.tool(name="netbox_list_objects", title="List Objects (generic)",
          description="List objects from ANY NetBox API endpoint. Use this for "
                      "resources without a dedicated tool, or to apply "
                      "arbitrary filters. 'app' is the API app (e.g. 'dcim', "
                      "'ipam', 'circuits', 'virtualization', 'tenancy', "
                      "'extras', 'wireless', 'vpn', 'core'); 'resource' is the "
                      "endpoint name (e.g. 'devices', 'prefixes', 'sites'). "
                      "Use netbox_list_apps to discover valid names. "
                      "query_params accepts any NetBox filter as key/value "
                      "pairs, e.g. {\"site\": \"gkm\", \"status\": \"active\"}.",
          annotations=_READ_ONLY)
async def netbox_list_objects(
    app: Annotated[str, Field(description="NetBox API app, e.g. 'dcim', "
        "'ipam', 'circuits', 'virtualization', 'tenancy', 'extras', "
        "'wireless', 'vpn', 'core'.")],
    resource: Annotated[str, Field(description="Endpoint/resource name, e.g. "
        "'devices', 'prefixes', 'sites'. See netbox_list_apps for valid "
        "names.")],
    q: Q = None,
    query_params: Annotated[Optional[Dict[str, str]], Field(
        description="Additional NetBox query parameters as key/value pairs, "
                    "e.g. {\"site\": \"gkm\", \"status\": \"active\"}.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    params = _params(q=q, **(query_params or {}))
    # Generic tools show a compact set of common columns.
    columns = ["id", "name", "slug", "status", "description"]
    return await _safe_list(app, resource, params, limit, offset,
                            columns, f"{app}/{resource}", response_format)


@mcp.tool(name="netbox_get_object", title="Get Object (generic)",
          description="Fetch a single object by numeric ID from ANY NetBox "
                      "API endpoint. 'app' is the API app (e.g. 'dcim'), "
                      "'resource' the endpoint name (e.g. 'devices'), and "
                      "'object_id' the numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_object(
    app: Annotated[str, Field(description="NetBox API app, e.g. 'dcim', "
        "'ipam'.")],
    resource: Annotated[str, Field(description="Endpoint/resource name, e.g. "
        "'devices', 'prefixes'.")],
    object_id: Annotated[int, Field(ge=1, description="Numeric ID of the "
        "object to fetch.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get(app, resource, object_id,
                           f"{app}/{resource}", response_format)


# =========================================================================== #
# Regions / sites / locations
# =========================================================================== #
@mcp.tool(name="netbox_list_regions", title="List Regions",
          description="List geographic regions (e.g. country/area groupings).",
          annotations=_READ_ONLY)
async def netbox_list_regions(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "regions", _params(q=q), limit, offset,
                            ["id", "name", "slug", "parent"], "Regions",
                            response_format)


@mcp.tool(name="netbox_list_sites", title="List Sites",
          description="List sites (physical locations / data centers).",
          annotations=_READ_ONLY)
async def netbox_list_sites(
    q: Q = None,
    region: Annotated[Optional[str], Field(description="Filter by region "
        "slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status, "
        "e.g. 'active', 'planned', 'decommissioning'.")] = None,
    asn: Annotated[Optional[int], Field(description="Filter by ASN number.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "sites",
                            _params(q=q, region=region, status=status,
                                    asn=asn),
                            limit, offset,
                            ["id", "name", "slug", "status", "region", "asn"],
                            "Sites", response_format)


@mcp.tool(name="netbox_get_site", title="Get Site",
          description="Fetch a single site by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_site(
    site_id: Annotated[int, Field(ge=1, description="Numeric site ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("dcim", "sites", site_id, "Site", response_format)


@mcp.tool(name="netbox_list_locations", title="List Locations",
          description="List locations (sub-areas within a site, e.g. halls, "
                      "rooms, floors).",
          annotations=_READ_ONLY)
async def netbox_list_locations(
    q: Q = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    parent: Annotated[Optional[str], Field(description="Filter by parent "
        "location slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "locations",
                            _params(q=q, site=site, parent=parent,
                                    status=status),
                            limit, offset,
                            ["id", "name", "slug", "site", "parent", "status"],
                            "Locations", response_format)


@mcp.tool(name="netbox_get_location", title="Get Location",
          description="Fetch a single location by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_location(
    location_id: Annotated[int, Field(ge=1, description="Numeric location ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("dcim", "locations", location_id, "Location",
                           response_format)


# =========================================================================== #
# Devices
# =========================================================================== #
@mcp.tool(name="netbox_list_devices", title="List Devices",
          description="List devices (physical network/IT hardware).",
          annotations=_READ_ONLY)
async def netbox_list_devices(
    q: Q = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    location: Annotated[Optional[str], Field(description="Filter by location "
        "slug or ID.")] = None,
    role: Annotated[Optional[str], Field(description="Filter by device role "
        "slug or ID.")] = None,
    manufacturer: Annotated[Optional[str], Field(description="Filter by "
        "manufacturer slug or ID.")] = None,
    device_type: Annotated[Optional[str], Field(description="Filter by device "
        "type slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status, "
        "e.g. 'active', 'offline'.")] = None,
    name: Annotated[Optional[str], Field(description="Exact device name.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "devices",
                            _params(q=q, site=site, location=location,
                                    role=role, manufacturer=manufacturer,
                                    device_type=device_type, status=status,
                                    name=name),
                            limit, offset,
                            ["id", "name", "device_type", "role", "site",
                             "location", "status", "primary_ip4"],
                            "Devices", response_format)


@mcp.tool(name="netbox_get_device", title="Get Device",
          description="Fetch a single device by its numeric ID, including "
                      "details such as serial, asset tag, platform, and "
                      "primary IP.",
          annotations=_READ_ONLY)
async def netbox_get_device(
    device_id: Annotated[int, Field(ge=1, description="Numeric device ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("dcim", "devices", device_id, "Device",
                           response_format)


@mcp.tool(name="netbox_list_device_types", title="List Device Types",
          description="List device types (hardware models).",
          annotations=_READ_ONLY)
async def netbox_list_device_types(
    q: Q = None,
    manufacturer: Annotated[Optional[str], Field(description="Filter by "
        "manufacturer slug or ID.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "device_types",
                            _params(q=q, manufacturer=manufacturer),
                            limit, offset,
                            ["id", "manufacturer", "model", "part_number",
                             "u_height"],
                            "Device Types", response_format)


@mcp.tool(name="netbox_list_device_roles", title="List Device Roles",
          description="List device roles (e.g. router, switch, server).",
          annotations=_READ_ONLY)
async def netbox_list_device_roles(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "device_roles", _params(q=q), limit,
                            offset, ["id", "name", "slug", "color"],
                            "Device Roles", response_format)


@mcp.tool(name="netbox_list_manufacturers", title="List Manufacturers",
          description="List hardware manufacturers (e.g. Cisco, Juniper).",
          annotations=_READ_ONLY)
async def netbox_list_manufacturers(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "manufacturers", _params(q=q), limit,
                            offset, ["id", "name", "slug", "part_number"],
                            "Manufacturers", response_format)


@mcp.tool(name="netbox_list_interfaces", title="List Interfaces",
          description="List device interfaces (physical/virtual ports).",
          annotations=_READ_ONLY)
async def netbox_list_interfaces(
    q: Q = None,
    device: Annotated[Optional[int], Field(description="Filter by device ID.")] = None,
    kind: Annotated[Optional[str], Field(description="Filter by interface "
        "kind, e.g. 'virtual', 'bridge', 'lag', 'wireless'.")] = None,
    enabled: Annotated[Optional[bool], Field(description="Filter by enabled "
        "state.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "interfaces",
                            _params(q=q, device_id=device, kind=kind,
                                    enabled=enabled),
                            limit, offset,
                            ["id", "device", "name", "type", "enabled",
                             "mac_address"],
                            "Interfaces", response_format)


@mcp.tool(name="netbox_get_interface", title="Get Interface",
          description="Fetch a single interface by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_interface(
    interface_id: Annotated[int, Field(ge=1, description="Numeric interface "
        "ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("dcim", "interfaces", interface_id, "Interface",
                           response_format)


# =========================================================================== #
# Racks
# =========================================================================== #
@mcp.tool(name="netbox_list_racks", title="List Racks",
          description="List racks (server/network racks).",
          annotations=_READ_ONLY)
async def netbox_list_racks(
    q: Q = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    location: Annotated[Optional[str], Field(description="Filter by location "
        "slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("dcim", "racks",
                            _params(q=q, site=site, location=location,
                                    status=status),
                            limit, offset,
                            ["id", "name", "site", "location", "status",
                             "type"],
                            "Racks", response_format)


@mcp.tool(name="netbox_get_rack", title="Get Rack",
          description="Fetch a single rack by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_rack(
    rack_id: Annotated[int, Field(ge=1, description="Numeric rack ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("dcim", "racks", rack_id, "Rack", response_format)


# =========================================================================== #
# IPAM
# =========================================================================== #
@mcp.tool(name="netbox_list_prefixes", title="List Prefixes",
          description="List IP prefixes (subnets).",
          annotations=_READ_ONLY)
async def netbox_list_prefixes(
    q: Q = None,
    prefix: Annotated[Optional[str], Field(description="Exact prefix, e.g. "
        "'10.0.0.0/24'.")] = None,
    contains: Annotated[Optional[str], Field(description="Prefixes that "
        "contain this address/prefix, e.g. '10.0.0.1'.")] = None,
    vrf: Annotated[Optional[str], Field(description="Filter by VRF name or "
        "ID.")] = None,
    vlan: Annotated[Optional[str], Field(description="Filter by VLAN ID.")] = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("ipam", "prefixes",
                            _params(q=q, prefix=prefix, contains=contains,
                                    vrf=vrf, vlan=vlan, site=site,
                                    status=status),
                            limit, offset,
                            ["id", "prefix", "vlan", "vrf", "site", "status",
                             "description"],
                            "Prefixes", response_format)


@mcp.tool(name="netbox_get_prefix", title="Get Prefix",
          description="Fetch a single IP prefix by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_prefix(
    prefix_id: Annotated[int, Field(ge=1, description="Numeric prefix ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("ipam", "prefixes", prefix_id, "Prefix",
                           response_format)


@mcp.tool(name="netbox_list_ip_addresses", title="List IP Addresses",
          description="List IP addresses.",
          annotations=_READ_ONLY)
async def netbox_list_ip_addresses(
    q: Q = None,
    vrf: Annotated[Optional[str], Field(description="Filter by VRF name or "
        "ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status, "
        "e.g. 'active', 'reserved'.")] = None,
    assigned: Annotated[Optional[bool], Field(description="true = only "
        "assigned addresses, false = only unassigned.")] = None,
    family: Annotated[Optional[int], Field(ge=4, le=6, description="IP "
        "version: 4 or 6.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("ipam", "ip-addresses",
                            _params(q=q, vrf=vrf, status=status,
                                    assigned=assigned, family=family),
                            limit, offset,
                            ["id", "address", "status", "vrf", "vlan",
                             "description"],
                            "IP Addresses", response_format)


@mcp.tool(name="netbox_get_ip_address", title="Get IP Address",
          description="Fetch a single IP address by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_ip_address(
    ip_address_id: Annotated[int, Field(ge=1, description="Numeric IP address "
        "ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("ipam", "ip-addresses", ip_address_id,
                           "IP Address", response_format)


@mcp.tool(name="netbox_list_vlans", title="List VLANs",
          description="List VLANs.",
          annotations=_READ_ONLY)
async def netbox_list_vlans(
    q: Q = None,
    vid: Annotated[Optional[int], Field(ge=1, le=4094, description="Filter "
        "by VLAN ID.")] = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    vlan_group: Annotated[Optional[str], Field(description="Filter by VLAN "
        "group slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("ipam", "vlans",
                            _params(q=q, vid=vid, site=site,
                                    vlan_group=vlan_group, status=status),
                            limit, offset,
                            ["id", "vid", "name", "vlan_group", "site",
                             "status"],
                            "VLANs", response_format)


@mcp.tool(name="netbox_get_vlan", title="Get VLAN",
          description="Fetch a single VLAN by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_vlan(
    vlan_id: Annotated[int, Field(ge=1, description="Numeric VLAN ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("ipam", "vlans", vlan_id, "VLAN", response_format)


@mcp.tool(name="netbox_list_vrfs", title="List VRFs",
          description="List VRFs (Virtual Routing and Forwarding instances).",
          annotations=_READ_ONLY)
async def netbox_list_vrfs(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("ipam", "vrfs", _params(q=q), limit, offset,
                            ["id", "name", "rd", "enforce"], "VRFs",
                            response_format)


@mcp.tool(name="netbox_list_asns", title="List ASNs",
          description="List ASNs (Autonomous System Numbers).",
          annotations=_READ_ONLY)
async def netbox_list_asns(
    q: Q = None,
    asn: Annotated[Optional[int], Field(description="Exact ASN number.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("ipam", "asns", _params(q=q, asn=asn), limit,
                            offset, ["id", "asn", "rir", "description"],
                            "ASNs", response_format)


# =========================================================================== #
# Circuits
# =========================================================================== #
@mcp.tool(name="netbox_list_circuits", title="List Circuits",
          description="List circuits (carrier/ISP connections).",
          annotations=_READ_ONLY)
async def netbox_list_circuits(
    q: Q = None,
    provider: Annotated[Optional[str], Field(description="Filter by provider "
        "slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("circuits", "circuits",
                            _params(q=q, provider=provider, status=status),
                            limit, offset,
                            ["id", "cid", "provider", "status", "type"],
                            "Circuits", response_format)


@mcp.tool(name="netbox_get_circuit", title="Get Circuit",
          description="Fetch a single circuit by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_circuit(
    circuit_id: Annotated[int, Field(ge=1, description="Numeric circuit ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("circuits", "circuits", circuit_id, "Circuit",
                           response_format)


@mcp.tool(name="netbox_list_providers", title="List Providers",
          description="List circuit providers (carriers/ISPs).",
          annotations=_READ_ONLY)
async def netbox_list_providers(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("circuits", "providers", _params(q=q), limit,
                            offset, ["id", "name", "slug", "account"],
                            "Providers", response_format)


# =========================================================================== #
# Virtualization
# =========================================================================== #
@mcp.tool(name="netbox_list_clusters", title="List Clusters",
          description="List virtualization clusters (e.g. VMware vCenter, "
                      "OpenStack).",
          annotations=_READ_ONLY)
async def netbox_list_clusters(
    q: Q = None,
    site: Annotated[Optional[str], Field(description="Filter by site slug or "
        "ID.")] = None,
    type: Annotated[Optional[str], Field(description="Filter by cluster type "
        "slug or ID.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("virtualization", "clusters",
                            _params(q=q, site=site, type=type),
                            limit, offset,
                            ["id", "name", "type", "site", "status"],
                            "Clusters", response_format)


@mcp.tool(name="netbox_list_virtual_machines", title="List Virtual Machines",
          description="List virtual machines.",
          annotations=_READ_ONLY)
async def netbox_list_virtual_machines(
    q: Q = None,
    cluster: Annotated[Optional[str], Field(description="Filter by cluster "
        "slug or ID.")] = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("virtualization", "virtual_machines",
                            _params(q=q, cluster=cluster, status=status),
                            limit, offset,
                            ["id", "name", "cluster", "status", "role"],
                            "Virtual Machines", response_format)


@mcp.tool(name="netbox_get_virtual_machine", title="Get Virtual Machine",
          description="Fetch a single virtual machine by its numeric ID.",
          annotations=_READ_ONLY)
async def netbox_get_virtual_machine(
    vm_id: Annotated[int, Field(ge=1, description="Numeric virtual machine "
        "ID.")],
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_get("virtualization", "virtual_machines", vm_id,
                           "Virtual Machine", response_format)


# =========================================================================== #
# Tenancy
# =========================================================================== #
@mcp.tool(name="netbox_list_tenants", title="List Tenants",
          description="List tenants (customers/organizations).",
          annotations=_READ_ONLY)
async def netbox_list_tenants(
    q: Q = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("tenancy", "tenants", _params(q=q), limit, offset,
                            ["id", "name", "slug", "description"], "Tenants",
                            response_format)


# =========================================================================== #
# DNS (netbox-dns plugin)
# =========================================================================== #
@mcp.tool(name="netbox_list_dns_zones", title="List DNS Zones",
          description="List DNS zones (from the netbox-dns plugin).",
          annotations=_READ_ONLY)
async def netbox_list_dns_zones(
    q: Q = None,
    status: Annotated[Optional[str], Field(description="Filter by status.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("plugins", "netbox-dns/zones",
                            _params(q=q, status=status),
                            limit, offset,
                            ["id", "name", "zone_type", "status",
                             "soa_serial"],
                            "DNS Zones", response_format)


@mcp.tool(name="netbox_list_dns_records", title="List DNS Records",
          description="List DNS records (from the netbox-dns plugin).",
          annotations=_READ_ONLY)
async def netbox_list_dns_records(
    q: Q = None,
    zone: Annotated[Optional[int], Field(description="Filter by zone ID.")] = None,
    type: Annotated[Optional[str], Field(description="Filter by record type, "
        "e.g. 'A', 'AAAA', 'CNAME', 'MX'.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    response_format: Fmt = "markdown",
) -> str:
    return await _safe_list("plugins", "netbox-dns/records",
                            _params(q=q, zone_id=zone, type=type),
                            limit, offset,
                            ["id", "name", "type", "value", "zone", "ttl"],
                            "DNS Records", response_format)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="mcp-netbox",
        description="NetBox MCP server (read-only) served over Streamable HTTP.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Interface to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5756,
        help="Port to listen on (default: 5756)",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to config.yaml (default: auto-discover)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in the background (detach) and exit",
    )
    # Hidden: set on the re-launched child so it records its own (real) PID.
    parser.add_argument(
        "--daemon-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _spawn_daemon(args: argparse.Namespace) -> None:
    """Re-launch this script as a detached background process, then exit.

    The child runs the server in the foreground (no ``--daemon``), with its
    output appended to ``mcp-netbox.log``. The child writes its own PID to
    ``mcp-netbox.pid`` so it can be stopped later (e.g. ``taskkill /PID`` on
    Windows, ``kill`` on Unix).

    The child records its *own* PID (rather than the parent recording
    ``proc.pid``) because on Windows a venv ``python.exe`` is a launcher stub
    that re-spawns the real interpreter; the launcher PID would not be the
    process actually serving the port.
    """
    cmd = [
        sys.executable,
        "-m",
        "mcp_netbox.server",
        "--host", args.host,
        "--port", str(args.port),
        "--daemon-child",
    ]
    if args.config:
        cmd += ["--config", args.config]

    log_path = os.path.join(os.getcwd(), "mcp-netbox.log")
    pid_path = os.path.join(os.getcwd(), "mcp-netbox.pid")
    if os.path.exists(pid_path):
        os.remove(pid_path)
    log_file = open(log_path, "a", encoding="utf-8")

    kwargs: Dict[str, Any] = dict(
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW: fully detach from the console.
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)
    log_file.close()

    # Wait briefly for the daemon to write its real PID.
    pid = None
    for _ in range(50):
        if os.path.exists(pid_path):
            try:
                with open(pid_path, encoding="utf-8") as fh:
                    pid = fh.read().strip()
                if pid:
                    break
            except OSError:
                pass
        time.sleep(0.1)

    print(f"mcp-netbox daemon started" + (f" (pid {pid})" if pid else ""))
    print(f"  endpoint: http://{args.host}:{args.port}/mcp")
    print(f"  log:      {log_path}")
    print(f"  pid file: {pid_path}")


def main() -> None:
    """Parse CLI args and run the MCP server over Streamable HTTP."""
    global _config_path
    args = _parse_args()
    _config_path = args.config

    if args.daemon:
        _spawn_daemon(args)
        return

    if args.daemon_child:
        # Record our own PID (the real serving process, not the venv launcher).
        pid_path = os.path.join(os.getcwd(), "mcp-netbox.pid")
        try:
            with open(pid_path, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    print(
        f"mcp-netbox listening on http://{args.host}:{args.port}/mcp",
        file=sys.stderr,
    )
    try:
        mcp.run(
            transport="http",
            host=args.host,
            port=args.port,
            path="/mcp",
        )
    finally:
        if _client is not None:
            import asyncio
            try:
                asyncio.run(_client.aclose())
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


if __name__ == "__main__":
    main()