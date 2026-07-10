"""Constants for the Ezlo HA Cloud integration."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "ezlocloudharc"
PLATFORMS: list[str] = []

# Default Ezlo HA Cloud API endpoint. Overridable per-entry via CONF_API_URI
# (advanced config-flow option) so QA can point at api-dev.harc.cloud without
# forking the integration.
DEFAULT_API_URI = "https://api.harc.cloud"
CONF_API_URI = "api_uri"

# FRPC binary version installed and managed by this integration.
FRPC_VERSION = "0.61.0"

# SHA-256 hashes of the upstream frpc release tarballs for FRPC_VERSION,
# keyed by the architecture string used in the GitHub download URL
# (frp_<ver>_linux_<arch>.tar.gz). Hardcoding these pins the download to a
# bit-identical artifact, defending against a compromised GitHub release.
# Source: https://github.com/fatedier/frp/releases/download/v0.61.0/frp_sha256_checksums.txt
FRPC_SHA256: dict[str, str] = {
    "amd64": "720a9fe2a3299346572544909a78c023344c88bde13c55b921e298e8c5ded21f",
    "arm64": "8d54b8faae5df02268bd784f78a155494893c6eb00070a185022198c1997ec7f",
    "arm": "38b2d2f9a46b636dcdf4d656373de86f6c869da98a4e323bd9587989c1c06db0",
    "arm_hf": "f151b5087870a72faa13c026336f3a6b97f0df2dcae3f3b122c43e604772cd23",
}

# Repair-issue identifier surfaced when configuration.yaml lacks the
# trusted_proxies block needed for remote access.
ISSUE_TRUSTED_PROXIES_RESTART = "restart_required_for_trusted_proxies"


class SubscriptionStatus(StrEnum):
    """Subscription state values from the Ezlo Cloud backend."""

    # Stripe-managed states
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    # Non-Stripe access classes managed by Ezlo operators
    INTERNAL = "internal"
    PARTNER_TRIAL = "partner_trial"
    PARTNER_TRIAL_EXPIRED = "partner_trial_expired"
    # Non-Stripe trial auto-provisioned while billing is parked
    INTERNAL_TRIAL = "internal_trial"


# States that grant access to the integration
SUBSCRIPTION_VALID_STATES: frozenset[str] = frozenset(
    {
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.INTERNAL,
        SubscriptionStatus.PARTNER_TRIAL,
        SubscriptionStatus.INTERNAL_TRIAL,
    }
)
# States that require remediation (resubscribe for Stripe users, contact
# account manager for partners)
SUBSCRIPTION_INVALID_STATES: frozenset[str] = frozenset(
    {
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCELED,
        SubscriptionStatus.INCOMPLETE,
        SubscriptionStatus.PARTNER_TRIAL_EXPIRED,
    }
)
