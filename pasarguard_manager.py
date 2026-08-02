#!/usr/bin/env python3
"""
pasarguard-manager.py

CLI script to manage Pasarguard Panel nodes and hosts.
Uses the pasarguard Python SDK (https://github.com/AmirKenzo/pasarguard_api).

Requirements:
    pip install "pasarguard[ssh]"

Usage:
    # Nodes
    python3 pasarguard-manager.py nodes list
    python3 pasarguard-manager.py nodes add --name my-node --address 1.2.3.4 --connection-type rest --server-ca "-----BEGIN CERTIFICATE-----..." --keep-alive 30 --core-config-id 1 --api-key secret123
    python3 pasarguard-manager.py nodes delete --id 5
    python3 pasarguard-manager.py nodes delete --id 5,7,9

    # Hosts
    python3 pasarguard-manager.py hosts list
    python3 pasarguard-manager.py hosts add --remark "my-host" --address "example.com" --port 443 --sni "example.com" --path "/" --security tls --priority 1
    python3 pasarguard-manager.py hosts remove --id 3
    python3 pasarguard-manager.py hosts remove --id 3,4,5

Environment variables (or use --base-url, --username, --password):
    PASARGUARD_BASE_URL      - Panel URL (e.g. https://panel.example.com)
    PASARGUARD_ADMIN_USERNAME - Admin username
    PASARGUARD_ADMIN_PASSWORD - Admin password
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

# ── SDK imports (imported inside async so pydantic loads correctly) ─────────

def _import_sdk():
    """Lazy-import pasarguard to avoid pydantic loading issues at module level."""
    import importlib
    pasarguard = importlib.import_module("pasarguard")
    return pasarguard


def _model_dump(model: Any) -> dict:
    """Serialize a pydantic model to dict, handling both v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=True)
    if hasattr(model, "dict"):
        return model.dict(exclude_none=True)
    return {}


# ── Auth ────────────────────────────────────────────────────────────────────

async def get_token(api, username: str, password: str) -> str:
    """Login and return access_token string."""
    token_resp = await api.get_token(username=username, password=password)
    return token_resp.access_token


# ── Node commands ───────────────────────────────────────────────────────────

async def nodes_list(api, token: str, args) -> None:
    """List all nodes."""
    resp = await api.get_nodes(token=token, offset=0, limit=100, enabled=True)
    for node in resp.nodes:
        print(json.dumps(_model_dump(node), indent=2, ensure_ascii=False))
    logging.debug(f"Total: {resp.total}")


async def nodes_add(api, token: str, args) -> None:
    """Add a new node."""
    pasarguard = _import_sdk()
    node = pasarguard.NodeCreate(
        name=args.name,
        address=args.address,
        port=args.port,
        api_port=args.api_port,
        connection_type=args.connection_type,
        server_ca=args.server_ca,
        keep_alive=args.keep_alive,
        core_config_id=args.core_config_id,
        api_key=args.api_key,
        usage_coefficient=args.usage_coefficient,
        data_limit=args.data_limit,
        default_timeout=args.default_timeout,
        internal_timeout=args.internal_timeout,
        proxy_url=args.proxy_url,
    )
    result = await api.create_node(node=node, token=token)
    print(json.dumps(_model_dump(result), indent=2, ensure_ascii=False))


async def nodes_delete(api, token: str, args) -> None:
    """Delete one or more nodes by ID(s)."""
    ids = [int(x) for x in args.id.split(",")]
    pasarguard = _import_sdk()

    if len(ids) == 1:
        await api.remove_node(node_id=ids[0], token=token)
        logging.info(f"Node {ids[0]} deleted.")
    else:
        bulk = pasarguard.BulkNodeSelection(ids=ids)
        result = await api.bulk_delete_nodes(bulk=bulk, token=token)
        logging.info(f"Deleted {result.count} nodes: {result.nodes}")


# ── Host commands ───────────────────────────────────────────────────────────

async def hosts_list(api, token: str, args) -> None:
    """List all hosts."""
    hosts = await api.get_hosts(token=token, offset=0, limit=100)
    for h in hosts:
        print(json.dumps(_model_dump(h), indent=2, ensure_ascii=False))


