"""YouTube URL → video ID parser. Чиста функція без I/O."""
import logging
import re

logger = logging.getLogger(__name__)


_PATTERNS = [
    # Стандартные форматы
    r'(?:youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
    r'(?:youtu\.be\/)([a-zA-Z0-9_-]{11})',
    r'(?:youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
    # Live стримы
    r'(?:youtube\.com\/live\/)([a-zA-Z0-9_-]{11})',
    # YouTube Shorts
    r'(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
    # Mobile версии
    r'(?:m\.youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
    # С timestamp и другими параметрами
    r'(?:youtube\.com\/watch\?.*v=)([a-zA-Z0-9_-]{11})',
    # YouTube Music
    r'(?:music\.youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
]


def extract_youtube_id(url: str):
    """Извлекает YouTube video ID из URL.

    Поддерживает: standard, short (youtu.be), embed, live, shorts, music, mobile.

    Returns:
        str (11-character video id) | None
    """
    if not url:
        return None
    for pattern in _PATTERNS:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            logger.info(f"Extracted YouTube ID: {video_id} from URL: {url}")
            return video_id
    logger.warning(f"Could not extract YouTube ID from URL: {url}")
    return None
