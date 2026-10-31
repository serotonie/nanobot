"""Lossless remux of raw AAC ADTS streams into an M4A (MP4) container.

Some transcription endpoints (Groq, OpenAI) inspect the uploaded bytes and
reject raw ADTS audio, which is what Signal voice notes contain. Wrapping the
AAC frames in a minimal MP4 container makes them acceptable without
re-encoding or requiring ffmpeg.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_SAMPLES_PER_FRAME = 1024
_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350,
)
_UNITY_MATRIX = struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


@dataclass(frozen=True)
class _StreamFormat:
    object_type: int
    sample_rate_index: int
    channel_config: int


def is_adts(data: bytes) -> bool:
    """Return whether *data* starts with an AAC ADTS frame header."""
    # 12-bit syncword plus layer bits that are always 00 for AAC (MP3 uses non-zero).
    return len(data) >= 7 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0


def _parse_frames(data: bytes) -> tuple[_StreamFormat, list[bytes]]:
    fmt: _StreamFormat | None = None
    frames: list[bytes] = []
    pos = 0
    while len(data) - pos >= 7:
        header = data[pos:pos + 7]
        if not is_adts(header):
            raise ValueError(f"invalid ADTS sync at offset {pos}")
        protection_absent = header[1] & 0x01
        frame_fmt = _StreamFormat(
            object_type=(header[2] >> 6) + 1,
            sample_rate_index=(header[2] >> 2) & 0x0F,
            channel_config=((header[2] & 0x01) << 2) | (header[3] >> 6),
        )
        frame_length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
        raw_blocks = (header[6] & 0x03) + 1
        header_length = 7 if protection_absent else 9

        if frame_fmt.sample_rate_index >= len(_SAMPLE_RATES):
            raise ValueError("unsupported ADTS sample rate index")
        if frame_fmt.channel_config == 0:
            raise ValueError("ADTS streams with in-band channel config are not supported")
        if raw_blocks != 1:
            raise ValueError("ADTS frames with multiple raw data blocks are not supported")
        if frame_length <= header_length:
            raise ValueError(f"invalid ADTS frame length at offset {pos}")
        if fmt is None:
            fmt = frame_fmt
        elif frame_fmt != fmt:
            raise ValueError("ADTS stream format changes mid-stream")
        if pos + frame_length > len(data):
            break  # Truncated final frame (e.g. interrupted recording).

        frames.append(data[pos + header_length:pos + frame_length])
        pos += frame_length

    if fmt is None or not frames:
        raise ValueError("no complete ADTS frames found")
    return fmt, frames


def _box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _full_box(kind: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *payload)


def _descriptor(tag: int, payload: bytes) -> bytes:
    if len(payload) > 0x7F:
        raise ValueError("descriptor too large")
    return bytes((tag, len(payload))) + payload


def _audio_specific_config(fmt: _StreamFormat) -> bytes:
    value = (fmt.object_type << 11) | (fmt.sample_rate_index << 7) | (fmt.channel_config << 3)
    return struct.pack(">H", value)


def _esds(fmt: _StreamFormat, frames: list[bytes], sample_rate: int) -> bytes:
    total_bytes = sum(len(f) for f in frames)
    duration_sec = len(frames) * _SAMPLES_PER_FRAME / sample_rate
    max_frame = max(len(f) for f in frames)
    avg_bitrate = int(total_bytes * 8 / duration_sec)
    max_bitrate = int(max_frame * 8 * sample_rate / _SAMPLES_PER_FRAME)
    decoder_config = _descriptor(
        0x04,
        bytes((0x40, 0x15))  # MPEG-4 Audio, AudioStream
        + max_frame.to_bytes(3, "big")
        + struct.pack(">II", max_bitrate, avg_bitrate)
        + _descriptor(0x05, _audio_specific_config(fmt)),
    )
    es_descriptor = _descriptor(
        0x03,
        struct.pack(">HB", 1, 0) + decoder_config + _descriptor(0x06, b"\x02"),
    )
    return _full_box(b"esds", 0, 0, es_descriptor)


def _moov(fmt: _StreamFormat, frames: list[bytes], mdat_payload_offset: int) -> bytes:
    sample_rate = _SAMPLE_RATES[fmt.sample_rate_index]
    duration = len(frames) * _SAMPLES_PER_FRAME
    channels = 8 if fmt.channel_config == 7 else fmt.channel_config

    mvhd = _full_box(
        b"mvhd", 0, 0,
        struct.pack(">IIII", 0, 0, sample_rate, duration),
        struct.pack(">IH", 0x00010000, 0x0100), bytes(10),
        _UNITY_MATRIX, bytes(24), struct.pack(">I", 2),
    )
    tkhd = _full_box(
        b"tkhd", 0, 0x7,
        struct.pack(">IIIII", 0, 0, 1, 0, duration), bytes(8),
        struct.pack(">HHHH", 0, 0, 0x0100, 0),
        _UNITY_MATRIX, struct.pack(">II", 0, 0),
    )
    mdhd = _full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, sample_rate, duration, 0x55C4, 0))
    hdlr = _full_box(b"hdlr", 0, 0, struct.pack(">I4s", 0, b"soun"), bytes(12), b"SoundHandler\x00")
    mp4a = _box(
        b"mp4a",
        bytes(6), struct.pack(">H", 1), bytes(8),
        struct.pack(">HHHH", channels, 16, 0, 0),
        struct.pack(">I", (sample_rate << 16) if sample_rate < 0x10000 else 0),
        _esds(fmt, frames, sample_rate),
    )
    stbl = _box(
        b"stbl",
        _full_box(b"stsd", 0, 0, struct.pack(">I", 1), mp4a),
        _full_box(b"stts", 0, 0, struct.pack(">III", 1, len(frames), _SAMPLES_PER_FRAME)),
        _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(frames), 1)),
        _full_box(
            b"stsz", 0, 0,
            struct.pack(">II", 0, len(frames)),
            struct.pack(f">{len(frames)}I", *(len(f) for f in frames)),
        ),
        _full_box(b"stco", 0, 0, struct.pack(">II", 1, mdat_payload_offset)),
    )
    minf = _box(
        b"minf",
        _full_box(b"smhd", 0, 0, struct.pack(">HH", 0, 0)),
        _box(b"dinf", _full_box(b"dref", 0, 0, struct.pack(">I", 1), _full_box(b"url ", 0, 1))),
        stbl,
    )
    trak = _box(b"trak", tkhd, _box(b"mdia", mdhd, hdlr, minf))
    return _box(b"moov", mvhd, trak)


def adts_to_m4a(data: bytes) -> bytes:
    """Remux an AAC ADTS stream into M4A bytes without re-encoding.

    Raises ``ValueError`` when *data* is not a supported ADTS stream.
    """
    fmt, frames = _parse_frames(data)
    ftyp = _box(b"ftyp", b"M4A ", struct.pack(">I", 0), b"M4A mp42isom")
    # moov size does not depend on the chunk offset value, so measure it first.
    moov_size = len(_moov(fmt, frames, 0))
    mdat_payload_offset = len(ftyp) + moov_size + 8
    moov = _moov(fmt, frames, mdat_payload_offset)
    return ftyp + moov + _box(b"mdat", *frames)
