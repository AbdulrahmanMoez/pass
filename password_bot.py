import os
import json
import time
import logging
import threading
import schedule
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import asyncio
from telegram.error import TelegramError, BadRequest
from functools import lru_cache  # Add caching decorator
import atexit  # For clean shutdown
from concurrent.futures import ProcessPoolExecutor  # For CPU-intensive tasks

# Import the password testing functionality
import password_tester

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global variables
ADMIN_CHAT_IDS = []  # Will be loaded from config
CONFIG = {}  # Global configuration cache
password_cache = {}  # In-memory password cache
notification_queue = None  # Will be initialized in main()

# Process pool for CPU-intensive tasks
process_pool = ProcessPoolExecutor(max_workers=2)

last_password = {
    "value": None,
    "timestamp": None,
    "attempts": 0,
    "total_time": 0,
    "valid": True,  # Add validity tracking
    "last_verified": None  # Track when it was last verified
}
is_test_running = False
last_test_status = {
    "start_time": None,
    "end_time": None,
    "found": False,
    "message_id": None
}
PUBLIC_ACCESS = True  # Set to True to allow any user to access basic bot functions
progress_message_ids = {}  # Store message IDs for progress updates
progress_data = {
    "current": 0,
    "total": 0,
    "start_time": None,
    "last_update_time": None
}

def load_config():
    """Load configuration from config.json with caching"""
    global ADMIN_CHAT_IDS, CONFIG
    
    if CONFIG:
        return CONFIG.get('bot_token', '')
    
    try:
        with open('config.json', 'r') as f:
            CONFIG = json.load(f)
            ADMIN_CHAT_IDS = CONFIG.get('admin_chat_ids', [])
            return CONFIG.get('bot_token', '')
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return None

def save_password_info(password_info):
    """Save password info with optimized disk writes"""
    global password_cache
    password_cache = password_info.copy()  # Store in memory
    
    # Only write to disk periodically to reduce I/O
    current_time = time.time()
    if not hasattr(save_password_info, 'last_write_time') or \
       current_time - save_password_info.last_write_time > 60:  # Write at most once per minute
        try:
            with open('last_password.json', 'w') as f:
                json.dump(password_info, f, indent=2)
            save_password_info.last_write_time = current_time
            logger.debug("Password info written to disk")
        except Exception as e:
            logger.error(f"Error saving password info: {e}")

def load_password_info():
    """Load password info from memory cache first, then file"""
    global last_password, password_cache
    
    # Return cached version if available
    if password_cache:
        last_password = password_cache.copy()
        return
    
    try:
        if os.path.exists('last_password.json'):
            with open('last_password.json', 'r') as f:
                last_password = json.load(f)
                password_cache = last_password.copy()  # Update cache
                logger.info(f"Loaded last password from file: {last_password['value']} (found on {last_password['timestamp']})")
    except Exception as e:
        logger.error(f"Error loading password info: {e}")

async def notify_admins(context, message, keyboard=None, parse_mode=None):
    """Queue notifications to all admin users"""
    global notification_queue
    
    # If we have a notification queue, use it
    if notification_queue:
        for chat_id in ADMIN_CHAT_IDS:
            await notification_queue.put((chat_id, message, keyboard, parse_mode))
        return
    
    # Fallback direct notification if queue not initialized
    for chat_id in ADMIN_CHAT_IDS:
        try:
            if keyboard:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=message, 
                    reply_markup=keyboard,
                    parse_mode=parse_mode
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=message,
                    parse_mode=parse_mode
                )
        except Exception as e:
            logger.error(f"Error sending message to {chat_id}: {e}")

# Use LRU cache for frequently called functions
@lru_cache(maxsize=128)
def get_readable_time(timestamp):
    """Convert timestamp to readable format with caching"""
    if not timestamp:
        return "Never"
    
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

@lru_cache(maxsize=128)
def format_elapsed_time(seconds):
    """Format seconds into readable time with caching"""
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    elif seconds < 3600:
        return f"{seconds/60:.1f} minutes"
    else:
        return f"{seconds/3600:.1f} hours"

