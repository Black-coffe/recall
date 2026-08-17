"""Pytest fixtures загального користування."""
import os
import sys

# Додаємо корень проекту в sys.path щоб імпорти працювали
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
