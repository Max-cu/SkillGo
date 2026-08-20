from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, insert, select
from sqlalchemy.engine import Connection

from app.database import Base, engine as target_engine
from app import models  # noqa: F401  Ensures every model is registered.


TABLE_ORDER = (
    "users",
    "skills",
    "skill_versions",
    "favorites",
    "endpoints",
    "conversations",
    "runs",
    "conversation_messages",
    "workspace_files",
    "audit_events",
)


def source_engine(path: Path) -> Engine:
    return create_engine(f"sqlite:///{path.resolve().as_posix()}")


def rows(connection: Connection, table_name: str) -> list[dict[str, Any]]:
    table = Base.metadata.tables[table_name]
    return [dict(item) for item in connection.execute(select(table)).mappings()]


def table_counts(engine: Engine) -> dict[str, int | None]:
    existing = set(inspect(engine).get_table_names())
    result: dict[str, int | None] = {}
    with engine.connect() as connection:
        for name in TABLE_ORDER:
            if name not in existing:
                result[name] = None
                continue
            table = Base.metadata.tables[name]
            result[name] = len(connection.execute(select(table.c.id)).all())
    return result


def show_inventory(source: Engine, target: Engine) -> None:
    payload: dict[str, Any] = {
        "source": table_counts(source),
        "target": table_counts(target),
    }
    for label, engine in (("source", source), ("target", target)):
        existing = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            payload[f"{label}_users"] = (
                [
                    {"id": item["id"], "email": item["email"], "role": str(item["role"])}
                    for item in rows(connection, "users")
                ]
                if "users" in existing
                else []
            )
            payload[f"{label}_skills"] = (
                [
                    {"id": item["id"], "slug": item["slug"], "name": item["name"]}
                    for item in rows(connection, "skills")
                ]
                if "skills" in existing
                else []
            )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def copy_storage(source_root: Path, target_root: Path) -> tuple[dict[str, str], int]:
    remapped: dict[str, str] = {}
    copied = 0
    if not source_root.exists():
        return remapped, copied
    migration_prefix = datetime.now(UTC).strftime("migrated/%Y%m%dT%H%M%SZ")
    for source_path in source_root.rglob("*"):
        if not source_path.is_file() or source_path.is_symlink():
            continue
        relative = source_path.relative_to(source_root).as_posix()
        destination = target_root / relative
        if destination.exists():
            if source_path.stat().st_size == destination.stat().st_size and digest(source_path) == digest(destination):
                remapped[relative] = relative
                continue
            migrated_relative = f"{migration_prefix}/{relative}"
            destination = target_root / migrated_relative
            remapped[relative] = migrated_relative
        else:
            remapped[relative] = relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        copied += 1
    return remapped, copied


def new_id(existing: set[str]) -> str:
    candidate = str(uuid.uuid4())
    while candidate in existing:
        candidate = str(uuid.uuid4())
    existing.add(candidate)
    return candidate


def available_id(row: dict[str, Any], existing: set[str]) -> None:
    if row["id"] in existing:
        row["id"] = new_id(existing)
    else:
        existing.add(row["id"])


def unique_value(base: str, occupied: set[str], suffix: str = "legacy") -> str:
    candidate = f"{base}-{suffix}"
    number = 2
    while candidate in occupied:
        candidate = f"{base}-{suffix}-{number}"
        number += 1
    occupied.add(candidate)
    return candidate


def unique_version(base: str, occupied: set[str]) -> str:
    without_build = base.split("+", 1)[0]
    stem = f"{without_build}.legacy" if "-" in without_build else f"{without_build}-legacy"
    candidate = stem
    number = 2
    while candidate in occupied:
        candidate = f"{stem}.{number}"
        number += 1
    occupied.add(candidate)
    return candidate


