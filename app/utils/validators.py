"""
Validators for input data
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
    """File validator"""
    
    @staticmethod
    def validate_audio_file(file: FileStorage) -> bool:
        """Checks the validity of an audio file"""
        config = get_config()
        
        if not file or not file.filename:
            raise ValidationError("No file selected")
        
        # Check the extension
        ext = Path(file.filename).suffix.lower().lstrip('.')
        if ext not in config.ALLOWED_EXTENSIONS:
            allowed = ", ".join(config.ALLOWED_EXTENSIONS)
            raise ValidationError(f"Unsupported format. Allowed: {allowed}")
        
        # Check the size (if available)
        if hasattr(file, 'content_length') and file.content_length:
            max_size = config.MAX_CONTENT_LENGTH
            if file.content_length > max_size:
                max_mb = max_size // (1024 * 1024)
                raise ValidationError(f"File is too large. Maximum: {max_mb} MB")
        
        return True
    
    @staticmethod
    def validate_file_path(filepath: str, base_dir: str) -> str:
        """Checks and normalizes the file path"""
        if not filepath:
            raise ValidationError("File path not specified")
        
        # Normalize the path
        normalized_path = os.path.abspath(filepath)
        base_dir_abs = os.path.abspath(base_dir)
        
        # Check that the file is in an allowed directory
        if not normalized_path.startswith(base_dir_abs):
            raise ValidationError("Invalid file path")
        
        # Check that the file exists
        if not os.path.exists(normalized_path):
            raise ValidationError("File not found")
        
        return normalized_path


class YouTubeValidator:
    """YouTube URL validator"""
    
    YOUTUBE_URL_PATTERNS = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/([a-zA-Z0-9_-]+)'
    ]
    
    @classmethod
    def validate_youtube_url(cls, url: str) -> str:
        """Checks and normalizes the YouTube URL"""
        if not url:
            raise ValidationError("URL not specified")
        
        url = url.strip()
        
        # Check that it matches the YouTube patterns
        for pattern in cls.YOUTUBE_URL_PATTERNS:
            if re.match(pattern, url):
                return url
        
        raise ValidationError("Invalid YouTube URL")
    
    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        """Extracts the video ID from a YouTube URL"""
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
    """Model parameter validator"""
    
    SUPPORTED_MODELS = ["tiny", "base", "small", "medium", "large"]
    SUPPORTED_LANGUAGES = [
        "uk", "en", "ru", "es", "fr", "de", "it", "pt", "pl", "tr", 
        "ja", "ko", "zh", "auto"
    ]
    
    @classmethod
    def validate_model_name(cls, model_name: str) -> str:
        """Checks the model name"""
        if not model_name:
            raise ValidationError("Model not specified")
        
        if model_name not in cls.SUPPORTED_MODELS:
            supported = ", ".join(cls.SUPPORTED_MODELS)
            raise ValidationError(f"Unsupported model. Supported: {supported}")
        
        return model_name
    
    @classmethod
    def validate_language(cls, language: str) -> str:
        """Checks the language code"""
        if not language:
            return "auto"  # Auto-detect by default
        
        if language not in cls.SUPPORTED_LANGUAGES:
            supported = ", ".join(cls.SUPPORTED_LANGUAGES)
            raise ValidationError(f"Unsupported language. Supported: {supported}")
        
        return language


class PaginationValidator:
    """Pagination parameter validator"""
    
    @staticmethod
    def validate_pagination(page: Union[str, int], per_page: Union[str, int], max_per_page: int = 100) -> tuple:
        """Checks and normalizes pagination parameters"""
        try:
            page = int(page) if page else 1
            per_page = int(per_page) if per_page else 20
        except (ValueError, TypeError):
            raise ValidationError("Invalid pagination parameters")
        
        if page < 1:
            page = 1
        
        if per_page < 1:
            per_page = 20
        elif per_page > max_per_page:
            per_page = max_per_page
        
        return page, per_page