"""Tests for the FRPC binary install + sha256 verification logic."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from homeassistant.core import HomeAssistant

from custom_components.ezlocloudharc import (
    _extract_frpc_binary,
    check_binary_current,
    install_frpc,
)
from custom_components.ezlocloudharc.exceptions import (
    FrpcChecksumError,
    FrpcInstallError,
)

VERSION = "0.61.0"
MACHINE = "amd64"


def _build_tarball(*, include_frpc: bool = True) -> bytes:
    """Build an in-memory frp release tarball mimicking the GitHub layout."""
    buffer = io.BytesIO()
    folder = f"frp_{VERSION}_linux_{MACHINE}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        readme = b"frp release\n"
        readme_info = tarfile.TarInfo(name=f"{folder}/README.md")
        readme_info.size = len(readme)
        tar.addfile(readme_info, io.BytesIO(readme))

        if include_frpc:
            frpc_bin = b"#!/bin/sh\necho frpc v0.61.0\n"
            frpc_info = tarfile.TarInfo(name=f"{folder}/frpc")
            frpc_info.size = len(frpc_bin)
            frpc_info.mode = 0o755
            tar.addfile(frpc_info, io.BytesIO(frpc_bin))

    return buffer.getvalue()


class _AsyncByteStream:
    """Async iterator yielding fixed byte chunks for httpx.stream().aiter_bytes()."""

    def __init__(self, payload: bytes, chunk_size: int = 8192) -> None:
        self._payload = payload
        self._chunk_size = chunk_size

    def __aiter__(self) -> _AsyncByteStream:
        self._offset = 0
        return self

    async def __anext__(self) -> bytes:
        if self._offset >= len(self._payload):
            raise StopAsyncIteration
        chunk = self._payload[self._offset : self._offset + self._chunk_size]
        self._offset += self._chunk_size
        return chunk


def _patch_client_stream(payload: bytes | None, *, raise_for_status: Any = None) -> MagicMock:
    """Build a mock httpx client whose stream() yields the given bytes."""
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=raise_for_status) if raise_for_status else MagicMock()
    if payload is not None:
        response.aiter_bytes = MagicMock(return_value=_AsyncByteStream(payload))

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=response)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_ctx)
    return client


# ── check_binary_current ────────────────────────────────────────────


async def test_check_binary_current_missing_file(tmp_path: Path) -> None:
    """Returns False when the binary file does not exist."""
    assert await check_binary_current(tmp_path / "frpc", VERSION) is False


async def test_check_binary_current_version_matches(tmp_path: Path) -> None:
    """Returns True when --version output contains the expected version."""
    binary = tmp_path / "frpc"
    binary.write_bytes(b"")

    proc = AsyncMock()
    proc.communicate = AsyncMock(
        return_value=(f"frpc version {VERSION}\n".encode(), b"")
    )
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await check_binary_current(binary, VERSION) is True


async def test_check_binary_current_version_mismatch(tmp_path: Path) -> None:
    """Returns False when --version output is a different version."""
    binary = tmp_path / "frpc"
    binary.write_bytes(b"")

    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"frpc version 0.50.0\n", b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await check_binary_current(binary, VERSION) is False


async def test_check_binary_current_oserror(tmp_path: Path) -> None:
    """OSError from subprocess (e.g., wrong-arch binary) returns False."""
    binary = tmp_path / "frpc"
    binary.write_bytes(b"")

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("Exec format error")),
    ):
        assert await check_binary_current(binary, VERSION) is False


# ── _extract_frpc_binary ────────────────────────────────────────────


def test_extract_frpc_binary_success(tmp_path: Path) -> None:
    """A well-formed tarball is extracted, copied, and chmod'd 0o755."""
    binary_path = tmp_path / "out" / "frpc"
    binary_path.parent.mkdir(parents=True)
    tar_path = tmp_path / "release.tar.gz"
    tar_path.write_bytes(_build_tarball())

    _extract_frpc_binary(tar_path, binary_path, str(tmp_path))

    assert binary_path.is_file()
    assert binary_path.stat().st_mode & 0o111  # executable bit set


def test_extract_frpc_binary_no_frpc_inside(tmp_path: Path) -> None:
    """A tarball without an frpc member raises FrpcInstallError."""
    binary_path = tmp_path / "out" / "frpc"
    binary_path.parent.mkdir(parents=True)
    tar_path = tmp_path / "release.tar.gz"
    tar_path.write_bytes(_build_tarball(include_frpc=False))

    with pytest.raises(FrpcInstallError, match="No frpc binary"):
        _extract_frpc_binary(tar_path, binary_path, str(tmp_path))


