#!/usr/bin/env python3
"""
pasarguard-autoscaler.py

Continuous PasarGuard node autoscaler.
Monitors panel nodes via ping from Iran, replaces underperforming ones
with new Doprax VPS instances automatically.

Workflow (every INTERVAL seconds):
  1. Fetch all enabled nodes from PasarGuard panel
  2. Ping each node's address from Iran datacenters (check-host.net)
  3. Find nodes where: success_rate < MINIMUM_RATING AND avg_ping > MINIMUM_PING
  4. For the first failing node:
     a. Find cheapest Doprax plan (VALID_COUNTRIES, VALID_DATACENTERS, < MINIMUM_BUDGET)
     b. Create VM, wait until active, get IP/username/password
     c. Ping the new VM from Iran
     d. If new VM passes checks -> install PasarGuard, add to panel, disable old node
     e. If new VM fails ping -> delete it, log, try different DC next cycle

Usage:
    python3 pasarguard-autoscaler.py
    python3 pasarguard-autoscaler.py --once       # single cycle then exit
    python3 pasarguard-autoscaler.py --dry-run    # report only, no changes
    python3 pasarguard-autoscaler.py --env /path/to/.env

Requirements:
    pip install paramiko "pasarguard[ssh]"
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Module path setup — import the 4 companion modules from ../upload/
# ─────────────────────────────────────────────────────────────────────────────
MODULES_DIR = Path(__file__).resolve().parent.parent / "upload"
if MODULES_DIR.exists():
    sys.path.insert(0, str(MODULES_DIR))

# ping.py (clean library imports)
from ping import (
    get_iran_nodes as _ping_get_iran_nodes,
    submit_ping_check as _ping_submit,
    poll_until_complete as _ping_poll,
    parse_results as _ping_parse,
)

# doprax.py
import doprax as _doprax_mod

# install_pasarguard_node.py
from install_pasarguard_node import (
    install_node as _install_pg_node,
    SSHCredentials,
    NodeInstallResult,
)

# pasarguard-manager.py  (hyphenated filename — use importlib)
from pasarguard_manager import (
    _import_sdk,
    _model_dump,
    get_token,
)

_pg_import_sdk = _import_sdk
_pg_model_dump = _model_dump
_pg_get_token = get_token


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AutoscalerConfig:
    # ── Timing ──
    interval: int = 300                     # seconds between full check cycles
    ping_poll_timeout: int = 120            # max seconds to wait for ping results
    vm_ready_timeout: int = 300             # max seconds to wait for Doprax VM
    vm_ready_poll_interval: int = 10        # seconds between VM status polls

    # ── Thresholds ──
    minimum_rating: float = 80.0            # min ping success-rate %
    minimum_ping: float = 200.0             # max acceptable avg ping (ms)

    # ── Doprax ──
    doprax_api_key: str = ""
    doprax_image: str = "ubuntu-22.04"
    minimum_budget: float = 5.0             # max monthly price (USD)
    valid_countries: List[str] = field(default_factory=lambda: ["ir"])
    valid_datacenters: List[str] = field(default_factory=list)

    # ── PasarGuard Panel ──
    pasarguard_base_url: str = ""
    pasarguard_username: str = ""
    pasarguard_password: str = ""

    # ── PasarGuard node defaults (used when adding new node to panel) ──
    node_connection_type: str = "rest"
    node_keep_alive: int = 30
    node_core_config_id: int = 1
    node_usage_coefficient: float = 1.0
    node_data_limit: int = 0
    node_default_timeout: int = 10
    node_internal_timeout: int = 15

    # ── Flags ──
    dry_run: bool = False
    once: bool = False
    state_file: str = ""


def _load_env_file(env_path: str) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file (no variable expansion)."""
    result: Dict[str, str] = {}
    if not os.path.isfile(env_path):
        return result
    with open(env_path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _str_list(value: str, default: str = "") -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()] if value else []