async def update_progress(context, current, total):
    """Update progress for admin users with reduced API calls"""
    global progress_data
    
    # Update progress data
    progress_data["current"] = current
    progress_data["total"] = total
    
    # Only update UI at reasonable intervals (reduced frequency)
    now = time.time()
    if progress_data["last_update_time"] and now - progress_data["last_update_time"] < 5:  # Increased from 2s to 5s
        return  # Don't update too frequently
    
    progress_data["last_update_time"] = now
    
    # Calculate progress percentage
    percent = int((current / total) * 100) if total > 0 else 0
    
    # Only send updates on meaningful changes to reduce API calls
    if hasattr(update_progress, 'last_percent') and percent - update_progress.last_percent < 5:
        return  # Skip minor updates
    
    update_progress.last_percent = percent
    
    # Calculate ETA
    eta_text = "Calculating..."
    if progress_data["start_time"] and current > 0:
        elapsed = now - progress_data["start_time"]
        if percent > 0:
            total_time_estimate = elapsed * 100 / percent
            time_remaining = total_time_estimate - elapsed
            eta = datetime.now() + timedelta(seconds=time_remaining)
            eta_text = eta.strftime("%H:%M:%S")
    
    # Create progress bar with better visuals
    bar_length = 10
    filled_length = int(bar_length * percent / 100)
    bar = '█' * filled_length + '▒' * (bar_length - filled_length)
    
    # Format message with better styling
    message = (
        f"🔍 *PASSWORD SEARCH PROGRESS*\n\n"
        f"*Progress:* {percent}%\n"
        f"|{bar}| \n\n"
        f"*Passwords Tested:* {current:,}/{total:,}\n"
        f"*Time Elapsed:* {format_elapsed_time(now - progress_data['start_time'])}\n"
        f"*ETA:* {eta_text}\n\n"
        f"_Search is running in background..._"
    )
    
    # Send/update progress messages using a more efficient approach
    # Only update a subset of messages if we have many admins
    update_count = 0
    for chat_id in ADMIN_CHAT_IDS:
        # Limit updates to 3 admins max per cycle to reduce API load
        if update_count >= 3:
            break
            
        try:
            # If we have a message ID for this chat, update it
            if chat_id in progress_message_ids:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_message_ids[chat_id],
                        text=message,
                        parse_mode='Markdown'
                    )
                    update_count += 1
                except TelegramError:
                    # If editing fails (message too old), send new one
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode='Markdown'
                    )
                    progress_message_ids[chat_id] = msg.message_id
                    update_count += 1
            else:
                # Otherwise send a new message
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='Markdown'
                )
                progress_message_ids[chat_id] = msg.message_id
                update_count += 1
        except Exception as e:
            logger.error(f"Error updating progress for {chat_id}: {e}")

# Add more clean shutdown logic
def shutdown():
    """Clean shutdown of resources"""
    logger.info("Bot shutting down, performing cleanup...")
    
    # Save any cached data to disk
    if password_cache:
        try:
            with open('last_password.json', 'w') as f:
                json.dump(password_cache, f, indent=2)
            logger.info("Password cache saved to disk")
        except Exception as e:
            logger.error(f"Error saving password cache during shutdown: {e}")
    
    # Close process pool
    if 'process_pool' in globals():
        process_pool.shutdown(wait=False)
        logger.info("Process pool shut down")
    
    logger.info("Cleanup complete")

# Register shutdown handler
atexit.register(shutdown)

# Update the notification worker with better timeout handling
async def notification_worker(app):
    """Process notifications in the background with improved reliability"""
    while True:
        try:
            # Get item from queue
            chat_id, message, keyboard, parse_mode = await notification_queue.get()
            
            try:
                # Set a timeout for sending messages
                async with asyncio.timeout(5.0):  # 5 second timeout
                    if keyboard:
                        await app.bot.send_message(
                            chat_id=chat_id, 
                            text=message, 
                            reply_markup=keyboard,
                            parse_mode=parse_mode
                        )
                    else:
                        await app.bot.send_message(
                            chat_id=chat_id, 
                            text=message,
                            parse_mode=parse_mode
                        )
                    logger.debug(f"Notification sent to {chat_id}")
            except asyncio.TimeoutError:
                logger.warning(f"Notification to {chat_id} timed out, will retry once")
                # Try once more with a longer timeout
                try:
                    async with asyncio.timeout(10.0):  # 10 second timeout for retry
                        if keyboard:
                            await app.bot.send_message(
                                chat_id=chat_id, 
                                text=message, 
                                reply_markup=keyboard,
                                parse_mode=parse_mode
                            )
                        else:
                            await app.bot.send_message(
                                chat_id=chat_id, 
                                text=message,
                                parse_mode=parse_mode
                            )
                except Exception as e:
                    logger.error(f"Failed to send notification to {chat_id} after retry: {e}")
            except Exception as e:
                logger.error(f"Failed to send notification to {chat_id}: {e}")
            
            # Allow some time between messages to avoid rate limits
            await asyncio.sleep(0.5)
            notification_queue.task_done()
        except Exception as e:
            logger.error(f"Error in notification worker: {e}")
            await asyncio.sleep(1)  # Prevent tight loop on error

