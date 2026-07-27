#!/usr/bin/env python3
"""
Credential store for Shopify Crawler Access signatures (web-bot-auth).

Shopify issues these in the merchant admin under
Online Store > Preferences > Crawler access. The merchant copies three header
values; they are bound to one authority (host) and expire after ~90 days.

Store location (never inside the repo):
    $NORDALUX_SHOPIFY_CREDS  or  ~/.nordalux/shopify-crawler-access.json

Usage:
    python shopify_auth.py add --authority shop.de --input-file headers.txt
    python shopify_auth.py list
    python shopify_auth.py check https://shop.de/collections/all
    python shopify_auth.py headers https://shop.de/products/x
    python shopify_auth.py remove --authority shop.de
"""

import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seo_env  # noqa: E402

STORE_ENV = "NORDALUX_SHOPIFY_CREDS"
DEFAULT_STORE = Path.home() / ".nordalux" / "shopify-crawler-access.json"
REQUIRED_TAG = "web-bot-auth"
EXPIRY_WARN_DAYS = 14


def store_path() -> Path:
    return Path(os.environ.get(STORE_ENV) or DEFAULT_STORE)


def load_store() -> dict:
    path = store_path()
    if not path.exists():
        return {"version": 1, "authorities": {}}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("authorities", {})
    return data


