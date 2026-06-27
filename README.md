# Ezlo HA Cloud

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant custom integration that connects your Home Assistant instance to
[Ezlo HA Cloud](https://ezlo.com/page/homeassistant), enabling secure remote
access to your Home Assistant without exposing it to the public internet or
configuring port forwarding on your router.

The integration installs and manages an [FRP](https://github.com/fatedier/frp)
client that opens an outbound tunnel to Ezlo's cloud, so you can reach your
Home Assistant from anywhere through the Ezlo HA Cloud service.

## Features

- Secure remote access to Home Assistant via an outbound FRP tunnel
- No port forwarding or public IP required
- Configuration entirely via the Home Assistant UI (config flow + options flow)
- Automatic install and version management of the `frpc` binary for your
  architecture (amd64, arm64, armv7 hard-float, armv6)
- Downloaded `frpc` tarball verified against a pinned SHA-256 hash before
  extraction
- Subscription management surfaced inside the integration options
- Repair issue raised when `configuration.yaml` still needs the
  trusted-proxies block — no automatic edits

## Use cases

- Open the Home Assistant frontend from anywhere on the internet without
  exposing your HA port or installing a VPN.
- Share access with a family member or contractor through their own Ezlo
  account.
- Configure the Home Assistant Companion app's external URL to point at the
  Ezlo HA Cloud URL so push notifications and remote control "just work".
- Bridge automations that need to reach the Home Assistant REST API from
  another network without VPN tunnelling.

## Supported functions

| Function | Notes |
|----------|-------|
| HTTPS access to the Home Assistant frontend | The tunnel exposes the local HA web server on a public `https://<subdomain>.connect.harc.cloud` URL. |
| WebSocket passthrough | Used by the HA frontend, Companion app, and any other websocket client. |
| REST API passthrough | Bearer-token API calls work through the tunnel exactly as they do on the LAN. |
| Trial subscription | Auto-provisioned for new accounts; surfaced in the integration options. |
| Paid subscription | Managed via Stripe Checkout, also surfaced in the integration options. |
| Partner / internal access classes | Operator-managed, no self-serve renewal. |

This integration creates **no entities**, no devices, and no service actions.
All interaction happens through the config and options flows.

## Requirements

- Home Assistant `2024.10.0` or newer
- An Ezlo HA Cloud account and an active subscription
  (a trial is available — see [www.ezlo.com](https://ezlo.com/page/homeassistant))
- A Home Assistant installation that can reach the public internet on outbound
  HTTPS (port 443) and the FRP control port `connect.harc.cloud:7000`
- A Linux x86_64 / aarch64 / armv7 (hard-float) / armv6 host — the
  integration downloads a prebuilt `frpc` binary and verifies its SHA-256
  before extracting

## Installation

This integration is available via [HACS](https://hacs.xyz), the Home Assistant
Community Store.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ezloteam&repository=home-assistant-ezlo-cloud&category=integration)

### Steps

1. Ensure you have [HACS](https://hacs.xyz) installed and configured in your
   Home Assistant instance.
2. Use the button above to open the repository in HACS, or search for
   `Ezlo HA Cloud` from inside HACS.
3. Install the integration and restart Home Assistant.
4. Go to **Settings → Devices & Services → Integrations** and add
   **Ezlo HA Cloud**.

### Manual

1. Copy the `custom_components/ezlohacloud` directory into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## Installation parameters

The initial config flow shows a menu and then one of two forms.

| Step | Field | Type | Required | Description |
|------|-------|------|----------|-------------|
| `user` | (menu) | – | – | Choose **Log in** or **Create a new account**. |
| `login` | `username` | string | yes | Your existing Ezlo Cloud username. |
| `login` | `password` | string | yes | Your existing Ezlo Cloud password. |
| `signup` | `username` | string | yes | New Ezlo Cloud username. |
| `signup` | `email` | string | yes | Contact email (used for billing and trial expiry notices). |
| `signup` | `password` | string | yes | Password for the new account. |

On success, the integration:

1. Installs the FRPC binary under
   `<config>/.storage/ezlohacloud/bin/frpc`.
2. Fetches the per-user tunnel config from
   `https://api.harc.cloud/api/user/<uuid>/server-config` and writes
   `<config>/.storage/ezlohacloud/frpc.toml` (mode `0600`).
3. Starts the FRPC client.
4. Raises a repair issue if `configuration.yaml` doesn't yet trust the
   local reverse proxy. **You must add the block shown in the repair issue
   and restart Home Assistant for remote access to work.**

## Configuration parameters

After setup, the integration options menu (the **Configure** button on the
integration card) exposes the following steps. Each step is just a form or a
menu — there is no YAML configuration.

| Step | Field | Type | Required | Description |
|------|-------|------|----------|-------------|
| `init` | (menu) | – | – | Routes to the right sub-step based on whether you are logged in. |
| `login` | `username` | string | yes | Switch the active Ezlo Cloud session to a different account. |
| `login` | `password` | string | yes | Password for the account above. |
| `signup` | `username` | string | yes | Create another Ezlo Cloud account (rare — only valid before the first successful login). |
| `signup` | `email` | string | yes | Contact email. |
| `signup` | `password` | string | yes | Password. |
| `cloud_status` | (read-only) | – | – | Displays tunnel connection status, account username, and the remote URL. |
| `view_status` | (read-only) | – | – | Shows the current subscription status (live-fetched from the backend, cached for 60s). |
| `subscribe` | (Stripe link) | – | – | Opens Stripe Checkout in your browser to start/resume the paid subscription. |
| `logout` | – | – | – | Clears the local credentials and tears down the tunnel. |
| `advanced` | `api_uri` | string | no | **Hidden unless `Advanced mode` is enabled in your HA profile.** Override the Ezlo Cloud API endpoint (QA/dev only). Clear the field to revert to `https://api.harc.cloud`. |

A **Reconfigure** option is available in the integration menu and uses the
same form as `login` to change credentials in place without removing the
entry.

## Data flow

The integration is event-driven; there is no polling on a fixed schedule.

| When | What | Endpoint |
|------|------|----------|
| Initial setup or reauth | Issue a JWT for the Home Assistant instance | `POST https://api.harc.cloud/api/auth/login` |
| Initial signup | Create a new account and provision a trial | `POST https://api.harc.cloud/api/auth/signup` |
| Setup, reload | Fetch the per-user FRP server config | `GET https://api.harc.cloud/api/user/{uuid}/server-config` |
| On every `subscribe` step open | Read the public Stripe price id | `GET https://api.harc.cloud/api/integration/config` |
| On `subscribe` step submit | Mint a Stripe Checkout session | `POST https://api.harc.cloud/api/stripe/create-session` |
| Every 5s for up to 15 min after Stripe Checkout opens | Poll for subscription activation | `GET https://api.harc.cloud/api/subscription/status` |
| On `view_status` step open (≤1 per 60s) | Display the live subscription status | `GET https://api.harc.cloud/api/subscription/status` |
| Continuous | Long-lived outbound FRP control channel | TCP `connect.harc.cloud:7000` |

All HTTP calls go through Home Assistant's shared HTTP client (`httpx` for
JSON APIs, `aiohttp` for the streaming server-config fetch).

## Subscription states

The integration keeps you informed about your Ezlo HA Cloud subscription
status. If your subscription is past due, canceled, incomplete, or a partner
trial has expired, the integration pauses the tunnel and surfaces a prompt
in the integration options to renew or contact your account manager. The
full state list is:

| State | Source | Grants access? |
|-------|--------|----------------|
| `trialing` | Stripe | yes |
| `active` | Stripe | yes |
| `incomplete` | Stripe | no — resubscribe |
| `past_due` | Stripe | no — update payment method |
| `canceled` | Stripe | no — resubscribe |
| `internal` | Operator-managed | yes |
| `internal_trial` | Operator-managed | yes |
| `partner_trial` | Operator-managed | yes |
| `partner_trial_expired` | Operator-managed | no — contact your account manager |

## Examples

After setup, the integration exposes one URL — the remote URL of your Home
Assistant instance. Use it like any other HA URL:

### Companion app remote URL

1. Open the Home Assistant Companion app on your phone.
2. Go to **Settings → Companion app → Server → External URL**.
3. Paste the URL shown in **Integration options → Cloud connection status →
   Remote URL** (e.g. `https://abc123.connect.harc.cloud`).

### Automation: notify a friend with the remote URL

```yaml
automation:
  - alias: "Share remote URL with family"
    trigger:
      - platform: state
        entity_id: input_boolean.share_access
        to: "on"
    action:
      - service: notify.family
        data:
          message: >
            Use this URL to access our Home Assistant:
            https://abc123.connect.harc.cloud
```

(The integration itself does not create any entities — `abc123` is the
subdomain reported in the integration options.)

## Known limitations

- Only one Ezlo HA Cloud config entry is supported per Home Assistant
  instance — re-running the config flow aborts with `already_configured`.
- The FRPC binary is downloaded from
  `github.com/fatedier/frp/releases`; the host needs outbound HTTPS to
  GitHub on first install (and on every version bump). The binary is
  cached under `<config>/.storage/ezlohacloud/bin/frpc`.
- Linux-only architectures: `amd64`, `arm64`, `arm` (armv6), `arm_hf`
  (armv7 hard-float).
- After first setup, a Home Assistant restart is required for the
  trusted-proxies block to take effect. The integration raises a repair
  issue with the exact block to add — it does **not** edit your
  `configuration.yaml` automatically.
- Subscription state is cached in memory for 60 seconds across menu opens
  to avoid hammering the backend; a manual reload or restart clears the
  cache.
- The integration does not currently support a "dry run" mode against a
  staging backend except via the hidden **Advanced** options step.

## Troubleshooting

### "Configuration.yaml needs to be updated" repair

Your Home Assistant `configuration.yaml` is missing the trusted-proxies
block for the local FRPC reverse proxy. Add this block and restart:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
```

The repair issue closes automatically once the file is updated and Home
Assistant is restarted.

### The tunnel shows "Connected" but the URL is broken

Almost always the trusted-proxies block above is missing — Home Assistant's
HTTP component rejects the proxied requests as untrusted. Apply the block
and restart.

### "Your session has expired"

The Ezlo Cloud token has expired or been revoked. A repair / re-auth flow
is raised automatically. Re-enter your credentials.

### "Subscription expired"

Open **Integration options → Resubscribe** to launch the Stripe Checkout
flow.

### FRPC won't start

Look in the Home Assistant logs for lines beginning with
`custom_components.ezlohacloud`. The most common causes are:

- Architecture not in the supported list (`amd64`, `arm64`, `arm`, `arm_hf`).
- `<config>/.storage/ezlohacloud/bin/frpc` was deleted while the
  integration was running — reload the integration.
- Outbound traffic to `connect.harc.cloud:7000` is blocked by a firewall.

### Generating diagnostics

From **Settings → Devices & Services → Integrations → Ezlo HA Cloud → ⋮ →
Download diagnostics** you can download a JSON file with redacted entry
data, the installed FRPC version, process state, and connection state.
Attach it to bug reports.

## Removal

To remove the integration:

1. **Settings → Devices & Services → Integrations → Ezlo HA Cloud → ⋮ →
   Delete**.
2. Optionally remove the trusted-proxies block from your
   `configuration.yaml` if you added it for this integration.
3. Optionally delete `<config>/.storage/ezlohacloud/` to remove the
   cached FRPC binary and tunnel config.

## Support

- Issues and feature requests:
  <https://github.com/ezloteam/home-assistant-ezlo-cloud/issues>
- Product documentation: <https://ezlo.com/page/homeassistant>

## License

See the repository for license details.
