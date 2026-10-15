# ADR-037: Outgoing links use RECALL_PUBLIC_URL, a private Tailscale serve address

- Status: proposed
- Date: 2026-10-15
- Spec: docs/specs/k5-brief-cards

## Context
Briefs are read in Telegram, which means on the phone. The plan assumed links to `RECALL_API_URL`
(`http://127.0.0.1:5050`) were fine on this PC only. Plan delta, 2026-10-15 (fix story `k5-brief-cards-03`), verbatim:

> `«Ссылки в новых карточках брифинга ведут на адрес Recall на этом компьютере, поэтому с телефона они не откроются.»` (Links in the new briefing cards lead to the Recall address on this computer, so they will not open from the phone.)
>
> The assumption "links open only on this computer — tell the owner" was wrong: the briefing is read
> in Telegram, i.e. from the phone. The owner's decision — Tailscale (already installed on the PC): `tailscale serve` gives a private
> https address `home-pc.example.ts.net`, visible only in the tailnet. The new setting `RECALL_PUBLIC_URL` is the base of
> links in the cards (empty → `RECALL_API_URL`, as it was). The serve proxy arrives in Flask as 127.0.0.1, so
> the auth gate does not change; the cost — the whole tailnet (including the server vps-prod) is trusted as localhost, cut off
> by the Tailscale ACL on the owner's side.

The auth half of this delta ("the auth gate does not change", ACL on the owner's side) was reversed by the next
delta; see ADR-038.

## Options
None recorded. The delta states the chosen shape only. Tailscale was the owner's pick because it was already
installed on the PC.

## Decision
Every link that Recall sends out of the machine (brief cards, "N more →", record links) is built on
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
