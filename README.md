# English Dictionary Bot for Vasilina

Telegram bot that helps with English vocabulary learning:
- Generates visual images for words/phrases
- Provides B2-level definitions
- Gives Russian translations
- Weekly review system with Google Sheets

## Environment Variables Required

- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token
- `DASHSCOPE_API_KEY` - Alibaba Dashscope API key
- `GOOGLE_CREDENTIALS_JSON` - Service account JSON (as string)
- `GOOGLE_SHEET_ID` - Google Sheet ID for storage
- `CHAT_ID` - Vasilina's Telegram chat ID

## Google Sheets Setup

Create a Google Sheet with 2 sheets:

**Sheet1** (Pending Words):
```
Definition | Russian | Word/Phrase | Timestamp | Week#
```

**Sheet2** (Saved Words):
```
Definition | Russian | Word/Phrase | Timestamp | Week#
```

## Deployment

1. Push to GitHub
2. Connect to Railway
3. Add environment variables
4. Deploy automatically
