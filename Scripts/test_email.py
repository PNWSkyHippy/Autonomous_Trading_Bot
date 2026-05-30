"""
Test email configuration.
Run with: python test_email.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_RECIPIENT

print("=" * 50)
print("  EMAIL CONNECTION TEST")
print("=" * 50)
print(f"  From:     {EMAIL_ADDRESS}")
print(f"  To:       {EMAIL_RECIPIENT}")
print(f"  Password: {'*' * len(EMAIL_PASSWORD) if EMAIL_PASSWORD else 'NOT SET'}")
print()

if not EMAIL_ADDRESS or EMAIL_ADDRESS == "your@email.com":
    print("  [ERROR] EMAIL_ADDRESS not set in .env file")
    sys.exit(1)

if not EMAIL_PASSWORD or EMAIL_PASSWORD == "your_app_password":
    print("  [ERROR] EMAIL_PASSWORD not set in .env file")
    print()
    print("  Gmail requires an App Password, not your regular password.")
    print("  Create one at: myaccount.google.com/apppasswords")
    print("  It looks like: xxxx xxxx xxxx xxxx (16 characters)")
    sys.exit(1)

print("  Attempting to connect to Gmail...")
try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
        print("  [OK] Connected to Gmail SMTP server")
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        print("  [OK] Login successful")

        # Send a test email
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Trading Bot - Email Test Successful"
        msg["From"]    = EMAIL_ADDRESS
        msg["To"]      = EMAIL_RECIPIENT

        html = """
        <div style="font-family:Arial;padding:20px;background:#0f1117;color:#e0e0e0">
            <h2 style="color:#4fc3f7">Trading Bot Email Test</h2>
            <p>Your email notifications are working correctly.</p>
            <p>You will receive daily trading reports at 5:00 PM Eastern time
            on each trading day.</p>
            <p style="color:#4caf50">Setup complete!</p>
        </div>
        """
        msg.attach(MIMEText(html, "html"))
        server.sendmail(EMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())
        print(f"  [OK] Test email sent to {EMAIL_RECIPIENT}")
        print()
        print("  Check your inbox -- you should receive it within a minute.")
        print("  Check spam folder if you don't see it.")

except smtplib.SMTPAuthenticationError:
    print("  [ERROR] Authentication failed")
    print()
    print("  This almost always means you used your regular Gmail")
    print("  password instead of an App Password.")
    print()
    print("  To fix:")
    print("  1. Go to myaccount.google.com/apppasswords")
    print("  2. Sign in and create a new App Password")
    print("  3. Select 'Mail' and 'Windows Computer'")
    print("  4. Copy the 16-character password it generates")
    print("  5. Paste it into your .env as EMAIL_PASSWORD=abcdabcdabcdabcd")
    print("     (no spaces in the password)")

except smtplib.SMTPConnectError:
    print("  [ERROR] Could not connect to Gmail")
    print("  Check your internet connection or firewall settings")

except Exception as e:
    print(f"  [ERROR] {e}")

print("=" * 50)
