import os
from dotenv import load_dotenv
import logging
import requests
import json
import time
import schedule
import telegram
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.constants import ParseMode
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
import asyncio

load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants for ConversationHandler states
SELECTING_MODULE, SELECTING_CHAT, CONFIRM_ADDITION = range(3)

# Data storage paths
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MODULES_FILE = DATA_DIR / "modules.json"
SENT_LINKS_FILE = DATA_DIR / "sent_links.json"
COOKIES_FILE = DATA_DIR / "cookies.json"

# Initialize data directory
DATA_DIR.mkdir(exist_ok=True)

# Default data structures
DEFAULT_MODULES = {}
DEFAULT_SENT_LINKS = {}

# Load configuration
def load_config():
    """Load bot configuration from environment variables"""
    return {
        "BOT_TOKEN": os.environ.get("BOT_TOKEN", ""),
        "USERNAME": os.environ.get("UNIV_USERNAME", ""),
        "PASSWORD": os.environ.get("UNIV_PASSWORD", ""),
        "ADMIN_ID": int(os.environ.get("ADMIN_ID", "0")),
        "LOGIN_URL": "https://elearning.univ-constantine2.dz/elearning/login/index.php",
        "BASE_URL": "https://elearning.univ-constantine2.dz/elearning/"
    }

CONFIG = load_config()

# Load and save JSON data
def load_json(file_path, default_data):
    """Load JSON data from file or return default if file doesn't exist or is invalid"""
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:  # Check if file is not empty
                    return json.loads(content)
        
        # Either file doesn't exist or is empty
        # Create the file with default data
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=2, ensure_ascii=False)
        return default_data
    except Exception as e:
        print(f"Error loading JSON from {file_path}: {e}")
        # If any error occurs, return default data
        return default_data

def save_json(file_path, data):
    """Save data to JSON file"""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving JSON to {file_path}: {e}")

# Session management
class UnivSession:
    """Class to manage university e-learning platform session"""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        })
        self.load_cookies()
    
    def load_cookies(self):
        """Load saved cookies if available"""
        try:
            if COOKIES_FILE.exists():
                with open(COOKIES_FILE, 'r') as f:
                    content = f.read().strip()
                    if content:  # Check if file is not empty
                        cookies = json.loads(content)
                        for cookie in cookies:
                            self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
                    else:
                        # File exists but is empty, no cookies to load
                        return
            else:
                # File doesn't exist, no cookies to load
                return
        except Exception as e:
            # Log error but continue without cookies
            print(f"Error loading cookies: {e}")
            return
    
    def save_cookies(self):
        """Save current session cookies"""
        cookies = [{'name': c.name, 'value': c.value, 'domain': c.domain} for c in self.session.cookies]
        with open(COOKIES_FILE, 'w') as f:
            json.dump(cookies, f)
    
    async def login(self):
        """Login to the university e-learning platform"""
        # First request to get the login form
        login_page = self.session.get(CONFIG["LOGIN_URL"])
        soup = BeautifulSoup(login_page.text, 'html.parser')
        
        # Prepare login form data
        login_data = {
            'username': CONFIG["USERNAME"],
            'password': CONFIG["PASSWORD"],
        }
        
        # Look for any hidden fields to include in form submission
        for input_field in soup.select('form input[type="hidden"]'):
            if input_field.get('name') and input_field.get('value'):
                login_data[input_field['name']] = input_field['value']
        
        # Submit login form
        response = self.session.post(CONFIG["LOGIN_URL"], data=login_data)
        
        # Check if login was successful
        if "loginerrors" in response.text or "Invalid login" in response.text:
            logger.error("Login failed")
            return False
        
        # Save cookies for later use
        self.save_cookies()
        logger.info("Login successful")
        return True
    
    async def get_page(self, url):
        """Get page content, login again if session expired"""
        response = self.session.get(url)
        if "loginerrors" in response.text or "You are not logged in" in response.text:
            logger.info("Session expired, logging in again")
            if await self.login():
                response = self.session.get(url)
            else:
                return None
        return response.text

    async def download_file(self, url, filename):
        """Download a file from the given URL"""
        try:
            # Special handling for Google Drive URLs
            if 'drive.google.com' in url:
                # Extract file ID from Drive URL
                file_id = None
                if '/file/d/' in url:
                    file_id = url.split('/file/d/')[1].split('/')[0]
                elif 'id=' in url:
                    file_id = url.split('id=')[1].split('&')[0]
                
                if file_id:
                    # Use the direct download link format
                    direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
                    response = self.session.get(direct_url, stream=True, allow_redirects=True)
                else:
                    logger.error(f"Could not extract file ID from Drive URL: {url}")
                    return None
            else:
                response = self.session.get(url, stream=True, allow_redirects=True)

            # Check status code
            if response.status_code != 200:
                logger.error(f"Download failed with status code: {response.status_code}")
                return None
            
            # If it's a redirect URL (like Moodle's redirect), follow the chain
            if "url/view.php" in url or "resource/view.php" in url:
                soup = BeautifulSoup(response.text, 'html.parser')
                redirect_link = soup.find('a', href=True)
                if redirect_link:
                    actual_url = redirect_link['href']
                    response = self.session.get(actual_url, stream=True, allow_redirects=True)
            
            # Create the file
            file_path = DATA_DIR / filename
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Check if file was downloaded and has content
            if file_path.exists() and file_path.stat().st_size > 0:
                return file_path
            else:
                logger.error(f"File download failed or resulted in empty file: {url}")
                # Delete empty file if it exists
                if file_path.exists():
                    file_path.unlink()
                return None
                    
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return None