def build_config(env_path: Optional[str] = None,
                  extra: Optional[Dict[str, str]] = None) -> AutoscalerConfig:
    """Build AutoscalerConfig from .env file + OS environment + optional overrides."""
    if env_path is None:
        env_path = str(Path(__file__).resolve().parent / ".env")

    env = _load_env_file(env_path)
    # OS env takes precedence over .env file for matching keys
    for key, value in os.environ.items():
        if key in env or key.startswith(("PASARGUARD_", "DOPRAX_")):
            env[key] = value
    if extra:
        env.update(extra)

    g = lambda k, d="": env.get(k, d)
    state = g("STATE_FILE", str(Path(__file__).resolve().parent / "autoscaler-state.json"))

    return AutoscalerConfig(
        interval=int(g("INTERVAL", "300")),
        ping_poll_timeout=int(g("PING_POLL_TIMEOUT", "120")),
        vm_ready_timeout=int(g("VM_READY_TIMEOUT", "300")),
        vm_ready_poll_interval=int(g("VM_READY_POLL_INTERVAL", "10")),
        minimum_rating=float(g("MINIMUM_RATING", "80")),
        minimum_ping=float(g("MINIMUM_PING", "200")),
        doprax_api_key=g("DOPRAX_API_KEY"),
        doprax_image=g("DOPRAX_IMAGE", "ubuntu-22.04"),
        minimum_budget=float(g("MINIMUM_BUDGET", "5")),
        valid_countries=_str_list(g("VALID_COUNTRIES", "ir")),
        valid_datacenters=_str_list(g("VALID_DATACENTERS", "")),
        pasarguard_base_url=g("PASARGUARD_BASE_URL"),
        pasarguard_username=g("PASARGUARD_ADMIN_USERNAME"),
        pasarguard_password=g("PASARGUARD_ADMIN_PASSWORD"),
        node_connection_type=g("PASARGUARD_NODE_CONNECTION_TYPE", "rest"),
        node_keep_alive=int(g("PASARGUARD_NODE_KEEP_ALIVE", "30")),
        node_core_config_id=int(g("PASARGUARD_NODE_CORE_CONFIG_ID", "1")),
        node_usage_coefficient=float(g("PASARGUARD_NODE_USAGE_COEFFICIENT", "1")),
        node_data_limit=int(g("PASARGUARD_NODE_DATA_LIMIT", "0")),
        node_default_timeout=int(g("PASARGUARD_NODE_DEFAULT_TIMEOUT", "10")),
        node_internal_timeout=int(g("PASARGUARD_NODE_INTERNAL_TIMEOUT", "15")),
        state_file=state,
    )


# ─────────────────────────────────────────────────────────────────────────────
# State persistence (avoids replacing the same node twice, tracks bad DCs)
# ─────────────────────────────────────────────────────────────────────────────

def _load_state(path: str) -> Dict[str, Any]:
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"replaced_nodes": {}, "failed_datacenters": {}, "stats": {"checks": 0, "replacements": 0, "failures": 0}}


def _save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Doprax helpers  (programmatic — no argparse)
# ─────────────────────────────────────────────────────────────────────────────

def _doprax_get_plan_traffic(plan: Dict) -> str:
    """Try to extract a human-readable traffic/bandwidth string from a plan.

    Looks in price_components, metadata, and specifications for hints.
    Returns something like '1TB', '500GB', or 'NA'.
    """
    # Check price_components for traffic-related items
    for pc in (plan.get("price_components") or []):
        name = (pc.get("name") or "").lower()
        if any(kw in name for kw in ("traffic", "bandwidth", "transfer", "data")):
            val = pc.get("value") or pc.get("display_value") or ""
            if val:
                return str(val).strip()

    # Check plan metadata / specifications
    for key in ("bandwidth", "traffic", "transfer", "data_allowance", "network_traffic"):
        val = plan.get(key) or (plan.get("specifications") or {}).get(key) or (plan.get("metadata") or {}).get(key)
        if val:
            return str(val).strip()

    # Check allowed_options metadata
    for opt_key, opts in (plan.get("allowed_options") or {}).items():
        for opt in (opts or []):
            meta = opt.get("metadata") or {}
            for mk in ("bandwidth", "traffic", "transfer"):
                v = meta.get(mk)
                if v:
                    return str(v).strip()

    return "NA"


