"""Constants for the Ezlo HA Cloud integration."""

DOMAIN = "ezlohacloud"

STORAGE_KEY = "ezlo_user_data"
STORAGE_VERSION = 1

EZLO_API_URI = "https://harc.ezlo.com"
HOMEASSISTANT_HOST = "homeassistant.local"

# Subscription status values (from Stripe)
SUBSCRIPTION_TRIALING = "trialing"
SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_PAST_DUE = "past_due"
SUBSCRIPTION_CANCELED = "canceled"
SUBSCRIPTION_INCOMPLETE = "incomplete"

# Non-Stripe access classes (managed by Ezlo operators, not self-serve)
SUBSCRIPTION_INTERNAL = "internal"
SUBSCRIPTION_PARTNER_TRIAL = "partner_trial"
SUBSCRIPTION_PARTNER_TRIAL_EXPIRED = "partner_trial_expired"
# Non-Stripe trial auto-provisioned while billing is parked (BILLING_MODE=internal_trial)
SUBSCRIPTION_INTERNAL_TRIAL = "internal_trial"

# States that grant access to the integration
SUBSCRIPTION_VALID_STATES = (
    SUBSCRIPTION_TRIALING,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_INTERNAL,
    SUBSCRIPTION_PARTNER_TRIAL,
    SUBSCRIPTION_INTERNAL_TRIAL,
)
# States that require remediation (resubscribe for Stripe users, contact
# account manager for partners)
SUBSCRIPTION_INVALID_STATES = (
    SUBSCRIPTION_PAST_DUE,
    SUBSCRIPTION_CANCELED,
    SUBSCRIPTION_INCOMPLETE,
    SUBSCRIPTION_PARTNER_TRIAL_EXPIRED,
)
