"""
Настройка логирования для приложения
"""

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional
from config import get_config
from app.core import settings as _settings


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
    # config-registry-fix-r3-01: читання переведено на settings.env_bool —
    # truthy-набір розширився з ('1','true','yes') до ('1','true','yes','on').
    _file_logging = _settings.env_bool("RECALL_LOG_TO_FILE")
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


class JsonFormatter(logging.Formatter):
    """config-registry-profiles S2: `RECALL_LOG_FORMAT=json` — один рядок JSON
    на запис замість тексту (`app.py:219-275` підключає це до file+stream
    хендлерів, коли увімкнено). Поля навмисно ті самі, що в text-форматі
    (`_RequestIdLogFilter` в app.py проставляє `record.request_id`) — жодних
    зайвих полів не додаємо (план, Contracts §Логи)."""

    def format(self, record: logging.LogRecord) -> str:
        # config-registry-fix S4 (знахідка 16): мілісекунди — text-режим у
        # app.py несе їх у `%(asctime)s` за замовчуванням (Formatter без
        # datefmt додає ",%03d"), тут `formatTime` з явним datefmt їх
        # губив. Дописуємо тим самим форматом, що й стандартний logging.
        payload = {
            "ts": f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            # exc_info — поле поза початковим контрактом Contracts (§Логи мали
            # ті самі поля, що text-формат); лишаємо, бо без нього
            # __format__ ковтав би трейсбеки на ERROR-записах. Зафіксувати
            # як свідоме відхилення — в Contracts S3 (історія 06).
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """
    Получает настроенный логгер
    """
    return logging.getLogger(name)


# config-registry-fix S4 (знахідка 5): раніше тут стояло
# `app_logger = setup_logger("whisper_ui")` — виконувалось при БУДЬ-якому
# імпорті цього модуля (навіть заради `JsonFormatter`), додавало
# StreamHandler і, при `RECALL_LOG_TO_FILE=1`, ДРУГИЙ `RotatingFileHandler`
# на той самий `whisper_ui.log`, за який уже конкурує `app.py`-хендлер —
# та сама WinError 32 гонитва за файлом, заради усунення якої існує прапорець.
# Нічого в кодовій базі не читає `app_logger` (перевірено грепом), тож
# прибираємо побічний ефект імпорту, а не переписуємо `setup_logger`.


class LoggerMixin:
    """Миксин для добавления логгера в классы"""
    
    @property
    def logger(self) -> logging.Logger:
        return get_logger(self.__class__.__name__)