"""
Service for managing files and processing them
"""

import os
import shutil
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from app.core.exceptions import FileProcessingError
from app.core.logger import LoggerMixin
from app.utils.helpers import format_file_size, safe_remove_file
from config import get_config


@dataclass
class FileInfo:
    """File information"""
    path: str
    name: str
    size: int
    size_formatted: str
    created: datetime
    modified: datetime
    extension: str
    mime_type: str
    checksum: Optional[str] = None


class FileManager(LoggerMixin):
    """File manager with extended functionality"""
    
    def __init__(self):
        self.config = get_config()
        
        # Supported MIME types
        self.mime_types = {
            '.mp3': 'audio/mpeg',
            '.mp4': 'video/mp4',
            '.m4a': 'audio/mp4',
            '.wav': 'audio/wav',
            '.ogg': 'audio/ogg',
            '.flac': 'audio/flac',
            '.webm': 'video/webm',
            '.mpeg': 'video/mpeg',
            '.mpga': 'audio/mpeg'
        }
    
    def get_file_info(self, file_path: str, calculate_checksum: bool = False) -> FileInfo:
        """Gets extended information about a file"""
        try:
            if not os.path.exists(file_path):
                raise FileProcessingError(f"File not found: {file_path}")
            
            path_obj = Path(file_path)
            stat = path_obj.stat()
            
            extension = path_obj.suffix.lower()
            mime_type = self.mime_types.get(extension, 'application/octet-stream')
            
            checksum = None
            if calculate_checksum:
                checksum = self._calculate_checksum(file_path)
            
            return FileInfo(
                path=str(path_obj.absolute()),
                name=path_obj.name,
                size=stat.st_size,
                size_formatted=format_file_size(stat.st_size),
                created=datetime.fromtimestamp(stat.st_ctime),
                modified=datetime.fromtimestamp(stat.st_mtime),
                extension=extension,
                mime_type=mime_type,
                checksum=checksum
            )
            
        except Exception as e:
            raise FileProcessingError(f"Failed to get file information: {e}")
    
    def list_files(
        self, 
        directory: str, 
        pattern: str = "*", 
        recursive: bool = False,
        include_hidden: bool = False
    ) -> List[FileInfo]:
        """Gets the list of files in a directory"""
        try:
            if not os.path.exists(directory):
                return []
            
            path_obj = Path(directory)
            files = []
            
            if recursive:
                glob_pattern = f"**/{pattern}"
                file_paths = path_obj.rglob(pattern)
            else:
                file_paths = path_obj.glob(pattern)
            
            for file_path in file_paths:
                if file_path.is_file():
                    # Skip hidden files if needed
                    if not include_hidden and file_path.name.startswith('.'):
                        continue
                    
                    try:
                        file_info = self.get_file_info(str(file_path))
                        files.append(file_info)
                    except Exception as e:
                        self.logger.warning(f"Could not get information about file {file_path}: {e}")
            
            # Sort by modification date (newest first)
            files.sort(key=lambda f: f.modified, reverse=True)
            
            return files
            
        except Exception as e:
            raise FileProcessingError(f"Failed to get the file list: {e}")
    
    def cleanup_directory(
        self, 
        directory: str, 
        max_age_days: int = 7,
        max_files: int = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """Cleans a directory of old files"""
        try:
            if not os.path.exists(directory):
                return {'deleted': 0, 'freed_space': 0, 'errors': []}
            
            files = self.list_files(directory, include_hidden=True)
            cutoff_date = datetime.now() - timedelta(days=max_age_days)
            
            to_delete = []
            
            # Files older than the given age
            for file_info in files:
                if file_info.modified < cutoff_date:
                    to_delete.append(file_info)
            
            # If a maximum number of files is given
            if max_files and len(files) > max_files:
                # Sort by date (oldest first) and take the excess
                files_sorted = sorted(files, key=lambda f: f.modified)
                excess_files = files_sorted[max_files:]
                to_delete.extend(excess_files)
            
            # Remove duplicates
            to_delete = list({f.path: f for f in to_delete}.values())
            
            deleted_count = 0
            freed_space = 0
            errors = []
            
            for file_info in to_delete:
                if dry_run:
                    self.logger.info(f"[DRY RUN] Would be deleted: {file_info.path}")
                    deleted_count += 1
                    freed_space += file_info.size
                else:
                    try:
                        os.remove(file_info.path)
                        deleted_count += 1
                        freed_space += file_info.size
                        self.logger.debug(f"File deleted: {file_info.path}")
                    except Exception as e:
                        error_msg = f"Failed to delete {file_info.path}: {e}"
                        errors.append(error_msg)
                        self.logger.error(error_msg)
            
            result = {
                'deleted': deleted_count,
                'freed_space': freed_space,
                'freed_space_formatted': format_file_size(freed_space),
                'errors': errors,
                'dry_run': dry_run
            }
            
            if deleted_count > 0:
                action = "Would delete" if dry_run else "Deleted"
                self.logger.info(f"{action} {deleted_count} files, freed {format_file_size(freed_space)}")
            
            return result
            
        except Exception as e:
            raise FileProcessingError(f"Failed to clean the directory: {e}")
    
    def get_directory_stats(self, directory: str) -> Dict[str, Any]:
        """Gets statistics for a directory"""
        try:
            if not os.path.exists(directory):
                return {
                    'exists': False,
                    'total_files': 0,
                    'total_size': 0,
                    'total_size_formatted': '0 B'
                }
            
            files = self.list_files(directory, recursive=True, include_hidden=True)
            
            total_size = sum(f.size for f in files)
            
            # Group by extension
            by_extension = {}
            for file_info in files:
                ext = file_info.extension or 'no_extension'
                if ext not in by_extension:
                    by_extension[ext] = {'count': 0, 'size': 0}
                by_extension[ext]['count'] += 1
                by_extension[ext]['size'] += file_info.size
            
            # Format the sizes
            for ext_info in by_extension.values():
                ext_info['size_formatted'] = format_file_size(ext_info['size'])
            
            # Find the oldest and newest files
            oldest_file = min(files, key=lambda f: f.modified) if files else None
            newest_file = max(files, key=lambda f: f.modified) if files else None
            
            return {
                'exists': True,
                'path': directory,
                'total_files': len(files),
                'total_size': total_size,
                'total_size_formatted': format_file_size(total_size),
                'by_extension': by_extension,
                'oldest_file': {
                    'name': oldest_file.name,
                    'modified': oldest_file.modified.isoformat()
                } if oldest_file else None,
                'newest_file': {
                    'name': newest_file.name,
                    'modified': newest_file.modified.isoformat()
                } if newest_file else None
            }
            
        except Exception as e:
            raise FileProcessingError(f"Failed to get directory statistics: {e}")
    
    def duplicate_detector(self, directory: str) -> Dict[str, List[str]]:
        """Finds duplicate files by checksum"""
        try:
            files = self.list_files(directory, recursive=True)
            checksums = {}
            
            self.logger.info(f"Computing checksums for {len(files)} files...")
            
            for file_info in files:
                try:
                    checksum = self._calculate_checksum(file_info.path)
                    if checksum not in checksums:
                        checksums[checksum] = []
                    checksums[checksum].append(file_info.path)
                except Exception as e:
                    self.logger.warning(f"Could not compute the checksum for {file_info.path}: {e}")
            
            # Keep only duplicates
            duplicates = {k: v for k, v in checksums.items() if len(v) > 1}
            
            if duplicates:
                self.logger.info(f"Found {len(duplicates)} groups of duplicate files")
            else:
                self.logger.info("No duplicate files found")
            
            return duplicates
            
        except Exception as e:
            raise FileProcessingError(f"Failed to find duplicates: {e}")
    
    def safe_move_file(self, source: str, destination: str, overwrite: bool = False) -> str:
        """Safely moves a file"""
        try:
            if not os.path.exists(source):
                raise FileProcessingError(f"Source file not found: {source}")
            
            dest_path = Path(destination)
            
            # Create the directory if it does not exist
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Check whether the destination file exists
            if dest_path.exists() and not overwrite:
                # Generate a unique name
                counter = 1
                base_name = dest_path.stem
                suffix = dest_path.suffix
                parent = dest_path.parent
                
                while dest_path.exists():
                    new_name = f"{base_name}_{counter}{suffix}"
                    dest_path = parent / new_name
                    counter += 1
            
            # Move the file
            shutil.move(source, str(dest_path))
            
            self.logger.info(f"File moved: {source} -> {dest_path}")
            return str(dest_path)
            
        except Exception as e:
            raise FileProcessingError(f"Failed to move the file: {e}")
    
    def create_backup(self, file_path: str, backup_dir: str = None) -> str:
        """Creates a backup copy of a file"""
        try:
            if not os.path.exists(file_path):
                raise FileProcessingError(f"File to back up not found: {file_path}")
            
            source_path = Path(file_path)
            
            if backup_dir is None:
                backup_dir = source_path.parent / "backups"
            else:
                backup_dir = Path(backup_dir)
            
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate the backup name with a timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{source_path.stem}_{timestamp}{source_path.suffix}"
            backup_path = backup_dir / backup_name
            
            # Copy the file
            shutil.copy2(file_path, str(backup_path))
            
            self.logger.info(f"Backup created: {backup_path}")
            return str(backup_path)
            
        except Exception as e:
            raise FileProcessingError(f"Failed to create the backup: {e}")
    
    def _calculate_checksum(self, file_path: str, algorithm: str = 'md5') -> str:
        """Computes the checksum of a file"""
        hash_obj = hashlib.new(algorithm)
        
        with open(file_path, 'rb') as f:
            # Read the file in parts to save memory
            for chunk in iter(lambda: f.read(8192), b""):
                hash_obj.update(chunk)
        
        return hash_obj.hexdigest()
    
    def validate_audio_file(self, file_path: str) -> Dict[str, Any]:
        """Validates an audio file"""
        try:
            file_info = self.get_file_info(file_path)
            
            validation_result = {
                'valid': True,
                'errors': [],
                'warnings': [],
                'file_info': file_info
            }
            
            # Check the extension
            if file_info.extension not in self.config.ALLOWED_EXTENSIONS:
                validation_result['valid'] = False
                validation_result['errors'].append(f"Unsupported format: {file_info.extension}")
            
            # Check the size
            if file_info.size > self.config.MAX_CONTENT_LENGTH:
                validation_result['valid'] = False
                max_size_mb = self.config.MAX_CONTENT_LENGTH / (1024 * 1024)
                validation_result['errors'].append(f"File is too large. Maximum: {max_size_mb} MB")
            
            # Check the minimum size (100 KB)
            if file_info.size < 100 * 1024:
                validation_result['warnings'].append("File is very small, possibly corrupted")
            
            # Additional checks could be added:
            # - Check the file headers
            # - Check the audio duration
            # - Check for corruption
            
            return validation_result
            
        except Exception as e:
            return {
                'valid': False,
                'errors': [f"Validation error: {e}"],
                'warnings': [],
                'file_info': None
            }