# Bot Commands

All commands must be run from inside the `arbitrage_betting_bot/` folder:
```
cd arbitrage_betting_bot
```

## Delete Database
```
rm storage/betting_bot.db
```

## Start Bot
```
python3.8 main.py --paper
```

## Start Bot (Live — real money)
```
python3.8 main.py
```

## Start Dashboard
```
python3.8 dashboard_server.py --paper
```
Access at: http://localhost:5000  
Find your Mac IP for phone access: `ipconfig getifaddr en0`

## Run One Scan (no loop)
```
python3.8 main.py --paper --once
```

## Live Validation Report (~6-12 API credits)
```
python3.8 validate.py
```
Output saved to `validation_report.txt`

## Unit Tests (no API calls, ~0.4s)
```
cd ..
python3.8 -m pytest tests/ -v
```

---

# Server Deployment (DigitalOcean)

## One-Time Setup — Pushover (phone notifications)
1. Download **Pushover** app on your iPhone
2. Create account at https://pushover.net
3. Note your **User Key** (shown on the main page after login)
4. Click "Your Applications" → "Create an Application" → name it "Arbitrage Bot"
5. Note the **App Token**
6. Add to your `.env`:
   ```
   PUSHOVER_USER_KEY=<your-user-key>
   PUSHOVER_APP_TOKEN=<your-app-token>
   ```

## One-Time Setup — Create DigitalOcean Droplet
- Ubuntu 22.04 LTS, Basic $6/mo (1 vCPU, 1 GB RAM)
- Add your SSH public key at creation: `cat ~/.ssh/id_rsa.pub`
- Note the server IP address

## Upload Secrets to Server
```bash
# From your Mac — update BANKROLL, KALSHI_PRIVATE_KEY_PATH, DASHBOARD_PASSWORD in .env first
scp arbitrage_betting_bot/.env ubuntu@<SERVER_IP>:/tmp/.env
scp ~/.kalshi/private_key.pem ubuntu@<SERVER_IP>:/tmp/private_key.pem
```

## Clone Repo & Run Setup on Server
```bash
ssh ubuntu@<SERVER_IP>
mkdir -p ~/.kalshi
mv /tmp/private_key.pem ~/.kalshi/private_key.pem && chmod 600 ~/.kalshi/private_key.pem
git clone <your-repo-url> /opt/arbitrage-bot
mv /tmp/.env /opt/arbitrage-bot/arbitrage_betting_bot/.env
cd /opt/arbitrage-bot && bash deploy/setup.sh
```

## Smoke Test Before Going Live
```bash
# On the server, inside the bot directory:
cd /opt/arbitrage-bot/arbitrage_betting_bot
python3.8 main.py --once
```
Confirm: scan completes with no errors. If an opportunity is found, a Pushover notification should arrive on your phone.

## Start Services
```bash
sudo systemctl start arbitrage-bot arbitrage-dashboard
sudo systemctl status arbitrage-bot
```

## Monitor
```bash
# Live logs
sudo journalctl -u arbitrage-bot -f

# Dashboard (password required)
# Open in browser: http://<SERVER_IP>:5000
```

## After Code Updates — Deploy to Server
```bash
# On the server:
cd /opt/arbitrage-bot
git pull
sudo systemctl restart arbitrage-bot arbitrage-dashboard
```
