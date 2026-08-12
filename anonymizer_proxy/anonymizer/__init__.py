"""
Модуль анонимизации
"""
from .ner_service import NERService
from .file_parser import FileParser
from .mapping_store import MappingStore
from .replacer import TextReplacer

__all__ = [
    "NERService",
    "FileParser",
    "MappingStore",
    "TextReplacer",
]