"""Controlled Drive A ``90_archive`` cold archive compactor (REQ-231 P0-3).

Safety invariants (Triple Fail-Closed):
1. Default read-only ``plan_only`` mode (``execute=False``): zero remote writes.
2. Active/protected overlap lock: refuses any compaction with zero writes if any
   candidate file or directory intersects active assets, protected zones,
   ``release_status``, ``manifest_rows``, or ``production_policy`` targets.
3. Byte-deterministic ``.tar.gz`` containing internal ``archive_index.json`` (no
   plaintext index uploaded to Drive), with mandatory remote ``sha256Checksum``
   verification before moving source items to Trash (``trashed=True``).
   Hard delete (``files().delete()``) is strictly forbidden and never called.
"""
from __future__ import annotations

import gzip
import io
import tarfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .common import canonical_json_bytes, digest
from .drive import FIELDS, _assert_main_thread_writer

FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_PROTECTED_BUNDLE_RELEASES = ("v2.2.1-1", "v2.2.2-1", "v2.3.0-1")

_COMPRESSED_SUFFIXES = (".tar.gz", ".tgz", ".zip")
_PROTECTED_POLICY_ZONES = ("current", "evidence", "staging", "snowball", "root", "root_folder_id")
_ID_FIELD_NAMES = (
    "id",
    "file_id",
    "fileId",
    "folder_id",
    "folderId",
    "drive_id",
    "driveId",
    "asset_id",
    "target_id",
    "backup_id",
    "bundle_id",
    "bundle_file_id",
    "manifest_id",
    "rollback_id",
)


class ArchiveCompactionError(RuntimeError):
    """Fail-closed error raised when archive compaction cannot proceed safely."""


def _sanitize_tag(tag: str) -> str:
    if not isinstance(tag, str) or not tag.strip():
        raise ArchiveCompactionError("ARCHIVE_COMPACT_INVALID_TAG:empty")
    clean = tag.strip()
    if "/" in clean or "\\" in clean or ".." in clean or "\x00" in clean:
        raise ArchiveCompactionError(f"ARCHIVE_COMPACT_INVALID_TAG:{clean}")
    return "_".join(clean.split())


def _add_str_ids(target: Set[str], value: Any) -> None:
    if isinstance(value, str):
        val = value.strip()
        if val:
            target.add(val)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _add_str_ids(target, item)


def _walk_mapping_for_ids(node: Any, out: Set[str], *, in_asset_container: bool = False) -> None:
    if isinstance(node, Mapping):
        for key, val in node.items():
            key_str = str(key)
            if key_str in _ID_FIELD_NAMES:
                _add_str_ids(out, val)
            elif key_str in (
                "active_asset_ids",
                "protected_ids",
                "protected_folder_ids",
                "file_ids",
                "folder_ids",
                "rollback_ids",
                "backup_ids",
            ):
                _add_str_ids(out, val)
            elif key_str in ("artifacts", "files", "targets", "protected_tables", "tables", "backups", "rollback"):
                _walk_mapping_for_ids(val, out, in_asset_container=True)
            elif in_asset_container and isinstance(val, str):
                _add_str_ids(out, val)
            elif isinstance(val, (Mapping, list, tuple, set)):
                _walk_mapping_for_ids(val, out, in_asset_container=in_asset_container)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            if in_asset_container and isinstance(item, str):
                _add_str_ids(out, item)
            elif isinstance(item, (Mapping, list, tuple, set)):
                _walk_mapping_for_ids(item, out, in_asset_container=in_asset_container)


