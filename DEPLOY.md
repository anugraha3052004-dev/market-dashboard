# Deploy to Render — Step by Step

## What you'll get
- Dashboard running 24/7 on the internet
- Works on your Android phone as an installed app
- No laptop needed after deployment
- Free forever (Render free tier)

---

## Step 1 — Create GitHub Account (if you don't have one)
1. Go to https://github.com
2. Click "Sign up" — use your email
3. Verify email

---

## Step 2 — Upload your code to GitHub

### Option A — GitHub Desktop (easiest for beginners)
1. Download GitHub Desktop: https://desktop.github.com
2. Install and sign in with your GitHub account
3. Click "File" → "Add Local Repository"
4. Browse to your `market-dashboard` folder → Click "Add Repository"
   - If it says "not a git repo", click "create a repository" instead
5. Click "Publish repository"
   - Name: `india-market-dashboard`
   - Keep "Keep this code private" CHECKED (your API key is in env vars, not code — safe either way)
6. Click "Publish Repository"
7. ✅ Your code is now on GitHub

### Option B — Command line
```cmd
cd %USERPROFILE%\Downloads\market-dashboard
git init
git add .
git commit -m "Initial deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/india-market-dashboard.git
git push -u origin main
```

---

## Step 3 — Deploy on Render (free)

1. Go to https://render.com
2. Click "Get Started for Free"
3. Sign up with GitHub (click "Continue with GitHub") — this links them
4. Click "New +" → "Web Service"
5. Click "Connect" next to your `india-market-dashboard` repo
6. Fill in these settings:
   - **Name**: india-market-dashboard
   - **Region**: Singapore (closest to India)
   - **Branch**: main
   - **Runtime**: Python 3
   - **Build Command**: `echo "no build"`
   - **Start Command**: `python server.py`
   - **Plan**: Free
7. Scroll down to **Environment Variables** → Click "Add Environment Variable"
   - Key: `TWELVE_DATA_API_KEY`
   - Value: your actual API key (e.g. `abc123xyz`)
8. Click "Create Web Service"
9. Wait 2–3 minutes while it deploys
10. ✅ You'll get a URL like: `https://india-market-dashboard.onrender.com`

---

## Step 4 — Install on Android as App

1. Open Chrome on your Android phone
2. Go to your Render URL (e.g. `https://india-market-dashboard.onrender.com`)
3. Wait for it to load fully
4. You'll see an "Install Market Dashboard" banner at the bottom — tap Install
   - OR tap Chrome menu (3 dots) → "Add to Home Screen"
5. Tap "Add" on the confirmation popup
6. ✅ App icon appears on your home screen — tap it to open like any app!

---

## Step 5 — Update when you make changes

Whenever you update files on your laptop:
1. Open GitHub Desktop
2. You'll see the changed files listed
3. Write a commit message (e.g. "update UI")
4. Click "Commit to main"
5. Click "Push origin"
6. Render automatically redeploys in 2–3 minutes ✅

---

## Render Free Tier Limits
- ✅ 750 hours/month (enough for 24/7 — 720 hours in a month)
- ✅ Spins down after 15 min of inactivity — first visit takes ~30 sec to wake up
- ✅ No credit card needed
- ✅ Custom domain optional (e.g. marketdashboard.yourdomain.com)

## To prevent sleep (optional)
Render free tier sleeps after 15 min idle. To keep it awake:
- Use UptimeRobot (free): https://uptimerobot.com
- Add a monitor pointing to your Render URL
- It pings every 5 min → keeps server awake 24/7

---

## Troubleshooting

**"Application error" on Render:**
→ Check Render logs (click your service → "Logs" tab)
→ Usually means API key not set — re-check environment variables

**App works on laptop but not on phone:**
→ Make sure you're using the Render URL, not localhost

**Install banner not showing:**
→ Make sure you're on HTTPS (Render gives you HTTPS automatically)
→ Try: Chrome menu → "Add to Home Screen" manually
