# Ezlo HA Cloud

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant custom integration that connects your Home Assistant instance to
[Ezlo HA Cloud](https://ezlo.com/page/homeassistant), enabling secure remote access to
your Home Assistant without exposing it to the public internet or configuring port
forwarding on your router.

The integration installs and manages an [FRP](https://github.com/fatedier/frp)
client that opens an outbound tunnel to Ezlo's cloud, so you can reach your
Home Assistant from anywhere through the Ezlo HA Cloud service.

## Features

- Secure remote access to Home Assistant via an outbound FRP tunnel
- No port forwarding or public IP required
- Configuration via the Home Assistant UI (config flow)
- Automatic install and version management of the `frpc` binary for your
  architecture (amd64, arm64, armv7, armv6, i386)
- Subscription management surfaced inside the integration options

## Requirements

- Home Assistant `2024.10.0` or newer
- An Ezlo HA Cloud account and an active subscription
  (a trial is available — see [www.ezlo.com](https://ezlo.com/page/homeassistant))
- A Home Assistant installation that can reach the public internet on outbound
  HTTPS

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** → menu → **Custom repositories**.
3. Add `https://github.com/ezloteam/home-assistant-ezlo-cloud` as an
   **Integration** category repository.
4. Search for **Ezlo HA Cloud** and install it.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/ezlohacloud` directory into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to **Settings** → **Devices & Services** →
   **Add Integration**.
2. Search for **Ezlo HA Cloud**.
3. Sign in (or sign up) with your Ezlo account and complete the flow.
4. The integration installs `frpc`, configures the tunnel, and updates the
   `http.use_x_forwarded_for` / `http.trusted_proxies` settings in your
   `configuration.yaml` to allow the local reverse proxy. **Restart Home
   Assistant** after the first setup so the trusted-proxy change takes effect.

## Subscription states

The integration keeps you informed about your Ezlo HA Cloud subscription
status. If your subscription is past due, canceled, incomplete, or a partner
trial has expired, the integration pauses the tunnel and surfaces a prompt in
the integration options to renew or contact your account manager.

## Support

- Issues and feature requests:
  <https://github.com/ezloteam/home-assistant-ezlo-cloud/issues>
- Product documentation: <https://ezlo.com/page/homeassistant>

## License

See the repository for license details.
