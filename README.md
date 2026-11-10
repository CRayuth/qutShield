# qutShield

qutShield is a Telegram bot that downloads public videos and photos from YouTube, TikTok, Instagram, Facebook, X, and Telegram. Supports short links, Reels, Stories, and album selection in private chats. Send up to five links per message; new requests wait in a per-user queue. `/stop` cancels the current download and clears the queue.

## Run

1. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`.
2. Install Python 3.12+ and FFmpeg, then run `pip install -r requirements.txt`.
3. Run `python -m bot` and send `/start`.

Optional cookie files help with login-gated posts. Private, deleted, or expired media may be unavailable.
