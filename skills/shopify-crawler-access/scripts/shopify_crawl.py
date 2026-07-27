#!/usr/bin/env python3
"""
Sitemap-driven Shopify crawler with Crawler Access signature support.

Replaces the 500-page link-following crawl for Shopify stores:
 - URLs come from sitemap.xml (Shopify always publishes a complete, nested one),
   so there is no link-graph BFS and no reason for a page cap.
 - If a Crawler Access signature is stored for the target authority
   (see shopify_auth.py), its three headers are attached to every request,
   which is what lifts the storefront rate limit.
 - Per-page results are written as JSONL plus an aggregate summary and a
   stratified sample, so an unlimited crawl still hands a small artifact to
   the analysis subagents.

Usage:
    python shopify_crawl.py https://shop.de --out ./crawl
    python shopify_crawl.py https://shop.de --max-pages 2000 --concurrency 4
    python shopify_crawl.py https://shop.de --resume --sample-per-template 8

Exit codes: 0 ok, 1 fatal, 5 rate limited beyond recovery.
"""

import argparse
import concurrent.futures as futures
import json
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests",
          file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seo_env  # noqa: E402
from shopify_auth import lookup, days_left  # noqa: E402

USER_AGENT = ("NordaluxSEOCrawler/1.0 "
              "(+https://www.nordalux.de/tools/ai-check; seo-audit)")
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# Shopify storefront URL shapes, in match order. Everything else is "page".
TEMPLATE_PATTERNS = [
    ("product", re.compile(r"/products/")),
    ("collection", re.compile(r"/collections/[^/]+/?$")),
    ("collection_filtered", re.compile(r"/collections/.+\?")),
    ("blog_article", re.compile(r"/blogs/[^/]+/[^/]+")),
    ("blog_index", re.compile(r"/blogs/[^/]+/?$")),
    ("policy", re.compile(r"/policies/")),
    ("account", re.compile(r"/account")),
    ("home", re.compile(r"^https?://[^/]+/?$")),
]


class RateLimitGovernor:
    """Grows the per-request delay when the storefront pushes back, and shrinks
    it again after successful requests. Concurrency stays fixed for the run —
    throttling happens through the delay alone."""

    def __init__(self, delay: float):
        self.lock = threading.Lock()
        self.base_delay = delay
        self.delay = delay
        self.hits = 0
        self.consecutive = 0

    def penalize(self) -> float:
        with self.lock:
            self.hits += 1
            self.consecutive += 1
            self.delay = min(self.delay * 2 if self.delay else 1.0, 30.0)
            return self.delay * (1 + random.random())

    def reward(self) -> None:
        with self.lock:
            self.consecutive = 0
            if self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay / 1.5)

    def wait(self) -> None:
        if self.delay:
            time.sleep(self.delay)


def classify(url: str) -> str:
    for name, pattern in TEMPLATE_PATTERNS:
        if pattern.search(url):
            return name
    return "page"


def headers_for(url: str, authority: str, sig_headers) -> dict:
    """Base headers, plus the signature only when the target host is the one the
    signature was issued for. Sitemaps and redirects point at other hosts
    (cdn.shopify.com among them); a client credential must never follow."""
    headers = dict(BASE_HEADERS)
    if sig_headers and urlparse(url).netloc.lower() == authority:
        headers.update(sig_headers)
    return headers


def fetch_sitemap_urls(session, root: str, governor, authority="", sig_headers=None,
                       verbose=True, target=0) -> list:
    """Walk sitemap.xml / sitemapindex recursively. Returns unique URLs.

    `target` > 0 stops the walk once that many URLs are collected, so a capped
    crawl does not pull every child sitemap of a large store.
    """
    seen_maps, queue, urls = set(), [], []

    robots = f"{root}/robots.txt"
    try:
        text = session.get(robots, timeout=15,
                           headers=headers_for(robots, authority, sig_headers)).text
        queue += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
    except requests.RequestException:
        pass
    queue.append(f"{root}/sitemap.xml")

    while queue:
        if target and len(urls) >= target:
            if verbose:
                print(f"  stopping sitemap walk at {len(urls)} URLs "
                      f"({len(queue)} sitemaps not opened)", file=sys.stderr)
            break
        current = queue.pop(0)
        if current in seen_maps:
            continue
        seen_maps.add(current)
        try:
            response = session.get(current, timeout=30,
                                   headers=headers_for(current, authority, sig_headers))
        except requests.RequestException as exc:
            if verbose:
                print(f"  sitemap failed {current}: {exc}", file=sys.stderr)
            continue
        if response.status_code == 429:
            governor.penalize()
            queue.append(current)
            governor.wait()
            continue
        if response.status_code != 200 or not response.content.strip():
            continue
        try:
            tree = ET.fromstring(response.content)
        except ET.ParseError:
            continue
        tag = tree.tag.split("}")[-1]
        locs = [el.text.strip() for el in tree.iter()
                if el.tag.split("}")[-1] == "loc" and el.text]
        if tag == "sitemapindex":
            queue += locs
        else:
            urls += locs
        if verbose:
            print(f"  {current}: {len(locs)} entries ({tag})", file=sys.stderr)

    return list(dict.fromkeys(urls))


TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
STRIP_RE = re.compile(r"<[^>]+>")


def analyze_html(html: str) -> dict:
    """Regex-level extraction. Deliberately dependency-free; good enough for
    aggregate signals, not a substitute for per-page DOM analysis."""
    def collapse(value):
        # Shopify themes wrap long titles across lines; keep one clean string.
        return re.sub(r"\s+", " ", unescape(value)).strip()

    def first(pattern, flags=re.I | re.S):
        found = re.search(pattern, html, flags)
        return collapse(found.group(1)) if found else None

    title = first(r"<title[^>]*>(.*?)</title>")
    description = first(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']')
    if not description:
        description = first(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']')
    canonical = first(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](.*?)["\']')
    robots_meta = first(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\'](.*?)["\']')

    h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    hreflang = re.findall(r'<link[^>]+rel=["\']alternate["\'][^>]+hreflang=["\']([^"\']+)', html, re.I)

    schema_types = []
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                            html, re.I | re.S):
        schema_types += re.findall(r'"@type"\s*:\s*"([^"]+)"', block)

    images = re.findall(r"<img\b[^>]*>", html, re.I)
    without_alt = [img for img in images if not re.search(r'\balt\s*=', img, re.I)]

    text = STRIP_RE.sub(" ", TAG_RE.sub(" ", html))
    words = len(text.split())

    return {
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": description,
        "meta_description_length": len(description) if description else 0,
        "canonical": canonical,
        "robots_meta": robots_meta,
        "h1_count": len(h1s),
        "h1_first": collapse(STRIP_RE.sub("", h1s[0]))[:200] if h1s else None,
        "hreflang": sorted(set(hreflang)),
        "schema_types": sorted(set(schema_types)),
        "image_count": len(images),
        "images_without_alt": len(without_alt),
        "word_count": words,
    }


def crawl_one(session, url, authority, sig_headers, governor, timeout, save_html_dir):
    headers = headers_for(url, authority, sig_headers)
    record = {"url": url, "template": classify(url), "signed": bool(sig_headers)}
    for attempt in range(4):
        governor.wait()
        started = time.time()
        try:
            response = session.get(url, headers=headers, timeout=timeout,
                                   allow_redirects=True)
        except requests.RequestException as exc:
            record["error"] = str(exc)
            time.sleep(1 + attempt)
            continue

        record["status"] = response.status_code
        record["elapsed_ms"] = int((time.time() - started) * 1000)
        record["final_url"] = response.url
        record["redirected"] = response.url.rstrip("/") != url.rstrip("/")

        if response.status_code in (429, 430, 503):
            record["rate_limited"] = True
            time.sleep(governor.penalize())
            continue

        governor.reward()
        record.pop("error", None)
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        record["content_type"] = content_type
        record["html"] = response.status_code == 200 and "html" in content_type
        if record["html"]:
            record.update(analyze_html(response.text))
            if save_html_dir:
                name = re.sub(r"[^A-Za-z0-9]+", "_", urlparse(url).path)[:120] or "index"
                (save_html_dir / f"{name}.html").write_text(response.text, encoding="utf-8")
        return record

    record["failed"] = True
    return record


