"""HTTP thumbnail service.

The process shells out to the vips installed in the image. It does not shell
out through a shell, so a JPEG target such as file.jpg[Q=60] is one argument.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_PIXELS = 268_402_689
DEFAULT_EDGE = 640
DEFAULT_QUALITY = 60


class Rejected(Exception):
    """The image cannot be turned into a thumbnail. The caller should not retry."""


class Unavailable(Exception):
    """vips could not be run. The caller can retry."""


@dataclass(frozen=True)
class Config:
    max_bytes: int
    timeout: float
    max_concurrency: int
    port: int
    semaphore: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "semaphore", threading.BoundedSemaphore(self.max_concurrency)
        )


def config_from_env() -> Config:
    return Config(
        max_bytes=int(os.environ.get("MAX_BYTES", "41943040")),
        timeout=float(os.environ.get("TIMEOUT_SECONDS", "60")),
        max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "2")),
        port=int(os.environ.get("PORT", "6020")),
    )


def run_command(args: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise Unavailable(f"{args[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise Unavailable(f"{args[0]} timed out") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise Rejected(detail or f"{args[0]} exited {completed.returncode}")
    return completed.stdout.decode("utf-8", "replace")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _flag_enabled(line: str) -> bool:
    value = line.partition(":")[2].strip().split(" ", 1)[0].lower()
    return value in {"yes", "true"}


def config_problem(config_text: str) -> str | None:
    heif = False
    magick = False
    for line in config_text.splitlines():
        lowered = line.lower()
        if "heif" in lowered and _flag_enabled(line):
            heif = True
        if "magick load" in lowered and _flag_enabled(line):
            magick = True
    if not heif:
        return "vips was built without HEIF"
    if not magick:
        return "vips was built without Magick load"
    return None


def de265_plugin_installed() -> bool:
    for root in (Path("/usr/lib"), Path("/usr/local/lib")):
        if not root.is_dir():
            continue
        for path in root.rglob("libheif-libde265.so*"):
            if path.is_file():
                return True
    return False


def check(run) -> str | None:
    try:
        version_text = run(["vips", "--version"], 30)
        config_text = run(["vips", "--vips-config"], 30)
    except (Rejected, Unavailable) as error:
        return str(error)
    version = parse_version(version_text)
    if version is None or version < (8, 18, 7):
        return f"vips {version_text.strip()} is below 8.18.7"
    problem = config_problem(config_text)
    if problem:
        return problem
    if not de265_plugin_installed():
        return "libheif libde265 plugin is not installed"
    return None


def _one_int(values: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = values.get(name)
    if not raw:
        return default
    try:
        number = int(raw[0])
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if number < low or number > high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def parse_params(query: str) -> tuple[int, int, int]:
    values = parse_qs(query, keep_blank_values=True)
    edge = _one_int(values, "edge", DEFAULT_EDGE, 1, 4096)
    quality = _one_int(values, "quality", DEFAULT_QUALITY, 1, 100)
    max_pixels = _one_int(values, "maxPixels", MAX_PIXELS, 1, MAX_PIXELS)
    return edge, quality, max_pixels


def suffix_for(body: bytes) -> str:
    if body.startswith(b"\xff\xd8"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith(b"GIF8"):
        return ".gif"
    if body.startswith(b"BM"):
        return ".bmp"
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith(b"II*\x00") or body.startswith(b"MM\x00*"):
        return ".tif"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        brand = body[8:12]
        if brand in {b"avif", b"avis"}:
            return ".avif"
        return ".heic"
    return ".img"


def _dimension(run, path: Path, field: str, timeout: float) -> int:
    output = run(["vipsheader", "-f", field, str(path)], timeout)
    try:
        value = int(output.strip())
    except ValueError as error:
        raise Rejected(f"vipsheader returned no {field}") from error
    return value


def render(
    source: Path,
    staged: Path,
    jpeg: Path,
    edge: int,
    quality: int,
    max_pixels: int,
    run,
    timeout: float,
) -> None:
    width = _dimension(run, source, "width", timeout)
    height = _dimension(run, source, "height", timeout)
    if width * height > max_pixels:
        raise Rejected("image exceeds the pixel ceiling")
    edge_text = str(edge)
    run(
        [
            "vips",
            "thumbnail",
            str(source),
            str(staged),
            edge_text,
            "--height",
            edge_text,
            "--size",
            "down",
        ],
        timeout,
    )
    bands = _dimension(run, staged, "bands", timeout)
    target = f"{jpeg}[Q={quality}]"
    if bands == 4:
        run(["vips", "flatten", str(staged), target, "--background", "255,255,255"], timeout)
    elif bands == 2:
        run(["vips", "flatten", str(staged), target, "--background", "255"], timeout)
    else:
        run(["vips", "copy", str(staged), target], timeout)
    if not jpeg.is_file():
        raise Rejected("vips wrote no jpeg")


def _message(error: Exception) -> bytes:
    text = str(error).strip() or error.__class__.__name__
    return (text[:2000] + "\n").encode("utf-8", "replace")


def handle_request(method: str, target: str, body: bytes, config: Config, run):
    path = urlsplit(target).path
    if method == "GET" and path == "/health":
        try:
            version = run(["vips", "--version"], config.timeout)
        except (Rejected, Unavailable) as error:
            return 503, "text/plain", _message(error)
        return 200, "text/plain", version.encode("utf-8")
    if path not in {"/health", "/thumbnails"}:
        return 404, "text/plain", b"not found\n"
    if method != "POST" or path != "/thumbnails":
        return 405, "text/plain", b"method not allowed\n"
    if len(body) == 0:
        return 400, "text/plain", b"empty body\n"
    if len(body) > config.max_bytes:
        return 413, "text/plain", b"body exceeds MAX_BYTES\n"
    try:
        edge, quality, max_pixels = parse_params(urlsplit(target).query)
    except ValueError as error:
        return 400, "text/plain", _message(error)
    if not config.semaphore.acquire(timeout=config.timeout):
        return 503, "text/plain", b"thumbnail generator is busy\n"
    try:
        with tempfile.TemporaryDirectory(prefix="thumbnail-") as directory:
            folder = Path(directory)
            source = folder / f"source{suffix_for(body)}"
            source.write_bytes(body)
            jpeg = folder / "thumbnail.jpg"
            render(
                source,
                folder / "staged.png",
                jpeg,
                edge,
                quality,
                max_pixels,
                run,
                config.timeout,
            )
            return 200, "image/jpeg", jpeg.read_bytes()
    except Rejected as error:
        return 422, "text/plain", _message(error)
    except Unavailable as error:
        return 503, "text/plain", _message(error)
    finally:
        config.semaphore.release()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config: Config
    run = staticmethod(run_command)

    def _respond(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._respond(*handle_request("GET", self.path, b"", self.config, self.run))

    def do_POST(self) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._respond(411, "text/plain", b"length required\n")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._respond(400, "text/plain", b"invalid Content-Length\n")
            return
        if length > self.config.max_bytes:
            self.close_connection = True
            self._respond(413, "text/plain", b"body exceeds MAX_BYTES\n")
            return
        body = self.rfile.read(length)
        self._respond(*handle_request("POST", self.path, body, self.config, self.run))

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(config: Config, run=run_command, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"config": config, "run": staticmethod(run)})
    httpd = ThreadingHTTPServer((host, config.port), handler)
    httpd.daemon_threads = True
    return httpd


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, color_type: int, samples: bytes) -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, color_type, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" + samples))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def write_bmp(path: Path) -> None:
    pixel_row = b"\x00\x00\xff\x00"
    size = 54 + len(pixel_row)
    header = b"BM" + struct.pack("<IHHI", size, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, len(pixel_row), 2835, 2835, 0, 0)
    path.write_bytes(header + info + pixel_row)


def self_test() -> int:
    problem = check(run_command)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    config = config_from_env()
    with tempfile.TemporaryDirectory(prefix="thumbnail-self-test-") as directory:
        folder = Path(directory)
        samples = {
            folder / "opaque.png": (2, b"\xff\x00\x00"),
            folder / "alpha.png": (6, b"\xff\x00\x00\x00"),
        }
        for path, (color_type, pixels) in samples.items():
            write_png(path, color_type, pixels)
        write_bmp(folder / "dot.bmp")
        for path in (*samples, folder / "dot.bmp"):
            body = path.read_bytes()
            status, content_type, payload = handle_request(
                "POST",
                "/thumbnails",
                body,
                config,
                run_command,
            )
            if status != 200 or content_type != "image/jpeg" or not payload.startswith(b"\xff\xd8"):
                print(f"{path.name} failed: {status} {payload[:300]!r}", file=sys.stderr)
                return 1
    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problem = check(run_command)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print(run_command(["vips", "--version"], 30).strip())
        return 0
    if "--self-test" in argv:
        return self_test()
    config = config_from_env()
    problem = check(run_command)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    httpd = serve(config, run_command, host="0.0.0.0")
    print(f"listening on 0.0.0.0:{httpd.server_address[1]}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
