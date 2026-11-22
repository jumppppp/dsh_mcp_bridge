# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, contact the
maintainers privately (e.g. via the email address on the GitHub profile) with:

- a description of the issue and its impact,
- steps to reproduce,
- affected version/commit.

You will get a response as soon as possible.

## Sensitive data

This project stores runtime data under `data/` by default:

- `data/state/http.token` — MCP Bearer token
- `data/state/web_launch.json` / `web-auth` — DPAPI-encrypted DSH Web credentials
- `data/backups/*.zip` — backups containing DSH configuration, sessions and attachments

**Never commit these files.** The provided `.gitignore` already excludes `data/`.

## Deployment notes

- The panel and MCP server bind to `127.0.0.1` by default. Do not expose them to
  the public internet without a reverse proxy, authentication and TLS.
- `danger-full-access` and `auto` permission presets broaden what the agent may do.
  Use them deliberately.
- Backup archives may contain credentials. Treat them as sensitive.