"""
Configuration settings for the Telegram Channel Saver.
Contains constants and settings used throughout the application.
"""
import os
import logging

# Configure logging
logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Batch size and timing settings
MESSAGES_BATCH_SIZE = 100  # Number of messages to process in one batch
BATCH_DELAY = 2  # Delay between batches in seconds
SAVE_INTERVAL = 300  # Save database every 5 minutes
MAX_RETRIES = 3  # Maximum retries for failed message fetches

# Media download settings
MEDIA_DOWNLOAD_DELAY = 3  # Delay between media downloads in seconds to avoid rate limits
MEDIA_DOWNLOAD_TIMEOUT = 120  # Timeout for media downloads in seconds (2 minutes)
MEDIA_DOWNLOAD_RETRY = 3  # Maximum number of retries for failed media downloads
MEDIA_RETRY_DELAY_BASE = 5  # Base delay for retry backoff in seconds
CHUNK_SIZE = 1024 * 1024  # 1MB chunk size for large downloads

# Directory settings
TEMP_DIR = 'temp/channel_saver'
VIDEO_TEMP_DIR = 'temp/videos'  # Directory for storing downloaded videos
EXPORT_DIR = 'temp/exports'  # Directory for storing exported message files

# OpenRouter API settings
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-4o-mini"  # Default model for image analysis
OPENROUTER_TIMEOUT = 30  # Timeout for API requests in seconds

# Create temp directories if they don't exist
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(VIDEO_TEMP_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True) 