def summarize(records: list) -> dict:
    by_template = Counter(r["template"] for r in records)
    statuses = Counter(r.get("status") for r in records)
    ok = [r for r in records if r.get("status") == 200]
    # Issue counts only make sense for HTML documents. Sitemaps list agents.md,
    # CDN assets and feeds too; counting those as "missing title" is noise.
    html_pages = [r for r in ok if r.get("html")]
    titles = Counter(r.get("title") for r in html_pages if r.get("title"))

    def count(predicate):
        return sum(1 for r in html_pages if predicate(r))

    schema_coverage = defaultdict(int)
    for record in html_pages:
        for schema_type in record.get("schema_types", []):
            schema_coverage[schema_type] += 1

    latencies = sorted(r["elapsed_ms"] for r in ok if r.get("elapsed_ms"))
    return {
        "pages_crawled": len(records),
        "pages_ok": len(ok),
        "html_documents": len(html_pages),
        "non_html_resources": len(ok) - len(html_pages),
        "content_types": {str(k): v for k, v in
                          Counter(r.get("content_type") for r in ok).most_common()},
        "status_distribution": {str(k): v for k, v in statuses.most_common()},
        "template_distribution": dict(by_template.most_common()),
        "rate_limited_requests": count(lambda r: r.get("rate_limited")),
        "issues": {
            "missing_title": count(lambda r: not r.get("title")),
            "title_over_60": count(lambda r: r.get("title_length", 0) > 60),
            "missing_meta_description": count(lambda r: not r.get("meta_description")),
            "meta_description_out_of_range": count(
                lambda r: r.get("meta_description") and
                not 150 <= r.get("meta_description_length", 0) <= 160),
            "duplicate_titles": sum(c for c in titles.values() if c > 1),
            "missing_canonical": count(lambda r: not r.get("canonical")),
            "noindex": count(lambda r: "noindex" in (r.get("robots_meta") or "")),
            "h1_missing": count(lambda r: r.get("h1_count") == 0),
            "h1_multiple": count(lambda r: r.get("h1_count", 0) > 1),
            "thin_content_under_250_words": count(lambda r: r.get("word_count", 0) < 250),
            "images_without_alt": sum(r.get("images_without_alt", 0) for r in ok),
        },
        "schema_coverage": dict(sorted(schema_coverage.items(), key=lambda kv: -kv[1])),
        "latency_ms": {
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "p95": latencies[int(len(latencies) * 0.95)] if latencies else None,
        },
        "duplicate_title_examples": [t for t, c in titles.most_common(10) if c > 1],
    }