def doprax_find_plan(api_key: str, config: AutoscalerConfig,
                      exclude_datacenters: Optional[List[str]] = None) -> Optional[Dict]:
    """Find the cheapest matching Doprax plan.

    Returns the raw plan dict or None.
    """
    exclude = set((d.lower() for d in (exclude_datacenters or [])))
    max_cents = int(config.minimum_budget * 100)
    countries = [c.lower() for c in config.valid_countries]
    dcs = [d.lower() for d in config.valid_datacenters]

    plans = _doprax_mod.get_catalogue(api_key)
    matching = []
    for p in plans:
        plan_dc = _doprax_mod.get_plan_datacenter(p).lower()
        plan_country = _doprax_mod.get_plan_country(p).lower()
        monthly = _doprax_mod.extract_monthly_price_cents(p)

        # Filter
        if monthly > max_cents:
            continue
        if countries and plan_country not in countries:
            continue
        if dcs and plan_dc not in dcs:
            continue
        if plan_dc in exclude:
            continue

        matching.append(p)

    if not matching:
        return None

    matching.sort(key=lambda p: _doprax_mod.extract_monthly_price_cents(p))
    return matching[0]


def doprax_create_vm(api_key: str, plan: Dict, image_hint: str,
                      name: str) -> Tuple[Optional[str], Optional[Dict]]:
    """Create a Doprax VM and return (service_id, create_response_data).

    Returns (None, None) on failure.
    """
    pv_id = plan["product_version_id"]
    image_code = _doprax_mod.find_image_code(plan, image_hint)
    if not image_code:
        logging.error(f"No image matching '{image_hint}' in plan")
        return None, None

    body: Dict[str, Any] = {
        "product_version_id": pv_id,
        "idempotency_key": str(_uuid.uuid4()),
        "name": name,
        "container_code": image_code,
    }

    try:
        resp = _doprax_mod.api_request("POST", "/api/v2/services/instances/", api_key, body=body)
    except Exception as e:
        logging.error(f"Doprax create VM failed: {e}")
        return None, None

    data = resp.get("data") or {}

    # Extract service_id from various possible locations
    service_id = (
        data.get("service_id")
        or data.get("id")
        or (data.get("service") or {}).get("id")
    )
    if not service_id:
        logging.error(f"Could not extract service_id from create response: {json.dumps(resp, default=str)}")
        return None, resp

    return str(service_id), resp


def doprax_wait_vm_ready(api_key: str, service_id: str,
                          timeout: int = 300, interval: int = 10) -> Optional[Dict]:
    """Poll VM detail until status is active/running.

    Returns the detail response data dict, or None on timeout/failure.
    """
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = _doprax_mod.api_request(
                "GET",
                f"/api/v2/services/instances/{service_id}/detail/",
                api_key,
            )
            data = resp.get("data") or {}

            # Check various status locations
            svc_status = (data.get("service") or {}).get("status", "")
            vm_status = (data.get("vm") or {}).get("status", "")
            status = svc_status or vm_status or ""

            logging.debug(f"  VM {service_id}: status={status} ({elapsed}s)")

            if status.lower() in ("active", "running", "on"):
                return data

            if status.lower() in ("failed", "error", "deleted", "suspended"):
                logging.error(f"VM {service_id} entered bad status: {status}")
                return None

        except Exception as e:
            logging.warning(f"  VM poll error: {e}")

        time.sleep(interval)
        elapsed += interval

    logging.error(f"VM {service_id} not ready after {timeout}s")
    return None


def doprax_extract_access(detail: Dict) -> Optional[Dict]:
    """Extract IP, username, password from VM detail.

    Returns {"ip": ..., "username": ..., "password": ...} or None.
    """
    vm = detail.get("vm") or {}
    access = detail.get("access") or {}

    ip = (
        access.get("public_ipv4")
        or vm.get("ipv4")
        or (vm.get("network") or {}).get("ipv4")
    )
    username = access.get("username") or vm.get("username") or "root"
    password = (
        access.get("password")
        or access.get("root_password")
        or vm.get("password")
        or vm.get("root_password")
    )

    if not ip:
        return None
    if not password:
        logging.warning(f"No password found for VM {ip}. SSH key auth may be required.")

    return {"ip": ip, "username": username, "password": password}


