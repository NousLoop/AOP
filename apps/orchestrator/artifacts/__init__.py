"""MinIO artifact storage for AOP tasks."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)


DEFAULT_BUCKET = "aop-artifacts"
# Primary human-readable node body (Markdown). Legacy: output.txt.
PRIMARY_TEXT_NAME = "output.md"
PRIMARY_TEXT_MIME = "text/markdown; charset=utf-8"
LEGACY_TEXT_NAMES = ("output.md", "output.txt")  # preference order
_MAX_BUNDLE_BYTES = 15_000_000
_MAX_BUNDLE_FILES = 32
_MAX_LOCAL_ARTIFACT = 2_000_000
_BUNDLE_EXACT = {"report.html", "report.css", "report.pdf", "deck.pptx", "report.pptx"}
_BUNDLE_DIRS = ("assets/images/", "assets/charts/", "assets/diagrams/")
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def is_primary_text_artifact(*, name: str | None = None, uri: str | None = None) -> bool:
    """True for output.md / legacy output.txt (by name or URI suffix)."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base in LEGACY_TEXT_NAMES:
        return True
    if uri:
        u = str(uri).replace("\\", "/").lower().rstrip("/")
        return any(u.endswith(f"/{n}") or u.endswith(n) for n in LEGACY_TEXT_NAMES)
    return False


def safe_bundle_path(name: str) -> str | None:
    """Allow only the report bundle layout. Reject absolute paths and `..`."""
    rel = (name or "").replace("\\", "/").lstrip("/")
    if not rel or rel.endswith("/"):
        return None
    parts = rel.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if rel in _BUNDLE_EXACT:
        return rel
    if any(rel.startswith(prefix) for prefix in _BUNDLE_DIRS):
        return rel
    return None


def decode_bundle_files(files: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    """Turn a report-agent ``files`` map into (relative path, bytes, mime)."""
    decoded: list[tuple[str, bytes, str]] = []
    for raw_name, body in files.items():
        if len(decoded) >= _MAX_BUNDLE_FILES:
            break
        rel = safe_bundle_path(str(raw_name))
        if rel is None:
            continue
        mime = _MIME.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, dict) and body.get("encoding") == "base64":
            try:
                data = base64.b64decode(body.get("content") or "", validate=True)
            except Exception:
                continue
            mime = str(body.get("mime") or mime)
        else:
            continue
        if not data or len(data) > _MAX_BUNDLE_BYTES:
            continue
        decoded.append((rel, data, mime))
    return decoded


def harness_workdir_roots() -> list[Path]:
    """Directories the worker may copy hosted runner files from."""
    roots: list[Path] = []
    raw = (os.getenv("HARNESS_WORKDIR") or "").strip()
    if raw:
        try:
            roots.append(Path(raw).expanduser().resolve())
        except OSError:
            pass
    try:
        roots.append((Path.home() / ".aop" / "workspaces").resolve())
    except OSError:
        pass
    return roots


