"""
Configuration module for GeminiBot
Loads and validates environment variables
"""
import os
from typing import List
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Application configuration"""
    
    # Telegram Configuration
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    
    # Gemini AI Configuration
    GEMINI_API_TOKEN: str = os.getenv("GEMINI_API_TOKEN", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    
    # Authorization
    AUTHORIZED_USERS: List[int] = [
        int(user_id.strip()) 
        for user_id in os.getenv("AUTHORIZED_USER", "").split(',') 
        if user_id.strip()
    ]
    
    # Localization
    LANGUAGE: str = os.getenv("LANGUAGE", "en")
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    
    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/conversations_data.db")
    
    # Persistence
    PERSISTENCE_PATH: str = os.getenv("PERSISTENCE_PATH", "data/conversation_persistence")
    
    # Safety Settings
    SAFETY_SETTINGS_PATH: str = os.getenv("SAFETY_SETTINGS_PATH", "./safety_settings.json")
    
    @classmethod
    def validate(cls) -> List[str]:
        """
        Validate required configuration
        Returns list of missing/invalid configuration items
        """
        errors = []
        
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        
        if not cls.GEMINI_API_TOKEN:
            errors.append("GEMINI_API_TOKEN is required")
        
        if not cls.AUTHORIZED_USERS:
            errors.append("AUTHORIZED_USER is required (comma-separated user IDs)")
        
        if cls.LOG_LEVEL not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            errors.append(f"Invalid LOG_LEVEL: {cls.LOG_LEVEL}")
        
        return errors
    
    @classmethod
    def is_valid(cls) -> bool:
        """Check if configuration is valid"""
        return len(cls.validate()) == 0


# Create a singleton instance
config = Config()
