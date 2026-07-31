# Running a fabric server

The fabric is the exit node: client sessions tunnel to it over WireGuard, so
the client-visible IP never changes when the appliance switches WANs
([ADR-005](../docs/adr/ADR-005-tunnel-is-mandatory.md)). This is where you run
it.

## Quick start

```bash
docker build -t dirty-fabric fabric
docker run -it \
  --cap-add=NET_ADMIN --device /dev/net/tun \
  -p 8081:8081 -p 51820:51820/udp \
  dirty-fabric
```

`--cap-add=NET_ADMIN` lets the container create the `wg0` interface and add
peers; `-p 51820:51820/udp` is the WireGuard data port appliances connect to.
`--device /dev/net/tun` is only needed if the host kernel has no WireGuard module
and the container falls back to a userspace implementation — with a modern kernel
(`ip link add type wireguard` works) it is harmless but unnecessary. Without
`NET_ADMIN`, the API still runs and `/health` answers, but `/register` returns
`503` (see below) rather than crashing.

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
docker run -d \
  --cap-add=NET_ADMIN --device /dev/net/tun \
  -p 8081:8081 -p 51820:51820/udp \
  -v fabric-state:/var/lib/fabric \
  -e FABRIC_ADDRESS=fabric.example.com:51820 \
  -e FABRIC_USERNAME=admin \
  -e FABRIC_PASSWORD=hunter2 \
  dirty-fabric
```

| Variable | Meaning |
|---|---|
| `FABRIC_ADDRESS` | The `host:port` an appliance should connect to. The container doesn't know its own public address — you provide it. |
| `FABRIC_USERNAME` / `FABRIC_PASSWORD` | HTTP Basic Auth credentials guarding every endpoint except `/health`. |
| `FABRIC_TUNNEL_POOL` | Optional. Private pool for tunnel addresses (default `10.99.0.0/24`; `.1` is the fabric itself). |
| `FABRIC_PEER_REGISTRY` | Optional. Path to the pubkey→address JSON (default `/var/lib/fabric/peers.json`). |
| `FABRIC_WG_PRIVATE_KEY_FILE` | Optional. Where the fabric's own WireGuard key lives (default `/var/lib/fabric/wg-privatekey`). |

### Effective NET_ADMIN and the non-root user

The image runs as the unprivileged `fabric` user (uid 10001). `--cap-add=NET_ADMIN`
puts the capability in the container's bounding set, but a non-root process does
**not** get it in its *effective* set automatically. If `/register` returns
`503 tunnel backend unavailable`, that is why. For MVP the pragmatic fix is to
run the container as root so the capability is effective:

```bash
docker run -d --user 0 --cap-add=NET_ADMIN --device /dev/net/tun ... dirty-fabric
```

Configuring ambient capabilities so the non-root user keeps `NET_ADMIN` is the
cleaner long-term answer, but it needs a Dockerfile change and is out of MVP
scope.

### Persisting the fabric's WireGuard identity

`-v fabric-state:/var/lib/fabric` mounts a volume for the peer registry **and**
the fabric's own WireGuard private key. This matters:

- **Without the volume**, the fabric generates a fresh keypair on every container
  start. Its public key changes, so every appliance's stored peer config is
  stale and each must re-register. Peer allocations are also lost.
- **With the volume**, the key and the pubkey→address map survive restarts, and
  appliances stay attached.

The private key is generated on the server and never leaves it — it is not baked
into the image and never returned by the API (only the *public* key is).

Values aren't persisted anywhere — the wizard only ever prints `export`
statements for the entrypoint to `eval`, so there's no credential file to
secure or clean up. Re-running the container without the env vars set (and
without a TTY) will fail closed rather than start unauthenticated.

## What's real right now

- `GET /health` — liveness, version, and the configured address. Unauthenticated.
- `POST /register` — authenticated. Allocates a tunnel address, adds the
  appliance as a WireGuard peer on `wg0`, and returns the parameters the
  appliance needs to bring its side up.

### `POST /register`

Request (JSON):

```json
{ "version": "0.1.0", "public_key": "<appliance wireguard public key>" }
```

Response (`200`):

```json
{
  "assigned_address": "10.99.0.2/32",
  "fabric_public_key": "<fabric wireguard public key>",
  "endpoint": "fabric.example.com:51820",
  "tunnel_pool": "10.99.0.0/24"
}
```

Other outcomes:

| Status | Meaning |
|---|---|
| `400` | `public_key` missing. |
| `401` | Missing or wrong Basic Auth credentials. |
| `409` | Appliance older than `MIN_APPLIANCE_VERSION`. |
| `503` | Tunnel backend unavailable (no effective `NET_ADMIN` / no kernel WireGuard) or the address pool is exhausted. The container keeps running. |

Registration is idempotent: a public key that has already registered gets its
existing address back rather than consuming a new one, so re-registering after a
restart is safe.
