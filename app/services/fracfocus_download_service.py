import logging
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from app.core.config import Settings, _ZIP_ALLOWED_HOSTS

log = logging.getLogger(__name__)


class DownloadService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _ZIP_ALLOWED_HOSTS:
            raise ValueError(
                f"Refusing request to {url!r}: must be https on {_ZIP_ALLOWED_HOSTS}"
            )

    def check_remote_changed(
        self,
        url: str,
        known_etag: Optional[str],
        known_last_modified: Optional[str],
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Sends a HEAD request to check if the remote file has changed.
        Returns (changed, new_etag, new_last_modified).
        Conservatively returns changed=True when headers are absent or request fails.
        """
        self._validate_url(url)
        try:
            response = requests.head(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning(f"HEAD request failed ({exc}). Assuming remote changed.")
            return True, None, None

        new_etag = response.headers.get("ETag")
        new_last_modified = response.headers.get("Last-Modified")

        if not new_etag and not new_last_modified:
            log.warning("Remote returned no ETag or Last-Modified — assuming changed.")
            return True, new_etag, new_last_modified

        changed = (new_etag != known_etag) or (new_last_modified != known_last_modified)
        log.info(
            f"Remote change check: changed={changed} | "
            f"ETag {known_etag!r} → {new_etag!r} | "
            f"Last-Modified {known_last_modified!r} → {new_last_modified!r}"
        )
        return changed, new_etag, new_last_modified

    def _validate_dest_path(self, dest_path: Path) -> None:
        base = Path(self.settings.EXTRACT_DIR).resolve().parent
        if not dest_path.resolve().is_relative_to(base):
            raise ValueError(
                f"Refusing to write to {dest_path!r}: path is outside the configured data directory"
            )

    def stream_download_to_disk(self, url: str, dest_path: Path) -> Path:
        """Downloads url to dest_path using streaming to avoid loading the whole file into memory."""
        self._validate_url(url)
        self._validate_dest_path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"Streaming download: {url} → {dest_path}")

        response = requests.get(url, stream=True, timeout=self.settings.REQUEST_TIMEOUT)
        response.raise_for_status()

        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=self.settings.DOWNLOAD_CHUNK_SIZE):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(
                        f"\r  Download: {pct:.1f}%"
                        f" ({downloaded // 1_000_000}MB / {total // 1_000_000}MB)",
                        end="",
                    )
        print()
        log.info(f"Download complete: {dest_path} ({downloaded // 1_000_000} MB)")
        return dest_path

    def read_zip_csv_infos(self, zip_path: Path) -> list[zipfile.ZipInfo]:
        """
        Reads the ZIP central directory and returns ZipInfo for every .csv entry.
        No decompression happens — only metadata is read.
        """
        with zipfile.ZipFile(zip_path, "r") as zf:
            return [info for info in zf.infolist() if info.filename.lower().endswith(".csv")]

    def extract_files(
        self, zip_path: Path, filenames: list[str], dest_dir: Path
    ) -> list[Path]:
        """Extracts only the listed filenames from the ZIP to dest_dir."""
        dest_dir = dest_dir.resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in filenames:
                dest = (dest_dir / name).resolve()
                if not dest.is_relative_to(dest_dir):
                    raise ValueError(
                        f"Refusing to extract {name!r}: path traversal detected"
                    )
                log.info(f"Extracting: {name}")
                zf.extract(name, dest_dir)
                extracted.append(dest)
        return extracted
