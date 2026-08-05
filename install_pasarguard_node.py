#!/usr/bin/env python3
"""
install_pasarguard_node.py

Library utilities to connect over SSH, install PasarGuard Node
(https://github.com/PasarGuard/node) non-interactively, and return the
resulting API key, SSL certificate, and service port for panel registration.

Requirements:
    pip install paramiko

Programmatic usage:
    from install_pasarguard_node import NodeInstaller, SSHCredentials

    creds = SSHCredentials(host="1.2.3.4", username="root", password="secret")
    installer = NodeInstaller()
    result = installer.install_node(creds, node_name="node-eu-1")
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

import logging
import re
import socket
import time
from dataclasses import dataclass

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException,
    BadAuthenticationType,
    NoValidConnectionsError,
)

logger = logging.getLogger(__name__)


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
    password: str | None = None
    key_filename: str | None = None
    key_passphrase: str | None = None
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


class NodeInstaller:
    def __init__(self, *, candidate_roots: list[str] | None = None) -> None:
        self.candidate_roots = candidate_roots or CANDIDATE_ROOTS

    def connect(
        self,
        creds: SSHCredentials,
        ready_timeout: int = 300,
        retry_interval: float = 5.0,
    ) -> paramiko.SSHClient:
        deadline = time.monotonic() + ready_timeout
        attempt = 0
        last_err: Exception | None = None

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
            except BadAuthenticationType as e:
                last_err = e
                client.close()
                logger.warning(
                    "[attempt %s] %s: password auth not accepted yet "
                    "(server currently allows %s). Retrying in %.0fs...",
                    attempt,
                    creds.host,
                    getattr(e, "allowed_types", "?"),
                    retry_interval,
                )
                time.sleep(retry_interval)
                continue
            except (
                NoValidConnectionsError,
                ConnectionRefusedError,
                socket.timeout,
                TimeoutError,
                OSError,
            ) as e:
                last_err = e
                client.close()
                logger.warning(
                    "[attempt %s] %s: SSH not reachable yet (%s). Retrying in %.0fs...",
                    attempt,
                    creds.host,
                    e,
                    retry_interval,
                )
                time.sleep(retry_interval)
                continue
            except AuthenticationException as e:
                client.close()
                raise NodeInstallError(
                    f"SSH authentication failed for {creds.username}@{creds.host}: {e}"
                ) from e
            else:
                return client

        raise NodeInstallError(
            f"Could not establish SSH connection to {creds.host} within {ready_timeout}s "
            f"(likely still finishing boot / cloud-init). Last error: {last_err}"
        )

    @staticmethod
    def run_command(
        client: paramiko.SSHClient,
        command: str,
        input_text: str = "",
        timeout: int = 900,
    ) -> tuple[int, str, str]:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=True)
        if input_text:
            stdin.write(input_text)
            stdin.flush()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return exit_code, out, err

    def find_env_file(self, client: paramiko.SSHClient, node_name: str | None) -> str:
        candidates: list[str] = []
        if node_name:
            candidates.append(f"/opt/{node_name}/.env")
            candidates.append(f"/var/lib/{node_name}/.env")
        for root in self.candidate_roots:
            candidates.append(f"{root.format(name=node_name or 'pg-node')}/.env")

        seen: set[str] = set()
        ordered = [c for c in candidates if not (c in seen or seen.add(c))]

        for path in ordered:
            exit_code, out, _ = self.run_command(client, f"sudo test -f {path} && sudo cat {path}")
            if exit_code == 0 and out.strip():
                return out

        exit_code, out, _ = self.run_command(
            client,
            "sudo find /opt /var/lib -maxdepth 2 -iname '.env' "
            "-exec grep -l API_KEY {} \\; 2>/dev/null | head -n1",
        )
        found_path = out.strip().splitlines()[-1] if out.strip() else ""
        if found_path:
            exit_code, out, _ = self.run_command(client, f"sudo cat {found_path}")
            if exit_code == 0:
                return out

        raise NodeInstallError(
            "Could not locate the node's .env file after installation. "
            "Check the install log and your server's install directory manually."
        )

    @staticmethod
    def extract_env_value(env_text: str, key: str) -> str | None:
        match = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", env_text, re.MULTILINE)
        if not match:
            return None
        return match.group(1).strip().strip('"').strip("'")

    def install_node(
        self,
        creds: SSHCredentials,
        node_name: str | None = None,
        extra_install_args: list[str] | None = None,
        ssh_ready_timeout: int = 300,
    ) -> NodeInstallResult:
        name_arg = f" --name {node_name}" if node_name else ""
        extra_args = ""
        if extra_install_args:
            extra_args = " " + " ".join(extra_install_args)

        install_cmd = INSTALL_CMD_TEMPLATE.format(name_arg=name_arg, extra_args=extra_args)
        client = self.connect(creds, ready_timeout=ssh_ready_timeout)
        try:
            exit_code, out, err = self.run_command(client, install_cmd, timeout=1200)
            if exit_code != 0:
                raise NodeInstallError(
                    f"Install script exited with code {exit_code}.\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
                )

            env_content = self.find_env_file(client, node_name)
            api_key = self.extract_env_value(env_content, "API_KEY")
            port = self.extract_env_value(env_content, "SERVICE_PORT") or "62050"
            cert_path = self.extract_env_value(env_content, "SSL_CERT_FILE")

            if not api_key:
                raise NodeInstallError(
                    "Install appeared to succeed but API_KEY could not be located "
                    "in the node's .env file. Install log:\n" + out
                )

            certificate = ""
            if cert_path:
                cert_exit, cert_out, _ = self.run_command(client, f"sudo cat {cert_path}")
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




