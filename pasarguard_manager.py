#!/usr/bin/env python3
"""
pasarguard_manager.py

Programmatic helpers for managing Pasarguard Panel nodes and hosts,
using the pasarguard Python SDK:
https://github.com/AmirKenzo/pasarguard_api

Requirements:
    pip install "pasarguard[ssh]"
"""

from __future__ import annotations

import json
import logging
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