def save_store(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def normalize_authority(value: str) -> str:
    """Accept a bare host or a full URL, return the authority (host[:port])."""
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        return (parsed.netloc or "").lower()
    return value.split("/")[0].lower()


def parse_signature_input(raw: str) -> dict:
    """Extract metadata from a Signature-Input header value.

    Example:
        sig1=("@authority" "signature-agent");keyid="...";nonce="...";
        tag="web-bot-auth";created=1784624987;expires=1792400987
    """
    meta = {"label": None, "covered": [], "keyid": None, "tag": None,
            "created": None, "expires": None}

    label = re.match(r"\s*([A-Za-z0-9_-]+)\s*=", raw)
    if label:
        meta["label"] = label.group(1)

    covered = re.search(r"=\s*\(([^)]*)\)", raw)
    if covered:
        meta["covered"] = re.findall(r'"([^"]+)"', covered.group(1))

    for key in ("keyid", "tag"):
        found = re.search(rf'{key}="([^"]*)"', raw)
        if found:
            meta[key] = found.group(1)

    for key in ("created", "expires"):
        found = re.search(rf"{key}=(\d+)", raw)
        if found:
            meta[key] = int(found.group(1))

    return meta


def validate(meta: dict) -> list:
    problems = []
    if meta["tag"] != REQUIRED_TAG:
        problems.append(f'tag is {meta["tag"]!r}, expected "{REQUIRED_TAG}"')
    if "@authority" not in meta["covered"]:
        problems.append('covered components do not include "@authority"')
    if not meta["keyid"]:
        problems.append("no keyid in signature-input")
    if not meta["expires"]:
        problems.append("no expires in signature-input")
    return problems


def parse_header_file(path: Path) -> dict:
    """Read a pasted block of the three headers, in any order.

    Accepts `Header: value` lines and tolerates the German admin labels.
    """
    wanted = {
        "signature-input": "signature_input",
        "signature": "signature",
        "signature-agent": "signature_agent",
        "signatur-input": "signature_input",
        "signatur": "signature",
        "signatur-agent": "signature_agent",
    }
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        key = wanted.get(name.strip().lower())
        if key and key not in out:
            out[key] = value.strip()
    return out


def days_left(expires) -> float:
    if not expires:
        return float("nan")
    return (expires - time.time()) / 86400.0


def cmd_add(args) -> int:
    values = {}
    if args.input_file:
        values = parse_header_file(Path(args.input_file))
    if args.signature_input:
        values["signature_input"] = args.signature_input
    if args.signature:
        values["signature"] = args.signature
    if args.signature_agent:
        values["signature_agent"] = args.signature_agent
    values.setdefault("signature_agent", '"https://shopify.com"')

    missing = [k for k in ("signature_input", "signature") if not values.get(k)]
    if missing:
        print(f"Error: missing {', '.join(missing)}", file=sys.stderr)
        return 2

    meta = parse_signature_input(values["signature_input"])
    problems = validate(meta)
    if problems and not args.force:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        print("Refusing to store. Re-copy from the Shopify admin, or use --force.",
              file=sys.stderr)
        return 2

    authority = normalize_authority(args.authority)
    store = load_store()
    store["authorities"][authority] = {
        "signature_agent": values["signature_agent"],
        "signature_input": values["signature_input"],
        "signature": values["signature"],
        "keyid": meta["keyid"],
        "created": meta["created"],
        "expires": meta["expires"],
        "label": args.label or "",
        "added_at": int(time.time()),
    }
    save_store(store)

    left = days_left(meta["expires"])
    if left <= 0:
        print(f"Stored signature for {authority}, but it EXPIRED {abs(left):.0f} days "
              f"ago and will not be used. Issue a new one in the Shopify admin.",
              file=sys.stderr)
    else:
        print(f"Stored signature for {authority} ({left:.0f} days left) in {store_path()}")
    if meta["covered"] and set(meta["covered"]) - {"@authority", "signature-agent"}:
        print(f"Note: covered components are {meta['covered']} — the crawler only "
              f"replays static headers, so anything beyond @authority and "
              f"signature-agent will not verify.", file=sys.stderr)
    return 0


def describe(authority: str, entry: dict, origin: str) -> str:
    left = days_left(entry.get("expires"))
    state = "EXPIRED" if left <= 0 else ("expiring" if left < EXPIRY_WARN_DAYS else "ok")
    label = f" — {entry['label']}" if entry.get("label") else ""
    return f"{authority:38s} {origin:6s} {state:8s} {left:6.0f}d{label}"


def cmd_list(args) -> int:
    env = seo_env.load()
    store = load_store()

    if env["path"]:
        print(f"Project file: {env['path']}")
        warning = seo_env.gitignore_warning(env["path"])
        if warning:
            print(warning, file=sys.stderr)
        for authority, entry in sorted(env["authorities"].items()):
            meta = parse_signature_input(entry["signature_input"])
            print(describe(authority, {"expires": meta["expires"]}, "env"))
        for problem in env["problems"]:
            print(f"  problem: {problem}", file=sys.stderr)
        if env["crawl"]:
            print(f"  crawl settings: {json.dumps(env['crawl'])}")

    if store["authorities"]:
        print(f"Global store: {store_path()}")
        for authority, entry in sorted(store["authorities"].items()):
            shadowed = " (shadowed by project file)" if authority in env["authorities"] else ""
            print(describe(authority, entry, "store") + shadowed)
    elif not env["path"]:
        print(f"No signatures found. Create a .claude-seo-env here or use `add`.")
    return 0


def lookup(url_or_host: str, start=None) -> tuple:
    """Resolve a signature for one authority.

    A project-local `.claude-seo-env` wins over the global store, so an audit
    folder can carry its own client credentials.
    """
    authority = normalize_authority(url_or_host)

    env = seo_env.load(start)
    entry = env["authorities"].get(authority)
    if entry:
        meta = parse_signature_input(entry["signature_input"])
        return authority, {
            "signature_agent": entry.get("signature_agent", '"https://shopify.com"'),
            "signature_input": entry["signature_input"],
            "signature": entry["signature"],
            "keyid": meta["keyid"],
            "created": meta["created"],
            "expires": meta["expires"],
            "label": f"from {entry.get('source', env['path'])}",
            "crawl": entry.get("crawl", {}),
            "origin": "env",
        }

    store = load_store()
    entry = store["authorities"].get(authority)
    if entry:
        entry = dict(entry, origin="store")
    return authority, entry


def cmd_check(args) -> int:
    authority, entry = lookup(args.url)
    if not entry:
        print(f"No signature for {authority}")
        return 3
    left = days_left(entry.get("expires"))
    if left <= 0:
        print(f"{authority}: EXPIRED {abs(left):.0f} days ago — regenerate in the "
              f"Shopify admin (Online Store > Preferences > Crawler access)")
        return 4
    print(f"{authority}: valid, {left:.0f} days left "
          f"(source: {entry.get('label') or entry.get('origin')})")
    return 0


def cmd_headers(args) -> int:
    """Emit the headers as JSON for a crawler to merge in. Empty dict if none."""
    authority, entry = lookup(args.url)
    if not entry or days_left(entry.get("expires")) <= 0:
        print("{}")
        return 3
    print(json.dumps({
        "Signature-Agent": entry["signature_agent"],
        "Signature-Input": entry["signature_input"],
        "Signature": entry["signature"],
    }))
    return 0


def cmd_remove(args) -> int:
    authority = normalize_authority(args.authority)
    store = load_store()
    if store["authorities"].pop(authority, None) is None:
        print(f"No entry for {authority}", file=sys.stderr)
        return 3
    save_store(store)
    print(f"Removed {authority}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="store a signature for one authority")
    add.add_argument("--authority", required=True, help="host the signature is bound to")
    add.add_argument("--input-file", help="file with the three pasted header lines")
    add.add_argument("--signature-input")
    add.add_argument("--signature")
    add.add_argument("--signature-agent")
    add.add_argument("--label", help="free note, e.g. client name and issue date")
    add.add_argument("--force", action="store_true", help="store despite validation problems")
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="list stored signatures and expiry")
    lst.set_defaults(func=cmd_list)

    chk = sub.add_parser("check", help="check whether a URL is covered")
    chk.add_argument("url")
    chk.set_defaults(func=cmd_check)

    hdr = sub.add_parser("headers", help="print headers as JSON for a URL")
    hdr.add_argument("url")
    hdr.set_defaults(func=cmd_headers)

    rm = sub.add_parser("remove", help="delete a stored signature")
    rm.add_argument("--authority", required=True)
    rm.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
