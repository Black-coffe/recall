"""
Пользовательские исключения для приложения
"""

from typing import Optional, Dict, Any


class WhisperUIException(Exception):
    """Базовое исключение для приложения"""
    
    def __init__(self, message: str, error_code: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(WhisperUIException):
    """Ошибка валидации данных"""
    pass


class FileProcessingError(WhisperUIException):
    """Ошибка обработки файла"""
    pass


class TranscriptionError(WhisperUIException):
    """Ошибка транскрибации"""
    pass


class YouTubeDownloadError(WhisperUIException):
    """Ошибка загрузки с YouTube"""
    pass


class ModelLoadError(WhisperUIException):
    """Ошибка загрузки модели Whisper"""
    pass


class DatabaseError(WhisperUIException):
    """Ошибка базы данных"""
    pass


class ConfigurationError(WhisperUIException):
    """Ошибка конфигурации"""
    pass


class SystemResourceError(WhisperUIException):
    """Ошибка системных ресурсов"""
    pass