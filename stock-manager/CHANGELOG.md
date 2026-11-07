# Changelog

## 1.1.0

Security and release hardening.

- Proprietary licence replaces MIT. A signed licence file is required to run; the program holds only the public key.
- Program files are signed at release and checked at start and every five minutes.
- Secrets (mail password, chat tokens, model keys, supplier tokens) are encrypted at rest.
- Four roles (viewer, buyer, approver, admin), several accounts, a Users screen, one permission table.
- Security headers and a content-security policy; sensitive API responses are never cached.
- The audit log is chained with a keyed hash; a broken chain is a critical alert; `stock audit --verify`.
- `stock doctor`, `stock version`, `/api/health`.
- Dependencies upgraded and audited (Pillow 13); `requirements.lock` and `THIRD-PARTY.md` added.
- Lint (ruff) and types for the security modules (mypy) in the build.

Also in this version: supplier replies are read and filed, Plutus chases and disputes inside its limits, order webhooks, an
SFTP bank drop, site requests, receiving checks with learned places, and a two-count rule for large stock gaps.

## 1.0.0

First version.
