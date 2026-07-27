#!/usr/bin/env python3
"""
Reader for the project-local `.claude-seo-env` file.

The file lives in the folder an audit is run from and holds one block per shop.
A new `Domain=` line starts a new block; keys before the first `Domain=` are
global crawl settings.

    # crawl settings (optional, apply to every domain below)
    Max-Pages   = 0
    Concurrency = 6

    Domain          = shop.de
    Signature-Input = sig1=("@authority" "signature-agent");keyid="...";...
    Signature       = sig1=:...:
    Signature-Agent = "https://shopify.com"      # optional, this is the default

    Domain          = anderer-shop.de
    Signature-Input = ...
    Signature       = ...

Lookup order: $CLAUDE_SEO_ENV, then `.claude-seo-env` in the current directory
and up to 5 parents, then the global store of shopify_auth.py.
"""

import os
import re
import subprocess
from pathlib import Path

ENV_FILENAME = ".claude-seo-env"
ENV_PATH_VAR = "CLAUDE_SEO_ENV"
DEFAULT_SIGNATURE_AGENT = '"https://shopify.com"'
PARENT_LEVELS = 5

# Keys kept byte-exact: they are header values, not shell values.
VERBATIM_KEYS = {"signature-input", "signature", "signature-agent"}
CRAWL_KEYS = {
    "max-pages": ("max_pages", int),
    "concurrency": ("concurrency", int),
    "delay": ("delay", float),
    "timeout": ("timeout", int),
    "sample-per-template": ("sample_per_template", int),
    "include": ("include", str),
    "exclude": ("exclude", str),
    "save-html": ("save_html", lambda v: str(v).strip().lower() in
                  ("1", "true", "yes", "ja", "on")),
    "ignore-robots": ("ignore_robots", lambda v: str(v).strip().lower() in
                      ("1", "true", "yes", "ja", "on")),
}


def normalize_key(raw: str) -> str:
    return raw.strip().lower().replace("_", "-")


def normalize_authority(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/")[0].lower()


def normalize_signature_agent(value: str) -> str:
    """Shopify requires the value including its quotation marks."""
    value = value.strip()
    if not value:
        return DEFAULT_SIGNATURE_AGENT
    if not value.startswith('"'):
        value = f'"{value.strip(chr(39))}"'
    return value


def find_env_file(start: Path = None) -> Path:
    explicit = os.environ.get(ENV_PATH_VAR)
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None

    current = (start or Path.cwd()).resolve()
    for _ in range(PARENT_LEVELS + 1):
        candidate = current / ENV_FILENAME
        if candidate.exists():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def parse_env_file(path: Path) -> dict:
    result = {"path": str(path), "authorities": {}, "crawl": {}, "problems": []}
    block = None

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            # tolerate INI-style headers by treating them as a domain
            line = f"Domain={line[1:-1]}"
        if "=" not in line:
            result["problems"].append(f"line {lineno}: no '=' — ignored: {line[:60]}")
            continue

        key, _, value = line.partition("=")
        key = normalize_key(key)
        value = value.strip() if key in VERBATIM_KEYS else value.strip().strip('"').strip("'")

        if key == "domain":
            authority = normalize_authority(value)
            if not authority:
                result["problems"].append(f"line {lineno}: empty Domain")
                continue
            block = {"authority": authority, "signature_agent": DEFAULT_SIGNATURE_AGENT,
                     "source": f"{path}:{lineno}"}
            result["authorities"][authority] = block
            continue

        if key in ("signature-input", "signature", "signature-agent"):
            if block is None:
                result["problems"].append(
                    f"line {lineno}: {key} before any Domain= line — ignored")
                continue
            field = key.replace("-", "_")
            block[field] = (normalize_signature_agent(value)
                            if key == "signature-agent" else value)
            continue

        if key in CRAWL_KEYS:
            field, caster = CRAWL_KEYS[key]
            try:
                cast = caster(value)
            except (TypeError, ValueError):
                result["problems"].append(f"line {lineno}: {key}={value!r} not usable")
                continue
            target = block.setdefault("crawl", {}) if block else result["crawl"]
            target[field] = cast
            continue

        result["problems"].append(f"line {lineno}: unknown key {key!r} — ignored")

    for authority, entry in list(result["authorities"].items()):
        missing = [k for k in ("signature_input", "signature") if not entry.get(k)]
        if missing:
            result["problems"].append(
                f"{authority}: missing {', '.join(m.replace('_', '-') for m in missing)} "
                f"— domain skipped")
            result["authorities"].pop(authority)

    return result


def load(start: Path = None) -> dict:
    path = find_env_file(start)
    if not path:
        return {"path": None, "authorities": {}, "crawl": {}, "problems": []}
    return parse_env_file(path)


def gitignore_warning(path) -> str:
    """Return a warning if the env file sits in a repo and is not ignored."""
    if not path:
        return ""
    path = Path(path)
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=str(path.parent), capture_output=True, timeout=10)
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path.parent), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if inside.returncode != 0 or b"true" not in inside.stdout:
        return ""
    if ignored.returncode == 0:
        return ""
    return (f"WARNING: {path.name} holds client crawl credentials and is NOT git-ignored "
            f"in {path.parent}. Add '{ENV_FILENAME}' to .gitignore before committing.")


if __name__ == "__main__":
    import json
    import sys

    data = load()
    if not data["path"]:
        print(f"No {ENV_FILENAME} found in the current directory or its parents.")
        sys.exit(3)
    print(f"Using {data['path']}")
    warning = gitignore_warning(data["path"])
    if warning:
        print(warning, file=sys.stderr)
    for authority, entry in data["authorities"].items():
        crawl = entry.get("crawl", {})
        print(f"  {authority:35s} signature ok"
              + (f"  crawl overrides: {json.dumps(crawl)}" if crawl else ""))
    if data["crawl"]:
        print(f"  global crawl settings: {json.dumps(data['crawl'])}")
    for problem in data["problems"]:
        print(f"  problem: {problem}", file=sys.stderr)
