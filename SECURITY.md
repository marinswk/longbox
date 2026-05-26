# Security policy

## Threat model

Longbox is designed for **single-user LAN deployment**. It has no
authentication, no rate-limiting, and exposes destructive admin
endpoints (`/admin/wipe`, `/admin/import`) on the same surface as
read-only browsing. Deploying it on the public internet without a
fronting auth proxy is **not supported** and should be assumed unsafe.

If you want a public deployment, put it behind a reverse proxy that
enforces TLS + authentication (Caddy + `basic_auth`, Traefik + a
forward-auth middleware, Cloudflare Access, etc.).

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security
vulnerabilities. Email **marins.wk@gmail.com** instead — same address
listed as the repo owner on GitHub.

When reporting, please include:

- A clear description of the issue.
- Steps to reproduce, or a minimal proof of concept.
- Affected version (`/health` returns the running version).
- Your assessment of severity if you have one.

Expect an initial reply within **7 days**. We'll work with you on a
disclosure timeline; if the issue is severe we'll prioritise a fix
ahead of routine work.

## Scope

In scope:

- Remote code execution.
- SQL injection, template injection, or any other server-side injection.
- Path traversal on the cover-storage or backup-restore code paths.
- Prompt-injection vectors that could survive past the pre-commit /
  pytest guard.
- Anything that lets one user's session affect a different user's data
  (theoretical — Longbox is single-user, but if a future PR adds
  multi-user this becomes relevant).

Out of scope:

- Anything requiring physical / network-level access to the LAN
  Longbox sits on.
- Denial of service via large CSV uploads / many simultaneous lookups
  (the app is single-user; rate-limit yourself).
- Missing authentication (intentional — see "Threat model" above).
- Issues in upstream metadata APIs (ComicVine, Metron, Wookieepedia,
  Open Library). Report those to the respective project.

## Defensive features already in place

- **No tracked secrets.** `.env` is gitignored; `.env.example` ships
  with empty values only.
- **Prompt-injection guard.** Two-layer scan
  (`app/tests/test_no_injection_markers.py` + `.githooks/pre-commit`)
  blocks source commits containing known injection markers.
- **Typed-confirmation factory reset.** `/admin/wipe` requires typing
  `WIPE EVERYTHING` into a confirm input AND a JS confirm dialog before
  destruction.
- **Single-transaction restore.** `/admin/import` truncates + reloads
  inside one SQL transaction. A mid-way failure leaves the original
  data intact.
- **`pre-commit` hook is opt-in.** Activate once per clone with
  `git config core.hooksPath .githooks`. The pytest scan is the
  belt-and-braces backstop.
- **Optional CSRF guard.** Set `CSRF_ALLOWED_ORIGINS` to a
  comma-separated list of the URLs you actually open Longbox at;
  any non-GET request whose `Origin` doesn't match gets 403. Stops
  a malicious site you visit in another tab from POSTing to
  `/admin/wipe`. Default unset so the first-run experience stays
  painless.
- **Optional TrustedHost allowlist.** Set `ALLOWED_HOSTS` to the
  hostnames you serve under (e.g. `longbox.lan,localhost`). Anything
  else gets 400 — blocks Host-header spoofing when fronted by a
  reverse proxy.
- **SSRF guard on remote cover fetches.** `covers.download` resolves
  the URL and refuses any address in a private / loopback /
  link-local / reserved range. Stops a user-supplied
  `cover_url_remote` from being used to probe internal LAN hosts
  (or AWS-metadata-style `169.254.x.y` endpoints if Longbox is ever
  cloud-deployed).
- **Cover download size cap.** Streamed with a 10 MB ceiling — a
  malicious upstream serving 500 MB can't OOM the container.
- **Cover content validation.** Downloaded bytes are round-tripped
  through Pillow; a server lying about `Content-Type` (claiming
  `image/jpeg` while serving HTML) gets the payload dropped on the
  floor before it lands in `/data/covers/`.