def migrate(source: Engine, target: Engine, source_storage: Path, target_storage: Path) -> dict[str, Any]:
    existing_source = set(inspect(source).get_table_names())
    missing_required = {"users", "skills", "skill_versions"} - existing_source
    if missing_required:
        raise RuntimeError(f"Source database is missing required tables: {sorted(missing_required)}")

    storage_map, copied_files = copy_storage(source_storage, target_storage)
    report: dict[str, Any] = {
        "inserted": defaultdict(int),
        "merged": defaultdict(int),
        "renamed": [],
        "copied_files": copied_files,
    }
    maps: dict[str, dict[str, str]] = defaultdict(dict)

    with source.connect() as source_connection, target.begin() as target_connection:
        # Users merge by normalized email so the Docker bootstrap owner remains usable.
        user_table = Base.metadata.tables["users"]
        target_users = rows(target_connection, "users")
        user_ids = {item["id"] for item in target_users}
        users_by_email = {item["email"].casefold(): item for item in target_users}
        for original in rows(source_connection, "users"):
            row = dict(original)
            match = users_by_email.get(row["email"].casefold())
            if match:
                maps["users"][original["id"]] = match["id"]
                report["merged"]["users"] += 1
                continue
            available_id(row, user_ids)
            target_connection.execute(insert(user_table).values(**row))
            maps["users"][original["id"]] = row["id"]
            users_by_email[row["email"].casefold()] = row
            report["inserted"]["users"] += 1

        # Skills merge by slug; their versions are merged separately.
        skill_table = Base.metadata.tables["skills"]
        target_skills = rows(target_connection, "skills")
        skill_ids = {item["id"] for item in target_skills}
        skills_by_slug = {item["slug"].casefold(): item for item in target_skills}
        for original in rows(source_connection, "skills"):
            row = dict(original)
            row["owner_id"] = maps["users"][row["owner_id"]]
            match = skills_by_slug.get(row["slug"].casefold())
            if match:
                maps["skills"][original["id"]] = match["id"]
                report["merged"]["skills"] += 1
                continue
            available_id(row, skill_ids)
            target_connection.execute(insert(skill_table).values(**row))
            maps["skills"][original["id"]] = row["id"]
            skills_by_slug[row["slug"].casefold()] = row
            report["inserted"]["skills"] += 1

        version_table = Base.metadata.tables["skill_versions"]
        target_versions = rows(target_connection, "skill_versions")
        version_ids = {item["id"] for item in target_versions}
        versions_by_key = {(item["skill_id"], item["version"]): item for item in target_versions}
        versions_by_skill: dict[str, set[str]] = defaultdict(set)
        for item in target_versions:
            versions_by_skill[item["skill_id"]].add(item["version"])
        for original in rows(source_connection, "skill_versions"):
            row = dict(original)
            row["skill_id"] = maps["skills"][row["skill_id"]]
            row["created_by_id"] = maps["users"][row["created_by_id"]]
            if row.get("reviewed_by_id"):
                row["reviewed_by_id"] = maps["users"][row["reviewed_by_id"]]
            key = (row["skill_id"], row["version"])
            match = versions_by_key.get(key)
            if match and match["package_sha256"] == row["package_sha256"]:
                maps["skill_versions"][original["id"]] = match["id"]
                report["merged"]["skill_versions"] += 1
                continue
            if match:
                old_version = row["version"]
                row["version"] = unique_version(old_version, versions_by_skill[row["skill_id"]])
                report["renamed"].append({"type": "skill_version", "from": old_version, "to": row["version"]})
            available_id(row, version_ids)
            row["package_path"] = storage_map.get(row["package_path"], row["package_path"])
            target_connection.execute(insert(version_table).values(**row))
            maps["skill_versions"][original["id"]] = row["id"]
            versions_by_key[(row["skill_id"], row["version"])] = row
            versions_by_skill[row["skill_id"]].add(row["version"])
            report["inserted"]["skill_versions"] += 1

        if "favorites" in existing_source:
            table = Base.metadata.tables["favorites"]
            target_items = rows(target_connection, "favorites")
            ids = {item["id"] for item in target_items}
            occupied = {(item["user_id"], item["skill_id"]) for item in target_items}
            for original in rows(source_connection, "favorites"):
                row = dict(original)
                row["user_id"] = maps["users"][row["user_id"]]
                row["skill_id"] = maps["skills"][row["skill_id"]]
                key = (row["user_id"], row["skill_id"])
                if key in occupied:
                    report["merged"]["favorites"] += 1
                    continue
                available_id(row, ids)
                target_connection.execute(insert(table).values(**row))
                occupied.add(key)
                report["inserted"]["favorites"] += 1

        if "endpoints" in existing_source:
            table = Base.metadata.tables["endpoints"]
            target_items = rows(target_connection, "endpoints")
            ids = {item["id"] for item in target_items}
            by_slug = {item["slug"].casefold(): item for item in target_items}
            occupied = set(by_slug)
            for original in rows(source_connection, "endpoints"):
                row = dict(original)
                row["owner_id"] = maps["users"][row["owner_id"]]
                row["skill_id"] = maps["skills"][row["skill_id"]]
                row["skill_version_id"] = maps["skill_versions"][row["skill_version_id"]]
                match = by_slug.get(row["slug"].casefold())
                if match and match["skill_version_id"] == row["skill_version_id"]:
                    maps["endpoints"][original["id"]] = match["id"]
                    report["merged"]["endpoints"] += 1
                    continue
                if match:
                    old_slug = row["slug"]
                    row["slug"] = unique_value(old_slug, occupied)
                    report["renamed"].append({"type": "endpoint", "from": old_slug, "to": row["slug"]})
                available_id(row, ids)
                target_connection.execute(insert(table).values(**row))
                maps["endpoints"][original["id"]] = row["id"]
                by_slug[row["slug"].casefold()] = row
                report["inserted"]["endpoints"] += 1

        if "conversations" in existing_source:
            table = Base.metadata.tables["conversations"]
            ids = {item["id"] for item in rows(target_connection, "conversations")}
            for original in rows(source_connection, "conversations"):
                row = dict(original)
                available_id(row, ids)
                row["user_id"] = maps["users"][row["user_id"]]
                row["skill_id"] = maps["skills"][row["skill_id"]]
                row["skill_version_id"] = maps["skill_versions"][row["skill_version_id"]]
                row["is_running"] = False
                row["run_started_at"] = None
                target_connection.execute(insert(table).values(**row))
                maps["conversations"][original["id"]] = row["id"]
                report["inserted"]["conversations"] += 1

        if "runs" in existing_source:
            table = Base.metadata.tables["runs"]
            ids = {item["id"] for item in rows(target_connection, "runs")}
            for original in rows(source_connection, "runs"):
                row = dict(original)
                available_id(row, ids)
                if row.get("user_id"):
                    row["user_id"] = maps["users"][row["user_id"]]
                if row.get("endpoint_id"):
                    row["endpoint_id"] = maps["endpoints"][row["endpoint_id"]]
                row["skill_id"] = maps["skills"][row["skill_id"]]
                row["skill_version_id"] = maps["skill_versions"][row["skill_version_id"]]
                target_connection.execute(insert(table).values(**row))
                maps["runs"][original["id"]] = row["id"]
                report["inserted"]["runs"] += 1

        if "conversation_messages" in existing_source:
            table = Base.metadata.tables["conversation_messages"]
            ids = {item["id"] for item in rows(target_connection, "conversation_messages")}
            for original in rows(source_connection, "conversation_messages"):
                row = dict(original)
                available_id(row, ids)
                row["conversation_id"] = maps["conversations"][row["conversation_id"]]
                if row.get("run_id"):
                    row["run_id"] = maps["runs"][row["run_id"]]
                target_connection.execute(insert(table).values(**row))
                report["inserted"]["conversation_messages"] += 1

        if "workspace_files" in existing_source:
            table = Base.metadata.tables["workspace_files"]
            ids = {item["id"] for item in rows(target_connection, "workspace_files")}
            for original in rows(source_connection, "workspace_files"):
                row = dict(original)
                available_id(row, ids)
                row["user_id"] = maps["users"][row["user_id"]]
                row["conversation_id"] = maps["conversations"][row["conversation_id"]]
                row["storage_path"] = storage_map.get(row["storage_path"], row["storage_path"])
                target_connection.execute(insert(table).values(**row))
                maps["workspace_files"][original["id"]] = row["id"]
                report["inserted"]["workspace_files"] += 1

        if "audit_events" in existing_source:
            table = Base.metadata.tables["audit_events"]
            ids = {item["id"] for item in rows(target_connection, "audit_events")}
            resource_maps = {
                "user": maps["users"],
                "skill": maps["skills"],
                "skill_version": maps["skill_versions"],
                "endpoint": maps["endpoints"],
                "conversation": maps["conversations"],
                "run": maps["runs"],
                "workspace_file": maps["workspace_files"],
            }
            for original in rows(source_connection, "audit_events"):
                row = dict(original)
                available_id(row, ids)
                if row.get("actor_id"):
                    row["actor_id"] = maps["users"].get(row["actor_id"], row["actor_id"])
                resource_map = resource_maps.get(row["resource_type"])
                if resource_map and row.get("resource_id"):
                    row["resource_id"] = resource_map.get(row["resource_id"], row["resource_id"])
                target_connection.execute(insert(table).values(**row))
                report["inserted"]["audit_events"] += 1

    report["inserted"] = dict(report["inserted"])
    report["merged"] = dict(report["merged"])
    report["after"] = table_counts(target)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge a SkillGo SQLite database into PostgreSQL.")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-storage", type=Path, required=True)
    parser.add_argument("--target-storage", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = source_engine(args.source_db)
    if not args.apply:
        show_inventory(source, target_engine)
        return
    report = migrate(source, target_engine, args.source_storage, args.target_storage)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
