from __future__ import annotations

import requests


def get_public_ip(timeout: int = 5) -> str:
    """
    Return current egress/global IP (best-effort). Never raise; return '' on failure.
    """
    try:
        r = requests.get("https://api.ipify.org", timeout=timeout)
        if r.ok:
            return (r.text or "").strip()
    except Exception:
        pass
    return ""


def mask_ip(ip: str) -> str:
    if not ip:
        return "<unknown>"
    # IPv4: a.b.c.d -> a.b.*.d  / IPv6 は先頭/末尾のみ
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            parts[2] = "*"
            return ".".join(parts)
    if ":" in ip:
        return ip[:6] + "…" + ip[-6:]
    return ip[:3] + "…" + ip[-2:]
