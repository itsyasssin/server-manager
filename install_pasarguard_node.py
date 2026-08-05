#!/usr/bin/env python3
"""
install_pasarguard_node.py

Library utilities to connect over SSH, install PasarGuard Node
(https://github.com/PasarGuard/node) non-interactively, and return the
resulting API key, SSL certificate, and service port for panel registration.

Requirements:
    pip install paramiko

Programmatic usage:
    from install_pasarguard_node import install_node, SSHCredentials

    creds = SSHCredentials(host="1.2.3.4", username="root", password="secret")
    result = install_node(creds, node_name="node-eu-1")
    print(result.api_key, result.port)
    print(result.certificate)

Notes:
- The official one-click installer is:
    sudo bash -c "$(curl -sL https://github.com/PasarGuard/scripts/raw/main/pg-node.sh)" @ install
- After install, the node's config usually lives in a .env file under
  /opt/<name> or /var/lib/<name>. This module searches common locations
  instead of hardcoding one path.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from typing import Optional
import logging

import paramiko
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


INSTALL_CMD_TEMPLATE = (
    'sudo bash -c "$(curl -sL https://github.com/PasarGuard/scripts/raw/main/pg-node.sh)" @ install -y'
)

# Directories the pg-node.sh installer (or its docker-compose variant) is
# known to use across versions. We search all of them.
CANDIDATE_ROOTS = [
    "/opt/{name}",
    "/var/lib/{name}",
    "/opt/pasarguard-node",
    "/var/lib/pasarguard-node",
    "/opt/pg-node",
    "/var/lib/pg-node",
]


@dataclass
class SSHCredentials:
    host: str
    username: str
    password: Optional[str] = None
    key_filename: Optional[str] = None
    key_passphrase: Optional[str] = None
    port: int = 22


@dataclass
class NodeInstallResult:
    api_key: str
    port: str
    certificate: str
    raw_env: str
    install_log: str


class NodeInstallError(RuntimeError):
    pass


def _connect(
    creds: SSHCredentials,
    ready_timeout: int = 300,
    retry_interval: float = 5.0,
) -> paramiko.SSHClient:
    """
    Connect over SSH, retrying for a while if the server isn't fully ready yet.

    Freshly-booted cloud VMs commonly bring up sshd before cloud-init has
    finished setting the root password / enabling password auth. During that
    window the server correctly reports "Bad authentication type; allowed
    types: ['publickey']" even though the password is valid and will work a
    few seconds later. We treat that (plus connection-refused/timeout, which
    happen while sshd itself is still starting) as transient and retry until
    ready_timeout elapses, instead of failing on the first attempt.
    """
    deadline = time.monotonic() + ready_timeout
    attempt = 0
    last_err: Optional[Exception] = None

    while time.monotonic() < deadline:
        attempt += 1
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=creds.host,
                port=creds.port,
                username=creds.username,
                password=creds.password,
                key_filename=creds.key_filename,
                passphrase=creds.key_passphrase,
                timeout=30,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.ssh_exception.BadAuthenticationType as e:
            # Password auth not enabled yet (cloud-init still running) -> retry.
            last_err = e
            client.close()
            logging.warning(
                f"[attempt {attempt}] {creds.host}: password auth not accepted yet "
                f"(server currently allows {getattr(e, 'allowed_types', '?')}). "
                f"Retrying in {retry_interval:.0f}s..."
            )
            time.sleep(retry_interval)
            continue
        except (
            paramiko.ssh_exception.NoValidConnectionsError,
            ConnectionRefusedError,
            socket.timeout,
            TimeoutError,
            OSError,
        ) as e:
            # sshd not up yet / network not ready -> retry.
            last_err = e
            client.close()
            logging.warning(
                f"[attempt {attempt}] {creds.host}: SSH not reachable yet ({e}). "
                f"Retrying in {retry_interval:.0f}s..."
            )
            time.sleep(retry_interval)
            continue
        except paramiko.ssh_exception.AuthenticationException as e:
            # Genuinely wrong credentials -> don't waste the whole timeout retrying.
            client.close()
            raise NodeInstallError(
                f"SSH authentication failed for {creds.username}@{creds.host}: {e}"
            ) from e
        else:
            # Connected successfully - return the live client as-is.
            return client

    raise NodeInstallError(
        f"Could not establish SSH connection to {creds.host} within {ready_timeout}s "
        f"(likely still finishing boot / cloud-init). Last error: {last_err}"
    )


def _run(client: paramiko.SSHClient, command: str, input_text: str = "", timeout: int = 900):
    """Run a command over SSH, optionally feeding stdin, and return (exit_code, stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=True)
    if input_text:
        stdin.write(input_text)
        stdin.flush()
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return exit_code, out, err


