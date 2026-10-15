# ADR-037: Outgoing links use RECALL_PUBLIC_URL, a private Tailscale serve address

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k5-brief-cards

## Context
Briefs are read in Telegram, which means on the phone. The plan assumed links to `RECALL_API_URL`
(`http://127.0.0.1:5050`) were fine on this PC only. Plan delta, 2026-10-15 (fix story `k5-brief-cards-03`), verbatim:

> «Ссылки в новых карточках брифинга ведут на адрес Recall на этом компьютере, поэтому с телефона они не откроются.»
>
> Допущення «посилання відкриваються лише на цьому компʼютері — власнику сказати» було хибним: брифінг читають
> у Telegram, тобто з телефона. Рішення власника — Tailscale (уже стоїть на ПК): `tailscale serve` дає приватну
> https-адресу `home-pc.example.ts.net`, видиму лише в tailnet. Нове налаштування `RECALL_PUBLIC_URL` — база
> посилань у картках (порожнє → `RECALL_API_URL`, як було). Проксі serve приходить у Flask як 127.0.0.1, тож
> auth-гейт не змінюється; ціна — весь tailnet (зокрема сервер vps-prod) довірений як localhost, відрізається
> ACL Tailscale на боці власника.

The auth half of this delta ("auth-гейт не змінюється", ACL on the owner's side) was reversed by the next
delta; see ADR-038.

## Options
None recorded. The delta states the chosen shape only. Tailscale was the owner's pick because it was already
installed on the PC.

## Decision
Every link that Recall sends out of the machine (brief cards, "ще N →", record links) is built on
`RECALL_PUBLIC_URL`. When that is empty, it falls back to `RECALL_API_URL`. The address is the private
`tailscale serve` https host, visible only inside the tailnet.

## Consequences
- Links open from the phone when the phone is on the tailnet.
- Recall is now reachable through a local proxy, so the auth gate has to be able to tell proxied requests
  from local ones (ADR-038).
- Any other trade-offs were not recorded.

## Invariants created
- Outgoing links never hard-code `127.0.0.1` or `RECALL_API_URL`. They use the `RECALL_PUBLIC_URL` base,
  falling back to `RECALL_API_URL` only when it is empty.
- The public base is a tailnet-private address (`tailscale serve`). Exposing it beyond the tailnet
  (for example with `tailscale funnel` or a public tunnel) is a new way in and needs its own decision;
  see `docs/defects/proxied-access-trusted-as-local.md`.

## Revisit when
Not recorded.
