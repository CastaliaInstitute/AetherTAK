# AetherTAK Field API

This edge sidecar provides revision-aware agriculture, ecology, observation,
and media synchronization for AetherTAK Field. TAK Server remains responsible
for standard CoT traffic; this API stores the domain records that do not belong
in CoT.

Every request requires a client certificate signed by the same CA used for TAK
enrollment. The peer certificate common name is recorded with each mutation and
media upload.

## Run

The certificate directory must expose these read-only names:

- `server.pem`: server certificate, including any needed intermediate
- `server.key`: server private key readable only by the container
- `ca.pem`: CA certificate used to verify client certificates
- `key-password`: required only when `server.key` is encrypted

Then:

```sh
export AETHER_FIELD_BIND_IP=192.168.86.69
export AETHER_TAK_CERT_DIR=/path/to/aether-field-certs
docker compose up -d --build
```

Use a dedicated directory containing only these three files. Do not mount the
entire TAK certificate workspace because it contains CA private keys.

## Protocol

- `POST /v1/mutations` applies an idempotent domain mutation.
- `GET /v1/changes?cursor=0&limit=100` returns ordered remote changes.
- `PUT /v1/media/{mediaId}` streams a media artifact.
- `GET /healthz` reports readiness (and still requires mutual TLS).

Updates and deletes require `baseRevision`. A stale revision returns HTTP 409
with the current server entity, allowing the mobile client to preserve both
versions and mark a conflict rather than silently overwriting field data.

Media uploads require `Content-Length` and `X-Aether-Sha256`. Optional
`X-Aether-Observation-Id` and `X-Aether-Role` headers retain artifact context.

## Test

```sh
cd deploy/field-api
python -m unittest -v
```
