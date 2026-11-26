# Security policy

This app holds health data and can write treatments to Nightscout, so security reports are taken seriously.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private reporting instead: **Security → Report a vulnerability** in this repository. Include what an attacker can do, the steps to reproduce and the affected version or commit. You should get an answer within a week.

## Supported versions

Only the latest commit on `main` is supported. There are no releases with backports.

## Running it safely

- One instance per person; the app has a single password and is not built for several users.
- Expose it only through HTTPS (reverse proxy). By default the container publishes its port on `127.0.0.1` only.
- Use a long `APP_PASSWORD`. Wrong passwords are slowed down, but a weak password is still a weak password.
- `API_READ_KEY` and `MCP_API_KEY` must be at least 32 random characters (`openssl rand -hex 32`). Leave them empty if you do not use the read API or MCP.
- Keep credentials in environment variables or a git-ignored `.env`, never in the repository or in build arguments.
- Back up the data directory; it holds the database and the meal photos.
