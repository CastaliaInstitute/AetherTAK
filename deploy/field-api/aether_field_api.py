#!/usr/bin/env python3
"""AetherTAK Field synchronization API.

The service deliberately uses only Python's standard library so the edge
deployment has a small, auditable dependency surface. Authentication is mutual
TLS: every request must present a client certificate signed by the configured
TAK certificate authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import ssl
import tempfile
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


MUTABLE_ENTITY_TYPES = {
    "property",
    "season",
    "field",
    "ecological_site",
    "observation",
    "media",
    "alert",
}
PUBLISHED_ENTITY_TYPES = {
    "sensor_reading",
    "al_insight",
    "guardian_participant",
    "guardian_alert",
}
SENSOR_MEASUREMENTS = {
    "soil_moisture",
    "air_temperature",
    "soil_temperature",
    "humidity",
    "water_level",
    "conductivity",
    "ph",
}
ALLOWED_OPERATIONS = {"create", "update", "delete"}
MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "video/mp4": ".mp4",
    "model/ply": ".ply",
    "model/obj": ".obj",
    "application/x-aether-depth-f32le": ".f32le",
    "application/x-aether-depth-u16le": ".u16le",
    "application/x-aether-depth-confidence": ".u8",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _published_payload_error(message: str) -> None:
    raise ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "INVALID_PUBLISHED_PAYLOAD",
        message,
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _valid_uuid(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _valid_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


def _valid_coordinate(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    if (
        not _finite_number(latitude)
        or not -90 <= latitude <= 90
        or not _finite_number(longitude)
        or not -180 <= longitude <= 180
    ):
        return False
    for key in (
        "altitudeMeters",
        "horizontalAccuracyMeters",
        "verticalAccuracyMeters",
        "headingDegrees",
    ):
        item = value.get(key)
        if item is not None and not _finite_number(item):
            return False
    if (
        value.get("horizontalAccuracyMeters") is not None
        and value["horizontalAccuracyMeters"] < 0
    ) or (
        value.get("verticalAccuracyMeters") is not None
        and value["verticalAccuracyMeters"] < 0
    ):
        return False
    heading = value.get("headingDegrees")
    return heading is None or 0 <= heading <= 360


def _exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _valid_guardian_coordinate(value: Any) -> bool:
    return _exact_keys(
        value,
        {
            "latitude",
            "longitude",
            "altitudeMeters",
            "horizontalAccuracyMeters",
            "verticalAccuracyMeters",
            "headingDegrees",
        },
    ) and _valid_coordinate(value)


def _valid_lorawan(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if (
        not _nonempty_string(value.get("applicationId"))
        or not _nonempty_string(value.get("devEui"))
        or isinstance(value.get("fPort"), bool)
        or not isinstance(value.get("fPort"), int)
        or not 0 <= value["fPort"] <= 255
        or isinstance(value.get("frameCounter"), bool)
        or not isinstance(value.get("frameCounter"), int)
        or value["frameCounter"] < 0
        or not isinstance(value.get("gatewayIds"), list)
        or not all(isinstance(item, str) for item in value["gatewayIds"])
    ):
        return False
    for key in ("rssi", "snr"):
        if value.get(key) is not None and not _finite_number(value[key]):
            return False
    spreading_factor = value.get("spreadingFactor")
    if spreading_factor is not None and (
        isinstance(spreading_factor, bool)
        or not isinstance(spreading_factor, int)
    ):
        return False
    frequency = value.get("frequencyHz")
    return frequency is None or (
        not isinstance(frequency, bool)
        and isinstance(frequency, int)
        and frequency > 0
    )


def validate_published_payload(
    entity_type: str, entity_id: str, payload: dict[str, Any]
) -> None:
    if not _valid_uuid(entity_id) or payload.get("id") != entity_id:
        _published_payload_error("Published IDs must be matching UUIDs.")
    if entity_type == "sensor_reading":
        if (
            not _nonempty_string(payload.get("deviceId"))
            or not _valid_uuid(payload.get("fieldId"), nullable=True)
            or not _valid_uuid(payload.get("siteId"), nullable=True)
            or not _nonempty_string(payload.get("label"))
            or payload.get("measurement") not in SENSOR_MEASUREMENTS
            or not _finite_number(payload.get("value"))
            or not _nonempty_string(payload.get("unit"))
            or payload.get("quality") not in {"good", "estimated", "suspect"}
            or not _valid_lorawan(payload.get("lorawan"))
            or not _valid_coordinate(payload.get("coordinate"))
            or not _valid_datetime(payload.get("recordedAt"))
        ):
            _published_payload_error(
                "Sensor reading payload does not match the mobile schema."
            )
        return
    if entity_type == "al_insight":
        if (
            not _nonempty_string(payload.get("title"))
            or not _nonempty_string(payload.get("summary"))
            or not isinstance(payload.get("rationale"), str)
            or not isinstance(payload.get("sourceReadingIds"), list)
            or not all(_valid_uuid(item) for item in payload["sourceReadingIds"])
            or not _valid_uuid(payload.get("fieldId"), nullable=True)
            or not _valid_uuid(payload.get("siteId"), nullable=True)
            or payload.get("severity") not in {"info", "attention"}
            or not _valid_datetime(payload.get("generatedAt"))
            or not _valid_datetime(payload.get("expiresAt"))
            or payload.get("readOnly") is not True
        ):
            _published_payload_error(
                "Al insight payload does not match the read-only mobile schema."
            )
        return
    if entity_type == "guardian_participant":
        location = payload.get("location")
        device = payload.get("device")
        valid = (
            _exact_keys(
                payload,
                {
                    "id",
                    "displayName",
                    "mode",
                    "team",
                    "state",
                    "zone",
                    "alertState",
                    "checkIn",
                    "location",
                    "device",
                    "updatedAt",
                },
            )
            and isinstance(payload.get("displayName"), str)
            and 1 <= len(payload["displayName"]) <= 120
            and payload.get("mode") in {"child", "guest", "supervisor", "medical"}
            and isinstance(payload.get("team"), str)
            and 1 <= len(payload["team"]) <= 64
            and payload.get("state") in {"normal", "caution", "critical", "offline"}
            and (
                payload.get("zone") is None
                or (
                    isinstance(payload.get("zone"), str)
                    and len(payload["zone"]) <= 120
                )
            )
            and payload.get("alertState") in {"none", "warning", "critical", "sos"}
            and payload.get("checkIn") in {"current", "due", "missed", "not_required"}
            and _exact_keys(
                location, {"coordinate", "source", "confidence", "observedAt"}
            )
            and _valid_guardian_coordinate(location.get("coordinate"))
            and location.get("source")
            in {
                "watch_gnss",
                "ble_estimate",
                "ble_presence",
                "meshtastic",
                "last_known",
            }
            and location.get("confidence") in {"good", "estimated", "poor", "stale"}
            and _valid_datetime(location.get("observedAt"))
            and _exact_keys(
                device, {"connectivity", "lastContactAt", "batteryPercent"}
            )
            and device.get("connectivity")
            in {
                "watch_phone_wifi",
                "watch_phone_cellular",
                "guardian_ble",
                "wifi",
                "meshtastic",
                "offline",
            }
            and _valid_datetime(device.get("lastContactAt"))
            and (
                device.get("batteryPercent") is None
                or (
                    _finite_number(device.get("batteryPercent"))
                    and 0 <= device["batteryPercent"] <= 100
                )
            )
            and _valid_datetime(payload.get("updatedAt"))
        )
        if not valid:
            _published_payload_error(
                "Guardian participant payload does not match the privacy-safe mobile schema."
            )
        return
    if entity_type == "guardian_alert":
        valid = (
            _exact_keys(
                payload,
                {
                    "id",
                    "participantId",
                    "ruleId",
                    "severity",
                    "status",
                    "reasonCode",
                    "title",
                    "detail",
                    "openedAt",
                    "acknowledgedAt",
                    "resolvedAt",
                    "resolutionReason",
                    "updatedAt",
                },
            )
            and _valid_uuid(payload.get("participantId"))
            and isinstance(payload.get("ruleId"), str)
            and 1 <= len(payload["ruleId"]) <= 120
            and payload.get("severity") in {"info", "warning", "critical"}
            and payload.get("status") in {"active", "acknowledged", "resolved"}
            and isinstance(payload.get("reasonCode"), str)
            and 1 <= len(payload["reasonCode"]) <= 120
            and isinstance(payload.get("title"), str)
            and 1 <= len(payload["title"]) <= 160
            and isinstance(payload.get("detail"), str)
            and len(payload["detail"]) <= 500
            and _valid_datetime(payload.get("openedAt"))
            and (
                payload.get("acknowledgedAt") is None
                or _valid_datetime(payload.get("acknowledgedAt"))
            )
            and (
                payload.get("resolvedAt") is None
                or _valid_datetime(payload.get("resolvedAt"))
            )
            and (
                payload.get("resolutionReason") is None
                or (
                    isinstance(payload.get("resolutionReason"), str)
                    and len(payload["resolutionReason"]) <= 500
                )
            )
            and _valid_datetime(payload.get("updatedAt"))
        )
        if valid and payload["status"] == "active":
            valid = (
                payload["acknowledgedAt"] is None and payload["resolvedAt"] is None
            )
        elif valid and payload["status"] == "acknowledged":
            valid = payload["acknowledgedAt"] is not None
        elif valid and payload["status"] == "resolved":
            valid = (
                payload["resolvedAt"] is not None
                and isinstance(payload["resolutionReason"], str)
                and bool(payload["resolutionReason"].strip())
            )
        if not valid:
            _published_payload_error(
                "Guardian alert payload does not match the mobile lifecycle schema."
            )
        return
    if (
        not _nonempty_string(payload.get("title"))
        or not _nonempty_string(payload.get("summary"))
        or not isinstance(payload.get("rationale"), str)
        or not isinstance(payload.get("sourceReadingIds"), list)
        or not all(_valid_uuid(item) for item in payload["sourceReadingIds"])
        or not _valid_uuid(payload.get("fieldId"), nullable=True)
        or not _valid_uuid(payload.get("siteId"), nullable=True)
        or payload.get("severity") not in {"info", "attention"}
        or not _valid_datetime(payload.get("generatedAt"))
        or not _valid_datetime(payload.get("expiresAt"))
        or payload.get("readOnly") is not True
    ):
        _published_payload_error("Unsupported published payload.")


@dataclass(frozen=True)
class Mutation:
    id: str
    entity_type: str
    entity_id: str
    operation: str
    payload: Any
    base_revision: int | None

    @classmethod
    def from_json(cls, value: Any) -> "Mutation":
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_MUTATION", "Expected a JSON object.")
        required = ("id", "entityType", "entityId", "operation")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_MUTATION",
                "id, entityType, entityId, and operation are required strings.",
            )
        if value["entityType"] in PUBLISHED_ENTITY_TYPES:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "READ_ONLY_ENTITY",
                f"{value['entityType']} records are publisher-managed and read-only.",
            )
        if value["entityType"] not in MUTABLE_ENTITY_TYPES:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_ENTITY_TYPE",
                f"Unsupported entity type: {value['entityType']}",
            )
        if value["operation"] not in ALLOWED_OPERATIONS:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_OPERATION",
                f"Unsupported operation: {value['operation']}",
            )
        base_revision = value.get("baseRevision")
        if base_revision is not None and (
            not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REVISION",
                "baseRevision must be a non-negative integer or null.",
            )
        if value["operation"] != "create" and base_revision is None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "REVISION_REQUIRED",
                "Updates and deletes require baseRevision.",
            )
        return cls(
            id=value["id"],
            entity_type=value["entityType"],
            entity_id=value["entityId"],
            operation=value["operation"],
            payload=value.get("payload"),
            base_revision=base_revision,
        )


@dataclass(frozen=True)
class PublishedRecord:
    entity_type: str
    entity_id: str
    operation: str
    payload: Any

    @classmethod
    def from_json(cls, value: Any) -> "PublishedRecord":
        if not isinstance(value, dict):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PUBLISHED_RECORD",
                "Expected a JSON object.",
            )
        entity_type = value.get("entityType")
        entity_id = value.get("entityId")
        operation = value.get("operation", "upsert")
        if entity_type not in PUBLISHED_ENTITY_TYPES:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PUBLISHED_ENTITY_TYPE",
                f"Unsupported published entity type: {entity_type}",
            )
        if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 128:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PUBLISHED_RECORD",
                "entityId must be a non-empty string no longer than 128 characters.",
            )
        if operation not in {"upsert", "delete"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PUBLISHED_OPERATION",
                "Published operation must be upsert or delete.",
            )
        payload = value.get("payload")
        if operation == "upsert" and not isinstance(payload, dict):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PUBLISHED_RECORD",
                "Published upserts require an object payload.",
            )
        if operation == "upsert":
            validate_published_payload(entity_type, entity_id, payload)
        return cls(
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            payload=payload,
        )


class FieldStore:
    def __init__(self, database_path: Path, media_root: Path):
        self.database_path = database_path
        self.media_root = media_root
        self._schema_lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        media_root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT,
                    updated_at TEXT NOT NULL,
                    author_cn TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_id)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT,
                    updated_at TEXT NOT NULL,
                    author_cn TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutations (
                    mutation_id TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    author_cn TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guardian_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    author_cn TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media (
                    media_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    observation_id TEXT,
                    role TEXT,
                    created_at TEXT NOT NULL,
                    author_cn TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _entity_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "entityType": row["entity_type"],
            "entityId": row["entity_id"],
            "revision": row["revision"],
            "deleted": bool(row["deleted"]),
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
            "updatedAt": row["updated_at"],
            "author": row["author_cn"],
        }

    def apply_mutation(self, mutation: Mutation, author_cn: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_mutation = connection.execute(
                "SELECT response_json FROM mutations WHERE mutation_id = ?",
                (mutation.id,),
            ).fetchone()
            if existing_mutation:
                response = json.loads(existing_mutation["response_json"])
                response["idempotentReplay"] = True
                return response

            current_row = connection.execute(
                "SELECT * FROM entities WHERE entity_type = ? AND entity_id = ?",
                (mutation.entity_type, mutation.entity_id),
            ).fetchone()
            current = self._entity_from_row(current_row)
            current_revision = current["revision"] if current else 0

            if mutation.operation == "create" and current and not current["deleted"]:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "ENTITY_EXISTS",
                    "The entity already exists.",
                    current=current,
                )
            if mutation.operation != "create" and (
                not current or mutation.base_revision != current_revision
            ):
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "REVISION_CONFLICT",
                    "The server entity changed since this edit was based.",
                    current=current,
                )

            revision = current_revision + 1
            deleted = mutation.operation == "delete"
            payload_json = None if deleted else json.dumps(
                mutation.payload, separators=(",", ":"), sort_keys=True
            )
            connection.execute(
                """
                INSERT INTO entities (
                    entity_type, entity_id, revision, deleted, payload_json, updated_at, author_cn
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                    revision = excluded.revision,
                    deleted = excluded.deleted,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    author_cn = excluded.author_cn
                """,
                (
                    mutation.entity_type,
                    mutation.entity_id,
                    revision,
                    int(deleted),
                    payload_json,
                    now,
                    author_cn,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO changes (
                    entity_type, entity_id, revision, operation, payload_json, updated_at, author_cn
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.entity_type,
                    mutation.entity_id,
                    revision,
                    mutation.operation,
                    payload_json,
                    now,
                    author_cn,
                ),
            )
            response = {
                "accepted": True,
                "mutationId": mutation.id,
                "entityType": mutation.entity_type,
                "entityId": mutation.entity_id,
                "revision": revision,
                "cursor": cursor.lastrowid,
                "serverUpdatedAt": now,
                "idempotentReplay": False,
            }
            connection.execute(
                """
                INSERT INTO mutations (mutation_id, response_json, created_at, author_cn)
                VALUES (?, ?, ?, ?)
                """,
                (
                    mutation.id,
                    json.dumps(response, separators=(",", ":"), sort_keys=True),
                    now,
                    author_cn,
                ),
            )
            connection.commit()
            return response

    def changes(self, cursor: int, limit: int) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, entity_type, entity_id, revision, operation,
                       payload_json, updated_at, author_cn
                FROM changes WHERE sequence > ? ORDER BY sequence LIMIT ?
                """,
                (cursor, limit),
            ).fetchall()
        changes = [
            {
                "cursor": row["sequence"],
                "entityType": row["entity_type"],
                "entityId": row["entity_id"],
                "revision": row["revision"],
                "operation": row["operation"],
                "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
                "serverUpdatedAt": row["updated_at"],
                "author": row["author_cn"],
            }
            for row in rows
        ]
        next_cursor = changes[-1]["cursor"] if changes else cursor
        return {"changes": changes, "nextCursor": next_cursor, "hasMore": len(changes) == limit}

    def publish_record(
        self, record: PublishedRecord, author_cn: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM entities WHERE entity_type = ? AND entity_id = ?",
                (record.entity_type, record.entity_id),
            ).fetchone()
            current = self._entity_from_row(current_row)
            deleting = record.operation == "delete"
            payload_json = (
                None
                if deleting
                else json.dumps(record.payload, separators=(",", ":"), sort_keys=True)
            )
            unchanged = (
                (deleting and (not current or current["deleted"]))
                or (
                    not deleting
                    and current is not None
                    and not current["deleted"]
                    and current_row["payload_json"] == payload_json
                )
            )
            if unchanged:
                return {
                    "accepted": True,
                    "entityType": record.entity_type,
                    "entityId": record.entity_id,
                    "revision": current["revision"] if current else 0,
                    "cursor": None,
                    "serverUpdatedAt": current["updatedAt"] if current else now,
                    "idempotentReplay": True,
                }

            revision = (current["revision"] if current else 0) + 1
            change_operation = (
                "delete"
                if deleting
                else "update"
                if current and not current["deleted"]
                else "create"
            )
            connection.execute(
                """
                INSERT INTO entities (
                    entity_type, entity_id, revision, deleted, payload_json, updated_at, author_cn
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                    revision = excluded.revision,
                    deleted = excluded.deleted,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    author_cn = excluded.author_cn
                """,
                (
                    record.entity_type,
                    record.entity_id,
                    revision,
                    int(deleting),
                    payload_json,
                    now,
                    author_cn,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO changes (
                    entity_type, entity_id, revision, operation,
                    payload_json, updated_at, author_cn
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.entity_type,
                    record.entity_id,
                    revision,
                    change_operation,
                    payload_json,
                    now,
                    author_cn,
                ),
            ).lastrowid
            return {
                "accepted": True,
                "entityType": record.entity_type,
                "entityId": record.entity_id,
                "revision": revision,
                "cursor": cursor,
                "serverUpdatedAt": now,
                "idempotentReplay": False,
            }

    def apply_guardian_action(
        self,
        *,
        idempotency_key: str,
        action: str,
        target_id: str,
        reason: str | None,
        observed_at: str | None,
        author_cn: str,
    ) -> dict[str, Any]:
        if not _valid_uuid(idempotency_key) or not _valid_uuid(target_id):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_GUARDIAN_ACTION",
                "Guardian action and target IDs must be UUIDs.",
            )
        if action not in {"check_in", "acknowledge", "resolve"}:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_GUARDIAN_ACTION",
                "Unsupported Guardian action.",
            )
        if action == "check_in":
            if reason is not None or not _valid_datetime(observed_at):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_GUARDIAN_ACTION",
                    "Check-in requires one timezone-aware observedAt timestamp.",
                )
            entity_type = "guardian_participant"
        else:
            if observed_at is not None:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_GUARDIAN_ACTION",
                    "Alert actions do not accept observedAt.",
                )
            if action == "resolve" and (
                not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500
            ):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_GUARDIAN_ACTION",
                    "Resolution reason must contain 3 to 500 characters.",
                )
            if action == "acknowledge" and reason is not None:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_GUARDIAN_ACTION",
                    "Acknowledgement does not accept a reason.",
                )
            entity_type = "guardian_alert"

        now = utc_now()
        request_json = json.dumps(
            {"observedAt": observed_at} if action == "check_in" else
            {"reason": reason.strip()} if action == "resolve" else {},
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT action, target_id, request_json, response_json
                FROM guardian_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if prior:
                if (
                    prior["action"] != action
                    or prior["target_id"] != target_id
                    or prior["request_json"] != request_json
                ):
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "IDEMPOTENCY_KEY_REUSED",
                        "This idempotency key was already used for another Guardian action.",
                    )
                response = json.loads(prior["response_json"])
                response["idempotentReplay"] = True
                return response

            current_row = connection.execute(
                """
                SELECT * FROM entities
                WHERE entity_type = ? AND entity_id = ? AND deleted = 0
                """,
                (entity_type, target_id),
            ).fetchone()
            current = self._entity_from_row(current_row)
            if not current or not isinstance(current["payload"], dict):
                raise ApiError(
                    HTTPStatus.NOT_FOUND,
                    "GUARDIAN_TARGET_NOT_FOUND",
                    "The Guardian action target does not exist.",
                )
            payload = current["payload"]
            if action == "check_in":
                payload["checkIn"] = "current"
            elif action == "acknowledge":
                if payload.get("status") == "resolved":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "GUARDIAN_ALERT_RESOLVED",
                        "A resolved Guardian alert cannot be acknowledged.",
                    )
                if payload.get("status") != "active":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "GUARDIAN_ALERT_ACKNOWLEDGED",
                        "This Guardian alert is already acknowledged.",
                    )
                payload["status"] = "acknowledged"
                payload["acknowledgedAt"] = now
            else:
                if payload.get("status") == "resolved":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "GUARDIAN_ALERT_RESOLVED",
                        "This Guardian alert is already resolved.",
                    )
                payload["status"] = "resolved"
                payload["resolvedAt"] = now
                payload["resolutionReason"] = reason.strip()
            payload["updatedAt"] = now
            validate_published_payload(entity_type, target_id, payload)

            revision = current["revision"] + 1
            payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            connection.execute(
                """
                UPDATE entities SET revision = ?, payload_json = ?, updated_at = ?,
                    author_cn = ?
                WHERE entity_type = ? AND entity_id = ?
                """,
                (revision, payload_json, now, author_cn, entity_type, target_id),
            )
            connection.execute(
                """
                INSERT INTO changes (
                    entity_type, entity_id, revision, operation, payload_json,
                    updated_at, author_cn
                ) VALUES (?, ?, ?, 'update', ?, ?, ?)
                """,
                (entity_type, target_id, revision, payload_json, now, author_cn),
            )
            response = {
                "accepted": True,
                "idempotencyKey": idempotency_key,
                "action": action,
                "targetId": target_id,
                "serverTime": now,
                "entityVersion": revision,
                "idempotentReplay": False,
            }
            connection.execute(
                """
                INSERT INTO guardian_actions (
                    idempotency_key, action, target_id, request_json, response_json,
                    created_at, author_cn
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    action,
                    target_id,
                    request_json,
                    json.dumps(response, separators=(",", ":"), sort_keys=True),
                    now,
                    author_cn,
                ),
            )
            return response

    def media_record(self, media_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media WHERE media_id = ?", (media_id,)
            ).fetchone()
        return dict(row) if row else None

    def media_download(self, media_id: str) -> tuple[dict[str, Any], Path]:
        record = self.media_record(media_id)
        if not record:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "MEDIA_NOT_FOUND",
                "No media artifact exists for this ID.",
            )
        media_root = self.media_root.resolve()
        path = (media_root / record["relative_path"]).resolve()
        if path.parent != media_root:
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "MEDIA_PATH_INVALID",
                "The indexed media path is invalid.",
            )
        if not path.is_file() or path.stat().st_size != record["size_bytes"]:
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "MEDIA_UNAVAILABLE",
                "The indexed media artifact is unavailable.",
            )
        return record, path

    def save_media(
        self,
        *,
        media_id: str,
        content_type: str,
        expected_sha256: str,
        observation_id: str | None,
        role: str | None,
        author_cn: str,
        stream: Any,
        content_length: int,
    ) -> dict[str, Any]:
        existing = self.media_record(media_id)
        if existing:
            if existing["sha256"] != expected_sha256:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "MEDIA_CONFLICT",
                    "This media ID already refers to different content.",
                )
            return {
                "accepted": True,
                "mediaId": media_id,
                "sha256": existing["sha256"],
                "sizeBytes": existing["size_bytes"],
                "idempotentReplay": True,
            }

        suffix = MEDIA_EXTENSIONS.get(content_type, ".bin")
        destination = self.media_root / f"{media_id}{suffix}"
        digest = hashlib.sha256()
        remaining = content_length
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{media_id}-", dir=self.media_root
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST,
                            "TRUNCATED_MEDIA",
                            "The media request ended before Content-Length bytes arrived.",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise ApiError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "CHECKSUM_MISMATCH",
                    "The uploaded media SHA-256 does not match its header.",
                    actualSha256=actual_sha256,
                )
            os.replace(temporary_name, destination)
            now = utc_now()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO media (
                        media_id, sha256, size_bytes, content_type, relative_path,
                        observation_id, role, created_at, author_cn
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_id,
                        actual_sha256,
                        content_length,
                        content_type,
                        destination.name,
                        observation_id,
                        role,
                        now,
                        author_cn,
                    ),
                )
            return {
                "accepted": True,
                "mediaId": media_id,
                "sha256": actual_sha256,
                "sizeBytes": content_length,
                "serverUpdatedAt": now,
                "idempotentReplay": False,
            }
        finally:
            Path(temporary_name).unlink(missing_ok=True)


class AetherFieldHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AetherField/1"

    @property
    def field_server(self) -> "AetherFieldServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format_string: str, *args: Any) -> None:
        peer = self.client_address[0] if self.client_address else "unknown"
        print(f"{utc_now()} {peer} {format_string % args}", flush=True)

    def _author_cn(self) -> str:
        certificate = self.connection.getpeercert()  # type: ignore[attr-defined]
        for relative_name in certificate.get("subject", ()):
            for key, value in relative_name:
                if key == "commonName":
                    return value
        raise ApiError(
            HTTPStatus.UNAUTHORIZED,
            "CLIENT_IDENTITY_MISSING",
            "The client certificate has no common name.",
        )

    def _json(self, status: HTTPStatus, value: Any) -> None:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _media(self, record: dict[str, Any], path: Path) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", record["content_type"])
        self.send_header("Content-Length", str(record["size_bytes"]))
        self.send_header("X-Aether-Sha256", record["sha256"])
        self.send_header("X-Aether-Media-Id", record["media_id"])
        if record["observation_id"]:
            self.send_header("X-Aether-Observation-Id", record["observation_id"])
        if record["role"]:
            self.send_header("X-Aether-Role", record["role"])
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("ETag", f'"sha256:{record["sha256"]}"')
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def _error(self, error: ApiError) -> None:
        self._json(
            error.status,
            {"error": {"code": error.code, "message": error.message, **error.details}},
        )

    def _read_json(self) -> Any:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "LENGTH_REQUIRED", "Content-Length is required.")
        try:
            length = int(length_header)
        except ValueError as error:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "INVALID_LENGTH", "Content-Length is invalid."
            ) from error
        if length < 0 or length > self.field_server.max_json_bytes:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "The JSON request exceeds the configured size limit.",
            )
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "Malformed JSON.") from error

    def do_GET(self) -> None:
        try:
            self._author_cn()
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok", "time": utc_now()})
                return
            if parsed.path == "/v1/changes":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    cursor = max(0, int(query.get("cursor", ["0"])[0]))
                    limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
                except ValueError as error:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "INVALID_PAGINATION",
                        "cursor and limit must be integers.",
                    ) from error
                self._json(HTTPStatus.OK, self.field_server.store.changes(cursor, limit))
                return
            prefix = "/v1/media/"
            if parsed.path.startswith(prefix) and len(parsed.path) > len(prefix):
                media_id = urllib.parse.unquote(parsed.path[len(prefix) :])
                if "/" in media_id or len(media_id) > 128:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "INVALID_MEDIA_ID",
                        "Invalid media ID.",
                    )
                record, path = self.field_server.store.media_download(media_id)
                self._media(record, path)
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "No such endpoint.")
        except ApiError as error:
            self._error(error)

    def do_POST(self) -> None:
        try:
            author = self._author_cn()
            if self.path == "/v1/mutations":
                mutation = Mutation.from_json(self._read_json())
                self._json(
                    HTTPStatus.OK,
                    self.field_server.store.apply_mutation(mutation, author),
                )
                return
            if self.path == "/v1/published":
                if author not in self.field_server.publisher_cns:
                    raise ApiError(
                        HTTPStatus.FORBIDDEN,
                        "PUBLISHER_REQUIRED",
                        "This client certificate is not authorized to publish read-only records.",
                    )
                record = PublishedRecord.from_json(self._read_json())
                self._json(
                    HTTPStatus.OK,
                    self.field_server.store.publish_record(record, author),
                )
                return
            parsed = urllib.parse.urlparse(self.path)
            participant_prefix = "/guardian/v1/participants/"
            alert_prefix = "/guardian/v1/alerts/"
            if (
                parsed.path.startswith(participant_prefix)
                and parsed.path.endswith("/check-ins")
            ):
                if author not in (
                    self.field_server.guardian_checkin_cns
                    | self.field_server.guardian_supervisor_cns
                ):
                    raise ApiError(
                        HTTPStatus.FORBIDDEN,
                        "GUARDIAN_CHECKIN_REQUIRED",
                        "This client certificate is not authorized for Guardian check-ins.",
                    )
                target_id = urllib.parse.unquote(
                    parsed.path[
                        len(participant_prefix) : -len("/check-ins")
                    ]
                )
                body = self._read_json()
                if not _exact_keys(body, {"observedAt"}):
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "INVALID_GUARDIAN_ACTION",
                        "Check-in body must contain only observedAt.",
                    )
                self._json(
                    HTTPStatus.OK,
                    self.field_server.store.apply_guardian_action(
                        idempotency_key=self.headers.get("Idempotency-Key", ""),
                        action="check_in",
                        target_id=target_id,
                        reason=None,
                        observed_at=body["observedAt"],
                        author_cn=author,
                    ),
                )
                return
            if parsed.path.startswith(alert_prefix) and (
                parsed.path.endswith(":acknowledge")
                or parsed.path.endswith(":resolve")
            ):
                if author not in self.field_server.guardian_supervisor_cns:
                    raise ApiError(
                        HTTPStatus.FORBIDDEN,
                        "GUARDIAN_SUPERVISOR_REQUIRED",
                        "This client certificate is not authorized to manage Guardian alerts.",
                    )
                action = (
                    "acknowledge"
                    if parsed.path.endswith(":acknowledge")
                    else "resolve"
                )
                suffix = f":{action}"
                target_id = urllib.parse.unquote(
                    parsed.path[len(alert_prefix) : -len(suffix)]
                )
                body = self._read_json()
                expected_keys = set() if action == "acknowledge" else {"reason"}
                if not _exact_keys(body, expected_keys):
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "INVALID_GUARDIAN_ACTION",
                        f"Invalid Guardian {action} body.",
                    )
                self._json(
                    HTTPStatus.OK,
                    self.field_server.store.apply_guardian_action(
                        idempotency_key=self.headers.get("Idempotency-Key", ""),
                        action=action,
                        target_id=target_id,
                        reason=body.get("reason"),
                        observed_at=None,
                        author_cn=author,
                    ),
                )
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "No such endpoint.")
        except ApiError as error:
            self._error(error)

    def do_PUT(self) -> None:
        try:
            author = self._author_cn()
            parsed = urllib.parse.urlparse(self.path)
            prefix = "/v1/media/"
            if not parsed.path.startswith(prefix) or len(parsed.path) == len(prefix):
                raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "No such endpoint.")
            media_id = urllib.parse.unquote(parsed.path[len(prefix) :])
            if "/" in media_id or len(media_id) > 128:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_MEDIA_ID", "Invalid media ID.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as error:
                raise ApiError(
                    HTTPStatus.LENGTH_REQUIRED,
                    "LENGTH_REQUIRED",
                    "A valid Content-Length is required.",
                ) from error
            if length < 0 or length > self.field_server.max_media_bytes:
                raise ApiError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "MEDIA_TOO_LARGE",
                    "The media request exceeds the configured size limit.",
                )
            checksum = self.headers.get("X-Aether-Sha256", "").lower()
            if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "CHECKSUM_REQUIRED",
                    "X-Aether-Sha256 must contain a hexadecimal SHA-256 digest.",
                )
            result = self.field_server.store.save_media(
                media_id=media_id,
                content_type=self.headers.get("Content-Type", "application/octet-stream"),
                expected_sha256=checksum,
                observation_id=self.headers.get("X-Aether-Observation-Id"),
                role=self.headers.get("X-Aether-Role"),
                author_cn=author,
                stream=self.rfile,
                content_length=length,
            )
            self._json(HTTPStatus.OK, result)
        except ApiError as error:
            self._error(error)


class AetherFieldServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: FieldStore,
        *,
        max_json_bytes: int,
        max_media_bytes: int,
        publisher_cns: frozenset[str],
        guardian_checkin_cns: frozenset[str],
        guardian_supervisor_cns: frozenset[str],
    ):
        super().__init__(address, AetherFieldHandler)
        self.store = store
        self.max_json_bytes = max_json_bytes
        self.max_media_bytes = max_media_bytes
        self.publisher_cns = publisher_cns
        self.guardian_checkin_cns = guardian_checkin_cns
        self.guardian_supervisor_cns = guardian_supervisor_cns


def build_tls_context(
    cert_file: str,
    key_file: str,
    ca_file: str,
    password_file: str | None = None,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    password = None
    if password_file:
        password = Path(password_file).read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("The TLS key password file is empty.")
    context.load_cert_chain(cert_file, key_file, password=password)
    context.load_verify_locations(cafile=ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    return context


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Initialize storage and exit.")
    args = parser.parse_args()

    database_path = Path(os.environ.get("AETHER_FIELD_DB", "/data/field.sqlite3"))
    media_root = Path(os.environ.get("AETHER_FIELD_MEDIA", "/data/media"))
    store = FieldStore(database_path, media_root)
    if args.check:
        print("Aether Field storage is ready.")
        return

    host = os.environ.get("AETHER_FIELD_HOST", "0.0.0.0")
    port = int(os.environ.get("AETHER_FIELD_PORT", "9443"))
    server = AetherFieldServer(
        (host, port),
        store,
        max_json_bytes=int(os.environ.get("AETHER_FIELD_MAX_JSON_BYTES", str(2 * 1024 * 1024))),
        max_media_bytes=int(
            os.environ.get("AETHER_FIELD_MAX_MEDIA_BYTES", str(512 * 1024 * 1024))
        ),
        publisher_cns=frozenset(
            value.strip()
            for value in os.environ.get("AETHER_FIELD_PUBLISHER_CNS", "Al").split(",")
            if value.strip()
        ),
        guardian_checkin_cns=frozenset(
            value.strip()
            for value in os.environ.get(
                "AETHER_GUARDIAN_CHECKIN_CNS", ""
            ).split(",")
            if value.strip()
        ),
        guardian_supervisor_cns=frozenset(
            value.strip()
            for value in os.environ.get(
                "AETHER_GUARDIAN_SUPERVISOR_CNS", ""
            ).split(",")
            if value.strip()
        ),
    )
    server.socket = build_tls_context(
        os.environ.get("AETHER_FIELD_TLS_CERT", "/certs/server.pem"),
        os.environ.get("AETHER_FIELD_TLS_KEY", "/certs/server.key"),
        os.environ.get("AETHER_FIELD_TLS_CA", "/certs/ca.pem"),
        os.environ.get("AETHER_FIELD_TLS_KEY_PASSWORD_FILE"),
    ).wrap_socket(server.socket, server_side=True)
    print(f"Aether Field API listening on https://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
