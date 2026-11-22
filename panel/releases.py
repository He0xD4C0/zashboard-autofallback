"""Install a versioned executable from the official MetaCubeX releases."""

import gzip
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .runtime import RUNTIME

TAG = re.compile(r"v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
REPOSITORY = "MetaCubeX/mihomo"
SOURCE = f"https://github.com/{REPOSITORY}/releases"
API = f"https://api.github.com/repos/{REPOSITORY}/releases"
DOWNLOAD_LIMIT = 64 * 1024**2
EXECUTABLE_LIMIT = 192 * 1024**2


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.netloc not in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }:
            raise RuntimeError("unexpected redirect from GitHub Releases")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class AssetDigests(HTMLParser):
    """Read the checksum shown in the same release asset row as its exact link."""

    def __init__(self):
        super().__init__()
        self.assets = {}
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.row = []
        if self.row is not None and tag in {"a", "clipboard-copy"}:
            fields = dict(attrs)
            self.row.append(fields.get("href" if tag == "a" else "value", "") or "")

    def handle_endtag(self, tag):
        if tag == "li" and self.row is not None:
            links = set(item for item in self.row if item.startswith(f"/{REPOSITORY}/releases/download/"))
            digests = set(item for item in self.row if DIGEST.fullmatch(item))
            if len(links) == len(digests) == 1:
                self.assets.setdefault(links.pop(), set()).update(digests)
            self.row = None


def asset_names(tag, machine):
    if machine in {"x86_64", "amd64"}:
        return [f"mihomo-linux-amd64-v1-{tag}.gz", f"mihomo-linux-amd64-compatible-{tag}.gz"]
    if machine in {"aarch64", "arm64"}:
        return [f"mihomo-linux-arm64-{tag}.gz", f"mihomo-linux-arm64-v8-{tag}.gz"]
    raise ValueError("Mihomo Releases support in Panel requires Linux amd64 or arm64")


def open_release(url):
    request = urllib.request.Request(url, headers={"User-Agent": "mihomo-stack-panel"})
    return urllib.request.build_opener(GitHubRedirect()).open(request, timeout=15)


def asset_digest(tag, names):
    try:
        with open_release(f"{API}/tags/{tag}") as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 429}:
            raise RuntimeError(f"official release lookup failed (HTTP {exc.code})") from None
        return expanded_digest(tag, names)
    except (urllib.error.URLError, TimeoutError):
        return expanded_digest(tag, names)

    if release.get("tag_name") != tag or release.get("draft") or release.get("prerelease"):
        raise ValueError("select a published stable Mihomo release")
    assets = {asset["name"]: asset.get("digest") for asset in release.get("assets", [])}
    for name in names:
        digest = assets.get(name)
        if digest and DIGEST.fullmatch(digest):
            return name, digest
    raise RuntimeError("official release has no verified executable for this architecture")


def expanded_digest(tag, names):
    try:
        with open_release(f"{SOURCE}/expanded_assets/{tag}") as response:
            source = response.read(4 * 1024**2 + 1)
    except (OSError, urllib.error.URLError):
        raise RuntimeError("could not read official release metadata from GitHub") from None
    if len(source) > 4 * 1024**2:
        raise RuntimeError("official release metadata is too large")
    parser = AssetDigests()
    parser.feed(source.decode())
    for name in names:
        key = f"/{REPOSITORY}/releases/download/{tag}/{name}"
        items = parser.assets.get(key, set())
        if len(items) == 1:
            return name, items.pop()
    raise RuntimeError("official release asset checksum is unavailable")


def executable(tag):
    if not TAG.fullmatch(tag):
        raise ValueError("enter a release tag such as v1.19.33")
    return RUNTIME / "versions" / tag / "mihomo"


def save_download(url, output, expected):
    digest = hashlib.sha256()
    deadline = time.monotonic() + 180
    total = 0
    with open_release(url) as response, output.open("wb") as stream:
        while chunk := response.read(256 * 1024):
            total += len(chunk)
            if total > DOWNLOAD_LIMIT or time.monotonic() > deadline:
                raise RuntimeError("official release download exceeded its limits")
            digest.update(chunk)
            stream.write(chunk)
    if not hmac.compare_digest(f"sha256:{digest.hexdigest()}", expected):
        raise RuntimeError("official release SHA-256 mismatch")


def check_version(binary, tag):
    result = subprocess.run([str(binary), "-v"], capture_output=True, text=True, timeout=5)
    if result.returncode or not re.search(rf"\b{re.escape(tag)}\b", result.stdout):
        raise RuntimeError(f"Mihomo executable did not report {tag}")


def install(tag):
    binary = executable(tag)
    if binary.is_file():
        check_version(binary, tag)
        return binary
    name, digest = asset_digest(tag, asset_names(tag, platform.machine()))
    versions = binary.parent.parent
    versions.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=versions, prefix=".download-") as temporary:
        directory = Path(temporary)
        compressed = directory / "release.gz"
        candidate = directory / "mihomo"
        url = f"{SOURCE}/download/{tag}/{name}"
        for attempt in range(3):
            try:
                save_download(url, compressed, digest)
                break
            except OSError:
                if attempt == 2:
                    raise RuntimeError("official GitHub Release download unavailable") from None
                time.sleep(2**attempt)
        with gzip.open(compressed) as source, candidate.open("wb") as target:
            total = 0
            while chunk := source.read(256 * 1024):
                total += len(chunk)
                if total > EXECUTABLE_LIMIT:
                    raise RuntimeError("official release executable is too large")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        candidate.chmod(0o500)
        check_version(candidate, tag)
        binary.parent.mkdir(mode=0o700, exist_ok=True)
        os.replace(candidate, binary)
    return binary