def install_node(
    creds: SSHCredentials,
    node_name: Optional[str] = None,
    extra_install_args: Optional[list[str]] = None,
    ssh_ready_timeout: int = 300,
) -> NodeInstallResult:
    """
    SSH into the target host, install PasarGuard Node non-interactively,
    then read back API_KEY, SERVICE_PORT, and the SSL certificate.

    ssh_ready_timeout: how long (seconds) to keep retrying the initial SSH
    connection while the VM finishes booting / cloud-init runs. Bump this up
    if your provider's VMs are slow to enable password auth after "active".
    """
    name_arg = f" --name {node_name}" if node_name else ""
    extra_args = ""
    if extra_install_args:
        extra_args = " " + " ".join(extra_install_args)

    install_cmd = INSTALL_CMD_TEMPLATE.format(name_arg=name_arg, extra_args=extra_args)
    client = _connect(creds, ready_timeout=ssh_ready_timeout)
    try:
        # Pipe "yes" as a safety net in case the script hits any interactive
        # prompt (package manager confirmations, overwrite prompts, etc).
        # This does not change any explicit flags you pass in extra_install_args.
        full_cmd = f"{install_cmd}"

        exit_code, out, err = _run(client, full_cmd, timeout=1200)
        if exit_code != 0:
            raise NodeInstallError(
                f"Install script exited with code {exit_code}.\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
            )

        env_content = _find_env_file(client, node_name)
        api_key = _extract(env_content, "API_KEY")
        port = _extract(env_content, "SERVICE_PORT") or "62050"
        cert_path = _extract(env_content, "SSL_CERT_FILE")

        if not api_key:
            raise NodeInstallError(
                "Install appeared to succeed but API_KEY could not be located "
                "in the node's .env file. Install log:\n" + out
            )

        certificate = ""
        if cert_path:
            cert_exit, cert_out, cert_err = _run(client, f"sudo cat {cert_path}")
            if cert_exit == 0:
                certificate = cert_out.strip()

        certificate = certificate.replace("\r", "")

        return NodeInstallResult(
            api_key=api_key,
            port=port,
            certificate=certificate,
            raw_env=env_content,
            install_log=out,
        )
    finally:
        client.close()


def _find_env_file(client: paramiko.SSHClient, node_name: Optional[str]) -> str:
    """Search common install locations for the node's .env file and return its content."""
    candidates = []
    if node_name:
        candidates.append(f"/opt/{node_name}/.env")
        candidates.append(f"/var/lib/{node_name}/.env")
    for root in CANDIDATE_ROOTS:
        candidates.append(f"{root.format(name=node_name or 'pg-node')}/.env")

    # De-duplicate while preserving order.
    seen = set()
    ordered = [c for c in candidates if not (c in seen or seen.add(c))]

    for path in ordered:
        exit_code, out, _ = _run(client, f"sudo test -f {path} && sudo cat {path}")
        if exit_code == 0 and out.strip():
            return out

    # Fallback: search the filesystem directly for any pg-node-style .env.
    exit_code, out, _ = _run(
        client,
        "sudo find /opt /var/lib -maxdepth 2 -iname '.env' "
        "-exec grep -l API_KEY {} \\; 2>/dev/null | head -n1",
    )
    found_path = out.strip().splitlines()[-1] if out.strip() else ""
    if found_path:
        exit_code, out, _ = _run(client, f"sudo cat {found_path}")
        if exit_code == 0:
            return out

    raise NodeInstallError(
        "Could not locate the node's .env file after installation. "
        "Check the install log and your server's install directory manually."
    )


def _extract(env_text: str, key: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", env_text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")



