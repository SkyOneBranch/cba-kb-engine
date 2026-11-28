"""Per-club sharded cloud bundle builder & <1s loader (REQ-251 P0-E3).

100% Python stdlib.
Builds and loads:
- `shard_manifest.json` (`cba-kb.shard-manifest.v1`)
- `shared_core.tar.gz` (cross-club `person_registry.json`, `organization_profiles.json`,
  `person_bio_profiles.json`, `operational_checkpoint.json`, `coverage_ledger.json`, etc.)
- `shards/<org_id>.tar.gz` (per-club stats, roster ledger, stories, social, club_pack/context)
Enforces SHA-256 verification, path traversal/symlink rejection, and zero secret/ACL/audio-video.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .common import atomic

SHARD_MANIFEST_SCHEMA = "cba-kb.shard-manifest.v1"
_FORBIDDEN_KEYWORDS = ("mcp_acl", "secret", "token")
_FORBIDDEN_MEDIA_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".mp3", ".wav", ".m4a", ".aac")


def _is_forbidden_shard_member(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    low = norm.lower()
    if any(low.endswith(ext) for ext in _FORBIDDEN_MEDIA_EXTS):
        return True
    parts = [p.lower() for p in PurePosixPath(norm).parts if p and p != "."]
    if any(kw in part for part in parts for kw in _FORBIDDEN_KEYWORDS):
        return True
    if any(p in ("evidence", "inbox", "raw", ".credentials", ".git", "__pycache__") for p in parts[:-1]):
        return True
    return False


def _write_deterministic_tar_gz(dest_path: Path, members: Sequence[Tuple[str, bytes]]) -> str:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for rel, data in sorted(members, key=lambda x: x[0]):
                ti = tarfile.TarInfo(name=rel)
                ti.size = len(data)
                ti.mtime = 0
                ti.uid = 0
                ti.gid = 0
                ti.mode = 0o644
                ti.type = tarfile.REGTYPE
                tf.addfile(ti, io.BytesIO(data))
    payload = buf.getvalue()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic(dest_path, payload)
    return hashlib.sha256(payload).hexdigest()


def _read_tar_gz_verified(tar_path: Path, expected_sha256: Optional[str] = None) -> Dict[str, bytes]:
    raw = tar_path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and actual_sha != expected_sha256.strip().lower():
        raise ValueError(f"SHARD_SHA256_MISMATCH:{tar_path.name}")

    files: Dict[str, bytes] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r|") as tf:
            for member in tf:
                name = member.name
                if not name or "\x00" in name or name.startswith("/") or "\\" in name:
                    raise ValueError(f"UNSAFE_SHARD_MEMBER:{name!r}")
                parts = PurePosixPath(name).parts
                if ".." in parts:
                    raise ValueError(f"PATH_TRAVERSAL_IN_SHARD:{name!r}")
                if member.issym() or member.islnk():
                    raise ValueError(f"SYMLINK_IN_SHARD:{name!r}")
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ValueError(f"UNSAFE_SHARD_MEMBER_TYPE:{name!r}")
                clean_rel = "/".join(p for p in parts if p not in ("", "."))
                if _is_forbidden_shard_member(clean_rel):
                    raise ValueError(f"FORBIDDEN_SHARD_MEMBER:{clean_rel!r}")
                fobj = tf.extractfile(member)
                files[clean_rel] = fobj.read() if fobj is not None else b""
    return files


def _build_org_alias_map(source_dir: Path) -> Dict[str, Set[str]]:
    org_file = source_dir / "organization_profiles.json"
    alias_map: Dict[str, Set[str]] = {}
    if org_file.is_file():
        parsed = json.loads(org_file.read_text(encoding="utf-8"))
        orgs = parsed.get("organizations") if isinstance(parsed, dict) else parsed
        if isinstance(orgs, list):
            for o in orgs:
                if not isinstance(o, Mapping):
                    continue
                oid = str(o.get("organization_id") or "").strip()
                if not oid:
                    continue
                tokens = {
                    str(x).strip().casefold()
                    for x in [oid, o.get("canonical_name"), o.get("short_name"), *(o.get("aliases") or ())]
                    if x and str(x).strip()
                }
                alias_map[oid] = tokens
    clubs_dir = source_dir / "clubs"
    if clubs_dir.is_dir():
        for sub in sorted(clubs_dir.iterdir()):
            if sub.is_dir():
                alias_map.setdefault(sub.name, {sub.name.casefold()})
    return alias_map


def _row_belongs_to_org(row: Mapping[str, Any], org_id: str, tokens: Set[str]) -> bool:
    org_ids = row.get("organization_ids")
    if isinstance(org_ids, list) and org_id in org_ids:
        return True
    for k in ("organization_id", "club", "club_official", "club_name"):
        val = str(row.get(k) or "").strip().casefold()
        if val and val in tokens:
            return True
    return False


def build_sharded_bundle(source_dir: Path | str, output_dir: Path | str) -> Dict[str, Any]:
    """Build `shard_manifest.json` + `shared_core.tar.gz` + `shards/<org_id>.tar.gz`."""
    src = Path(source_dir)
    out = Path(output_dir)
    if not src.is_dir():
        raise ValueError(f"SOURCE_DIR_NOT_FOUND:{src}")

    org_aliases = _build_org_alias_map(src)
    shared_members: List[Tuple[str, bytes]] = []
    shard_members: Dict[str, List[Tuple[str, bytes]]] = {oid: [] for oid in org_aliases}

    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(src).as_posix()
        if _is_forbidden_shard_member(rel):
            continue
        parts = PurePosixPath(rel).parts
        if len(parts) >= 3 and parts[0] == "clubs" and parts[1] in shard_members:
            shard_members[parts[1]].append((rel, path.read_bytes()))
        else:
            shared_members.append((rel, path.read_bytes()))

    core_tar_path = out / "shared_core.tar.gz"
    core_sha = _write_deterministic_tar_gz(core_tar_path, shared_members)

    shards_meta: Dict[str, Dict[str, Any]] = {}
    for oid in sorted(shard_members):
        shard_rel = f"shards/{oid}.tar.gz"
        s_sha = _write_deterministic_tar_gz(out / shard_rel, shard_members[oid])
        shards_meta[oid] = {
            "path": shard_rel,
            "sha256": s_sha,
            "member_count": len(shard_members[oid]),
        }

    manifest: Dict[str, Any] = {
        "schema": SHARD_MANIFEST_SCHEMA,
        "shared_core": {
            "path": "shared_core.tar.gz",
            "sha256": core_sha,
            "member_count": len(shared_members),
        },
        "shards": shards_meta,
    }
    atomic(
        out / "shard_manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return manifest


def load_sharded_file_map(
    shard_dir: Path | str,
    *,
    clubs: Optional[Sequence[str]] = None,
) -> Dict[str, bytes]:
    """Load `shared_core.tar.gz` and selected club shards into an in-memory file map."""
    root = Path(shard_dir)
    manifest_path = root / "shard_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"SHARD_MANIFEST_NOT_FOUND:{root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SHARD_MANIFEST_SCHEMA:
        raise ValueError(f"INVALID_SHARD_MANIFEST_SCHEMA:{manifest.get('schema')}")

    core_meta = manifest["shared_core"]
    files = _read_tar_gz_verified(root / core_meta["path"], expected_sha256=core_meta.get("sha256"))

    shards_meta = manifest.get("shards") or {}
    target_clubs = list(clubs) if clubs else sorted(shards_meta.keys())
    for cid in target_clubs:
        if cid not in shards_meta:
            raise ValueError(f"CLUB_SHARD_NOT_FOUND:{cid}")
        s_meta = shards_meta[cid]
        shard_files = _read_tar_gz_verified(root / s_meta["path"], expected_sha256=s_meta.get("sha256"))
        files.update(shard_files)
    return files
