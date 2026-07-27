#!/usr/bin/env python3
"""
Decide how a Shopify SEO audit should run. Machine-readable, never fatal.

Prints one JSON object and always exits 0, so the caller never has to interpret
an error. Two possible modes:

    signed    -> run the uncapped signed crawl, then audit its artifacts
    fallback  -> run the plain upstream seo-audit, unchanged, cap included

Usage:
    python audit_precheck.py https://shop.de
"""

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SKILL_DIR = Path(__file__).resolve().parent.parent


def decide(url: str) -> dict:
    result = {
        "mode": "fallback",
        "url": url,
        "authority": None,
        "reason": None,
        "detail": None,
        "command": None,
    }

    if importlib.util.find_spec("requests") is None:
        result["reason"] = "requests_missing"
        result["detail"] = ("The requests library is not installed, so the signed crawler "
                            "cannot run. Install it with: pip install requests")
        return result

    try:
        from shopify_auth import days_left, lookup, normalize_authority
    except Exception as exc:  # noqa: BLE001 - any import problem means fallback
        result["reason"] = "crawler_unavailable"
        result["detail"] = f"Crawler scripts could not be loaded: {exc}"
        return result

    authority = normalize_authority(url)
    result["authority"] = authority

    try:
        _, entry = lookup(url)
    except Exception as exc:  # noqa: BLE001 - a broken env file must not abort an audit
        result["reason"] = "credential_store_unreadable"
        result["detail"] = f"Could not read the credential source: {exc}"
        return result

    if not entry:
        result["reason"] = "no_signature"
        result["detail"] = (
            f"No Crawler Access signature for {authority}. Note that a signature is bound "
            f"to one host — if the store is served on the other of apex/www, check that "
            f"host instead.")
        return result

    left = days_left(entry.get("expires"))
    if left <= 0:
        result["reason"] = "signature_expired"
        result["detail"] = (
            f"The signature for {authority} expired {abs(left):.0f} days ago. Issue a new "
            f"one in the Shopify admin under Online Store > Preferences > Crawler access. "
            f"Until then the audit runs capped.")
        return result

    result["mode"] = "signed"
    result["reason"] = "signature_valid"
    result["days_left"] = round(left)
    result["source"] = entry.get("origin", "store")
    result["detail"] = (f"Signature for {authority} is valid for {left:.0f} more days. "
                        f"Crawl the complete sitemap with no page cap.")
    result["command"] = (f'python "{SKILL_DIR}/scripts/shopify_crawl.py" {url} '
                         f'--out ./crawl')
    return result


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"mode": "fallback", "reason": "no_url",
                          "detail": "Usage: audit_precheck.py <url>"}))
        return 0
    print(json.dumps(decide(sys.argv[1]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
