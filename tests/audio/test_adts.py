from __future__ import annotations

import struct

import pytest

from nanobot.audio.adts import adts_to_m4a, is_adts


def _adts_frame(
    payload: bytes,
    *,
    object_type: int = 2,
    rate_index: int = 4,
    channels: int = 1,
    crc: bool = False,
) -> bytes:
    header_len = 9 if crc else 7
    length = header_len + len(payload)
    header = bytes((
        0xFF,
        0xF0 | (0 if crc else 1),
        ((object_type - 1) << 6) | (rate_index << 2) | (channels >> 2),
        ((channels & 0x03) << 6) | (length >> 11),
        (length >> 3) & 0xFF,
        ((length & 0x07) << 5) | 0x1F,
        0xFC,
    ))
    return header + (b"\xAB\xCD" if crc else b"") + payload


def _boxes(data: bytes) -> dict[bytes, bytes]:
    found: dict[bytes, bytes] = {}
    pos = 0
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        found[kind] = data[pos + 8:pos + size]
        pos += size
    return found


def _find(data: bytes, path: list[bytes]) -> bytes:
    for kind in path:
        data = _boxes(data)[kind]
    return data


_STBL = [b"moov", b"trak", b"mdia", b"minf", b"stbl"]


def test_is_adts_distinguishes_aac_from_mp3_and_containers() -> None:
    assert is_adts(_adts_frame(b"x"))
    assert not is_adts(b"\xFF\xFB\x90\x00\x00\x00\x00")  # MPEG-1 Layer III
    assert not is_adts(b"OggS\x00\x02\x00")
    assert not is_adts(b"\xFF\xF1")


def test_adts_to_m4a_wraps_frames_without_reencoding() -> None:
    payloads = [b"first-frame", b"second", b"third-frame-payload"]
    m4a = adts_to_m4a(b"".join(_adts_frame(p) for p in payloads))

    top = _boxes(m4a)
    assert list(top) == [b"ftyp", b"moov", b"mdat"]
    assert top[b"ftyp"].startswith(b"M4A ")
    assert top[b"mdat"] == b"".join(payloads)

    stsz = _find(m4a, [*_STBL, b"stsz"])
    assert struct.unpack(">II", stsz[4:12]) == (0, len(payloads))
    assert list(struct.unpack(f">{len(payloads)}I", stsz[12:])) == [len(p) for p in payloads]
    stco = _find(m4a, [*_STBL, b"stco"])
    offset = struct.unpack(">I", stco[8:12])[0]
    assert m4a[offset:offset + len(payloads[0])] == payloads[0]
    stts = _find(m4a, [*_STBL, b"stts"])
    assert struct.unpack(">II", stts[8:16]) == (len(payloads), 1024)

    mdhd = _find(m4a, [b"moov", b"trak", b"mdia", b"mdhd"])
    assert struct.unpack(">II", mdhd[12:20]) == (44100, len(payloads) * 1024)


@pytest.mark.parametrize(
    ("rate_index", "channels", "audio_specific_config"),
    [(4, 1, b"\x12\x08"), (3, 2, b"\x11\x90"), (8, 1, b"\x14\x08")],
)
def test_adts_to_m4a_derives_audio_specific_config(
    rate_index: int, channels: int, audio_specific_config: bytes
) -> None:
    m4a = adts_to_m4a(_adts_frame(b"payload", rate_index=rate_index, channels=channels))

    mp4a = _find(m4a, [*_STBL, b"stsd"])[8 + 8:]
    assert struct.unpack(">H", mp4a[16:18])[0] == channels
    assert b"\x05\x02" + audio_specific_config in mp4a


def test_adts_to_m4a_strips_crc_and_drops_truncated_tail() -> None:
    data = _adts_frame(b"protected", crc=True) + _adts_frame(b"cut-off-frame")[:-3]

    assert _boxes(adts_to_m4a(data))[b"mdat"] == b"protected"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        _adts_frame(b"ok") + b"garbage-after-frame",
        _adts_frame(b"a", rate_index=4) + _adts_frame(b"b", rate_index=3),
    ],
)
def test_adts_to_m4a_rejects_unsupported_streams(data: bytes) -> None:
    with pytest.raises(ValueError):
        adts_to_m4a(data)
