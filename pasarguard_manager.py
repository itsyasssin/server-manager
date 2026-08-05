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


# ── Client ──────────────────────────────────────────────────────────────────


class PasarguardClient:
    """OOP wrapper around Pasarguard SDK operations used by autoscaler."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout
        self._api = None
        self._token: str | None = None

    async def connect(self):
        if self._api is None:
            pg = _import_sdk()
            self._api = pg.PasarguardAPI(
                base_url=self.base_url,
                verify=self.verify,
                timeout=self.timeout,
            )
        if self._token is None:
            token_resp = await self._api.get_token(username=self.username, password=self.password)
            self._token = token_resp.access_token
        return self._api, self._token

    async def list_nodes(self, *, enabled: bool | None = None, limit: int = 100) -> list[dict]:
        api, token = await self.connect()
        kwargs = {"token": token, "offset": 0, "limit": limit}
        if enabled is not None:
            kwargs["enabled"] = enabled
        resp = await api.get_nodes(**kwargs)
        return [_model_dump(n) for n in getattr(resp, "nodes", [])]

    async def add_node(
        self,
        *,
        name: str,
        address: str,
        port: str,
        connection_type: str,
        keep_alive: int,
        core_config_id: int,
        api_key: str,
        usage_coefficient: float,
        data_limit: int,
        default_timeout: int,
        internal_timeout: int,
        certificate: str,
    ) -> int | None:
        api, token = await self.connect()
        pg = _import_sdk()
        node = pg.NodeCreate(
            name=name,
            address=address,
            port=int(port) if str(port).isdigit() else 62050,
            api_port=int(port) + 1 if str(port).isdigit() else 62051,
            connection_type=connection_type,
            server_ca=certificate or "",
            keep_alive=keep_alive,
            core_config_id=core_config_id,
            api_key=api_key,
            usage_coefficient=usage_coefficient,
            data_limit=data_limit,
            default_timeout=default_timeout,
            internal_timeout=internal_timeout,
        )
        try:
            result = await api.create_node(node=node, token=token)
            result_dict = _model_dump(result)
            return result_dict.get("id") or result_dict.get("node_id")
        except Exception as e:
            logging.error(f"Failed to add node '{name}' to panel: {e}")
            return None

    async def add_host(self, *, country: str, address: str, tag: str) -> int | None:
        api, token = await self.connect()
        pg = _import_sdk()
        host = pg.CreateHost(
            remark=country,
            address=[address],
            security="inbound_default",
            inbound_tag=tag or None,
            priority=1,
        )
        try:
            result = await api.create_host(host=host, token=token)
            result_dict = _model_dump(result)
            host_id = result_dict.get("id") or result_dict.get("host_id")
            logging.info(f"Added new host '{country}' (address={address}) to panel, id={host_id}")
            return host_id
        except Exception as e:
            logging.error(f"Failed to add host '{country}' to panel: {e}")
            return None

    async def disable_node(self, node_id: int) -> bool:
        api, token = await self.connect()
        pg = _import_sdk()

        try:
            if hasattr(pg, "NodeUpdate"):
                update = pg.NodeUpdate(enabled=False)
                if hasattr(api, "update_node"):
                    await api.update_node(node_id=node_id, node=update, token=token)
                    logging.info(f"Node {node_id} disabled via update_node")
                    return True
        except Exception as e:
            logging.debug(f"update_node failed: {e}")

        try:
            if hasattr(api, "patch_node"):
                await api.patch_node(node_id=node_id, data={"enabled": False}, token=token)
                logging.info(f"Node {node_id} disabled via patch_node")
                return True
        except Exception as e:
            logging.debug(f"patch_node failed: {e}")

        try:
            if hasattr(api, "toggle_node"):
                await api.toggle_node(node_id=node_id, token=token)
                logging.info(f"Node {node_id} disabled via toggle_node")
                return True
            if hasattr(api, "toggle_node_status"):
                await api.toggle_node_status(node_id=node_id, token=token)
                logging.info(f"Node {node_id} disabled via toggle_node_status")
                return True
        except Exception as e:
            logging.debug(f"toggle failed: {e}")

        logging.error(
            f"Could not disable node {node_id}. "
            f"SDK methods not found. Please disable it manually from the panel."
        )
        return False

    async def purge_by_ip(self, ip: str) -> dict:
        api, token = await self.connect()

        resp = await api.get_nodes(token=token, offset=0, limit=100)
        nodes = [
            _model_dump(n)
            for n in getattr(resp, "nodes", [])
            if (_model_dump(n).get("address") == ip)
        ]
        node_ids = [int(n["id"]) for n in nodes if n.get("id") is not None]

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

