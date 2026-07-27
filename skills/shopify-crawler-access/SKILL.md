---
name: shopify-crawler-access
description: "Crawl a Shopify storefront at full rate using the store's Crawler Access signature (web-bot-auth), with no 500-page cap. Use when auditing a Shopify shop where Nordalux or the client has admin access — triggers on 'Shopify Audit', 'Crawler Access', 'signature-agent', 'web-bot-auth', 'rate limit beim Crawlen', 'ganzen Shop crawlen'."
user-invokable: true
argument-hint: "[shop-url]"
metadata:
  author: Nordalux
  version: "1.0.0"
  internal: true
---

# Shopify Crawler Access

Nordalux-internal. Not upstreamed to `AgriciDaniel/claude-seo` — do not open an issue or PR
for anything in this directory.

## What this is

Shopify rate-limits storefront crawling. A merchant can mint a cryptographic crawl
credential in their own admin under **Online Store → Preferences → Crawler access**.
The admin outputs three header values. A crawler that replays them on every request is
treated as an authenticated crawler and is not rate limited.

The values are an HTTP Message Signature (RFC 9421, `tag="web-bot-auth"`):

```
Signature-Agent: "https://shopify.com"
Signature-Input: sig1=("@authority" "signature-agent");keyid="…";nonce="…";tag="web-bot-auth";created=…;expires=…
Signature: sig1=:…:
```

Only `Signature-Agent` is constant. `keyid`, `nonce`, `created` and `expires` change with
every signature the admin issues, and the signature covers `@authority` — it is bound to
one host and valid roughly 90 days.

## Hard limits — state these before promising a crawl

- **Only for shops we have admin access to.** The signature is issued inside the merchant's
  admin. There is no way to obtain one for a shop we do not control, so cold audits of
  prospects or competitors run unsigned at normal rate limits.
- **Never reuse a signature across shops.** It covers `@authority`; sending shop A's
  signature to shop B leaks a client credential and fails verification anyway.
- **One signature per host.** `shop.de`, `www.shop.de` and `shop.myshopify.com` are
  different authorities. Crawl the host the signature was issued for.
- **No checkout.** Shopify explicitly excludes Checkout from signature access.
- **Signatures cannot be extended.** When one expires, the merchant issues a new one.

## Workflow

### 1. Obtain the signature (client or Nordalux admin user)

Shopify admin → Online Store → Preferences → Crawler access → create a signature, then
copy all three header values. Paste them into a scratch file, one per line:

```
Signature-Agent: "https://shopify.com"
Signature-Input: sig1=("@authority" "signature-agent");keyid="…";…
Signature: sig1=:…:
```

### 2. Put it in `.claude-seo-env`

Create `.claude-seo-env` in the folder the audit runs from. One block per shop; a new
`Domain=` line starts the next block. Keys before the first `Domain=` are crawl settings
for every shop in the file. See `.claude-seo-env.example`.

```
Max-Pages   = 0
Concurrency = 6

Domain          = kundenshop.de
Signature-Input = sig1=("@authority" "signature-agent");keyid="…";tag="web-bot-auth";created=…;expires=…
Signature       = sig1=:…:

Domain          = zweiter-shop.de
Signature-Input = …
Signature       = …
Concurrency     = 3
```

`Signature-Agent` is optional and defaults to `"https://shopify.com"` — it never changes.
Recognised crawl keys: `Max-Pages` (0 = no limit), `Concurrency`, `Delay`, `Timeout`,
`Sample-Per-Template`, `Include`, `Exclude`, `Save-HTML`, `Ignore-Robots`. Inside a block
they override the global values for that shop only.

The file is searched in the current directory and up to five parents, so running from a
subfolder still finds it. `$CLAUDE_SEO_ENV` points at an explicit path instead.

**Never commit it.** It holds client credentials. The scripts check `git check-ignore` and
warn if the file sits in a repo without being ignored.

For credentials that should follow you across client folders there is still a global store
(`~/.nordalux/shopify-crawler-access.json`, override `$NORDALUX_SHOPIFY_CREDS`):

```bash
python …/shopify_auth.py add --authority shop.de --input-file headers.txt --label "Client X"
python …/shopify_auth.py list      # env file and global store, env wins
python …/shopify_auth.py check https://shop.de/products/x
python …/shopify_auth.py headers https://shop.de     # JSON, for other tooling
```

`.claude-seo-env` always beats the global store for the same authority. `add` rejects
values whose `tag` is not `web-bot-auth` or that do not cover `@authority`.

### 3. Crawl

```bash
python .claude/skills/shopify-crawler-access/scripts/shopify_crawl.py https://shop.de --out ./crawl
```

