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

import json
import logging
from random import shuffle
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import requests
import uuid as _uuid

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://www.doprax.com"

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
# ── HTTP helper ──────────────────────────────────────────────────────────────

def api_request(
    method: str,
    path: str,
    api_key: str,
    body: dict | None = None,
    query: dict | None = None,
    proxy: str | None = None,
) -> dict:
    url = f"{BASE_URL}{path}"

    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    proxies = None
    if proxy:
        proxies = {
            "http": proxy,
            "https": proxy,
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
                timeout=60,
            )

            resp.raise_for_status()

            try:

                return resp.json()
            except requests.JSONDecodeError:
                logging.warning(
                    "JSON decode failed (%d/%d), retrying...",
                    attempt + 1,
                    max_attempts,
                )
                logging.debug(resp.text)

                if attempt == max_attempts - 1:
                    return {}

                time.sleep(1)
                continue

        except:
            logging.exception("trackback",
            )
            logging.warning(
                "get response failed (%d/%d), retrying...",
                attempt + 1,
                max_attempts,
            )

            if attempt == max_attempts - 1:
                return {}

            time.sleep(1)

    # return {}

def _all_pages(api_key: str, path: str,
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
        resp = api_request("GET", path, api_key, query=query)
        items = resp.get("data", [])
        all_items.extend(items)
        meta = resp.get("meta", {})
        if not first_meta:
            first_meta = meta
        if not meta.get("has_next"):
            break
        query["page"] = meta["page"] + 1

    return all_items, first_meta


# ── Catalogue helpers ────────────────────────────────────────────────────────

def get_password(api_key: str, vm_code: str) -> str:
    resp = api_request("GET", f"/api/v2/vms/{vm_code}/actions/access/", api_key)
    return resp.get("data", {}).get("tempPass")


def get_catalogue(api_key: str, service_type: str | None = None,
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
    resp = api_request("GET", "/api/v2/catalogue/service-catalogue/", api_key, query=query)
    return resp.get("data", [])


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


def get_plan_country(plan: dict, flag=False) -> str:
    """Get the 2-letter country code from a plan's location options."""
    for _, opt in _extract_all_options(plan):
        meta = opt.get("metadata") or {}
        # Direct country_code in metadata
        cc = meta.get("country_code")
        if cc:
            if flag:
                _code = FLAGS.get(cc.lower(), "Vip")
                return _code
            return cc
        # Code might be like "ir-thr" — take first segment
        code = opt.get("code", "")
        if len(code) >= 2 and "-" in code:
            if flag:
                _code = FLAGS.get(code.split("-")[0].lower(), "Vip")
                return _code
            return code.split("-")[0]
    return "vip"


def get_plan_datacenter(plan: dict) -> str:
    """Get the first datacenter/region code from a plan."""
    return plan.get("provider", {}).get("code", "")

def find_image_option(plan: dict, image_hint: str) -> tuple[str, str, str] | None:
    """Find an OS/image option matching the hint (case-insensitive substring match).

    Searches all allowed_options for image/os/system related option groups
    (e.g. 'operating_system').

    Returns (opt_key, option_id, display_code) for use in the 'selections'
    field of the create request, or None if nothing matched.
    """
    hint = image_hint.lower()
    for opt_key, opt in _extract_all_options(plan):
        kl = opt_key.lower()
        if "image" not in kl and "os" not in kl and "system" not in kl:
            continue
        code = opt.get("code", "")
        label = opt.get("label", "")
        if hint in code.lower() or hint in label.lower():
            opt_id = get_option_id(opt)
            if not opt_id:
                continue
            return (opt_key, opt_id, code or label)
    return None


def match_plan(plan: dict, countries: list[str] | None,
               datacenter: str | None, max_budget_cents: int | None) -> bool:
    """Check if a plan matches all given filters."""
    if datacenter:
        plan_dc = get_plan_datacenter(plan)
        if plan_dc.lower() != datacenter.lower():
            return False

    if countries:
        plan_country = get_plan_country(plan)
        if plan_country not in [c.lower() for c in countries]:
            return False

    if max_budget_cents is not None:
        monthly = extract_monthly_price_cents(plan)
        if monthly > max_budget_cents:
            return False

    return True


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_list(api_key: str, args) -> None:
    """List all VM instances.

    GET /api/v2/services/instances/list/
    Query: page, page_size, search, region, status, service_type, provider,
           sort_by, sort_dir, date_from, date_to
    Response: { success, data: [ServiceSummarySchema], meta: ServiceMetaSchema }
    """
    query: dict = {}
    if args.search:
        query["search"] = args.search
    if args.region:
        query["region"] = args.region
    if args.status:
        query["status"] = args.status
    if args.service_type:
        query["service_type"] = args.service_type

    vms, meta = _all_pages(api_key, "/api/v2/services/instances/list/", query)

    result = {
        "total": meta.get("total", 0),
        "active_total": meta.get("active_total", 0),
        "vms": vms,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_detail(api_key: str, args) -> None:
    """Get full VM detail including IP, specs, access info.

    GET /api/v2/services/instances/{service_id}/detail/
    Response: { success, data: ServiceDetailDataSchema }
      data.service  — ServiceSummarySchema
      data.vm       — VMDataSchema (id, name, cpu, ram_gb, ssd_gb, ipv4, ipv6, country, location_name, os_name, ...)
      data.access   — ServiceAccessSchema (username, public_ipv4, public_ipv6, active_ssh_key, ...)
      data.selections, data.dimensions, data.price_components, data.links
    """
    sid = args.service_id
    resp = api_request("GET", f"/api/v2/services/instances/{sid}/detail/", api_key)
    print(json.dumps(resp, indent=2, ensure_ascii=False))


def cmd_catalogue(api_key: str, args) -> None:
    """List available plans, optionally filtered by country and budget.

    GET /api/v2/catalogue/service-catalogue/
    """
    plans = get_catalogue(api_key)

    countries = [c.strip() for c in args.country.split(",")] if args.country else None
    max_cents = int(args.max_budget_usd * 100) if args.max_budget_usd is not None else None

    filtered = []
    for plan in plans:
        if not match_plan(plan, countries, None, max_cents):
            continue
        monthly = extract_monthly_price_cents(plan)
        filtered.append({
            "product_version_id": plan.get("product_version_id"),
            "plan_name": plan.get("product", {}).get("name"),
            "service_type": plan.get("product", {}).get("service_type"),
            "provider": plan.get("provider", {}).get("name"),
            "provider_code": plan.get("provider", {}).get("code"),
            "country": get_plan_country(plan),
            "datacenter": get_plan_datacenter(plan),
            "monthly_price_usd": round(monthly / 100, 2) if monthly else None,
            "currency": plan.get("currency"),
        })

    filtered.sort(key=lambda x: x["monthly_price_usd"] or 999999)
    print(json.dumps(filtered, indent=2, ensure_ascii=False))


def get_plan_location_selection(plan: dict) -> tuple[str, str] | None:
    """Get the location option key and UUID for the 'selections' field.

    Returns (option_key, option_id) — e.g. ("location", "ec5bc1aa-db5f-...") —
    for direct use as selections[option_key] = {"optionId": option_id}.
    Returns None if no location option (with a resolvable id) exists.
    """
    all_location = []
    for opt_key, opt in _extract_all_options(plan):
        if "location" in opt_key.lower() or "region" in opt_key.lower() or "datacenter" in opt_key.lower():
            opt_id = get_option_id(opt)
            if opt_id:
                all_location.append((opt_key, opt_id))

    shuffle(all_location)
    return all_location[0] if all_location else None

def cmd_add(api_key: str, args) -> None:
    """Smart-add: find the cheapest matching plan and create a VM.

    POST /api/v2/services/instances/
    Body (ServiceCreateRequestSchemaRequest):
      Required: product_version_id (uuid), idempotency_key
      Optional: name, description, metadata, selections, image, container_code,
                container_codes, container_id, container_ids, app_name, container_name
    """
    countries = [c.strip() for c in args.country.split(",")] if args.country else None
    max_cents = int(args.max_budget_usd * 100) if args.max_budget_usd is not None else None

    # 1. Fetch catalogue
    print("[*] Fetching catalogue ...")
    plans = get_catalogue(api_key)

    # 2. Filter matching plans
    matching = [
        p for p in plans
        if match_plan(p, countries, args.datacenter, max_cents)
    ]

    if not matching:
        print("[ERROR] No plans match your filters.")
        print(f"  Countries : {countries or 'any'}")
        print(f"  Datacenter: {args.datacenter or 'any'}")
        print(f"  Max budget: ${args.max_budget_usd}/mo")
        sys.exit(1)

    # 3. Sort by monthly price (cheapest first)
    matching.sort(key=lambda p: extract_monthly_price_cents(p))

    # 4. Find first plan with a matching OS image (if --image given)
    chosen_plan = None
    chosen_image_option = None  # (opt_key, option_id, display_code)

    for plan in matching:
        if args.image:
            img_opt = find_image_option(plan, args.image)
            if not img_opt:
                continue
            chosen_image_option = img_opt
        chosen_plan = plan
        break

    if not chosen_plan:
        print(f"[ERROR] No plan has an image matching '{args.image}'")
        sys.exit(1)

    pv_id = chosen_plan["product_version_id"]
    monthly = extract_monthly_price_cents(chosen_plan)
    provider = chosen_plan.get("provider", {}).get("name", "?")
    plan_name = chosen_plan.get("product", {}).get("name", "?")
    dc = get_plan_datacenter(chosen_plan)

    print(f"[+] Best match:")
    print(f"    Plan      : {plan_name}")
    print(f"    Provider  : {provider}")
    print(f"    Datacenter: {dc or '(unknown)'}")
    print(f"    Price     : ${monthly / 100:.2f}/mo")
    if chosen_image_option:
        print(f"    Image     : {chosen_image_option[2]}")

    if not args.yes:
        confirm = input("\n[?] Create this VM? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    # 5. Build create request per OpenAPI spec.
    # NOTE: both location AND operating system are chosen via 'selections',
    # each as {"optionId": "<uuid>"} — NOT via a top-level 'container_code'
    # or a bare code string.
    body: dict = {
        "product_version_id": pv_id,
        "idempotency_key": str(_uuid.uuid4()),
        "name": args.name,
        "metadata": {"access_method": "password"},
    }
    if args.description:
        body["description"] = args.description

    selections: dict = {}

    loc = get_plan_location_selection(chosen_plan)
    if loc:
        opt_key, opt_id = loc
        selections[opt_key] = {"optionId": opt_id}
    else:
        print("[WARNING] No location option found in plan — VM creation will likely fail.")

    if chosen_image_option:
        opt_key, opt_id, _display = chosen_image_option
        selections[opt_key] = {"optionId": opt_id}

    if selections:
        body["selections"] = selections

    print(f"\n[*] Creating VM '{args.name}' ...")
    resp = api_request("POST", "/api/v2/services/instances/", api_key, body=body)
    print(json.dumps(resp, indent=2, ensure_ascii=False))

    # 6. Poll the async operation if one was returned
    data = resp.get("data")
    if data and data.get("operation_id"):
        op_id = data["operation_id"]
        print(f"\n[*] Operation {op_id} — polling status ...")
        _poll_operation(api_key, op_id)


def delete_vm(api_key: str, service_id: str) -> dict:
    """Delete a VM by issuing the 'delete' lifecycle action.

    POST /api/v2/services/instances/{service_id}/operations/
    Returns the raw API response.
    """
    body = {
        "action": "delete",
        "idempotency_key": str(_uuid.uuid4()),
    }
    return api_request(
        "POST",
        f"/api/v2/services/instances/{service_id}/operations/",
        api_key,
        body=body,
    )


def _get_vm_ip(api_key: str, service_id: str) -> str:
    """Fetch VM detail and extract its IPv4 address.

    The list endpoint only returns a summary (no IP), so each VM must be
    looked up individually via GET /services/instances/{service_id}/detail/.
    Response: { success, data: ServiceDetailDataSchema }
      data.vm     — VMDataSchema (id, ..., ipv4, ipv6, ...)
      data.access — ServiceAccessSchema (username, public_ipv4, ...)
    """
    resp = api_request("GET", f"/api/v2/services/instances/{service_id}/detail/", api_key)
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


def delete_vm_by_ip(api_key: str, ip: str, *, require_unique: bool = True) -> dict:
    """Find Doprax VM(s) by IP and delete them.

    - Scans all instances via /services/instances/list/ (all pages)
    - Matches if extracted ipv4 == ip
    - If require_unique=True and multiple match, returns an error summary (no deletions).

    Returns a summary dict.
    """
    vms, _meta = _all_pages(api_key, "/api/v2/services/instances/list/")

    matches: list[dict] = []
    for vm in vms:
        sid = vm.get("service_id") or vm.get("id")
        if not sid:
            continue
        vm_ip = _get_vm_ip(api_key, str(sid))
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
            resp = delete_vm(api_key, sid)
            summary["deleted_service_ids"].append(sid)
            # If operation_id exists, include it for the caller
            data = (resp or {}).get("data") or {}
            op_id = data.get("operation_id")
            if op_id:
                summary.setdefault("operation_ids", []).append(op_id)
        except Exception as e:
            summary["errors"].append({"type": "delete", "service_id": sid, "error": str(e)})

    return summary


def cmd_delete_by_ip(api_key: str, args) -> None:
    """Delete a VM by IP address."""
    result = delete_vm_by_ip(api_key, args.ip, require_unique=not args.allow_multiple)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_delete(api_key: str, args) -> None:
    """Delete a VM by issuing the 'delete' lifecycle action.

    POST /api/v2/services/instances/{service_id}/operations/
    Body (ServiceActionRequestSchemaRequest):
      Required: action (non-empty), idempotency_key (non-empty)
      Optional: ip_version, container_code, container_codes, container_id, container_ids
    """
    service_id = args.service_id

    print(f"[*] Deleting VM {service_id} ...")
    resp = delete_vm(api_key, service_id)
    print(json.dumps(resp, indent=2, ensure_ascii=False))

    data = resp.get("data")
    if data and data.get("operation_id"):
        _poll_operation(api_key, data["operation_id"])


# ── Operation polling ────────────────────────────────────────────────────────

def _poll_operation(api_key: str, operation_id: str, max_wait: int = 120) -> None:
    """Poll GET /api/v2/services/operations/{operation_id}/ until done."""
    elapsed = 0
    interval = 5
    while elapsed < max_wait:
        try:
            resp = api_request(
                "GET",
                f"/api/v2/services/operations/{operation_id}/",
                api_key,
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



