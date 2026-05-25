"""Tests for the FRPC binary install/check logic in __init__.py."""

from __future__ import annotations

import io
from pathlib import Path
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from homeassistant.components.ezlohacloud import (
    FrpcInstallError,
    _sync_install_frpc,
    check_binary_current,
    get_system_architecture,
    install_frpc,
)
from homeassistant.core import HomeAssistant

VERSION = "0.61.0"
MACHINE = "amd64"


def _build_tarball(*, include_frpc: bool = True, arch: str = MACHINE) -> bytes:
    """Build an in-memory frp release tarball mimicking the GitHub layout.

    The real release format is `frp_<ver>_linux_<arch>/frpc` plus other files.
    Set include_frpc=False to simulate a corrupt/wrong release.
    """
    buffer = io.BytesIO()
    folder = f"frp_{VERSION}_linux_{arch}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # Always include a readme so the tarball isn't empty
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


def _mock_httpx_stream(payload: bytes) -> MagicMock:
    """Return a MagicMock that emulates httpx.stream() as a context manager."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_bytes = MagicMock(return_value=[payload])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ── get_system_architecture ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "amd64"),
        ("aarch64", "arm64"),
        ("armv7l", "arm"),
        ("armv6l", "arm"),
        ("i686", "386"),
        ("X86_64", "amd64"),  # case-insensitive
    ],
)
async def test_get_system_architecture_known(
    hass: HomeAssistant, machine: str, expected: str
) -> None:
    """Known architectures resolve to the expected mapping."""
    with patch(
        "homeassistant.components.ezlohacloud.platform.machine",
        return_value=machine,
    ):
        assert await get_system_architecture(hass) == expected


async def test_get_system_architecture_unsupported(hass: HomeAssistant) -> None:
    """Unsupported architecture raises FrpcInstallError."""
    with (
        patch(
            "homeassistant.components.ezlohacloud.platform.machine",
            return_value="riscv64",
        ),
        pytest.raises(FrpcInstallError, match="Unsupported architecture"),
    ):
        await get_system_architecture(hass)


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


# ── _sync_install_frpc ──────────────────────────────────────────────


def test_sync_install_frpc_success(tmp_path: Path) -> None:
    """Successful download and extraction writes binary and returns its path."""
    binary_path = tmp_path / "bin" / "frpc"
    binary_path.parent.mkdir(parents=True, exist_ok=True)

    tarball = _build_tarball()
    with patch(
        "homeassistant.components.ezlohacloud.httpx.stream",
        return_value=_mock_httpx_stream(tarball),
    ):
        result = _sync_install_frpc(VERSION, MACHINE, binary_path)

    assert Path(result) == binary_path
    assert binary_path.is_file()
    # Binary is marked executable
    assert binary_path.stat().st_mode & 0o111


def test_sync_install_frpc_download_failure(tmp_path: Path) -> None:
    """HTTP errors during download are wrapped in FrpcInstallError."""
    binary_path = tmp_path / "bin" / "frpc"
    binary_path.parent.mkdir(parents=True, exist_ok=True)

    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "homeassistant.components.ezlohacloud.httpx.stream",
            return_value=cm,
        ),
        pytest.raises(FrpcInstallError, match="Download failed"),
    ):
        _sync_install_frpc(VERSION, MACHINE, binary_path)


def test_sync_install_frpc_missing_frpc_in_archive(tmp_path: Path) -> None:
    """A release tarball without frpc inside raises FrpcInstallError."""
    binary_path = tmp_path / "bin" / "frpc"
    binary_path.parent.mkdir(parents=True, exist_ok=True)

    tarball = _build_tarball(include_frpc=False)
    with (
        patch(
            "homeassistant.components.ezlohacloud.httpx.stream",
            return_value=_mock_httpx_stream(tarball),
        ),
        pytest.raises(FrpcInstallError, match="No frpc binary"),
    ):
        _sync_install_frpc(VERSION, MACHINE, binary_path)


def test_sync_install_frpc_extraction_failure(tmp_path: Path) -> None:
    """Corrupt archive (not a real tar.gz) raises FrpcInstallError."""
    binary_path = tmp_path / "bin" / "frpc"
    binary_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        patch(
            "homeassistant.components.ezlohacloud.httpx.stream",
            return_value=_mock_httpx_stream(b"not a real tarball"),
        ),
        pytest.raises(FrpcInstallError, match="Extraction failed"),
    ):
        _sync_install_frpc(VERSION, MACHINE, binary_path)


# ── install_frpc (orchestration) ────────────────────────────────────


async def test_install_frpc_skips_download_when_current(
    hass: HomeAssistant,
) -> None:
    """If check_binary_current returns True, no download is attempted."""
    with (
        patch(
            "homeassistant.components.ezlohacloud.check_binary_current",
            AsyncMock(return_value=True),
        ),
        patch(
            "homeassistant.components.ezlohacloud._sync_install_frpc"
        ) as sync_install,
    ):
        result = await install_frpc(hass, VERSION, MACHINE)

    sync_install.assert_not_called()
    assert result.endswith("/bin/frpc")


async def test_install_frpc_downloads_when_outdated(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """If check_binary_current returns False, _sync_install_frpc is invoked."""
    expected_path = "/fake/installed/frpc"
    with (
        patch(
            "homeassistant.components.ezlohacloud.check_binary_current",
            AsyncMock(return_value=False),
        ),
        patch(
            "homeassistant.components.ezlohacloud._sync_install_frpc",
            return_value=expected_path,
        ) as sync_install,
    ):
        result = await install_frpc(hass, VERSION, MACHINE)

    sync_install.assert_called_once()
    assert result == expected_path
