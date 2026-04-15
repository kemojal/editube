"""DNS TXT verification for workspace custom domains (Editube token at _editube-verify.<domain>)."""

from __future__ import annotations

import logging
from typing import Tuple

import dns.exception
import dns.resolver

log = logging.getLogger(__name__)

# Relative name under the workspace custom_domain (FQDN = f"{TXT_HOST_LABEL}.{custom_domain}")
TXT_HOST_LABEL = "_editube-verify"


def txt_verification_fqdn(custom_domain: str) -> str:
    d = (custom_domain or "").strip().lower().rstrip(".")
    return f"{TXT_HOST_LABEL}.{d}"


def _txt_rdata_as_string(rdata) -> str:
    parts: list[str] = []
    for s in rdata.strings:
        if isinstance(s, bytes):
            parts.append(s.decode("utf-8", errors="replace"))
        elif isinstance(s, memoryview):
            parts.append(s.tobytes().decode("utf-8", errors="replace"))
        else:
            parts.append(str(s))
    return "".join(parts).strip().strip('"')


def verify_editube_domain_txt(custom_domain: str, expected_token: str) -> Tuple[bool, str]:
    """
    Look up TXT at ``_editube-verify.<custom_domain>`` and check that one record equals ``expected_token``.

    Returns (success, human_message).
    """
    domain = (custom_domain or "").strip().lower().rstrip(".")
    token = (expected_token or "").strip()
    if not domain:
        return False, "Set a custom domain on this workspace before verifying DNS."
    if not token:
        return False, "Generate a verification token first, then add the TXT record at your DNS provider."

    name = txt_verification_fqdn(domain)
    res = dns.resolver.Resolver()
    res.lifetime = 15.0
    res.timeout = 5.0

    try:
        answers = res.resolve(name, "TXT")
    except dns.resolver.NXDOMAIN:
        return (
            False,
            f"No DNS name exists for {name}. Create a TXT record with host {TXT_HOST_LABEL} "
            f"under {domain} (full name {name}) and wait for propagation.",
        )
    except dns.resolver.NoAnswer:
        return (
            False,
            f"No TXT records at {name}. Add a TXT whose value is exactly your verification token.",
        )
    except dns.resolver.LifetimeTimeout:
        return False, "DNS lookup timed out. Try again after your DNS changes propagate (often a few minutes)."
    except dns.resolver.Timeout:
        return False, "DNS resolver timed out. Try again in a few minutes."
    except dns.exception.DNSException as e:
        log.warning("DNS verify failed for %s: %s", name, e)
        return False, f"DNS lookup failed: {e}"

    found_values: list[str] = []
    for rdata in answers:
        try:
            val = _txt_rdata_as_string(rdata)
        except (AttributeError, TypeError, ValueError) as e:
            log.debug("Skip TXT rdata parse: %s", e)
            continue
        found_values.append(val)
        if val == token:
            return True, "DNS TXT verification succeeded."

    if not found_values:
        return (
            False,
            f"TXT lookup at {name} returned no readable values. Check your DNS provider’s TXT format.",
        )

    preview = "; ".join(repr(v[:64] + ("…" if len(v) > 64 else "")) for v in found_values[:4])
    if len(found_values) > 4:
        preview += f" … (+{len(found_values) - 4} more)"
    return (
        False,
        f"No TXT value matched your verification token at {name}. Found: {preview}",
    )
