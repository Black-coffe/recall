"""File extension helpers. Чисті функції без I/O."""

ALLOWED_AUDIO_EXTENSIONS = frozenset({'mp3', 'mpeg', 'mpga', 'm4a', 'wav', 'webm', 'ogg', 'flac'})
ALLOWED_VIDEO_EXTENSIONS = frozenset({'mp4', 'avi', 'mov', 'mkv', 'wmv'})
ALLOWED_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

# Phase 16A/16B/16C/16D: документи для підвантаження у RAG-архів. Розбираються
# у текст (document_parser.py) і кладуться в transcriptions з source_type='document'.
# pptx — 16B; xlsx/csv — 16C; зображення (OCR) — 16D.
ALLOWED_DOCUMENT_EXTENSIONS = frozenset({
    'pdf', 'docx', 'md', 'markdown', 'txt', 'pptx', 'xlsx', 'csv',
    'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp',
})


def _ext(filename: str) -> str:
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[1].lower()


def allowed_file(filename: str) -> bool:
    """Чи дозволений формат файлу для transcription."""
    return _ext(filename) in ALLOWED_EXTENSIONS


def is_video_file(filename: str) -> bool:
    """Чи це відео файл (потребує extraction аудіо через FFmpeg)."""
    return _ext(filename) in ALLOWED_VIDEO_EXTENSIONS


def is_audio_file(filename: str) -> bool:
    """Чи це аудіо файл."""
    return _ext(filename) in ALLOWED_AUDIO_EXTENSIONS


def allowed_document_file(filename: str) -> bool:
    """Чи дозволений формат документа для підвантаження (Phase 16A)."""
    return _ext(filename) in ALLOWED_DOCUMENT_EXTENSIONS


def is_document_file(filename: str) -> bool:
    """Аліас allowed_document_file — для симетрії з is_video_file/is_audio_file."""
    return _ext(filename) in ALLOWED_DOCUMENT_EXTENSIONS


def format_srt_timestamp(seconds: float) -> str:
    """SRT-формат таймстампа: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}".replace('.', ',')
