# Recall — Design System (the single UI/UX standard)

> One standard that we hold to across the whole frontend. Style: **Samsung One-UI-
> inspired** — soft, rounded, airy; a blue-violet accent on a
> light background. **Light theme only** (the dark one was removed in Phase 17 — a single
> source of truth for colours, less CSS/JS).
>
> Files: `templates/index.html`, `static/css/style.css`, `static/js/app.js`.
> All colours/radii/animations go through CSS variables in `:root`. **No
> hard-coded hex in components** (except brand accents like YouTube-red).

---

## 1. Design tokens (`:root` in style.css)

### Colour
| Token | Value | Usage |
|-------|----------|--------------|
| `--primary-color` | `#1c7cf2` | primary action, accent, active state |
| `--primary-hover` | `#1565d8` | hover of the primary action |
| `--background` | `#f5f7fa` | page background |
| `--surface` | `#ffffff` | cards, fields, modals |
| `--surface-hover` | `#f8f9fa` | hover of surfaces |
| `--text-primary` | `#1a1a1a` | primary text |
| `--text-secondary` | `#6c757d` | captions, auxiliary text |
| `--border-color` | `#e9ecef` | borders, dividers |
| `--shadow` / `--shadow-hover` | `rgba(0,0,0,.08/.12)` | shadows |
| `--success` `--warning` `--error` | `#4caf50` `#ff9800` `#f44336` | statuses |

Accent gradient (hero, main CTAs): `linear-gradient(135deg,#1c7cf2,#7c4dff)`.

### Radius
`--radius-small: 12px` (fields, small buttons) · `--radius-medium: 20px` (cards,
panels) · `--radius-large: 28px` (hero, large blocks) · `--radius-full: 9999px`
(chips, round buttons, badges).

### Spacing (a scale — use it, not arbitrary px)
`4 · 8 · 12 · 16 · 20 · 24 · 32`. Inner padding of a card — 20–24px,
gap between elements of a row — 8–12px, sections — 24–32px.

### Typography
- Font: `'Inter', -apple-system, BlinkMacSystemFont, sans-serif`.
- Sizes: section heading 2rem/700; subheading 1.25rem/600; body
  0.92–0.96rem/400; caption 0.82–0.88rem/`--text-secondary`; badge 0.72rem.
- `line-height: 1.6` for text, 1.2–1.3 for headings.

### Motion
- Base: `--transition: all .3s cubic-bezier(.4,0,.2,1)`.
- Micro-interactions (hover of buttons/chips): 0.15s.
- Appearance of lists/modals: one staggered fade-in, do not scatter small animations.
- Spinner: `fa-spinner fa-spin` while loading.

---

## 2. Components (standard)

### Buttons
- **Primary** (`.btn-primary` / accent CTAs): background `--primary-color`, white text,
  `--radius-small`, padding `10px 18px`, hover → `--primary-hover` + a light
  `translateY(-1px)` + shadow. For the main ones — a gradient.
- **Secondary** (`.btn-secondary`): background `--surface`, border `--border-color`,
  text `--text-primary`; hover → border `--primary-color`, text darkens.
- **Round icon button** (`.history-btn`, `.settings-btn`): 48×48, `--radius-full`,
  background `--surface`, hover → `--surface-hover`.
- Always `cursor:pointer`, `transition` 0.15s, a visible `:focus-visible`
  (outline 2px `--primary-color`).

### Fields / select (`.one-ui-select`, inputs)
- Background `--surface`, border 1px `--border-color`, `--radius-small`, padding `8–10px 12px`.
- `:focus` → border `--primary-color` + a light ring `box-shadow: 0 0 0 3px rgba(28,124,242,.15)`.
- Placeholder — `--text-secondary`.

### Filter chips (`.tg-chip` — the reference, apply wherever there are filters)
- `--radius-full`, padding `5px 12px`, border `--border-color`, text
  `--text-secondary`; hover → accent border; **active** → background `--primary-color`
  + white text. The counter in `.tg-chip-n` (a pill with a semi-transparent background).

### Cards / panels
- Background `--surface`, `--radius-medium`, shadow `0 2px 20px var(--shadow)`,
  padding 20–24px. Hover (if clickable) → `--shadow-hover` + `translateY(-2px)`.

### Tabs (`.source-tabs` / `.source-tab`)
- A row of tabs in a card; the active one — background `--surface`, accent text/icon;
  inactive ones — `--text-secondary`. Switching through `data-action="switch-source"`.

### Badge (`.tg-type-badge`, type tags)
- `--radius-full`, small font 0.72rem, a semi-transparent grey background, `--text-secondary`.

### Toast (`.toast`)
- Top/bottom right, `--surface`, `--radius-small`, shadow, a status icon
  (success/info/error → `--success`/`--primary`/`--error`), auto-dismiss.

### Modal
- Overlay `rgba(0,0,0,.4)`, content `--surface` `--radius-large`, max-width,
  appearance fade+scale. Closing: a cross + a click on the overlay + Esc.

### States
- **Loading**: a spinner + the text "Loading…".
- **Empty**: centred muted text + (optional) an action hint.
- **Error**: `--error` text + a triangle icon.

---

## 3. Interactions
- **Hover**: there is always feedback (colour/shadow/lift). Duration 0.15s.
- **Focus**: a visible focus ring for the keyboard (`:focus-visible`), do not remove the outline.
- **Event delegation (CRITICAL)**: click → `data-action`, change →
  `data-change`, input → `data-input`, blur → `data-blur`. Do not mix them up
  (otherwise the handler is not called).
- **Appearance animation**: one chord (staggered), not hundreds of small ones.

---

## 4. Rules
1. Only `:root` tokens — no raw hex/radii/timings in components.
2. One accent `--primary-color`; secondary actions — `.btn-secondary`.
3. New filters — after the `.tg-chip` pattern. New fields — `.one-ui-select`.
4. Brand: the name **Recall**; content units — "**records**" (not "calls"),
   because the archive is mixed (calls/files/documents/Telegram).
5. The theme is light only. `[data-theme]`, `prefers-color-scheme`, a switcher — do NOT
   bring back.
6. Text is in Ukrainian, neutral to the content type (do not tie it to "calls").