def stratified_sample(records: list, per_template: int) -> list:
    buckets = defaultdict(list)
    for record in records:
        buckets[record["template"]].append(record)
    sample = []
    for template, items in buckets.items():
        ok = [r for r in items if r.get("html")] or items
        step = max(1, len(ok) // per_template)
        sample += ok[::step][:per_template]
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description="Sitemap-driven Shopify crawler")
    parser.add_argument("url", help="storefront root, e.g. https://shop.de")
    parser.add_argument("--out", default="./crawl", help="output directory")
    # Defaults stay None so .claude-seo-env settings can fill them in; anything
    # passed on the command line still wins.
    parser.add_argument("--max-pages", type=int,
                        help="0 = no limit (default). Set a number to cap.")
    parser.add_argument("--concurrency", type=int,
                        help="default 6 signed, 2 unsigned")
    parser.add_argument("--delay", type=float,
                        help="base delay per request, grows on 429")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--sample-per-template", type=int)
    parser.add_argument("--include", help="regex; only crawl matching URLs")
    parser.add_argument("--exclude", help="regex; skip matching URLs")
    parser.add_argument("--save-html", action="store_true", default=None)
    parser.add_argument("--resume", action="store_true",
                        help="skip URLs already present in pages.jsonl")
    parser.add_argument("--ignore-robots", action="store_true", default=None)
    parser.add_argument("--no-signature", action="store_true",
                        help="crawl unsigned even if a signature is stored")
    args = parser.parse_args()

    parsed = urlparse(args.url if "://" in args.url else f"https://{args.url}")
    root = f"{parsed.scheme}://{parsed.netloc}"
    authority = parsed.netloc.lower()

    env = seo_env.load()
    if env["path"]:
        print(f"Using crawl settings from {env['path']}", file=sys.stderr)
        warning = seo_env.gitignore_warning(env["path"])
        if warning:
            print(warning, file=sys.stderr)
        for problem in env["problems"]:
            print(f"  {problem}", file=sys.stderr)

    sig_headers = None
    entry = None
    if not args.no_signature:
        _, entry = lookup(root)
        if entry and days_left(entry.get("expires")) > 0:
            sig_headers = {
                "Signature-Agent": entry["signature_agent"],
                "Signature-Input": entry["signature_input"],
                "Signature": entry["signature"],
            }
            print(f"Crawler Access signature active for {authority} "
                  f"({days_left(entry['expires']):.0f} days left, "
                  f"{entry.get('origin', 'store')})", file=sys.stderr)
        elif entry:
            print(f"WARNING: signature for {authority} is EXPIRED — crawling unsigned "
                  f"at normal rate limits.", file=sys.stderr)
        else:
            print(f"No signature for {authority} — crawling unsigned.", file=sys.stderr)

    # CLI beats the domain block, which beats the global keys, which beat these.
    signed = bool(sig_headers)
    builtin = {
        "max_pages": 0,                       # no cap, ever, unless asked for one
        "concurrency": 6 if signed else 2,
        "delay": 0.2 if signed else 1.0,
        "timeout": 30,
        "sample_per_template": 6,
        "include": None,
        "exclude": None,
        "save_html": False,
        "ignore_robots": False,
    }
    layers = [builtin, env["crawl"], (entry or {}).get("crawl", {})]
    for field, fallback in builtin.items():
        if getattr(args, field, None) is None:
            value = fallback
            for layer in layers:
                if layer.get(field) is not None:
                    value = layer[field]
            setattr(args, field, value)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_file = out_dir / "pages.jsonl"
    html_dir = None
    if args.save_html:
        html_dir = out_dir / "html"
        html_dir.mkdir(exist_ok=True)

    governor = RateLimitGovernor(args.delay)
    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    print(f"Discovering URLs from sitemaps of {root} ...", file=sys.stderr)
    urls = fetch_sitemap_urls(session, root, governor, authority=authority,
                              sig_headers=sig_headers, target=args.max_pages)
    if not urls:
        print("Error: no sitemap URLs found. Shopify storefronts always expose "
              "/sitemap.xml — check the domain or a password-protected store.",
              file=sys.stderr)
        return 1

    if args.include:
        pattern = re.compile(args.include)
        urls = [u for u in urls if pattern.search(u)]
    if args.exclude:
        pattern = re.compile(args.exclude)
        urls = [u for u in urls if not pattern.search(u)]

    if not args.ignore_robots:
        parser_robots = RobotFileParser()
        parser_robots.set_url(f"{root}/robots.txt")
        try:
            parser_robots.read()
            urls = [u for u in urls if parser_robots.can_fetch(USER_AGENT, u)]
        except Exception:
            pass

    done = set()
    if args.resume and pages_file.exists():
        for line in pages_file.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["url"])
            except (json.JSONDecodeError, KeyError):
                continue
        urls = [u for u in urls if u not in done]
        print(f"Resuming: {len(done)} already crawled, {len(urls)} left", file=sys.stderr)

    if args.max_pages and len(urls) > args.max_pages:
        print(f"Capping at --max-pages {args.max_pages} of {len(urls)} discovered URLs "
              f"— report this truncation in the audit.", file=sys.stderr)
        urls = urls[:args.max_pages]

    print(f"Crawling {len(urls)} URLs, concurrency {args.concurrency}, "
          f"delay {args.delay}s"
          + ("" if args.max_pages else " (no page cap)"), file=sys.stderr)
    progress_every = max(25, min(500, len(urls) // 50 or 25))
    started_at = time.time()
    records, write_lock = [], threading.Lock()
    mode = "a" if (args.resume and pages_file.exists()) else "w"

    with pages_file.open(mode, encoding="utf-8") as sink:
        with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            pending = {pool.submit(crawl_one, session, url, authority, sig_headers,
                                   governor, args.timeout, html_dir): url
                       for url in urls}
            for index, future in enumerate(futures.as_completed(pending), 1):
                record = future.result()
                with write_lock:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                records.append(record)
                if index % progress_every == 0 or index == len(urls):
                    done_ratio = index / len(urls)
                    elapsed = time.time() - started_at
                    eta = elapsed / done_ratio - elapsed
                    print(f"  {index}/{len(urls)} ({done_ratio:.0%}) "
                          f"ETA {eta / 60:.0f} min, delay {governor.delay:.2f}s, "
                          f"{governor.hits} rate-limit hits", file=sys.stderr)

    if args.resume and done:
        for line in pages_file.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record["url"] in done:
                records.append(record)

    summary = summarize(records)
    summary["authority"] = authority
    summary["signed"] = bool(sig_headers)
    summary["urls_discovered"] = len(urls) + len(done)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "sample.json").write_text(
        json.dumps(stratified_sample(records, args.sample_per_template),
                   indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone. {summary['pages_crawled']} pages -> {out_dir}", file=sys.stderr)
    print(json.dumps(summary["issues"], indent=2, ensure_ascii=False))

    if governor.hits and sig_headers:
        print("\nWARNING: rate limited despite a signature. Shopify treats that as an "
              "invalid signature — check expiry and that the authority matches.",
              file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
