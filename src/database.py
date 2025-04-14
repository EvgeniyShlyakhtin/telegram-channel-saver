"""
Database operations for the Telegram Channel Saver.
Handles loading, saving, and managing the JSON database.
"""
import os
import json
import logging
from datetime import datetime

from src.config import TEMP_DIR

logger = logging.getLogger(__name__)

def load_database(db_path):
    """
    Load database from JSON file or create new if doesn't exist
    
    Args:
        db_path: Path to the database file
        
    Returns:
        dict: Loaded database or new database structure
    """
    if os.path.exists(db_path):
        try:
            with open(db_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("Corrupted database file, creating new")
            return create_new_database(db_path)
    return create_new_database(db_path)

def create_new_database(db_path):
    """
    Create new database structure
    
    Args:
        db_path: Path to save the new database
        
    Returns:
        dict: New empty database structure
    """
    db = {
        'users': {},
        'last_login': None,
        'sessions': {},
        'active_channel': None,
        'messages': {},
        'videos': {}
    }
    save_database(db_path, db)
    return db

def save_database(db_path, db):
    """
    Save database to JSON file
    
    Args:
        db_path: Path to save the database
        db: Database dictionary to save
    """
    with open(db_path, 'w') as f:
        json.dump(db, f, indent=4, default=str)

def get_db_path():
    """
    Get the path to the database file
    
    Returns:
        str: Path to the database file
    """
    return os.path.join(TEMP_DIR, 'database.json') 