# Update main function to handle missing job queue
def main():
    """Start the bot with optimized configuration"""
    global notification_queue
    
    # Load configuration
    bot_token = load_config()
    if not bot_token:
        logger.error("Bot token not found in config.json")
        return
    
    # Create the Application with compatible optimized settings
    application = (
        Application.builder()
        .token(bot_token)
        .concurrent_updates(True)  # This is still valid
        .build()
    )
    
    # Initialize the notification queue
    notification_queue = asyncio.Queue()
    
    # Save application reference for scheduled jobs
    schedule_job.app = application
    
    # Record bot start time
    with open('bot_start_time.txt', 'w') as f:
        f.write(str(time.time()))
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Add callback query handler for buttons
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Load last password info
    load_password_info()
    ensure_valid_password()
    
    # Schedule password check
    schedule_password_check()
    
    # Try to set up job queue if available
    job_queue = application.job_queue
    if job_queue is not None:
        # Schedule hourly password verification
        job_queue.run_repeating(
            scheduled_password_verification, 
            interval=3600,  # 1 hour in seconds
            first=60  # Run first check after 60 seconds
        )
        logger.info("Hourly password verification scheduled")
    else:
        logger.warning("Job queue not available. To enable scheduled verification, install 'python-telegram-bot[job-queue]'")
        logger.warning("Run: pip install 'python-telegram-bot[job-queue]'")
    
    # Start notification worker
    asyncio.get_event_loop().create_task(notification_worker(application))
    
    # Set up error handler
    application.add_error_handler(error_handler)
    
    # Start the Bot
    application.run_polling(drop_pending_updates=True)
    
    logger.info("Bot started")

async def error_handler(update, context):
    """Handle errors gracefully"""
    logger.error(f"Update {update} caused error: {context.error}")
    
    # Only notify admins about critical errors
    if isinstance(context.error, (ConnectionError, TimeoutError)):
        for chat_id in ADMIN_CHAT_IDS:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ *Bot Error*\n\nA connection error occurred. The bot will attempt to recover automatically.",
                    parse_mode='Markdown'
                )
            except:
                pass

