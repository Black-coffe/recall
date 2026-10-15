# ADR-038: Proxied requests are not local; tailnet devices are allowlisted by IP, deny by default

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k5-brief-cards

## Context
Recall lets `127.0.0.1` in without a key. `tailscale serve` (ADR-037) delivers every tailnet request as
`127.0.0.1`. Plan delta, 2026-10-15 (fix story `k5-brief-cards-04`), verbatim:

> «После включения Tailscale-доступа сервер vps-prod сможет открыть весь Recall без пароля, пока вы сами не закроете ему доступ.»
>
> Ціну «весь tailnet довірений як localhost, відрізається ACL на боці власника» з поправки 1 знято: захист — у коді
> за замовчуванням. `tailscale serve` перезаписує `X-Forwarded-For` tailnet-IP клієнта (`ipn/ipnlocal/serve.go`,
> `addProxyForwardedHeaders`); `Tailscale-User-Login` однаковий для всіх нетегованих вузлів власника, тож пристрої
> розрізняються лише за IP. Запит із localhost, що несе `X-Forwarded-For` або `Tailscale-User-Login`, — проксійований:
> до `/api/*` пускається лише з IP у `RECALL_PROXY_ALLOWED_IPS` (порожньо — нікого) або з `RECALL_API_KEY`.

## Options
1. Trust the whole tailnet as localhost and cut nodes off with a Tailscale ACL on the owner's side. This was
   delta 1's choice, rejected by the owner: the path is open until the owner remembers to close it.
2. Detect proxied requests in code and allowlist by tailnet IP, denying by default. This was chosen.
3. Tell devices apart by `Tailscale-User-Login`. Rejected: every untagged node of the owner carries the same login.

## Decision
Option 2. A request from localhost that carries `X-Forwarded-For` or `Tailscale-User-Login` is proxied, not
local. It reaches `/api/*` only from an IP in `RECALL_PROXY_ALLOWED_IPS` (empty means nobody) or with a valid
`RECALL_API_KEY`. The deciding factor: the guard must live in code and be on by default, not in a setting
outside Recall that the owner has to remember.

## Consequences
- Turning on `tailscale serve` no longer exposes the archive to other tailnet nodes (vps-prod).
- The owner must list the phone's tailnet IP in `RECALL_PROXY_ALLOWED_IPS` before links open from it.
- The gate change sits in `app/core/auth.py`, outside the pipes module.
- Regression check: `docs/defects/proxied-access-trusted-as-local.md` (`scripts/check_proxy_trust.py`).

## Invariants created
- "Local" is never decided by the socket address alone. A localhost request carrying `X-Forwarded-For` or
  `Tailscale-User-Login` is treated as proxied.
- Devices are told apart by tailnet IP, never by `Tailscale-User-Login`.
- Any new way in (proxy, tunnel, bind) denies by default. "Configure it yourself later" is not a guard.
- Paths outside `/api/*` (the SPA shell, no data) and `/api/health` stay open.

## Open issue (owner, 2026-10-15, raised during the ADR harvest)
> «Если телефон зайдёт в Recall по своему IPv6-адресу в Tailscale, а в разрешённом списке будет только адрес 100.x, ссылки из брифинга могут не открыться.»

A tailnet node has two addresses: IPv4 `100.x` and IPv6 `fd7a:115c:a1e0::/48`. `X-Forwarded-For` carries whichever
one the client used. The delta does not say whether the allowlist accounts for this. Unresolved: it needs a fix
story, not an archivist decision.

Owner's choice (2026-10-15): «Давай как лучше вариантом». The archivist offered two options: (1) list both
addresses per device in `RECALL_PROXY_ALLOWED_IPS`, or (2) admit both of a listed device's addresses in code.
Decided in the main session the same day, after the cost of option 2 was named (the gate would have to ask
Tailscale `whois` per request and list devices by name): **option 1**. Both addresses of each allowed device go
into `RECALL_PROXY_ALLOWED_IPS` at setup, entry over each is checked live; the setting's description says a
device has two addresses. No code change in the gate.

Access level (owner, 2026-10-15): an allowlisted device gets FULL access, not read-only — «Пусть будет полный». Offered and declined: proxied requests limited to reads. Revocation: drop the device from Tailscale or from `RECALL_PROXY_ALLOWED_IPS` and restart; the next request gets 401.

## Revisit when
Not recorded. The open issue above is a known trigger.
