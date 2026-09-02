# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated and approved through `docs/PROJECT_PLAN.md`: FastAPI and Python for the API,
controller, and privileged worker contract; React and TypeScript for the WebUI; SQLite for
single-host persistence; native Debian systemd, network namespace, nftables, OpenVPN,
sing-box, and HAProxy for the production data plane.

## Users

The primary user is the owner and operator of one VPS. They configure several stable local
SOCKS5 ports, inspect regional exit health, test candidates, switch routes, and diagnose
failures. The product is not intended to serve anonymous public users.

## Product Purpose

Gate turns volatile VPN Gate volunteer nodes into a small set of stable, region-oriented
SOCKS5 entry points. Success means the operator can keep client proxy settings unchanged
while Gate discovers, measures, selects, and safely replaces the underlying exits.

## Positioning

Gate selects exits using measurements made through real tunnels from the target VPS, then
switches a stable regional port only after the candidate passes end-to-end validation. It
does not treat VPN Gate's public ranking as proof of local route quality.

## Operating Context

- Development happens on Windows.
- Production runs on the Debian VPS reached through the SSH alias `HK-Aliyun`.
- The operator reaches the WebUI and SOCKS ports through SSH local forwarding by default.
- Routine work is repeated operational scanning: identify unhealthy regions, inspect
  candidates, test, switch, lock, and review the result or rollback reason.

## Capabilities and Constraints

- Preset regional ports cover Japan, Korea, North America, Europe, and Southeast Asia.
- VPN Gate provides country, not city, metadata; unavailable regions remain unavailable
  rather than silently falling back to a wrong country.
- Each active or candidate tunnel is isolated in its own Linux network namespace.
- A kill switch must prevent fallback to the VPS public route when a VPN fails.
- Remote OpenVPN profiles are untrusted and must be parsed and rebuilt from a strict
  allowlist before privileged use.
- The MVP guarantees SOCKS5 TCP and remote DNS behavior. UDP is a later, separately tested
  capability.
- The target is one VPS, not a multi-host control plane.

## Brand Commitments

The product name is Gate. Product language is concise, factual, and operations-oriented.
Status labels must describe reality directly and must not conceal unavailable exits or
failed validation.

## Evidence on Hand

- `docs/PROJECT_PLAN.md` is the approved functional and technical baseline.
- `README.md` records the preset ports and intended SSH access workflow.
- A live VPN Gate API inspection confirmed the current CSV shape and country-only metadata.
- No logo, customer claims, testimonials, commercial SLA, or production screenshots exist;
  future work must not fabricate them.

## Product Principles

1. Fixed entry, dynamic exit.
2. Measure from the VPS and verify before switching.
3. Fail closed: an unavailable tunnel must never leak to the VPS route.
4. Show exact state and failure reasons instead of implying false availability.
5. Keep frequent operator actions fast, reversible, and auditable.

## Accessibility & Inclusion

The WebUI must remain keyboard operable, expose programmatic labels and focus states, avoid
color-only status communication, respect reduced-motion preferences, and remain usable at
360 px mobile width and common desktop widths.
