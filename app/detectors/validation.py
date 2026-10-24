"""Validacion de la URL de un servicio antes de guardarla.

El orquestador hace peticiones a esta URL, asi que un admin no deberia poder apuntarlo a los
endpoints de metadatos de la nube (SSRF). Se permiten hosts privados y localhost, porque los
servicios de IA suelen estar en la red interna. No cubre un nombre DNS que resuelva a una
direccion bloqueada.
"""
import ipaddress
import re
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
_LEGACY_IPV4 = re.compile(r"[0-9a-fx.]+")


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _LEGACY_IPV4.fullmatch(host):  # formas como 2852039166 o 0xa9fea9fe, que los sockets si aceptan
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def validate_service_url(url: str) -> str:
    url = url.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("La URL debe ser http o https y tener un host")
    if parts.username or parts.password:
        raise ValueError("La URL no debe incluir credenciales")
    host = parts.hostname.lower()
    if host in _BLOCKED_HOSTS:
        raise ValueError("Host no permitido")
    ip = _as_ip(host)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip is not None and (ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved):
        raise ValueError("Direccion no permitida")
    return url.rstrip("/")
