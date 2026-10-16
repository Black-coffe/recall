"""
Helper functions
"""

import os
import json
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
from werkzeug.utils import secure_filename

from app.core.logger import get_logger
from app.utils.proc import NO_WINDOW

logger = get_logger(__name__)


def generate_secure_filename(original_filename: str, prefix: str = None) -> str:
    """
    Generates a safe file name with a timestamp
    """
    if not original_filename:
        original_filename = "file"
    
    # Get the extension
    ext = Path(original_filename).suffix.lower()
    
    # Create a safe file name
    safe_name = secure_filename(Path(original_filename).stem)
    if not safe_name:
        safe_name = "file"
    
    # Add a timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    
    if prefix:
        filename = f"{prefix}_{timestamp}_{safe_name}{ext}"
    else:
        filename = f"{timestamp}_{safe_name}{ext}"
    
    return filename


def format_duration(seconds: int) -> str:
    """
    Formats a duration into a readable form
    """
    if not seconds or seconds < 0:
        return "0:00"
    
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"


def format_file_size(size_bytes: int) -> str:
    """
    Formats a file size into a readable form
    """
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    
    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1
    
    return f"{size:.1f} {size_names[i]}"


def cleanup_old_files(directory: str, max_age_days: int = 7, pattern: str = "*") -> int:
    """
    Cleans up old files in a directory
    """
    if not os.path.exists(directory):
        return 0
    
    cutoff_time = datetime.now() - timedelta(days=max_age_days)
    deleted_count = 0
    
    try:
        for file_path in Path(directory).glob(pattern):
            if file_path.is_file():
                file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_mtime < cutoff_time:
                    file_path.unlink()
                    deleted_count += 1
                    logger.info(f"Removed old file: {file_path}")
    except Exception as e:
        logger.error(f"Error cleaning up files in {directory}: {e}")
    
    return deleted_count


def check_ffmpeg_availability() -> bool:
    """
    Checks whether FFmpeg is available on the system
    """
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def save_json_file(data: Dict[str, Any], filepath: str) -> bool:
    """
    Saves data to a JSON file
    """
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return True
    except Exception as e:
        logger.error(f"Error saving JSON file {filepath}: {e}")
        return False


def load_json_file(filepath: str) -> Optional[Dict[str, Any]]:
    """
    Loads data from a JSON file
    """
    try:
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    except Exception as e:
        logger.error(f"Error loading JSON file {filepath}: {e}")
        return None


def create_temp_file(suffix: str = None, prefix: str = None, dir: str = None) -> str:
    """
    Creates a temporary file and returns its path
    """
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir)
    os.close(fd)  # Close the file descriptor
    return temp_path


def safe_remove_file(filepath: str) -> bool:
    """
    Safely deletes a file
    """
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug(f"Removed file: {filepath}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error removing file {filepath}: {e}")
        return False


def calculate_eta(start_time: datetime, current_progress: float) -> Optional[str]:
    """
    Computes the approximate completion time
    """
    if current_progress <= 0 or current_progress >= 100:
        return None
    
    elapsed = (datetime.now() - start_time).total_seconds()
    if elapsed <= 0:
        return None
    
    total_time = elapsed / (current_progress / 100.0)
    remaining_time = total_time - elapsed
    
    if remaining_time <= 0:
        return "0:00"
    
    return format_duration(int(remaining_time))


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Truncates text to the given length
    """
    if not text or len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix


def normalize_text(text: str) -> str:
    """
    Normalizes text (removes extra spaces and line breaks)
    """
    if not text:
        return ""
    
    # Remove extra spaces and line breaks
    normalized = " ".join(text.split())
    
    return normalized.strip()


def batch_items(items: List[Any], batch_size: int) -> List[List[Any]]:
    """
    Splits a list into batches of the given size
    """
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append(items[i:i + batch_size])
    return batches