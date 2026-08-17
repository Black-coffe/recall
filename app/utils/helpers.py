"""
Вспомогательные функции
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
    Генерирует безопасное имя файла с временной меткой
    """
    if not original_filename:
        original_filename = "file"
    
    # Получаем расширение
    ext = Path(original_filename).suffix.lower()
    
    # Создаем безопасное имя файла
    safe_name = secure_filename(Path(original_filename).stem)
    if not safe_name:
        safe_name = "file"
    
    # Добавляем временную метку
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    
    if prefix:
        filename = f"{prefix}_{timestamp}_{safe_name}{ext}"
    else:
        filename = f"{timestamp}_{safe_name}{ext}"
    
    return filename


def format_duration(seconds: int) -> str:
    """
    Форматирует продолжительность в читаемый вид
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
    Форматирует размер файла в читаемый вид
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
    Очищает старые файлы в директории
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
                    logger.info(f"Удален старый файл: {file_path}")
    except Exception as e:
        logger.error(f"Ошибка очистки файлов в {directory}: {e}")
    
    return deleted_count


def check_ffmpeg_availability() -> bool:
    """
    Проверяет доступность FFmpeg в системе
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
    Сохраняет данные в JSON файл
    """
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения JSON файла {filepath}: {e}")
        return False


def load_json_file(filepath: str) -> Optional[Dict[str, Any]]:
    """
    Загружает данные из JSON файла
    """
    try:
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    except Exception as e:
        logger.error(f"Ошибка загрузки JSON файла {filepath}: {e}")
        return None


def create_temp_file(suffix: str = None, prefix: str = None, dir: str = None) -> str:
    """
    Создает временный файл и возвращает путь к нему
    """
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=dir)
    os.close(fd)  # Закрываем файловый дескриптор
    return temp_path


def safe_remove_file(filepath: str) -> bool:
    """
    Безопасно удаляет файл
    """
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug(f"Удален файл: {filepath}")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления файла {filepath}: {e}")
        return False


def calculate_eta(start_time: datetime, current_progress: float) -> Optional[str]:
    """
    Вычисляет примерное время завершения
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
    Обрезает текст до указанной длины
    """
    if not text or len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix


def normalize_text(text: str) -> str:
    """
    Нормализует текст (удаляет лишние пробелы, переносы строк)
    """
    if not text:
        return ""
    
    # Удаляем лишние пробелы и переносы строк
    normalized = " ".join(text.split())
    
    return normalized.strip()


def batch_items(items: List[Any], batch_size: int) -> List[List[Any]]:
    """
    Разбивает список на батчи заданного размера
    """
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append(items[i:i + batch_size])
    return batches