"""
Object storage for evidence / report frames.

Primary: Cloudflare R2 (S3-compatible API + public URL base).
Fallback: Backblaze B2 (legacy) when R2 is not configured.

R2 env (all required for R2 uploads):
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET_NAME
  R2_ENDPOINT_URL          e.g. https://<accountid>.r2.cloudflarestorage.com
  R2_PUBLIC_BASE_URL       e.g. https://pub-xxxxx.r2.dev  (no trailing slash)
  R2_EVIDENCE_PREFIX       optional, default evidence/frames

Legacy B2 (optional fallback):
  B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME
  B2_EVIDENCE_PREFIX, B2_CUSTOM_DOMAIN
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Avoid repeated R2/B2 uploads of the same local file in one process (same path → same blob key).
_evidence_upload_cache: dict[str, str] = {}
_evidence_upload_lock = threading.Lock()

R2_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
R2_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"
R2_BUCKET_NAME = "R2_BUCKET_NAME"
R2_ENDPOINT_URL = "R2_ENDPOINT_URL"
R2_PUBLIC_BASE_URL = "R2_PUBLIC_BASE_URL"
R2_EVIDENCE_PREFIX = "R2_EVIDENCE_PREFIX"
R2_REPORTS_PREFIX = "R2_REPORTS_PREFIX"

B2_KEY_ID = "B2_KEY_ID"
B2_APPLICATION_KEY = "B2_APPLICATION_KEY"
B2_BUCKET_NAME = "B2_BUCKET_NAME"
B2_EVIDENCE_PREFIX = "B2_EVIDENCE_PREFIX"
B2_CUSTOM_DOMAIN = "B2_CUSTOM_DOMAIN"
B2_REPORTS_PREFIX = "B2_REPORTS_PREFIX"

EVIDENCE_DOWNLOADS_DIR = Path("uploads/downloads/evidence")


def _content_type_for_path(path: Path) -> str:
    guessed, _enc = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _build_public_url(base: str, remote_key: str) -> str:
    return f"{base.rstrip('/')}/{remote_key.lstrip('/')}"


def _get_prefix(kind: str) -> str:
    kind = (kind or "").strip().lower()
    if kind == "report":
        return os.getenv(R2_REPORTS_PREFIX, "reports").strip().strip("/")
    return os.getenv(R2_EVIDENCE_PREFIX, "evidence/frames").strip().strip("/")


def _is_r2_configured() -> bool:
    return bool(
        os.getenv(R2_ACCESS_KEY_ID)
        and os.getenv(R2_SECRET_ACCESS_KEY)
        and os.getenv(R2_BUCKET_NAME)
        and os.getenv(R2_ENDPOINT_URL)
        and os.getenv(R2_PUBLIC_BASE_URL)
    )


def _is_b2_configured() -> bool:
    return bool(
        os.getenv(B2_KEY_ID)
        and os.getenv(B2_APPLICATION_KEY)
        and os.getenv(B2_BUCKET_NAME)
    )


def _upload_r2(path: Path, *, kind: str = "evidence") -> Optional[str]:
    key_id = os.getenv(R2_ACCESS_KEY_ID, "").strip()
    secret = os.getenv(R2_SECRET_ACCESS_KEY, "").strip()
    bucket = os.getenv(R2_BUCKET_NAME, "").strip()
    endpoint = os.getenv(R2_ENDPOINT_URL, "").strip().rstrip("/")
    public_base = os.getenv(R2_PUBLIC_BASE_URL, "").strip().rstrip("/")
    prefix = _get_prefix(kind)
    content_type = _content_type_for_path(path)

    remote_key = f"{prefix}/{path.name}" if prefix else path.name

    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )
        client.upload_file(
            str(path),
            bucket,
            remote_key,
            ExtraArgs={"ContentType": content_type},
        )
        url = _build_public_url(public_base, remote_key)
        logger.info("Uploaded %s to R2: %s", kind, remote_key)
        return url
    except ImportError:
        logger.warning("boto3 not installed. Run: pip install boto3")
        return None
    except Exception as e:
        logger.warning("R2 upload failed for %s: %s", path, e)
        return None


def _upload_b2(path: Path, *, kind: str = "evidence") -> Optional[str]:
    if not _is_b2_configured():
        return None
    try:
        import b2sdk.v2 as b2

        key_id = os.getenv(B2_KEY_ID)
        app_key = os.getenv(B2_APPLICATION_KEY)
        bucket_name = os.getenv(B2_BUCKET_NAME)
        prefix = (
            os.getenv(B2_REPORTS_PREFIX, "reports").rstrip("/")
            if kind == "report"
            else os.getenv(B2_EVIDENCE_PREFIX, "evidence/frames").rstrip("/")
        )
        custom_domain = os.getenv(B2_CUSTOM_DOMAIN)

        remote_name = f"{prefix}/{path.name}"

        info = b2.InMemoryAccountInfo()
        api = b2.B2Api(info)
        api.authorize_account("production", key_id, app_key)
        bucket = api.get_bucket_by_name(bucket_name)

        bucket.upload_local_file(
            local_file=str(path),
            file_name=remote_name,
            content_type=_content_type_for_path(path),
        )

        if custom_domain:
            url = f"{custom_domain.rstrip('/')}/{remote_name}"
        else:
            url = api.get_download_url_for_file_name(bucket_name, remote_name)

        logger.info("Uploaded %s to B2: %s", kind, remote_name)
        return url

    except ImportError:
        logger.warning("b2sdk not installed. Run: pip install b2sdk")
        return None
    except Exception as e:
        logger.warning("B2 upload failed for %s: %s", path, e)
        return None


def upload_evidence_frame(local_path: str | Path) -> Optional[str]:
    """
    Upload a local image to R2 (preferred) or B2. Returns a public HTTPS URL, or None.

    Repeated calls for the same resolved path reuse the cached URL (no duplicate upload).
    """
    path = Path(local_path)
    if not path.exists() or not path.is_file():
        logger.warning("Evidence frame not found: %s", local_path)
        return None

    cache_key = str(path.resolve())
    with _evidence_upload_lock:
        if cache_key in _evidence_upload_cache:
            logger.debug(
                "Evidence upload cache hit, skipping duplicate: %s", cache_key
            )
            return _evidence_upload_cache[cache_key]

        url: Optional[str] = None
        if _is_r2_configured():
            url = _upload_r2(path, kind="evidence")
        if not url and _is_b2_configured():
            url = _upload_b2(path, kind="evidence")

        if url:
            _evidence_upload_cache[cache_key] = url
        return url


def upload_report_file(local_path: str | Path) -> Optional[str]:
    """Upload a generated report file to R2 (preferred) or B2."""
    path = Path(local_path)
    if not path.exists() or not path.is_file():
        logger.warning("Report file not found: %s", local_path)
        return None

    if _is_r2_configured():
        url = _upload_r2(path, kind="report")
        if url:
            return url

    if _is_b2_configured():
        return _upload_b2(path, kind="report")

    return None


def _is_remote_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


def _ensure_downloads_dir() -> Path:
    EVIDENCE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DOWNLOADS_DIR


def _parse_b2_url(url: str) -> Optional[tuple[str, str]]:
    if not url or "backblazeb2.com/file/" not in url:
        return None
    try:
        parts = url.split("/file/", 1)[-1].split("/", 1)
        if len(parts) != 2:
            return None
        bucket_name, file_name = parts[0], parts[1]
        if not bucket_name or not file_name:
            return None
        return (bucket_name, file_name)
    except Exception:
        return None


def _download_from_b2(bucket_name: str, file_name: str, save_path: Path) -> bool:
    if not _is_b2_configured():
        return False
    try:
        import b2sdk.v2 as b2

        key_id = os.getenv(B2_KEY_ID)
        app_key = os.getenv(B2_APPLICATION_KEY)
        info = b2.InMemoryAccountInfo()
        api = b2.B2Api(info)
        api.authorize_account("production", key_id, app_key)
        bucket = api.get_bucket_by_name(bucket_name)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = bucket.download_file_by_name(file_name)
        downloaded.save_to(save_path)
        logger.info("Downloaded from B2: %s/%s -> %s", bucket_name, file_name, save_path.name)
        return True
    except Exception as e:
        logger.warning("B2 download failed for %s/%s: %s", bucket_name, file_name, e)
        return False


def _download_via_http(url: str, save_path: Path) -> bool:
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "ForeSyte-Report/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(data)
        return True
    except Exception as e:
        logger.warning("HTTP download failed for %s: %s", url[:80], e)
        return False


def get_report_blob_url(filename: str) -> Optional[str]:
    filename = Path(str(filename or "")).name
    if not filename:
        return None
    if _is_r2_configured():
        public_base = os.getenv(R2_PUBLIC_BASE_URL, "").strip().rstrip("/")
        prefix = os.getenv(R2_REPORTS_PREFIX, "reports").strip().strip("/")
        key = f"{prefix}/{filename}" if prefix else filename
        return _build_public_url(public_base, key)
    if _is_b2_configured():
        prefix = os.getenv(B2_REPORTS_PREFIX, "reports").strip().strip("/")
        key = f"{prefix}/{filename}" if prefix else filename
        custom_domain = os.getenv(B2_CUSTOM_DOMAIN, "").strip()
        if custom_domain:
            return _build_public_url(custom_domain, key)
        bucket = os.getenv(B2_BUCKET_NAME, "").strip()
        if bucket:
            return f"https://f005.backblazeb2.com/file/{bucket}/{key}"
    return None


def download_report_bytes(filename: str) -> Optional[tuple[bytes, str]]:
    """Download report content from blob storage, preferring R2 then B2."""
    filename = Path(str(filename or "")).name
    if not filename:
        return None

    if _is_r2_configured():
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            prefix = os.getenv(R2_REPORTS_PREFIX, "reports").strip().strip("/")
            key = f"{prefix}/{filename}" if prefix else filename
            client = boto3.client(
                "s3",
                endpoint_url=os.getenv(R2_ENDPOINT_URL, "").strip().rstrip("/"),
                aws_access_key_id=os.getenv(R2_ACCESS_KEY_ID, "").strip(),
                aws_secret_access_key=os.getenv(R2_SECRET_ACCESS_KEY, "").strip(),
                config=BotoConfig(signature_version="s3v4"),
                region_name="auto",
            )
            obj = client.get_object(Bucket=os.getenv(R2_BUCKET_NAME, "").strip(), Key=key)
            data = obj["Body"].read()
            content_type = str(obj.get("ContentType") or _content_type_for_path(Path(filename)))
            return (data, content_type)
        except Exception as e:
            logger.warning("R2 report download failed for %s: %s", filename, e)

    if _is_b2_configured():
        try:
            import b2sdk.v2 as b2
            import tempfile

            prefix = os.getenv(B2_REPORTS_PREFIX, "reports").strip().strip("/")
            key = f"{prefix}/{filename}" if prefix else filename
            info = b2.InMemoryAccountInfo()
            api = b2.B2Api(info)
            api.authorize_account(
                "production",
                os.getenv(B2_KEY_ID, "").strip(),
                os.getenv(B2_APPLICATION_KEY, "").strip(),
            )
            bucket = api.get_bucket_by_name(os.getenv(B2_BUCKET_NAME, "").strip())
            downloader = bucket.download_file_by_name(key)
            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as tmp:
                tmp_path = Path(tmp.name)
            try:
                downloader.save_to(tmp_path)
                return (tmp_path.read_bytes(), _content_type_for_path(Path(filename)))
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("B2 report download failed for %s: %s", filename, e)

    return None


def prepare_evidence_files_for_report(
    report_id: str,
    evidence_urls: list[str],
) -> dict[str, str]:
    """
    Download remote evidence images to local disk for PDF embedding.
    Public R2 URLs are fetched via HTTP; legacy B2 URLs may use the SDK.
    """
    out_dir = _ensure_downloads_dir()
    file_map: dict[str, str] = {}
    seen_urls: set[str] = set()

    for idx, orig_url in enumerate(evidence_urls):
        if not orig_url or orig_url in ("N/A", ""):
            continue
        orig_url = str(orig_url).strip()

        if orig_url.startswith("/uploads/"):
            local_path = Path(orig_url.lstrip("/"))
            if local_path.exists():
                file_map[orig_url] = str(local_path)
            continue

        if _is_remote_url(orig_url):
            if orig_url in seen_urls:
                continue
            seen_urls.add(orig_url)
            ext = ".jpg"
            if "." in orig_url.split("?")[0]:
                ext = "." + orig_url.split("?")[0].rsplit(".", 1)[-1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            safe_id = str(report_id).replace("-", "_")
            filename = f"evidence_{safe_id}_{idx}{ext}"
            local_path = out_dir / filename
            downloaded = False
            b2_parsed = _parse_b2_url(orig_url)
            if b2_parsed:
                bucket_name, file_name = b2_parsed
                downloaded = _download_from_b2(bucket_name, file_name, local_path)
            if not downloaded:
                downloaded = _download_via_http(orig_url, local_path)
            if downloaded and local_path.exists():
                file_map[orig_url] = str(local_path)

    return file_map
