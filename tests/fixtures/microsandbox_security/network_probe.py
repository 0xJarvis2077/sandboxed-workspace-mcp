from __future__ import annotations

import json
import socket
from pathlib import Path

DNS_QUERY = (
    b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    b"\x07example\x03com\x00\x00\x01\x00\x01"
)


def _nameserver() -> str | None:
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "nameserver":
            return fields[1]
    return None


def main() -> None:
    nameserver = _nameserver()
    dns_success = False
    tcp_dns_success = False
    udp_dns_response = False
    external_tcp_success = False

    try:
        infos = socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
        dns_success = bool(infos)
    except OSError:
        pass

    if nameserver is not None:
        try:
            with socket.create_connection((nameserver, 53), timeout=1.0):
                tcp_dns_success = True
        except OSError:
            pass

        query = (
            b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x07example\x03com\x00\x00\x01\x00\x01"
        )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.0)
                sock.sendto(query, (nameserver, 53))
                data, _address = sock.recvfrom(512)
                udp_dns_response = bool(data)
        except OSError:
            pass

    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1.0):
            external_tcp_success = True
    except OSError:
        pass

    print(
        json.dumps(
            {
                "nameserver_present": nameserver is not None,
                "dns_success": dns_success,
                "tcp_dns_success": tcp_dns_success,
                "udp_dns_response": udp_dns_response,
                "external_tcp_success": external_tcp_success,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
