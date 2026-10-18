"""Local, tightly scoped photo attachment storage.

The ledger never stores an upload's original name.  Attachments are named with
fresh UUIDs and are only served by the web layer after it has authenticated the
request.  This module is deliberately independent of the rest of the ledger
so it can also be used by import and backup code without importing the service.
"""

from __future__ import annotations

import os
import re
import struct
import uuid
import zlib
from pathlib import Path


MAX_PHOTO_BYTES = 8 * 1024 * 1024
# The bound protects consumers that decode dimensions before displaying an
# image.  It is deliberately separate from the encoded-file-size bound.
MAX_IMAGE_PIXELS = 100_000_000

_UUID_FILENAME = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _invalid(message: str) -> ValueError:
    return ValueError(message)


def _ensure_photos_dir(root: Path, *, create: bool) -> Path:
    """Return ``root/photos`` after checking the directory boundary.

    A symlink in the storage boundary is rejected even when it points back into
    the private directory.  That keeps attachment reads deterministic and
    prevents a later replacement of a symlink target from changing the meaning
    of an otherwise safe photo id.
    """

    root = Path(root)
    if root.is_symlink():
        raise _invalid("attachment root must not be a symlink")
    if not root.exists():
        if not create:
            raise _invalid("attachment root does not exist")
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise _invalid("attachment root is not a directory")

    photos = root / "photos"
    if photos.exists() or photos.is_symlink():
        if photos.is_symlink():
            raise _invalid("photos directory must not be a symlink")
        if not photos.is_dir():
            raise _invalid("photos path is not a directory")
    elif create:
        # On Windows mode 0700 installs an owner-only ACL and can exclude the
        # desktop user when a sandbox account creates the directory. Inherit
        # the private ledger's ACL instead; retain owner-only mode on POSIX.
        photos.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    else:
        raise _invalid("photos directory does not exist")

    try:
        root_resolved = root.resolve(strict=True)
        photos_resolved = photos.resolve(strict=True)
        photos_resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise _invalid("photos path escapes attachment root") from exc
    return photos


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise _invalid("image dimensions are invalid")
    if width * height > MAX_IMAGE_PIXELS:
        raise _invalid("image dimensions are too large")


def _validate_jpeg(data: bytes, *, allow_trailing: bool = False) -> int:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise _invalid("invalid JPEG signature")

    # Parse marker segments and scan data enough to establish that declared
    # segment lengths stay inside the bounded input.  Entropy bytes are not
    # decoded here, but stuffed bytes and restart markers are handled so a
    # normal progressive JPEG is accepted.
    length = len(data)
    cursor = 2
    in_scan = False
    saw_eoi = False
    while cursor < length:
        if in_scan:
            marker_at = data.find(b"\xff", cursor)
            if marker_at < 0:
                raise _invalid("JPEG scan has no end marker")
            marker_cursor = marker_at + 1
            while marker_cursor < length and data[marker_cursor] == 0xFF:
                marker_cursor += 1
            if marker_cursor >= length:
                raise _invalid("truncated JPEG marker")
            marker = data[marker_cursor]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                # Stuffed 0xFF or restart marker remains part of entropy data.
                cursor = marker_cursor + 1
                continue
            in_scan = False
            cursor = marker_at
            continue

        if data[cursor] != 0xFF:
            raise _invalid("invalid JPEG marker stream")
        marker_cursor = cursor + 1
        while marker_cursor < length and data[marker_cursor] == 0xFF:
            marker_cursor += 1
        if marker_cursor >= length:
            raise _invalid("truncated JPEG marker")
        marker = data[marker_cursor]
        cursor = marker_cursor + 1

        if marker == 0xD9:  # EOI
            if cursor != length and not allow_trailing:
                raise _invalid("bytes follow JPEG EOI")
            saw_eoi = True
            break
        if marker == 0xD8 or marker == 0x00:
            raise _invalid("unexpected JPEG marker")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if cursor + 2 > length:
            raise _invalid("truncated JPEG segment length")
        segment_length = struct.unpack_from(">H", data, cursor)[0]
        if segment_length < 2:
            raise _invalid("invalid JPEG segment length")
        segment_start = cursor + 2
        segment_end = cursor + segment_length
        if segment_end > length:
            raise _invalid("JPEG segment exceeds file")

        # SOF markers carry dimensions.  We inspect them when present but do
        # not require one: this keeps the validator useful for tiny synthetic
        # signature fixtures while still rejecting malformed marker streams.
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 8:
                raise _invalid("truncated JPEG frame header")
            height, width = struct.unpack_from(">HH", data, segment_start + 1)
            _validate_dimensions(width, height)

        if marker == 0xDA:  # SOS; following bytes are entropy-coded data.
            if segment_length < 2:
                raise _invalid("invalid JPEG scan header")
            in_scan = True
        cursor = segment_end

    if not saw_eoi:
        raise _invalid("JPEG has no EOI marker")
    return cursor


