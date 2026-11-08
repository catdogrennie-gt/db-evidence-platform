"""Resolve and pin explicitly allowed database endpoints before opening sockets."""

from __future__ import annotations

import ipaddress
import re
import socket

from .common import PlatformError

_HOST = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or not _HOST.fullmatch(value):
        raise PlatformError(422, "invalid_host", "数据库地址必须为主机名或 IP 地址。")
    return value.strip("[]")


def _resolve(host, port, networks):
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = list(
                dict.fromkeys(
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                )
            )
        except (socket.gaierror, OSError, ValueError):
            raise PlatformError(
                422, "dns_resolution_failed", "主机名解析失败，请检查地址或内部 DNS。"
            ) from None
    if not addresses:
        raise PlatformError(422, "dns_resolution_failed", "主机名没有可用地址。")
    for address in addresses:
        if (
            address.is_unspecified
            or address.is_multicast
            or address.is_link_local
            or not any(address.version == net.version and address in net for net in networks)
        ):
            raise PlatformError(403, "target_not_allowed", "目标地址不在平台允许的数据库网络范围。")
        if address.version == 4 and str(address).endswith(".255"):
            raise PlatformError(403, "target_not_allowed", "广播地址不能作为数据库目标。")
    return str(addresses[0])


def validate_target(config, connection: dict) -> dict:
    """Return sanitized socket configuration; do not persist credentials here.

    A resolved IP is pinned for direct connectors while the original hostname is
    retained for TLS identity. SSH destinations are evaluated on the SSH server;
    they must be explicitly configured in that pinned profile.
    """
    mode = connection.get("mode", "direct")
    if mode not in ("direct", "ssh"):
        raise PlatformError(422, "invalid_connection_mode", "仅支持直连或已登记的 SSH 通道。")
    host = _host(connection.get("db_host"))
    try:
        port = int(connection.get("db_port", 3306))
    except (TypeError, ValueError):
        raise PlatformError(422, "invalid_port", "数据库端口无效。") from None
    if isinstance(connection.get("db_port"), bool) or not 1 <= port <= 65535:
        raise PlatformError(422, "invalid_port", "数据库端口无效。")
    networks = [ipaddress.ip_network(value, strict=False) for value in config.allowed_networks]
    allowed_ports = getattr(config, "allowed_db_ports", None) or [3306, 3307, 3308, 3310]
    if port not in allowed_ports:
        raise PlatformError(403, "port_not_allowed", "数据库端口尚未加入平台允许列表。")
    # Client-supplied filesystem paths, protocol URLs, SSH credentials and timeouts
    # never cross this boundary. TLS file references are resolved server-side.
    result = {
        "mode": mode,
        "db_host": host,
        "db_port": port,
        "tls_profile_ref": connection.get("tls_profile_ref"),
        "ssh_profile_ref": connection.get("ssh_profile_ref"),
        "tls_mode": connection.get("tls_mode", "verify_identity"),
    }
    if mode == "ssh":
        ref = result["ssh_profile_ref"]
        profile = next((item for item in config.ssh_profiles if item.get("id") == ref), None)
        if not profile:
            raise PlatformError(422, "ssh_profile_not_found", "请选择已登记并核验指纹的 SSH 通道。")
        _resolve(_host(profile["host"]), profile.get("port", 22), networks)
        if host not in profile.get("allowed_destinations", []) or port not in profile.get(
            "allowed_ports", []
        ):
            raise PlatformError(
                403, "ssh_destination_not_allowed", "目标不在该 SSH 通道允许的数据库范围。"
            )
    else:
        result["db_host"] = _resolve(host, port, networks)
        result["tls_server_name"] = host
        if (
            result["tls_mode"] == "disabled"
            and not ipaddress.ip_address(result["db_host"]).is_loopback
        ):
            raise PlatformError(422, "tls_required", "远端直连必须验证 TLS 身份。")
    return result


def resolve_ssh_profile(config, ref: str) -> dict:
    profile = next((item for item in config.ssh_profiles if item.get("id") == ref), None)
    if not profile:
        raise PlatformError(422, "ssh_profile_not_found", "SSH 通道引用不存在。")
    networks = [ipaddress.ip_network(value, strict=False) for value in config.allowed_networks]
    # Keep pinned host-key lookup name; this config is an OS-protected trusted
    # profile, never a name supplied in an onboarding request.
    _resolve(_host(profile["host"]), profile.get("port", 22), networks)
    return dict(profile)
