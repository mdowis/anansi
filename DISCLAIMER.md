# Disclaimer and Acceptable Use

## No warranty

Anansi is provided **"AS IS", without warranty of any kind**, express or implied,
as set out in Sections 7 ("Disclaimer of Warranty") and 8 ("Limitation of
Liability") of the Apache License, Version 2.0 under which it is distributed (see
[`LICENSE`](LICENSE)). To the maximum extent permitted by applicable law, the
authors and contributors accept **no liability** for any direct, indirect,
incidental, special, consequential, or other damages, legal consequences,
account suspensions or bans, service disruption, or losses of any kind arising
out of the use, misuse, or inability to use this software — whether by you or by
any third party to whom you provide it.

## You are solely responsible for your use

This tool only fetches the URLs it is told to fetch. The operator and the person
or system driving it (including any LLM/MCP client) are **solely responsible** for
ensuring that each use is lawful and authorized. Before scraping any site you must,
at minimum:

- Have the legal right to access and use the targeted content and data.
- Comply with the target site's Terms of Service and `robots.txt`.
- Respect rate limits and avoid causing degradation of the target service.
- Comply with all applicable laws and regulations, including but not limited to
  computer-misuse / unauthorized-access statutes (e.g. the U.S. Computer Fraud
  and Abuse Act and equivalents), data-protection and privacy law (e.g. GDPR,
  CCPA), intellectual-property law, and database rights.

## Anti-bot and evasion features

Anansi includes features that can reduce the likelihood of automated traffic being
blocked — TLS/HTTP-2-fingerprint impersonation (`curl-cffi`), stealth
browser-fingerprint injection, user-agent rotation, per-host session warm-up,
waiting out Cloudflare challenges, and a graduated escalation ladder for
edge bot-managers such as Akamai. These exist to support **authorized**
testing, security research, and scraping of content the user has a right to
access (for example, sites that block well-behaved clients for no legitimate
reason, or a site you own/operate).

Using these features to circumvent access controls, authentication, or anti-abuse
measures **without authorization** may be unlawful and is **not endorsed or
supported** by the authors. If you have any concern about these capabilities, an
operator can disable all evasion behavior by setting the environment variable:

```
ANANSI_DISABLE_ANTIBOT=1
```

When set, this disables all evasion: stealth-JS injection, the
Cloudflare-challenge wait, curl-cffi TLS/HTTP-2 impersonation, the per-host
session warm-up, the browser→HTTP cookie hand-off, and the Akamai escalation
ladder. Block *detection* still runs so the caller receives an honest
"blocked" status. This switch always wins over `ANANSI_IMPERSONATE`.

## Network reach

By default Anansi refuses to fetch URLs that resolve to loopback, private
(RFC1918), link-local, or cloud-metadata addresses, to prevent server-side request
forgery (SSRF) and lateral movement from the host running the server. An operator
who genuinely needs to scrape internal/trusted hosts may opt in with
`ANANSI_ALLOW_PRIVATE_NETWORKS=1`; this should only be done on a trusted, isolated
host and never when an untrusted LLM client can drive the server.

## No legal advice

Nothing in this document or the project documentation constitutes legal advice.
If you are unsure whether a particular use is permitted, seek qualified legal
counsel before proceeding.
