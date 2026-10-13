# Security Policy

## Supported versions

Only the latest release on `main` receives fixes.

## Reporting a vulnerability

Please **don't** open a public issue. Use [GitHub private vulnerability reporting](https://github.com/Hasib105/tomeshift/security/advisories/new) instead. You should hear back within 7 days.

## Things to know

- `tomeshift ui` listens on `127.0.0.1` by default and has **no authentication**. Don't pass `--host 0.0.0.0` on a shared or public network.
- tomeshift sends your book's text to whichever backend you choose (a CLI agent or an API). Treat confidential documents accordingly.
- API keys are read only from environment variables. They are never written to project folders or logs.