def _collect_protected_ids(
    *,
    active_asset_ids: Sequence[str] = (),
    protected_folder_ids: Sequence[str] = (),
    release_status: Optional[Mapping[str, Any]] = None,
    manifest_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    production_policy: Optional[Mapping[str, Any]] = None,
) -> Set[str]:
    protected: Set[str] = set()
    _add_str_ids(protected, list(active_asset_ids or ()))
    _add_str_ids(protected, list(protected_folder_ids or ()))

    if isinstance(release_status, Mapping):
        _walk_mapping_for_ids(release_status, protected)

    if manifest_rows:
        for row in manifest_rows:
            if isinstance(row, Mapping):
                for key in _ID_FIELD_NAMES:
                    _add_str_ids(protected, row.get(key))

    if isinstance(production_policy, Mapping):
        _add_str_ids(protected, production_policy.get("root_folder_id"))
        _add_str_ids(protected, production_policy.get("snowball_folder_id"))
        zones = production_policy.get("zones")
        if isinstance(zones, Mapping):
            for zone_key in _PROTECTED_POLICY_ZONES:
                _add_str_ids(protected, zones.get(zone_key))
        for container_key in ("targets", "protected_tables", "tables"):
            if container_key in production_policy:
                _walk_mapping_for_ids(
                    production_policy.get(container_key),
                    protected,
                    in_asset_container=True,
                )

    return protected


def _collect_protected_release_names(
    *,
    keep_release_dirs: Sequence[str],
    previous_release_id: Optional[str],
    release_status: Optional[Mapping[str, Any]],
) -> Set[str]:
    names: Set[str] = set()
    for rel in keep_release_dirs or ():
        if isinstance(rel, str) and rel.strip():
            names.add(rel.strip())
    if isinstance(previous_release_id, str) and previous_release_id.strip():
        names.add(previous_release_id.strip())
    if isinstance(release_status, Mapping):
        for key in ("previous_release_id", "release_id"):
            val = release_status.get(key)
            if isinstance(val, str) and val.strip():
                names.add(val.strip())
    return names


def _is_compressed_archive_name(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in _COMPRESSED_SUFFIXES)


def _trash_drive_object(drive: Any, file_id: str) -> None:
    """Move a Drive object to Trash via trashed=True. Never calls delete()."""
    trash_fn = getattr(drive, "trash", None)
    if callable(trash_fn):
        trash_fn(file_id)
        return
    svc = getattr(drive, "api", None) or getattr(drive, "_svc", None)
    if svc is None:
        raise ArchiveCompactionError(f"ARCHIVE_COMPACT_TRASH_UNAVAILABLE:{file_id}")
    svc.files().update(
        fileId=file_id,
        body={"trashed": True},
        fields=FIELDS,
        supportsAllDrives=True,
    ).execute(num_retries=0)


def _build_deterministic_tar_gz(members: Sequence[Tuple[str, bytes]]) -> bytes:
    """Build a byte-deterministic gzip-compressed tar archive."""
    raw_buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw_buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for arcname, payload in sorted(members, key=lambda pair: pair[0]):
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(payload)
                ti.mtime = 0
                ti.uid = 0
                ti.gid = 0
                ti.uname = ""
                ti.gname = ""
                ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(payload))
    return raw_buf.getvalue()


