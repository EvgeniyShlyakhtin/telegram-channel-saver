import os
import json
import logging
import math
import random
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, ServerError, TimedOutError
from telethon.tl.types import InputMessagesFilterRoundVideo, InputMessagesFilterVideo, DocumentAttributeFilename
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

MESSAGES_BATCH_SIZE = 100  # Number of messages to process in one batch
BATCH_DELAY = 2  # Delay between batches in seconds
SAVE_INTERVAL = 300  # Save database every 5 minutes
MAX_RETRIES = 3  # Maximum retries for failed message fetches
MEDIA_DOWNLOAD_DELAY = 3  # Delay between media downloads in seconds to avoid rate limits
MEDIA_DOWNLOAD_TIMEOUT = 120  # Timeout for media downloads in seconds (2 minutes)
MEDIA_DOWNLOAD_RETRY = 3  # Maximum number of retries for failed media downloads
MEDIA_RETRY_DELAY_BASE = 5  # Base delay for retry backoff in seconds
CHUNK_SIZE = 1024 * 1024  # 1MB chunk size for large downloads
VIDEO_TEMP_DIR = 'temp/videos'  # Directory for storing downloaded videos

class ChannelSaver:
    def __init__(self):
        # Create temp directories if they don't exist
        self.temp_dir = 'temp/channel_saver'
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(VIDEO_TEMP_DIR, exist_ok=True)
        
        # Database file path
        self.db_path = os.path.join(self.temp_dir, 'database.json')
        
        # Load or create database
        self.db = self.load_database()
        
        # Telegram client
        try:
            self.api_id = int(os.getenv('API_ID'))
            if not self.api_id:
                raise ValueError("API_ID not found in environment variables")
                
            self.api_hash = os.getenv('API_HASH')
            if not self.api_hash:
                raise ValueError("API_HASH not found in environment variables")
                
        except (TypeError, ValueError) as e:
            logger.error(f"Error loading API credentials: {e}")
            print("\nError: Please make sure API_ID and API_HASH are properly set in .env file")
            raise
            
        self.client = None
        self.phone = None

    def load_database(self):
        """Load database from JSON file or create new if doesn't exist"""
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("Corrupted database file, creating new")
                return self.create_new_database()
        return self.create_new_database()

    def create_new_database(self):
        """Create new database structure"""
        db = {
            'users': {},
            'last_login': None,
            'sessions': {},
            'active_channel': None  # Add active_channel field
        }
        self.save_database(db)
        return db

    def save_database(self, db=None):
        """Save database to JSON file"""
        if db is None:
            db = self.db
        with open(self.db_path, 'w') as f:
            json.dump(db, f, indent=4, default=str)

    async def check_authorized(self):
        """Check if user is already authorized"""
        if not self.client:
            return False
        try:
            return await self.client.is_user_authorized()
        except Exception as e:
            logger.error(f"Error checking authorization: {e}")
            return False

    async def login(self, force=False):
        """Handle login process"""
        if not force:
            # Try to restore existing session
            if await self.restore_session():
                return True
        
        # New login required
        self.phone = input('Please enter your phone number (international format): ')
        
        # Create new client
        self.client = TelegramClient(
            self.get_session_path(self.phone),
            self.api_id,
            self.api_hash
        )
        
        await self.client.connect()
        
        if not await self.client.is_user_authorized():
            try:
                await self.client.send_code_request(self.phone)
                code = input('Enter the code you received: ')
                await self.client.sign_in(self.phone, code)
            except SessionPasswordNeededError:
                # 2FA is enabled
                password = input('Please enter your 2FA password: ')
                await self.client.sign_in(password=password)
        
        # Save session after successful login
        me = await self.client.get_me()
        await self.save_session(me)
        
        logger.info(f"Successfully logged in as {me.first_name} (@{me.username})")
        return True

    async def list_channels(self):
        """List all channels/groups user is subscribed to"""
        try:
            dialogs = await self.client.get_dialogs()
            channels = []
            
            # Collect channel info
            for i, dialog in enumerate(dialogs, 1):
                if dialog.is_channel or dialog.is_group:
                    entity = dialog.entity
                    
                    # Store channel info
                    channel_info = {
                        'id': entity.id,
                        'title': entity.title,
                        'username': getattr(entity, 'username', None),
                        'participants_count': getattr(entity, 'participants_count', 0),
                        'type': 'Channel' if dialog.is_channel else 'Group',
                        'index': i
                    }
                    channels.append(channel_info)
            
            # Sort channels by member count (descending)
            channels.sort(key=lambda x: x['participants_count'] or 0, reverse=True)
            
            # Update indices after sorting
            for i, channel in enumerate(channels, 1):
                channel['index'] = i
            
            if not channels:
                print("\nNo channels/groups found!")
                return []
            
            # Print header
            print("\nAvailable Channels and Groups:")
            print(f"{'#':>3} | {'Members':>7} | {'Type':^7} | {'Title':<30} | {'Username':<15}")
            print("-" * 70)
            
            # Print each channel on one line
            for channel in channels:
                # Format members count
                members = f"{channel['participants_count']:,}" if channel['participants_count'] else 'N/A'
                members = members[:7]  # Limit length
                
                # Format username
                username = f"@{channel['username']}" if channel['username'] else '-'
                username = username[:15]  # Limit length
                
                # Format title (with ellipsis if too long)
                title = channel['title']
                if len(title) > 30:
                    title = title[:27] + "..."
                
                print(f"{channel['index']:3} | {members:>7} | {channel['type']:<7} | {title:<30} | {username:<15}")
            
            print("-" * 70)
            print(f"Total: {len(channels)} channels/groups")
            
            return channels
        except Exception as e:
            logger.error(f"Error listing channels: {e}")
            return []

    async def select_active_channel(self):
        """Select active channel/group"""
        channels = await self.list_channels()
        
        if not channels:
            print("\nNo channels/groups found!")
            return False
        
        while True:
            try:
                choice = input("\nEnter channel number to select (or 0 to cancel): ")
                if choice == '0':
                    return False
                
                index = int(choice)
                selected = next((c for c in channels if c['index'] == index), None)
                
                if selected:
                    # Update active channel in database
                    self.db['active_channel'] = selected
                    self.save_database()
                    print(f"\nSelected channel: {selected['title']}")
                    return True
                else:
                    print("\nInvalid channel number!")
            except ValueError:
                print("\nPlease enter a valid number!")

    def get_active_channel(self):
        """Get currently active channel from database"""
        return self.db.get('active_channel')

    async def show_active_channel(self):
        """Display information about active channel"""
        active = self.get_active_channel()
        if active:
            print("\nActive Channel/Group:")
            print("--------------------")
            print(f"Title: {active['title']}")
            print(f"Type: {active['type']}")
            if active['username']:
                print(f"Username: @{active['username']}")
            print(f"ID: {active['id']}")
            print(f"Members: {active['participants_count']}")
            
            # Get message count information
            try:
                channel_id = str(active['id'])
                
                # Check if we have messages saved in DB
                saved_count = 0
                if 'messages' in self.db and channel_id in self.db.get('messages', {}):
                    saved_count = len(self.db['messages'][channel_id])
                
                print(f"Saved Messages: {saved_count}")
                
                # Get total message count from the server
                print("Fetching total message count from server...")
                # Get first message (oldest)
                first_message = None
                last_message = None
                async for msg in self.client.iter_messages(active['id'], limit=1, reverse=True):
                    first_message = msg
                
                # Get last message (newest) 
                async for msg in self.client.iter_messages(active['id'], limit=1):
                    last_message = msg
                    
                if first_message and last_message:
                    total = last_message.id - first_message.id + 1
                    print(f"Total Messages (estimate): {total}")
                    print(f"First Message ID: {first_message.id}")
                    print(f"Last Message ID: {last_message.id}")
                    
                    # Get video counts
                    saved_videos_count = 0
                    if 'videos' in self.db and channel_id in self.db.get('videos', {}):
                        saved_videos_count = len(self.db['videos'][channel_id])
                    
                    print(f"Saved Videos: {saved_videos_count}")
                    
                    # Check for media in saved messages
                    media_count = 0
                    if 'messages' in self.db and channel_id in self.db.get('messages', {}):
                        media_count = sum(1 for msg in self.db['messages'][channel_id].values() 
                                         if msg.get('has_media'))
                    
                    print(f"Messages with Media: {media_count}")
                else:
                    print("Unable to determine total message count.")
                
            except Exception as e:
                logger.error(f"Error getting message count: {e}")
                print("Unable to determine message count.")
        else:
            print("\nNo active channel selected!")

    def get_session_path(self, phone):
        """Get full path to session file for given phone number"""
        return os.path.join(self.temp_dir, f'user_{phone}')

    async def save_session(self, me):
        """Save current session info to database"""
        if not self.phone:
            return
            
        # Update sessions info
        self.db['sessions'][self.phone] = {
            'session_file': f'user_{self.phone}',
            'created_at': self.db['sessions'].get(self.phone, {}).get('created_at', str(datetime.now())),
            'last_used': str(datetime.now()),
            'user_id': me.id,
            'username': me.username,
            'active': True
        }
        
        # Update last login
        self.db['last_login'] = {
            'phone': self.phone,
            'user_id': me.id,
            'username': me.username,
            'date': str(datetime.now())
        }
        
        # Deactivate other sessions
        for phone in self.db['sessions']:
            if phone != self.phone:
                self.db['sessions'][phone]['active'] = False
        
        self.save_database()

    async def restore_session(self):
        """Try to restore last active session"""
        if not self.db.get('sessions'):
            return False
            
        # Find active session
        active_session = None
        active_phone = None
        
        for phone, session in self.db['sessions'].items():
            if session.get('active'):
                active_session = session
                active_phone = phone
                break
        
        if not active_session:
            return False
            
        # Try to restore session
        try:
            # Ensure any existing client is disconnected
            if self.client:
                await self.client.disconnect()
                self.client = None
            
            self.phone = active_phone
            self.client = TelegramClient(
                self.get_session_path(self.phone),
                self.api_id,
                self.api_hash
            )
            
            await self.client.connect()
            if await self.check_authorized():
                # Update last used time
                me = await self.client.get_me()
                await self.save_session(me)
                logger.info(f"Restored session for {self.phone}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to restore session: {e}")
            if self.client:
                await self.client.disconnect()
                self.client = None
            
        return False

    async def list_sessions(self):
        """Display all saved sessions"""
        if not self.db['sessions']:
            print("\nNo saved sessions found!")
            return
            
        print("\nSaved Sessions:")
        print("--------------")
        for phone, session in self.db['sessions'].items():
            status = "ACTIVE" if session['active'] else "inactive"
            print(f"\nPhone: {phone} [{status}]")
            print(f"Username: @{session['username']}")
            print(f"Created: {session['created_at']}")
            print(f"Last used: {session['last_used']}")

    async def switch_session(self):
        """Switch to a different saved session"""
        if not self.db['sessions']:
            print("\nNo saved sessions found!")
            return False
            
        await self.list_sessions()
        
        while True:
            phone = input("\nEnter phone number to switch to (or 0 to cancel): ")
            if phone == '0':
                return False
                
            if phone in self.db['sessions']:
                # Disconnect current client if exists
                if self.client:
                    await self.client.disconnect()
                
                # Update active status
                for p, s in self.db['sessions'].items():
                    s['active'] = (p == phone)
                
                # Create new client with selected session
                self.phone = phone
                self.client = TelegramClient(
                    self.get_session_path(phone),
                    self.api_id,
                    self.api_hash
                )
                
                await self.client.connect()
                if await self.check_authorized():
                    # Update last used
                    self.db['sessions'][phone]['last_used'] = datetime.now()
                    self.save_database()
                    print(f"\nSwitched to session: {phone}")
                    return True
                else:
                    print("\nSession is no longer valid!")
                    return False
            else:
                print("\nInvalid phone number!")
    
    async def download_video_messages(self, limit=None, round_videos_only=False, video_dir=None):
        """
        Download video messages (video circles or regular videos) from active channel
        
        Args:
            limit: Maximum number of videos to download (None for all)
            round_videos_only: If True, download only round videos (video circles/video messages), 
                             otherwise download all videos
            video_dir: Custom directory to save videos (default: temp/videos)
        
        Returns:
            bool: True if download was successful, False otherwise
        """
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return False
        
        try:
            # Set up download directory
            if video_dir:
                download_dir = video_dir
                os.makedirs(download_dir, exist_ok=True)
            else:
                download_dir = VIDEO_TEMP_DIR
            
            print("\n" + "="*50)
            print(f"Downloading {'round' if round_videos_only else 'all'} videos from channel: {active['title']}")
            print("="*50)
            
            # Initialize video messages list in the database
            channel_id = str(active['id'])
            if 'videos' not in self.db:
                self.db['videos'] = {}
            if channel_id not in self.db['videos']:
                self.db['videos'][channel_id] = {}
            
            # Counters for tracking progress
            downloaded = 0
            skipped = 0
            errors = 0
            retry_count = 0
            
            # Progress tracking
            start_time = datetime.now()
            last_save_time = start_time
            
            # Define the video filter
            video_filter = InputMessagesFilterRoundVideo() if round_videos_only else InputMessagesFilterVideo()
            
            # Get total count of videos (approximate)
            total_count = 0
            try:
                async for _ in self.client.iter_messages(active['id'], filter=video_filter, limit=1):
                    # Just to get the first message to check if any exist
                    total_count = 1
                print(f"Scanning for {'round' if round_videos_only else 'all'} videos in the channel...")
            except Exception as e:
                print(f"Error checking for videos: {e}")
                return False
            
            # If no videos found
            if total_count == 0:
                print("\nNo videos found in this channel!")
                return False
            
            # Get videos from newest to oldest
            async for message in self.client.iter_messages(active['id'], filter=video_filter, limit=limit):
                try:
                    msg_id = str(message.id)
                    video_info = {
                        'id': message.id,
                        'date': str(message.date),
                        'from_id': message.from_id.user_id if message.from_id else None,
                        'media_type': type(message.media).__name__ if message.media else None,
                        'file_path': None,
                        'download_date': None,
                        'file_size': None,
                        'duration': getattr(message.media.document, 'duration', None) if message.media else None,
                        'mime_type': getattr(message.media.document, 'mime_type', None) if message.media else None,
                        'size': getattr(message.media.document, 'size', None) if message.media else None,
                    }
                    
                    # Check if video is already downloaded
                    if msg_id in self.db['videos'][channel_id] and self.db['videos'][channel_id][msg_id].get('file_path'):
                        existing_path = self.db['videos'][channel_id][msg_id]['file_path']
                        if os.path.exists(existing_path):
                            print(f"Video from message #{message.id} already downloaded, skipping...")
                            skipped += 1
                            continue
                    
                    # Create a filename based on message ID and date
                    filename = f"video_{message.id}_{message.date.strftime('%Y%m%d_%H%M%S')}"
                    
                    # Get video file size if available
                    file_size = None
                    if hasattr(message.media, 'document'):
                        file_size = getattr(message.media.document, 'size', None)
                        if file_size:
                            size_mb = file_size / (1024 * 1024)
                            print(f"Video size: {size_mb:.2f} MB")
                    
                    # Download the video using our enhanced method
                    print(f"Downloading video from message #{message.id}...")
                    download_result = await self.download_media_safely(
                        message=message,
                        filename=filename,
                        file_size=file_size
                    )
                    
                    if download_result['success']:
                        file_path = download_result['file_path']
                        print(f"Video saved to: {file_path}")
                        
                        # Update video info with download details
                        video_info['file_path'] = file_path
                        video_info['download_date'] = str(datetime.now())
                        video_info['file_size'] = os.path.getsize(file_path) if os.path.exists(file_path) else None
                        
                        # Save to database
                        self.db['videos'][channel_id][msg_id] = video_info
                        downloaded += 1
                        
                        # Save database periodically
                        current_time = datetime.now()
                        if (current_time - last_save_time).total_seconds() > SAVE_INTERVAL:
                            self.save_database()
                            last_save_time = current_time
                    else:
                        # Handle download failure
                        print(f"Failed to download video: {download_result['error']}")
                        logger.warning(f"Video download failed for message {message.id}: {download_result['error']}")
                        errors += 1
                    
                    # Display progress
                    elapsed = datetime.now() - start_time
                    print(f"Progress: Downloaded: {downloaded}, Skipped: {skipped}, Errors: {errors}")
                    print(f"Elapsed time: {str(elapsed).split('.')[0]}")
                    print(f"Retries due to rate limits: {retry_count}")
                    print("-"*50)
                    
                    # Add delay to avoid rate limits
                    await asyncio.sleep(MEDIA_DOWNLOAD_DELAY)
                    
                except Exception as e:
                    logger.error(f"Error downloading video from message {message.id}: {str(e)}")
                    print(f"Error downloading video from message #{message.id}: {str(e)}")
                    errors += 1
                    continue
            
            # Final save
            self.save_database()
            
            # Final statistics
            end_time = datetime.now()
            elapsed = end_time - start_time
            
            print("\n" + "="*50)
            print("Download Completed!")
            print("="*50)
            print(f"\nFinal Statistics:")
            print(f"Total videos downloaded: {downloaded}")
            print(f"Videos skipped (already downloaded): {skipped}")
            print(f"Errors: {errors}")
            print(f"Retries due to rate limits: {retry_count}")
            print(f"\nTime Elapsed: {str(elapsed).split('.')[0]}")
            print("="*50)
            
            return True
            
        except Exception as e:
            logger.error(f"Error downloading videos: {e}")
            print(f"\nError downloading videos: {str(e)}")
            return False

    async def list_downloaded_videos(self):
        """List all downloaded videos for active channel"""
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return
            
        channel_id = str(active['id'])
        if 'videos' not in self.db or channel_id not in self.db['videos'] or not self.db['videos'][channel_id]:
            print("\nNo downloaded videos for this channel!")
            return
            
        videos = self.db['videos'][channel_id]
        total = len(videos)
        
        print("\nDownloaded Videos:")
        print("-" * 80)
        print(f"{'ID':<10} | {'Date':<20} | {'Type':<15} | {'Size':<10} | {'Duration':<10} | {'Path':<30}")
        print("-" * 80)
        
        for video_id, video in sorted(videos.items(), key=lambda x: int(x[0]), reverse=True):
            if not video.get('file_path') or not os.path.exists(video['file_path']):
                continue
                
            # Format size to KB/MB
            size = "N/A"
            if video.get('file_size'):
                size_bytes = video['file_size']
                if size_bytes < 1024:
                    size = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    size = f"{size_bytes / 1024:.1f} KB"
                else:
                    size = f"{size_bytes / (1024 * 1024):.1f} MB"
                    
            # Format duration to mm:ss
            duration = "N/A"
            if video.get('duration'):
                seconds = video['duration']
                minutes = seconds // 60
                seconds = seconds % 60
                duration = f"{minutes:02d}:{seconds:02d}"
                
            # Format date
            date = video.get('date', 'Unknown')
            if date and date != 'Unknown':
                try:
                    # Parse and format date string
                    date_obj = datetime.fromisoformat(date.split('+')[0])
                    date = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    pass
                    
            # Format path
            path = video.get('file_path', 'Unknown')
            if len(path) > 30:
                path = "..." + path[-27:]
                
            # Get media type (video type)
            media_type = video.get('media_type', 'Unknown')
            if media_type == 'MessageMediaDocument':
                mime_type = video.get('mime_type', '')
                if 'video' in mime_type:
                    if 'round' in mime_type:
                        media_type = 'Video Circle'
                    else:
                        media_type = 'Video'
                        
            print(f"{video['id']:<10} | {date:<20} | {media_type:<15} | {size:<10} | {duration:<10} | {path:<30}")
            
        print("-" * 80)
        print(f"Total Videos: {total}")
        
    
    async def cleanup_sessions(self):
        """Remove invalid sessions"""
        if not self.db['sessions']:
            print("\nNo sessions to clean up!")
            return
            
        print("\nChecking sessions validity...")
        invalid = []
        
        for phone, session in self.db['sessions'].items():
            # Skip active session
            if session['active']:
                continue
                
            # Try to connect with session
            client = TelegramClient(
                self.get_session_path(phone),
                self.api_id,
                self.api_hash
            )
            
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    invalid.append(phone)
            except Exception:
                invalid.append(phone)
            finally:
                await client.disconnect()
        
        if invalid:
            print(f"\nFound {len(invalid)} invalid sessions")
            if input("Remove them? (y/N): ").lower() == 'y':
                for phone in invalid:
                    # Remove session file
                    try:
                        os.remove(self.get_session_path(phone))
                    except OSError:
                        pass
                    # Remove from database
                    del self.db['sessions'][phone]
                self.save_database()
                print("\nInvalid sessions removed!")
        else:
            print("\nAll sessions are valid!")

    async def start(self):
        """Main entry point"""
        print("\nWelcome to Channel Saver!")
        print("------------------------")
        
        # Ensure clean start
        if self.client:
            await self.client.disconnect()
            self.client = None
        
        # Try to restore session first
        if await self.restore_session():
            print(f"\nRestored session for {self.phone}")
            relogin = False
        else:
            relogin = True
        
        try:
            if await self.login(force=relogin):
                print("\nSuccessfully connected!")
                
                while True:
                    # Show active channel in menu if selected
                    active = self.get_active_channel()
                    if active:
                        print(f"\nActive: {active['title']} ({active['type']})")
                    
                    print("\nOptions:")
                    print("1. Show account info")
                    print("2. List channels/groups")
                    print("3. Select active channel")
                    print("4. Show active channel info")
                    print("5. Save channel users")
                    print("6. Show users statistics")
                    print("7. List saved sessions")
                    print("8. Switch session")
                    print("9. Cleanup invalid sessions")
                    print("10. Save channel messages")
                    print("11. List saved users")
                    print("12. Search messages")
                    print("13. Download videos")
                    print("14. List downloaded videos")
                    print("15. Logout")
                    print("16. Exit")
                    
                    choice = input("\nEnter your choice (1-16): ")
                    
                    if choice == '1':
                        me = await self.client.get_me()
                        print(f"\nAccount Information:")
                        print(f"Phone: {self.phone}")
                        print(f"Username: @{me.username}")
                        print(f"First Name: {me.first_name}")
                        print(f"Last Name: {me.last_name}")
                        print(f"User ID: {me.id}")
                    elif choice == '2':
                        await self.list_channels()
                    elif choice == '3':
                        await self.select_active_channel()
                    elif choice == '4':
                        await self.show_active_channel()
                    elif choice == '5':
                        await self.save_channel_users()
                    elif choice == '6':
                        await self.show_channel_users_stats()
                    elif choice == '7':
                        await self.list_sessions()
                    elif choice == '8':
                        await self.switch_session()
                    elif choice == '9':
                        await self.cleanup_sessions()
                    elif choice == '10':
                        print("\nMessage Download Options:")
                        print("1. Download new messages only")
                        print("2. Force redownload all messages")
                        print("3. Download most recent messages")
                        print("4. Download messages by ID range")
                        print("5. Back to main menu")
                        
                        dl_choice = input("\nEnter choice (1-5): ")
                        
                        if dl_choice == '1':
                            limit = input("\nEnter number of messages to save (or press Enter for all): ")
                            limit = int(limit) if limit.strip() else None
                            await self.save_channel_messages(limit=limit, force_redownload=False)
                        elif dl_choice == '2':
                            confirm = input("\nThis will redownload all messages. Continue? (y/N): ").lower()
                            if confirm == 'y':
                                limit = input("\nEnter number of messages to save (or press Enter for all): ")
                                limit = int(limit) if limit.strip() else None
                                await self.save_channel_messages(limit=limit, force_redownload=True)
                        elif dl_choice == '3':
                            count = input("\nEnter number of recent messages to download: ")
                            try:
                                count = int(count)
                                if count <= 0:
                                    print("\nPlease enter a positive number")
                                    continue
                                await self.save_channel_messages(recent_count=count)
                            except ValueError:
                                print("\nPlease enter a valid number")
                        elif dl_choice == '4':
                            try:
                                min_id = input("\nEnter minimum message ID (or press Enter for first message): ")
                                min_id = int(min_id) if min_id.strip() else None
                                
                                max_id = input("Enter maximum message ID (or press Enter for last message): ")
                                max_id = int(max_id) if max_id.strip() else None
                                
                                limit = input("Enter maximum number of messages to download (or press Enter for all): ")
                                limit = int(limit) if limit.strip() else None
                                
                                force = input("Force redownload existing messages? (y/N): ").lower() == 'y'
                                
                                await self.save_channel_messages(
                                    min_id=min_id, 
                                    max_id=max_id, 
                                    limit=limit,
                                    force_redownload=force
                                )
                            except ValueError:
                                print("\nPlease enter valid message IDs (numbers only)")
                        elif dl_choice == '5':
                            continue
                    elif choice == '11':
                        await self.list_saved_users()
                    elif choice == '12':
                        await self.search_messages()
                    elif choice == '13':
                        print("\nVideo Download Options:")
                        print("1. Download all videos")
                        print("2. Download video circles only (round videos)")
                        print("3. Back to main menu")
                        
                        video_choice = input("\nEnter choice (1-3): ")
                        
                        if video_choice == '1':
                            limit = input("\nEnter number of videos to download (or press Enter for all): ")
                            limit = int(limit) if limit.strip() else None
                            await self.download_video_messages(limit=limit, round_videos_only=False)
                        elif video_choice == '2':
                            limit = input("\nEnter number of video circles to download (or press Enter for all): ")
                            limit = int(limit) if limit.strip() else None
                            await self.download_video_messages(limit=limit, round_videos_only=True)
                        elif video_choice == '3':
                            continue
                    elif choice == '14':
                        await self.list_downloaded_videos()
                    elif choice == '15':
                        await self.client.log_out()
                        print("\nLogged out successfully!")
                        if self.phone in self.db['sessions']:
                            del self.db['sessions'][self.phone]
                        self.db['last_login'] = None
                        self.db['active_channel'] = None
                        self.save_database()
                        break
                    elif choice == '16':
                        break
                    else:
                        print("\nInvalid choice!")

        finally:
            if self.client:
                await self.client.disconnect()
                self.client = None

    async def save_channel_users(self):
        """Save all users from active channel to database"""
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return False
            
        try:
            print(f"\nFetching users from {active['title']}...")
            
            # Initialize channel users dict if doesn't exist
            channel_id = str(active['id'])
            if channel_id not in self.db['users']:
                self.db['users'][channel_id] = {}
            
            # Get all participants
            participants = await self.client.get_participants(active['id'])
            
            # Counter for progress
            total = len(participants)
            saved = 0
            updated = 0
            
            print(f"\nProcessing {total} users...")
            
            for user in participants:
                user_dict = {
                    'id': user.id,
                    'username': user.username,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'phone': getattr(user, 'phone', None),
                    'bot': user.bot,
                    'scam': user.scam,
                    'fake': user.fake,
                    'premium': user.premium,
                    'verified': user.verified,
                    'restricted': user.restricted,
                    'last_seen': str(datetime.now())
                }
                
                user_id = str(user.id)
                if user_id in self.db['users'][channel_id]:
                    # Update existing user
                    user_dict['first_seen'] = self.db['users'][channel_id][user_id]['first_seen']
                    self.db['users'][channel_id][user_id].update(user_dict)
                    updated += 1
                else:
                    # Add new user
                    user_dict['first_seen'] = str(datetime.now())
                    self.db['users'][channel_id][user_id] = user_dict
                    saved += 1
                
                # Show progress every 10 users
                if (saved + updated) % 10 == 0:
                    print(f"Progress: {saved + updated}/{total}")
            
            self.save_database()
            print(f"\nOperation completed!")
            print(f"New users saved: {saved}")
            print(f"Users updated: {updated}")
            return True
            
        except Exception as e:
            logger.error(f"Error saving channel users: {e}")
            print(f"\nError saving users: {str(e)}")
            return False

    async def show_channel_users_stats(self):
        """Show statistics about saved users in active channel"""
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return
            
        channel_id = str(active['id'])
        if channel_id not in self.db['users']:
            print("\nNo saved users for this channel!")
            return
            
        users = self.db['users'][channel_id]
        total = len(users)
        bots = sum(1 for u in users.values() if u['bot'])
        premium = sum(1 for u in users.values() if u['premium'])
        verified = sum(1 for u in users.values() if u['verified'])
        
        print(f"\nChannel Users Statistics:")
        print(f"------------------------")
        print(f"Total users saved: {total}")
        print(f"Bots: {bots}")
        print(f"Premium users: {premium}")
        print(f"Verified users: {verified}")
        print(f"\nLast update: {max(u['last_seen'] for u in users.values())}")

    async def save_channel_messages(self, limit: int = None, force_redownload: bool = False, 
                              min_id: int = None, max_id: int = None, recent_count: int = None,
                              download_media: bool = True):  # Set download_media default to True
        """
        Save messages from active channel with support for ID ranges and rate limiting
        
        Args:
            limit: Maximum number of messages to download (None for all)
            force_redownload: Whether to redownload existing messages
            min_id: Minimum message ID to fetch (inclusive)
            max_id: Maximum message ID to fetch (exclusive)
            recent_count: Number of most recent messages to fetch
            download_media: Whether to download media (videos, photos, etc.) from messages
        """
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return False
        
        try:
            print("\n" + "="*50)
            print(f"Channel: {active['title']}")
            print(f"Type: {active['type']}")
            print("="*50)
            
            # Initialize channel messages dict if doesn't exist
            channel_id = str(active['id'])
            if 'messages' not in self.db:
                self.db['messages'] = {}
            if channel_id not in self.db['messages']:
                self.db['messages'][channel_id] = {}
            
            # Get channel message boundaries
            print("\nAnalyzing channel messages...")
            first_message = None
            last_message = None
            
            # Get first message (oldest)
            async for msg in self.client.iter_messages(active['id'], limit=1, reverse=True):
                first_message = msg
                print(f"First message found: #{msg.id} ({msg.date})")
            
            # Get last message (newest) 
            async for msg in self.client.iter_messages(active['id'], limit=1):
                last_message = msg
                print(f"Last message found: #{msg.id} ({msg.date})")
                
            if not first_message or not last_message:
                print("\nNo messages found in channel!")
                return False
            
            # Determine message range to fetch
            fetch_min_id = min_id if min_id is not None else first_message.id
            fetch_max_id = max_id if max_id is not None else last_message.id + 1
            
            # Handle recent_count if specified
            if recent_count is not None:
                fetch_min_id = max(first_message.id, last_message.id - recent_count)
                fetch_max_id = last_message.id + 1
            
            # Ensure range is valid
            if fetch_min_id >= fetch_max_id:
                print("\nInvalid message range: min_id must be less than max_id")
                return False
            
            # Calculate total messages in range
            total_in_range = fetch_max_id - fetch_min_id
            if limit:
                total = min(total_in_range, limit)
            else:
                total = total_in_range
            
            print(f"\nChannel Information:")
            print(f"First Message in Channel: #{first_message.id} ({first_message.date})")
            print(f"Last Message in Channel: #{last_message.id} ({last_message.date})")
            print(f"Message Range to Fetch: #{fetch_min_id} to #{fetch_max_id - 1}")
            print(f"Messages in Range: {total_in_range}")
            print(f"Messages to Process: {total}")
            print(f"Media Download: {'Enabled' if download_media else 'Disabled'}")
            print(f"Batch Size: {MESSAGES_BATCH_SIZE} messages")
            print(f"Delay between batches: {BATCH_DELAY} seconds")
            print(f"Rate Limit: Maximum 100 messages per request")
            
            confirm = input("\nProceed with message download? (y/N): ").lower()
            if confirm != 'y':
                print("\nOperation cancelled!")
                return False
            
            print("\nStarting message download...")
            print("="*50)
            
            # Counters
            saved = 0
            updated = 0
            skipped = 0
            errors = 0
            retry_count = 0
            processed = 0
            media_downloaded = 0
            media_skipped = 0
            media_errors = 0
            
            # Progress tracking
            start_time = datetime.now()
            last_save_time = start_time
            
            # Process messages in batches from newest to oldest
            current_id = fetch_max_id - 1
            
            while current_id >= fetch_min_id and (limit is None or processed < limit):
                try:
                    # Check if we need to adjust batch size to respect limits
                    remaining = limit - processed if limit is not None else None
                    batch_size = min(MESSAGES_BATCH_SIZE, remaining) if remaining is not None else MESSAGES_BATCH_SIZE
                    
                    print(f"\nFetching batch for messages <= {current_id}")
                    print(f"Batch parameters:")
                    print(f"- Channel ID: {active['id']}")
                    print(f"- Limit: {batch_size}")
                    print(f"- Min ID: {fetch_min_id}")
                    print(f"- Max ID: {current_id + 1}")
                    
                    # Get batch of messages with Telegram's limit of 100 per request
                    batch_messages = []
                    async for message in self.client.iter_messages(
                        active['id'],
                        limit=batch_size,
                        max_id=current_id + 1,
                        min_id=fetch_min_id - 1  # -1 to include the min_id message
                    ):
                        batch_messages.append(message)
                        processed += 1
                        if limit is not None and processed >= limit:
                            break
                    
                    print(f"Retrieved {len(batch_messages)} messages in batch")
                    if batch_messages:
                        print(f"First message in batch: #{batch_messages[0].id}")
                        print(f"Last message in batch: #{batch_messages[-1].id}")
                    
                    if not batch_messages:
                        print("\nNo more messages in batch, breaking loop")
                        break
                    
                    # Update current_id for next batch
                    current_id = min(msg.id for msg in batch_messages) - 1
                    print(f"Next batch will start from ID: {current_id}")
                    
                    # Add delay to respect rate limits
                    if len(batch_messages) == MESSAGES_BATCH_SIZE:
                        print(f"Waiting {BATCH_DELAY} seconds before next batch to respect rate limits...")
                        await asyncio.sleep(BATCH_DELAY)
                    
                    # Process batch
                    for message in batch_messages:
                        try:
                            # Create message dict with all available fields
                            message_dict = {
                                'id': message.id,
                                'date': str(message.date),
                                'edit_date': str(message.edit_date) if message.edit_date else None,
                                'from_id': message.from_id.user_id if message.from_id else None,
                                'text': message.text,
                                'raw_text': message.raw_text,
                                'out': message.out,
                                'mentioned': message.mentioned,
                                'media_unread': message.media_unread,
                                'silent': message.silent,
                                'post': message.post,
                                'from_scheduled': message.from_scheduled,
                                'legacy': message.legacy,
                                'edit_hide': message.edit_hide,
                                'pinned': message.pinned,
                                'noforwards': message.noforwards,
                                'views': getattr(message, 'views', 0),
                                'forwards': getattr(message, 'forwards', 0),
                                'has_media': bool(message.media),
                                'media_type': type(message.media).__name__ if message.media else None,
                                'media_file_path': None,  # Will store path to downloaded media
                                'grouped_id': str(message.grouped_id) if message.grouped_id else None,
                                'reactions': [],
                                'reply_to': message.reply_to.reply_to_msg_id if message.reply_to else None,
                                'last_update': str(datetime.now())
                            }
                            
                            # Add reactions if present
                            if hasattr(message, 'reactions') and message.reactions:
                                try:
                                    for reaction in message.reactions.results:
                                        reaction_data = {
                                            'emoticon': reaction.reaction.emoticon if hasattr(reaction.reaction, 'emoticon') else None,
                                            'document_id': reaction.reaction.document_id if hasattr(reaction.reaction, 'document_id') else None,
                                            'count': reaction.count,
                                            # Only add chosen if it exists
                                            'chosen': getattr(reaction, 'chosen', False)
                                        }
                                        message_dict['reactions'].append(reaction_data)
                                except Exception as reaction_error:
                                    logger.debug(f"Could not process reactions for message {message.id}: {str(reaction_error)}")
                                    # Add basic reaction info without chosen status
                                    for reaction in message.reactions.results:
                                        try:
                                            reaction_data = {
                                                'emoticon': reaction.reaction.emoticon if hasattr(reaction.reaction, 'emoticon') else None,
                                                'document_id': reaction.reaction.document_id if hasattr(reaction.reaction, 'document_id') else None,
                                                'count': reaction.count
                                            }
                                            message_dict['reactions'].append(reaction_data)
                                        except Exception as e:
                                            logger.debug(f"Skipping malformed reaction in message {message.id}: {str(e)}")
                                            continue
                            
                            msg_id = str(message.id)
                            
                            # Download media if requested and message has media
                            if download_media and message.media:
                                try:
                                    # Create directories if needed
                                    os.makedirs(VIDEO_TEMP_DIR, exist_ok=True)
                                    
                                    # Check if we already have the media downloaded
                                    existing_media_path = None
                                    if msg_id in self.db['messages'][channel_id]:
                                        existing_media_path = self.db['messages'][channel_id][msg_id].get('media_file_path')
                                    
                                    if (not existing_media_path or 
                                        not os.path.exists(existing_media_path) or 
                                        force_redownload):
                                        
                                        # Create a filename based on message ID and date
                                        filename = f"media_{message.id}_{message.date.strftime('%Y%m%d_%H%M%S')}"
                                        
                                        # Download the media
                                        print(f"Downloading media from message #{message.id}...")
                                        
                                        # Get media file size if available
                                        file_size = None
                                        if hasattr(message.media, 'document'):
                                            file_size = getattr(message.media.document, 'size', None)
                                            if file_size:
                                                size_mb = file_size / (1024 * 1024)
                                                print(f"Media size: {size_mb:.2f} MB")
                                        
                                        # Use our enhanced download method
                                        download_result = await self.download_media_safely(
                                            message=message,
                                            filename=filename,
                                            file_size=file_size
                                        )
                                        
                                        if download_result['success']:
                                            file_path = download_result['file_path']
                                            print(f"Media saved to: {file_path}")
                                            # Update message dict with media path
                                            message_dict['media_file_path'] = file_path
                                            media_downloaded += 1
                                            
                                            # Also store in videos database if it's a video
                                            is_video = False
                                            mime_type = getattr(message.media.document, 'mime_type', '') if hasattr(message.media, 'document') else ''
                                            if mime_type and 'video' in mime_type:
                                                is_video = True
                                                
                                            if is_video:
                                                # Initialize videos dict if needed
                                                if 'videos' not in self.db:
                                                    self.db['videos'] = {}
                                                if channel_id not in self.db['videos']:
                                                    self.db['videos'][channel_id] = {}
                                                    
                                                # Add video information
                                                video_info = {
                                                    'id': message.id,
                                                    'date': str(message.date),
                                                    'from_id': message_dict['from_id'],
                                                    'media_type': message_dict['media_type'],
                                                    'file_path': file_path,
                                                    'download_date': str(datetime.now()),
                                                    'file_size': os.path.getsize(file_path) if os.path.exists(file_path) else None,
                                                    'duration': getattr(message.media.document, 'duration', None) if hasattr(message.media, 'document') else None,
                                                    'mime_type': mime_type,
                                                    'size': getattr(message.media.document, 'size', None) if hasattr(message.media, 'document') else None,
                                                }
                                                self.db['videos'][channel_id][msg_id] = video_info
                                        else:
                                            # Handle download failure
                                            print(f"Failed to download media: {download_result['error']}")
                                            logger.warning(f"Media download failed for message {message.id}: {download_result['error']}")
                                            media_errors += 1
                                        
                                        # Add delay between media downloads to avoid rate limits
                                        await asyncio.sleep(MEDIA_DOWNLOAD_DELAY)
                                    else:
                                        # Media already downloaded
                                        message_dict['media_file_path'] = existing_media_path
                                        print(f"Media for message #{message.id} already downloaded, skipping...")
                                        media_skipped += 1
                                        
                                except Exception as media_error:
                                    print(f"Error downloading media from message #{message.id}: {str(media_error)}")
                                    logger.error(f"Error downloading media: {str(media_error)}")
                                    media_errors += 1
                            
                            if msg_id in self.db['messages'][channel_id] and not force_redownload:
                                # Check if message needs update
                                existing = self.db['messages'][channel_id][msg_id]
                                if (existing.get('views') != message_dict['views'] or 
                                    existing.get('forwards') != message_dict['forwards'] or
                                    existing.get('reactions') != message_dict['reactions'] or
                                    (download_media and existing.get('media_file_path') != message_dict['media_file_path'])):
                                    self.db['messages'][channel_id][msg_id].update(message_dict)
                                    updated += 1
                                else:
                                    skipped += 1
                            else:
                                # Add new message or force update
                                self.db['messages'][channel_id][msg_id] = message_dict
                                saved += 1
                            
                        except Exception as msg_error:
                            logger.error(f"Error processing message {message.id}: {str(msg_error)}")
                            errors += 1
                            continue
                    
                    # Update progress
                    current_time = datetime.now()
                    elapsed = current_time - start_time
                    speed = (saved + updated + skipped) / elapsed.total_seconds() if elapsed.total_seconds() > 0 else 0
                    
                    # Save database periodically
                    if (current_time - last_save_time).total_seconds() > SAVE_INTERVAL:
                        self.save_database()
                        last_save_time = current_time
                    
                    # Update display
                    print("\033[F\033[K" * 7)
                    print(f"Progress: {saved + updated + skipped}/{total} messages ({current_time - start_time})")
                    print(f"New: {saved} | Updated: {updated} | Skipped: {skipped} | Errors: {errors}")
                    print(f"Speed: {speed:.1f} messages/second")
                    print(f"Elapsed: {str(elapsed).split('.')[0]}")
                    print(f"Current Batch: {len(batch_messages)} messages (ID: {current_id})")
                    print(f"Retries: {retry_count}/{MAX_RETRIES}")
                    print("-"*50)
                    
                    # Reset retry count on successful batch
                    retry_count = 0
                    
                except Exception as batch_error:
                    # Enhance error logging
                    print(f"\nDebug: Batch error details:")
                    print(f"- Error type: {type(batch_error).__name__}")
                    print(f"- Error message: {str(batch_error)}")
                    print(f"- Current message ID: {current_id}")
                    logger.error(f"Error processing batch: {str(batch_error)}", exc_info=True)
                    
                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        print(f"\nToo many errors, stopping download at message {current_id}")
                        break
                    print(f"\nRetrying batch in {BATCH_DELAY * 2} seconds... ({retry_count}/{MAX_RETRIES})")
                    await asyncio.sleep(BATCH_DELAY * 2)
            
            # Final save
            self.save_database()
            
            # Final statistics
            end_time = datetime.now()
            elapsed = end_time - start_time
            speed = (saved + updated + skipped) / elapsed.total_seconds() if elapsed.total_seconds() > 0 else 0
            
            print("\n" + "="*50)
            print("Download Completed!")
            print("="*50)
            print(f"\nFinal Statistics:")
            print(f"Total Processed: {saved + updated + skipped}")
            print(f"New Messages: {saved}")
            print(f"Updated Messages: {updated}")
            print(f"Skipped Messages: {skipped}")
            print(f"Errors: {errors}")
            print(f"Total Retries: {retry_count}")
            print(f"\nMedia Statistics:")
            print(f"Media Downloaded: {media_downloaded}")
            print(f"Media Skipped: {media_skipped}")
            print(f"Media Errors: {media_errors}")
            print(f"\nTime Elapsed: {str(elapsed).split('.')[0]}")
            print(f"Average Speed: {speed:.1f} messages/second")
            print("="*50)
            
            return True
            
        except Exception as e:
            logger.error(f"Error saving channel messages: {e}")
            print(f"\nError saving messages: {str(e)}")
            return False

    async def list_saved_users(self):
        """List users saved from active channel"""
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return
            
        channel_id = str(active['id'])
        if channel_id not in self.db.get('users', {}):
            print("\nNo saved users for this channel!")
            return
            
        users = self.db['users'][channel_id]
        if not users:
            print("\nNo users found!")
            return
            
        print("\nSaved Users:")
        print("-" * 80)
        print(f"{'ID':<12} | {'Username':<15} | {'Name':<20} | {'Type':<8} | {'Status'}")
        print("-" * 80)
        
        for user_id, user in sorted(users.items(), key=lambda x: x[1].get('username') or ''):
            username = f"@{user['username']}" if user['username'] else '-'
            name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or '-'
            user_type = 'Bot' if user['bot'] else 'User'
            
            status = []
            if user['premium']: status.append('Premium')
            if user['verified']: status.append('Verified')
            if user['scam']: status.append('Scam')
            if user['fake']: status.append('Fake')
            if user['restricted']: status.append('Restricted')
            
            status_str = ', '.join(status) if status else '-'
            
            print(f"{user_id:<12} | {username:<15} | {name[:20]:<20} | {user_type:<8} | {status_str}")
        
        print("-" * 80)
        print(f"Total Users: {len(users)}")

    async def search_messages(self):
        """Search in saved messages"""
        active = self.get_active_channel()
        if not active:
            print("\nNo active channel selected!")
            return
            
        channel_id = str(active['id'])
        if channel_id not in self.db.get('messages', {}):
            print("\nNo saved messages for this channel!")
            return
            
        messages = self.db['messages'][channel_id]
        if not messages:
            print("\nNo messages found!")
            return
        
        print("\nSearch Options:")
        print("1. Search by text")
        print("2. Search by date range")
        print("3. Search by message ID")
        print("4. Show messages with reactions")
        print("5. Show messages with media")
        print("6. Show user's last messages")
        print("7. Back to main menu")
        
        choice = input("\nEnter your choice (1-7): ")
        
        if choice == '1':
            query = input("\nEnter search text: ").lower()
            results = []
            
            for msg_id, msg in messages.items():
                if msg.get('text') and query in msg['text'].lower():
                    results.append(msg)
                    
            self._display_message_results(results, f"Messages containing '{query}'")
            
        elif choice == '2':
            from_date = input("\nEnter start date (YYYY-MM-DD): ")
            to_date = input("Enter end date (YYYY-MM-DD): ")
            
            try:
                from_dt = datetime.strptime(from_date, '%Y-%m-%d')
                to_dt = datetime.strptime(to_date, '%Y-%m-%d')
                
                results = []
                for msg_id, msg in messages.items():
                    msg_date = datetime.strptime(msg['date'].split('+')[0], '%Y-%m-%d %H:%M:%S')
                    if from_dt <= msg_date <= to_dt:
                        results.append(msg)
                        
                self._display_message_results(results, f"Messages from {from_date} to {to_date}")
                
            except ValueError:
                print("\nInvalid date format! Use YYYY-MM-DD")
                
        elif choice == '3':
            msg_id = input("\nEnter message ID: ")
            if msg_id in messages:
                self._display_message_results([messages[msg_id]], "Message found")
            else:
                print("\nMessage not found!")
                
        elif choice == '4':
            results = []
            for msg_id, msg in messages.items():
                if msg.get('reactions') and len(msg['reactions']) > 0:
                    results.append(msg)
                    
            self._display_message_results(results, "Messages with reactions")
            
        elif choice == '5':
            results = []
            for msg_id, msg in messages.items():
                if msg.get('has_media'):
                    results.append(msg)
                    
            self._display_message_results(results, "Messages with media")
            
        elif choice == '6':
            # Show users to choose from
            if channel_id not in self.db.get('users', {}):
                print("\nNo saved users for this channel! Please save users first.")
                return
                
            users = self.db['users'][channel_id]
            print("\nAvailable Users:")
            print("-" * 60)
            print(f"{'ID':<12} | {'Username':<15} | {'Name':<20}")
            print("-" * 60)
            
            # Show users sorted by username
            for user_id, user in sorted(users.items(), key=lambda x: x[1].get('username') or ''):
                username = f"@{user['username']}" if user['username'] else '-'
                name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or '-'
                print(f"{user_id:<12} | {username:<15} | {name[:20]:<20}")
            
            # Get user choice
            user_id = input("\nEnter user ID (or username with @): ")
            
            # Find user by ID or username
            target_user_id = None
            if user_id.startswith('@'):
                username = user_id[1:]
                for uid, user in users.items():
                    if user.get('username') == username:
                        target_user_id = uid
                        break
            else:
                target_user_id = user_id
            
            if target_user_id not in users:
                print("\nUser not found!")
                return
            
            # Find user's messages
            user_messages = []
            target_user_id_str = str(target_user_id)  # Convert to string for comparison
            for msg_id, msg in messages.items():
                # Check both from_id and sender_id (for compatibility)
                msg_from_id = msg.get('from_id')
                if msg_from_id is not None:
                    msg_from_id_str = str(msg_from_id)  # Convert to string
                    if msg_from_id_str == target_user_id_str:
                        user_messages.append(msg)
            
            if not user_messages:
                user = users[target_user_id]
                username = f"@{user['username']}" if user['username'] else 'No username'
                name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or 'No name'
                print(f"\nNo messages found for user {name} ({username})")
                
                # Debug info
                print("\nDebug info:")
                print(f"Looking for user ID: {target_user_id_str}")
                print(f"Total messages in channel: {len(messages)}")
                print(f"Sample message from_ids: {[str(msg.get('from_id')) for msg in list(messages.values())[:5]]}")
                return
            
            # Sort by date (newest first) and take last 10
            user_messages.sort(key=lambda x: x['date'], reverse=True)
            last_messages = user_messages[:10]
            
            # Display results
            user = users[target_user_id]
            username = f"@{user['username']}" if user['username'] else 'No username'
            name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or 'No name'
            
            self._display_message_results(
                last_messages,
                f"Last 10 messages from {name} ({username})"
            )
            
            # Show statistics
            print(f"\nUser Message Statistics:")
            print(f"Total messages: {len(user_messages)}")
            if user_messages:
                first_msg_date = min(msg['date'] for msg in user_messages)
                last_msg_date = max(msg['date'] for msg in user_messages)
                print(f"First message: {first_msg_date}")
                print(f"Last message: {last_msg_date}")
                
                # Count messages with media
                media_count = sum(1 for msg in user_messages if msg.get('has_media'))
                print(f"Messages with media: {media_count}")
                
                # Count reactions received
                total_reactions = sum(
                    sum(r['count'] for r in msg.get('reactions', []))
                    for msg in user_messages
                )
                print(f"Total reactions received: {total_reactions}")
            
        elif choice == '7':
            return
        else:
            print("\nInvalid choice!")

    async def download_media_safely(self, message, filename, file_size=None):
        """
        Enhanced media download method with safety features:
        - Timeout handling
        - Chunked downloads for large files
        - Retry mechanism with exponential backoff
        - Detailed error reporting
        
        Args:
            message: The message containing media
            filename: Base filename to save as
            file_size: Size of the file in bytes if known
            
        Returns:
            dict: Result with 'success', 'file_path', and optional 'error' fields
        """
        result = {
            'success': False,
            'file_path': None,
            'error': None
        }
        
        # Get target file path
        target_path = os.path.join(VIDEO_TEMP_DIR, filename)
        
        # Determine if file is large (over 10MB)
        is_large_file = False
        if file_size and file_size > 10 * 1024 * 1024:  # > 10MB
            is_large_file = True
            print(f"Large file detected ({file_size/(1024*1024):.2f} MB). Using chunked download.")
        
        # For retry mechanism
        retry_count = 0
        max_retries = MEDIA_DOWNLOAD_RETRY
        
        # Initialize start_time variable for both large and small files
        start_time = datetime.now()
        
        # Try to download with retries
        while retry_count <= max_retries:
            try:
                if retry_count > 0:
                    # Calculate backoff delay with jitter to avoid thundering herd
                    backoff_time = MEDIA_RETRY_DELAY_BASE * (2 ** (retry_count - 1))
                    # Add jitter (±25%)
                    jitter = random.uniform(0.75, 1.25)
                    delay = backoff_time * jitter
                    print(f"Retry {retry_count}/{max_retries} after {delay:.1f} seconds...")
                    await asyncio.sleep(delay)
                
                # Reset start_time for each attempt
                start_time = datetime.now()
                
                if is_large_file:
                    # Use chunked download for large files
                    print(f"Starting chunked download with {CHUNK_SIZE/1024:.0f}KB chunks...")
                    
                    # Use a custom progress callback
                    last_update_time = start_time
                    bytes_downloaded = 0
                    
                    # Progress callback function
                    def progress_callback(downloaded_bytes, total_bytes):
                        nonlocal bytes_downloaded, last_update_time
                        bytes_downloaded = downloaded_bytes
                        
                        # Only update display every second
                        current_time = datetime.now()
                        if (current_time - last_update_time).total_seconds() >= 1:
                            # Calculate speed
                            elapsed = (current_time - start_time).total_seconds()
                            speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                            
                            # Format sizes
                            downloaded_mb = downloaded_bytes / (1024 * 1024)
                            total_mb = total_bytes / (1024 * 1024) if total_bytes else 0
                            speed_kbps = speed / 1024
                            
                            # Calculate percentage
                            percent = (downloaded_bytes / total_bytes * 100) if total_bytes else 0
                            
                            # Calculate ETA
                            if speed > 0 and total_bytes:
                                remaining_bytes = total_bytes - downloaded_bytes
                                eta_seconds = remaining_bytes / speed
                                eta_str = str(datetime.fromtimestamp(eta_seconds) - datetime.fromtimestamp(0))
                                eta_str = eta_str.split('.')[0]  # Remove microseconds
                            else:
                                eta_str = "unknown"
                            
                            print(f"\rProgress: {downloaded_mb:.2f}/{total_mb:.2f} MB " + 
                                  f"({percent:.1f}%) at {speed_kbps:.1f} KB/s - ETA: {eta_str}", 
                                  end='')
                            
                            last_update_time = current_time
                        
                        return True  # Continue download
                    
                    # Attempt the chunked download with timeout
                    file_path = await asyncio.wait_for(
                        self.client.download_media(
                            message.media,
                            file=target_path,
                            progress_callback=progress_callback
                        ),
                        timeout=MEDIA_DOWNLOAD_TIMEOUT
                    )
                    
                    print()  # New line after progress output
                    
                else:
                    # Regular download for smaller files
                    file_path = await asyncio.wait_for(
                        self.client.download_media(
                            message.media,
                            file=target_path
                        ),
                        timeout=MEDIA_DOWNLOAD_TIMEOUT
                    )
                
                # Success!
                if file_path:
                    # Verify downloaded file
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                        downloaded_size = os.path.getsize(file_path)
                        if file_size and abs(downloaded_size - file_size) > 1024:  # Allow 1KB difference
                            # File size mismatch, file may be incomplete
                            print(f"Warning: File size mismatch. Expected: {file_size}, Got: {downloaded_size}")
                            logger.warning(f"File size mismatch for {file_path}. Expected: {file_size}, Got: {downloaded_size}")
                        
                        # Calculate download speed
                        elapsed = (datetime.now() - start_time).total_seconds()
                        speed_mbps = (downloaded_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                        print(f"Download completed: {downloaded_size/(1024*1024):.2f} MB in {elapsed:.1f} seconds ({speed_mbps:.2f} MB/s)")
                        
                        result['success'] = True
                        result['file_path'] = file_path
                        return result
                    else:
                        # File doesn't exist or is empty
                        result['error'] = "Download failed - file is empty or missing"
                        if os.path.exists(file_path):
                            os.remove(file_path)  # Clean up empty file
                        # Continue to retry
                
                else:
                    result['error'] = "Download returned None"
                    # Continue to retry
            
            except asyncio.TimeoutError:
                result['error'] = f"Download timed out after {MEDIA_DOWNLOAD_TIMEOUT} seconds"
                print(f"Timeout error: {result['error']}")
                logger.warning(f"Download timeout for {filename}")
                # Continue to next retry
            
            except FloodWaitError as e:
                wait_time = e.seconds
                result['error'] = f"Rate limit exceeded! Required to wait {wait_time} seconds"
                print(f"Flood wait error: Need to wait {wait_time} seconds")
                logger.warning(f"FloodWaitError: {wait_time} seconds wait required")
                
                # This is a special case - wait the required time then retry
                await asyncio.sleep(wait_time)
                continue  # Skip retry increment
            
            except (ServerError, TimedOutError) as e:
                result['error'] = f"Telegram server error: {str(e)}"
                print(f"Server error: {result['error']}")
                logger.warning(f"Server error during download: {str(e)}")
                # Continue to retry
            
            except ConnectionError as e:
                result['error'] = f"Connection error: {str(e)}"
                print(f"Connection error: {result['error']}")
                logger.warning(f"Connection error during download: {str(e)}")
                # Continue to retry
            
            except OSError as e:
                result['error'] = f"OS error: {str(e)}"
                print(f"OS error: {result['error']}")
                logger.warning(f"OS error during download: {str(e)}")
                # Continue to retry
            
            except Exception as e:
                result['error'] = f"Unexpected error: {str(e)}"
                print(f"Unexpected error: {result['error']}")
                logger.error(f"Unexpected error during download: {str(e)}", exc_info=True)
                # Continue to retry
            
            # Increment retry counter
            retry_count += 1
        
        # If we get here, all retries failed
        print(f"All {max_retries} download attempts failed")
        return result

    def _display_message_results(self, messages, title):
        """Helper method to display message search results"""
        if not messages:
            print("\nNo messages found!")
            return
            
        print(f"\n{title}")
        print(f"Found {len(messages)} messages")
        print("-" * 80)
        
        # Sort messages by date
        messages.sort(key=lambda x: x['date'])
        
        for msg in messages:
            print(f"\nMessage #{msg['id']} ({msg['date']})")
            print(f"{'='*40}")
            
            if msg.get('text'):
                print(f"Text: {msg['text'][:200]}{'...' if len(msg['text']) > 200 else ''}")
            
            if msg.get('has_media'):
                print(f"Media: {msg['media_type']}")
            
            if msg.get('reactions'):
                reactions = []
                for reaction in msg['reactions']:
                    emoji = reaction.get('emoticon') or f"Custom({reaction.get('document_id')})"
                    reactions.append(f"{emoji}({reaction['count']})")
                print(f"Reactions: {' '.join(reactions)}")
            
            if msg.get('views'):
                print(f"Views: {msg['views']}")
            
            if msg.get('forwards'):
                print(f"Forwards: {msg['forwards']}")
            
            print("-" * 80)
        
        print(f"\nTotal results: {len(messages)}")

def main():
    """Entry point"""
    app = ChannelSaver()
    asyncio.run(app.start())

if __name__ == '__main__':
    main() 