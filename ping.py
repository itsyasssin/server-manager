#!/usr/bin/env python3
"""
ping.py

Programmatic helpers to ping a host/IP from Iran datacenters using the
check-host.net API.

API Reference: https://check-host.net/about/api
"""

import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Constants ────────────────────────────────────────────────────────────────

API_BASE = "https://check-host.net"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "check-host-ping-iran/1.0",
}

POLL_INTERVAL = 5
MAX_WAIT = 120


# ── Pure / Functional helpers ────────────────────────────────────────────────

def api_get(url: str) -> dict:
    """Perform a GET request and return parsed JSON."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_iran_nodes() -> list[str]:
    """Dynamically fetch all Iran nodes from the check-host.net nodes list."""
    nodes_resp = api_get(f"{API_BASE}/nodes/hosts")
    all_nodes = nodes_resp.get("nodes", {})
    return [
        name for name, info in all_nodes.items()
        if isinstance(info.get("location"), list) and len(info["location"]) > 0
        and info["location"][0] == "ir"
    ]


def build_check_url(host: str, nodes: list[str]) -> str:
    """Build the ping-check URL targeting specific nodes."""
    params = urllib.parse.urlencode(
        [("host", host)] + [("node", n) for n in nodes]
    )
    return f"{API_BASE}/check-ping?{params}"


def submit_ping_check(host: str, nodes: list[str]) -> dict:
    """Submit a ping check request and return the response dict."""
    url = build_check_url(host, nodes)
    logging.debug(f"[*] Submitting ping check for '{host}' ...")
    return api_get(url)


def fetch_results(request_id: str) -> dict:
    """Fetch (possibly still-running) results for a request."""
    return api_get(f"{API_BASE}/check-result/{request_id}")


def _unwrap(value):
    """Unwrap double-nested ping array: [[ping1, ...]] -> [ping1, ...]"""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        first = value[0]
        if len(first) > 0 and isinstance(first[0], list):
            return value[0]
    return value


def are_results_complete(results: dict) -> bool:
    """Check if all nodes have finished their checks."""
    for value in results.values():
        if value is None:
            return False
        unwrapped = _unwrap(value)
        if isinstance(unwrapped, list) and (len(unwrapped) == 0 or
            (len(unwrapped) == 1 and unwrapped[0] is None)):
            return False
    return True


def parse_results(raw: dict) -> tuple[int, int, float | None]:
    """Parse raw results into (total_ok, total_pings, overall_avg_ms).

    Returns overall_avg_ms as None if no successful pings.
    """
    total_ok = 0
    total_pings = 0
    all_rtts = []  # individual ping RTTs in seconds

    for node_id, value in raw.items():
        pings = _unwrap(value)
        if pings is None or not isinstance(pings, list):
            continue
        for entry in pings:
            if entry is None or not isinstance(entry, list) or len(entry) < 1:
                continue
            total_pings += 1
            if entry[0] == "OK" and len(entry) > 1 and entry[1] is not None:
                total_ok += 1
                all_rtts.append(entry[1])
            # TIMEOUT, MALFORMED, etc. count as pings but not OK

    avg_ms = (sum(all_rtts) / len(all_rtts) * 1000) if all_rtts else None
    return total_ok, total_pings, avg_ms


# ── Main pipeline ────────────────────────────────────────────────────────────

def poll_until_complete(request_id: str, node_count: int) -> dict:
    """Poll the API until results are complete or MAX_WAIT is reached."""
    elapsed = 0
    while elapsed < MAX_WAIT:
        try:
            results = fetch_results(request_id)
        except urllib.error.URLError:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            continue

        if are_results_complete(results):
            logging.debug(f"[*] All nodes finished (waited {elapsed}s)")
            return results

        done = sum(1 for v in results.values()
                    if v is not None and _unwrap(v) and _unwrap(v) != [None])
        logging.debug(f"    ... {done}/{node_count} nodes done (elapsed {elapsed}s)")
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    logging.warning(f"[!] Timeout after {MAX_WAIT}s - returning partial results")
    return results


def run(host: str) -> None:
    """Main pipeline: discover nodes -> submit -> poll -> raw + summary."""

    # 1. Discover Iran nodes dynamically
    logging.debug("[*] Discovering Iran nodes ...")
    iran_nodes = get_iran_nodes()
    if not iran_nodes:
        logging.error("[ERROR] No Iran nodes found.")
        sys.exit(1)
    logging.debug(f"[+] Found {len(iran_nodes)} Iran nodes: {iran_nodes}")


    # 2. Submit ping check
    check_resp = submit_ping_check(host, iran_nodes)

    if not check_resp.get("ok"):
        logging.error(f"Check request failed: {check_resp}")
        sys.exit(1)

    request_id = check_resp["request_id"]
    logging.debug(f"[+] Request ID     : {request_id}")
    logging.debug(f"[+] Permanent link : {check_resp.get('permanent_link', '')}")
    logging.debug(f"[+] Nodes assigned : {list(check_resp.get('nodes', {}).keys())}")
    # print()

    # 3. Poll for results
    logging.debug("[*] Waiting for results ...")
    raw_results = poll_until_complete(request_id, len(iran_nodes))

    # 4. Print raw results
    logging.debug(json.dumps(raw_results, indent=2, ensure_ascii=False))

    # 5. Compute & print overall stats
    total_ok, total_pings, avg_ms = parse_results(raw_results)
    overall_rate = (total_ok / total_pings * 100) if total_pings > 0 else 0
    avg_str = f"{avg_ms:.1f}ms" if avg_ms is not None else "N/A"
    logging.info(f"{host}: overall_rate: {overall_rate:.1f}% | overall_avg: {avg_str}")



