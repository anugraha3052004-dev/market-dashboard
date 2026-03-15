# India Market Dashboard
## Secure · Free · Python Backend + HTML Frontend

---

## Project Structure

```
market-dashboard/
├── server.py          ← Python backend (API key lives HERE only)
├── requirements.txt   ← No extra libraries needed
├── README.md          ← This file
└── static/
    └── index.html     ← Dashboard frontend (no API key here)
```

---

## Step 1 — Get Free Twelve Data API Key

1. Go to: https://twelvedata.com/register
2. Sign up with email (free, no credit card)
3. Go to Dashboard → API Keys
4. Copy your API key

---

## Step 2 — Set API Key as Environment Variable

### On Mac / Linux:
```bash
export TWELVE_DATA_API_KEY=your_api_key_here
```

### On Windows (Command Prompt):
```cmd
set TWELVE_DATA_API_KEY=your_api_key_here
```

### On Windows (PowerShell):
```powershell
$env:TWELVE_DATA_API_KEY = "your_api_key_here"
```

> ⚠️ NEVER paste your API key into server.py or index.html

---

## Step 3 — Run the Server

```bash
cd market-dashboard
python server.py
```

You should see:
```
=======================================================
  India Market Dashboard — Backend Server
=======================================================
  URL:     http://localhost:8000
  API Key: ✅ SET
=======================================================
```

---

## Step 4 — Open the Dashboard

Open your browser and go to:
```
http://localhost:8000
```

---

## How the Security Works

```
Browser (index.html)
    ↓ calls /stock?symbol=TCS  (NO API KEY)
Python server.py
    ↓ adds API key from environment variable
Twelve Data API
    ↓ returns market data
Python server.py
    ↓ strips API key, sends only market data
Browser (index.html)
    ✅ receives only prices, never sees API key
```

---

## API Endpoints (for reference)

| Endpoint | What it does |
|---|---|
| GET /health | Check server + API key status |
| GET /stock?symbol=TCS | Single NSE stock |
| GET /stocks?symbols=TCS,RELIANCE | Multiple stocks |
| GET /history?symbol=TCS | 30-day price history |
| GET /commodities | Gold, Silver, Copper + USD/INR |

---

## Free Tier Limits (Twelve Data)

- 800 API calls per day
- Dashboard refreshes every 5 minutes
- 5 min × 288 refreshes = well within limits
- ~15 minute data delay on free tier

---

## Deploy to the Internet (Optional)

To share with others, deploy to Railway.app (free):

1. Push this folder to GitHub
2. Go to railway.app → New Project → GitHub repo
3. Set environment variable: TWELVE_DATA_API_KEY=your_key
4. Railway gives you a public URL automatically

---

## Troubleshooting

**"API key not set" error:**
→ Run: export TWELVE_DATA_API_KEY=your_key (then restart server)

**"Cannot reach backend" in browser:**
→ Make sure server.py is running first

**Stock not found:**
→ Use exact NSE ticker (e.g. HDFCBANK not HDFC-BANK)

**Port already in use:**
→ Run: python server.py  (it uses port 8000 by default)
→ Or change PORT: PORT=8080 python server.py
