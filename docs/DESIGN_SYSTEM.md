# Recall — Design System (єдиний стандарт UI/UX)

> Один стандарт, якого тримаємось у всьому фронтенді. Стиль: **Samsung One-UI-
> inspired** — м'який, скруглений, повітряний; синьо-фіолетовий акцент на
> світлому тлі. **Тільки світла тема** (темну видалено у Phase 17 — один
> джерело правди по кольорах, менше CSS/JS).
>
> Файли: `templates/index.html`, `static/css/style.css`, `static/js/app.js`.
> Усі кольори/радіуси/анімації — через CSS-змінні в `:root`. **Ніяких
> hard-coded hex у компонентах** (крім бренд-акцентів типу YouTube-red).

---

## 1. Design tokens (`:root` у style.css)

### Колір
| Токен | Значення | Застосування |
|-------|----------|--------------|
| `--primary-color` | `#1c7cf2` | основна дія, акцент, активний стан |
| `--primary-hover` | `#1565d8` | hover основної дії |
| `--background` | `#f5f7fa` | тло сторінки |
| `--surface` | `#ffffff` | картки, поля, модалі |
| `--surface-hover` | `#f8f9fa` | hover поверхонь |
| `--text-primary` | `#1a1a1a` | основний текст |
| `--text-secondary` | `#6c757d` | підписи, допоміжний текст |
| `--border-color` | `#e9ecef` | рамки, роздільники |
| `--shadow` / `--shadow-hover` | `rgba(0,0,0,.08/.12)` | тіні |
| `--success` `--warning` `--error` | `#4caf50` `#ff9800` `#f44336` | статуси |

Акцент-градієнт (hero, головні CTA): `linear-gradient(135deg,#1c7cf2,#7c4dff)`.

### Радіус
`--radius-small: 12px` (поля, дрібні кнопки) · `--radius-medium: 20px` (картки,
панелі) · `--radius-large: 28px` (hero, великі блоки) · `--radius-full: 9999px`
(чипси, круглі кнопки, badge).

### Відступи (scale — використовувати, не довільні px)
`4 · 8 · 12 · 16 · 20 · 24 · 32`. Внутрішній padding картки — 20–24px,
gap між елементами рядка — 8–12px, секції — 24–32px.

### Типографіка
- Шрифт: `'Inter', -apple-system, BlinkMacSystemFont, sans-serif`.
- Розміри: заголовок секції 2rem/700; підзаголовок 1.25rem/600; тіло
  0.92–0.96rem/400; підпис 0.82–0.88rem/`--text-secondary`; badge 0.72rem.
- `line-height: 1.6` для тексту, 1.2–1.3 для заголовків.

### Рух
- Базовий: `--transition: all .3s cubic-bezier(.4,0,.2,1)`.
- Мікро-інтеракції (hover кнопок/чипсів): 0.15s.
- Поява списків/модалей: один staggered fade-in, не розсипати дрібні анімації.
- Спінер: `fa-spinner fa-spin` на час завантаження.

---

## 2. Компоненти (стандарт)

### Кнопки
- **Primary** (`.btn-primary` / акцентні CTA): фон `--primary-color`, текст білий,
  `--radius-small`, padding `10px 18px`, hover → `--primary-hover` + лёгкий
  `translateY(-1px)` + тінь. Для головних — градієнт.
- **Secondary** (`.btn-secondary`): фон `--surface`, рамка `--border-color`,
  текст `--text-primary`; hover → border `--primary-color`, текст темніє.
- **Іконкова кругла** (`.history-btn`, `.settings-btn`): 48×48, `--radius-full`,
  фон `--surface`, hover → `--surface-hover`.
- Завжди `cursor:pointer`, `transition` 0.15s, видимий `:focus-visible`
  (outline 2px `--primary-color`).

### Поля / select (`.one-ui-select`, інпути)
- Фон `--surface`, рамка 1px `--border-color`, `--radius-small`, padding `8–10px 12px`.
- `:focus` → border `--primary-color` + лёгке кільце `box-shadow: 0 0 0 3px rgba(28,124,242,.15)`.
- Placeholder — `--text-secondary`.

### Чипси-фільтри (`.tg-chip` — еталон, застосувати всюди де фільтри)
- `--radius-full`, padding `5px 12px`, рамка `--border-color`, текст
  `--text-secondary`; hover → border акцент; **active** → фон `--primary-color`
  + білий текст. Лічильник у `.tg-chip-n` (пігулка з напівпрозорим тлом).

### Картки / панелі
- Фон `--surface`, `--radius-medium`, тінь `0 2px 20px var(--shadow)`,
  padding 20–24px. Hover (якщо клікабельна) → `--shadow-hover` + `translateY(-2px)`.

### Таби (`.source-tabs` / `.source-tab`)
- Рядок табів у картці; активний — фон `--surface`, акцент-текст/іконка;
  неактивні — `--text-secondary`. Перемикання через `data-action="switch-source"`.

### Badge (`.tg-type-badge`, теги типів)
- `--radius-full`, дрібний шрифт 0.72rem, напівпрозоре сіре тло, `--text-secondary`.

### Toast (`.toast`)
- Правий верх/низ, `--surface`, `--radius-small`, тінь, іконка статусу
  (success/info/error → `--success`/`--primary`/`--error`), авто-зникнення.

### Модаль
- Оверлей `rgba(0,0,0,.4)`, контент `--surface` `--radius-large`, max-width,
  поява fade+scale. Закриття: хрестик + клік по оверлею + Esc.

### Стани
- **Loading**: спінер + текст «Завантажую…».
- **Empty**: центрований приглушений текст + (опц.) підказка дії.
- **Error**: текст `--error` + іконка трикутника.

---

## 3. Інтеракції
- **Hover**: завжди є зворотний зв'язок (колір/тінь/підняття). Тривалість 0.15s.
- **Focus**: видимий focus-ring для клавіатури (`:focus-visible`), не прибирати outline.
- **Delegування подій (КРИТИЧНО)**: click → `data-action`, change →
  `data-change`, input → `data-input`, blur → `data-blur`. Не плутати
  (інакше handler не викликається).
- **Анімація появи**: один акорд (staggered), не сотні дрібних.

---

## 4. Правила
1. Тільки `:root`-токени — без сирих hex/радіусів/таймінгів у компонентах.
2. Один акцент `--primary-color`; вторинні дії — `.btn-secondary`.
3. Нові фільтри — за зразком `.tg-chip`. Нові поля — `.one-ui-select`.
4. Бренд: назва **Recall**; одиниці контенту — «**записів**» (не «дзвінків»),
   бо архів змішаний (дзвінки/файли/документи/Telegram).
5. Тема — лише світла. `[data-theme]`, `prefers-color-scheme`, перемикач — НЕ
   повертати.
6. Текст — українською, нейтрально до типу контенту (не прив'язувати до «дзвінків»).
