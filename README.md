# Telegram ZIP Video Channel Bot

## What it does
- Receives `.zip` files in a private chat from the configured owner.
- Accepts ZIP archives up to 4 GiB (subject to Telegram's own upload allowance).
- Extracts video files recursively.
- Uses Telegram Local Bot API for large file transfers (up to 2000 MB per upload).
- Sends videos with `sendVideo`. Non-MP4/non-H.264 videos are converted to MP4/H.264 first.
- Stores job/video state in SQLite so unfinished jobs can resume after a restart.
- `/status` reports progress; `/retry` retries failed videos.

## Required environment variables
`BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `CHANNEL_USERNAME`, `OWNER_ID`.

Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from https://my.telegram.org/apps.
Create the bot with @BotFather. `OWNER_ID` is your numeric Telegram user ID.
Never publish the bot token or commit a real `.env` file.

## Telegram setup
1. Make the destination channel public and set `CHANNEL_USERNAME` to `@channelusername`.
2. Add the bot as a channel administrator and allow it to post messages.
3. Open the bot in a private chat and send `/start`.
4. Send ZIP archives as a Telegram **Document/File**, not as a photo.

## Important limits and reliability notes
- Telegram Local Bot API supports uploads up to 2000 MB. Keep each final MP4 below 2,000,000,000 bytes.
- A 4 GiB ZIP needs enough Telegram account upload allowance to send it to the bot. The receiving bot server cannot override Telegram's client-side upload limit.
- ZIP extraction can use much more disk than the compressed archive. Configure persistent storage large enough for the ZIP plus extracted files and any temporary converted MP4s.
- Use one running replica/instance only. Mount persistent storage at `/app/data`.
- Telegram does not provide an idempotency key for `sendVideo`; if the process crashes after Telegram accepted a video but before SQLite records success, that one video may be sent again on recovery. Failed files are reported and can be retried with `/retry`.
- MP4/H.264 videos are sent directly. Other formats/codecs are converted using FFmpeg; conversion may take time and can fail for damaged or unsupported source files.
- No bot can guarantee that every arbitrary archive will succeed: corrupted/encrypted ZIPs, insufficient disk, unsupported codecs, or Telegram-side errors can cause failures. Check the final report and `/status`.

## Sevalla deployment outline
1. Push all project files to a GitHub repository.
2. Create a Docker-based service in Sevalla from that repository.
3. Add the environment variables listed above as secrets/environment variables.
4. Attach persistent storage mounted at `/app/data`.
5. Keep the service at one replica/instance.
6. Deploy and wait for the logs to show `Local Bot API is ready` and `Bot starting`.
7. In a private chat, send `/start`, then upload a ZIP as a Document.

## Switching this bot to Local Bot API
Stop any other instance polling with this bot token first. Before using the local Bot API, call `logOut` once on the official Bot API endpoint:
`https://api.telegram.org/bot<TOKEN>/logOut`
If you previously configured a webhook, remove it first with `deleteWebhook`. Do not paste the token into public places or screenshots.
