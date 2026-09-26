"""Minimal Ogg Opus reader: enough to check what the encoder produced.

Parses pages, reassembles packets, decodes ``OpusHead`` and ``OpusTags`` and
reports the duration from the last granule position. Used by the tests and by
``selftest``; not on the capture path.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class OpusInfo:
    channels: int
    pre_skip: int
    input_sample_rate: int
    vendor: str
    tags: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    n_pages: int = 0


class OggError(ValueError):
    pass


def _pages(data: bytes):
    pos = 0
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise OggError(f"no OggS capture pattern at {pos}")
        if pos + 27 > len(data):
            raise OggError("truncated page header")
        (version, htype, granule, serial, seq, crc, nsegs) = struct.unpack_from(
            "<BBqIIIB", data, pos + 4)
        if version != 0:
            raise OggError("unsupported ogg version")
        table = data[pos + 27:pos + 27 + nsegs]
        body_start = pos + 27 + nsegs
        body_len = sum(table)
        body = data[body_start:body_start + body_len]
        if len(body) != body_len:
            raise OggError("truncated page body")
        yield htype, granule, table, body
        pos = body_start + body_len


def packets(data: bytes):
    """Yield (packet bytes, granule of the page it ended on)."""
    buf = b""
    for _htype, granule, table, body in _pages(data):
        off = 0
        for seg in table:
            buf += body[off:off + seg]
            off += seg
            if seg < 255:
                yield buf, granule
                buf = b""


def parse(data: bytes) -> OpusInfo:
    it = packets(data)
    try:
        head, _ = next(it)
        tags, _ = next(it)
    except StopIteration as e:
        raise OggError("fewer than two packets") from e
    if head[:8] != b"OpusHead":
        raise OggError("first packet is not OpusHead")
    version, channels, pre_skip, rate, _gain, mapping = struct.unpack_from("<BBHIhB", head, 8)
    if tags[:8] != b"OpusTags":
        raise OggError("second packet is not OpusTags")
    pos = 8
    (vlen,) = struct.unpack_from("<I", tags, pos)
    pos += 4
    vendor = tags[pos:pos + vlen].decode("utf-8", "replace")
    pos += vlen
    (count,) = struct.unpack_from("<I", tags, pos)
    pos += 4
    comments: dict[str, str] = {}
    for _ in range(count):
        (clen,) = struct.unpack_from("<I", tags, pos)
        pos += 4
        item = tags[pos:pos + clen].decode("utf-8", "replace")
        pos += clen
        k, _, v = item.partition("=")
        comments[k.upper()] = v
    info = OpusInfo(channels=channels, pre_skip=pre_skip, input_sample_rate=rate,
                    vendor=vendor, tags=comments)
    last_granule = 0
    n_pages = 0
    for _htype, granule, _table, _body in _pages(data):
        n_pages += 1
        if granule > 0:
            last_granule = granule
    info.n_pages = n_pages
    info.duration_s = max(0, last_granule - pre_skip) / 48000.0
    return info
