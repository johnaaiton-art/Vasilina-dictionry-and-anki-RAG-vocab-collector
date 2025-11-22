#!/usr/bin/env python3
"""
English Dictionary Bot for Vasilina
- Generates images, definitions, and translations for English words/phrases
- Saves to Google Sheets with weekly review system
"""

import os
import json
import uuid
import logging
import asyncio
import requests
import tempfile
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import pytz
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import dashscope
from dashscope import Generation, ImageSynthesis

# ------------------ CONFIG ------------------
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DASHSCOPE_API_KEY = os.getenv('DASHSCOPE_API_KEY')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
CHAT_ID = int(os.getenv('CHAT_ID'))  # Vasilina's chat ID

if not all([TELEGRAM_BOT_TOKEN, DASHSCOPE_API_KEY, GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID, CHAT_ID]):
    raise EnvironmentError("Missing required environment variables")

# Dashscope setup
dashscope.api_key = DASHSCOPE_API_KEY
dashscope.base_http_api_url = 'https://dashscope-intl.aliyuncs.com/api/v1'

MODEL_IMAGE = 'wan2.2-t2i-flash'
SIZE = '1024*1024'
MODEL_LLM = 'qwen-plus'

TEMP_DIR = tempfile.mkdtemp()
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Google Sheets setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('dict_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------ GOOGLE SHEETS CLIENT ------------------
def get_sheets_client():
    """Initialize Google Sheets client"""
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        return gspread.authorize(credentials)
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets client: {e}")
        raise

def get_week_number() -> int:
    """Get ISO week number"""
    return datetime.now(MOSCOW_TZ).isocalendar()[1]

def get_moscow_time() -> datetime:
    """Get current Moscow time"""
    return datetime.now(MOSCOW_TZ)

# ------------------ LLM FUNCTIONS ------------------
async def generate_definition_and_translation(phrase: str) -> Dict[str, str]:
    """Generate learner-friendly definition and Russian translation using Qwen"""
    system_prompt = (
        "You are an English language teacher helping a B2 (upper-intermediate) Russian-speaking student. "
        "For the given English word or phrase, provide:\n"
        "1. A clear, learner-friendly definition in English (1-2 sentences, avoid overly complex vocabulary)\n"
        "2. An accurate Russian translation\n\n"
        "Format your response EXACTLY as:\n"
        "DEFINITION: [your definition here]\n"
        "RUSSIAN: [Russian translation here]\n\n"
        "Be concise and precise."
    )
    
    user_prompt = f"Word/phrase: {phrase}"
    
    try:
        response = Generation.call(
            model=MODEL_LLM,
            prompt=user_prompt,
            system=system_prompt,
            max_tokens=300,
            temperature=0.5
        )
        
        if response.status_code == 200:
            text = response.output['text'].strip()
            
            # Parse response
            definition = ""
            russian = ""
            
            for line in text.split('\n'):
                if line.startswith('DEFINITION:'):
                    definition = line.replace('DEFINITION:', '').strip()
                elif line.startswith('RUSSIAN:'):
                    russian = line.replace('RUSSIAN:', '').strip()
            
            if not definition or not russian:
                # Fallback parsing
                lines = [l.strip() for l in text.split('\n') if l.strip()]
                if len(lines) >= 2:
                    definition = lines[0]
                    russian = lines[1]
            
            return {
                'definition': definition or "A common English expression",
                'russian': russian or phrase
            }
        else:
            logger.error(f"Qwen error: {response.code} - {response.message}")
            return {
                'definition': f"Common English expression: {phrase}",
                'russian': phrase
            }
    except Exception as e:
        logger.error(f"Definition generation exception: {e}")
        return {
            'definition': f"English expression: {phrase}",
            'russian': phrase
        }

async def generate_image_prompt(phrase: str, definition: str) -> str:
    """Generate image prompt that visualizes the meaning"""
    system_prompt = (
        "You are creating image prompts for English language learners. "
        "Given an English phrase and its definition, create a visual scene that represents its meaning. "
        "The scene should:\n"
        "- Show the underlying concept or typical usage\n"
        "- Be clear and educational\n"
        "- Include people, animals, or animated objects when helpful\n"
        "- Be realistic and relatable\n\n"
        "Output ONLY the image prompt in English, no explanations. Keep it under 100 words."
    )
    
    user_prompt = f"Phrase: {phrase}\nDefinition: {definition}"
    
    try:
        response = Generation.call(
            model=MODEL_LLM,
            prompt=user_prompt,
            system=system_prompt,
            max_tokens=150,
            temperature=0.7
        )
        
        if response.status_code == 200:
            return response.output['text'].strip()
        else:
            return f"A scene showing the concept of '{phrase}', realistic style, clear composition"
    except Exception as e:
        logger.error(f"Image prompt generation exception: {e}")
        return f"Visual representation of '{phrase}', educational illustration"

# ------------------ IMAGE GENERATION ------------------
async def generate_image(prompt: str) -> Optional[str]:
    """Generate image using Dashscope"""
    try:
        resp = ImageSynthesis.async_call(
            model=MODEL_IMAGE,
            prompt=prompt,
            size=SIZE,
            n=1
        )
        
        if resp.status_code != 200:
            logger.error(f"Image API error: {resp.code} - {resp.message}")
            return None
        
        task_id = resp.output['task_id']
        logger.info(f"Image task created: {task_id}")
        
        max_wait = 180
        poll_interval = 4
        elapsed = 0
        
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            
            try:
                status_resp = ImageSynthesis.fetch(task_id)
            except Exception as e:
                logger.error(f"Status check exception: {e}")
                continue
            
            if status_resp.status_code != 200:
                continue
            
            task_status = status_resp.output.get('task_status', 'UNKNOWN')
            
            if task_status == 'SUCCEEDED':
                return status_resp.output['results'][0]['url']
            elif task_status == 'FAILED':
                logger.error(f"Image generation failed")
                return None
        
        logger.error(f"Image generation timed out")
        return None
        
    except Exception as e:
        logger.error(f"Image generation exception: {e}")
        return None

# ------------------ GOOGLE SHEETS OPERATIONS ------------------
def add_word_to_sheet(phrase: str, definition: str, russian: str):
    """Add word to Sheet 1 (pending words)"""
    try:
        client = get_sheets_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet1 = spreadsheet.worksheet('Sheet1')
        
        timestamp = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        week_num = get_week_number()
        
        # Append to Sheet 1: Definition | Russian | Phrase | Timestamp | Week#
        sheet1.append_row([definition, russian, phrase, timestamp, week_num])
        logger.info(f"Added '{phrase}' to Sheet1")
        
    except Exception as e:
        logger.error(f"Error adding to sheet: {e}")
        raise

def get_pending_words_for_current_week() -> List[Dict]:
    """Get all words from current week in Sheet 1"""
    try:
        client = get_sheets_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet1 = spreadsheet.worksheet('Sheet1')
        
        current_week = get_week_number()
        all_rows = sheet1.get_all_values()
        
        words = []
        for idx, row in enumerate(all_rows[1:], start=2):  # Skip header
            if len(row) >= 5:
                week_num = row[4]
                if str(week_num) == str(current_week):
                    words.append({
                        'row': idx,
                        'definition': row[0],
                        'russian': row[1],
                        'phrase': row[2],
                        'timestamp': row[3],
                        'week': row[4]
                    })
        
        logger.info(f"Found {len(words)} words for week {current_week}")
        return words
        
    except Exception as e:
        logger.error(f"Error getting pending words: {e}")
        return []

def move_word_to_sheet2(word_data: Dict):
    """Move selected word to Sheet 2"""
    try:
        client = get_sheets_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet2 = spreadsheet.worksheet('Sheet2')
        
        # Add to Sheet 2: Definition | Russian | Phrase | Timestamp | Week#
        sheet2.append_row([
            word_data['definition'],
            word_data['russian'],
            word_data['phrase'],
            word_data['timestamp'],
            word_data['week']
        ])
        
        logger.info(f"Moved '{word_data['phrase']}' to Sheet2")
        return True
        
    except Exception as e:
        logger.error(f"Error moving to Sheet2: {e}")
        return False

def check_if_words_added_today() -> bool:
    """Check if any words were added to Sheet 2 today"""
    try:
        client = get_sheets_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet2 = spreadsheet.worksheet('Sheet2')
        
        today = get_moscow_time().date()
        all_rows = sheet2.get_all_values()
        
        for row in all_rows[1:]:  # Skip header
            if len(row) >= 4:
                timestamp_str = row[3]
                try:
                    row_date = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S').date()
                    if row_date == today:
                        return True
                except:
                    continue
        
        return False
        
    except Exception as e:
        logger.error(f"Error checking Sheet2: {e}")
        return False

def check_if_words_added_since_sunday() -> bool:
    """Check if any words were added to Sheet 2 since Sunday"""
    try:
        client = get_sheets_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet2 = spreadsheet.worksheet('Sheet2')
        
        # Get last Sunday
        now = get_moscow_time()
        days_since_sunday = (now.weekday() + 1) % 7  # Monday=0, Sunday=6
        last_sunday = (now - timedelta(days=days_since_sunday)).date()
        
        all_rows = sheet2.get_all_values()
        
        for row in all_rows[1:]:  # Skip header
            if len(row) >= 4:
                timestamp_str = row[3]
                try:
                    row_date = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S').date()
                    if row_date >= last_sunday:
                        return True
                except:
                    continue
        
        return False
        
    except Exception as e:
        logger.error(f"Error checking Sheet2: {e}")
        return False

# ------------------ TELEGRAM HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    await update.message.reply_text(
        "📚 English Dictionary Bot\n\n"
        "Send me any English word or phrase (up to 7 words) and I'll:\n"
        "• Generate a visual image\n"
        "• Provide a learner-friendly definition\n"
        "• Give you a Russian translation\n"
        "• Save it for weekly review\n\n"
        "Every Sunday at 18:10, I'll send you all the week's words for review!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages"""
    # Only process messages from Vasilina's chat
    if update.effective_chat.id != CHAT_ID:
        return
    
    text = update.message.text.strip()
    
    # Ignore commands
    if text.startswith('/'):
        return
    
    # Only process if 7 words or fewer
    word_count = len(text.split())
    if word_count > 7:
        logger.info(f"Ignored message with {word_count} words (too long)")
        return
    
    logger.info(f"Processing phrase: {text}")
    
    try:
        # Step 1: Generate definition and translation
        await update.message.reply_text("🔍 Looking up definition and translation...")
        result = await generate_definition_and_translation(text)
        
        definition = result['definition']
        russian = result['russian']
        
        # Step 2: Generate image prompt
        await update.message.reply_text("🎨 Creating visual representation...")
        image_prompt = await generate_image_prompt(text, definition)
        
        # Step 3: Generate image
        img_url = await generate_image(image_prompt)
        
        # Step 4: Send response
        response_text = f"📖 **{text}**\n\n"
        response_text += f"**Definition:** {definition}\n\n"
        response_text += f"**Russian:** {russian}"
        
        if img_url:
            try:
                # Download and send image
                img_name = f"{uuid.uuid4().hex[:8]}.png"
                img_path = os.path.join(TEMP_DIR, img_name)
                
                with requests.get(img_url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(img_path, 'wb') as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                
                with open(img_path, 'rb') as photo:
                    await update.message.reply_photo(
                        photo=photo,
                        caption=response_text,
                        parse_mode='Markdown'
                    )
                
                os.remove(img_path)
            except Exception as e:
                logger.error(f"Error sending image: {e}")
                await update.message.reply_text(response_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(response_text, parse_mode='Markdown')
        
        # Step 5: Save to Google Sheets
        add_word_to_sheet(text, definition, russian)
        logger.info(f"Successfully processed: {text}")
        
    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        await update.message.reply_text("❌ Sorry, something went wrong. Please try again.")

async def send_weekly_review(context: ContextTypes.DEFAULT_TYPE):
    """Send weekly review buttons (Sunday 18:10)"""
    logger.info("Sending weekly review...")
    
    try:
        words = get_pending_words_for_current_week()
        
        if not words:
            logger.info("No words to review this week")
            return
        
        # Create inline keyboard with buttons
        keyboard = []
        for word in words:
            button_text = f"{word['phrase']}\n{word['russian']}"
            callback_data = f"select_{word['row']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"📚 **Weekly Review - Week {get_week_number()}**\n\n"
            f"Select the words/phrases you want to save for future study.\n"
            f"Click the buttons below:"
        )
        
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        logger.info(f"Sent review with {len(words)} words")
        
    except Exception as e:
        logger.error(f"Error sending weekly review: {e}", exc_info=True)

async def check_and_resend_review(context: ContextTypes.DEFAULT_TYPE):
    """Check if review was completed, resend if needed"""
    logger.info("Checking if review needs to be resent...")
    
    try:
        # Check if any words were added to Sheet 2 today
        if check_if_words_added_today():
            logger.info("Words already added today, not resending")
            return
        
        # Resend review
        await send_weekly_review(context)
        
    except Exception as e:
        logger.error(f"Error in check_and_resend: {e}", exc_info=True)

async def check_and_resend_review_monday(context: ContextTypes.DEFAULT_TYPE):
    """Final check on Monday, resend if needed"""
    logger.info("Monday check - resending if needed...")
    
    try:
        # Check if any words were added since Sunday
        if check_if_words_added_since_sunday():
            logger.info("Words already added since Sunday, not resending")
            return
        
        # Final resend
        await send_weekly_review(context)
        
    except Exception as e:
        logger.error(f"Error in Monday check: {e}", exc_info=True)

async def handle_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks for word selection"""
    query = update.callback_query
    await query.answer()
    
    try:
        callback_data = query.data
        
        if callback_data.startswith('select_'):
            row_num = int(callback_data.split('_')[1])
            
            # Get word data from Sheet 1
            client = get_sheets_client()
            spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
            sheet1 = spreadsheet.worksheet('Sheet1')
            
            row_data = sheet1.row_values(row_num)
            
            if len(row_data) >= 5:
                word_data = {
                    'definition': row_data[0],
                    'russian': row_data[1],
                    'phrase': row_data[2],
                    'timestamp': row_data[3],
                    'week': row_data[4]
                }
                
                # Move to Sheet 2
                if move_word_to_sheet2(word_data):
                    # Edit message to show checkmark
                    original_text = query.message.text
                    updated_text = original_text + f"\n\n✅ Saved: {word_data['phrase']}"
                    
                    try:
                        await query.edit_message_text(
                            text=updated_text,
                            reply_markup=query.message.reply_markup,
                            parse_mode='Markdown'
                        )
                    except:
                        # If edit fails, send confirmation message
                        await context.bot.send_message(
                            chat_id=CHAT_ID,
                            text=f"✅ Saved: {word_data['phrase']}"
                        )
                    
                    logger.info(f"User selected: {word_data['phrase']}")
        
    except Exception as e:
        logger.error(f"Error handling button callback: {e}", exc_info=True)

# ------------------ MAIN ------------------
def main():
    """Main function"""
    logger.info("🚀 Starting English Dictionary Bot...")
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_callback))
    
    # Setup scheduler
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    
    # Sunday 18:10 - Send weekly review
    scheduler.add_job(
        send_weekly_review,
        CronTrigger(day_of_week='sun', hour=18, minute=10, timezone=MOSCOW_TZ),
        args=[application],
        id='weekly_review',
        replace_existing=True
    )
    
    # Sunday 20:30 - Check and resend if needed
    scheduler.add_job(
        check_and_resend_review,
        CronTrigger(day_of_week='sun', hour=20, minute=30, timezone=MOSCOW_TZ),
        args=[application],
        id='resend_check_1',
        replace_existing=True
    )
    
    # Monday 18:00 - Final check and resend if needed
    scheduler.add_job(
        check_and_resend_review_monday,
        CronTrigger(day_of_week='mon', hour=18, minute=0, timezone=MOSCOW_TZ),
        args=[application],
        id='resend_check_2',
        replace_existing=True
    )
    
    scheduler.start()
    
    logger.info("✅ Bot started successfully!")
    logger.info("📅 Weekly review: Sunday 18:10 Moscow time")
    logger.info("🔄 Resend checks: Sunday 20:30, Monday 18:00")
    logger.info(f"💬 Monitoring chat ID: {CHAT_ID}")
    
    # Run bot
    application.run_polling()

if __name__ == '__main__':
    main()
