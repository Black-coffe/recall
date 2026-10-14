# ADR-032: Screen OCR must return word bounding boxes

- Status: proposed
- Date: 2026-10-13
- Spec: docs/specs/video-visual-timeline

## Context
Story 01's measurement found that Tesseract was not installed and that `ocr_available()` had
always returned False since a broken import. The owner chose to install Tesseract. The other
path, OCR through a vision model, was rejected. Plan delta, verbatim:

> 2026-10-13, історія 01, з виміру: Tesseract у системі не стояв, а `document_parser.ocr_available()` з 14.08
> (коміт 67e71e0) завжди давав False — незімпортований `silence_pytesseract_console_windows`, NameError
> ковтався. Власник: «Поставить Tesseract (Рекомендую)». Поставлено UB-Mannheim 5.4, мовні пакети в
> `%LOCALAPPDATA%\Recall\tessdata(_fast)`; `document_parser.py` додано до `## Files` історії 01 (імпорт +
> пошук бінаря/пакетів, 3 нові налаштування OCR_*). Відкинуто: OCR vision-моделлю — нема рамок слів, а без них
> не зафарбувати секрети.

## Options
1. Local Tesseract (`image_to_data`, word-level boxes). Needs a system install and language packs.
   This is the one chosen.
2. OCR through a vision model. Rejected because it gives no word boxes, and without boxes the
   secrets can't be painted over.

## Decision
Option 1. The deciding factor is word boxes: secret redaction (ADR-030) paints the boxes of the
detected words, and a frame can only be redacted if its OCR located those words.

## Consequences
Recorded: Recall now depends on a system Tesseract (UB-Mannheim 5.4) with language packs in
`%LOCALAPPDATA%\Recall\tessdata(_fast)`, plus three new `OCR_*` settings. Not recorded: how a
fresh install gets Tesseract, or what the timeline does when it is missing beyond
`ocr_available()` returning False.

## Invariants created
- Any OCR engine used on screen frames must return per-word bounding boxes. An engine that
  returns text only can't replace it, though it may add text alongside it (`vision_text`), which
  still gets masked.
- Secret detection runs on OCR words that have coordinates.

## Revisit when
Not recorded. A natural trigger would be a vision or OCR model that returns word-level boxes,
but the delta does not state one.
