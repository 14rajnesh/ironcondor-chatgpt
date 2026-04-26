# Weekly NIFTY Iron Condor Paper Bot

Paper-only bot using FYERS market data, Telegram alerts, Supabase state, and GitHub Actions scheduling.

## Files

- `weekly_iron_condor_bot.py` - main bot
- `.github/workflows/weekly_iron_condor.yml` - GitHub Actions workflow
- `supabase_schema.sql` - run once in Supabase SQL Editor
- `requirements.txt` - Python dependencies

## Required GitHub Secrets

- `FYERS_CLIENT_ID`
- `FYERS_ACCESS_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SUPABASE_URL`
- `SUPABASE_KEY`

## Behavior

- Enters one weekly NIFTY paper iron condor when no active trade exists and DTE is within `ENTRY_DTE_MIN` to `ENTRY_DTE_MAX`.
- Uses FYERS option chain symbols directly instead of manually creating expiry symbols.
- Selects liquid strikes using bid/ask, volume, spread, and delta when available.
- Does not send real orders.
- On every scheduled run, sends MTM to Telegram.
- Adjusts by paper-closing old condor and paper-opening a fresh condor on the same expiry when a short strike is tested or NIFTY moves strongly.
- Settles on expiry day after configured time.