async def hosts_add(api, token: str, args) -> None:
    """Add a new host."""
    pasarguard = _import_sdk()
    addresses = args.address.split(",") if args.address else None
    snis = args.sni.split(",") if args.sni else None
    host_list = args.host.split(",") if args.host else None

    host = pasarguard.CreateHost(
        remark=args.remark,
        address=addresses,
        port=args.port,
        sni=snis,
        host=host_list,
        path=args.path,
        security=args.security,
        alpn=args.alpn.split(",") if args.alpn else None,
        fingerprint=args.fingerprint,
        allowinsecure=args.allowinsecure,
        is_disabled=args.disabled,
        inbound_tag=args.inbound_tag,
        priority=args.priority,
    )
    result = await api.create_host(host=host, token=token)
    print(json.dumps(_model_dump(result), indent=2, ensure_ascii=False))


async def hosts_remove(api, token: str, args) -> None:
    """Remove one or more hosts by ID(s)."""
    ids = [int(x) for x in args.id.split(",")]
    pasarguard = _import_sdk()

    if len(ids) == 1:
        await api.remove_host(host_id=ids[0], token=token)
        logging.info(f"Host {ids[0]} removed.")
    else:
        bulk = pasarguard.BulkHostSelection(ids=ids)
        result = await api.bulk_delete_hosts(bulk=bulk, token=token)
        logging.info(f"Removed {result.count} hosts: {result.hosts}")