def compact_drive_archive(
    drive: Any,
    *,
    history_folder_id: str,
    active_asset_ids: Sequence[str] = (),
    protected_folder_ids: Sequence[str] = (),
    previous_release_id: Optional[str] = None,
    keep_release_dirs: Sequence[str] = DEFAULT_PROTECTED_BUNDLE_RELEASES,
    execute: bool = False,
    tag: str = "v231",
    release_status: Optional[Mapping[str, Any]] = None,
    manifest_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    production_policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Plan or execute controlled cold compaction of Drive A ``90_archive``."""
    if not isinstance(history_folder_id, str) or not history_folder_id.strip():
        raise ArchiveCompactionError("ARCHIVE_COMPACT_HISTORY_FOLDER_REQUIRED")
    root_history_id = history_folder_id.strip()
    clean_tag = _sanitize_tag(tag)
    bundle_name = f"cold_archive_legacy_releases_{clean_tag}.tar.gz"

    if execute:
        _assert_main_thread_writer(drive, "compact_drive_archive")

    protected_ids = _collect_protected_ids(
        active_asset_ids=active_asset_ids,
        protected_folder_ids=protected_folder_ids,
        release_status=release_status,
        manifest_rows=manifest_rows,
        production_policy=production_policy,
    )
    if root_history_id in protected_ids:
        raise ArchiveCompactionError(
            f"ARCHIVE_COMPACT_ACTIVE_OVERLAP:history_folder_id={root_history_id}"
        )

    protected_release_names = _collect_protected_release_names(
        keep_release_dirs=keep_release_dirs,
        previous_release_id=previous_release_id,
        release_status=release_status,
    )

    candidate_files: List[Dict[str, Any]] = []
    candidate_dirs: List[Dict[str, Any]] = []
    top_level_trash_targets: List[str] = []
    skipped_compressed: List[str] = []
    skipped_protected_dirs: List[str] = []
    visited_dirs: Set[str] = {root_history_id}

    def _walk_legacy_dir(folder_id: str, prefix: str) -> None:
        children = sorted(
            (item for item in drive.list(folder_id) if isinstance(item, Mapping)),
            key=lambda m: (str(m.get("name") or ""), str(m.get("id") or "")),
        )
        for child in children:
            if child.get("trashed", False):
                continue
            cid = str(child.get("id") or "").strip()
            cname = str(child.get("name") or "").strip()
            cmime = str(child.get("mimeType") or "")
            if not cid or not cname:
                continue
            rel_path = f"{prefix}/{cname}" if prefix else cname
            if cmime == FOLDER_MIME:
                if cid in visited_dirs:
                    continue
                visited_dirs.add(cid)
                candidate_dirs.append({"id": cid, "name": cname, "logical_path": rel_path})
                _walk_legacy_dir(cid, rel_path)
            else:
                candidate_files.append(
                    {
                        "id": cid,
                        "name": cname,
                        "logical_path": rel_path,
                        "mimeType": cmime,
                        "modifiedTime": str(child.get("modifiedTime") or ""),
                        "sha256Checksum": child.get("sha256Checksum"),
                    }
                )

    top_items = sorted(
        (item for item in drive.list(root_history_id) if isinstance(item, Mapping)),
        key=lambda m: (str(m.get("name") or ""), str(m.get("id") or "")),
    )
    for item in top_items:
        if item.get("trashed", False):
            continue
        fid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        mime = str(item.get("mimeType") or "")
        if not fid or not name:
            continue
        if mime == FOLDER_MIME:
            if name in protected_release_names:
                skipped_protected_dirs.append(name)
                continue
            visited_dirs.add(fid)
            candidate_dirs.append({"id": fid, "name": name, "logical_path": name})
            top_level_trash_targets.append(fid)
            _walk_legacy_dir(fid, name)
        else:
            if _is_compressed_archive_name(name):
                skipped_compressed.append(name)
                continue
            candidate_files.append(
                {
                    "id": fid,
                    "name": name,
                    "logical_path": name,
                    "mimeType": mime,
                    "modifiedTime": str(item.get("modifiedTime") or ""),
                    "sha256Checksum": item.get("sha256Checksum"),
                }
            )
            top_level_trash_targets.append(fid)

    candidate_ids = sorted(
        {c["id"] for c in candidate_files} | {d["id"] for d in candidate_dirs}
    )

    # Lock 1: Fail-Closed Active/Protected Overlap Check (0 writes)
    overlap = sorted(set(candidate_ids) & protected_ids)
    if overlap:
        raise ArchiveCompactionError(
            f"ARCHIVE_COMPACT_ACTIVE_OVERLAP:count={len(overlap)}:ids={','.join(overlap[:5])}"
        )

    plan_result: Dict[str, Any] = {
        "mode": "execute" if execute else "plan_only",
        "tag": clean_tag,
        "bundle_name": bundle_name,
        "candidate_ids": candidate_ids,
        "candidate_file_count": len(candidate_files),
        "candidate_dir_count": len(candidate_dirs),
        "skipped_compressed_count": len(skipped_compressed),
        "skipped_protected_dir_count": len(skipped_protected_dirs),
        "uploaded_bundles": [],
        "trashed_ids": [],
        "trashed_count": 0,
    }

    if not execute:
        return plan_result

    if not candidate_files and not candidate_dirs:
        return plan_result

    # Lock 2: Deterministic Pack + Internal archive_index.json + Remote sha256Checksum Verify
    tar_members: List[Tuple[str, bytes]] = []
    index_entries: List[Dict[str, Any]] = []

    for fmeta in sorted(candidate_files, key=lambda m: (m["logical_path"], m["id"])):
        mime = fmeta["mimeType"]
        if mime.startswith("application/vnd.google-apps."):
            raise ArchiveCompactionError(
                f"ARCHIVE_COMPACT_UNSUPPORTED_NATIVE_OBJECT:{fmeta['logical_path']}"
            )
        raw_bytes = bytes(drive.get(fmeta["id"]))
        actual_sha = digest(raw_bytes)
        declared_sha = fmeta.get("sha256Checksum")
        if isinstance(declared_sha, str) and declared_sha.strip():
            if actual_sha != declared_sha.strip().lower():
                raise ArchiveCompactionError(
                    f"ARCHIVE_COMPACT_SOURCE_SHA256_MISMATCH:{fmeta['logical_path']}"
                )
        arcname = f"payload/{fmeta['logical_path']}"
        tar_members.append((arcname, raw_bytes))
        index_entries.append(
            {
                "logical_path": fmeta["logical_path"],
                "archive_path": arcname,
                "name": fmeta["name"],
                "mime_type": mime,
                "size_bytes": len(raw_bytes),
                "sha256": actual_sha,
                "modified_time": fmeta["modifiedTime"],
            }
        )

    index_doc = {
        "schema_version": 1,
        "bundle_name": bundle_name,
        "tag": clean_tag,
        "file_count": len(index_entries),
        "entries": index_entries,
    }
    tar_members.append(("archive_index.json", canonical_json_bytes(index_doc, trailing_newline=True)))

    bundle_bytes = _build_deterministic_tar_gz(tar_members)
    local_bundle_sha = digest(bundle_bytes)

    created = drive.create_binary(root_history_id, bundle_name, bundle_bytes, "application/gzip")
    uploaded_id = str(created.get("id") or "").strip() if isinstance(created, Mapping) else ""
    if not uploaded_id:
        raise ArchiveCompactionError("ARCHIVE_COMPACT_UPLOAD_ID_MISSING")

    remote_meta = drive.meta(uploaded_id) if callable(getattr(drive, "meta", None)) else created
    remote_sha = str(
        (remote_meta.get("sha256Checksum") if isinstance(remote_meta, Mapping) else "") or ""
    ).strip().lower()
    if remote_sha != local_bundle_sha:
        raise ArchiveCompactionError(
            f"ARCHIVE_COMPACT_REMOTE_SHA256_MISMATCH:expected={local_bundle_sha}:actual={remote_sha or 'missing'}"
        )

    # Lock 3: Trash-Only (Never Hard Delete)
    trashed_ids: List[str] = []
    for target_id in top_level_trash_targets:
        _trash_drive_object(drive, target_id)
        trashed_ids.append(target_id)

    plan_result["uploaded_bundles"] = [
        {
            "file_id": uploaded_id,
            "name": bundle_name,
            "sha256": local_bundle_sha,
            "size_bytes": len(bundle_bytes),
            "file_count": len(index_entries),
        }
    ]
    plan_result["trashed_ids"] = trashed_ids
    plan_result["trashed_count"] = len(trashed_ids)
    return plan_result
