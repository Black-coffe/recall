"""File extension helpers. Pure functions without I/O."""

ALLOWED_AUDIO_EXTENSIONS = frozenset({'mp3', 'mpeg', 'mpga', 'm4a', 'wav', 'webm', 'ogg', 'flac'})
ALLOWED_VIDEO_EXTENSIONS = frozenset({'mp4', 'avi', 'mov', 'mkv', 'wmv'})
ALLOWED_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

# Phase 16A/16B/16C/16D: documents to upload into the RAG archive. They are parsed
# into text (document_parser.py) and stored in transcriptions with source_type='document'.
# pptx — 16B; xlsx/csv — 16C; images (OCR) — 16D.
ALLOWED_DOCUMENT_EXTENSIONS = frozenset({
    'pdf', 'docx', 'md', 'markdown', 'txt', 'pptx', 'xlsx', 'csv',
    'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp',
})


def _ext(filename: str) -> str:
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[1].lower()


def allowed_file(filename: str) -> bool:
    """Whether the file format is allowed for transcription."""
    return _ext(filename) in ALLOWED_EXTENSIONS


def is_video_file(filename: str) -> bool:
    """Whether this is a video file (needs audio extraction via FFmpeg)."""
    return _ext(filename) in ALLOWED_VIDEO_EXTENSIONS


def is_audio_file(filename: str) -> bool:
    """Whether this is an audio file."""
    return _ext(filename) in ALLOWED_AUDIO_EXTENSIONS


def allowed_document_file(filename: str) -> bool:
    """Whether the document format is allowed for upload (Phase 16A)."""
    return _ext(filename) in ALLOWED_DOCUMENT_EXTENSIONS


def is_document_file(filename: str) -> bool:
    """Alias of allowed_document_file — for symmetry with is_video_file/is_audio_file."""
    return _ext(filename) in ALLOWED_DOCUMENT_EXTENSIONS


def format_srt_timestamp(seconds: float) -> str:
    """SRT timestamp format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}".replace('.', ',')
