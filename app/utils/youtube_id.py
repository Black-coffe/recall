"""YouTube URL → video ID parser. Pure function without I/O."""
import logging
import re

logger = logging.getLogger(__name__)


_PATTERNS = [
    # Standard formats
    r'(?:youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
    r'(?:youtu\.be\/)([a-zA-Z0-9_-]{11})',
    r'(?:youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
    # Live streams
    r'(?:youtube\.com\/live\/)([a-zA-Z0-9_-]{11})',
    # YouTube Shorts
    r'(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
    # Mobile versions
    r'(?:m\.youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
    # With timestamp and other parameters
    r'(?:youtube\.com\/watch\?.*v=)([a-zA-Z0-9_-]{11})',
    # YouTube Music
    r'(?:music\.youtube\.com\/watch\?v=)([a-zA-Z0-9_-]{11})',
]


def extract_youtube_id(url: str):
    """Extracts the YouTube video ID from a URL.

    Supports: standard, short (youtu.be), embed, live, shorts, music, mobile.

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
