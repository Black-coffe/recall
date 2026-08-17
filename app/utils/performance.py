"""
Утилиты для мониторинга производительности
"""

import time
import psutil
import threading
from datetime import datetime
from typing import Dict, Any, Optional
from functools import wraps
from contextlib import contextmanager

from app.core.logger import get_logger

logger = get_logger(__name__)


class PerformanceMonitor:
    """Монитор производительности системы"""
    
    def __init__(self):
        self.metrics = {}
        self.start_time = datetime.now()
        self._monitoring = False
        self._monitor_thread = None
    
    def start_monitoring(self, interval: int = 5):
        """Запускает мониторинг системы"""
        if self._monitoring:
            return
        
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, args=(interval,))
        self._monitor_thread.daemon = True
        self._monitor_thread.start()
        logger.info("Мониторинг производительности запущен")
    
    def stop_monitoring(self):
        """Останавливает мониторинг"""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=1)
        logger.info("Мониторинг производительности остановлен")
    
    def _monitor_loop(self, interval: int):
        """Основной цикл мониторинга"""
        while self._monitoring:
            try:
                self.metrics = self.get_current_metrics()
                time.sleep(interval)
            except Exception as e:
                logger.error(f"Ошибка мониторинга: {e}")
                time.sleep(interval)
    
    def get_current_metrics(self) -> Dict[str, Any]:
        """Получает текущие метрики системы"""
        try:
            # CPU метрики
            cpu_percent = psutil.cpu_percent(interval=1)
            cpu_count = psutil.cpu_count()
            
            # Память метрики
            memory = psutil.virtual_memory()
            
            # Дисковые метрики
            disk = psutil.disk_usage('/')
            
            # GPU метрики (если доступен)
            gpu_info = self._get_gpu_metrics()
            
            return {
                'timestamp': datetime.now().isoformat(),
                'uptime_seconds': (datetime.now() - self.start_time).total_seconds(),
                'cpu': {
                    'percent': cpu_percent,
                    'count': cpu_count,
                    'freq': psutil.cpu_freq()._asdict() if psutil.cpu_freq() else None
                },
                'memory': {
                    'total': memory.total,
                    'available': memory.available,
                    'used': memory.used,
                    'percent': memory.percent,
                    'total_gb': round(memory.total / (1024**3), 2),
                    'available_gb': round(memory.available / (1024**3), 2),
                    'used_gb': round(memory.used / (1024**3), 2)
                },
                'disk': {
                    'total': disk.total,
                    'free': disk.free,
                    'used': disk.used,
                    'percent': (disk.used / disk.total) * 100,
                    'total_gb': round(disk.total / (1024**3), 2),
                    'free_gb': round(disk.free / (1024**3), 2),
                    'used_gb': round(disk.used / (1024**3), 2)
                },
                'gpu': gpu_info,
                'process': self._get_process_metrics()
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения метрик: {e}")
            return {}
    
    def _get_gpu_metrics(self) -> Optional[Dict[str, Any]]:
        """Получает метрики GPU"""
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            
            gpu_count = torch.cuda.device_count()
            gpus = []
            
            for i in range(gpu_count):
                props = torch.cuda.get_device_properties(i)
                memory_allocated = torch.cuda.memory_allocated(i)
                memory_cached = torch.cuda.memory_reserved(i)
                
                gpus.append({
                    'id': i,
                    'name': props.name,
                    'memory_total': props.total_memory,
                    'memory_allocated': memory_allocated,
                    'memory_cached': memory_cached,
                    'memory_free': props.total_memory - memory_cached,
                    'utilization_percent': (memory_cached / props.total_memory) * 100,
                    'compute_capability': f"{props.major}.{props.minor}"
                })
            
            return {
                'available': True,
                'count': gpu_count,
                'devices': gpus
            }
            
        except Exception as e:
            logger.debug(f"GPU метрики недоступны: {e}")
            return {'available': False, 'error': str(e)}
    
    def _get_process_metrics(self) -> Dict[str, Any]:
        """Получает метрики текущего процесса"""
        try:
            process = psutil.Process()
            
            return {
                'pid': process.pid,
                'cpu_percent': process.cpu_percent(),
                'memory_info': process.memory_info()._asdict(),
                'memory_percent': process.memory_percent(),
                'num_threads': process.num_threads(),
                'create_time': process.create_time()
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения метрик процесса: {e}")
            return {}
    
    def get_summary(self) -> Dict[str, Any]:
        """Возвращает сводку по производительности"""
        current_metrics = self.get_current_metrics()
        
        if not current_metrics:
            return {}
        
        return {
            'status': 'healthy' if current_metrics['cpu']['percent'] < 80 and current_metrics['memory']['percent'] < 80 else 'warning',
            'uptime': current_metrics['uptime_seconds'],
            'cpu_usage': current_metrics['cpu']['percent'],
            'memory_usage': current_metrics['memory']['percent'],
            'disk_usage': current_metrics['disk']['percent'],
            'gpu_available': current_metrics['gpu']['available'] if current_metrics['gpu'] else False,
            'timestamp': current_metrics['timestamp']
        }


def timing_decorator(func):
    """Декоратор для измерения времени выполнения функции"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time
            logger.debug(f"{func.__name__} выполнена за {execution_time:.3f} сек")
            return result
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"{func.__name__} завершилась с ошибкой за {execution_time:.3f} сек: {e}")
            raise
    return wrapper


@contextmanager
def performance_context(operation_name: str):
    """Контекстный менеджер для измерения производительности"""
    start_time = time.time()
    start_memory = psutil.Process().memory_info().rss
    
    logger.info(f"Начало операции: {operation_name}")
    
    try:
        yield
    finally:
        end_time = time.time()
        end_memory = psutil.Process().memory_info().rss
        
        execution_time = end_time - start_time
        memory_delta = end_memory - start_memory
        
        logger.info(f"Операция '{operation_name}' завершена:")
        logger.info(f"  Время: {execution_time:.3f} сек")
        logger.info(f"  Память: {memory_delta / 1024 / 1024:.2f} MB")


class ResourceTracker:
    """Отслеживает использование ресурсов"""
    
    def __init__(self):
        self.operations = {}
    
    def start_operation(self, operation_id: str):
        """Начинает отслеживание операции"""
        self.operations[operation_id] = {
            'start_time': time.time(),
            'start_memory': psutil.Process().memory_info().rss,
            'start_cpu': psutil.cpu_percent()
        }
    
    def end_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        """Завершает отслеживание операции"""
        if operation_id not in self.operations:
            return None
        
        start_data = self.operations.pop(operation_id)
        end_time = time.time()
        end_memory = psutil.Process().memory_info().rss
        
        return {
            'operation_id': operation_id,
            'duration': end_time - start_data['start_time'],
            'memory_used': end_memory - start_data['start_memory'],
            'peak_cpu': psutil.cpu_percent(),
            'start_time': start_data['start_time'],
            'end_time': end_time
        }
    
    def get_active_operations(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает активные операции"""
        current_time = time.time()
        active = {}
        
        for op_id, data in self.operations.items():
            active[op_id] = {
                'duration': current_time - data['start_time'],
                'memory_delta': psutil.Process().memory_info().rss - data['start_memory']
            }
        
        return active


# Глобальные экземпляры
performance_monitor = PerformanceMonitor()
resource_tracker = ResourceTracker()