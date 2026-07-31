# Running a fabric server

The fabric is the exit node: client sessions tunnel to it over WireGuard, so
the client-visible IP never changes when the appliance switches WANs
([ADR-005](../docs/adr/ADR-005-tunnel-is-mandatory.md)). This is where you run
it.

## Quick start

```bash
docker build -t dirty-fabric fabric
docker run -it -p 8081:8081 dirty-fabric
```

With no configuration, the first run walks you through a setup wizard:

```
=== Fabric first-run setup ===
FABRIC_ADDRESS / FABRIC_USERNAME / FABRIC_PASSWORD are not set. Enter them
now, or set the environment variables to skip this next run.
Public address devices will connect to (host:port): fabric.example.com:51820
Admin username: admin
Admin password:
Confirm password:
```

The wizard needs a real terminal (`-it`). In a detached or orchestrated
container (`docker run -d`, Compose, Kubernetes) there's nothing to prompt, so
the container exits immediately with an error telling you which environment
variables to set instead of hanging.

## Non-interactive / production

Skip the wizard entirely by providing the three environment variables:

```bash
docker run -d -p 8081:8081 \
  -e FABRIC_ADDRESS=fabric.example.com:51820 \
  -e FABRIC_USERNAME=admin \
  -e FABRIC_PASSWORD=hunter2 \
  dirty-fabric
```

| Variable | Meaning |
|---|---|
| `FABRIC_ADDRESS` | The `host:port` an appliance should connect to. The container doesn't know its own public address — you provide it. |
| `FABRIC_USERNAME` / `FABRIC_PASSWORD` | HTTP Basic Auth credentials guarding every endpoint except `/health`. |

Values aren't persisted anywhere — the wizard only ever prints `export`
statements for the entrypoint to `eval`, so there's no credential file to
secure or clean up. Re-running the container without the env vars set (and
without a TTY) will fail closed rather than start unauthenticated.

## What's real right now

- `GET /health` — liveness, version, and the configured address. Unauthenticated.
- `POST /register` — authenticated, but still returns `501` (WS-E scope, not
  yet implemented). See [`docs/roadmap.md`](../docs/roadmap.md).

There is no WireGuard peer management yet. Standing up an actual tunnel today
means doing it by hand (`wg` on both ends) — nothing in this API does it for
you.