def doprax_delete_vm(api_key: str, service_id: str) -> bool:
    """Delete a Doprax VM. Returns True on success."""
    body = {
        "action": "delete",
        "idempotency_key": str(_uuid.uuid4()),
    }
    try:
        resp = _doprax_mod.api_request(
            "POST",
            f"/api/v2/services/instances/{service_id}/operations/",
            api_key,
            body=body,
        )
        logging.info(f"Doprax VM {service_id} delete initiated: {resp.get('success')}")
        return True
    except Exception as e:
        logging.error(f"Failed to delete Doprax VM {service_id}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Ping helpers
# ─────────────────────────────────────────────────────────────────────────────

def ping_host(host: str, timeout: int = 120) -> Tuple[float, Optional[float]]:
    """Ping a host from Iran and return (success_rate%, avg_ping_ms).

    avg_ping_ms is None if no successful pings.
    """
    iran_nodes = _ping_get_iran_nodes()
    if not iran_nodes:
        logging.error("No Iran check-host nodes available")
        return 0.0, None

    check_resp = _ping_submit(host, iran_nodes)
    if not check_resp.get("ok"):
        logging.error(f"Ping submit failed for {host}: {check_resp}")
        return 0.0, None

    request_id = check_resp["request_id"]
    raw = _ping_poll(request_id, len(iran_nodes))

    # Override MAX_WAIT for polling by adjusting the module's value temporarily
    # (the poll function uses module-level MAX_WAIT; we already passed timeout
    #  awareness — actually it uses the module constant.  Let's call parse directly.)

    total_ok, total_pings, avg_ms = _ping_parse(raw)
    rate = (total_ok / total_pings * 100) if total_pings > 0 else 0.0
    return rate, avg_ms


# ─────────────────────────────────────────────────────────────────────────────
# PasarGuard panel helpers (async — wraps the pasarguard SDK)
# ─────────────────────────────────────────────────────────────────────────────

async def pg_login(config: AutoscalerConfig):
    """Return (api, token) for the PasarGuard panel."""
    pg = _pg_import_sdk()
    api = pg.PasarguardAPI(
        base_url=config.pasarguard_base_url,
        verify=True,
        timeout=20.0,
    )
    token = await _pg_get_token(api, config.pasarguard_username, config.pasarguard_password)
    return api, token


async def pg_list_nodes(api, token: str) -> List[Dict[str, Any]]:
    """Fetch all enabled nodes as plain dicts."""
    resp = await api.get_nodes(token=token, offset=0, limit=100, enabled=True)
    nodes = []
    for node in resp.nodes:
        nodes.append(_pg_model_dump(node))
    return nodes


async def pg_add_node(api, token: str, config: AutoscalerConfig,
                      name: str, address: str, port: str,
                      api_key: str, certificate: str) -> Optional[int]:
    """Add a new node to the PasarGuard panel.

    Returns the new node ID or None.
    """
    pg = _pg_import_sdk()
    node = pg.NodeCreate(
        name=name,
        address=address,
        port=int(port) if port.isdigit() else 62050,
        api_port=int(port) + 1 if port.isdigit() else 62051,
        connection_type=config.node_connection_type,
        server_ca=certificate or "",
        keep_alive=config.node_keep_alive,
        core_config_id=config.node_core_config_id,
        api_key=api_key,
        usage_coefficient=config.node_usage_coefficient,
        data_limit=config.node_data_limit,
        default_timeout=config.node_default_timeout,
        internal_timeout=config.node_internal_timeout,
    )
    try:
        result = await api.create_node(node=node, token=token)
        result_dict = _pg_model_dump(result)
        node_id = result_dict.get("id") or result_dict.get("node_id")
        logging.info(f"Added new node '{name}' to panel, id={node_id}")
        return node_id
    except Exception as e:
        logging.error(f"Failed to add node '{name}' to panel: {e}")
        return None


async def pg_disable_node(api, token: str, node_id: int) -> bool:
    """Disable a PasarGuard panel node (set enabled=False).

    Tries multiple SDK method patterns. Returns True on success.
    """
    pg = _pg_import_sdk()

    # Strategy 1: NodeUpdate with enabled=False
    try:
        if hasattr(pg, "NodeUpdate"):
            update = pg.NodeUpdate(enabled=False)
            if hasattr(api, "update_node"):
                await api.update_node(node_id=node_id, node=update, token=token)
                logging.info(f"Node {node_id} disabled via update_node")
                return True
    except Exception as e:
        logging.debug(f"update_node failed: {e}")

    # Strategy 2: patch_node with dict
    try:
        if hasattr(api, "patch_node"):
            await api.patch_node(node_id=node_id, data={"enabled": False}, token=token)
            logging.info(f"Node {node_id} disabled via patch_node")
            return True
    except Exception as e:
        logging.debug(f"patch_node failed: {e}")

    # Strategy 3: toggle_node
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


# ─────────────────────────────────────────────────────────────────────────────
# Node naming
# ─────────────────────────────────────────────────────────────────────────────

def build_node_name(datacenter: str, plan: Dict) -> str:
    """Build node name: datacenter + budget + free traffic.

    Example: "ir-thr-5usd-1TB"
    """
    monthly_cents = _doprax_mod.extract_monthly_price_cents(plan)
    price_usd = monthly_cents / 100 if monthly_cents else 0
    traffic = _doprax_get_plan_traffic(plan)

    # Clean up traffic string (extract just the number+unit)
    traffic_clean = traffic.replace(" ", "").upper()
    if traffic_clean == "NA" or traffic_clean == "0":
        traffic_clean = "NA"

    dc = datacenter.replace("_", "-").lower()
    return f"{dc}-{price_usd:g}usd-{traffic_clean}"


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def _replace_failing_node(
    failing_node: Dict[str, Any],
    config: AutoscalerConfig,
    state: Dict[str, Any],
) -> bool:
    """Attempt to replace a single failing node with a new Doprax VM.

    Returns True if replacement succeeded.
    """
    node_id = failing_node.get("id")
    node_addr = failing_node.get("address", "")
    node_name = failing_node.get("name", f"node-{node_id}")

    logging.info(f"=== Replacing node '{node_name}' (id={node_id}, addr={node_addr}) ===")

    # ── 1. Find a Doprax plan ──
    exclude_dcs = list(state.get("failed_datacenters", {}).keys())
    plan = doprax_find_plan(config.doprax_api_key, config, exclude_datacenters=exclude_dcs)
    if not plan:
        logging.warning("No matching Doprax plan found. Will retry next cycle.")
        return False

    dc = _doprax_mod.get_plan_datacenter(plan)
    traffic = _doprax_get_plan_traffic(plan)
    monthly_cents = _doprax_mod.extract_monthly_price_cents(plan)
    new_node_name = build_node_name(dc, plan)

    logging.info(
        f"Selected plan: dc={dc}, ${monthly_cents/100:.2f}/mo, traffic={traffic}, name='{new_node_name}'"
    )

    if config.dry_run:
        logging.info("[DRY-RUN] Would create Doprax VM and proceed with replacement.")
        return True

    # ── 2. Create Doprax VM ──
    service_id, create_resp = doprax_create_vm(
        config.doprax_api_key, plan, config.doprax_image, new_node_name
    )
    if not service_id:
        logging.error("Failed to create Doprax VM.")
        return False

    logging.info(f"Doprax VM created: service_id={service_id}")

    # Poll operation if present
    create_data = (create_resp or {}).get("data") or {}
    op_id = create_data.get("operation_id")
    if op_id:
        _doprax_mod._poll_operation(config.doprax_api_key, op_id, max_wait=config.vm_ready_timeout)

    # ── 3. Wait for VM to be ready ──
    logging.info(f"Waiting for VM {service_id} to become active...")
    detail = doprax_wait_vm_ready(
        config.doprax_api_key, service_id,
        timeout=config.vm_ready_timeout,
        interval=config.vm_ready_poll_interval,
    )
    if not detail:
        logging.error(f"VM {service_id} did not become active. Cleaning up...")
        doprax_delete_vm(config.doprax_api_key, service_id)
        return False

    # ── 4. Get VM access (IP, username, password) ──
    access = doprax_extract_access(detail)
    if not access or not access["ip"]:
        logging.error(f"Could not extract access info for VM {service_id}")
        doprax_delete_vm(config.doprax_api_key, service_id)
        return False

    new_ip = access["ip"]
    new_user = access["username"]
    new_pass = access["password"]
    logging.info(f"VM ready: {new_user}@{new_ip}")

    # ── 5. Ping the new VM from Iran ──
    logging.info(f"Pinging new VM {new_ip} from Iran...")
    rate, avg_ms = ping_host(new_ip, timeout=config.ping_poll_timeout)
    logging.info(f"New VM ping results: rate={rate:.1f}%, avg={avg_ms:.1f}ms" if avg_ms else f"rate={rate:.1f}%, avg=N/A")

    if rate < config.minimum_rating or (avg_ms is not None and avg_ms > config.minimum_ping):
        logging.warning(
            f"New VM {new_ip} also fails checks (rate={rate:.1f}%<{config.minimum_rating}%, "
            f"ping={avg_ms}>{config.minimum_ping}ms). Deleting VM and marking DC as bad."
        )
        doprax_delete_vm(config.doprax_api_key, service_id)
        # Mark this datacenter as bad so we try a different one next time
        state.setdefault("failed_datacenters", {})[dc] = datetime.now(timezone.utc).isoformat()
        _save_state(config.state_file, state)
        return False

    # ── 6. Install PasarGuard node on the new VM ──
    logging.info(f"Installing PasarGuard node on {new_user}@{new_ip}...")
    if not new_pass:
        logging.error("No password available for SSH. Cannot install node.")
        doprax_delete_vm(config.doprax_api_key, service_id)
        return False

    creds = SSHCredentials(host=new_ip, username=new_user, password=new_pass)
    try:
        install_result: NodeInstallResult = _install_pg_node(creds)
    except Exception as e:
        logging.error(f"PasarGuard node install failed: {e}")
        doprax_delete_vm(config.doprax_api_key, service_id)
        return False

    logging.info(
        f"PasarGuard node installed: api_key={install_result.api_key[:8]}..., port={install_result.port}, "
        f"cert={'yes' if install_result.certificate else 'no'}"
    )

    # ── 7. Add new node to PasarGuard panel ──
    pg_api, pg_token = await pg_login(config)
    new_pg_node_id = await pg_add_node(
        pg_api, pg_token, config,
        name=new_node_name,
        address=new_ip,
        port=install_result.port,
        api_key=install_result.api_key,
        certificate=install_result.certificate,
    )
    if not new_pg_node_id:
        logging.error("Failed to add new node to PasarGuard panel. Aborting.")
        doprax_delete_vm(config.doprax_api_key, service_id)
        return False

    # ── 8. Disable old failing node ──
    logging.info(f"Disabling old node {node_id} ('{node_name}')...")
    disabled = await pg_disable_node(pg_api, pg_token, node_id)
    if not disabled:
        logging.warning(
            f"Could not auto-disable old node {node_id}. "
            f"Please disable it manually from the PasarGuard panel."
        )

    # ── 9. Update state ──
    state.setdefault("replaced_nodes", {})[str(node_id)] = {
        "old_name": node_name,
        "old_address": node_addr,
        "new_node_name": new_node_name,
        "new_node_id": new_pg_node_id,
        "new_address": new_ip,
        "doprax_service_id": service_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    state["stats"]["replacements"] = state["stats"].get("replacements", 0) + 1
    # Clear failed_datacenters since we succeeded
    state["failed_datacenters"] = {}
    _save_state(config.state_file, state)

    logging.info(
        f"=== Replacement complete: '{node_name}' -> '{new_node_name}' ({new_ip}) ==="
    )
    return True


async def run_one_cycle(config: AutoscalerConfig, state: Dict[str, Any]) -> None:
    """Execute one full check-and-replace cycle."""
    cycle_start = datetime.now(timezone.utc)
    logging.info(f"--- Cycle started at {cycle_start.isoformat()} ---")
    state["stats"]["checks"] = state["stats"].get("checks", 0) + 1

    # ── 1. Get all enabled PasarGuard nodes ──
    try:
        pg_api, pg_token = await pg_login(config)
        nodes = await pg_list_nodes(pg_api, pg_token)
    except Exception as e:
        logging.error(f"Failed to fetch PasarGuard nodes: {e}")
        state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
        _save_state(config.state_file, state)
        return

    if not nodes:
        logging.info("No enabled nodes found in PasarGuard panel. Nothing to check.")
        _save_state(config.state_file, state)
        return

    logging.info(f"Found {len(nodes)} enabled node(s) in PasarGuard panel.")

    # ── 2. Ping each node and find failing ones ──
    failing_nodes = []
    for node in nodes:
        addr = node.get("address", "")
        nid = node.get("id")
        nname = node.get("name", "?")
        if not addr:
            logging.warning(f"Node {nname} (id={nid}) has no address, skipping.")
            continue

        logging.info(f"Pinging node '{nname}' ({addr})...")
        try:
            rate, avg_ms = ping_host(addr, timeout=config.ping_poll_timeout)
        except Exception as e:
            logging.error(f"Ping error for {addr}: {e}")
            rate, avg_ms = 0.0, None

        avg_str = f"{avg_ms:.1f}ms" if avg_ms is not None else "N/A"
        logging.info(f"  '{nname}': rate={rate:.1f}%, avg_ping={avg_str}")

        if rate < config.minimum_rating and (avg_ms is None or avg_ms > config.minimum_ping):
            logging.warning(
                f"  '{nname}' FAILS thresholds: rate={rate:.1f}%<{config.minimum_rating}%, "
                f"ping={avg_str}>{config.minimum_ping}ms"
            )
            failing_nodes.append(node)
        else:
            logging.info(f"  '{nname}' passes thresholds.")

    # ── 3. Replace first failing node (one per cycle to avoid chaos) ──
    if not failing_nodes:
        logging.info("All nodes pass thresholds. No action needed.")
        _save_state(config.state_file, state)
        return

    # Skip nodes already replaced in this session
    replaced_ids = set(state.get("replaced_nodes", {}).keys())
    for node in failing_nodes:
        nid_str = str(node.get("id", ""))
        if nid_str in replaced_ids:
            logging.info(f"Node {node.get('name')} (id={nid_str}) already replaced in a previous cycle. Skipping.")
            continue

        success = await _replace_failing_node(node, config, state)
        if not success:
            state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
        _save_state(config.state_file, state)
        break  # Only one replacement per cycle
    else:
        logging.info("All failing nodes were already replaced. No action needed.")
        _save_state(config.state_file, state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PasarGuard Node Autoscaler — monitors and replaces underperforming nodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env", default=None, help="Path to .env config file")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Report only, make no changes")
    args = parser.parse_args()

    # ── Setup logging ──
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Load config ──
    config = build_config(env_path=args.env)
    config.dry_run = config.dry_run or args.dry_run
    config.once = config.once or args.once

    # Validate config
    errors = []
    if not config.doprax_api_key:
        errors.append("DOPRAX_API_KEY is not set")
    if not config.pasarguard_base_url:
        errors.append("PASARGUARD_BASE_URL is not set")
    if not config.pasarguard_username:
        errors.append("PASARGUARD_ADMIN_USERNAME is not set")
    if not config.pasarguard_password:
        errors.append("PASARGUARD_ADMIN_PASSWORD is not set")
    if errors:
        logging.error("Missing configuration:")
        for e in errors:
            logging.error(f"  - {e}")
        sys.exit(1)

    logging.info("PasarGuard Autoscaler starting...")
    logging.info(f"  Interval: {config.interval}s")
    logging.info(f"  Min rating: {config.minimum_rating}%")
    logging.info(f"  Max ping: {config.minimum_ping}ms")
    logging.info(f"  Max budget: ${config.minimum_budget}/mo")
    logging.info(f"  Countries: {config.valid_countries}")
    logging.info(f"  Datacenters: {config.valid_datacenters or '(any)'}")
    logging.info(f"  Dry run: {config.dry_run}")

    # ── Load state ──
    state = _load_state(config.state_file)

    # ── Main loop ──
    try:
        while True:
            try:
                asyncio.run(run_one_cycle(config, state))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f"Cycle error: {e}", exc_info=True)
                state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
                _save_state(config.state_file, state)

            if config.once:
                logging.info("--once flag set, exiting.")
                break

            logging.info(f"Sleeping {config.interval}s until next cycle...")
            time.sleep(config.interval)

    except KeyboardInterrupt:
        logging.info("Interrupted. Shutting down.")
        _save_state(config.state_file, state)


if __name__ == "__main__":
    main()