def test_extract_frpc_binary_corrupt_archive(tmp_path: Path) -> None:
    """A non-gzip payload raises FrpcInstallError."""
    binary_path = tmp_path / "out" / "frpc"
    binary_path.parent.mkdir(parents=True)
    tar_path = tmp_path / "release.tar.gz"
    tar_path.write_bytes(b"not a real tarball")

    with pytest.raises(FrpcInstallError, match="Extraction failed"):
        _extract_frpc_binary(tar_path, binary_path, str(tmp_path))


# ── install_frpc ─────────────────────────────────────────────────────


async def test_install_frpc_skips_download_when_current(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """If check_binary_current returns True, no download is attempted."""
    with (
        patch(
            "custom_components.ezlocloudharc.get_frp_binary_path",
            return_value=tmp_path / "bin" / "frpc",
        ),
        patch(
            "custom_components.ezlocloudharc.check_binary_current",
            AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.ezlocloudharc.get_async_client"
        ) as get_client,
    ):
        result = await install_frpc(hass, VERSION, MACHINE)

    get_client.assert_not_called()
    assert result.endswith("bin/frpc")


async def test_install_frpc_verifies_sha256(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A matching sha256 lets the install proceed and extract the binary."""
    tarball = _build_tarball()
    expected_sha = hashlib.sha256(tarball).hexdigest()
    binary_path = tmp_path / "bin" / "frpc"

    client = _patch_client_stream(tarball)
    with (
        patch(
            "custom_components.ezlocloudharc.get_frp_binary_path",
            return_value=binary_path,
        ),
        patch(
            "custom_components.ezlocloudharc.check_binary_current",
            AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.ezlocloudharc.FRPC_SHA256",
            {MACHINE: expected_sha},
        ),
        patch(
            "custom_components.ezlocloudharc.get_async_client", return_value=client
        ),
    ):
        result = await install_frpc(hass, VERSION, MACHINE)

    assert result == str(binary_path)
    assert binary_path.is_file()


async def test_install_frpc_sha256_mismatch_raises(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A bad sha256 raises FrpcChecksumError and does NOT extract the binary."""
    tarball = _build_tarball()
    binary_path = tmp_path / "bin" / "frpc"

    client = _patch_client_stream(tarball)
    with (
        patch(
            "custom_components.ezlocloudharc.get_frp_binary_path",
            return_value=binary_path,
        ),
        patch(
            "custom_components.ezlocloudharc.check_binary_current",
            AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.ezlocloudharc.FRPC_SHA256",
            {MACHINE: "0" * 64},  # never matches
        ),
        patch(
            "custom_components.ezlocloudharc.get_async_client", return_value=client
        ),
        pytest.raises(FrpcChecksumError),
    ):
        await install_frpc(hass, VERSION, MACHINE)

    assert not binary_path.is_file()


async def test_install_frpc_no_pinned_hash_raises(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """An architecture missing from FRPC_SHA256 raises FrpcChecksumError."""
    binary_path = tmp_path / "bin" / "frpc"
    with (
        patch(
            "custom_components.ezlocloudharc.get_frp_binary_path",
            return_value=binary_path,
        ),
        patch(
            "custom_components.ezlocloudharc.check_binary_current",
            AsyncMock(return_value=False),
        ),
        patch("custom_components.ezlocloudharc.FRPC_SHA256", {}),
        pytest.raises(FrpcChecksumError, match="No SHA-256 hash pinned"),
    ):
        await install_frpc(hass, VERSION, MACHINE)


async def test_install_frpc_http_error_raises_install_error(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """An HTTP error during download surfaces as FrpcInstallError."""
    binary_path = tmp_path / "bin" / "frpc"
    raise_for_status_exc = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404)
    )
    client = _patch_client_stream(b"", raise_for_status=raise_for_status_exc)

    with (
        patch(
            "custom_components.ezlocloudharc.get_frp_binary_path",
            return_value=binary_path,
        ),
        patch(
            "custom_components.ezlocloudharc.check_binary_current",
            AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.ezlocloudharc.FRPC_SHA256",
            {MACHINE: "0" * 64},
        ),
        patch(
            "custom_components.ezlocloudharc.get_async_client", return_value=client
        ),
        pytest.raises(FrpcInstallError, match="Download failed"),
    ):
        await install_frpc(hass, VERSION, MACHINE)
