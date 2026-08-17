"""
Валидаторы для входных данных
"""

import os
import re
from pathlib import Path
from typing import List, Optional, Union
from werkzeug.datastructures import FileStorage
from urllib.parse import urlparse

from app.core.exceptions import ValidationError
from config import get_config


class FileValidator:
    """Валидатор файлов"""
    
    @staticmethod
    def validate_audio_file(file: FileStorage) -> bool:
        """Проверяет валидность аудио файла"""
        config = get_config()
        
        if not file or not file.filename:
            raise ValidationError("Файл не выбран")
        
        # Проверяем расширение
        ext = Path(file.filename).suffix.lower().lstrip('.')
        if ext not in config.ALLOWED_EXTENSIONS:
            allowed = ", ".join(config.ALLOWED_EXTENSIONS)
            raise ValidationError(f"Неподдерживаемый формат. Разрешены: {allowed}")
        
        # Проверяем размер (если доступен)
        if hasattr(file, 'content_length') and file.content_length:
            max_size = config.MAX_CONTENT_LENGTH
            if file.content_length > max_size:
                max_mb = max_size // (1024 * 1024)
                raise ValidationError(f"Файл слишком большой. Максимум: {max_mb} MB")
        
        return True
    
    @staticmethod
    def validate_file_path(filepath: str, base_dir: str) -> str:
        """Проверяет и нормализует путь к файлу"""
        if not filepath:
            raise ValidationError("Путь к файлу не указан")
        
        # Нормализуем путь
        normalized_path = os.path.abspath(filepath)
        base_dir_abs = os.path.abspath(base_dir)
        
        # Проверяем что файл находится в разрешенной директории
        if not normalized_path.startswith(base_dir_abs):
            raise ValidationError("Недопустимый путь к файлу")
        
        # Проверяем существование файла
        if not os.path.exists(normalized_path):
            raise ValidationError("Файл не найден")
        
        return normalized_path


class YouTubeValidator:
    """Валидатор YouTube URL"""
    
    YOUTUBE_URL_PATTERNS = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/([a-zA-Z0-9_-]+)'
    ]
    
    @classmethod
    def validate_youtube_url(cls, url: str) -> str:
        """Проверяет и нормализует YouTube URL"""
        if not url:
            raise ValidationError("URL не указан")
        
        url = url.strip()
        
        # Проверяем соответствие паттернам YouTube
        for pattern in cls.YOUTUBE_URL_PATTERNS:
            if re.match(pattern, url):
                return url
        
        raise ValidationError("Некорректный YouTube URL")
    
    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        """Извлекает ID видео из YouTube URL"""
        patterns = [
            r'(?:v=|/)([0-9A-Za-z_-]{11}).*',
            r'(?:embed/)([0-9A-Za-z_-]{11})',
            r'(?:youtu\.be/)([0-9A-Za-z_-]{11})'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None


class ModelValidator:
    """Валидатор параметров модели"""
    
    SUPPORTED_MODELS = ["tiny", "base", "small", "medium", "large"]
    SUPPORTED_LANGUAGES = [
        "uk", "en", "ru", "es", "fr", "de", "it", "pt", "pl", "tr", 
        "ja", "ko", "zh", "auto"
    ]
    
    @classmethod
    def validate_model_name(cls, model_name: str) -> str:
        """Проверяет название модели"""
        if not model_name:
            raise ValidationError("Модель не указана")
        
        if model_name not in cls.SUPPORTED_MODELS:
            supported = ", ".join(cls.SUPPORTED_MODELS)
            raise ValidationError(f"Неподдерживаемая модель. Поддерживаются: {supported}")
        
        return model_name
    
    @classmethod
    def validate_language(cls, language: str) -> str:
        """Проверяет код языка"""
        if not language:
            return "auto"  # По умолчанию автоопределение
        
        if language not in cls.SUPPORTED_LANGUAGES:
            supported = ", ".join(cls.SUPPORTED_LANGUAGES)
            raise ValidationError(f"Неподдерживаемый язык. Поддерживаются: {supported}")
        
        return language


class PaginationValidator:
    """Валидатор параметров пагинации"""
    
    @staticmethod
    def validate_pagination(page: Union[str, int], per_page: Union[str, int], max_per_page: int = 100) -> tuple:
        """Проверяет и нормализует параметры пагинации"""
        try:
            page = int(page) if page else 1
            per_page = int(per_page) if per_page else 20
        except (ValueError, TypeError):
            raise ValidationError("Некорректные параметры пагинации")
        
        if page < 1:
            page = 1
        
        if per_page < 1:
            per_page = 20
        elif per_page > max_per_page:
            per_page = max_per_page
        
        return page, per_page