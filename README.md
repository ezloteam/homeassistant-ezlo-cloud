# Ezlo Cloud HARC

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ezloteam&repository=home-assistant-ezlo-cloud&category=integration)

**Secure remote access to your Home Assistant — from anywhere, with no port forwarding, no VPN, and no public IP.**

Ezlo Cloud HARC (Home Assistant Remote Connection) links your Home Assistant
to the Ezlo cloud through a secure, outbound-only tunnel. Reach your
dashboards, the Companion app, and the REST API at a private
`https://<your-name>.connect.harc.cloud` address — without ever exposing your
home network to the internet.

## Why Ezlo Cloud HARC

- 🔒 **Private by design** — the tunnel only makes outbound connections;
  nothing is opened on your router or firewall.
- 🌍 **Access from anywhere** — your full Home Assistant frontend, the
  Companion app, and the API, all over HTTPS.
- ⚡ **Set up in minutes** — everything is configured from the Home Assistant
  UI. No YAML, no certificates, no networking know-how.
- 🧩 **Just works** — point the Companion app at your remote URL and push
  notifications and remote control work right away.

## Requirements

- Home Assistant 2024.10.0 or newer
- An Ezlo Cloud HARC account — a free trial is available
  ([get started](https://ezlo.com/page/homeassistant))
- A Home Assistant host with outbound internet access

## Installation

This integration is installed through [HACS](https://hacs.xyz).

1. Make sure [HACS](https://hacs.xyz) is installed in your Home Assistant.
2. Use the install badge at the top of this page, or search for
   **Ezlo Cloud HARC** inside HACS.
3. Install the integration and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and choose
   **Ezlo Cloud HARC**.

## Getting started

1. When you add the integration, choose **Log in** or **Create a new account**
   (new accounts start a free trial).
2. After you sign in, your secure tunnel is set up automatically.
3. If prompted, add this small block to your `configuration.yaml` and restart
   — it lets Home Assistant trust the local connection:

   ```yaml
   http:
     use_x_forwarded_for: true
     trusted_proxies:
       - 127.0.0.1
   ```

That's it. Your remote URL appears in the integration options under
**Cloud connection status**.

## Using your remote URL

Open the integration card → **Configure** to find your remote URL (for
example `https://abc123.connect.harc.cloud`). Use it anywhere you'd use your
Home Assistant address:

- Open your dashboards from any browser, away from home.
- Set it as the **External URL** in the Home Assistant Companion app so
  notifications and remote control work on the go.
- Share access with a family member through their own Ezlo account.

## Subscription

Your subscription is managed right inside the integration options:

- **Trial** — started automatically for new accounts.
- **Paid** — upgrade anytime through secure Stripe checkout.
- **Status** — check your current plan under **Subscription status**. If a
  renewal is needed, the integration lets you know.

## Troubleshooting

**The tunnel says "Connected" but the URL won't load.**
Home Assistant needs to trust the local connection. Add the `trusted_proxies`
block shown in [Getting started](#getting-started) and restart.

**"Your session has expired."**
Just sign in again when prompted — the integration reconnects automatically.

**Need to share diagnostics?**
Open the integration → **⋮ → Download diagnostics** for a redacted report you
can attach to a support request.

## Support

- Issues and feature requests:
  <https://github.com/ezloteam/home-assistant-ezlo-cloud/issues>
- Product info and plans: <https://ezlo.com/page/homeassistant>

## License

See the repository for license details.
