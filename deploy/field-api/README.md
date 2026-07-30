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
- `GET /v1/identity` reports the authenticated certificate common name and
  effective publisher/Guardian roles for the requesting client.
- `POST /v1/published` upserts or deletes publisher-managed sensor readings,
  read-only Al insights, Guardian participants, and Guardian alerts.
- `POST /guardian/v1/participants/{id}/check-ins` records a participant
  check-in.
- `POST /guardian/v1/alerts/{id}:acknowledge` acknowledges an active Guardian
  alert.
- `POST /guardian/v1/alerts/{id}:resolve` resolves an active or acknowledged
  Guardian alert with a reason.
- `GET /v1/changes?cursor=0&limit=100` returns ordered remote changes.
- `PUT /v1/media/{mediaId}` streams a media artifact.
- `GET /v1/media/{mediaId}` downloads an indexed media artifact.
- `GET /healthz` reports readiness (and still requires mutual TLS).

Updates and deletes require `baseRevision`. A stale revision returns HTTP 409
with the current server entity, allowing the mobile client to preserve both
versions and mark a conflict rather than silently overwriting field data.

Media uploads require `Content-Length` and `X-Aether-Sha256`. Optional
`X-Aether-Observation-Id` and `X-Aether-Role` headers retain artifact context.
Downloads return the stored content type and length plus `X-Aether-Sha256`;
clients must validate all three before committing a file to offline storage.

## Read-only publishers

`sensor_reading`, `al_insight`, `guardian_participant`, and `guardian_alert`
records can never be submitted through the mobile mutation endpoint. They enter
the same ordered change feed through `POST /v1/published`, which additionally
requires the client certificate common name to appear in
`AETHER_FIELD_PUBLISHER_CNS`. The default allowlist contains only the
case-sensitive local AI identity `Al`; use a distinct service certificate
before adding a ChirpStack or Guardian Fusion publisher identity.

An upsert body has this shape:

```json
{
  "entityType": "sensor_reading",
  "entityId": "reading UUID",
  "operation": "upsert",
  "payload": {}
}
```

The payload must conform to the AetherTAK Field mobile schema. Replaying the
same canonical payload is idempotent and does not advance the change cursor.
Use `"operation": "delete"` to publish a tombstone. Publisher requests still
require mutual TLS and are rejected with HTTP 403 for ordinary TAK users.

## Guardian actions

Guardian participant and alert snapshots use exact-key schemas. Unknown fields,
including raw biometrics, are rejected so they cannot leak into the routine
mobile roster. A Guardian publisher may retain richer protected telemetry
outside this API and publish only the operational state needed by TAK clients.

Every action requires a UUID `Idempotency-Key` header. The same key and body
return the original receipt without generating another change; reusing a key
for a different target or body returns HTTP 409. Check-in bodies contain only
`observedAt`; acknowledge bodies are empty objects; resolution bodies contain
only a 3–500 character `reason`.

Certificate authorization is fail-closed:

- `AETHER_GUARDIAN_CHECKIN_CNS` allows participant check-ins.
- `AETHER_GUARDIAN_SUPERVISOR_CNS` allows check-ins plus alert acknowledgement
  and resolution.

Both are comma-separated, case-sensitive certificate common-name allowlists
and default to empty. Give supervisors distinct TAK client certificates; do not
authorize a shared server or publisher identity for interactive actions.
An enrolled client can query `/v1/identity` to confirm its exact common name
and effective roles without exposing certificate or private-key material.

## ChirpStack bridge

The optional `chirpstack-bridge` Compose profile subscribes to ChirpStack v4
decoded uplinks over authenticated MQTT. It maps configured decoder object
paths to the mobile sensor schema, preserves LoRaWAN gateway/radio metadata,
uses deterministic UUIDs for idempotent frame replay, and publishes directly
into the revisioned SQLite change feed.

Before enabling it:

1. Create a dedicated least-privilege Mosquitto user.
2. Copy `chirpstack-bindings.example.json` outside the repository and replace
   its example DevEUI, field/site UUIDs, decoder paths, units, scaling, and
   coordinates.
3. Store only the MQTT password in a mode-0600 file outside the repository.
4. Set `AETHER_CHIRPSTACK_BINDINGS_FILE`,
   `AETHER_CHIRPSTACK_MQTT_PASSWORD_FILE`, and optionally
   `AETHER_CHIRPSTACK_MQTT_USERNAME`. The default MQTT host is the
   `mosquitto` service alias on the external ChirpStack network; set
   `AETHER_CHIRPSTACK_MQTT_HOST` only when that deployment uses another alias.
5. Start the profile with
   `docker compose --profile chirpstack up -d --build`.

The bridge shares only the field data volume and the existing internal
`chirpstack_default` Docker network. It publishes no ports, runs read-only as a
non-root user, and drops all Linux capabilities. Unconfigured DevEUIs and
undecoded uplinks are ignored.

## Test

```sh
cd deploy/field-api
python -m unittest -v
```
