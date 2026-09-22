from __future__ import annotations

import base64
import fnmatch
import http.client
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from ..config import RemoteConfig
from ..system.shell import run


class RcloneAPIError(RuntimeError):
    pass


class RcloneClient:
    """Thin wrapper around the `rclone` CLI.

    We deliberately shell out to the real rclone binary rather than using a
    Python rclone library, since rclone's remotes (configured via
    `rclone config`) already handle auth/credentials for ~70 storage
    backends -- reimplementing that would be reinventing rclone badly.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def sync_dir(
        self,
        local_dir: Path,
        remote: str,
        *,
        include: Optional[str] = None,
        bandwidth_limit: Optional[str] = None,
        extra_flags: Optional[list] = None,
    ):
        cmd = ["rclone", "copy", str(local_dir), remote, "--create-empty-src-dirs"]
        if include:
            cmd += ["--include", include]
        if bandwidth_limit:
            cmd += ["--bwlimit", bandwidth_limit]
        if extra_flags:
            cmd += list(extra_flags)
        return run(cmd, dry_run=self.dry_run)

    def list_files(self, remote: str) -> list:
        """Recursively list files under `remote`, relative paths, files only."""
        result = run(["rclone", "lsf", "-R", "--files-only", remote], dry_run=False, quiet=True, check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_dirs(self, remote: str) -> list:
        """Immediate subdirectories of `remote` (no trailing slashes)."""
        result = run(["rclone", "lsf", "--dirs-only", remote], dry_run=False, quiet=True, check=False)
        if result.returncode != 0:
            return []
        return [line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()]

    def delete_file(self, remote_path: str):
        return run(["rclone", "deletefile", remote_path], dry_run=self.dry_run)

    def copy_to_local(self, remote_path: str, local_path: Path):
        return run(["rclone", "copyto", remote_path, str(local_path)], dry_run=self.dry_run)

    def list_remotes(self) -> list:
        result = run(["rclone", "listremotes"], dry_run=False, quiet=True, check=False)
        return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]

    def is_installed(self) -> bool:
        result = run(["rclone", "version"], dry_run=False, quiet=True, check=False)
        return result.returncode == 0


def _split_remote(target: str) -> tuple[str, str]:
    if ":" not in target:
        raise RcloneAPIError(f"Invalid rclone target '{target}': expected remote:path")
    name, path = target.split(":", 1)
    return f"{name}:", path.strip("/")


class RcloneRCClient:
    """Authenticated client for an existing rclone Remote Control server.

    Archives stay on Companion's local disk and are streamed over HTTP; the
    rclone container owns cloud credentials and provider-specific behavior.
    """

    def __init__(self, url: str, username: str, password_env: str, *, dry_run: bool = False):
        self.url = url.rstrip("/")
        self.username = username
        self.password_env = password_env
        self.dry_run = dry_run

    def _password(self) -> str:
        password = os.environ.get(self.password_env)
        if not password:
            raise RcloneAPIError(f"Environment variable {self.password_env} is required for rclone API access")
        return password

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self._password()}".encode()).decode()
        return f"Basic {token}"

    def _request(self, endpoint: str, payload: Optional[dict] = None) -> dict:
        body = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            f"{self.url}/{endpoint.lstrip('/')}",
            data=body,
            headers={"Authorization": self._auth_header(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise RcloneAPIError(f"rclone API call {endpoint} failed: {e}") from e

    def _upload_file(self, source: Path, target: str) -> None:
        fs, remote_path = _split_remote(target)
        remote_dir, _, filename = remote_path.rpartition("/")
        boundary = "----runtipi-companion-upload"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/gzip\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        query = urllib.parse.urlencode({"fs": fs, "remote": remote_dir})
        parsed = urllib.parse.urlsplit(self.url)
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(parsed.hostname, parsed.port, timeout=3600)
        endpoint = f"{parsed.path.rstrip('/')}/operations/uploadfile?{query}"
        try:
            connection.putrequest("POST", endpoint)
            connection.putheader("Authorization", self._auth_header())
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + source.stat().st_size + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            detail = response.read().decode(errors="replace")
            if response.status >= 300:
                raise RcloneAPIError(f"rclone upload failed ({response.status}): {detail}")
        except OSError as e:
            raise RcloneAPIError(f"rclone upload failed: {e}") from e
        finally:
            connection.close()

        stat = self._request("operations/stat", {"fs": fs, "remote": remote_path, "opt": {"filesOnly": True}})
        item = stat.get("item") or {}
        if item.get("Size") != source.stat().st_size:
            raise RcloneAPIError(f"Remote size verification failed for {target}")

    def sync_dir(
        self,
        local_dir: Path,
        remote: str,
        *,
        include: Optional[str] = None,
        bandwidth_limit: Optional[str] = None,
        extra_flags: Optional[list] = None,
    ):
        if bandwidth_limit or extra_flags:
            raise RcloneAPIError("bandwidth_limit and extra_rclone_flags are not supported by the rclone API transport")
        for source in sorted(Path(local_dir).rglob("*")):
            if not source.is_file() or (include and not fnmatch.fnmatch(source.name, include)):
                continue
            target = f"{remote.rstrip('/')}/{source.relative_to(local_dir).as_posix()}"
            if self.dry_run:
                continue
            self._upload_file(source, target)

    def list_files(self, remote: str) -> list:
        fs, path = _split_remote(remote)
        result = self._request(
            "operations/list", {"fs": fs, "remote": path, "opt": {"recurse": True, "filesOnly": True}}
        )
        return [item["Path"] for item in result.get("list", []) if not item.get("IsDir")]

    def list_dirs(self, remote: str) -> list:
        fs, path = _split_remote(remote)
        result = self._request("operations/list", {"fs": fs, "remote": path, "opt": {"dirsOnly": True}})
        return [item["Path"].rstrip("/") for item in result.get("list", []) if item.get("IsDir")]

    def delete_file(self, remote_path: str):
        if self.dry_run:
            return None
        fs, path = _split_remote(remote_path)
        return self._request("operations/deletefile", {"fs": fs, "remote": path})

    def copy_to_local(self, remote_path: str, local_path: Path):
        if self.dry_run:
            return None
        fs, path = _split_remote(remote_path)
        encoded_fs = urllib.parse.quote(fs, safe=":")
        encoded_path = urllib.parse.quote(path, safe="/")
        request = urllib.request.Request(
            f"{self.url}/[{encoded_fs}]/{encoded_path}", headers={"Authorization": self._auth_header()}
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(request, timeout=3600) as response, local_path.open("wb") as destination:
                shutil.copyfileobj(response, destination, length=1024 * 1024)
        except (urllib.error.URLError, OSError) as e:
            local_path.unlink(missing_ok=True)
            raise RcloneAPIError(f"rclone download failed for {remote_path}: {e}") from e


def client_for_remote(remote: RemoteConfig, *, dry_run: bool = False):
    if remote.api_url:
        return RcloneRCClient(
            remote.api_url,
            remote.api_username or "",
            remote.api_password_env or "",
            dry_run=dry_run,
        )
    return RcloneClient(dry_run=dry_run)
