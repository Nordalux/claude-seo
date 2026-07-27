---
name: seo-audit-shopify
description: "Full SEO audit of a Shopify store using the store's Crawler Access signature: signed, sitemap-complete crawl with no page cap, then the standard seo-audit analysis on the crawl artifacts. Use for 'Shopify Audit', 'kompletten Shop auditieren', 'seo-audit ohne 500-Seiten-Limit', or any seo-audit of a host listed in .claude-seo-env."
user-invokable: true
argument-hint: "[shop-url]"
metadata:
  author: Nordalux
  version: "1.0.0"
  internal: true
---

# Shopify SEO Audit (signed, uncapped)

One command instead of crawling and auditing separately. This replaces **step 3 only** of
the `seo-audit` skill — business-type detection, subagent delegation, scoring and report
generation stay exactly as upstream defines them.

Credentials, crawl settings and the reasoning behind them: `shopify-crawler-access`.

## Step 1 — Decide the mode

```bash
python .claude/skills/shopify-crawler-access/scripts/audit_precheck.py <url>
```

Always exits 0 and always prints JSON. There is no error case to handle — read `mode`:

- `"signed"` → continue with step 2.
- `"fallback"` → **run the plain `seo-audit` skill, completely unchanged**, 500-page cap
  and link-following crawl included. Apply nothing else from this skill. Tell the user one
  sentence about why (the `detail` field says it), then get on with the audit. A capped
  audit is a normal result, not a failure.

Fallback reasons, all handled the same way: `no_signature`, `signature_expired`,
`credential_store_unreadable`, `requests_missing`, `crawler_unavailable`.

One thing worth checking before accepting `no_signature`: the signature is bound to one
host, so `shop.de` and `www.shop.de` are different authorities. If the store is served on
the other form, re-run the precheck against that one.

## Step 2 — Crawl

```bash
python .claude/skills/shopify-crawler-access/scripts/shopify_crawl.py <url> --out ./crawl
```

No `--max-pages`. The crawl covers the complete sitemap; a cap only applies if
`.claude-seo-env` sets one, and then the truncation goes into the report. Report the real
number of pages crawled in the executive summary — never round it to a cap.

Exit codes:

- `0` → crawl complete, continue.
- `5` → crawl **also complete**, artifacts are written and usable, but the storefront rate
  limited a signed request. Shopify treats that as an invalid signature. Continue the audit
  on the artifacts and flag in the report that the signature should be reissued.
- `1` → nothing was crawled (no sitemap reachable, store password-protected, host wrong).
  Fall back to the plain `seo-audit` skill unchanged and say why.

Large stores: the crawl is resumable (`--resume`). If it is interrupted, resume it instead
of auditing a partial crawl.

## Step 3 — Audit on the crawl artifacts

Now follow the `seo-audit` skill from its step 2 onwards, with one substitution: the crawl
is already done, so nobody crawls again.

Business type is settled — Shopify means E-commerce, so `seo-ecommerce` is in scope.

What each subagent gets:

- `crawl/summary.json` — aggregate counts across **all** crawled pages. This is the
  evidence base for anything quantitative: status distribution, per-template counts,
  missing and duplicate titles, meta description lengths, missing canonicals, noindex,
  h1 problems, thin pages, images without alt, schema coverage, latency percentiles.
- `crawl/sample.json` — a stratified sample, N pages per template type, for the agents that
  need real page content: `seo-content`, `seo-schema`, `seo-sxo`, `seo-ecommerce`.
- `crawl/pages.jsonl` — **never** passed to a subagent. It is the raw record set and will
  blow up the context on any store worth crawling uncapped. Query it with a script if a
  specific number is missing from `summary.json`.

Say in each subagent prompt that the pages were already fetched, so they analyze the
artifacts instead of re-fetching the site. Agents that genuinely need live requests
(`seo-performance` for CWV, `seo-visual` for screenshots, `seo-google` for API data) do
their own thing as usual — those are a handful of requests and run unsigned.

## Step 4 — Score and report

Unchanged from `seo-audit`: the same scoring weights, the same report structure,
`FULL-AUDIT-REPORT.md` and `ACTION-PLAN.md`.

Two additions to the executive summary, because they change how the numbers should be
read:

- Pages crawled, and that the crawl was signed and complete — an uncapped Shopify crawl is
  a different claim than "500 pages sampled".
- Any truncation, with what was left out.