def safe_hosted_artifact_path(raw: str | None) -> Path | None:
    """Absolute file under a harness workdir root, size-capped."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        path = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_LOCAL_ARTIFACT:
            return None
    except OSError:
        return None
    for root in harness_workdir_roots():
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    return None


def hosted_local_files(data: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    """Copy hosted ``data.artifact`` / ``data.artifacts[].path`` into the object store."""
    out: list[tuple[str, bytes, str]] = []
    seen: set[str] = set()
    candidates: list[str] = []
    art = data.get("artifact")
    if isinstance(art, str):
        candidates.append(art)
    extra = data.get("artifacts")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                p = item.get("path") or item.get("artifact")
                if isinstance(p, str):
                    candidates.append(p)
    for raw in candidates:
        path = safe_hosted_artifact_path(raw)
        if path is None:
            continue
        name = path.name
        if not name or name in seen or name in {".", ".."}:
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        seen.add(name)
        out.append((name, body, mime))
    return out


@dataclass
class ArtifactRef:
    type: str  # text | json | file
    name: str
    uri: str
    mime_type: str
    node_id: str
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore:
    """
    Path convention:
      s3://{bucket}/tasks/{task_id}/{node_key}/{name}
    Public HTTP (dev):
      http://127.0.0.1:9000/{bucket}/tasks/...
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        secure: bool | None = None,
        public_base: str | None = None,
    ):
        self.endpoint = (endpoint or os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")).replace(
            "http://", ""
        ).replace("https://", "")
        self.access_key = access_key or os.getenv("MINIO_ACCESS_KEY", "aopminio")
        self.secret_key = secret_key or os.getenv("MINIO_SECRET_KEY", "aopminio123")
        self.bucket = bucket or os.getenv("MINIO_BUCKET", DEFAULT_BUCKET)
        if secure is None:
            secure = os.getenv("MINIO_SECURE", "false").lower() in {"1", "true", "yes"}
        self.secure = secure
        self.public_base = (
            public_base
            or os.getenv("MINIO_PUBLIC_BASE")
            or f"http://{self.endpoint}/{self.bucket}"
        ).rstrip("/")

        self.client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure,
        )
        self.ensure_bucket()

    def ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        except S3Error as exc:
            # Race or already exists
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        except Exception as exc:  # noqa: BLE001
            # CI/unit tests often run without MinIO; defer failure until upload/download.
            logger.warning("MinIO ensure_bucket skipped (%s): %s", self.endpoint, exc)

    def object_key(self, task_id: str, node_key: str, name: str) -> str:
        safe_name = name.replace("\\", "/").lstrip("/")
        return f"tasks/{task_id}/{node_key}/{safe_name}"

    def s3_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def http_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    def put_bytes(
        self,
        *,
        task_id: str,
        node_key: str,
        name: str,
        data: bytes,
        content_type: str,
        artifact_type: str,
    ) -> ArtifactRef:
        key = self.object_key(task_id, node_key, name)
        from io import BytesIO

        self.client.put_object(
            self.bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return ArtifactRef(
            type=artifact_type,
            name=name,
            uri=self.s3_uri(key),
            mime_type=content_type,
            node_id=node_key,
            size=len(data),
        )

    def put_text(
        self,
        *,
        task_id: str,
        node_key: str,
        name: str,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        artifact_type: str | None = None,
    ) -> ArtifactRef:
        atype = artifact_type or ("markdown" if name.lower().endswith(".md") else "text")
        return self.put_bytes(
            task_id=task_id,
            node_key=node_key,
            name=name,
            data=text.encode("utf-8"),
            content_type=content_type,
            artifact_type=atype,
        )

    def put_json(
        self,
        *,
        task_id: str,
        node_key: str,
        name: str,
        payload: dict[str, Any] | list[Any],
    ) -> ArtifactRef:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self.put_bytes(
            task_id=task_id,
            node_key=node_key,
            name=name,
            data=raw,
            content_type="application/json",
            artifact_type="json",
        )

    def persist_node_output(
        self,
        *,
        task_id: str,
        node_key: str,
        text: str | None,
        data: dict[str, Any] | None,
    ) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        if text:
            refs.append(
                self.put_text(
                    task_id=task_id,
                    node_key=node_key,
                    name=PRIMARY_TEXT_NAME,
                    text=text,
                    content_type=PRIMARY_TEXT_MIME,
                )
            )
        if data is not None:
            payload: dict[str, Any] | list[Any] = data
            if isinstance(data, dict):
                payload = dict(data)
                if isinstance(data.get("files"), dict):
                    index: dict[str, Any] = {}
                    for rel, raw, mime in decode_bundle_files(data["files"]):
                        ref = self.put_bytes(
                            task_id=task_id,
                            node_key=node_key,
                            name=rel,
                            data=raw,
                            content_type=mime,
                            artifact_type="file",
                        )
                        refs.append(ref)
                        index[rel] = {"uri": ref.uri, "size": ref.size, "mime_type": mime}
                    payload["files"] = index
                ingested: dict[str, Any] = {}
                for rel, raw, mime in hosted_local_files(data):
                    if rel in {PRIMARY_TEXT_NAME, "output.json", "meta.json"}:
                        rel = f"hosted-{rel}"
                    ref = self.put_bytes(
                        task_id=task_id,
                        node_key=node_key,
                        name=rel,
                        data=raw,
                        content_type=mime,
                        artifact_type="file",
                    )
                    refs.append(ref)
                    ingested[rel] = {"uri": ref.uri, "size": ref.size, "mime_type": mime}
                if ingested:
                    payload["ingested"] = ingested
            refs.append(
                self.put_json(
                    task_id=task_id,
                    node_key=node_key,
                    name="output.json",
                    payload=payload,
                )
            )
        # Always write a meta sidecar for listing
        meta = {
            "task_id": task_id,
            "node_id": node_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": [r.to_dict() for r in refs],
        }
        refs.append(
            self.put_json(
                task_id=task_id,
                node_key=node_key,
                name="meta.json",
                payload=meta,
            )
        )
        return refs

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = parse_s3_uri(uri, default_bucket=self.bucket)
        resp = self.client.get_object(bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def get_text(self, uri: str) -> str:
        return self.get_bytes(uri).decode("utf-8")

    def get_json(self, uri: str) -> Any:
        return json.loads(self.get_text(uri))

    def list_task_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        prefix = f"tasks/{task_id}/"
        return self._list_prefix(prefix, task_id=task_id)

    def list_all_artifacts(
        self,
        *,
        task_id: str | None = None,
        type_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 500))
        prefix = f"tasks/{task_id}/" if task_id else "tasks/"
        items = self._list_prefix(prefix, task_id=task_id)
        if type_filter:
            tf = type_filter.lower().strip()
            items = [
                i
                for i in items
                if (i.get("type") or "").lower() == tf
                or (i.get("mime_type") or "").lower().find(tf) >= 0
                or (i.get("name") or "").lower().endswith(f".{tf}")
            ]
        # newest first
        items.sort(key=lambda x: x.get("last_modified") or "", reverse=True)
        return items[:limit]

    def _list_prefix(self, prefix: str, *, task_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            objects = self.client.list_objects(self.bucket, prefix=prefix, recursive=True)
        except S3Error:
            return out
        for obj in objects:
            key = obj.object_name
            # tasks/{task_id}/{node}/{name}
            parts = key.split("/")
            tid = task_id or (parts[1] if len(parts) >= 2 else "")
            node_id = parts[2] if len(parts) >= 4 else ""
            name = parts[-1] if parts else key
            if name in ("",):
                continue
            mime = "application/octet-stream"
            if name.endswith(".json"):
                mime = "application/json"
            elif name.endswith(".md"):
                mime = "text/markdown; charset=utf-8"
            elif name.endswith(".txt"):
                mime = "text/plain; charset=utf-8"
            elif name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                mime = "image/" + name.rsplit(".", 1)[-1].replace("jpg", "jpeg")
            elif name.endswith((".mp4", ".webm")):
                mime = "video/" + name.rsplit(".", 1)[-1]
            elif name.endswith(".pdf"):
                mime = "application/pdf"
            elif name.endswith(".pptx"):
                mime = (
                    "application/vnd.openxmlformats-officedocument"
                    ".presentationml.presentation"
                )
            elif name.endswith(".ppt"):
                mime = "application/vnd.ms-powerpoint"
            out.append(
                {
                    "task_id": tid,
                    "node_id": node_id,
                    "name": name,
                    "type": _guess_type(name),
                    "uri": self.s3_uri(key),
                    "url": self.http_url(key),
                    "mime_type": mime,
                    "size": obj.size,
                    "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                }
            )
        return out


def parse_s3_uri(uri: str, *, default_bucket: str = DEFAULT_BUCKET) -> tuple[str, str]:
    if uri.startswith("s3://"):
        parsed = urlparse(uri)
        bucket = parsed.netloc or default_bucket
        key = parsed.path.lstrip("/")
        return bucket, key
    # http(s)://host/bucket/key
    parsed = urlparse(uri)
    path = parsed.path.lstrip("/")
    if "/" not in path:
        raise ValueError(f"invalid artifact uri: {uri}")
    bucket, key = path.split("/", 1)
    return bucket, key


def _guess_type(name: str) -> str:
    if name.endswith(".json"):
        return "json"
    if name.endswith(".md"):
        return "markdown"
    if name.endswith(".txt"):
        return "text"
    if name.endswith((".pptx", ".ppt")):
        return "ppt"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if name.endswith((".mp4", ".webm")):
        return "video"
    return "file"