def _validate_png(data: bytes) -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) + 12 or data[:8] != signature:
        raise _invalid("invalid PNG signature")

    cursor = 8
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    length = len(data)
    while cursor < length:
        if length - cursor < 12:
            raise _invalid("truncated PNG chunk")
        chunk_length = struct.unpack_from(">I", data, cursor)[0]
        chunk_type = data[cursor + 4 : cursor + 8]
        if not re.fullmatch(rb"[A-Za-z]{4}", chunk_type):
            raise _invalid("invalid PNG chunk type")
        if chunk_length > MAX_PHOTO_BYTES:
            raise _invalid("PNG chunk is too large")
        chunk_start = cursor + 8
        chunk_end = chunk_start + chunk_length
        crc_end = chunk_end + 4
        if crc_end > length:
            raise _invalid("PNG chunk exceeds file")
        chunk_data = data[chunk_start:chunk_end]
        stored_crc = struct.unpack_from(">I", data, chunk_end)[0]
        calculated_crc = zlib.crc32(chunk_type)
        calculated_crc = zlib.crc32(chunk_data, calculated_crc) & 0xFFFFFFFF
        if calculated_crc != stored_crc:
            raise _invalid("PNG CRC mismatch")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise _invalid("PNG must start with IHDR")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            _validate_dimensions(width, height)
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if color_type not in valid_depths or bit_depth not in valid_depths[color_type]:
                raise _invalid("invalid PNG color format")
            if compression != 0 or filter_method != 0 or interlace not in (0, 1):
                raise _invalid("invalid PNG encoding parameters")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise _invalid("duplicate PNG IHDR")

        if chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if chunk_length != 0:
                raise _invalid("PNG IEND must be empty")
            saw_iend = True
            if crc_end != length:
                raise _invalid("bytes follow PNG IEND")
            break
        cursor = crc_end

    if not saw_ihdr or not saw_idat or not saw_iend:
        raise _invalid("PNG is missing required chunks")


def _validate_webp(data: bytes) -> None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise _invalid("invalid WebP signature")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size != len(data) - 8:
        raise _invalid("WebP RIFF size mismatch")

    cursor = 12
    saw_image = False
    length = len(data)
    while cursor < length:
        if length - cursor < 8:
            raise _invalid("truncated WebP chunk")
        chunk_type = data[cursor : cursor + 4]
        chunk_length = struct.unpack_from("<I", data, cursor + 4)[0]
        payload_start = cursor + 8
        payload_end = payload_start + chunk_length
        padded_end = payload_end + (chunk_length & 1)
        if chunk_length > MAX_PHOTO_BYTES or padded_end > length:
            raise _invalid("WebP chunk exceeds file")
        payload = data[payload_start:payload_end]

        if chunk_type == b"VP8 ":
            if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                raise _invalid("invalid WebP VP8 frame")
            width, height = struct.unpack_from("<HH", payload, 6)
            _validate_dimensions(width & 0x3FFF, height & 0x3FFF)
            saw_image = True
        elif chunk_type == b"VP8L":
            if len(payload) < 5 or payload[0] != 0x2F:
                raise _invalid("invalid WebP VP8L frame")
            width = 1 + payload[1] + ((payload[2] & 0x3F) << 8)
            height = 1 + ((payload[2] >> 6) | (payload[3] << 2) | ((payload[4] & 0x0F) << 10))
            _validate_dimensions(width, height)
            saw_image = True
        elif chunk_type == b"VP8X":
            if len(payload) < 10:
                raise _invalid("invalid WebP VP8X frame")
            width = 1 + payload[4] + (payload[5] << 8) + (payload[6] << 16)
            height = 1 + payload[7] + (payload[8] << 8) + (payload[9] << 16)
            _validate_dimensions(width, height)
            saw_image = True

        cursor = padded_end

    if cursor != length or not saw_image:
        raise _invalid("WebP has no image frame")