# PDF Detection and Processing
class PDFMonitor:
    """Class to monitor and detect new PDFs on module pages"""
    def __init__(self, univ_session):
        self.session = univ_session
        try:
            self.modules = load_json(MODULES_FILE, DEFAULT_MODULES)
            self.sent_links = load_json(SENT_LINKS_FILE, DEFAULT_SENT_LINKS)
        except Exception as e:
            print(f"Error initializing PDFMonitor: {e}")
            # If there's an error, use default values
            self.modules = DEFAULT_MODULES
            self.sent_links = DEFAULT_SENT_LINKS
    
    def save_data(self):
        """Save modules and sent links data"""
        save_json(MODULES_FILE, self.modules)
        save_json(SENT_LINKS_FILE, self.sent_links)
    
    def add_module(self, module_id, module_name, module_url, chat_id):
        """Add a new module to monitor"""
        self.modules[module_id] = {
            "name": module_name,
            "url": module_url,
            "chat_id": chat_id,  # This can now be a topic ID like "-1002612777381_1"
            "added_at": datetime.now().isoformat()
        }
        
        # Create chat-specific entry if it doesn't exist
        if module_id not in self.sent_links:
            self.sent_links[module_id] = {}
        
        # Initialize empty list for this chat/topic
        # Convert the chat_id to string to ensure consistency
        chat_id_str = str(chat_id)
        if chat_id_str not in self.sent_links[module_id]:
            self.sent_links[module_id][chat_id_str] = []
            
        self.save_data()

    
    def remove_module(self, module_id):
        """Remove a module from monitoring"""
        if module_id in self.modules:
            del self.modules[module_id]
            self.save_data()
            return True
        return False
    
    async def check_modules(self, bot):
        """Check all modules for new PDFs"""
        all_new_files = {}
        
        for module_id, module_info in self.modules.items():
            new_files = await self.check_module_page(module_id, module_info["url"])
            if new_files and isinstance(new_files, list):
                all_new_files[module_id] = new_files
        
        # Send notifications for new files
        for module_id, files in all_new_files.items():
            if not isinstance(files, list):
                logger.error(f"Expected files to be a list for module {module_id}, got {type(files)}")
                continue
                    
            module_info = self.modules[module_id]
            chat_id = module_info["chat_id"]
            
            # Convert chat_id to string for dictionary key
            chat_id_str = str(chat_id)
            
            for file_info in files:
                if not isinstance(file_info, dict):
                    logger.error(f"Expected file_info to be a dict, got {type(file_info)}")
                    continue
                    
                try:
                    await self.send_file_notification(bot, chat_id, module_info["name"], file_info)
                    
                    # Ensure dictionaries exist
                    if module_id not in self.sent_links:
                        self.sent_links[module_id] = {}
                    if chat_id_str not in self.sent_links[module_id]:
                        self.sent_links[module_id][chat_id_str] = []
                    
                    # Add URL to sent links
                    if "url" in file_info and isinstance(self.sent_links[module_id][chat_id_str], list):
                        self.sent_links[module_id][chat_id_str].append(file_info["url"])
                    
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"Error sending notification: {e}")
        
        # Save updated sent links
        if all_new_files:
            self.save_data()

    # Here's the correct implementation of check_module_page
    async def check_module_page(self, module_id, url):
        """Check a single module page for new PDFs"""
        html_content = await self.session.get_page(url)
        if not html_content:
            logger.error(f"Failed to get content for module {module_id}")
            return []
        
        module_info = self.modules[module_id]
        chat_id = str(module_info["chat_id"])  # Convert to string
        
        # Initialize if not exists
        if module_id not in self.sent_links:
            self.sent_links[module_id] = {}
        if chat_id not in self.sent_links[module_id]:
            self.sent_links[module_id][chat_id] = []
        
        # Get sent links for this specific chat - ensure it's a list
        chat_sent_links = self.sent_links[module_id].get(chat_id, [])
        if not isinstance(chat_sent_links, list):
            chat_sent_links = []
            self.sent_links[module_id][chat_id] = chat_sent_links
        
        soup = BeautifulSoup(html_content, 'html.parser')
        new_files = []
        
        # Check for direct resource links (Type 1)
        for resource in soup.select('li.activity.resource.modtype_resource'):
            link_tag = resource.select_one('div.activityinstance a')
            if not link_tag:
                continue
                
            # Get URL from href or onclick attribute
            resource_url = link_tag.get('href')
            
            # If href is empty, try to extract URL from onclick
            if not resource_url or resource_url.strip() == '':
                onclick = link_tag.get('onclick', '')
                url_match = re.search(r"window\.open\('([^']+)'", onclick)
                if url_match:
                    resource_url = url_match.group(1)
            
            # If still no URL, skip this resource
            if not resource_url or resource_url.strip() == '':
                continue
                
            name_tag = link_tag.select_one('span.instancename')
            resource_name = name_tag.get_text(strip=True) if name_tag else "Unnamed resource"

            
            # Remove any "URL" or other suffix text from the name
            resource_name = re.sub(r'<span class="accesshide[^>]*>.*?</span>', '', resource_name)
            resource_name = resource_name.strip()
            
            # Check if this is a new link FOR THIS CHAT
            if resource_url not in chat_sent_links:
                # Check if it's likely a PDF by following the link
                file_info = await self.process_resource_link(resource_url, resource_name)
                if file_info:
                    new_files.append(file_info)
        
        # Check for external URL links (Type 2)
        for url_item in soup.select('li.activity.url.modtype_url'):
            link_tag = url_item.select_one('div.activityinstance a')
            if not link_tag:
                continue
                
            # Get URL from href or onclick attribute
            url_resource = link_tag.get('href')
            
            # If href is empty, try to extract URL from onclick
            if not url_resource or url_resource.strip() == '':
                onclick = link_tag.get('onclick', '')
                url_match = re.search(r"window\.open\('([^']+)'", onclick)
                if url_match:
                    url_resource = url_match.group(1)
            
            # If still no URL, skip this resource
            if not url_resource or url_resource.strip() == '':
                continue
                
            name_tag = link_tag.select_one('span.instancename')
            url_name = name_tag.get_text(strip=True) if name_tag else "Unnamed URL"
            
            # Remove any "URL" or other suffix text from the name
            url_name = re.sub(r'<span class="accesshide[^>]*>.*?</span>', '', url_name)
            url_name = url_name.strip()
            
            # Check if this is a new link FOR THIS CHAT
            if url_resource not in chat_sent_links:
                # Check if it leads to a PDF or Google Drive
                file_info = await self.process_url_link(url_resource, url_name)
                if file_info:
                    new_files.append(file_info)
        
        return new_files
    
    async def process_resource_link(self, url, name):
        """Process a resource link to determine if it's a PDF"""
        try:
            # Clean the name - remove ALL accesshide spans completely
            name = re.sub(r'<span class="accesshide[^>]*>.*?</span>', '', name, flags=re.DOTALL)
            # Also clean up any trailing/leading spaces
            name = name.strip()
            
            # Rest of the function remains the same
            # Validate URL before proceeding
            if not url or url.strip() == '':
                logger.warning(f"Empty resource URL found for '{name}', skipping")
                return None
                
            # Add scheme if missing
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
                
            response = self.session.session.get(url, allow_redirects=True)
            content_type = response.headers.get('Content-Type', '')
            
            # Direct PDF detection
            if 'application/pdf' in content_type:
                return {
                    "url": url,
                    "name": name,
                    "type": "pdf",
                    "final_url": response.url
                }
            
            # Check for PDF in URL 
            if response.url.lower().endswith('.pdf'):
                return {
                    "url": url,
                    "name": name,
                    "type": "pdf",
                    "final_url": response.url
                }
            
            # If it's HTML, it might be a redirect page
            if 'text/html' in content_type:
                soup = BeautifulSoup(response.text, 'html.parser')
                # Look for a direct link that might be the actual file
                main_link = soup.find('a', href=True)
                if main_link and main_link['href'].strip():
                    target_url = main_link['href']
                    
                    # Add scheme if missing in target URL
                    if not target_url.startswith(('http://', 'https://')):
                        target_url = 'https://' + target_url
                        
                    if 'drive.google.com' in target_url:
                        return {
                            "url": url,
                            "name": name,
                            "type": "drive",
                            "final_url": target_url
                        }
                    elif target_url.lower().endswith('.pdf'):
                        return {
                            "url": url,
                            "name": name,
                            "type": "pdf",
                            "final_url": target_url
                        }
            
            return None
        except requests.exceptions.InvalidURL as e:
            logger.error(f"Error processing resource link: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing resource link: {e}")
            return None

    async def process_url_link(self, url, name):
        """Process a URL link to determine if it leads to a PDF or Drive"""
        try:
            # Clean the name - remove ALL accesshide spans completely
            name = re.sub(r'<span class="accesshide[^>]*>.*?</span>', '', name, flags=re.DOTALL)
            # Also clean up any trailing/leading spaces
            name = name.strip()
            
            # Rest of the function remains the same
            # Validate URL before proceeding
            if not url or url.strip() == '':
                logger.warning(f"Empty URL found for '{name}', skipping")
                return None
                
            # Add scheme if missing
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
                
            response = self.session.session.get(url, allow_redirects=True)
            
            # Check if it's a redirect page
            soup = BeautifulSoup(response.text, 'html.parser')
            redirect_link = soup.find('a', href=True)
            
            if redirect_link:
                target_url = redirect_link['href']
                
                # Skip empty target URLs
                if not target_url or target_url.strip() == '':
                    return None
                    
                # Add scheme if missing in target URL
                if not target_url.startswith(('http://', 'https://')):
                    target_url = 'https://' + target_url
                
                # Check for Google Drive links
                if 'drive.google.com' in target_url:
                    return {
                        "url": url,
                        "name": name,
                        "type": "drive",
                        "final_url": target_url
                    }
                
                # Check for PDF links
                elif target_url.lower().endswith('.pdf'):
                    return {
                        "url": url,
                        "name": name,
                        "type": "pdf",
                        "final_url": target_url
                    }
                
                # Check for YouTube links
                elif 'youtube.com' in target_url or 'youtu.be' in target_url:
                    return {
                        "url": url,
                        "name": name,
                        "type": "youtube",
                        "final_url": target_url
                    }
            
            return None
        except requests.exceptions.InvalidURL as e:
            logger.error(f"Error processing URL link: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing URL link: {e}")
            return None
    
    async def send_file_notification(self, bot, chat_id, module_name, file_info):
        """Send notification about new file to the specified chat"""
        try:
            cleaned_name = re.sub(r'URL|Fichier', '', file_info['name']).strip()
            message = f"\n📄 *{cleaned_name}*"
            
            # Extract the chat ID and thread ID if this is a topic format
            thread_id = None
            base_chat_id = chat_id
            
            # Check if the chat_id is a string with format "-100xxx_yyy"
            if isinstance(chat_id, str) and "_" in chat_id:
                parts = chat_id.split("_")
                base_chat_id = int(parts[0])
                thread_id = int(parts[1])
            
            kwargs = {
                "chat_id": base_chat_id,
                "parse_mode": ParseMode.MARKDOWN
            }
            
            # Add message_thread_id if this is a topic
            if thread_id:
                kwargs["message_thread_id"] = thread_id

            
            if file_info['type'] == 'youtube':
                # For YouTube links, just send the link
                kwargs["text"] = f"{message}\n\n🎬 YouTube video: {file_info['final_url']}"
                await bot.send_message(**kwargs)
                # Add a delay to avoid rate limiting
                await asyncio.sleep(1)
            
            elif file_info['type'] == 'drive' or file_info['type'] == 'pdf':
                # For both PDFs and Drive files, download and send the file
                cleaned_name = re.sub(r'\s*(Fichier|URL|Dossier|Document|File|Link|Resource)\s*$', '', file_info['name'], flags=re.IGNORECASE)
                # Then sanitize for filesystem
                sanitized_name = re.sub(r'[^\w\.-]', '_', cleaned_name.strip())
                filename = f"{sanitized_name}.pdf"
                
                # Download the file without sending a status message
                file_path = await self.session.download_file(file_info['final_url'], filename)
                
                if file_path and file_path.exists() and file_path.stat().st_size > 0:
                    # Send the file
                    try:
                        with open(file_path, 'rb') as f:
                            kwargs["document"] = f
                            kwargs["filename"] = filename
                            kwargs["caption"] = message
                            await bot.send_document(**kwargs)
                        # Add a delay to avoid rate limiting
                        await asyncio.sleep(2)
                    except telegram.error.RetryAfter as e:
                        # Handle rate limiting
                        logger.warning(f"Rate limited. Waiting {e.retry_after} seconds")
                        await asyncio.sleep(e.retry_after)
                        # Try again after waiting
                        with open(file_path, 'rb') as f:
                            kwargs["document"] = f
                            kwargs["filename"] = filename
                            kwargs["caption"] = message
                            await bot.send_document(**kwargs)
                    finally:
                        # Delete the file after sending or if an error occurred
                        if file_path.exists():
                            file_path.unlink()
                else:
                    # If download failed, send a message with the link as fallback
                    kwargs["text"] = f"{message}\n\n⚠️ Couldn't download the file. Access it directly: {file_info['final_url']}"
                    await bot.send_message(**kwargs)
                    # Add a delay to avoid rate limiting
                    await asyncio.sleep(1)
            
            logger.info(f"Sent notification for {file_info['name']} to chat {chat_id}")
            return True
        
        except telegram.error.RetryAfter as e:
            # Handle rate limiting
            logger.warning(f"Rate limited. Waiting {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
            # Try recursively after waiting
            return await self.send_file_notification(bot, chat_id, module_name, file_info)
        except Exception as e:
            logger.error(f"Error sending file notification: {e}")
            # Send error notification with the link as fallback
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"{message}\n\n⚠️ Error processing file. Access it directly: {file_info['final_url']}\n\nError: {str(e)}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            return False

# Telegram Bot Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    welcome_message = (
        "🎓 *E-Learning Monitor Bot* 🎓\n\n"
        "This bot monitors your university's e-learning platform for new PDF files and other resources.\n\n"
        "*Available commands:*\n"
        "• /addmodule - Add a new module to monitor\n"
        "• /listmodules - List all modules being monitored\n"
        "• /removemodule - Remove a module from monitoring\n"
        "• /check - Manually check all modules for updates\n"
        "• /login - Test login to the university platform\n"
        "• /help - Show this help message"
    )
    
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    help_message = (
        "🎓 *E-Learning Monitor Bot Help* 🎓\n\n"
        "*Commands:*\n"
        "• /addmodule - Add a new module to monitor\n"
        "  Format: `/addmodule [Module Name] [URL]`\n"
        "  Example: `/addmodule Algorithms https://elearning.univ-constantine2.dz/elearning/course/view.php?id=123`\n\n"
        "• /listmodules - List all modules being monitored\n\n"
        "• /removemodule - Remove a module from monitoring\n"
        "  The bot will show you a list of modules to choose from\n\n"
        "• /check - Manually check all modules for updates\n\n"
        "• /login - Test login to the university platform\n\n"
        "• /help - Show this help message\n\n"
        "The bot automatically checks for updates once a day."
    )
    
    await update.message.reply_text(help_message, parse_mode=ParseMode.MARKDOWN)

