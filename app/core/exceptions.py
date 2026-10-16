"""
Custom exceptions for the application
"""

from typing import Optional, Dict, Any


class WhisperUIException(Exception):
    """Base exception for the application"""
    
    def __init__(self, message: str, error_code: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(WhisperUIException):
    """Data validation error"""
    pass


class FileProcessingError(WhisperUIException):
    """File processing error"""
    pass


class TranscriptionError(WhisperUIException):
    """Transcription error"""
    pass


class YouTubeDownloadError(WhisperUIException):
    """YouTube download error"""
    pass


class ModelLoadError(WhisperUIException):
    """Whisper model loading error"""
    pass


class DatabaseError(WhisperUIException):
    """Database error"""
    pass


class ConfigurationError(WhisperUIException):
    """Configuration error"""
    pass


class SystemResourceError(WhisperUIException):
    """System resources error"""
    pass