def _validate_image(data: bytes) -> tuple[str, str]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _invalid("photo data must be bytes")
    data = bytes(data)
    if len(data) == 0 or len(data) > MAX_PHOTO_BYTES:
        raise _invalid("photo size is invalid")

    # Signature dispatch makes HTML/SVG and other text payloads fail before any
    # extension can be selected.  No user-provided content type is trusted.
    if data[:2] == b"\xff\xd8":
        _validate_jpeg(data)
        return ".jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        _validate_png(data)
        return ".png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        _validate_webp(data)
        return ".webp", "image/webp"
    raise _invalid("unsupported or unsafe photo format")


def _photo_filename(photo_id: str) -> str:
    if not isinstance(photo_id, str) or not _UUID_FILENAME.fullmatch(photo_id):
        raise _invalid("invalid photo id")
    # Return the original spelling for filesystem lookup, but callers can use
    # the lower-case suffix for MIME validation.
    return photo_id


def _contained_photo_path(root: Path, photo_id: str) -> Path:
    photos = _ensure_photos_dir(Path(root), create=False)
    filename = _photo_filename(photo_id)
    candidate = photos / filename
    if candidate.is_symlink():
        raise _invalid("photo path must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        photos_resolved = photos.resolve(strict=True)
        resolved.relative_to(photos_resolved)
    except (OSError, ValueError) as exc:
        raise _invalid("photo path escapes storage directory") from exc
    if not resolved.is_file():
        raise _invalid("photo does not exist")
    return candidate


def save_photo(root: Path, data: bytes) -> str:
    """Validate and save a photo under ``root/photos``.

    The returned id is the only name the caller should persist in the ledger.
    ``ValueError`` is used for every rejected input so the service can map it to
    its normal input-validation error without exposing filesystem details.
    """

    # Some camera/messaging exports append application data after JPEG EOI.
    # Locate EOI through the marker parser (not a byte search, since metadata
    # may contain those bytes), and store only the validated image stream.
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _invalid("photo data must be bytes")
    data = bytes(data)
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise _invalid("photo size is invalid")
    if data[:2] == b"\xff\xd8":
        data = data[:_validate_jpeg(data, allow_trailing=True)]
    extension, _mime = _validate_image(data)
    photos = _ensure_photos_dir(Path(root), create=True)
    for _attempt in range(8):
        # Use the canonical UUID spelling because the service and API expose
        # this id as an opaque, validated filename.
        photo_id = f"{uuid.uuid4()}{extension}"
        target = photos / photo_id
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bytes(data))
                stream.flush()
                os.fsync(stream.fileno())
            return photo_id
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
    raise _invalid("could not allocate a unique photo id")


def read_photo(root: Path, photo_id: str) -> tuple[bytes, str]:
    """Read and revalidate one generated attachment id."""

    target = _contained_photo_path(Path(root), photo_id)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise _invalid("photo cannot be inspected") from exc
    if size <= 0 or size > MAX_PHOTO_BYTES:
        raise _invalid("photo size is invalid")
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_PHOTO_BYTES + 1)
    except OSError as exc:
        raise _invalid("photo cannot be read") from exc
    if len(data) != size or len(data) > MAX_PHOTO_BYTES:
        raise _invalid("photo changed while being read")
    extension, mime = _validate_image(data)
    expected_extension = Path(photo_id).suffix.lower()
    if extension != expected_extension and not (extension == ".jpg" and expected_extension == ".jpeg"):
        raise _invalid("photo extension does not match content")
    return data, mime


__all__ = [
    "MAX_IMAGE_PIXELS",
    "MAX_PHOTO_BYTES",
    "read_photo",
    "save_photo",
]
