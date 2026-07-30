# AetherTAK ARM64 edge deployment

This directory contains the reproducible build and operations notes for the
Castalia Institute Raspberry Pi deployment. Runtime certificates, passwords,
database volumes, and enrollment packages are intentionally excluded.

## Build the TAK Server distribution

The upstream build requires Java 17 and `patch`. Build the ARM64 builder image
from the repository root:

```sh
docker build -t aethertak-builder:java17 -f deploy/Dockerfile.builder .
```

Run the Gradle build with caches kept outside the repository:

```sh
docker run --rm \
  -v "$PWD:/workspace" \
  -v aethertak-gradle-cache:/root/.gradle \
  -w /workspace/src \
  aethertak-builder:java17 \
  ./gradlew bootWar bootJar shadowJar -x test
```

The deployable Docker bundle is produced under:

```text
src/takserver-package/build/distributions/takserver-docker-<version>.zip
```

The current Pi deployment uses AetherTAK `5.7-RELEASE-14-main` and stores
Docker data on the NVMe SSD.

## Edge services

The active Pi deployment exposes only the interfaces needed by field clients
and LoRaWAN gateways:

| Service | Bind address | Purpose |
| --- | --- | --- |
| TAK CoT TLS | `192.168.86.69:8089` | ATAK, iTAK, and AetherTAK clients |
| TAK API | `192.168.86.69:8443` | Certificate-authenticated TAK API |
| ChirpStack UI | `192.168.86.69:8080` | LoRaWAN administration |
| MQTT | `192.168.86.69:1883` | Authenticated ChirpStack traffic |
| Semtech UDP | `192.168.86.69:1700/udp` | Packet-forwarder gateways |
| Basics Station | `192.168.86.69:3001` | WebSocket gateway transport |
| ChirpStack REST proxy | `127.0.0.1:8090` | Local integrations only |

Remote clients reach the LAN addresses through the Tailscale subnet route
`192.168.86.0/24`. Do not port-forward these services to the public internet.

## ChirpStack

The Pi runs ChirpStack `4.18.0` from the official
`chirpstack/chirpstack-docker` Compose stack, configured as follows:

- Region and gateway topic prefix: `us915_0`.
- Anonymous MQTT access disabled.
- PostgreSQL, Redis, and Docker restart policies enabled.
- API, PostgreSQL, and MQTT secrets generated locally and stored outside this
  repository.
- PostgreSQL and Redis volumes stored beneath the Docker data root on the NVMe
  SSD.
- Redis memory overcommit enabled through
  `/etc/sysctl.d/98-chirpstack-redis.conf`.

Gateways using the Semtech UDP packet forwarder should send to
`192.168.86.69:1700`. Basics Station gateways should use
`ws://192.168.86.69:3001` and the `US915 / us915_0` channel plan.

## Certificates and identities

Create a unique non-administrator TAK certificate for every person, device, or
service. Enrollment packages contain private keys and must not be committed or
shared between devices. Revoked certificates should remain in the certificate
audit archive and CRL.

The local AI identity is `Al` (case-sensitive). Its former lowercase
certificate has been revoked and replaced.

## Verification

Run the health check with a certificate-authenticated TAK administrator:

```sh
TAK_ADMIN_PEM=/path/to/admin.pem \
TAK_ADMIN_KEY=/path/to/admin.key \
TAK_CERT_PASSWORD='certificate-password' \
deploy/verify-edge.sh
```
