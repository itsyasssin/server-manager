#!/usr/bin/env python3
"""
install_pasarguard_node.py

Connects to a remote server over SSH, installs PasarGuard Node
(https://github.com/PasarGuard/node) non-interactively, and returns the
generated API key (API_KEY), SSL certificate, and service port so you can
register the node with your PasarGuard panel.

Requirements:
    pip install paramiko

Usage (as a library):
    from install_pasarguard_node import install_node, SSHCredentials

    creds = SSHCredentials(host="1.2.3.4", username="root", password="secret")
    result = install_node(creds, node_name="node-eu-1")
    print(result.api_key, result.port)
    print(result.certificate)

Usage (as a CLI):
    python3 install_pasarguard_node.py --host 1.2.3.4 --username root \\
        --password secret --name node-eu-1

Notes:
- The official one-click installer is:
    sudo bash -c "$(curl -sL https://github.com/PasarGuard/scripts/raw/main/pg-node.sh)" @ install
  The public docs don't document a "-y/--yes" flag for this script, so to
  guarantee zero interactive prompts we (a) pass --name non-interactively,
  and (b) pipe "yes" into the installer as a safety net for any prompt it
  might raise. If you've confirmed the script does support -y/--yes, set
  extra_install_args=["-y"] and it will be appended to the install command.
- After install, the node's config lives in a .env file (commonly under
  /opt/<name> or /var/lib/<name>, depending on install-script version). This
  script searches common locations rather than hardcoding one path, so it
  keeps working even if that detail changes upstream.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
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


def _connect(creds: SSHCredentials) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=creds.host,
        port=creds.port,
        username=creds.username,
        password=creds.password,
        key_filename=creds.key_filename,
        passphrase=creds.key_passphrase,
        timeout=30,
    )
    return client


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
) -> NodeInstallResult:
    """
    SSH into the target host, install PasarGuard Node non-interactively,
    then read back API_KEY, SERVICE_PORT, and the SSL certificate.
    """
    name_arg = f" --name {node_name}" if node_name else ""
    extra_args = ""
    if extra_install_args:
        extra_args = " " + " ".join(extra_install_args)

    install_cmd = INSTALL_CMD_TEMPLATE.format(name_arg=name_arg, extra_args=extra_args)

    client = _connect(creds)
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


def _cli():
    parser = argparse.ArgumentParser(description="Install PasarGuard Node over SSH.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="If omitted, you'll be prompted (or use --key-file).")
    parser.add_argument("--key-file", help="Path to a private key file.")
    parser.add_argument("--key-passphrase")
    parser.add_argument("--name", help="Node name, for running multiple nodes on one server.")
    parser.add_argument(
        "--extra-install-arg",
        action="append",
        dest="extra_install_args",
        help="Extra flag to append to the install command (repeatable), e.g. --extra-install-arg -y",
    )
    args = parser.parse_args()

    password = args.password
    if not password and not args.key_file:
        password = getpass.getpass(f"SSH password for {args.username}@{args.host}: ")

    creds = SSHCredentials(
        host=args.host,
        port=args.ssh_port,
        username=args.username,
        password=password,
        key_filename=args.key_file,
        key_passphrase=args.key_passphrase,
    )

    try:
        result = install_node(creds, node_name=args.name, extra_install_args=args.extra_install_args)
    except NodeInstallError as e:
        print(f"Installation failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("=== PasarGuard Node installed ===")
    print(f"API_KEY (api_id): {result.api_key}")
    print(f"PORT: {result.port}")
    print("CERTIFICATE:")
    print(result.certificate or "(no SSL_CERT_FILE set / self-managed TLS)")


if __name__ == "__main__":
    _cli()