URLs come from `sitemap.xml` (Shopify publishes a complete nested one), not from link
following. The signature is attached automatically when one exists for the authority, and
is never sent to any other host — including after a cross-host redirect.

Defaults: no page limit, concurrency 6 and 0.2 s delay when signed, concurrency 2 and 1 s
when not, robots.txt respected. Precedence is CLI flag > domain block > global keys >
these defaults. On `429/430/503` it backs off exponentially and retries; if that happens *with* a
signature it exits 5, because Shopify's documented meaning of a rate-limit error on a
signed request is that the signature is not valid.

Useful flags: `--include`/`--exclude` (regex), `--resume`, `--save-html`,
`--sample-per-template N`, `--no-signature` to measure the unsigned baseline.

## Page limits — this is an extension, not a replacement

**No `.claude-seo-env` in the folder → change nothing.** `seo-audit` from the claude-seo
plugin runs exactly as upstream: link following, 500-page cap, its own defaults. Do not
apply anything from this skill.

**`.claude-seo-env` present → this skill takes over the crawl step** of `seo-audit` and the
500-page cap does not apply. Everything after the crawl (subagent delegation, scoring,
report) stays upstream's job.

| Setting | claude-seo default | With `.claude-seo-env` |
|---|---|---|
| Max pages | 500 | unlimited — `Max-Pages=0` is the default here |
| Discovery | link BFS | sitemap.xml (complete, no crawl-graph blind spots) |
| Concurrency | 5 | 6 signed / 2 unsigned, override per shop |
| Delay | 1 s | 0.2 s signed, 1 s unsigned, adaptive on 429 |

A cap is only ever applied when someone asks for one (`Max-Pages=N` or `--max-pages N`),
and the run then reports the truncation. Never stop at an arbitrary number and call the
audit complete.

Large shops: use `--resume` — `pages.jsonl` is flushed per page, so an interrupted 30k-page
crawl continues where it stopped instead of starting over.

The 500 cap exists partly to protect the model's context, and that reason does not go away
just because the crawl is faster. So never feed `pages.jsonl` to a subagent. The crawl
writes three artifacts:

- `pages.jsonl` — one record per URL. Machine input only. Sitemaps also list non-HTML
  resources (`agents.md`, CDN assets, feeds); those are recorded but excluded from issue
  counts, so "missing title" never counts an image.
- `summary.json` — aggregate counts (status distribution, per-template counts, missing
  titles, duplicate titles, thin pages, schema coverage, latency percentiles). This is what
  goes into the audit report.
- `sample.json` — a stratified sample, N pages per template type, for the subagents that
  need real page content (`seo-content`, `seo-schema`, `seo-sxo`).

Report the true crawl size in the audit, and report truncation explicitly whenever
`--max-pages` was set below the number of discovered URLs.

## Running a full audit

Use the `seo-audit-shopify` skill — `/seo-audit-shopify https://shop.de`. It resolves the
credential, runs the uncapped signed crawl and then hands the artifacts to the upstream
`seo-audit` pipeline. This skill here is the crawl layer it builds on.

Start the session in the folder holding `.claude-seo-env`; the hook resolves the file from
the working directory.

The audit's subagents fetch with their own fetchers and do not know about the signature.
That is a handful of unsigned requests per agent — the signed path covers the crawl step,
which is the only step where the page count matters.

## Discovery hook

`~/.claude/hooks/seo-env-notice.sh` runs on every prompt (global `UserPromptSubmit` hook in
`~/.claude/settings.json`). It looks for `.claude-seo-env` in the working folder and up to
five parents. Found: it injects the file path, the domains that actually carry a
`Signature=`, and the no-cap rule, so an audit started in any client folder picks this up
without the skill being loaded. Not found: it stays silent and nothing changes.

Edit the `SKILL_DIR` variable at the top of that script if this repo ever moves.

## Plugin boundary

`~/.claude/plugins/marketplaces/agricidaniel-seo/` is a clone of the upstream repo. Edits
there are overwritten on the next plugin update and would dirty the upstream diff. Keep all
Nordalux crawler-access behaviour in this skill.

## Live AI-Check tool (not yet wired)

`app/Services/AgenticAuditService.php` sends every request through one `fetch()` at
[AgenticAuditService.php:209](../../../app/Services/AgenticAuditService.php#L209), with headers
built at line 214. That is the single place to merge signature headers if we ever want the
public AI-Check to run signed for client shops. Not implemented: the tool accepts arbitrary
visitor-supplied domains and we hold no signatures for those, so it would be dead code
today.
