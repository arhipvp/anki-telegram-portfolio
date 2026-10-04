# Deploy To VPS

This portfolio copy does not deploy automatically. The address `203.0.113.10` below is a documentation-only example; replace it with your own server address.

The bot is deployed with Docker Compose into a dedicated directory and Compose project so it does not collide with other small projects on the same server.

## One-Time VPS Setup

Run these commands on the VPS as `root` or another sudo-capable user.

Create a deploy user:

```bash
adduser deploy
usermod -aG docker deploy
mkdir -p /opt/anki-telegram-bot
chown -R deploy:deploy /opt/anki-telegram-bot
```

Install Docker and the Compose plugin if they are not installed yet:

```bash
curl -fsSL https://get.docker.com | sh
docker compose version
```

Add the public key matching the GitHub secret `DEPLOY_SSH_KEY` to:

```text
/home/deploy/.ssh/authorized_keys
```

Then verify SSH access from your machine:

```bash
ssh deploy@203.0.113.10
```

## GitHub Secrets

If you create your own deployment workflow, configure these repository secrets in GitHub:

```text
DEPLOY_HOST=203.0.113.10
DEPLOY_USER=deploy
DEPLOY_PORT=22
DEPLOY_SSH_KEY=<private SSH key for the deploy user>
PROD_ENV=<full production .env content>
```

Use `DEPLOY_PORT=22` unless your own SSH server uses another port.

`PROD_ENV` should contain the complete production `.env`, for example:

```dotenv
TELEGRAM_BOT_TOKEN=...
POLZA_AI_API_KEY=...
ALLOWED_TELEGRAM_USER_IDS=100000001
ANKI_WEB_PORT=13000
ANKI_CONNECT_URL=http://anki-desktop:8765
ANKI_DECK_NAME=German
ANKI_NOTE_TYPE=German Context v1
POLZA_BASE_URL=https://polza.ai/api/v1
POLZA_TEXT_MODEL=google/gemini-3.5-flash
POLZA_IMAGE_MODEL=google/gemini-2.5-flash-image
POLZA_TRANSCRIPTION_MODEL=openai/gpt-4o-mini-transcribe
APP_LOG_LEVEL=DEBUG
APP_LOG_FULL_DEBUG=true
```

A custom deployment workflow can write this secret to `/opt/anki-telegram-bot/.env`. The included test workflow does not use it.

## Deploy Behavior

The included GitHub Actions workflow only installs development dependencies and runs `python -m pytest`. It does not access any production server.

To deploy your own instance, copy the application files to your server, create a private `.env` with your own credentials, then run:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot up -d --build --remove-orphans
```

The secrets above describe configuration for an optional deployment workflow you may create yourself. Do not commit a real `.env`, SSH keys, logs or user data.

## Operations

Check service status:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot ps
```

Follow bot logs:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot logs -f bot
```

Show recent logs:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot logs --tail=100 bot
```

Restart the app:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot restart
```

Stop containers without deleting data:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot down
```

Do not run `docker compose down -v` for this project unless you intentionally want to delete the Anki collection, bot database, media, and logs stored in Docker volumes.

## Rollback

To roll back your own deployment, restore a known-good source version on your server and rebuild the containers. Pushing to this portfolio repository does not deploy anything.

For an urgent server-side rollback, SSH to the VPS, restore the previous source tree if you have a backup, and run:

```bash
cd /opt/anki-telegram-bot
docker compose -p anki-telegram-bot up -d --build --remove-orphans
```

## Isolation Notes

- App path: `/opt/anki-telegram-bot`
- Compose project: `anki-telegram-bot`
- Anki web UI bind: `127.0.0.1:${ANKI_WEB_PORT:-3000}:3000`
- Recommended production Anki web port: `13000`
- No nginx or public HTTP route is required for the Telegram bot.
