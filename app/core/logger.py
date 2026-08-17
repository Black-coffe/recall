"""
Настройка логирования для приложения
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional
from config import get_config


def setup_logger(
    name: str = "whisper_ui",
    log_file: Optional[str] = None,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Настраивает логгер с ротацией файлов и консольным выводом
    """
    
    config = get_config()
    
    # Создаем логгер
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Очищаем существующие обработчики
    logger.handlers.clear()
    
    # Формат логов
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Консольный обработчик
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # Файловый обработчик с ротацией. ВЫКЛЮЧЕН по умолчанию (RECALL_LOG_TO_FILE != 1):
    # этот модульный logger («whisper_ui») нигде не используется, а его второй
    # RotatingFileHandler на общем whisper_ui.log лишь блокировал rollover главного
    # процесса на Windows (WinError 32 — файл занят: app.py пишет своим basicConfig,
    # listener — своим, каждый stdio-mcp_server открывал ещё по копии). Включить
    # обратно для конкретного процесса можно через RECALL_LOG_TO_FILE=1.
    _file_logging = os.environ.get("RECALL_LOG_TO_FILE", "0").strip().lower() in ("1", "true", "yes")
    if _file_logging and (log_file or hasattr(config, 'LOG_FILE')):
        log_path = log_file or config.LOG_FILE
        
        # Создаем папку для логов если не существует
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    # Предотвращаем дублирование логов
    logger.propagate = False
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Получает настроенный логгер
    """
    return logging.getLogger(name)


# Создаем основной логгер приложения
app_logger = setup_logger("whisper_ui")


class LoggerMixin:
    """Миксин для добавления логгера в классы"""
    
    @property
    def logger(self) -> logging.Logger:
        return get_logger(self.__class__.__name__)