def get_status_keyboard():
    """Create an inline keyboard for the status message"""
    keyboard = [
        [
            InlineKeyboardButton("Start Test", callback_data='start_test'),
            InlineKeyboardButton("Copy Password", callback_data='copy_password')
        ],
        [
            InlineKeyboardButton("Verify Password", callback_data='verify_password'),
            InlineKeyboardButton("Test Status", callback_data='test_status') 
        ],
        [
            InlineKeyboardButton("Schedule Info", callback_data='schedule_info'),
            InlineKeyboardButton("Analytics", callback_data='show_analytics')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def password_callback(success, password, attempts, total_time, current, total):
    """Callback for password tester that safely bridges to asyncio"""
    global is_test_running, last_password, last_test_status
    
    # Log the result
    logger.info(f"Password test completed: success={success}, password={password}, attempts={attempts}")
    
    # Update the global state
    is_test_running = False
    last_test_status["end_time"] = time.time()
    last_test_status["found"] = success
    
    # If a password was found, save it
    if success and password:
        last_password = {
            "value": password,
            "timestamp": datetime.now().isoformat(),
            "attempts": attempts,
            "total_time": total_time
        }
        save_password_info(last_password)
    
    # Schedule the notification in the main event loop
    try:
        # Get reference to the main event loop
        loop = asyncio.get_event_loop_policy().get_event_loop()
        
        # If we're in a different thread, use run_coroutine_threadsafe
        if threading.current_thread() is not threading.main_thread():
            # Use run_coroutine_threadsafe to bridge to the main event loop
            asyncio.run_coroutine_threadsafe(
                notify_after_test(success, password, attempts, total_time),
                loop
            )
        else:
            # If we're in the main thread, we can create a task directly
            asyncio.create_task(notify_after_test(success, password, attempts, total_time))
            
    except Exception as e:
        logger.error(f"Error in password callback: {e}")

async def start_password_test(context, scheduled=False):
    """Start the password testing process"""
    global is_test_running, last_test_status, progress_data, progress_message_ids
    
    if is_test_running:
        if not scheduled:
            await notify_admins(context, "⚠️ A password test is already running!")
        return False
    
    # Reset progress tracking
    progress_data = {
        "current": 0,
        "total": 0,
        "start_time": time.time(),
        "last_update_time": None
    }
    progress_message_ids = {}
    
    is_test_running = True
    last_test_status["start_time"] = time.time()
    last_test_status["found"] = False
    
    if not scheduled:
        message = await notify_admins(context, "🚀 Starting password test...\nThis may take some time. You'll be notified when it completes.")
        # Save the message ID for updating progress
        if message:
            last_test_status["message_id"] = message.message_id
    
    # Pass app reference to callback
    password_callback.app = context
    
    # Start the password test in a separate thread
    test_thread = threading.Thread(
        target=password_tester.run_password_test,
        args=(password_callback,)
    )
    test_thread.daemon = True
    test_thread.start()
    
    return True

def schedule_password_check():
    """Schedule a password check every 5 hours"""
    schedule.every(6).hours.do(lambda: schedule_job())
    
    # Start a background thread for the scheduler
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()

def run_scheduler():
    """Run the scheduler in the background"""
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute for scheduled jobs

async def schedule_job():
    """Function called by the scheduler every 5 hours"""
    logger.info("Running scheduled password check")
    
    # Get the bot from the application
    if not hasattr(schedule_job, 'app'):
        logger.error("Application not set for scheduled job")
        return
    
    app = schedule_job.app
    
    # First verify if current password still works
    is_valid = await verify_current_password(app, notify=True)
    
    # If password is not valid, start a new password test
    if not is_valid:
        await notify_admins(app, "🔍 Starting automatic password search...")
        await start_password_test(app, scheduled=True)

async def verify_current_password(context, notify=True):
    """Verify if the current password is still valid and update status"""
    global last_password
    
    if not last_password.get("value"):
        logger.warning("No password to verify")
        return False
    
    logger.info(f"Verifying current password: {last_password['value']}")
    
    # Test the current password
    is_valid = password_tester.verify_password(last_password["value"])
    
    # Update validity status and verification timestamp
    last_password["valid"] = is_valid
    last_password["last_verified"] = datetime.now().isoformat()
    
    # Save the updated password info
    save_password_info(last_password)
    
    if notify:
        status = "valid" if is_valid else "invalid"
        logger.info(f"Password verification result: {status}")
        
        if is_valid:
            message = (
                "✅ *Password Verification*\n\n"
                f"The password `{last_password['value']}` is still valid."
            )
        else:
            message = (
                "❌ *Password Verification*\n\n"
                f"The password `{last_password['value']}` is no longer valid.\n\n"
                "Consider starting a new password test."
            )
        
        await notify_admins(context, message, parse_mode='Markdown')
    
    return is_valid

async def notify_after_test(success, password, attempts, total_time):
    """Send notification after test completes"""
    message = ""
    if success:
        message = (
            f"✅ *Password Test Completed Successfully*\n\n"
            f"Password: `{password}`\n"
            f"Found after {attempts:,} attempts\n"
            f"Total time: {format_elapsed_time(total_time)}\n\n"
            f"Password has been saved."
        )
    else:
        message = (
            f"❌ *Password Test Completed*\n\n"
            f"No password found after {attempts:,} attempts\n"
            f"Total time: {format_elapsed_time(total_time)}"
        )
    
    app = schedule_job.app
    await notify_admins(app, message, parse_mode='Markdown')
    
    # Update any progress messages to show completion
    for chat_id in progress_message_ids:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_ids[chat_id],
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to update progress message: {e}")

# Improved keyboard with better organization for admins
def get_admin_keyboard():
    """Create a well-balanced inline keyboard for admin users"""
    keyboard = [
        [
            InlineKeyboardButton("🔑 View Password", callback_data='copy_password')
        ],
        [
            InlineKeyboardButton("🚀 Start Test", callback_data='start_test'),
            InlineKeyboardButton("🔍 Verify Password", callback_data='verify_password')
        ],
        [
            InlineKeyboardButton("📊 Analytics", callback_data='show_analytics'),
            InlineKeyboardButton("⏱️ Status", callback_data='test_status') 
        ],
        [
            InlineKeyboardButton("📅 Schedule Info", callback_data='schedule_info'),
            InlineKeyboardButton("◀️ Back", callback_data='back_to_main')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Enhanced public keyboard with icons
def get_public_keyboard():
    """Create a well-balanced inline keyboard for public users"""
    keyboard = [
        [
            InlineKeyboardButton("🔑 View Password", callback_data='public_password')
        ],
        [
            InlineKeyboardButton("📊 Check Status", callback_data='public_status')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Updated status command with enhanced styling
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the current status with enhanced styling"""
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_CHAT_IDS
    
    # Load password info
    load_password_info()
    ensure_valid_password()
    
    # Create status message with better formatting
    header = "🔐 *PASSWORD STATUS* 🔐\n\n"
    
    if is_test_running:
        elapsed = time.time() - last_test_status["start_time"]
        status = (
            f"{header}"
            f"*Current Status:* 🟢 Test Running\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"▶️ Started: {get_readable_time(last_test_status['start_time'])}\n"
            f"⏱️ Running for: {format_elapsed_time(elapsed)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        )
    else:
        if last_test_status["end_time"]:
            result_icon = "✅" if last_test_status["found"] else "❌"
            status = (
                f"{header}"
                f"*Current Status:* 🔴 No Test Running\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ Last test: {get_readable_time(last_test_status['end_time'])}\n"
                f"🏁 Result: {result_icon} {' Password found' if last_test_status['found'] else ' Not found'}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
            )
        else:
            status = (
                f"{header}"
                f"*Current Status:* 🔴 No Test Running\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ℹ️ No previous test data available\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
            )
    
    if last_password["value"]:
        status += (
            f"*Current Password:*\n\n"
            f"```\n{last_password['value']}\n```\n\n"
        )
        
    # Add additional details for admins with better formatting
    if is_admin and last_password.get("timestamp"):
        status += (
            f"*Password Details:*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Found on: {last_password['timestamp'].split('T')[0]}\n"
            f"🔢 Attempts: {last_password.get('attempts', 'unknown'):,}\n"
            f"⏱️ Search time: {format_elapsed_time(last_password.get('total_time', 0))}\n"
        )
    
    # Use appropriate keyboard based on user type
    keyboard = get_admin_keyboard() if is_admin else get_public_keyboard()
    
    await update.message.reply_text(
        status,
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

# Enhanced password display for copy_password and public_password
async def display_password_message(password, is_admin=False):
    """Create a beautifully formatted password display message with validity status"""
    # Check validity status
    is_valid = last_password.get("valid", True)
    validity_text = "✅ VALID" if is_valid else "❌ EXPIRED"
    
    # Add verification time info
    verification_info = ""
    if last_password.get("last_verified"):
        try:
            verify_time = datetime.fromisoformat(last_password["last_verified"])
            verify_time_str = verify_time.strftime("%Y-%m-%d %H:%M")
            verification_info = f"Last verified: {verify_time_str}"
        except:
            verification_info = "Last verified: Unknown"
    
    message = (
        "🔐 *PASSWORD INFORMATION* 🔐\n\n"
        f"*Status: {validity_text}*\n"
        f"🔑 *Current Password:*\n\n"
        f"```\n{password}\n```\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Copy Instructions:*\n"
        f"• Tap on the code block above\n"
        f"• The password will be selected\n"
        f"• Use copy option from your device\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    if verification_info:
        message += f"ℹ️ {verification_info}\n"
        
    if is_admin:
        message += (
            "ℹ️ *Admin Options*\n"
            "Use the buttons below to manage the password"
        )
    else:
        message += (
            "ℹ️ To check password status, use the Status button below"
        )
    
    return message

# Update button callback to use enhanced display
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses with complete handlers"""
    query = update.callback_query
    user_id = query.from_user.id
    is_admin = user_id in ADMIN_CHAT_IDS
    
    try:
        # Public buttons accessible to everyone
        if query.data.startswith('public_'):
            await handle_public_buttons(query, context)
            return
        
        # Admin-only buttons
        if not is_admin:
            await query.answer("⚠️ Admin access required for this feature")
            return
        
        # Handle different admin button actions
        if query.data == 'start_test':
            await query.answer("Starting password test...")
            if await start_password_test(context):
                await query.edit_message_text(
                    "🚀 *Password Test Started*\n\n"
                    "🔹 The test is now running in the background\n"
                    "🔹 You'll be notified when it completes\n"
                    "🔹 You can check status anytime with the Status button",
                    reply_markup=get_admin_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(
                    "⚠️ *Test Already Running*\n\n"
                    "A password test is already in progress.\n"
                    "Please wait for it to complete before starting a new one.",
                    reply_markup=get_admin_keyboard(),
                    parse_mode='Markdown'
                )
                
        elif query.data == 'verify_password':
            await query.answer("Verifying current password...")
            await query.edit_message_text(
                "🔍 *Verifying Password*\n\n"
                "Checking if the current password is still valid...\n"
                "Please wait a moment.",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            
            # Safely perform verification and report result
            try:
                is_valid = await verify_current_password(context, notify=False)
                if is_valid:
                    await query.edit_message_text(
                        "✅ *Password Verified*\n\n"
                        f"The password `{last_password.get('value', 'Unknown')}` is still valid.\n\n"
                        "No action needed at this time.",
                        reply_markup=get_admin_keyboard(),
                        parse_mode='Markdown'
                    )
                else:
                    # Ask if the admin wants to start a new search
                    keyboard = [
                        [
                            InlineKeyboardButton("🚀 Start Search", callback_data='start_test'),
                            InlineKeyboardButton("◀️ Back", callback_data='back_to_main')
                        ]
                    ]
                    await query.edit_message_text(
                        "❌ *Password Invalid*\n\n"
                        f"The password `{last_password.get('value', 'Unknown')}` is no longer valid.\n\n"
                        "Would you like to start a search for a new password?",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
            except Exception as e:
                logger.error(f"Error verifying password: {e}")
                await query.edit_message_text(
                    "⚠️ *Verification Error*\n\n"
                    "An error occurred while verifying the password.\n"
                    "Please try again later.",
                    reply_markup=get_admin_keyboard(),
                    parse_mode='Markdown'
                )
                
        elif query.data == 'copy_password':
            if last_password.get("value"):
                await query.answer("Password ready to copy")
                message = await display_password_message(last_password["value"], is_admin=True)
                await query.edit_message_text(
                    message,
                    reply_markup=get_admin_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await query.answer("No password found yet")
        
        # Handle remaining admin buttons
        elif query.data == 'show_analytics':
            await query.answer("Generating analytics...")
            analytics = await generate_analytics(context)
            await query.edit_message_text(
                analytics,
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            
        elif query.data == 'test_status':
            await query.answer("Fetching status...")
            status_message = await create_status_message(is_admin)
            await query.edit_message_text(
                status_message,
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            
        elif query.data == 'schedule_info':
            await query.answer("Fetching schedule info...")
            schedule_message = (
                "📅 *Scheduled Password Checks*\n\n"
                f"Password verification happens every {CONFIG.get('check_interval_hours', 5)} hours.\n\n"
                "If the password becomes invalid, a new search will start automatically.\n\n"
                f"Last check: {get_readable_time(last_test_status.get('end_time'))}\n"
                f"Next check: ~{get_next_check_time()}"
            )
            await query.edit_message_text(
                schedule_message,
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            
        elif query.data == 'back_to_main':
            await query.answer("Returning to main menu...")
            guide_message = (
                "🔐 *PASSWORD MANAGER BOT* 🔐\n\n"
                "Use the buttons below to manage passwords."
            )
            await query.edit_message_text(
                guide_message,
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            await query.answer("No changes needed")
        else:
            logger.error(f"BadRequest error in button_callback: {e}")
            await query.answer("An error occurred")
    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        await query.answer("An error occurred")

# Update the public buttons handler with enhanced UI
async def handle_public_buttons(query, context):
    """Handle button presses from public users with enhanced UI"""
    try:
        if query.data == 'public_status':
            await query.answer("Fetching current status...")
            
            status_header = "📊 *PASSWORD STATUS* 📊\n\n"
            
            if is_test_running:
                elapsed = time.time() - last_test_status["start_time"]
                status = (
                    f"{status_header}"
                    f"🟢 *Test in Progress*\n"
                    f"Started {format_elapsed_time(elapsed)} ago\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
            else:
                status = (
                    f"{status_header}"
                    f"🔴 *No Test Running*\n"
                    f"System is idle\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
            
            # Always check if we have a password
            ensure_valid_password()
            if last_password["value"]:
                status += (
                    f"🔑 *Current Password:*\n"
                    f"`{last_password['value']}`\n\n"
                    f"Use the View Password button for a copyable version"
                )
            else:
                status += "⚠️ No password currently available"
            
            await query.edit_message_text(
                status,
                reply_markup=get_public_keyboard(),
                parse_mode='Markdown'
            )
        
        elif query.data == 'public_password':
            ensure_valid_password()  # Make sure we have valid password data
            if last_password["value"]:
                await query.answer("Password ready to copy")
                message = await display_password_message(last_password["value"])
                await query.edit_message_text(
                    message,
                    reply_markup=get_public_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await query.answer("⚠️ No password available")
                await query.edit_message_text(
                    "⚠️ *No Password Available*\n\n"
                    "There is currently no password in the system.\n"
                    "Please check back later or contact an administrator.",
                    reply_markup=get_public_keyboard(),
                    parse_mode='Markdown'
                )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            await query.answer("No changes needed")
        else:
            logger.error(f"BadRequest error in handle_public_buttons: {e}")
            await query.answer("An error occurred")
    except Exception as e:
        logger.error(f"Error in handle_public_buttons: {e}")
        await query.answer("An error occurred")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a help message when the command /help is issued."""
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_CHAT_IDS
    
    help_text = (
        "🤖 *Password Test Bot Help*\n\n"
        "*Available Commands:*\n"
        "/start - Start the bot and show main menu\n"
        "/status - Check current test status and password\n"
        "/help - Show this help message\n\n"
    )
    
    if is_admin:
        help_text += (
            "*Admin-Only Features:*\n"
            "• Start new password tests\n"
            "• View detailed test statistics\n"
            "• Configure scheduled checks\n"
        )
    else:
        help_text += (
            "*Public Features:*\n"
            "• View current password\n"
            "• Check if a test is running\n"
        )
    
    await update.message.reply_text(
        help_text,
        parse_mode='Markdown'
    )

# Add this function to make sure there's always a valid default password
def ensure_valid_password():
    """Ensure there's a valid password structure, even if empty"""
    global last_password
    
    # If the last password doesn't have a value, try to load it
    if not last_password.get("value"):
        load_password_info()
    
    # Set a default "not found" password if nothing was loaded
    if not last_password.get("value"):
        # Check if there's a valid success_details.json or other password file
        try:
            if os.path.exists('success_details.json'):
                with open('success_details.json', 'r') as f:
                    success_data = json.load(f)
                    if success_data.get("password"):
                        last_password = {
                            "value": success_data["password"],
                            "timestamp": datetime.now().isoformat(),
                            "attempts": 0,
                            "total_time": 0
                        }
                        save_password_info(last_password)
                        logger.info(f"Loaded password from success_details.json: {last_password['value']}")
        except Exception as e:
            logger.error(f"Error checking alternate password files: {e}")

# Add an analytics function
async def generate_analytics(context):
    """Generate analytics about password testing"""
    # Get stats from log files
    stats = {
        "total_tests": 0,
        "success_rate": 0,
        "avg_time": 0,
        "last_updated": "Never",
        "uptime": 0
    }
    
    try:
        # Count log files to estimate number of tests
        log_files = [f for f in os.listdir('logs') if f.startswith('password_test_')]
        stats["total_tests"] = len(log_files)
        
        # Get last updated time
        if last_password["timestamp"]:
            stats["last_updated"] = last_password["timestamp"].split('T')[0]
        
        # Estimate success rate
        success_files = [f for f in os.listdir('logs') if "success" in f]
        if stats["total_tests"] > 0:
            stats["success_rate"] = len(success_files) / stats["total_tests"] * 100
        
        # Calculate uptime since last restart
        if os.path.exists('bot_start_time.txt'):
            with open('bot_start_time.txt', 'r') as f:
                start_time = float(f.read().strip())
                stats["uptime"] = time.time() - start_time
    except Exception as e:
        logger.error(f"Error generating analytics: {e}")
    
    # Format the analytics text
    analytics = (
        "📊 *Password Bot Analytics*\n\n"
        f"*Current Password:* `{last_password.get('value', 'None')}`\n"
        f"*Last Updated:* {stats['last_updated']}\n"
        f"*Total Password Tests:* {stats['total_tests']}\n"
        f"*Success Rate:* {stats['success_rate']:.1f}%\n"
        f"*Bot Uptime:* {format_elapsed_time(stats['uptime'])}\n\n"
        f"_Stats based on available logs_"
    )
    
    return analytics

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a guide message when the command /start is issued."""
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_CHAT_IDS
    
    # Different guide messages for admin vs. regular users
    if is_admin:
        guide_message = (
            "🔐 *PASSWORD MANAGER BOT* 🔐\n\n"
            "📋 *ADMIN GUIDE*\n\n"
            "This bot helps you manage and automate password testing.\n\n"
            "🔹 *Available Actions:*\n"
            "• Start a new password test\n"
            "• Copy the current password\n"
            "• Check if the password is still valid\n"
            "• View test status and analytics\n"
            "• Configure scheduled checks\n\n"
            "🔹 *Commands:*\n"
            "• /start - Show this guide\n"
            "• /status - Check current password\n"
            "• /help - Show detailed help\n\n"
            "Use the buttons below to manage your passwords."
        )
        keyboard = get_admin_keyboard()
    else:
        guide_message = (
            "🔑 *AMR PASSWORD ASSISTANT* 🔑\n\n"
            "👋 *Welcome!*\n\n"
            "This bot provides easy access to the website password.\n\n"
            "🔹 *What You Can Do:*\n"
            "• View the current website password\n"
            "• Check if the password is being updated\n\n"
            "🔹 *Simple Commands:*\n"
            "• /start - Show this welcome screen\n"
            "• /status - Check password status\n"
            "• /help - Get help information\n\n"
            "Just tap the buttons below to get started!"
        )
        keyboard = get_public_keyboard()
    
    # Send the guide message
    await update.message.reply_text(
        guide_message,
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

def get_next_check_time():
    """Get formatted time for next scheduled check"""
    interval = CONFIG.get('check_interval_hours', 5)
    now = time.time()
    last_check = last_test_status.get('end_time', now - (interval * 3600))
    next_check = last_check + (interval * 3600)
    
    if next_check < now:
        return "Due now"
    
    return get_readable_time(next_check)

async def create_status_message(is_admin=False):
    """Create a status message with validity information"""
    header = "🔐 *PASSWORD STATUS* 🔐\n\n"
    
    if is_test_running:
        elapsed = time.time() - last_test_status["start_time"]
        status = (
            f"{header}"
            f"*Current Status:* 🟢 Test Running\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"▶️ Started: {get_readable_time(last_test_status['start_time'])}\n"
            f"⏱️ Running for: {format_elapsed_time(elapsed)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        )
    else:
        if last_test_status["end_time"]:
            result_icon = "✅" if last_test_status["found"] else "❌"
            status = (
                f"{header}"
                f"*Current Status:* 🔴 No Test Running\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ Last test: {get_readable_time(last_test_status['end_time'])}\n"
                f"🏁 Result: {result_icon} {' Password found' if last_test_status['found'] else ' Not found'}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
            )
        else:
            status = (
                f"{header}"
                f"*Current Status:* 🔴 No Test Running\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ℹ️ No previous test data available\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
            )
    
    ensure_valid_password()
    if last_password["value"]:
        # Add validity status
        is_valid = last_password.get("valid", True)
        validity_text = "✅ VALID" if is_valid else "❌ EXPIRED"
        
        status += (
            f"*Current Password:*\n\n"
            f"```\n{last_password['value']}\n```\n\n"
            f"*Status: {validity_text}*\n"
        )
        
        # Add verification time
        if last_password.get("last_verified"):
            try:
                verify_time = datetime.fromisoformat(last_password["last_verified"])
                verify_time_str = verify_time.strftime("%Y-%m-%d %H:%M")
                status += f"Last verified: {verify_time_str}\n"
            except:
                pass
    
    # Add additional details for admins
    if is_admin and last_password.get("timestamp"):
        status += (
            f"*Password Details:*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Found on: {last_password['timestamp'].split('T')[0]}\n"
            f"🔢 Attempts: {last_password.get('attempts', 'unknown'):,}\n"
            f"⏱️ Search time: {format_elapsed_time(last_password.get('total_time', 0))}\n"
        )
    
    return status

# Add this function for scheduled validation
async def scheduled_password_verification(context):
    """Verify the current password hourly and mark as expired if invalid"""
    logger.info("Running scheduled password verification")
    
    # Skip if no password or test is running
    if not last_password.get("value") or is_test_running:
        logger.info("Skipping verification: No password available or test running")
        return
    
    # Verify current password
    is_valid = await verify_current_password(context, notify=False)
    
    # Update validity status and verification timestamp
    last_password["valid"] = is_valid
    last_password["last_verified"] = datetime.now().isoformat()
    
    # Save updated status
    save_password_info(last_password)
    
    # Only notify if password is invalid
    if not is_valid:
        # Notify admins about expired password
        await notify_admins(
            context,
            "⚠️ *PASSWORD EXPIRED* ⚠️\n\n"
            f"The password `{last_password['value']}` is no longer valid.\n\n"
            "Use the Verify Password button to confirm, or start a new password test.",
            parse_mode='Markdown'
        )
    else:
        logger.info(f"Password verification successful: {last_password['value']}")

if __name__ == '__main__':
    main() 
