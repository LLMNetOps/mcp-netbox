# mcp-netbox

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that
exposes a [NetBox](https://netbox.io) DCIM/IPAM instance as a set of
**read-only** tools. It lets an LLM (e.g. Claude, Copilot, or any MCP client)
explore sites, regions, locations, devices, interfaces, racks, IP prefixes, IP
addresses, VLANs, VRFs, ASNs, circuits, providers, clusters, virtual machines,
tenants, and DNS zones/records (via the `netbox-dns` plugin).

Built with [FastMCP](https://github.com/jlowin/fastmcp) and `httpx`.

## Features

- **Read-only & safe** — every tool is annotated `read_only` / non-destructive /
  idempotent. No writes are ever performed.
- **Dedicated tools** for the most common resources, plus **generic tools**
  (`netbox_list_objects`, `netbox_get_object`) that reach *any* NetBox API
  endpoint.
- **Two output formats** — human-readable Markdown tables or machine-readable
  JSON, selectable per call.
- **Pagination** — `limit` / `offset` on every list tool, with a hint about the
  next page when more results exist.
- **Rich filtering** — per-resource filters (site, region, status, VRF, VLAN,
  manufacturer, …) plus a free-form `query_params` map on the generic tool.
- **Config via file or environment** — `config.yaml` with environment
  overrides for secrets.

## Configuration

The server reads a `config.yaml` file. Copy the provided example and fill in
your values:

```bash
cp config.example.yaml config.yaml
```

```yaml
netbox:
  url: https://netbox.example.com
  token: Token your-api-token-here
  timeout: 30000        # milliseconds
  verify_ssl: true
```

The config file is located in this order:

1. The `--config` command-line argument (if provided).
2. The path in the `NETBOX_CONFIG` environment variable.
3. `config.yaml` in the current working directory.
4. `config.yaml` next to the `mcp_netbox` package (the project root).

Environment variables override the file values (handy for secrets):

| Variable            | Meaning                                   |
| ------------------- | ----------------------------------------- |
| `NETBOX_CONFIG`     | Explicit path to the config file          |
| `NETBOX_URL`        | NetBox base URL                           |
| `NETBOX_TOKEN`      | API token (with or without `Token ` prefix) |
| `NETBOX_TIMEOUT`    | Request timeout in milliseconds           |
| `NETBOX_VERIFY_SSL` | `true` / `false` to control TLS verification |

> The token may include the `Token ` prefix or not — both are handled.
> `config.yaml` is git-ignored; never commit a real token.

## Installation

The package is installable with **pipx** (recommended) or **pip**.

```bash
# from a local clone of this repository
pipx install mcp-netbox

# or straight from GitHub (replace <your-username>)
pipx install "git+https://github.com/LLMNetOps/mcp-netbox.git"

# or with pip into a virtualenv
pip install .
```

## Running the server

```bash
mcp-netbox
```

The server speaks MCP over **Streamable HTTP** at `http://<host>:<port>/mcp`.
You can also run it as a module: `python -m mcp_netbox`.

### CLI options

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Interface to bind. |
| `--port` | `5756` | Port to listen on. |
| `--config` | auto-discover | Path to `config.yaml`. |
| `--daemon` | off | Run in the background (detached) and exit. |

Examples:

```bash
# foreground, defaults (http://0.0.0.0:5756/mcp)
mcp-netbox

# custom host/port and explicit config
mcp-netbox --host 0.0.0.0 --port 5756 --config ./config.yaml

# run in the background (writes mcp-netbox.log and mcp-netbox.pid)
mcp-netbox --daemon
```

With `--daemon`, the process detaches and the parent exits. The daemon appends
its output to `mcp-netbox.log` and writes its PID to `mcp-netbox.pid` (in the
current working directory) so you can stop it later (e.g. `taskkill /PID <pid>`
on Windows, `kill <pid>` on Unix).

## Using it with an MCP client

The server is a **remote** MCP server over Streamable HTTP, so clients connect
to a URL rather than spawning a process. Start it (e.g. `mcp-netbox --daemon`),
then point your client at `http://<host>:<port>/mcp`.

Example (any client that supports Streamable HTTP / remote MCP servers):

```json
{
  "mcpServers": {
    "netbox": {
      "url": "http://127.0.0.1:5756/mcp"
    }
  }
}
```

Replace `127.0.0.1` with the host/IP where the server is running and `5756`
with the port you chose.

## Tools

### Status & discovery
| Tool | Description |
| --- | --- |
| `netbox_status` | Verify connectivity; show NetBox version and plugins. |
| `netbox_list_apps` | List API apps and their endpoints (pass `app` for detail). |

### Generic (any endpoint)
| Tool | Description |
| --- | --- |
| `netbox_list_objects` | List objects from any `app/resource` with arbitrary filters. |
| `netbox_get_object` | Fetch a single object by `app/resource/id`. |

### Sites / regions / locations
`netbox_list_regions`, `netbox_list_sites`, `netbox_get_site`,
`netbox_list_locations`, `netbox_get_location`

### Devices
`netbox_list_devices`, `netbox_get_device`, `netbox_list_device_types`,
`netbox_list_device_roles`, `netbox_list_manufacturers`,
`netbox_list_interfaces`, `netbox_get_interface`

### Racks
`netbox_list_racks`, `netbox_get_rack`

### IPAM
`netbox_list_prefixes`, `netbox_get_prefix`, `netbox_list_ip_addresses`,
`netbox_get_ip_address`, `netbox_list_vlans`, `netbox_get_vlan`,
`netbox_list_vrfs`, `netbox_list_asns`

### Circuits
`netbox_list_circuits`, `netbox_get_circuit`, `netbox_list_providers`

### Virtualization
`netbox_list_clusters`, `netbox_list_virtual_machines`,
`netbox_get_virtual_machine`

### Tenancy
`netbox_list_tenants`

### DNS (netbox-dns plugin)
`netbox_list_dns_zones`, `netbox_list_dns_records`

### Common parameters
- `limit` (1–100, default 20) and `offset` (default 0) — pagination.
- `response_format` — `"markdown"` (default) or `"json"`.
- `q` — global search string (where supported).
- Resource-specific filters (see each tool's description).

## Examples

**List active sites in a region:**
```json
{ "tool": "netbox_list_sites",
  "arguments": { "region": "indonesia", "status": "active", "limit": 10 } }
```

**Find a device by name:**
```json
{ "tool": "netbox_list_devices",
  "arguments": { "q": "sw-dmz", "response_format": "json" } }
```

**Look up which prefix contains an address:**
```json
{ "tool": "netbox_list_prefixes",
  "arguments": { "contains": "10.0.0.1" } }
```

**Query an endpoint without a dedicated tool (e.g. power feeds):**
```json
{ "tool": "netbox_list_objects",
  "arguments": { "app": "dcim", "resource": "power_feeds",
                 "query_params": { "site": "gkm" } } }
```

## Project layout

```
mcp-netbox/
├── pyproject.toml         # packaging + entry point (pipx/pip)
├── README.md
├── config.example.yaml    # config template (no secrets)
├── .gitignore
└── mcp_netbox/
    ├── __init__.py
    ├── __main__.py        # `python -m mcp_netbox`
    ├── config.py          # config loading (file + env overrides)
    ├── client.py          # async httpx NetBox API client
    ├── formatting.py      # Markdown / JSON response formatters
    └── server.py          # FastMCP server + all tools + CLI/HTTP entrypoint
```

## Notes & limitations

- **Read-only.** This server intentionally performs no mutations.
- **Large collections.** Listing very large collections (e.g. all 27k IP
  addresses) is slow; always filter and paginate.
- **Filter semantics.** NetBox filters vary by resource: some accept slugs,
  some IDs, some exact values. When unsure, use `q` (global search) or the
  generic `netbox_list_objects` with `query_params`.
- **DNS plugin.** DNS tools require the `netbox-dns` plugin to be installed on
  the target NetBox instance.
