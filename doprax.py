#!/usr/bin/env python3
"""
doprax.py

Programmatic helpers for Doprax VPS management:
- list and inspect VM instances
- browse/filter catalogue plans
- create VMs from matching plans
- delete VMs directly or by IP lookup

Authentication:
    DOPRAX API key string in format '<prefix>.<secret>'.

OpenAPI Spec:
    https://www.doprax.com/reference/api/
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid
from random import shuffle

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://www.doprax.com"


class DopraxClient:
    """OOP client for the Doprax VPS API."""

    FLAGS = {
      "au": "🇦🇺",
      "be": "🇧🇪",
      "br": "🇧🇷",
      "ca": "🇨🇦",
      "ch": "🇨🇭",
      "cl": "🇨🇱",
      "de": "🇩🇪",
      "es": "🇪🇸",
      "fi": "🇫🇮",
      "fr": "🇫🇷",
      "gb": "🇬🇧",
      "hk": "🇭🇰",
      "id": "🇮🇩",
      "il": "🇮🇱",
      "in": "🇮🇳",
      "it": "🇮🇹",
      "jp": "🇯🇵",
      "kr": "🇰🇷",
      "mx": "🇲🇽",
      "nl": "🇳🇱",
      "pl": "🇵🇱",
      "qa": "🇶🇦",
      "sa": "🇸🇦",
      "se": "🇸🇪",
      "sg": "🇸🇬",
      "tw": "🇹🇼",
      "us": "🇺🇸",
      "za": "🇿🇦"
    }

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        proxy: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.proxy = proxy
        self.timeout = timeout

    # ── HTTP helper ──────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        """Perform an API request with retries, returning the JSON payload."""
        url = f"{self.base_url}{path}"

        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

        proxies = None
        if self.proxy:
            proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    params=query,
                    json=body,
                    headers=headers,
                    proxies=proxies,
                    timeout=self.timeout,
                )

                resp.raise_for_status()

                try:
                    return resp.json()
                except ValueError:
                    logger.warning(
                        "JSON decode failed (%d/%d), retrying...",
                        attempt + 1,
                        max_attempts,
                    )
                    logger.debug(resp.text)

                    if attempt == max_attempts - 1:
                        return {}

                    time.sleep(1)
                    continue

            except Exception as e:
                logger.exception("API request failed: %s", e)
                logger.warning(
                    "get response failed (%d/%d), retrying...",
                    attempt + 1,
                    max_attempts,
                )

                if attempt == max_attempts - 1:
                    return {}

                time.sleep(1)

        return {}

    def _all_pages(self, path: str,
                   query: dict | None = None) -> tuple[list[dict], dict]:
        """Fetch every page of a paginated list endpoint.

        Returns (items, first_page_meta).
        """
        query = dict(query or {})
        query.setdefault("page", 1)
        query.setdefault("page_size", 100)
        all_items: list[dict] = []
        first_meta: dict = {}

        while True:
            resp = self.request("GET", path, query=query)
            items = resp.get("data", [])
            all_items.extend(items)
            meta = resp.get("meta", {})
            if not first_meta:
                first_meta = meta
            if not meta.get("has_next"):
                break
            query["page"] = meta["page"] + 1

        return all_items, first_meta

    # ── Catalogue helpers ────────────────────────────────────────────────────

    def get_password(self, vm_code: str) -> str:
        resp = self.request("GET", f"/api/v2/vms/{vm_code}/actions/access/")
        return resp.get("data", {}).get("tempPass")

    def get_catalogue(self, service_type: str | None = None,
                      provider: str | None = None) -> list[dict]:
        """Fetch the service catalogue.

        GET /api/v2/catalogue/service-catalogue/
        Optional query params: provider, service_type
        """
        query: dict = {}
        if service_type:
            query["service_type"] = service_type
        if provider:
            query["provider"] = provider
        resp = self.request("GET", "/api/v2/catalogue/service-catalogue/", query=query)
        return resp.get("data", [])

    @staticmethod
    def extract_monthly_price_cents(plan: dict) -> int:
        """Sum all monthly recurring price_components (unit_price_cents).

        billing_unit can be: month, mo, monthly, etc.
        """
        total = 0
        for pc in plan.get("price_components") or []:
            bu = (pc.get("billing_unit") or "").lower()
            code = pc.get("code", "")
            if bu in ("month", "monthly", "mo") and code == "base_plan":
                total += pc.get("unit_price_cents", 0)
        return total

    @staticmethod
    def _extract_all_options(plan: dict) -> list[tuple[str, dict]]:
        """Flatten allowed_options into [(option_key, option_dict), ...].

        allowed_options schema:
          { "location": [{code, label, metadata: {country_code, ...}}, ...],
            "image":    [{code, label, metadata: {...}}, ...],
            ... }
        """
        results: list[tuple[str, dict]] = []
        for opt_key, opt_list in (plan.get("allowed_options") or {}).items():
            for opt in (opt_list or []):
                results.append((opt_key, opt))
        return results

    @staticmethod
    def get_option_id(opt: dict) -> str | None:
        """Extract the UUID identifier from an allowed_options entry.

        The 'selections' field in the create-VM request needs the option's UUID
        (wrapped as {"optionId": "<uuid>"}), NOT the human-readable 'code'
        (e.g. "ir-thr"). Different catalogue entries have been observed using
        different key names for this id, so we try the common variants.
        """
        for key in ("id", "option_id", "optionId", "uuid", "value"):
            val = opt.get(key)
            if val:
                return val
        return None

    @staticmethod
    def get_plan_country(plan: dict, flag=False) -> str:
        """Get the 2-letter country code from a plan's location options."""
        for _, opt in DopraxClient._extract_all_options(plan):
            meta = opt.get("metadata") or {}
            # Direct country_code in metadata
            cc = meta.get("country_code")
            if cc:
                if flag:
                    _code = DopraxClient.FLAGS.get(cc.lower(), "Vip")
                    return _code
                return cc
            # Code might be like "ir-thr" — take first segment
            code = opt.get("code", "")
            if len(code) >= 2 and "-" in code:
                if flag:
                    _code = DopraxClient.FLAGS.get(code.split("-")[0].lower(), "Vip")
                    return _code
                return code.split("-")[0]
        return "vip"

    @staticmethod
    def get_plan_datacenter(plan: dict) -> str:
        """Get the first datacenter/region code from a plan."""
        return plan.get("provider", {}).get("code", "")

    @staticmethod
    def find_image_option(plan: dict, image_hint: str) -> tuple[str, str, str] | None:
        """Find an OS/image option matching the hint (case-insensitive substring match).

        Searches all allowed_options for image/os/system related option groups
        (e.g. 'operating_system').

        Returns (opt_key, option_id, display_code) for use in the 'selections'
        field of the create request, or None if nothing matched.
        """
        hint = image_hint.lower()
        for opt_key, opt in DopraxClient._extract_all_options(plan):
            kl = opt_key.lower()
            if "image" not in kl and "os" not in kl and "system" not in kl:
                continue
            code = opt.get("code", "")
            label = opt.get("label", "")
            if hint in code.lower() or hint in label.lower():
                opt_id = DopraxClient.get_option_id(opt)
                if not opt_id:
                    continue
                return (opt_key, opt_id, code or label)
        return None

    @staticmethod
    def get_plan_location_selection(plan: dict) -> tuple[str, str] | None:
        """Get the location option key and UUID for the 'selections' field.

        Returns (option_key, option_id) — e.g. ("location", "ec5bc1aa-db5f-...") —
        for direct use as selections[option_key] = {"optionId": option_id}.
        Returns None if no location option (with a resolvable id) exists.
        """
        all_location = []
        for opt_key, opt in DopraxClient._extract_all_options(plan):
            if "location" in opt_key.lower() or "region" in opt_key.lower() or "datacenter" in opt_key.lower():
                opt_id = DopraxClient.get_option_id(opt)
                if opt_id:
                    all_location.append((opt_key, opt_id))

        shuffle(all_location)
        return all_location[0] if all_location else None

    # ── Catalogue and VM helpers ─────────────────────────────────────────────

    def delete_vm(self, service_id: str) -> dict:
        """Delete a VM by issuing the 'delete' lifecycle action.

        POST /api/v2/services/instances/{service_id}/operations/
        Returns the raw API response.
        """
        body = {
            "action": "delete",
            "idempotency_key": str(_uuid.uuid4()),
        }
        return self.request(
            "POST",
            f"/api/v2/services/instances/{service_id}/operations/",
            body=body,
        )

    def _get_vm_ip(self, service_id: str) -> str:
        """Fetch VM detail and extract its IPv4 address.

        The list endpoint only returns a summary (no IP), so each VM must be
        looked up individually via GET /services/instances/{service_id}/detail/.
        Response: { success, data: ServiceDetailDataSchema }
          data.vm     — VMDataSchema (id, ..., ipv4, ipv6, ...)
          data.access — ServiceAccessSchema (username, public_ipv4, ...)
        """
        resp = self.request("GET", f"/api/v2/services/instances/{service_id}/detail/")
        data = resp.get("data") or {}

        vm = data.get("vm") or {}
        access = data.get("access") or {}

        for container in (vm, access, data):
            if not isinstance(container, dict):
                continue
            for key in ("public_ipv4", "ipv4", "ip", "address"):
                val = container.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    def delete_vm_by_ip(self, ip: str, *, require_unique: bool = True) -> dict:
        """Find Doprax VM(s) by IP and delete them.

        - Scans all instances via /services/instances/list/ (all pages)
        - Matches if extracted ipv4 == ip
        - If require_unique=True and multiple match, returns an error summary (no deletions).

        Returns a summary dict.
        """
        vms, _meta = self._all_pages("/api/v2/services/instances/list/")

        matches: list[dict] = []
        for vm in vms:
            sid = vm.get("service_id") or vm.get("id")
            if not sid:
                continue
            vm_ip = self._get_vm_ip(str(sid))
            if vm_ip == ip:
                matches.append(vm)

        service_ids: list[str] = []
        for m in matches:
            sid = m.get("service_id") or m.get("id")
            if sid:
                service_ids.append(str(sid))

        summary: dict = {
            "ip": ip,
            "matched_service_ids": service_ids,
            "deleted_service_ids": [],
            "errors": [],
        }

        if require_unique and len(service_ids) > 1:
            summary["errors"].append(
                {
                    "type": "ambiguous",
                    "error": f"Multiple VMs match ip={ip}. Refusing to delete.",
                }
            )
            return summary

        for sid in service_ids:
            try:
                resp = self.delete_vm(sid)
                summary["deleted_service_ids"].append(sid)
                # If operation_id exists, include it for the caller
                data = (resp or {}).get("data") or {}
                op_id = data.get("operation_id")
                if op_id:
                    summary.setdefault("operation_ids", []).append(op_id)
            except Exception as e:
                summary["errors"].append({"type": "delete", "service_id": sid, "error": str(e)})

        return summary

    # ── Operation polling ─────────────────────────────────────────────────────

    def _poll_operation(self, operation_id: str, max_wait: int = 120) -> None:
        """Poll GET /api/v2/services/operations/{operation_id}/ until done."""
        elapsed = 0
        interval = 5
        while elapsed < max_wait:
            try:
                resp = self.request(
                    "GET",
                    f"/api/v2/services/operations/{operation_id}/",
                )
                data = resp.get("data") or {}
                status = data.get("status", "unknown")
                reason = data.get("status_reason", "")

                print(f"    [{elapsed:3d}s] status={status}  {reason}")

                if status in ("completed", "succeeded", "success"):
                    print("[+] Operation completed successfully.")
                    return
                if status in ("failed", "error", "cancelled", "rejected"):
                    print(f"[!] Operation failed: {reason}")
                    return

            except Exception as e:
                print(f"    [{elapsed:3d}s] poll error: {e}")

            time.sleep(interval)
            elapsed += interval

        print(f"[!] Timeout after {max_wait}s.")
