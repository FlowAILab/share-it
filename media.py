"""Inline-image handling for exports: decode, strip metadata, name, dedupe.

Stdlib only. macOS screenshots are PNG (text/eXIf chunk walk); user-attached
photos are typically JPEG (APP1/COM segment walk). Anything else passes through
unmodified — better an EXIF-bearing share than a corrupted image.
"""
import base64
import hashlib
import struct

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# ancillary chunks that can carry provenance/location/text metadata
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def _strip_png(data):
    if not data.startswith(_PNG_MAGIC):
        return data
    out = [_PNG_MAGIC]
    i = len(_PNG_MAGIC)
    try:
        while i + 8 <= len(data):
            length = struct.unpack(">I", data[i:i + 4])[0]
            ctype = data[i + 4:i + 8]
            end = i + 12 + length
            if end > len(data):
                return data  # truncated — don't pretend we cleaned it
            if ctype not in _PNG_DROP:
                out.append(data[i:end])
            if ctype == b"IEND":
                break
            i = end
        return b"".join(out)
    except struct.error:
        return data


def _strip_jpeg(data):
    if not data.startswith(b"\xff\xd8"):
        return data
    out = [data[:2]]
    i = 2
    try:
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return data  # lost sync — pass through untouched
            marker = data[i + 1]
            if marker == 0xDA:            # start of scan: copy the rest verbatim
                out.append(data[i:])
                break
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            seg = data[i:i + 2 + length]
            # APP1 (EXIF/XMP) and COM carry metadata; keep APP0/JFIF and the rest
            if marker not in (0xE1, 0xFE):
                out.append(seg)
            i += 2 + length
        return b"".join(out)
    except struct.error:
        return data


def strip_metadata(data, media_type):
    if media_type == "image/png":
        return _strip_png(data)
    if media_type in ("image/jpeg", "image/jpg"):
        return _strip_jpeg(data)
    return data


_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/gif": "gif", "image/webp": "webp"}


def collect(messages, cap=None):
    """Decode + clean every inline image across messages, deduped by content.

    Returns [{name, data, content_type}]; annotates each message's media
    entries with their assigned object name (msg media entry gains "name").
    """
    if cap is None:
        import parsers as _p
        cap = _p.MEDIA_MAX_PER_SESSION
    out, by_hash = [], {}
    clean_b64 = {}  # name → metadata-stripped base64 (what downstream may embed)
    for msg in messages:
        for m in msg.get("media") or []:
            try:
                raw = base64.b64decode(m.get("data") or "", validate=False)
            except (ValueError, TypeError):
                continue
            if not raw:
                continue
            raw = strip_metadata(raw, m.get("media_type") or "")
            h = hashlib.sha256(raw).hexdigest()
            if h in by_hash:
                m["name"] = by_hash[h]
                m["data"] = clean_b64[by_hash[h]]   # never leak pre-strip bytes
                continue
            if len(out) >= cap:
                continue
            ext = _EXT.get(m.get("media_type"), "png")
            name = f"m{len(out) + 1}.{ext}"
            by_hash[h] = name
            clean_b64[name] = base64.b64encode(raw).decode()
            m["name"] = name
            m["data"] = clean_b64[name]             # downstream embeds cleaned bytes only
            out.append({"name": name, "data": raw,
                        "content_type": m.get("media_type") or "image/png"})
    return out
