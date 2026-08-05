#!/usr/bin/env python3
"""
ping.py

Programmatic helpers to ping a host/IP from Iran datacenters using the
check-host.net API.

API Reference: https://check-host.net/about/api
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

API_BASE = "https://check-host.net"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "check-host-ping-iran/1.0",
}

POLL_INTERVAL = 5
MAX_WAIT = 120


# ── OOP client ───────────────────────────────────────────────────────────────


class PingClient:
    def __init__(
        self,
        *,
        api_base: str = API_BASE,
        headers: dict[str, str] | None = None,
        poll_interval: int = POLL_INTERVAL,
        max_wait: int = MAX_WAIT,
    ) -> None:
        self.api_base = api_base
        self.headers = headers or HEADERS
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    def api_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_iran_nodes(self) -> list[str]:
        nodes_resp = self.api_get(f"{self.api_base}/nodes/hosts")
        all_nodes = nodes_resp.get("nodes", {})
        return [
            name
            for name, info in all_nodes.items()
            if isinstance(info.get("location"), list)
            and len(info["location"]) > 0
            and info["location"][0] == "ir"
        ]

    def build_check_url(self, host: str, nodes: list[str]) -> str:
        params = urllib.parse.urlencode([("host", host)] + [("node", n) for n in nodes])
        return f"{self.api_base}/check-ping?{params}"

    def submit_ping_check(self, host: str, nodes: list[str]) -> dict:
        url = self.build_check_url(host, nodes)
        logger.debug("[*] Submitting ping check for '%s' ...", host)
        return self.api_get(url)

    def fetch_results(self, request_id: str) -> dict:
        return self.api_get(f"{self.api_base}/check-result/{request_id}")

    @staticmethod
    def _unwrap(value: object) -> object:
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
            first = value[0]
            if len(first) > 0 and isinstance(first[0], list):
                return value[0]
        return value

    @classmethod
    def are_results_complete(cls, results: dict) -> bool:
        for value in results.values():
            if value is None:
                return False
            unwrapped = cls._unwrap(value)
            if isinstance(unwrapped, list) and (
                len(unwrapped) == 0 or (len(unwrapped) == 1 and unwrapped[0] is None)
            ):
                return False
        return True

    @classmethod
    def parse_results(cls, raw: dict) -> tuple[int, int, float | None]:
        total_ok = 0
        total_pings = 0
        all_rtts: list[float] = []

        for value in raw.values():
            pings = cls._unwrap(value)
            if pings is None or not isinstance(pings, list):
                continue
            for entry in pings:
                if entry is None or not isinstance(entry, list) or len(entry) < 1:
                    continue
                total_pings += 1
                if entry[0] == "OK" and len(entry) > 1 and entry[1] is not None:
                    total_ok += 1
                    all_rtts.append(entry[1])

        avg_ms = (sum(all_rtts) / len(all_rtts) * 1000) if all_rtts else None
        return total_ok, total_pings, avg_ms

    def poll_until_complete(self, request_id: str, node_count: int) -> dict:
        elapsed = 0
        results: dict = {}
        while elapsed < self.max_wait:
            try:
                results = self.fetch_results(request_id)
            except urllib.error.URLError:
                time.sleep(self.poll_interval)
                elapsed += self.poll_interval
                continue

            if self.are_results_complete(results):
                logger.debug("[*] All nodes finished (waited %ss)", elapsed)
                return results

            done = sum(
                1
                for v in results.values()
                if v is not None and self._unwrap(v) and self._unwrap(v) != [None]
            )
            logger.debug("    ... %s/%s nodes done (elapsed %ss)", done, node_count, elapsed)
            time.sleep(self.poll_interval)
            elapsed += self.poll_interval

        logger.warning("[!] Timeout after %ss - returning partial results", self.max_wait)
        return results
