"""Typed exceptions raised by the Ezlo HA Cloud integration.

Each exception subclasses ``HomeAssistantError`` and carries a
``translation_key`` so the UI can render a localized message.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class EzloError(HomeAssistantError):
    """Base class for Ezlo HA Cloud errors."""

    translation_key: str = "unknown"

    def __init__(
        self, *args: object, translation_placeholders: dict[str, str] | None = None
    ) -> None:
        super().__init__(*args)
        self.translation_domain = DOMAIN
        if translation_placeholders is not None:
            self.translation_placeholders = translation_placeholders


class EzloAuthError(EzloError):
    """The Ezlo Cloud backend rejected the credentials/token."""

    translation_key = "auth_failed"


class EzloSubscriptionExpiredError(EzloError):
    """The user's subscription is not in a valid state."""

    translation_key = "subscription_expired"


class EzloSubscriptionInvalidError(EzloError):
    """A non-trialing/non-active subscription state was returned."""

    translation_key = "subscription_invalid"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"subscription status {status} is not valid",
            translation_placeholders={"status": status},
        )


class EzloMissingUUIDError(EzloError):
    """The JWT payload did not include a user uuid."""

    translation_key = "missing_uuid"


class EzloApiUnreachableError(EzloError):
    """Network failure talking to the Ezlo Cloud API."""

    translation_key = "api_unreachable"

    def __init__(self, detail: str) -> None:
        super().__init__(detail, translation_placeholders={"detail": detail})


class EzloApiUnexpectedResponseError(EzloError):
    """The API returned an unexpected payload shape."""

    translation_key = "api_unexpected_response"

    def __init__(self, detail: str) -> None:
        super().__init__(detail, translation_placeholders={"detail": detail})


class FrpcInstallError(EzloError):
    """Installing the FRPC binary failed."""

    translation_key = "frpc_install_failed"

    def __init__(self, detail: str) -> None:
        super().__init__(detail, translation_placeholders={"detail": detail})


class FrpcUnsupportedArchitectureError(EzloError):
    """The host architecture isn't one we ship FRPC binaries for."""

    translation_key = "frpc_unsupported_arch"

    def __init__(self, arch: str, supported: list[str]) -> None:
        super().__init__(
            f"unsupported architecture {arch}",
            translation_placeholders={
                "arch": arch,
                "supported": ", ".join(supported),
            },
        )


class FrpcSetupError(EzloError):
    """Anything went wrong setting up the FRPC tunnel post-install."""

    translation_key = "frpc_setup_failed"

    def __init__(self, detail: str) -> None:
        super().__init__(detail, translation_placeholders={"detail": detail})


class FrpcChecksumError(FrpcInstallError):
    """The downloaded FRPC tarball did not match the pinned SHA-256."""

    translation_key = "frpc_checksum_mismatch"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.translation_key = "frpc_checksum_mismatch"
        self.translation_placeholders = {"detail": detail}