def _coerce_addresses(value: Any) -> list[str]:
    """Best-effort: normalize a host.address field into a list[str]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            if x is None:
                continue
            out.append(str(x))
        return out
    return [str(value)]


async def purge_by_ip(api, token: str, ip: str) -> dict:
    """Delete every host and node in panel matching the given IP.

    Matching rules:
      - Nodes: node.address == ip
      - Hosts: ip is present in host.address list

    Returns a summary dict.
    """
    # Nodes
    resp = await api.get_nodes(token=token, offset=0, limit=100)
    nodes = [
        _model_dump(n)
        for n in getattr(resp, "nodes", [])
        if (_model_dump(n).get("address") == ip)
    ]
    node_ids = [int(n["id"]) for n in nodes if n.get("id") is not None]

    # Hosts
    hosts_raw = await api.get_hosts(token=token, offset=0, limit=100)
    hosts = [_model_dump(h) for h in (hosts_raw or [])]
    host_ids: list[int] = []
    for h in hosts:
        addresses = _coerce_addresses(h.get("address"))
        if ip in addresses and h.get("id") is not None:
            host_ids.append(int(h["id"]))

    summary = {
        "ip": ip,
        "matched": {"nodes": node_ids, "hosts": host_ids},
        "deleted": {"nodes": [], "hosts": []},
        "errors": [],
    }

    # Delete hosts first (they may point at node IPs; safer order)
    for hid in host_ids:
        try:
            await api.remove_host(host_id=hid, token=token)
            summary["deleted"]["hosts"].append(hid)
            logging.info(f"Host {hid} removed (matched ip={ip}).")
        except Exception as e:
            summary["errors"].append({"type": "host", "id": hid, "error": str(e)})
            logging.error(f"Failed removing host {hid}: {e}")

    for nid in node_ids:
        try:
            await api.remove_node(node_id=nid, token=token)
            summary["deleted"]["nodes"].append(nid)
            logging.info(f"Node {nid} deleted (matched ip={ip}).")
        except Exception as e:
            summary["errors"].append({"type": "node", "id": nid, "error": str(e)})
            logging.error(f"Failed deleting node {nid}: {e}")

    return summary


async def purge_cmd(api, token: str, args) -> None:
    """CLI handler: purge by IP."""
    result = await purge_by_ip(api, token, args.ip)
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ── CLI setup ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Pasarguard Panel nodes and hosts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global connection args
    parser.add_argument("--base-url", default=os.environ.get("PASARGUARD_BASE_URL"),
                        help="Panel base URL (env: PASARGUARD_BASE_URL)")
    parser.add_argument("-u", "--username", default=os.environ.get("PASARGUARD_ADMIN_USERNAME"),
                        help="Admin username (env: PASARGUARD_ADMIN_USERNAME)")
    parser.add_argument("-p", "--password", default=os.environ.get("PASARGUARD_ADMIN_PASSWORD"),
                        help="Admin password (env: PASARGUARD_ADMIN_PASSWORD)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds")
    parser.add_argument("--no-verify", action="store_true", help="Disable TLS verification")

    sub = parser.add_subparsers(dest="resource", required=True)

    # ── nodes ──
    nodes_p = sub.add_parser("nodes", help="Manage nodes")

    nodes_sub = nodes_p.add_subparsers(dest="action", required=True)

    # nodes list
    nodes_sub.add_parser("list", help="List all nodes")

    # nodes add
    n_add = nodes_sub.add_parser("add", help="Add a new node")
    n_add.add_argument("--name", required=True, help="Node name")
    n_add.add_argument("--address", required=True, help="Node IP or domain")
    n_add.add_argument("--port", type=int, default=62050)
    n_add.add_argument("--api-port", type=int, default=62051)
    n_add.add_argument("--connection-type", required=True, choices=["grpc", "rest"],
                       help="Connection type")
    n_add.add_argument("--server-ca", required=True, help="TLS CA certificate")
    n_add.add_argument("--keep-alive", type=int, required=True, help="Keep-alive interval")
    n_add.add_argument("--core-config-id", type=int, required=True, help="Core config ID")
    n_add.add_argument("--api-key", required=True, help="API key for the node")
    n_add.add_argument("--usage-coefficient", type=float, default=1.0)
    n_add.add_argument("--data-limit", type=int, default=0, help="Data limit in bytes (0=unlimited)")
    n_add.add_argument("--default-timeout", type=int, default=10)
    n_add.add_argument("--internal-timeout", type=int, default=15)
    n_add.add_argument("--proxy-url", default=None, help="Proxy URL for the node")

    # nodes delete
    n_del = nodes_sub.add_parser("delete", help="Delete node(s)")
    n_del.add_argument("--id", required=True, help="Node ID(s), comma-separated (e.g. 5 or 5,7,9)")

    # ── hosts ──
    hosts_p = sub.add_parser("hosts", help="Manage hosts")

    hosts_sub = hosts_p.add_subparsers(dest="action", required=True)

    # hosts list
    hosts_sub.add_parser("list", help="List all hosts")

    # hosts add
    h_add = hosts_sub.add_parser("add", help="Add a new host")
    h_add.add_argument("--remark", required=True, help="Host remark/name")
    h_add.add_argument("--address", default=None, help="Address(es), comma-separated")
    h_add.add_argument("--port", type=int, default=None)
    h_add.add_argument("--sni", default=None, help="SNI value(s), comma-separated")
    h_add.add_argument("--host", default=None, help="Host header(s), comma-separated")
    h_add.add_argument("--path", default=None, help="WebSocket path")
    h_add.add_argument("--security", default="inbound_default",
                       choices=["inbound_default", "none", "tls"])
    h_add.add_argument("--alpn", default=None, help="ALPN(s), comma-separated (e.g. h2,http/1.1)")
    h_add.add_argument("--fingerprint", default="")
    h_add.add_argument("--allowinsecure", action="store_true", default=None)
    h_add.add_argument("--disabled", action="store_true", default=False, dest="disabled")
    h_add.add_argument("--inbound-tag", default=None)
    h_add.add_argument("--priority", type=int, default=1)

    # hosts remove
    h_rm = hosts_sub.add_parser("remove", help="Remove host(s)")
    h_rm.add_argument("--id", required=True, help="Host ID(s), comma-separated (e.g. 3 or 3,4,5)")

    # purge
    purge_p = sub.add_parser("purge", help="Delete all nodes + hosts that match an IP")
    purge_p.add_argument("--ip", required=True, help="IP address to purge")

    return parser


# ── Main ────────────────────────────────────────────────────────────────────

ACTION_MAP = {
    ("nodes", "list"):   nodes_list,
    ("nodes", "add"):    nodes_add,
    ("nodes", "delete"): nodes_delete,
    ("hosts", "list"):   hosts_list,
    ("hosts", "add"):    hosts_add,
    ("hosts", "remove"): hosts_remove,
    ("purge", None):      purge_cmd,
}


async def async_main(args: argparse.Namespace) -> None:
    pasarguard = _import_sdk()

    if not args.base_url:
        print("[ERROR] --base-url or PASARGUARD_BASE_URL is required")
        sys.exit(1)
    if not args.username:
        print("[ERROR] --username or PASARGUARD_ADMIN_USERNAME is required")
        sys.exit(1)
    if not args.password:
        print("[ERROR] --password or PASARGUARD_ADMIN_PASSWORD is required")
        sys.exit(1)

    api = pasarguard.PasarguardAPI(
        base_url=args.base_url,
        verify=not args.no_verify,
        timeout=args.timeout,
    )

    token = await get_token(api, args.username, args.password)

    handler = ACTION_MAP.get((args.resource, args.action))
    if handler is None:
        print(f"[ERROR] Unknown command: {args.resource} {args.action}")
        sys.exit(1)

    await handler(api, token, args)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
