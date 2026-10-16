"""LAN discovery for Samsung TVs via SSDP.

Sends an SSDP M-SEARCH multicast and collects responses for `timeout` seconds.
Returns one entry per unique IP whose SSDP description identifies as Samsung.

No external deps — plain UDP socket. The `requests`-based detail fetch is
best-effort: TVs in standby may answer SSDP without serving the description
XML, in which case we still return the IP with a generic name.
"""
from __future__ import annotations

import html
import logging
import re
import socket
from urllib.request import urlopen

log = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
# RemoteControlReceiver is Samsung-specific; ssdp:all also works but is noisier.
SEARCH_TARGETS = ("urn:samsung.com:device:RemoteControlReceiver:1", "ssdp:all")


def _msearch(target: str, timeout: float) -> list[dict[str, str]]:
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(timeout)}\r\n"
        f"ST: {target}\r\n\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        responses: list[dict[str, str]] = []
        while True:
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except socket.timeout:
                break
            headers = _parse_headers(data.decode("utf-8", errors="ignore"))
            headers["_ip"] = ip
            responses.append(headers)
        return responses
    finally:
        sock.close()


def _parse_headers(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _looks_like_samsung(headers: dict[str, str]) -> bool:
    blob = " ".join(headers.values()).lower()
    return "samsung" in blob


def _friendly_name(location: str | None) -> str | None:
    """Best-effort fetch of friendlyName from the device description XML.
    Returns None on any failure — caller should fall back to the IP."""
    # LOCATION comes from whoever answered on the LAN; urlopen also takes
    # file:// and ftp://, so only follow http(s).
    if not location or not location.lower().startswith(("http://", "https://")):
        return None
    try:
        with urlopen(location, timeout=2) as r:
            xml = r.read().decode("utf-8", errors="ignore")
        m = re.search(r"<friendlyName>([^<]+)</friendlyName>", xml)
        return html.unescape(m.group(1).strip()) if m else None
    except Exception as e:
        log.debug(f"description fetch failed for {location}: {e}")
        return None


def discover_tvs(timeout: float = 3.0) -> list[dict[str, str]]:
    """Returns a list of {ip, name} dicts, deduped by IP."""
    found: dict[str, dict[str, str]] = {}
    for target in SEARCH_TARGETS:
        for h in _msearch(target, timeout):
            ip = h.get("_ip", "")
            if not ip or ip in found:
                continue
            if not _looks_like_samsung(h):
                # Fetch description as a fallback — some Samsungs reply to ssdp:all
                # without "samsung" in the headers but identify in the XML.
                name = _friendly_name(h.get("location"))
                if not name or "samsung" not in name.lower():
                    continue
                found[ip] = {"ip": ip, "name": name}
                continue
            name = _friendly_name(h.get("location")) or f"Samsung TV ({ip})"
            found[ip] = {"ip": ip, "name": name}
    return list(found.values())
