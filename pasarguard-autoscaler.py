#!/usr/bin/env python3
"""
pasarguard-autoscaler.py

Continuous PasarGuard node autoscaler.
Monitors panel nodes via ping from Iran, replaces underperforming ones
with new Doprax VPS instances automatically.

Workflow (every INTERVAL seconds):
  1. Fetch all enabled nodes from PasarGuard panel
  2. If active node count < MIN_NODES, provision new nodes up to MIN_NODES
  3. Ping each node's address from Iran datacenters (check-host.net)
  4. Find nodes where: success_rate < MINIMUM_RATING AND avg_ping > MINIMUM_PING
  5. For the first failing node:
     a. Find cheapest Doprax plan (VALID_COUNTRIES, VALID_DATACENTERS, <= MAX_BUDGET)
     b. Create VM, wait until active, get IP/username/password
     c. Ping the new VM from Iran
     d. If new VM passes checks -> install PasarGuard, add to panel (with data_limit if plan has traffic cap), disable old node
     e. If new VM fails ping -> delete it, log, try different DC next cycle

Usage:
    python3 pasarguard-autoscaler.py
    python3 pasarguard-autoscaler.py --once       # single cycle then exit
    python3 pasarguard-autoscaler.py --env /path/to/.env

Requirements:
    pip install paramiko "pasarguard[ssh]"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from random import shuffle
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Module path setup — import the 4 companion modules from ../upload/
# ─────────────────────────────────────────────────────────────────────────────
MODULES_DIR = Path(__file__).resolve().parent.parent / "upload"
if MODULES_DIR.exists():
    sys.path.insert(0, str(MODULES_DIR))

# ping.py
from ping import PingClient

# doprax.py
from doprax import DopraxClient
# install_pasarguard_node.py
from install_pasarguard_node import NodeInstallResult, NodeInstaller, SSHCredentials

# pasarguard-manager.py  (hyphenated filename — use importlib)
from pasarguard_manager import PasarguardClient


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
    max_budget: float = 5.0                  # max monthly price (USD)
    valid_countries: list[str] = field(default_factory=lambda: ["ir"])
    valid_datacenters: list[str] = field(default_factory=list)

    # ── PasarGuard host defaults (used when adding new host to panel) ──
    host_inbound_tag: str = ""

    # ── Scaling ──
    min_nodes: int = 1                       # minimum active nodes to maintain
    max_create_retries: int = 3              # retries when creating/replacing a VM

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
    once: bool = False
    state_file: str = ""

    @staticmethod
    def _load_env_file(env_path: str) -> dict[str, str]:
        """Parse a simple KEY=VALUE .env file (no variable expansion)."""
        result: dict[str, str] = {}
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

    @staticmethod
    def _str_list(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()] if value else []

    @classmethod
    def from_env(cls, env_path: str | None = None,
                 extra: dict[str, str] | None = None) -> "AutoscalerConfig":
        """Build AutoscalerConfig from .env file + OS environment + optional overrides."""
        if env_path is None:
            env_path = str(Path(__file__).resolve().parent / ".env")

        env = cls._load_env_file(env_path)
        # OS env takes precedence over .env file for matching keys
        for key, value in os.environ.items():
            if key in env or key.startswith(("PASARGUARD_", "DOPRAX_")):
                env[key] = value
        if extra:
            env.update(extra)

        g = lambda k, d="": env.get(k, d)
        state = g("STATE_FILE", str(Path(__file__).resolve().parent / "autoscaler-state.json"))

        return cls(
        interval=int(g("INTERVAL", "300")),
        ping_poll_timeout=int(g("PING_POLL_TIMEOUT", "120")),
        vm_ready_timeout=int(g("VM_READY_TIMEOUT", "300")),
        vm_ready_poll_interval=int(g("VM_READY_POLL_INTERVAL", "10")),
        minimum_rating=float(g("MINIMUM_RATING", "80")),
        minimum_ping=float(g("MINIMUM_PING", "200")),
        doprax_api_key=g("DOPRAX_API_KEY"),
        doprax_image=g("DOPRAX_IMAGE", "ubuntu-22.04"),
        max_budget=float(g("MAX_BUDGET", "5")),
        min_nodes=int(g("MIN_NODES", "1")),
        max_create_retries=int(g("MAX_CREATE_RETRIES", "3")),
        valid_countries=cls._str_list(g("VALID_COUNTRIES", "ir")),
        valid_datacenters=cls._str_list(g("VALID_DATACENTERS", "")),
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
        host_inbound_tag=g("PASARGUARD_HOST_INBOUND_TAG", ""),
        state_file=state,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OOP services
# ─────────────────────────────────────────────────────────────────────────────


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "replaced_nodes": {},
            "failed_datacenters": {},
            "stats": {"checks": 0, "replacements": 0, "failures": 0},
        }

    def save(self, state: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def mark_dc_failed(self, state: dict[str, Any], dc: str) -> None:
        state.setdefault("failed_datacenters", {})[dc] = datetime.now(timezone.utc).isoformat()
        self.save(state)


class NodeProvisioner:
    def __init__(
        self,
        config: AutoscalerConfig,
        state_store: StateStore,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self._doprax = DopraxClient(config.doprax_api_key)

    def _delete_vm(self, service_id: str) -> bool:
        """Delete a Doprax VM. Returns True on success."""
        body = {
            "action": "delete",
            "idempotency_key": str(_uuid.uuid4()),
        }
        try:
            resp = self._doprax.request(
                "POST",
                f"/api/v2/services/instances/{service_id}/operations/",
                body=body,
            )
            logging.info(f"Doprax VM {service_id} delete initiated: {resp.get('success')}")
            return True
        except Exception as e:
            logging.error(f"Failed to delete Doprax VM {service_id}: {e}")
            return False

    def _cleanup_failed_vm(self, service_id: str, state: dict[str, Any], dc: str) -> None:
        self._delete_vm(service_id)
        self.state_store.mark_dc_failed(state, dc)

    @staticmethod
    def _get_plan_traffic(plan: dict) -> str:
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
        for opts in (plan.get("allowed_options") or {}).values():
            for opt in (opts or []):
                meta = opt.get("metadata") or {}
                for mk in ("bandwidth", "traffic", "transfer"):
                    v = meta.get(mk)
                    if v:
                        return str(v).strip()

        return "NA"

    @staticmethod
    def _get_plan_traffic_bytes(plan: dict) -> int | None:
        """Parse the plan's traffic/bandwidth string into bytes.

        Handles formats like: '1 TB', '500GB', '1000 GB', 'Unlimited', 'NA', '0'.
        Returns the integer byte count, or None if unlimited / not parseable.
        """
        import re as _re

        raw = NodeProvisioner._get_plan_traffic(plan)
        if not raw or raw.upper() in ("NA", "UNLIMITED", "UNLTD", "INF", "0"):
            return None

        # Normalise whitespace and case
        text = raw.strip().upper().replace(" ", "")

        # Regex: optional float + optional space + unit
        m = _re.match(r"^(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|T|G|M|K|B)?$", text)
        if not m:
            logging.debug(f"Could not parse traffic string: '{raw}'")
            return None

        value = float(m.group(1))
        unit = (m.group(2) or "B").upper()

        multipliers = {
            "TB": 1_099_511_627_776, "T": 1_099_511_627_776,
            "GB": 1_073_741_824,      "G": 1_073_741_824,
            "MB": 1_048_576,          "M": 1_048_576,
            "KB": 1_024,             "K": 1_024,
            "B":  1,
        }
        return int(value * multipliers.get(unit, 1))

    def _find_plan(self, exclude_datacenters: list[str] | None = None) -> dict | None:
        """Find the cheapest matching Doprax plan.

        Returns the raw plan dict or None.
        """
        exclude = {d.lower() for d in (exclude_datacenters or [])}
        max_cents = int(self.config.max_budget * 100)
        countries = [c.lower() for c in self.config.valid_countries]
        dcs = [d.lower() for d in self.config.valid_datacenters]

        plans = self._doprax.get_catalogue(service_type="vm")
        matching = []
        for p in plans:
            plan_dc = DopraxClient.get_plan_datacenter(p).lower()
            plan_country = DopraxClient.get_plan_country(p).lower()
            monthly = DopraxClient.extract_monthly_price_cents(p)

            # Filter
            if not monthly or monthly > max_cents:
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

        shuffle(matching)
        # matching.sort(key=lambda p: DopraxClient.extract_monthly_price_cents(p))
        return matching[0]

    def _create_vm(self, plan: dict, name: str) -> tuple[str | None, dict | None]:
        """Create a Doprax VM and return (service_id, create_response_data).

        Returns (None, None) on failure.
        """
        pv_id = plan["product_version_id"]
        image_opt = DopraxClient.find_image_option(plan, self.config.doprax_image)
        if not image_opt:
            logging.error(f"No image matching '{self.config.doprax_image}' in plan")
            return None, None

        # Build selections: both location AND operating system are chosen this
        # way, each as {"optionId": "<uuid>"} — NOT via a top-level
        # 'container_code' or a bare code string.
        selections: dict[str, dict[str, str]] = {}
        loc = DopraxClient.get_plan_location_selection(plan)
        if loc:
            loc_key, loc_id = loc
            selections[loc_key] = {"optionId": loc_id}
        else:
            logging.warning("No location option found in plan — VM creation will likely fail.")

        img_key, img_id, _img_display = image_opt
        selections[img_key] = {"optionId": img_id}

        body: dict[str, Any] = {
            "product_version_id": pv_id,
            "idempotency_key": str(_uuid.uuid4()),
            "name": name,
            "metadata": {"access_method": "password"},
        }
        if selections:
            body["selections"] = selections

        try:
            print(body)
            resp = self._doprax.request("POST", "/api/v2/services/instances/", body=body)
        except Exception as e:
            logging.error(f"Doprax create VM failed: {e}")
            return None, None

        data = resp.get("data") or {}

        service_id = (
            data.get("service_id")
            or data.get("id")
            or (data.get("service") or {}).get("id")
        )
        if not service_id:
            logging.error(f"Could not extract service_id from create response: {json.dumps(resp, default=str)}")
            return None, resp

        return str(service_id), resp

    def _wait_vm_ready(self, service_id: str) -> dict | None:
        """Poll VM detail until status is active/running.

        Returns the detail response data dict, or None on timeout/failure.
        """
        timeout = self.config.vm_ready_timeout
        interval = self.config.vm_ready_poll_interval
        elapsed = 0
        while elapsed < timeout:
            try:
                resp = self._doprax.request(
                    "GET",
                    f"/api/v2/services/instances/{service_id}/detail/",
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

    @staticmethod
    def _extract_access(detail: dict) -> dict | None:
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

    def _ping(self, host: str) -> tuple[float, float | None]:
        """Ping a host from Iran and return (success_rate%, avg_ping_ms).

        avg_ping_ms is None if no successful pings.
        """
        client = PingClient(max_wait=self.config.ping_poll_timeout)

        iran_nodes = client.get_iran_nodes()
        if not iran_nodes:
            logging.error("No Iran check-host nodes available")
            return 0.0, None

        check_resp = client.submit_ping_check(host, iran_nodes)
        if not check_resp.get("ok"):
            logging.error(f"Ping submit failed for {host}: {check_resp}")
            return 0.0, None

        request_id = check_resp["request_id"]
        raw = client.poll_until_complete(request_id, len(iran_nodes))

        total_ok, total_pings, avg_ms = PingClient.parse_results(raw)
        rate = (total_ok / total_pings * 100) if total_pings > 0 else 0.0
        return rate, avg_ms

    @staticmethod
    def _build_node_name(datacenter: str, plan: dict) -> str:
        """Build node name: datacenter + budget + free traffic.

        Example: "ir-thr-5usd-1TB"
        """
        monthly_cents = DopraxClient.extract_monthly_price_cents(plan)
        price_usd = monthly_cents / 100 if monthly_cents else 0
        traffic = NodeProvisioner._get_plan_traffic(plan)

        # Clean up traffic string (extract just the number+unit)
        traffic_clean = traffic.replace(" ", "").upper()
        if traffic_clean == "NA" or traffic_clean == "0":
            traffic_clean = "NA"

        dc = datacenter.replace("_", "-").lower()
        epoch_time = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"automatic - {dc}-${price_usd:g}-{traffic_clean}-{epoch_time}"

    def create_validated_vm(self, state: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(1, self.config.max_create_retries + 1):
            logging.info(f"--- VM create attempt {attempt}/{self.config.max_create_retries} ---")

            # exclude_dcs = list(state.get("failed_datacenters", {}).keys())
            plan = self._find_plan(exclude_datacenters=[])
            if not plan:
                logging.warning("No matching Doprax plan found. Giving up.")
                return None

            dc = DopraxClient.get_plan_datacenter(plan)
            monthly_cents = DopraxClient.extract_monthly_price_cents(plan)
            new_node_name = self._build_node_name(dc, plan)
            logging.info(
                f"Selected plan: dc={dc}, ${monthly_cents/100:.2f}/mo, "
                f"traffic={self._get_plan_traffic(plan)}, name='{new_node_name}'"
            )

            service_id = None
            try:
                service_id, create_resp = self._create_vm(
                    plan,
                    new_node_name,
                )
                if not service_id:
                    logging.error(f"Failed to create Doprax VM (attempt {attempt}).")
                    continue

                logging.info(f"Doprax VM created: service_id={service_id}")
                create_data = (create_resp or {}).get("data") or {}
                op_id = create_data.get("operation_id")
                if op_id:
                    self._doprax._poll_operation(op_id, max_wait=self.config.vm_ready_timeout)

                detail = self._wait_vm_ready(service_id)
                if not detail:
                    logging.error(f"VM {service_id} did not become active. Cleaning up and retrying...")
                    self._cleanup_failed_vm(service_id, state, dc)
                    continue

                access = self._extract_access(detail)
                if not access or not access["ip"]:
                    logging.error(f"Could not extract access info for VM {service_id}. Cleaning up and retrying...")
                    self._cleanup_failed_vm(service_id, state, dc)
                    continue

                new_ip = access["ip"]
                new_user = access["username"]
                vm_code = (
                    (detail.get("vm") or {}).get("vm_code", "")
                    or (detail.get("links") or {}).get("vm_code", "")
                )
                new_pass = self._doprax.get_password(vm_code)
                if not new_pass:
                    logging.error(f"Could not get password for VM {service_id}. Cleaning up and retrying...")
                    self._cleanup_failed_vm(service_id, state, dc)
                    continue
                logging.info(f"VM ready: {new_user}@{new_ip}")

                logging.info(f"Pinging new VM {new_ip} from Iran...")
                rate, avg_ms = self._ping(new_ip)
                logging.info(
                    f"New VM ping results: rate={rate:.1f}%, avg={avg_ms:.1f}ms"
                    if avg_ms else f"rate={rate:.1f}%, avg=N/A"
                )

                if rate < self.config.minimum_rating or (avg_ms is not None and avg_ms > self.config.minimum_ping):
                    logging.warning(
                        f"New VM {new_ip} fails checks (rate={rate:.1f}%<{self.config.minimum_rating}%, "
                        f"ping={avg_ms}>{self.config.minimum_ping}ms). Deleting VM and marking DC as bad."
                    )
                    self._cleanup_failed_vm(service_id, state, dc)
                    continue

                return {
                    "plan": plan,
                    "detail": detail,
                    "service_id": service_id,
                    "new_ip": new_ip,
                    "new_user": new_user,
                    "new_pass": new_pass,
                    "new_node_name": new_node_name,
                }
            except Exception as e:
                logging.error(f"VM creation/validation failed (attempt {attempt}): {e}")
                if service_id:
                    self._cleanup_failed_vm(service_id, state, dc)
                continue

        logging.error(f"All {self.config.max_create_retries} VM create attempts failed. Giving up.")
        return None

    async def _install_and_attach(self, vm_info: dict[str, Any]) -> tuple[bool, int | None]:
        plan = vm_info["plan"]
        service_id = vm_info["service_id"]
        new_ip = vm_info["new_ip"]
        new_user = vm_info["new_user"]
        new_pass = vm_info["new_pass"]
        new_node_name = vm_info["new_node_name"]
        traffic_bytes = self._get_plan_traffic_bytes(plan)

        logging.info(f"Installing PasarGuard node on {new_user}@{new_ip}...")
        creds = SSHCredentials(host=new_ip, username=new_user, password=new_pass)
        try:
            installer = NodeInstaller()
            install_result: NodeInstallResult = installer.install_node(creds)
        except Exception as e:
            logging.exception(f"PasarGuard node install failed: {e}")
            self._delete_vm(service_id)
            return False, None

        logging.info(
            f"PasarGuard node installed: api_key={install_result.api_key[:8]}..., port={install_result.port}, "
            f"cert={'yes' if install_result.certificate else 'no'}"
        )

        pg_client = PasarguardClient(
            base_url=self.config.pasarguard_base_url,
            username=self.config.pasarguard_username,
            password=self.config.pasarguard_password,
            verify=True,
            timeout=20.0,
        )
        new_pg_node_id = await pg_client.add_node(
            name=new_node_name,
            address=new_ip,
            port=install_result.port,
            connection_type=self.config.node_connection_type,
            keep_alive=self.config.node_keep_alive,
            core_config_id=self.config.node_core_config_id,
            api_key=install_result.api_key,
            usage_coefficient=self.config.node_usage_coefficient,
            data_limit=traffic_bytes if traffic_bytes is not None else self.config.node_data_limit,
            default_timeout=self.config.node_default_timeout,
            internal_timeout=self.config.node_internal_timeout,
            certificate=install_result.certificate,
        )
        if not new_pg_node_id:
            logging.error("Failed to add new node to PasarGuard panel. Aborting.")
            self._delete_vm(service_id)
            return False, None

        country = DopraxClient.get_plan_country(plan, flag=True)
        if country and self.config.host_inbound_tag:
            tags = self.config.host_inbound_tag.split(",")
            for t in tags:
                host_id = await pg_client.add_host(country=country, address=new_ip, tag=t)
                if not host_id:
                    logging.warning(f"Failed to add host for country '{country}'. Node still added.")
        elif not self.config.host_inbound_tag:
            logging.debug("No PASARGUARD_HOST_INBOUND_TAG set, skipping host creation.")

        return True, new_pg_node_id

    async def provision_new_node(self, state: dict[str, Any]) -> bool:
        logging.info("=== Provisioning new node (scale-up) ===")
        vm_info = self.create_validated_vm(state)
        if not vm_info:
            return False

        ok, new_pg_node_id = await self._install_and_attach(vm_info)
        if not ok or new_pg_node_id is None:
            return False

        state.setdefault("provisioned_nodes", []).append(
            {
                "node_name": vm_info["new_node_name"],
                "node_id": new_pg_node_id,
                "address": vm_info["new_ip"],
                "doprax_service_id": vm_info["service_id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        state["stats"]["provisions"] = state["stats"].get("provisions", 0) + 1
        state["failed_datacenters"] = {}
        self.state_store.save(state)

        logging.info(f"=== Scale-up complete: new node '{vm_info['new_node_name']}' ({vm_info['new_ip']}) ===")
        return True

    async def replace_failing_node(self, failing_node: dict[str, Any], state: dict[str, Any]) -> bool:
        node_id = failing_node.get("id")
        node_addr = failing_node.get("address", "")
        node_name = failing_node.get("name", f"node-{node_id}")

        logging.info(f"=== Replacing node '{node_name}' (id={node_id}, addr={node_addr}) ===")

        vm_info = self.create_validated_vm(state)
        if not vm_info:
            return False

        ok, new_pg_node_id = await self._install_and_attach(vm_info)
        if not ok or new_pg_node_id is None:
            return False

        pg_client = PasarguardClient(
            base_url=self.config.pasarguard_base_url,
            username=self.config.pasarguard_username,
            password=self.config.pasarguard_password,
            verify=True,
            timeout=20.0,
        )
        if node_id is not None:
            logging.info(f"Disabling old node {node_id} ('{node_name}')...")
            disabled = await pg_client.disable_node(int(node_id))
            if not disabled:
                logging.warning(
                    f"Could not auto-disable old node {node_id}. "
                    f"Please disable it manually from the PasarGuard panel."
                )
        else:
            logging.warning(f"Old node '{node_name}' has no id; skipping auto-disable.")

        state.setdefault("replaced_nodes", {})[str(node_id)] = {
            "old_name": node_name,
            "old_address": node_addr,
            "new_node_name": vm_info["new_node_name"],
            "new_node_id": new_pg_node_id,
            "new_address": vm_info["new_ip"],
            "doprax_service_id": vm_info["service_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state["stats"]["replacements"] = state["stats"].get("replacements", 0) + 1
        state["failed_datacenters"] = {}
        self.state_store.save(state)

        logging.info(
            f"=== Replacement complete: '{node_name}' -> '{vm_info['new_node_name']}' ({vm_info['new_ip']}) ==="
        )
        return True


class Autoscaler:
    def __init__(
        self,
        config: AutoscalerConfig,
        state_store: StateStore,
        provisioner: NodeProvisioner,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.provisioner = provisioner
        self._doprax = DopraxClient(config.doprax_api_key)

    async def run_one_cycle(self, state: dict[str, Any]) -> None:
        cycle_start = datetime.now(timezone.utc)
        logging.info(f"--- Cycle started at {cycle_start.isoformat()} ---")
        state["stats"]["checks"] = state["stats"].get("checks", 0) + 1

        pg_client = PasarguardClient(
            base_url=self.config.pasarguard_base_url,
            username=self.config.pasarguard_username,
            password=self.config.pasarguard_password,
            verify=True,
            timeout=20.0,
        )
        try:
            nodes = await pg_client.list_nodes(limit=100)
        except Exception as e:
            logging.error(f"Failed to fetch PasarGuard nodes: {e}")
            state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
            self.state_store.save(state)
            return

        active_count = len(nodes)
        logging.info(f"Found {active_count} enabled node(s) in PasarGuard panel.")

        if active_count < self.config.min_nodes:
            needed = self.config.min_nodes - active_count
            logging.info(
                f"Active nodes ({active_count}) < MIN_NODES ({self.config.min_nodes}). "
                f"Need to provision {needed} more node(s)."
            )
            success = await self.provisioner.provision_new_node(state)
            if not success:
                state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
            self.state_store.save(state)
            try:
                nodes = await pg_client.list_nodes(limit=100)
                active_count = len(nodes)
                logging.info(f"After scale-up: {active_count} enabled node(s).")
            except Exception:
                pass

        if not nodes:
            logging.info("No enabled nodes found. Nothing to ping-check.")
            self.state_store.save(state)
            return

        failing_nodes = []
        for node in nodes:
            addr = node.get("address", "")
            nid = node.get("id")
            nname = node.get("name", "?")
            nstatus = node.get("status", "?")

            if not addr:
                logging.warning(f"Node {nname} (id={nid}) has no address, skipping.")
                continue

            if nstatus == "limited" or nstatus == "disabled":
                logging.warning(f"Node {nname} has hit the data limit or disabled.")
                failing_nodes.append(node)
                continue

            logging.info(f"Pinging node '{nname}' ({addr})...")
            try:
                rate, avg_ms = self.provisioner._ping(addr)
            except Exception as e:
                logging.error(f"Ping error for {addr}: {e}")
                rate, avg_ms = 0.0, None

            avg_str = f"{avg_ms:.1f}ms" if avg_ms is not None else "N/A"
            logging.info(f"  '{nname}': rate={rate:.1f}%, avg_ping={avg_str}")

            if rate < self.config.minimum_rating or (avg_ms is None or avg_ms > self.config.minimum_ping):
                logging.warning(
                    f"  '{nname}' FAILS thresholds: rate={rate:.1f}%<{self.config.minimum_rating}%, "
                    f"ping={avg_str}>{self.config.minimum_ping}ms"
                )
                failing_nodes.append(node)
            else:
                logging.info(f"  '{nname}' passes thresholds.")

        if not failing_nodes:
            logging.info("All nodes pass thresholds. No action needed.")
            self.state_store.save(state)
            return

        replaced_ids = set(state.get("replaced_nodes", {}).keys())
        for node in failing_nodes:
            nid_str = str(node.get("id", ""))
            addr = str(node.get("address", ""))
            logging.info(f"Replacing node {node.get('name')} (id={nid_str}) at {addr}...")
            if nid_str in replaced_ids:
                logging.info(f"Node {node.get('name')} (id={nid_str}) already replaced in a previous cycle. Skipping.")
                continue

            success = await self.provisioner.replace_failing_node(node, state)

            if success:
                await pg_client.purge_by_ip(addr)
                self._doprax.delete_vm_by_ip(addr)
            else:
                state["stats"]["failures"] = state["stats"].get("failures", 0) + 1

            self.state_store.save(state)
            break
        else:
            logging.info("All failing nodes were already replaced. No action needed.")
            self.state_store.save(state)


class AutoscalerApp:
    def __init__(self, *, env_path: str | None, once: bool) -> None:
        self.env_path = env_path
        self.once = once
        self._last_state: dict[str, Any] | None = None
        self._last_state_path: str | None = None

    @staticmethod
    def _validate_config(config: AutoscalerConfig) -> None:
        errors = []
        if not config.doprax_api_key:
            errors.append("DOPRAX_API_KEY is not set")
        if not config.pasarguard_base_url:
            errors.append("PASARGUARD_BASE_URL is not set")
        if not config.pasarguard_username:
            errors.append("PASARGUARD_ADMIN_USERNAME is not set")
        if not config.host_inbound_tag:
            errors.append("PASARGUARD_HOST_INBOUND_TAG is not set")
        if not config.pasarguard_password:
            errors.append("PASARGUARD_ADMIN_PASSWORD is not set")

        if errors:
            logging.error("Missing configuration:")
            for e in errors:
                logging.error(f"  - {e}")
            sys.exit(1)

    @staticmethod
    def _log_config(config: AutoscalerConfig) -> None:
        logging.info("PasarGuard Autoscaler starting...")
        logging.info(f"  Host inbound tag: {config.host_inbound_tag or '(not set - hosts will not be created)'}")
        logging.info(f"  Interval: {config.interval}s")
        logging.info(f"  Min rating: {config.minimum_rating}%")
        logging.info(f"  Max ping: {config.minimum_ping}ms")
        logging.info(f"  Max budget: ${config.max_budget}/mo")
        logging.info(f"  Min nodes: {config.min_nodes}")
        logging.info(f"  Countries: {config.valid_countries}")
        logging.info(f"  Datacenters: {config.valid_datacenters or '(any)'}")

    def run(self) -> None:
        try:
            while True:
                config = AutoscalerConfig.from_env(env_path=self.env_path)
                config.once = config.once or self.once

                self._validate_config(config)
                self._log_config(config)

                state_store = StateStore(config.state_file)
                state = state_store.load()
                self._last_state = state
                self._last_state_path = config.state_file

                provisioner = NodeProvisioner(config, state_store)
                autoscaler = Autoscaler(config, state_store, provisioner)

                try:
                    asyncio.run(autoscaler.run_one_cycle(state))
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logging.error(f"Cycle error: {e}", exc_info=True)
                    state["stats"]["failures"] = state["stats"].get("failures", 0) + 1
                    state_store.save(state)

                if config.once:
                    logging.info("--once flag set, exiting.")
                    break

                logging.info(f"Sleeping {config.interval}s until next cycle...")
                time.sleep(config.interval)

        except KeyboardInterrupt:
            logging.info("Interrupted. Shutting down.")
            if self._last_state is not None and self._last_state_path:
                StateStore(self._last_state_path).save(self._last_state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PasarGuard Node Autoscaler — monitors and replaces underperforming nodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env", default=None, help="Path to .env config file")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = AutoscalerApp(env_path=args.env, once=args.once)
    app.run()


if __name__ == "__main__":
    main()