async def test_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test login to the university platform."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    await update.message.reply_text("Attempting to login to the university platform...")
    
    univ_session = UnivSession()
    success = await univ_session.login()
    
    if success:
        await update.message.reply_text("✅ Login successful! Your credentials are working.")
    else:
        await update.message.reply_text("❌ Login failed. Please check your username and password.")

async def add_module_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the process of adding a new module."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return ConversationHandler.END
    
    # Parse command arguments
    args = context.args
    
    if len(args) < 2:
        await update.message.reply_text(
            "Please provide the module name and URL.\n"
            "Example: `/addmodule \"Module Name\" https://elearning.univ-constantine2.dz/elearning/course/view.php?id=123`",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END
    
    # Extract module URL from the last argument
    module_url = args[-1]
    
    # Extract module name from all arguments except the last one
    module_name = " ".join(args[:-1])
    
    # Try to extract the module ID from the URL
    try:
        parsed_url = urllib.parse.urlparse(module_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        module_id = query_params.get('id', ['unknown'])[0]
    except:
        module_id = f"module_{int(time.time())}"  # Fallback to a timestamp-based ID
    
    # Store the data for later use
    context.user_data["add_module"] = {
        "id": module_id,
        "name": module_name,
        "url": module_url
    }
    
    # Ask the user where to send notifications
    keyboard = [
        [InlineKeyboardButton("This chat", callback_data=f"chat_{update.effective_chat.id}")],
        [InlineKeyboardButton("Another chat", callback_data="select_other")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Where should I send notifications for *{module_name}*?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    return SELECTING_CHAT

async def select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle chat selection for module updates."""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("chat_"):
        # User selected the current chat
        # Keep as integer for regular chats
        chat_id = int(query.data.split("_")[1])
        
        # Save the selected chat ID
        module_data = context.user_data["add_module"]
        module_data["chat_id"] = chat_id
        
        # Show confirmation
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_add"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_add")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"I'll send updates for *{module_data['name']}* to this chat.\n\n"
            f"*Module:* {module_data['name']}\n"
            f"*URL:* {module_data['url']}\n"
            f"*Destination:* Chat ID {chat_id}\n\n"
            "Is this correct?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        return CONFIRM_ADDITION

async def select_other_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selection of another chat."""
    query = update.callback_query
    await query.answer()
    
    # Set flag that we're waiting for chat ID input
    context.user_data["waiting_for_chat_id"] = True
    
    await query.edit_message_text(
        "Please enter the chat ID or topic ID for notifications.\n\n"
        "For regular chats, enter the numeric ID (e.g., `-1001234567890`).\n"
        "For topics in a forum, use the format: `-1001234567890_123` where the part after the underscore is the topic ID.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Make sure to return SELECTING_CHAT to stay in the conversation flow
    return SELECTING_CHAT

async def handle_chat_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the input of a chat ID when selecting another chat."""
    if not context.user_data.get("waiting_for_chat_id", False):
        return None
    
    # Get the text message containing the chat ID
    chat_id_text = update.message.text.strip()
    
    try:
        # Check if it's a topic ID format (contains underscore)
        if "_" in chat_id_text:
            parts = chat_id_text.split("_")
            # Validate both parts are integers
            base_chat = int(parts[0])
            topic_id = int(parts[1])
            # Use the original string format to preserve the topic format
            chat_id = chat_id_text
        else:
            # Regular chat ID
            chat_id = int(chat_id_text)
        
        # Save the selected chat ID
        module_data = context.user_data["add_module"]
        module_data["chat_id"] = chat_id
        context.user_data["waiting_for_chat_id"] = False
        
        # Show confirmation
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_add"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_add")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Escape the chat_id for markdown by surrounding it with backticks
        display_chat_id = f"`{chat_id}`"
        
        await update.message.reply_text(
            f"I'll send updates for *{module_data['name']}* to the selected chat.\n\n"
            f"*Module:* {module_data['name']}\n"
            f"*URL:* {module_data['url']}\n"
            f"*Destination:* Chat ID {display_chat_id}\n\n"
            "Is this correct?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        return CONFIRM_ADDITION
    
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid chat ID. Please enter a valid numeric chat ID or topic ID in format `-100xxxxxxxxx_y`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SELECTING_CHAT

# Replace the complete_module_addition function with this fixed version
async def complete_module_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Complete the module addition process."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_add":
        try:
            # Get module data from context
            module_data = context.user_data.get("add_module", {})
            
            if not module_data or "id" not in module_data:
                await query.edit_message_text("❌ Error: Module data is missing or incomplete.")
                return ConversationHandler.END
            
            # Create a new UnivSession
            univ_session = UnivSession()
            
            # Create PDFMonitor
            pdf_monitor = PDFMonitor(univ_session)
            
            # Add the module
            module_id = module_data.get("id")
            module_name = module_data.get("name")
            module_url = module_data.get("url")
            chat_id = module_data.get("chat_id", update.effective_chat.id)
            
            # Use the add_module method directly
            pdf_monitor.add_module(module_id, module_name, module_url, chat_id)
            
            await query.edit_message_text(
                f"✅ Module *{module_name}* has been added successfully!\n\n"
                "The bot will now monitor this module for new PDF files and other resources.",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            await query.edit_message_text(f"❌ Error adding module: {str(e)}")
            # Log the full exception for debugging
            logger.error(f"Error in complete_module_addition: {e}", exc_info=True)
            
    else:  # cancel_add
        await query.edit_message_text("❌ Module addition cancelled.")
    
    # Clear user data
    if "add_module" in context.user_data:
        del context.user_data["add_module"]
    
    return ConversationHandler.END

async def list_modules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all modules being monitored."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    pdf_monitor = PDFMonitor(UnivSession())
    modules = pdf_monitor.modules
    
    if not modules:
        await update.message.reply_text("No modules are currently being monitored.")
        return
    
    message = "📚 *Modules Being Monitored* 📚\n\n"
    
    for module_id, module_info in modules.items():
        message += f"*{module_info['name']}*\n"
        message += f"URL: `{module_info['url']}`\n"
        message += f"Chat ID: `{module_info['chat_id']}`\n"
        
        # Add timestamp when the module was added
        if 'added_at' in module_info:
            added_at = datetime.fromisoformat(module_info['added_at'])
            message += f"Added: {added_at.strftime('%Y-%m-%d %H:%M')}\n"
        
        message += "\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def remove_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a module from monitoring."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    pdf_monitor = PDFMonitor(UnivSession())
    modules = pdf_monitor.modules
    
    if not modules:
        await update.message.reply_text("No modules are currently being monitored.")
        return
    
    # Create inline keyboard with modules
    keyboard = []
    for module_id, module_info in modules.items():
        button = InlineKeyboardButton(
            module_info['name'], 
            callback_data=f"remove_{module_id}"
        )
        keyboard.append([button])
    
    # Add cancel button
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_remove")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Select a module to remove:",
        reply_markup=reply_markup
    )

async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback for removing a module."""
    query = update.callback_query
    logger.info(f"Received callback data: {query.data}")
    await query.answer()
    
    if query.data == "cancel_remove":
        await query.edit_message_text("Operation cancelled.")
        return
    
    if query.data.startswith("remove_"):
        module_id = query.data[7:]  # Remove 'remove_' prefix
        
        pdf_monitor = PDFMonitor(UnivSession())
        module_name = pdf_monitor.modules.get(module_id, {}).get('name', 'Unknown module')
        
        # Store the module ID in context for the confirmation step
        context.user_data["module_to_remove"] = module_id
        context.user_data["module_name"] = module_name
        
        # Ask if user wants to delete sent links data too
        keyboard = [
            [InlineKeyboardButton("Remove module only", callback_data=f"confirm_remove_{module_id}_keep_history")],
            [InlineKeyboardButton("Remove module and history", callback_data=f"confirm_remove_{module_id}_delete_history")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_remove")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"How do you want to remove module *{module_name}*?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_remove_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation for removing a module."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_remove":
        await query.edit_message_text("Operation cancelled.")
        return
    
    if query.data.startswith("confirm_remove_"):
        # Parse the callback data with proper handling
        parts = query.data.split("_")
        if len(parts) >= 5:
            # Format is "confirm_remove_[module_id]_[keep/delete]_history"
            # If module_id contains underscores, we need to join those parts
            action_index = len(parts) - 2  # Second-to-last part is the action
            module_id_parts = parts[2:action_index]
            module_id = "_".join(module_id_parts)
            action = parts[action_index]  # "keep" or "delete"
            
            pdf_monitor = PDFMonitor(UnivSession())
            module_name = pdf_monitor.modules.get(module_id, {}).get('name', 'Unknown module')
            
            # Remove the module from monitoring
            result = pdf_monitor.remove_module(module_id)
            
            # If user chose to delete history, remove sent links data too
            if action == "delete" and result:
                if module_id in pdf_monitor.sent_links:
                    del pdf_monitor.sent_links[module_id]
                    pdf_monitor.save_data()
                    await query.edit_message_text(
                        f"✅ Module *{module_name}* and its history have been removed.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await query.edit_message_text(
                        f"✅ Module *{module_name}* has been removed. No history was found.",
                        parse_mode=ParseMode.MARKDOWN
                    )
            elif result:
                await query.edit_message_text(
                    f"✅ Module *{module_name}* has been removed. History data was kept.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text("❌ Failed to remove the module.")
        else:
            await query.edit_message_text("❌ Error processing the request: Invalid callback data.")

async def check_modules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually check all modules for updates."""
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    # Send the checking message and store the message object
    checking_msg = await update.message.reply_text("🔍 Checking modules for new files...")
    
    # Create session and monitor
    univ_session = UnivSession()
    pdf_monitor = PDFMonitor(univ_session)
    
    # Test login first
    login_success = await univ_session.login()
    if not login_success:
        # Delete checking message
        await checking_msg.delete()
        await update.message.reply_text("❌ Login failed. Please check your credentials.")
        return
    
    # Run the check
    await pdf_monitor.check_modules(context.bot)
    
    # Send completed message and store the message object
    completed_msg = await update.message.reply_text("✅ Check completed!")
    
    # Delete both messages after a short delay
    await asyncio.sleep(3)  # 3 seconds delay
    
    try:
        await checking_msg.delete()
        await completed_msg.delete()
    except Exception as e:
        logger.error(f"Error deleting messages: {e}")

async def link_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link the current chat for future module additions."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    
    message_thread_id = update.message.message_thread_id
    
    if message_thread_id:
        topic_id = f"{chat_id}_{message_thread_id}"
        await update.message.reply_text(
            f"This topic has been linked! Topic ID: `{topic_id}`\n\n"
            "You can use this topic ID when adding new modules.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"This chat has been linked! Chat ID: `{chat_id}`\n\n"
            "You can use this chat ID when adding new modules.",
            parse_mode=ParseMode.MARKDOWN
        )

# Scheduled task to check for updates
async def scheduled_check(app):
    """Run scheduled check for updates."""
    logger.info("Running scheduled check...")
    
    # Create session and monitor
    univ_session = UnivSession()
    pdf_monitor = PDFMonitor(univ_session)
    
    # Login
    login_success = await univ_session.login()
    if not login_success:
        logger.error("Scheduled check: Login failed")
        # Optionally notify admin of login failure
        try:
            await app.bot.send_message(
                chat_id=CONFIG["ADMIN_ID"],
                text="❌ Scheduled check failed: Login error"
            )
        except Exception as e:
            logger.error(f"Error sending failure notification: {e}")
        return
    
    # Run the check
    bot = app.bot
    await pdf_monitor.check_modules(bot)
    
    logger.info("Scheduled check completed")

def run_scheduled_check(app):
    """Run the scheduled check in the event loop."""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.run_coroutine_threadsafe(scheduled_check(app), loop)
    else:
        loop.run_until_complete(scheduled_check(app))

async def error_handler(update, context):
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")
    
    # Special handling for rate limiting errors
    if isinstance(context.error, telegram.error.RetryAfter):
        retry_after = context.error.retry_after
        logger.warning(f"Rate limited. Waiting {retry_after} seconds.")
        return
    
    # If update is available, notify the user about the error
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="An error occurred while processing your request. Please try again later."
            )
        except telegram.error.RetryAfter:
            # If we get rate limited while sending the error message, just log it
            logger.warning("Rate limited while sending error message.")
            pass

# Main function
def main():
    """Start the bot."""
    # Create the Application and pass it your bot's token
    application = Application.builder().token(CONFIG["BOT_TOKEN"]).build()

    # conversation handler for adding modules
    add_module_conv = ConversationHandler(
        entry_points=[CommandHandler("addmodule", add_module_start)],
        states={
            SELECTING_CHAT: [
                # Change this line to handle both patterns
                CallbackQueryHandler(select_chat, pattern=r"^chat_"),
                CallbackQueryHandler(select_other_chat, pattern=r"^select_other$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_id_input)
            ],
            CONFIRM_ADDITION: [CallbackQueryHandler(complete_module_addition)]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", test_login))
    application.add_handler(add_module_conv)
    application.add_handler(CommandHandler("listmodules", list_modules))
    application.add_handler(CommandHandler("removemodule", remove_module))
    application.add_handler(CommandHandler("check", check_modules_command))
    application.add_handler(CommandHandler("link", link_chat))
    

    # Handler for the initial module selection
    application.add_handler(CallbackQueryHandler(
        handle_remove_callback, 
        pattern=r"^(remove_[0-9a-zA-Z_]+|cancel_remove)$"
    ))

    # Handler for the confirmation step
    application.add_handler(CallbackQueryHandler(
        handle_remove_confirmation, 
        pattern=r"^confirm_remove_[0-9a-zA-Z_]+_(keep|delete)_history$"
    ))

    # Set up the scheduler
    schedule.every().day.at("08:00").do(run_scheduled_check, app=application)
    
    # Start the scheduler in a separate thread
    import threading
    
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
    
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
