"""
India Market Dashboard - Backend v5
Commodities  → Twelve Data + gold-api.com (free)
NSE Stocks   → Yahoo Finance (parallel + 5-min cache)
News         → Yahoo Finance RSS (free)
Batch Scan   → End-of-day auto scan at 3:30 PM IST (saved to file)
Price Alerts → Monitor user-set alerts every 5 minutes
Universe     → 150 liquid NSE stocks + ETFs
"""
import os, json, time, math, threading, re, datetime
try:
    import yfinance as yf
    YF_LIB = True
    print("  [INIT] yfinance library loaded successfully")
except ImportError:
    YF_LIB = False
    print("  [INIT] yfinance not available — using direct URL fetch")
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

import random
API_KEY          = os.environ.get("TWELVE_DATA_API_KEY","")
TWELVE_DATA_BASE = "https://api.twelvedata.com"

# Multiple Yahoo endpoints for load balancing
YF_BASES = [
    "https://query1.finance.yahoo.com/v8/finance/chart",
    "https://query2.finance.yahoo.com/v8/finance/chart",
]

# Rotate User-Agents to avoid rate limiting
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

def get_yf_headers():
    """Return headers with a random User-Agent to avoid rate limiting."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://finance.yahoo.com",
        "Origin": "https://finance.yahoo.com",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

# Keep old names for compatibility
YF_BASE  = YF_BASES[0]
YF_BASE2 = YF_BASES[1]
YF_HEADERS = get_yf_headers()

# ── MULTI-LEVEL CACHE ────────────────────────────────────────────────────────
# Level 1: In-memory (fast, lost on restart)
# Level 2: Disk cache (persistent across restarts)
_cache     = {}
_cache_lck = threading.Lock()
CACHE_TTL  = 300   # 5 min for stock prices
DISK_CACHE_FILE = "price_cache.json"
_disk_cache = {}
_disk_loaded = False

def _load_disk_cache():
    global _disk_cache, _disk_loaded
    if _disk_loaded: return
    try:
        with open(DISK_CACHE_FILE, "r") as f:
            _disk_cache = json.load(f)
        print(f"  [CACHE] Loaded {len(_disk_cache)} entries from disk")
    except:
        _disk_cache = {}
    _disk_loaded = True

def _save_disk_cache():
    try:
        # Only save entries < 30 mins old
        now = time.time()
        fresh = {k:v for k,v in _disk_cache.items() if now - v.get("ts",0) < 1800}
        with open(DISK_CACHE_FILE, "w") as f:
            json.dump(fresh, f)
    except: pass

def cache_get(key):
    _load_disk_cache()
    with _cache_lck:
        # Check memory first (respects per-key TTL)
        e = _cache.get(key)
        if e:
            ttl = e.get("ttl", CACHE_TTL)
            if (time.time() - e["ts"]) < ttl:
                return e["data"]
        # Check disk cache
        e = _disk_cache.get(key)
        if e:
            ttl = e.get("ttl", 1800)
            if (time.time() - e["ts"]) < ttl:
                _cache[key] = e  # promote to memory
                return e["data"]
    return None

def cache_set(key, data, ttl=None):
    _load_disk_cache()
    with _cache_lck:
        entry = {"data": data, "ts": time.time()}
        _cache[key] = entry
        # Also save to disk (for scan results and stock data)
        _disk_cache[key] = entry
    # Save disk cache in background
    threading.Thread(target=_save_disk_cache, daemon=True).start()
# ── BATCH SCAN STORAGE (end-of-day results) ─────────────────────────────────
SCAN_RESULTS_FILE = "scan_results.json"
_scan_lock = threading.Lock()

def save_scan_results(results):
    """Save end-of-day scan results to disk."""
    with _scan_lock:
        try:
            data = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M IST"),
                "date":      time.strftime("%Y-%m-%d"),
                "results":   results
            }
            with open(SCAN_RESULTS_FILE, "w") as f:
                json.dump(data, f)
            print(f"  [BATCH SCAN] Saved {len(results)} results to {SCAN_RESULTS_FILE}")
        except Exception as e:
            print(f"  [BATCH SCAN] Save error: {e}")

def load_scan_results():
    """Load last saved scan results."""
    with _scan_lock:
        try:
            with open(SCAN_RESULTS_FILE, "r") as f:
                return json.load(f)
        except:
            return {"timestamp": None, "date": None, "results": []}

# ── PRICE ALERTS STORAGE ─────────────────────────────────────────────────────
ALERTS_FILE = "price_alerts.json"
_alerts_lock = threading.Lock()

def load_alerts():
    with _alerts_lock:
        try:
            with open(ALERTS_FILE, "r") as f:
                return json.load(f)
        except:
            return []

def save_alerts(alerts):
    with _alerts_lock:
        try:
            with open(ALERTS_FILE, "w") as f:
                json.dump(alerts, f)
        except Exception as e:
            print(f"  [ALERTS] Save error: {e}")

# Triggered alerts (in-memory, shown once per session)
_triggered_alerts = []
_triggered_lock   = threading.Lock()

def add_triggered(alert):
    with _triggered_lock:
        _triggered_alerts.append(alert)
        if len(_triggered_alerts) > 50:
            _triggered_alerts.pop(0)

def get_triggered():
    with _triggered_lock:
        return list(_triggered_alerts)

def clear_triggered():
    with _triggered_lock:
        _triggered_alerts.clear()


# ── TECHNICAL INDICATORS ────────────────────────────────────────────────────
def calc_sma(closes, period):
    if len(closes) < period: return None
    return round(sum(closes[-period:]) / period, 2)

def calc_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_macd(closes):
    if len(closes) < 26: return None, None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if ema12 is None or ema26 is None: return None, None, None
    macd  = round(ema12 - ema26, 2)
    # signal = 9-day EMA of macd (simplified)
    signal = round(macd * 0.9, 2)
    hist   = round(macd - signal, 2)
    return macd, signal, hist

def calc_support_resistance(closes):
    if len(closes) < 5: return None, None
    recent = closes[-20:] if len(closes) >= 20 else closes
    support    = round(min(recent), 2)
    resistance = round(max(recent), 2)
    return support, resistance

def calc_volume_signal(volumes):
    if not volumes or len(volumes) < 5: return "normal"
    avg_vol = sum(volumes[:-1]) / max(len(volumes)-1, 1)
    last_vol = volumes[-1]
    if avg_vol == 0: return "normal"
    ratio = last_vol / avg_vol
    if ratio > 1.5: return "high"
    if ratio < 0.5: return "low"
    return "normal"

def calc_confidence(rsi, macd_hist, vol_signal, change_pct, spread_from_support):
    score = 50
    if rsi:
        if 40 <= rsi <= 60:  score += 5
        elif rsi < 35:       score += 15   # oversold = buy opportunity
        elif rsi > 65:       score -= 10
    if macd_hist:
        if macd_hist > 0:    score += 10
        else:                score -= 5
    if vol_signal == "high": score += 10
    if change_pct > 1:       score += 10
    elif change_pct < -1:    score -= 10
    if spread_from_support is not None:
        if spread_from_support < 3:  score += 10  # near support
    return min(max(score, 10), 95)

def get_recommendation(rsi, macd_hist, change_pct, vol_signal, price, support, resistance, sma20, sma50, ema9):
    bullish_signals = 0
    bearish_signals = 0
    reasons = []
    strategy_points = []

    # ── RSI Analysis ──
    if rsi:
        if rsi < 35:
            bullish_signals += 3
            reasons.append(f"RSI {rsi} — deeply oversold. Strong mean-reversion buy signal.")
            strategy_points.append(f"RSI at {rsi} is historically a high-probability reversal zone. Market has been aggressively sold.")
        elif rsi < 45:
            bullish_signals += 2
            reasons.append(f"RSI {rsi} — oversold territory. Buying pressure building.")
            strategy_points.append(f"RSI below 45 shows selling exhaustion. Buyers likely to step in soon.")
        elif rsi > 75:
            bearish_signals += 3
            reasons.append(f"RSI {rsi} — severely overbought. Correction imminent.")
            strategy_points.append(f"RSI at {rsi} means stock is extremely stretched. Profit booking expected.")
        elif rsi > 65:
            bearish_signals += 2
            reasons.append(f"RSI {rsi} — overbought. Momentum slowing.")
            strategy_points.append(f"RSI above 65 suggests stock has run up fast. Upside is limited short-term.")
        else:
            reasons.append(f"RSI {rsi} — neutral zone. No extreme signal.")

    # ── MACD Analysis ──
    if macd_hist is not None:
        if macd_hist > 0:
            bullish_signals += 1
            reasons.append("MACD histogram positive — upward momentum confirmed.")
            strategy_points.append("Positive MACD histogram means bulls are in control of short-term momentum.")
        else:
            bearish_signals += 1
            reasons.append("MACD histogram negative — downward momentum.")
            strategy_points.append("Negative MACD histogram confirms bears dominating. Avoid fresh longs.")

    # ── Price Action ──
    if change_pct > 1.5:
        bullish_signals += 2
        reasons.append(f"Strong up move +{change_pct:.1f}% today — institutional buying likely.")
        strategy_points.append(f"A {change_pct:.1f}% single-day move often signals fresh institutional interest.")
    elif change_pct > 0.5:
        bullish_signals += 1
        reasons.append(f"Up {change_pct:.1f}% today — positive price action.")
    elif change_pct < -1.5:
        bearish_signals += 2
        reasons.append(f"Sharp fall -{abs(change_pct):.1f}% today — heavy selling pressure.")
        strategy_points.append(f"A {abs(change_pct):.1f}% single-day fall suggests panic selling or bad news. Caution.")
    elif change_pct < -0.5:
        bearish_signals += 1
        reasons.append(f"Down {abs(change_pct):.1f}% today — selling pressure.")

    # ── Volume ──
    if vol_signal == "high":
        bullish_signals += 1
        reasons.append("Volume spike — strong institutional participation. Move likely sustained.")
        strategy_points.append("High volume on price move confirms conviction. Not a random fluctuation.")
    elif vol_signal == "low":
        strategy_points.append("Low volume means weak conviction. Be cautious with position sizing.")

    # ── Moving Averages ──
    if sma20 and sma50 and price:
        if price > sma20 > sma50:
            bullish_signals += 1
            strategy_points.append(f"Price above SMA20 (₹{sma20}) and SMA50 (₹{sma50}) — confirmed uptrend. Trade with the trend.")
        elif price < sma20 < sma50:
            bearish_signals += 1
            strategy_points.append(f"Price below SMA20 (₹{sma20}) and SMA50 (₹{sma50}) — confirmed downtrend. Avoid buying dips.")
        elif price > sma20:
            bullish_signals += 1
            strategy_points.append(f"Price reclaimed SMA20 (₹{sma20}) — short-term trend turning positive.")

    # ── Support / Resistance ──
    if support and resistance and price:
        spread = resistance - support
        if spread > 0:
            pos = (price - support) / spread * 100
            if pos < 20:
                bullish_signals += 2
                reasons.append(f"Price at support ₹{support} — ideal low-risk entry point.")
                strategy_points.append(f"Trading near support ₹{support} gives a natural stop-loss level. Risk/reward is favorable.")
            elif pos < 35:
                bullish_signals += 1
                reasons.append(f"Price near support ₹{support} — good entry zone.")
            elif pos > 80:
                bearish_signals += 1
                reasons.append(f"Price near resistance ₹{resistance} — limited upside from here.")
                strategy_points.append(f"Close to resistance ₹{resistance}. Upside potential is capped unless it breaks through.")

    # ── Final Recommendation ──
    if bullish_signals >= 4:   rec = "BUY"
    elif bullish_signals >= 3: rec = "BUY"
    elif bearish_signals >= 4: rec = "AVOID"
    elif bearish_signals >= 3: rec = "AVOID"
    else:                      rec = "HOLD"

    sentiment = "Bullish" if bullish_signals > bearish_signals else ("Bearish" if bearish_signals > bullish_signals else "Neutral")
    return rec, sentiment, reasons[:5], strategy_points[:4]


def calc_trade_levels(price, support, resistance, rec, rsi, sma20):
    """Calculate target price, stop loss, hold price, expected return and days."""
    if not price or price <= 0:
        return {}

    # Base volatility assumption: ~1.5% daily move for NSE stocks
    atr_pct = 1.5

    if rec == "BUY":
        # Entry: current price
        entry = round(price, 2)
        # Stop loss: just below support or 2.5% below entry
        if support and support < price:
            stop_loss = round(max(support * 0.995, price * 0.975), 2)
        else:
            stop_loss = round(price * 0.975, 2)  # 2.5% stop
        # Target: resistance level or 4% above entry
        if resistance and resistance > price:
            target1 = round(min(resistance * 0.995, price * 1.05), 2)
            target2 = round(price * 1.07, 2)
        else:
            target1 = round(price * 1.04, 2)  # 4% target
            target2 = round(price * 1.07, 2)  # stretch target
        # Hold price: support level — below this, exit
        hold_above = round(stop_loss, 2)
        risk_pct   = round((price - stop_loss) / price * 100, 1)
        reward_pct = round((target1 - price) / price * 100, 1)
        rr_ratio   = round(reward_pct / risk_pct, 1) if risk_pct > 0 else 0
        days_est   = "2–3 trading days"
        return {
            "entry":        entry,
            "target1":      target1,
            "target2":      target2,
            "stop_loss":    stop_loss,
            "hold_above":   hold_above,
            "risk_pct":     risk_pct,
            "reward_pct":   reward_pct,
            "rr_ratio":     rr_ratio,
            "days_est":     days_est,
            "strategy":     f"Buy at ₹{entry}. Target ₹{target1} (+{reward_pct}%) in {days_est}. Stop loss at ₹{stop_loss} (risk {risk_pct}%). Risk:Reward = 1:{rr_ratio}."
        }

    elif rec == "AVOID":
        entry      = round(price, 2)
        target_dn  = round(price * 0.96, 2)    # expected fall target
        cover_at   = round(price * 0.965, 2)   # short cover
        stop_loss  = round(price * 1.025, 2)   # stop for shorts
        risk_pct   = round(2.5, 1)
        reward_pct = round((price - target_dn) / price * 100, 1)
        return {
            "entry":        entry,
            "target1":      target_dn,
            "target2":      round(price * 0.93, 2),
            "stop_loss":    stop_loss,
            "hold_above":   round(price * 0.97, 2),
            "risk_pct":     risk_pct,
            "reward_pct":   reward_pct,
            "rr_ratio":     round(reward_pct / risk_pct, 1),
            "days_est":     "1–3 trading days",
            "strategy":     f"Avoid fresh buying. If short, cover at ₹{cover_at} (-{reward_pct}%). Exit longs immediately. Stop for shorts at ₹{stop_loss}."
        }

    else:  # HOLD
        hold_above = round((support or price * 0.97), 2)
        sell_above = round((resistance or price * 1.04), 2)
        return {
            "entry":        round(price, 2),
            "target1":      sell_above,
            "target2":      round(price * 1.05, 2),
            "stop_loss":    hold_above,
            "hold_above":   hold_above,
            "risk_pct":     round((price - hold_above) / price * 100, 1),
            "reward_pct":   round((sell_above - price) / price * 100, 1),
            "rr_ratio":     0,
            "days_est":     "3–5 trading days",
            "strategy":     f"Hold current position. Keep stop at ₹{hold_above}. Take profit if price reaches ₹{sell_above}. No fresh buying until trend is clearer."
        }

# ── YAHOO FINANCE FETCH (with cache) ────────────────────────────────────────
def fetch_yahoo_yf(symbol, range_="30d"):
    """Fetch using yfinance library — handles Yahoo blocks properly."""
    clean  = symbol.upper().replace(".NS","").replace(".BO","").strip()
    cache_key = f"{clean}:{range_}"
    cached = cache_get(cache_key)
    if cached: return cached

    try:
        print(f"  [YF-LIB] Fetching {clean}.NS...")
        ticker  = yf.Ticker(f"{clean}.NS")
        hist    = ticker.history(period="1mo" if "30" in range_ else "2mo")
        info    = ticker.fast_info

        if hist.empty:
            print(f"  [YF-LIB] Empty history for {clean}")
            return {"symbol": clean, "status": "error", "error": "No data"}

        closes  = hist["Close"].tolist()
        volumes = hist["Volume"].tolist()
        highs   = hist["High"].tolist()
        lows    = hist["Low"].tolist()
        dates   = [str(d)[:10] for d in hist.index]

        price  = float(info.last_price or closes[-1])
        prev   = float(closes[-2] if len(closes) > 1 else price)
        chg    = round(price - prev, 2)
        pct    = round((chg/prev*100) if prev else 0, 2)
        volume = int(info.last_volume or volumes[-1] or 0)
        name   = clean

        try:
            slow_info = ticker.info
            name = slow_info.get("longName") or slow_info.get("shortName") or clean
            h52  = float(slow_info.get("fiftyTwoWeekHigh") or max(highs))
            l52  = float(slow_info.get("fiftyTwoWeekLow")  or min(lows))
        except:
            h52 = max(highs) if highs else price * 1.3
            l52 = min(lows)  if lows  else price * 0.7

        sma20  = calc_sma(closes, 20)
        sma50  = calc_sma(closes, 50)
        ema9   = calc_ema(closes, 9)
        rsi    = calc_rsi(closes)
        macd, sig_line, macd_hist = calc_macd(closes)
        support, resistance = calc_support_resistance(closes)
        vol_sig = calc_volume_signal(volumes[-10:] if len(volumes) >= 10 else volumes)

        spread = ((price - support)/support*100) if support and price else None
        confidence = calc_confidence(rsi, macd_hist, vol_sig, pct, spread)
        rec, sentiment, reasons, strat_pts = get_recommendation(
            rsi, macd_hist, pct, vol_sig, price, support, resistance, sma20, sma50, ema9
        )
        trade_levels = calc_trade_levels(price, support, resistance, rec, rsi, sma20)

        history = [{"date": d, "close": round(float(c),2),
                    "high": round(float(h),2), "low": round(float(l),2),
                    "volume": int(v)}
                   for d,c,h,l,v in zip(dates, closes, highs, lows, volumes)]

        result = {
            "symbol": clean, "name": name,
            "price": round(price,2), "change": chg, "changePct": pct,
            "prevClose": round(prev,2), "volume": volume,
            "high52w": round(h52,2), "low52w": round(l52,2),
            "history": history,
            "indicators": {
                "sma20":sma20,"sma50":sma50,"ema9":ema9,
                "rsi":rsi,"macd":macd,"macdSignal":sig_line,
                "macdHist":macd_hist,"support":support,
                "resistance":resistance,"volumeSignal":vol_sig,
            },
            "recommendation":rec,"sentiment":sentiment,
            "confidence":confidence,"reasons":reasons,
            "strategyPoints":strat_pts,"tradeLevels":trade_levels,
            "source":"yfinance","status":"ok"
        }
        print(f"  [YF-LIB] OK: {clean} ₹{price} RSI={rsi} {rec}")
        cache_set(cache_key, result)
        return result

    except Exception as e:
        print(f"  [YF-LIB] Failed {clean}: {e}")
        return {"symbol": clean, "status": "error", "error": str(e)}


def fetch_yahoo(symbol, range_="30d"):
    clean  = symbol.upper().replace(".NS","").replace(".BO","").strip()
    yf_sym = clean + ".NS"

    # Check cache first
    cache_key = f"{clean}:{range_}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    # Use yfinance library if available (handles Yahoo blocks)
    if YF_LIB:
        return fetch_yahoo_yf(symbol, range_)

    print(f"\n  [YAHOO] Fetching {yf_sym} via direct URL...")
    bases = YF_BASES.copy()
    random.shuffle(bases)
    for attempt, base in enumerate(bases, 1):
        try:
            url = f"{base}/{yf_sym}?interval=1d&range={range_}"
            req = Request(url, headers=get_yf_headers())
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            res  = data["chart"]["result"][0]
            meta = res["meta"]
            q    = res["indicators"]["quote"][0]
            ts   = res.get("timestamp",[])

            closes  = q.get("close",[])
            volumes = q.get("volume",[])
            highs   = q.get("high",[])
            lows    = q.get("low",[])

            # clean nulls
            def clean_list(lst): return [v for v in lst if v is not None]
            closes_c  = clean_list(closes)
            volumes_c = clean_list(volumes)

            price  = meta.get("regularMarketPrice") or (closes_c[-1] if closes_c else 0)
            prev   = meta.get("chartPreviousClose") or meta.get("previousClose") or (closes_c[-2] if len(closes_c)>1 else price)
            volume = meta.get("regularMarketVolume") or (volumes_c[-1] if volumes_c else 0)
            chg    = round(price - prev, 2)
            pct    = round((chg/prev*100) if prev else 0, 2)
            name   = meta.get("shortName") or meta.get("longName") or clean

            # technical indicators
            sma20  = calc_sma(closes_c, 20)
            sma50  = calc_sma(closes_c, 50)
            ema9   = calc_ema(closes_c, 9)
            rsi    = calc_rsi(closes_c)
            macd, sig_line, macd_hist = calc_macd(closes_c)
            support, resistance = calc_support_resistance(closes_c)
            vol_sig = calc_volume_signal(volumes_c[-10:] if len(volumes_c)>=10 else volumes_c)

            spread_from_support = None
            if support and price and support > 0:
                spread_from_support = round((price - support) / support * 100, 1)

            confidence = calc_confidence(rsi, macd_hist, vol_sig, pct, spread_from_support)
            rec, sentiment, reasons, strategy_points = get_recommendation(
                rsi, macd_hist, pct, vol_sig, price, support, resistance, sma20, sma50, ema9
            )
            trade_levels = calc_trade_levels(price, support, resistance, rec, rsi, sma20)

            # build OHLCV history
            history = []
            for i in range(len(ts)):
                try:
                    history.append({
                        "date":   time.strftime("%Y-%m-%d", time.localtime(ts[i])),
                        "close":  round(closes[i],2)  if closes[i]  else None,
                        "high":   round(highs[i],2)   if highs[i]   else None,
                        "low":    round(lows[i],2)    if lows[i]    else None,
                        "volume": int(volumes[i])     if volumes[i] else 0,
                    })
                except: pass
            history = [h for h in history if h["close"]]

            print(f"  [YAHOO] OK {name} ₹{price} {rec} conf={confidence}%")
            result = {
                "symbol": clean, "name": name,
                "price": round(price,2), "change": chg, "changePct": pct,
                "prevClose": round(prev,2), "volume": int(volume),
                "high52w": meta.get("fiftyTwoWeekHigh",0),
                "low52w":  meta.get("fiftyTwoWeekLow",0),
                "history": history,
                "indicators": {
                    "sma20": sma20, "sma50": sma50, "ema9": ema9,
                    "rsi": rsi, "macd": macd, "macdSignal": sig_line,
                    "macdHist": macd_hist, "support": support,
                    "resistance": resistance, "volumeSignal": vol_sig,
                },
                "recommendation":  rec,
                "sentiment":       sentiment,
                "confidence":      confidence,
                "reasons":         reasons,
                "strategyPoints":  strategy_points,
                "tradeLevels":     trade_levels,
                "status": "ok"
            }
            cache_set(cache_key, result)
            return result
        except Exception as e:
            print(f"  [YAHOO] Attempt {attempt} failed for {yf_sym}: {type(e).__name__}: {e}")
    print(f"  [YAHOO] ALL ATTEMPTS FAILED for {yf_sym}")
    # Fallback: try Twelve Data for basic price data
    if API_KEY:
        print(f"  [TD FALLBACK] Trying Twelve Data for {clean}...")
        td_batch = fetch_td_batch_quote([clean])
        if clean in td_batch:
            stock = build_stock_from_td(td_batch[clean], clean, compute_indicators=True)
            if stock:
                print(f"  [TD FALLBACK] OK for {clean} via Twelve Data")
                cache_set(cache_key, stock)
                return stock
    return {"symbol":clean,"status":"error","error":f"Could not load {clean} from Yahoo Finance or Twelve Data"}

def fetch_yahoo_multi(symbols, range_="30d"):
    """
    Fetch stocks in parallel batches of 3.
    - Batch size 3 avoids Yahoo rate-limiting (which causes slowness/failures)
    - Each batch runs in parallel threads
    - Cached stocks skip the network entirely
    """
    out  = {}
    lock = threading.Lock()
    # Fetch ALL stocks in parallel simultaneously — no batching
    # Each thread independent, max 8s timeout per stock
    def fetch_one(sym):
        result = fetch_yahoo(sym, range_)
        with lock:
            out[sym] = result

    threads = [threading.Thread(target=fetch_one, args=(s,), daemon=True) for s in symbols]
    for t in threads: t.start()
    for t in threads: t.join(timeout=8)  # 8s max per stock, all run simultaneously

    return out

# ── NEWS VIA YAHOO FINANCE RSS ───────────────────────────────────────────────
POSITIVE_WORDS = ["gain","rise","up","surge","growth","profit","buy","bull","strong","rally","positive","beat","record","high","boost","win","upgrade","outperform","expand","launch","acquire","dividend","q4","q3","q2","q1","beats","above","jump","soar"]
NEGATIVE_WORDS = ["fall","drop","down","loss","decline","sell","bear","weak","crash","negative","miss","cut","below","plunge","suspend","penalty","fine","probe","fraud","warn","downgrade","underperform","recall","lay","strike","debt","default","resign"]

def classify_sentiment(title):
    tl = title.lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in tl)
    neg = sum(1 for w in NEGATIVE_WORDS if w in tl)
    if pos > neg: return "positive"
    if neg > pos: return "negative"
    return "neutral"

def fetch_news(symbol, limit=10):
    clean = symbol.upper().replace(".NS","").replace(".BO","").strip()
    news  = []
    seen  = set()

    # Source 1: Yahoo Finance RSS (works during market hours)
    for yf_url in [
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={clean}.NS&region=IN&lang=en-IN",
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={clean}&region=IN&lang=en-IN",
    ]:
        if len(news) >= limit: break
        try:
            req = Request(yf_url, headers={"User-Agent":"Mozilla/5.0 (compatible)"})
            with urlopen(req, timeout=7) as r:
                xml = r.read()
            root = ET.fromstring(xml)
            for item in root.findall(".//item")[:limit]:
                title = (item.findtext("title") or "").strip()
                if not title or title in seen: continue
                seen.add(title)
                link  = (item.findtext("link") or "").strip()
                date  = (item.findtext("pubDate") or "").strip()
                desc  = re.sub(r'<[^>]+>', '', (item.findtext("description") or ""))[:200]
                sentiment = classify_sentiment(title + " " + desc)
                impact = "HIGH"   if any(w in title.lower() for w in ["result","q4","q3","q2","q1","earnings","profit","loss","acquisition","merger","penalty","fraud","upgrade","downgrade","dividend"]) \
                    else "MEDIUM" if any(w in title.lower() for w in ["launch","expand","contract","order","deal","partner"]) \
                    else "LOW"
                news.append({"title":title,"link":link,"date":date,"desc":desc,"sentiment":sentiment,"impact":impact})
            if news:
                print(f"  [NEWS] Yahoo {clean}.NS: {len(news)} items")
                break
        except Exception as e:
            print(f"  [NEWS] Yahoo error: {e}")

    # Source 2: Economic Times fallback (works 24/7)
    if len(news) < 3:
        name_map = {
            "TCS":"tata-consultancy-services","INFY":"infosys","RELIANCE":"reliance-industries",
            "HDFCBANK":"hdfc-bank","ICICIBANK":"icici-bank","SBIN":"state-bank-of-india",
            "WIPRO":"wipro","TATAMOTORS":"tata-motors","BAJFINANCE":"bajaj-finance",
            "DRREDDY":"dr-reddys-laboratories","ADANIENT":"adani-enterprises",
            "ZOMATO":"zomato","BHARTIARTL":"bharti-airtel","HCLTECH":"hcl-technologies",
            "KOTAKBANK":"kotak-mahindra-bank","AXISBANK":"axis-bank","ITC":"itc",
            "LT":"larsen-and-toubro","SUNPHARMA":"sun-pharmaceutical-industries",
            "TATASTEEL":"tata-steel","JSWSTEEL":"jsw-steel","ONGC":"ongc",
        }
        slug = name_map.get(clean, clean.lower())
        try:
            et_url = f"https://economictimes.indiatimes.com/{slug}/rssfeeds.cms"
            req = Request(et_url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*"})
            with urlopen(req, timeout=7) as r:
                xml = r.read()
            root = ET.fromstring(xml)
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()
                if not title or title in seen: continue
                seen.add(title)
                link  = (item.findtext("link") or "").strip()
                date  = (item.findtext("pubDate") or "").strip()
                desc  = re.sub(r'<[^>]+>', '', (item.findtext("description") or ""))[:200]
                sentiment = classify_sentiment(title + " " + desc)
                impact = "HIGH" if any(w in title.lower() for w in ["result","earnings","profit","loss","dividend","upgrade","downgrade"]) else "MEDIUM"
                news.append({"title":title,"link":link,"date":date,"desc":desc,"sentiment":sentiment,"impact":impact,"source":"Economic Times"})
            print(f"  [NEWS] ET {clean}: +{len(news)} items total")
        except Exception as e:
            print(f"  [NEWS] ET fallback: {e}")

    return news[:limit]

def fetch_market_news():
    """
    Fetch India market & business news from multiple 24/7 RSS feeds.
    These work after-hours, weekends and holidays — not just during market hours.
    """
    feeds = [
        # Economic Times — India's #1 business news, always active
        ("Economic Times", "Markets",
         "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Economic Times", "Stocks",
         "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
        ("Economic Times", "Economy",
         "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms"),
        # Moneycontrol — major Indian financial portal
        ("Moneycontrol", "Markets",
         "https://www.moneycontrol.com/rss/marketreports.xml"),
        ("Moneycontrol", "Business",
         "https://www.moneycontrol.com/rss/business.xml"),
        # Business Standard
        ("Business Standard", "Markets",
         "https://www.business-standard.com/rss/markets-106.rss"),
        ("Business Standard", "Economy",
         "https://www.business-standard.com/rss/economy-102.rss"),
        # Mint
        ("Mint", "Markets",
         "https://www.livemint.com/rss/markets"),
        # NDTV Profit
        ("NDTV Profit", "Business",
         "https://feeds.feedburner.com/ndtvprofit-latest"),
        # Yahoo Finance India fallback
        ("Yahoo Finance", "India",
         "https://finance.yahoo.com/rss/topstories"),
    ]

    all_news = []
    seen     = set()

    for source, category, url in feeds:
        if len(all_news) >= 25: break
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0",
                "Accept":     "application/rss+xml, application/xml, text/xml, */*",
            })
            with urlopen(req, timeout=7) as r:
                raw = r.read()
            # Try parsing — handle encoding issues gracefully
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                raw2 = raw.decode("utf-8", errors="replace").encode("utf-8")
                root = ET.fromstring(raw2)

            items = root.findall(".//item")
            added = 0
            for item in items:
                if added >= 5: break
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                date  = (item.findtext("pubDate") or item.findtext("dc:date","") or "").strip()
                desc  = (item.findtext("description") or "").strip()

                # Strip HTML tags from desc
                desc = re.sub(r'<[^>]+>', '', desc)[:250]

                if not title or title in seen: continue
                # Filter: only India/market relevant
                combined = (title + " " + desc).lower()
                india_kw = ["india","nse","bse","nifty","sensex","sebi","rbi","rupee","inr",
                            "crore","lakh","₹","mumbai","delhi","bengaluru","infosys","tata",
                            "reliance","hdfc","icici","sbi","bajaj","wipro","equity","ipo",
                            "stock","share","market","fund","gdp","inflation","rate","budget"]
                if source in ("Yahoo Finance",) and not any(k in combined for k in india_kw):
                    continue

                seen.add(title)
                sentiment = classify_sentiment(title + " " + desc)
                impact    = "HIGH"   if any(w in title.lower() for w in ["result","earnings","q4","q3","q2","q1","profit","loss","acquisition","merger","penalty","fraud","upgrade","downgrade","dividend","rbi","sebi","budget","gdp","inflation"]) \
                       else "MEDIUM" if any(w in title.lower() for w in ["launch","expand","contract","order","deal","partner","ipo","stake","invest"]) \
                       else "LOW"

                all_news.append({
                    "title":     title,
                    "link":      link,
                    "date":      date,
                    "desc":      desc,
                    "sentiment": sentiment,
                    "source":    source,
                    "category":  category,
                    "impact":    impact
                })
                added += 1

            print(f"  [MARKET NEWS] {source}/{category}: {added} items")
        except Exception as e:
            print(f"  [MARKET NEWS] {source} error: {type(e).__name__}: {e}")

    print(f"  [MARKET NEWS] Total: {len(all_news)} items from {len(feeds)} feeds")
    return all_news[:24]

# ── TWELVE DATA (commodities + forex) ───────────────────────────────────────
def fetch_td(path, params):
    if not API_KEY: return {"error":"TWELVE_DATA_API_KEY not set"}
    params["apikey"] = API_KEY
    url  = f"{TWELVE_DATA_BASE}{path}?{urlencode(params)}"
    safe = f"{TWELVE_DATA_BASE}{path}?{urlencode({k:v for k,v in params.items() if k!='apikey'})}"
    print(f"\n  [TD] {safe}")
    try:
        req = Request(url, headers={"User-Agent":"MarketDashboard/1.0"})
        with urlopen(req, timeout=12) as r:
            raw = r.read().decode()
            print(f"  [TD RESP] {raw[:150]}")
            return json.loads(raw)
    except Exception as e:
        return {"error":str(e)}


# ── TWELVE DATA BATCH QUOTE (1 API call = 30 stocks instantly) ────────────────
# This is the KEY optimization — replaces slow Yahoo per-stock fetching for scan
def fetch_td_batch_quote(symbols):
    """
    Fetch current price + basic data for multiple NSE stocks in ONE API call.
    Twelve Data free plan: supports batch quotes, 8 calls/min, 800/day.
    Returns dict: {symbol: {price, change, changePct, volume, high52w, low52w, ...}}
    """
    if not API_KEY:
        return {}

    # Format symbols for NSE: "RELIANCE:NSE,TCS:NSE,..."
    td_syms = ",".join([f"{s}:NSE" for s in symbols])

    # Check cache (30 min TTL for batch quotes)
    cache_key = f"td_batch:{len(symbols)}:{sorted(symbols)[0] if symbols else 'empty'}"
    cached = cache_get(cache_key)
    if cached:
        print(f"  [TD BATCH] Cache hit for {len(symbols)} symbols")
        return cached

    print(f"  [TD BATCH] Fetching {len(symbols)} stocks in 1 API call...")
    try:
        url = f"{TWELVE_DATA_BASE}/quote?symbol={td_syms}&dp=2&apikey={API_KEY}"
        req = Request(url, headers={"User-Agent": "MarketDashboard/1.0"})
        with urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode())

        result = {}
        # Handle both single and batch response formats
        if isinstance(raw, dict) and "symbol" in raw:
            # Single stock response
            raw = {raw["symbol"]: raw}

        for td_sym, d in raw.items():
            if not isinstance(d, dict) or d.get("status") == "error":
                continue
            # Extract NSE symbol (remove :NSE suffix)
            sym = td_sym.replace(":NSE", "").replace(":BSE", "")
            try:
                price   = float(d.get("close") or d.get("price") or 0)
                prev    = float(d.get("previous_close") or price)
                change  = float(d.get("change") or 0)
                chg_pct = float(d.get("percent_change") or 0)
                vol     = int(float(d.get("volume") or 0))
                h52     = float(d.get("fifty_two_week", {}).get("high") or d.get("high") or price*1.3)
                l52     = float(d.get("fifty_two_week", {}).get("low")  or d.get("low")  or price*0.7)
                name    = d.get("name") or sym
                if price > 0:
                    result[sym] = {
                        "symbol":    sym,
                        "name":      name,
                        "price":     round(price, 2),
                        "change":    round(change, 2),
                        "changePct": round(chg_pct, 2),
                        "prevClose": round(prev, 2),
                        "volume":    vol,
                        "high52w":   round(h52, 2),
                        "low52w":    round(l52, 2),
                        "source":    "twelvedata",
                        "status":    "ok"
                    }
            except Exception as ex:
                print(f"  [TD BATCH] Parse error for {td_sym}: {ex}")

        print(f"  [TD BATCH] Got {len(result)}/{len(symbols)} stocks OK")
        if result:
            cache_set(cache_key, result)
        return result

    except Exception as e:
        print(f"  [TD BATCH] Failed: {e}")
        return {}


def fetch_td_history_parallel(sym, result_dict, lock):
    """Fetch TD time_series for one stock - runs in parallel thread."""
    try:
        url = (f"{TWELVE_DATA_BASE}/time_series"
               f"?symbol={sym}:NSE&interval=1day&outputsize=60&dp=2&apikey={API_KEY}")
        req = Request(url, headers={"User-Agent":"MarketDashboard/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if "values" in data:
            vals = list(reversed(data["values"]))
            closes = [float(v["close"]) for v in vals if v.get("close")]
            vols   = [int(float(v.get("volume",0))) for v in vals]
            hist   = [{"date":v.get("datetime","")[:10],
                       "close":round(float(v["close"]),2),
                       "high":round(float(v.get("high",0)),2),
                       "low":round(float(v.get("low",0)),2),
                       "volume":int(float(v.get("volume",0)))}
                      for v in vals if v.get("close")]
            with lock:
                result_dict[sym] = {"closes":closes,"volumes":vols,"history":hist}
            print(f"  [TD-TS] {sym}: {len(closes)} days OK")
        else:
            print(f"  [TD-TS] {sym}: no values - {data.get('message','unknown error')}")
    except Exception as e:
        print(f"  [TD-TS] {sym} failed: {e}")


def fetch_stocks_master(symbols):
    """
    MASTER STOCK FETCHER
    Strategy: Stooq (free CSV, no limits) → TD time_series (fallback)
    Cache: 30 minutes per stock
    """
    if not symbols:
        return {}

    syms = [s.upper().replace(".NS","").replace(".BO","").strip() for s in symbols]
    result     = {}
    need_fetch = []

    # Step 1: Cache check (30 min TTL)
    for sym in syms:
        cached = cache_get(f"{sym}:master")
        if cached and cached.get("status") == "ok":
            result[sym] = cached
        else:
            need_fetch.append(sym)

    if not need_fetch:
        print(f"  [MASTER] All {len(result)} stocks from cache — instant!")
        return result

    print(f"  [MASTER] Need to fetch: {need_fetch}")

    # Step 2: Stooq parallel fetch (all stocks simultaneously, no rate limits)
    hist_data = {}
    lock      = threading.Lock()
    threads   = [
        threading.Thread(target=fetch_stooq_history_thread,
                         args=(sym, hist_data, lock), daemon=True)
        for sym in need_fetch
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=12)
    print(f"  [MASTER] Stooq got history for: {list(hist_data.keys())}")

    # Step 3: Get live prices from TD (1 batch call for ALL stocks)
    td_quotes = {}
    if API_KEY:
        try:
            td_syms = ",".join([f"{s}:NSE" for s in need_fetch])
            url     = f"{TWELVE_DATA_BASE}/quote?symbol={td_syms}&dp=2&apikey={API_KEY}"
            req     = Request(url, headers={"User-Agent": "MarketDashboard/1.0"})
            with urlopen(req, timeout=12) as r:
                raw = json.loads(r.read().decode())
            # Normalize single vs batch
            if isinstance(raw, dict) and "close" in raw:
                raw = {raw.get("symbol", need_fetch[0]+":NSE"): raw}
            for k, v in raw.items():
                if isinstance(v, dict) and v.get("close") and v.get("status") != "error":
                    sym = k.replace(":NSE","").replace(":BSE","")
                    td_quotes[sym] = v
                    print(f"  [MASTER] TD quote OK: {sym} ₹{v['close']}")
        except Exception as e:
            print(f"  [MASTER] TD quote failed: {e}")

    # Step 4: Build stock objects — Stooq history + TD live price
    stooq_failed = []
    for sym in need_fetch:
        hist = hist_data.get(sym)
        td_q = td_quotes.get(sym)

        if hist and hist.get("closes"):
            # Stooq history available — build full stock
            stock = build_stock_from_stooq(sym, hist, td_q)
            if stock:
                cache_set(f"{sym}:master", stock, ttl=1800)  # 30 min cache
                result[sym] = stock
                print(f"  [MASTER] Built from Stooq: {sym} ₹{stock['price']}")
                continue

        if td_q:
            # No Stooq history but have TD quote — build basic stock
            price   = float(td_q.get("close", 0))
            prev    = float(td_q.get("previous_close") or price)
            chg     = round(price - prev, 2)
            pct     = round(float(td_q.get("percent_change") or 0), 2)
            vol     = int(float(td_q.get("volume") or 0))
            stock   = {
                "symbol": sym, "name": td_q.get("name", sym),
                "price": round(price,2), "change": chg, "changePct": pct,
                "prevClose": round(prev,2), "volume": vol,
                "high52w": float((td_q.get("fifty_two_week") or {}).get("high") or price*1.3),
                "low52w":  float((td_q.get("fifty_two_week") or {}).get("low")  or price*0.7),
                "history": [],
                "indicators": {"rsi":None,"sma20":None,"sma50":None,"ema9":None,
                                "macd":None,"macdSignal":None,"macdHist":None,
                                "support":None,"resistance":None,
                                "volumeSignal":"high" if vol>5000000 else "normal"},
                "recommendation":"HOLD","sentiment":"Neutral",
                "confidence":50,"reasons":["Price data from Twelve Data. Load individual stock for full analysis."],
                "strategyPoints":[],"tradeLevels":calc_trade_levels(price,None,None,"HOLD",None,None),
                "source":"twelvedata","status":"ok"
            }
            cache_set(f"{sym}:master", stock, ttl=1800)
            result[sym] = stock
            print(f"  [MASTER] Built from TD: {sym} ₹{price}")
            continue

        stooq_failed.append(sym)

    # Step 5: TD /time_series for stocks where both Stooq and TD quote failed
    for sym in stooq_failed:
        print(f"  [MASTER] Trying TD time_series for {sym}...")
        try:
            url = (f"{TWELVE_DATA_BASE}/time_series"
                   f"?symbol={sym}:NSE&interval=1day&outputsize=60&dp=2&apikey={API_KEY}")
            req = Request(url, headers={"User-Agent": "MarketDashboard/1.0"})
            with urlopen(req, timeout=12) as r:
                ts = json.loads(r.read().decode())
            if "values" in ts:
                vals = list(reversed(ts["values"]))
                closes  = [float(v["close"]) for v in vals if v.get("close")]
                volumes = [int(float(v.get("volume",0))) for v in vals]
                highs   = [float(v.get("high",0)) for v in vals]
                lows    = [float(v.get("low",0)) for v in vals]
                if closes:
                    price = closes[-1]
                    prev  = closes[-2] if len(closes)>1 else price
                    chg   = round(price-prev,2)
                    pct   = round((chg/prev*100) if prev else 0,2)
                    hist_obj = {
                        "symbol":sym,"rows":[{"date":v.get("datetime","")[:10],
                            "close":float(v["close"]),"high":float(v.get("high",0)),
                            "low":float(v.get("low",0)),"volume":int(float(v.get("volume",0)))}
                            for v in vals if v.get("close")],
                        "closes":closes,"volumes":volumes,"highs":highs,"lows":lows
                    }
                    stock = build_stock_from_stooq(sym, hist_obj, None)
                    if stock:
                        cache_set(f"{sym}:master", stock, ttl=1800)
                        result[sym] = stock
                        print(f"  [MASTER] Built from TD ts: {sym} ₹{stock['price']}")
                        continue
        except Exception as e:
            print(f"  [MASTER] TD ts failed for {sym}: {e}")

    print(f"  [MASTER] Final: {len(result)}/{len(syms)} stocks")
    return result


def parse_stooq_csv(csv_text):
    """Parse Stooq CSV response into list of OHLCV dicts."""
    rows = []
    lines = csv_text.strip().split('\n')
    if len(lines) < 2:
        return rows
    headers = [h.strip().lower() for h in lines[0].split(',')]
    for line in lines[1:]:
        vals = line.strip().split(',')
        if len(vals) < len(headers):
            continue
        row = dict(zip(headers, vals))
        try:
            rows.append({
                'date':   row.get('date',''),
                'open':   float(row.get('open', 0) or 0),
                'high':   float(row.get('high', 0) or 0),
                'low':    float(row.get('low', 0) or 0),
                'close':  float(row.get('close', 0) or 0),
                'volume': int(float(row.get('volume', 0) or 0)),
            })
        except:
            pass
    return rows


def fetch_stooq_history(symbol, days=60):
    """Fetch stock history from Stooq - free, no auth, no rate limits."""
    clean = symbol.upper().replace('.NS','').replace('.BO','').strip()
    cache_key = f"stooq:{clean}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    stooq_sym = clean.lower() + '.ns'
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            text = r.read().decode('utf-8', errors='ignore')

        rows = parse_stooq_csv(text)
        if not rows:
            return None

        # Sort oldest first
        rows.sort(key=lambda x: x['date'])
        rows = rows[-days:]  # last N days

        result = {
            'symbol':  clean,
            'rows':    rows,
            'closes':  [r['close']  for r in rows],
            'volumes': [r['volume'] for r in rows],
            'highs':   [r['high']   for r in rows],
            'lows':    [r['low']    for r in rows],
        }
        cache_set(cache_key, result)
        print(f"  [STOOQ] {clean}: {len(rows)} days OK")
        return result
    except Exception as e:
        print(f"  [STOOQ] {clean} failed: {e}")
        return None


def fetch_stooq_history_thread(symbol, result_dict, lock):
    """Thread worker for parallel Stooq history fetch."""
    data = fetch_stooq_history(symbol)
    with lock:
        result_dict[symbol] = data


def build_stock_from_stooq(symbol, hist, td_quote=None):
    """Build full stock dict from Stooq history + optional TD live price."""
    if not hist or not hist.get('closes'):
        return None

    closes  = hist['closes']
    volumes = hist['volumes']
    highs   = hist['highs']
    lows    = hist['lows']
    rows    = hist['rows']

    if not closes:
        return None

    # Price: use TD quote if available (more current), else latest Stooq close
    if td_quote and td_quote.get('close'):
        price = float(td_quote['close'])
        prev  = float(td_quote.get('previous_close') or closes[-1])
        chg   = round(price - prev, 2)
        pct   = round(float(td_quote.get('percent_change') or 0), 2)
        vol   = int(float(td_quote.get('volume') or volumes[-1] or 0))
        name  = td_quote.get('name') or symbol
    else:
        price = closes[-1]
        prev  = closes[-2] if len(closes) > 1 else price
        chg   = round(price - prev, 2)
        pct   = round((chg/prev*100) if prev else 0, 2)
        vol   = volumes[-1] if volumes else 0
        name  = symbol

    if price <= 0:
        return None

    h52 = max(highs) if highs else price * 1.3
    l52 = min(lows)  if lows  else price * 0.7

    # Compute indicators
    sma20  = calc_sma(closes, 20)
    sma50  = calc_sma(closes, 50)
    ema9   = calc_ema(closes, 9)
    rsi    = calc_rsi(closes)
    macd, sig_line, macd_hist = calc_macd(closes)
    support, resistance = calc_support_resistance(closes)
    vol_sig = calc_volume_signal(volumes[-10:] if len(volumes) >= 10 else volumes)

    spread = ((price - support)/support*100) if support and price else None
    confidence = calc_confidence(rsi, macd_hist, vol_sig, pct, spread)
    rec, sentiment, reasons, strat_pts = get_recommendation(
        rsi, macd_hist, pct, vol_sig, price,
        support, resistance, sma20, sma50, ema9
    )
    trade_levels = calc_trade_levels(price, support, resistance, rec, rsi, sma20)

    history = [{'date':r['date'],'close':round(r['close'],2),
                'high':round(r['high'],2),'low':round(r['low'],2),
                'volume':r['volume']} for r in rows[-30:]]

    return {
        "symbol": symbol, "name": name,
        "price": round(price,2), "change": chg, "changePct": pct,
        "prevClose": round(prev,2), "volume": vol,
        "high52w": round(h52,2), "low52w": round(l52,2),
        "history": history,
        "indicators": {
            "sma20":sma20,"sma50":sma50,"ema9":ema9,
            "rsi":rsi,"macd":macd,"macdSignal":sig_line,
            "macdHist":macd_hist,"support":support,
            "resistance":resistance,"volumeSignal":vol_sig,
        },
        "recommendation":rec,"sentiment":sentiment,
        "confidence":confidence,"reasons":reasons,
        "strategyPoints":strat_pts,"tradeLevels":trade_levels,
        "source":"stooq","status":"ok"
    }


# ── STOCKSENSE BATCH SCORER (pure Python, same rules as frontend JS) ──────────
def score_stock_python(d):
    """Run 5-layer STOCKSENSE scoring on a stock dict from fetch_yahoo.
    Returns dict with score, signal, setup, confidence, entry, sl, t1, t2.
    Same rules as the frontend JS calcLayer2/calcLayer3/calcFinalSignal."""
    if not d or d.get('status') != 'ok':
        return None

    ind   = d.get('indicators', {})
    price = d.get('price', 0)
    if not price:
        return None

    rsi      = float(ind.get('rsi') or 50)
    macd_h   = float(ind.get('macdHist') or 0)
    macd_l   = float(ind.get('macd') or 0)
    vol_sig  = ind.get('volumeSignal', 'normal')
    sma20    = float(ind.get('sma20') or 0)
    sma50    = float(ind.get('sma50') or 0)
    support  = float(ind.get('support') or 0)
    resist   = float(ind.get('resistance') or 0)
    h52      = float(d.get('high52w') or price * 1.3)
    l52      = float(d.get('low52w')  or price * 0.7)
    chg_pct  = float(d.get('changePct') or 0)
    conf     = float(d.get('confidence') or 50)
    rec      = d.get('recommendation', 'HOLD')
    sent     = d.get('sentiment', 'Neutral')

    # Layer 2: Technical (0-7)
    l2 = 0.0
    # Moving averages
    if sma20 and sma50 and price < sma20 and price < sma50:
        l2 -= 2
    else:
        if sma20 and price > sma20: l2 += 0.5
        if sma50 and price > sma50: l2 += 1.0
    # RSI
    if   rsi < 30:  l2 += 2.0
    elif rsi < 45:  l2 += 1.5
    elif rsi < 60:  l2 += 1.0
    elif rsi < 70:  l2 += 0.5
    else:           l2 -= 1.0
    # MACD
    if   macd_h > 0 and macd_l > 0: l2 += 1.5
    elif macd_h > 0:                 l2 += 1.0
    elif macd_h < 0 and macd_l < 0: l2 -= 1.0
    # Volume
    if   vol_sig == 'high' and chg_pct < -1: l2 -= 1.0
    elif vol_sig == 'high':                  l2 += 1.5
    elif vol_sig == 'normal':                l2 += 0.5
    # Support/Resistance
    if support and price:
        dist_s = ((price - support) / support) * 100
        if   dist_s <= 2 and chg_pct >= 0: l2 += 1.0
        elif dist_s <= 5:                  l2 += 0.5
    if resist and price and price >= resist * 0.998: l2 += 1.0
    # 52W position
    pct_above_low = ((price - l52) / l52 * 100) if l52 > 0 else 50
    pct_below_hi  = ((h52 - price) / h52 * 100) if h52 > 0 else 10
    if   pct_above_low <= 5:  l2 += 1.0
    elif pct_above_low <= 30: l2 += 0.5
    elif pct_below_hi  <= 3:  l2 -= 0.5
    l2 = max(0, min(7, round(l2, 1)))

    # Layer 3: Fundamentals proxy (0-6)
    l3 = 0.0
    if   conf >= 75: l3 += 2.0
    elif conf >= 60: l3 += 1.0
    elif conf >= 45: l3 += 0.5
    if sma20 and sma50 and price > sma20 and sma20 > sma50: l3 += 1.0
    elif sma50 and price > sma50: l3 += 0.5
    elif sma20 and sma50 and price < sma20 and price < sma50: l3 -= 1.0
    if   rsi < 35:  l3 += 1.0
    elif rsi < 50:  l3 += 0.5
    elif rsi >= 65: l3 -= 0.5
    if   vol_sig == 'high' and chg_pct > 0:  l3 += 1.0
    elif vol_sig == 'high' and chg_pct < 0:  l3 -= 0.5
    elif vol_sig == 'high':                  l3 += 0.5
    if   sent == 'Bullish': l3 += 1.0
    elif sent == 'Neutral': l3 += 0.5
    elif sent == 'Bearish': l3 -= 0.5
    if   rec == 'BUY':   l3 += 0.5
    elif rec == 'AVOID': l3 -= 0.5
    l3 = max(0, min(6, round(l3, 1)))

    # Layer 4: News proxy from sentiment (0-5)
    l4 = 0.0
    if   sent == 'Bullish' and rec == 'BUY':   l4 = 2.0
    elif sent == 'Bullish':                     l4 = 1.0
    elif sent == 'Bearish' and rec == 'AVOID':  l4 = -2.0
    elif sent == 'Bearish':                     l4 = -1.0
    if chg_pct > 2 and vol_sig == 'high':       l4 = min(l4 + 1.0, 5)
    if chg_pct < -3:                            l4 = max(l4 - 1.0, -5)
    l4 = max(-5, min(5, round(l4, 1)))

    # Layer 1: Use market health cache (3 = CAUTION default)
    l1 = 3.0
    mh = cache_get("market-data")
    if mh:
        vix   = float((mh.get('vix') or {}).get('price') or 15)
        crude = float((mh.get('crude') or {}).get('price') or 78)
        nchg  = float((mh.get('nifty') or {}).get('changePct') or 0)
        s5chg = float((mh.get('sp500') or {}).get('changePct') or 0)
        l1 = 0.0
        l1 += (2 if nchg >= 1 else 1 if nchg >= 0.3 else 0 if nchg > -0.3 else -1 if nchg >= -1 else -2)
        l1 += (1 if vix < 13 else 0.5 if vix < 16 else 0 if vix < 20 else -1)
        l1 += (1 if crude < 80 else 0.5 if crude < 90 else 0 if crude < 100 else -1)
        l1 += (1 if s5chg >= 0.5 else 0.5 if s5chg >= 0 else 0 if s5chg >= -1 else -1)
        l1 += (0.5 if datetime.datetime.now().weekday() != 3 else 0)
        l1 = max(0, min(5, round(l1, 1)))

    total = l1 + l2 + l3 + l4

    # Signal
    if   total >= 19: signal = 'STRONG BUY'
    elif total >= 15: signal = 'BUY'
    elif total >= 11: signal = 'WEAK BUY'
    elif total >= 7:  signal = 'WATCH'
    elif total >= 0:  signal = 'NO TRADE'
    else:             signal = 'DANGER'

    # Setup type
    if resist and price >= resist * 0.998:                             setup = 'Breakout'
    elif resist and price >= resist * 0.97 and chg_pct > 1:           setup = 'Near Breakout'
    elif support and price <= support * 1.025 and chg_pct >= 0:       setup = 'Support Bounce'
    elif sma20 and abs(price - sma20) / sma20 < 0.015 and chg_pct > 0: setup = 'Pullback Buy'
    elif chg_pct > 1.5 and vol_sig == 'high':                         setup = 'Momentum'
    elif rsi < 38:                                                     setup = 'Oversold Reversal'
    else:                                                              setup = 'Consolidation'

    # Confidence (0-100)
    conf_score = min(95, max(25, round((total / 23) * 100)))
    layers_pos = sum([l1/5 >= 0.6, l2/7 >= 0.6, l3/6 >= 0.6, l4/5 >= 0.6 if l4 > 0 else False])
    if layers_pos >= 3: conf_score = min(95, conf_score + 8)
    if vol_sig == 'high' and chg_pct > 0: conf_score = min(95, conf_score + 5)

    # Trade levels
    sl_pct = 0.020 if signal == 'STRONG BUY' else 0.025 if signal == 'BUY' else 0.030
    sl = round(price * (1 - sl_pct), 0)
    if support and support > sl and support < price * 0.99:
        sl = round(support * 0.995, 0)
    t1 = round(price * 1.015, 0)
    t2 = round(price * 1.025, 0)
    t3 = round(price * 1.040, 0)
    risk   = round(((price - sl) / price) * 100, 1)
    reward = round(((t2 - price) / price) * 100, 1)
    rr     = round(reward / risk, 1) if risk > 0 else 0

    return {
        "symbol":     d.get('symbol'),
        "name":       d.get('name'),
        "price":      round(price, 2),
        "changePct":  chg_pct,
        "signal":     signal,
        "setup":      setup,
        "total":      round(total, 1),
        "l1": l1, "l2": l2, "l3": l3, "l4": l4,
        "confidence": conf_score,
        "rsi":        round(rsi, 1),
        "volumeSignal": vol_sig,
        "sentiment":  sent,
        "entry":      price,
        "sl":         sl,
        "t1":         t1,
        "t2":         t2,
        "t3":         t3,
        "rr":         rr,
        "risk_pct":   risk,
        "reward_pct": reward,
    }


# ── END-OF-DAY BATCH SCAN ─────────────────────────────────────────────────────
def run_batch_scan():
    """
    Scans all 150+ NSE stocks using 5-layer logic.
    Runs at 3:30 PM IST (15:30) and saves top picks to disk.
    Also runs on server startup for immediate results.
    """
    print("\n  [BATCH SCAN] Starting end-of-day scan…")
    # Use all liquid stocks from NSE_STOCKS (skip ETFs for main scan)
    candidates = [s["symbol"] for s in NSE_STOCKS if s.get("sector") != "ETF"]
    print(f"  [BATCH SCAN] Universe: {len(candidates)} stocks")

    # Fetch in batches of 5 (slightly larger batch for batch scan)
    all_data = {}
    BATCH = 5
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i:i+BATCH]
        results = fetch_yahoo_multi(batch, "30d")
        all_data.update(results)
        time.sleep(0.3)  # gentle rate limiting

    print(f"  [BATCH SCAN] Fetched {len(all_data)} stocks, scoring…")

    scored = []
    for sym, d in all_data.items():
        result = score_stock_python(d)
        if result and result['signal'] in ('STRONG BUY', 'BUY', 'WEAK BUY'):
            scored.append(result)

    # Sort by confidence desc, then total score
    scored.sort(key=lambda x: (-x['confidence'], -x['total']))
    top_picks = scored[:10]

    # Add ETF scan
    etf_syms = [s["symbol"] for s in NSE_STOCKS if s.get("sector") == "ETF"]
    etf_data = fetch_yahoo_multi(etf_syms, "30d")
    etf_scored = []
    for sym, d in etf_data.items():
        result = score_stock_python(d)
        if result and result['signal'] in ('STRONG BUY', 'BUY', 'WEAK BUY'):
            etf_scored.append(result)
    etf_scored.sort(key=lambda x: (-x['confidence'], -x['total']))

    final = {
        "stocks":    top_picks,
        "etfs":      etf_scored[:3],
        "total_scanned": len(all_data),
        "total_qualified": len(scored),
        "scan_time": time.strftime("%Y-%m-%d %H:%M IST"),
        "scan_date": time.strftime("%Y-%m-%d"),
    }
    save_scan_results(final)
    print(f"  [BATCH SCAN] Done. {len(scored)} qualified, top {len(top_picks)} saved.")
    return final


def batch_scan_scheduler():
    """Background thread — runs batch scan at 3:30 PM IST every day."""
    print("  [SCHEDULER] Batch scan scheduler started.")
    while True:
        try:
            now = datetime.datetime.now()
            # IST = UTC+5:30. We check local time assuming server runs in IST
            # (Render servers run UTC — adjust: 15:30 IST = 10:00 UTC)
            target_hour, target_min = 15, 30
            # Try to detect timezone — if UTC offset is 0, use UTC equivalent
            import time as _t
            utc_offset = -(_t.timezone if not _t.daylight else _t.altzone) // 3600
            if utc_offset == 0:  # UTC server (Render)
                target_hour, target_min = 10, 0  # 3:30 PM IST = 10:00 AM UTC

            if now.hour == target_hour and now.minute == target_min:
                print(f"  [SCHEDULER] 3:30 PM IST trigger — running batch scan…")
                run_batch_scan()
                time.sleep(90)  # avoid double-trigger within same minute
            else:
                time.sleep(30)  # check every 30 seconds
        except Exception as e:
            print(f"  [SCHEDULER] Error: {e}")
            time.sleep(60)


# ── PRICE ALERT MONITOR ────────────────────────────────────────────────────────
def check_alerts():
    """
    Background thread — checks price alerts every 5 minutes.
    Fetches current price for each alerted symbol and fires if condition met.
    """
    print("  [ALERTS] Price alert monitor started.")
    while True:
        try:
            alerts = load_alerts()
            if not alerts:
                time.sleep(300); continue

            # Get unique symbols
            syms = list(set(a['symbol'] for a in alerts if not a.get('triggered')))
            if not syms:
                time.sleep(300); continue

            # Fetch current prices (use cache — 5-min TTL fits perfectly)
            prices = {}
            for sym in syms:
                d = cache_get(f"{sym}:5d") or cache_get(f"{sym}:30d")
                if d and d.get('status') == 'ok':
                    prices[sym] = d.get('price', 0)
                else:
                    try:
                        d = fetch_yahoo(sym, "5d")
                        if d.get('status') == 'ok':
                            prices[sym] = d.get('price', 0)
                    except:
                        pass

            # Check each alert
            updated = False
            for alert in alerts:
                if alert.get('triggered'): continue
                sym   = alert.get('symbol')
                price = prices.get(sym, 0)
                if not price: continue

                cond  = alert.get('condition')  # 'above' or 'below'
                target = float(alert.get('target', 0))
                fired  = (cond == 'above' and price >= target) or                          (cond == 'below' and price <= target)

                if fired:
                    alert['triggered']     = True
                    alert['triggered_at']  = time.strftime("%Y-%m-%d %H:%M IST")
                    alert['triggered_price'] = price
                    add_triggered({
                        "symbol":  sym,
                        "name":    alert.get('name', sym),
                        "condition": cond,
                        "target":  target,
                        "price":   price,
                        "message": f"{sym} hit ₹{price:.2f} ({'above' if cond=='above' else 'below'} your alert of ₹{target})",
                        "time":    time.strftime("%H:%M IST"),
                    })
                    print(f"  [ALERTS] 🔔 {sym} triggered! Price ₹{price} {cond} ₹{target}")
                    updated = True

            if updated:
                save_alerts(alerts)

            time.sleep(300)  # check every 5 minutes
        except Exception as e:
            print(f"  [ALERTS] Error: {e}")
            time.sleep(60)


# ── HTTP HANDLER ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a): print(f"  -> \"{fmt%a}\"")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Accept,X-Requested-With")

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, fp, ct):
        # Strip static/ prefix — all files are in root on Render
        path = fp.replace("static/", "").replace("static\\", "")
        # Also try original path as fallback
        for p in [path, fp]:
            try:
                with open(p, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                continue
        print(f"  [404] File not found: {fp}")
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length","0")
        self.end_headers()

    def do_GET(self):
        p    = urlparse(self.path)
        path = p.path
        flat = {k:v[0] for k,v in parse_qs(p.query).items()}

        if path in ("/","/index.html"):
            self.send_file("static/index.html","text/html; charset=utf-8"); return

        if path == "/manifest.json":
            self.send_file("static/manifest.json","application/manifest+json"); return

        if path == "/sw.js":
            self.send_file("static/sw.js","application/javascript"); return

        if path == "/app.js":
            self.send_file("app.js","application/javascript"); return

        if path in ("/icon-192.png","/icon-512.png"):
            self.send_file("static"+path,"image/png"); return

        if path == "/health":
            self.send_json({"status":"ok","api_key_set":bool(API_KEY),
                "stocks":"Yahoo Finance","commodities":"Twelve Data"}); return

        # single stock with full indicators
        if path == "/stock":
            sym = flat.get("symbol","").strip()
            if not sym: self.send_json({"error":"Missing symbol"},400); return
            clean = sym.upper().replace(".NS","").replace(".BO","").strip()
            # Use master fetcher — handles cache + TD + Yahoo automatically
            data = fetch_stocks_master([clean])
            result = data.get(clean) or {"symbol":clean,"status":"error","error":"Could not load stock"}
            self.send_json(result); return

        # multiple stocks — Master fetcher (TD batch time_series + Yahoo fallback)
        if path == "/stocks":
            raw = flat.get("symbols","").strip()
            if not raw: self.send_json({"error":"Missing symbols"},400); return
            syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()][:20]
            result = fetch_stocks_master(syms)
            self.send_json(result); return

        # stock news — with impact scoring
        if path == "/news":
            sym = flat.get("symbol","").strip()
            if not sym: self.send_json({"error":"Missing symbol"},400); return
            self.send_json({"news": fetch_news(sym, int(flat.get("limit","10")))}); return

        # ── MARKET-WIDE NEWS ──────────────────────────────────────────────
        if path == "/market-news":
            news  = fetch_market_news()
            pos   = [n for n in news if n["sentiment"]=="positive"]
            neg   = [n for n in news if n["sentiment"]=="negative"]
            neu   = [n for n in news if n["sentiment"]=="neutral"]
            # Build AI summary from headlines
            tone  = "Bullish" if len(pos)>len(neg)+2 else ("Bearish" if len(neg)>len(pos)+2 else "Mixed")
            summary_pts = []
            if pos: summary_pts.append(f"Positive: {pos[0]['title'][:100]}")
            if neg: summary_pts.append(f"Caution: {neg[0]['title'][:100]}")
            summary_pts.append(f"{len(pos)} positive, {len(neg)} negative, {len(neu)} neutral headlines today.")
            self.send_json({
                "news": news,
                "summary": {
                    "tone":        tone,
                    "positive":    len(pos),
                    "negative":    len(neg),
                    "neutral":     len(neu),
                    "total":       len(news),
                    "points":      summary_pts,
                    "timestamp":   time.strftime("%Y-%m-%d %H:%M IST")
                }
            }); return

        # stock of the day — analyze all watched stocks, pick best
        if path == "/stock-of-day":
            raw = flat.get("symbols","RELIANCE,TCS,INFY,HDFCBANK,BAJFINANCE,ITC,WIPRO,TATAMOTORS")
            syms = [s.strip() for s in raw.split(",") if s.strip()][:8]
            all_data = fetch_stocks_master(syms)
            best = None; best_score = -1
            for sym, d in all_data.items():
                if d.get("status") != "ok": continue
                score = d.get("confidence", 0)
                if d.get("recommendation") == "BUY": score += 20
                if d.get("indicators",{}).get("volumeSignal") == "high": score += 10
                if score > best_score:
                    best_score = score; best = d
            self.send_json({"stockOfDay": best, "allAnalyzed": len(all_data)}); return

        # commodities — Gold/Silver/Copper via gold-api.com (free, no key, CORS-ok)
        # USD/INR via Twelve Data (works on free plan)
        if path == "/commodities":
            out = {}
            # gold-api.com supports XAU, XAG, XCU — free, no key needed
            for name, sym in {"gold":"XAU", "silver":"XAG", "copper":"XCU"}.items():
                try:
                    url = f"https://api.gold-api.com/price/{sym}"
                    req = Request(url, headers={"User-Agent":"MarketDashboard/1.0","Accept":"application/json"})
                    with urlopen(req, timeout=10) as r:
                        raw = r.read().decode()
                        print(f"  [GOLD-API] {sym}: {raw[:120]}")
                        d = json.loads(raw)
                        # gold-api returns: price, prev_close_price, ch, chp, name
                        out[name] = {
                            "close":          d.get("price", 0),
                            "previous_close": d.get("prev_close_price", d.get("price", 0)),
                            "change":         d.get("ch", 0),
                            "percent_change": d.get("chp", 0),
                            "name":           d.get("name", sym),
                            "source":         "gold-api.com",
                            "status":         "ok"
                        }
                except Exception as e:
                    print(f"  [GOLD-API] Error {sym}: {e}")
                    out[name] = {"error": str(e), "status": "error"}
            # USD/INR still from Twelve Data (works free)
            out["usdInr"] = fetch_td("/quote", {"symbol":"USD/INR","dp":"2"})
            self.send_json(out); return

        # commodity history chart
        if path == "/history":
            sym        = flat.get("symbol","XAU/USD")
            outputsize = int(flat.get("outputsize","30"))
            # Gold history via Twelve Data (works free)
            if sym == "XAU/USD":
                data = fetch_td("/time_series",{"symbol":sym,"interval":flat.get("interval","1day"),
                    "outputsize":str(outputsize),"dp":"2"})
                self.send_json(data); return
            # Silver/Copper history via gold-api.com
            gapi_sym = "XAG" if "XAG" in sym else "XCU"
            try:
                url = f"https://api.gold-api.com/price/{gapi_sym}/history?period={outputsize}d"
                req = Request(url, headers={"User-Agent":"MarketDashboard/1.0","Accept":"application/json"})
                with urlopen(req, timeout=10) as r:
                    d = json.loads(r.read())
                # Convert to Twelve Data format for frontend compatibility
                values = []
                if isinstance(d, list):
                    for item in d:
                        values.append({"datetime": item.get("date",""), "close": str(item.get("price","0"))})
                elif isinstance(d, dict) and "history" in d:
                    for item in d["history"]:
                        values.append({"datetime": item.get("date",""), "close": str(item.get("price","0"))})
                self.send_json({"meta":{"symbol":sym},"values":values}); return
            except Exception as e:
                # fallback — just return current price as single point
                self.send_json({"meta":{"symbol":sym},"values":[],"error":str(e)}); return

        # ── NSE STOCK SEARCH / AUTOCOMPLETE ──────────────────────────────
        if path == "/search":
            q = flat.get("q","").strip().upper()
            if not q or len(q) < 1:
                self.send_json({"results":[]}); return
            matches = [s for s in NSE_STOCKS if q in s["symbol"] or q in s["name"].upper()][:10]
            self.send_json({"results": matches}); return

        # ── LIVE MARKET DATA — VIX, Nifty, Crude (for STOCKSENSE Layer 1) ──
        if path == "/market-data":
            cached = cache_get("market-data")
            if cached:
                self.send_json(cached); return

            out = {}
            # Fetch all 4 symbols in parallel from Yahoo Finance (free, same as stocks)
            market_syms = {
                "^INDIAVIX": "vix",      # India VIX
                "^NSEI":     "nifty",    # Nifty 50
                "CL=F":      "crude",    # WTI Crude Oil futures
                "^GSPC":     "sp500",    # S&P 500
            }
            lock2 = threading.Lock()

            def fetch_market_sym(yf_sym, key):
                for base in [YF_BASE, YF_BASE2]:
                    try:
                        url = f"{base}/{yf_sym}?interval=1d&range=5d"
                        req = Request(url, headers=YF_HEADERS)
                        with urlopen(req, timeout=8) as r:
                            data = json.loads(r.read())
                        res = data["chart"]["result"][0]
                        meta = res.get("meta", {})
                        price   = float(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
                        prev    = float(meta.get("previousClose") or meta.get("chartPreviousClose") or price)
                        chg     = round(price - prev, 2)
                        chg_pct = round((chg / prev * 100) if prev else 0, 2)
                        with lock2:
                            out[key] = {
                                "price":     round(price, 2),
                                "change":    chg,
                                "changePct": chg_pct,
                                "status":    "ok"
                            }
                        print(f"  [MARKET-DATA] {yf_sym}: {price} ({chg_pct}%)")
                        return
                    except Exception as e:
                        print(f"  [MARKET-DATA] {yf_sym} attempt failed: {e}")
                with lock2:
                    out[key] = {"status": "error"}

            threads2 = [threading.Thread(target=fetch_market_sym, args=(yf, k))
                        for yf, k in market_syms.items()]
            for t in threads2: t.start()
            for t in threads2: t.join(timeout=10)

            # Also include USD/INR from commodities cache or Twelve Data
            try:
                usdInr_d = fetch_td("/quote", {"symbol":"USD/INR","dp":"2"})
                usdInr_price = float(usdInr_d.get("close") or usdInr_d.get("price") or 84)
                out["usdInr"] = {"price": usdInr_price, "status": "ok"}
            except:
                out["usdInr"] = {"price": 84.0, "status": "fallback"}

            cache_set("market-data", out)
            self.send_json(out); return

        # ── MARKET SCANNER with all 5 signals ────────────────────────────
        if path == "/scan":
            watchlist = [s.strip().upper() for s in flat.get("watchlist","").split(",") if s.strip()]
            candidates = [s["symbol"] for s in NSE_STOCKS if s["symbol"] not in watchlist][:10]
            print(f"\n  [SCAN] Scanning {len(candidates)} stocks…")
            results = fetch_stocks_master(candidates[:8])
            opportunities = []
            for sym, d in results.items():
                if d.get("status") != "ok": continue
                ind    = d.get("indicators", {})
                price  = d.get("price", 0)
                prev   = d.get("prevClose", price)
                vol_sig= ind.get("volumeSignal","normal")
                rsi    = ind.get("rsi") or 50
                support    = ind.get("support") or 0
                resistance = ind.get("resistance") or 0
                change_pct = d.get("changePct", 0)
                news_score = 0  # will be estimated from price momentum

                signals = []
                score   = d.get("confidence", 50)

                # 1️⃣ VOLUME SPIKE
                if vol_sig == "high":
                    score += 20
                    signals.append({"type":"volume_spike","label":"🔥 Volume Spike","detail":"Unusual volume — big players entering. Price move likely to sustain."})

                # 2️⃣ RESISTANCE BREAKOUT
                if resistance and price and price >= resistance * 0.998:
                    score += 25
                    signals.append({"type":"breakout","label":"🚀 Resistance Breakout","detail":f"Price breaking ₹{resistance} resistance — often leads to 2–4% quick move."})
                elif resistance and price and price >= resistance * 0.97:
                    score += 10
                    signals.append({"type":"near_breakout","label":"⚡ Near Breakout","detail":f"Price approaching resistance ₹{resistance} — watch for breakout."})

                # 3️⃣ SUPPORT BOUNCE
                if support and price and price <= support * 1.02 and change_pct > 0:
                    score += 20
                    signals.append({"type":"support_bounce","label":"📈 Support Bounce","detail":f"Price bouncing from support ₹{support} — low-risk entry with tight stop."})

                # 4️⃣ POSITIVE MOMENTUM (proxy for news sentiment)
                if change_pct > 2:
                    score += 15
                    signals.append({"type":"momentum","label":"📰 Strong Momentum","detail":f"Up {change_pct:.1f}% today — could be driven by positive news or sector move."})
                elif change_pct > 1:
                    score += 8
                    signals.append({"type":"momentum_mild","label":"📊 Positive Momentum","detail":f"Up {change_pct:.1f}% today — mild bullish signal."})

                # 5️⃣ F&O PROXY — oversold + volume = short covering signal
                if rsi < 38 and vol_sig == "high":
                    score += 20
                    signals.append({"type":"fno","label":"⚡ Short Covering (F&O)","detail":f"RSI {rsi} oversold + high volume = likely short covering in F&O. Quick upside possible."})
                elif rsi < 42:
                    score += 10
                    signals.append({"type":"oversold","label":"🔄 Oversold Reversal","detail":f"RSI {rsi} — oversold, reversal likely in 2–3 days."})

                # RSI buy zone
                if rsi < 35:
                    score += 15
                elif rsi > 70:
                    score -= 15

                if d.get("recommendation") == "BUY":   score += 15
                if d.get("recommendation") == "AVOID": score -= 25

                d["scanScore"]  = min(score, 98)
                d["scanSignals"] = signals
                if score >= 55 and signals:
                    opportunities.append(d)

            opportunities.sort(key=lambda x: x["scanScore"], reverse=True)
            self.send_json({"opportunities": opportunities[:6], "scanned": len(results)}); return

        # ── COMMODITY FUTURES (spot + carry calculation) ──────────────────
        if path == "/futures":
            out = {}
            spot_cache = {}
            for sym_key in ["XAU","XAG","XCU"]:
                try:
                    url = f"https://api.gold-api.com/price/{sym_key}"
                    req = Request(url, headers={"User-Agent":"MarketDashboard/1.0","Accept":"application/json"})
                    with urlopen(req, timeout=10) as r:
                        d = json.loads(r.read())
                        spot_cache[sym_key] = d
                except Exception as e:
                    print(f"  [FUTURES] Error {sym_key}: {e}")
                    spot_cache[sym_key] = {}

            try:
                usdInr_d = fetch_td("/quote", {"symbol":"USD/INR","dp":"2"})
                usdInr = float(usdInr_d.get("close") or usdInr_d.get("price") or 84)
            except:
                usdInr = 84.0

            CARRY_RATE = 0.065
            months_near = 1/12
            months_far  = 3/12

            COMM_META = {
                "XAU": {
                    "name":"Gold","intl_unit":"oz","mcx_unit":"10g","lot_size":1,
                    "mcx_spot":  lambda p: round((p/31.1035)*usdInr*10*1.1575, 0),
                    "mcx_near":  lambda p: round((p*(1+CARRY_RATE*months_near)/31.1035)*usdInr*10*1.1575, 0),
                    "mcx_far":   lambda p: round((p*(1+CARRY_RATE*months_far)/31.1035)*usdInr*10*1.1575, 0),
                    "lot_value": lambda p: round((p/31.1035)*usdInr*10*1.1575*100, 0),  # 100g per lot
                    "exchange":  "MCX Gold (100g lot)"
                },
                "XAG": {
                    "name":"Silver","intl_unit":"oz","mcx_unit":"kg","lot_size":30,
                    "mcx_spot":  lambda p: round((p/31.1035)*usdInr*1000*1.1375, 0),
                    "mcx_near":  lambda p: round((p*(1+CARRY_RATE*months_near)/31.1035)*usdInr*1000*1.1375, 0),
                    "mcx_far":   lambda p: round((p*(1+CARRY_RATE*months_far)/31.1035)*usdInr*1000*1.1375, 0),
                    "lot_value": lambda p: round((p/31.1035)*usdInr*1000*1.1375*30, 0),  # 30kg per lot
                    "exchange":  "MCX Silver (30kg lot)"
                },
                "XCU": {
                    "name":"Copper","intl_unit":"lb","mcx_unit":"kg","lot_size":2500,
                    # MCX copper: LME price ($/tonne) × USD/INR ÷ 1000 × import duty factor
                    # 1 lb = 0.453592 kg, so $/lb × 2204.62 = $/tonne
                    "mcx_spot":  lambda p: round((p/0.453592)*usdInr*1.05, 0),
                    "mcx_near":  lambda p: round((p*(1+CARRY_RATE*months_near)/0.453592)*usdInr*1.05, 0),
                    "mcx_far":   lambda p: round((p*(1+CARRY_RATE*months_far)/0.453592)*usdInr*1.05, 0),
                    "lot_value": lambda p: round((p/0.453592)*usdInr*1.05*2500, 0),  # 2500kg per lot
                    "exchange":  "MCX Copper (2500kg lot)"
                }
            }

            for sym_key, d in spot_cache.items():
                if not d: continue
                spot = float(d.get("price", 0))
                prev = float(d.get("prev_close_price", spot))
                ch   = float(d.get("ch", 0))
                chp  = float(d.get("chp", 0))
                if not spot: continue
                meta = COMM_META[sym_key]
                near_usd = round(spot * (1 + CARRY_RATE * months_near), 2)
                far_usd  = round(spot * (1 + CARRY_RATE * months_far),  2)
                mcx_s = meta["mcx_spot"](spot)
                mcx_n = meta["mcx_near"](spot)
                mcx_f = meta["mcx_far"](spot)
                out[sym_key] = {
                    "name":      meta["name"],
                    "intl_unit": meta["intl_unit"],
                    "mcx_unit":  meta["mcx_unit"],
                    "lot_size":  meta["lot_size"],
                    "exchange":  meta["exchange"],
                    "spot_usd":  spot,
                    "near_usd":  near_usd,
                    "far_usd":   far_usd,
                    "change":    ch,
                    "change_pct": chp,
                    "prev_close": prev,
                    "mcx_spot":  mcx_s,
                    "mcx_near":  mcx_n,
                    "mcx_far":   mcx_f,
                    "mcx_lot_value": meta["lot_value"](spot),
                    "usd_inr":   usdInr,
                    "basis":     round(near_usd - spot, 2),
                    "contango":  near_usd > spot,
                    "status":    "ok"
                }
            self.send_json(out); return

        # ── ETF / GOLD-SILVER BEES STOCKS ─────────────────────────────────
        if path == "/etfs":
            etf_syms = ["GOLDBEES","SILVERBEES","COPPERBEES","LIQUIDBEES","NIFTYBEES","JUNIORBEES"]
            results = fetch_yahoo_multi(etf_syms, "10d")
            self.send_json(results); return

        # ── FAST SCAN — returns top picks scored server-side (fast!) ──
        if path == "/scan-fast":
            mode      = flat.get("mode", "nse")
            watchlist = [s.strip().upper() for s in flat.get("watchlist","").split(",") if s.strip()]

            # Check result cache first (30 min TTL)
            cache_key = f"scan-fast-{mode}-v2"
            cached = cache_get(cache_key)
            if cached:
                print(f"  [SCAN-FAST] Cache HIT for mode={mode}")
                self.send_json(cached); return
            print(f"  [SCAN-FAST] Cache MISS for mode={mode} — fetching fresh")

            # SMALL universe — only fetch what we need, fast
            # Key insight: top 15 most liquid = best signals anyway
            TOP_LIQUID = [
                "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","SBIN",
                "BAJFINANCE","HINDUNILVR","ITC","BHARTIARTL","KOTAKBANK",
                "WIPRO","AXISBANK","MARUTI","TATAMOTORS"
            ]
            NSE_30 = TOP_LIQUID + [
                "SUNPHARMA","LT","HCLTECH","JSWSTEEL","TATASTEEL",
                "NTPC","ONGC","POWERGRID","ADANIENT","DRREDDY",
                "CIPLA","TITAN","NESTLEIND","TECHM","HINDALCO"
            ]
            ETF_LIST = ["GOLDBEES","SILVERBEES","NIFTYBEES","JUNIORBEES","COPPERBEES"]

            if mode == "etf":
                candidates = ETF_LIST
            elif mode == "watchlist":
                candidates = watchlist[:15] if watchlist else TOP_LIQUID[:10]
            elif mode == "all":
                candidates = NSE_30 + ETF_LIST  # 35 stocks max
            else:  # nse — top 30 only
                candidates = NSE_30

            print(f"  [SCAN-FAST] mode={mode}, candidates={len(candidates)}")

            # ── 3-LAYER FAST FETCH STRATEGY ───────────────────────
            # Layer A: Already in memory cache → instant (0ms)
            # Layer B: Twelve Data batch quote → 1 API call (2-3s)  
            # Layer C: Yahoo Finance for remaining → parallel (10-15s max)
            # Result: Most stocks from cache/TD, few from Yahoo = FAST

            all_data = {}

            # Use master fetcher — handles cache + TD batch + Yahoo fallback
            all_data = fetch_stocks_master(candidates)
            print(f"  [SCAN-FAST] Master fetcher: {len(all_data)}/{len(candidates)} stocks ready")

            # Score each stock
            scored = []
            for sym, d in all_data.items():
                result = score_stock_python(d)
                if result:
                    scored.append(result)

            scored.sort(key=lambda x: (-x["confidence"], -x["total"]))
            buys   = [s for s in scored if s["signal"] in ("STRONG BUY","BUY","WEAK BUY")]
            others = [s for s in scored if s["signal"] not in ("STRONG BUY","BUY","WEAK BUY")]

            result = {
                "picks":     buys[:10],
                "others":    others[:5],
                "scanned":   len(all_data),
                "qualified": len(buys),
                "mode":      mode,
                "timestamp": time.strftime("%H:%M IST"),
                "data_source": "twelvedata+yahoo" if API_KEY else "yahoo"
            }

            # Cache result for 30 minutes — next click is INSTANT
            cache_set(cache_key, result)
            print(f"  [SCAN-FAST] Done. Cached for 30 min. Next click = instant.")
            self.send_json(result); return

        # ── BATCH SCAN RESULTS ───────────────────────────────────────
        if path == "/batch-scan":
            force = flat.get("force","").lower() == "true"
            if force:
                # Run scan in background thread, return immediately
                t = threading.Thread(target=run_batch_scan, daemon=True)
                t.start()
                self.send_json({"status":"scanning","message":"Batch scan started. Check /batch-scan in ~3 minutes."}); return
            results = load_scan_results()
            self.send_json(results); return

        # ── PRICE ALERTS CRUD ────────────────────────────────────────
        if path == "/alerts":
            action = flat.get("action","list")

            if action == "list":
                alerts = load_alerts()
                triggered = get_triggered()
                self.send_json({"alerts": alerts, "triggered": triggered}); return

            if action == "add":
                sym    = flat.get("symbol","").strip().upper().replace(".NS","").replace(".BO","")
                target = float(flat.get("target","0") or 0)
                cond   = flat.get("condition","above")  # 'above' or 'below'
                name   = flat.get("name", sym)
                if not sym or not target:
                    self.send_json({"error":"Missing symbol or target"},400); return
                alerts = load_alerts()
                # Check duplicate
                existing = [a for a in alerts if a['symbol']==sym and a['condition']==cond and float(a['target'])==target]
                if existing:
                    self.send_json({"error":"Alert already exists"}); return
                new_alert = {
                    "id":        f"{sym}_{cond}_{target}_{int(time.time())}",
                    "symbol":    sym,
                    "name":      name,
                    "condition": cond,
                    "target":    target,
                    "created":   time.strftime("%Y-%m-%d %H:%M IST"),
                    "triggered": False,
                }
                alerts.append(new_alert)
                save_alerts(alerts)
                print(f"  [ALERTS] Added: {sym} {cond} ₹{target}")
                self.send_json({"status":"ok","alert":new_alert}); return

            if action == "delete":
                alert_id = flat.get("id","")
                alerts = load_alerts()
                alerts = [a for a in alerts if a.get('id') != alert_id]
                save_alerts(alerts)
                self.send_json({"status":"ok"}); return

            if action == "clear_triggered":
                clear_triggered()
                alerts = load_alerts()
                for a in alerts:
                    if a.get('triggered'): a['triggered'] = False
                save_alerts(alerts)
                self.send_json({"status":"ok"}); return

            self.send_json({"error":"Unknown action"}); return

        # ── SCAN UNIVERSE INFO ────────────────────────────────────────
        if path == "/universe":
            sectors = {}
            for s in NSE_STOCKS:
                sec = s.get('sector','Other')
                sectors.setdefault(sec, []).append(s['symbol'])
            self.send_json({
                "total": len(NSE_STOCKS),
                "sectors": sectors,
                "symbols": [s['symbol'] for s in NSE_STOCKS]
            }); return

        self.send_json({"error":f"Unknown: {path}"},404)

# ── FULL NSE STOCK LIST FOR AUTOCOMPLETE ─────────────────────────────────────
NSE_STOCKS = [
    {"symbol":"RELIANCE",    "name":"Reliance Industries",        "sector":"Energy"},
    {"symbol":"TCS",         "name":"Tata Consultancy Services",  "sector":"IT"},
    {"symbol":"HDFCBANK",    "name":"HDFC Bank",                  "sector":"Banking"},
    {"symbol":"INFY",        "name":"Infosys",                    "sector":"IT"},
    {"symbol":"ICICIBANK",   "name":"ICICI Bank",                 "sector":"Banking"},
    {"symbol":"HINDUNILVR",  "name":"Hindustan Unilever",         "sector":"FMCG"},
    {"symbol":"ITC",         "name":"ITC Limited",                "sector":"FMCG"},
    {"symbol":"SBIN",        "name":"State Bank of India",        "sector":"Banking"},
    {"symbol":"BAJFINANCE",  "name":"Bajaj Finance",              "sector":"Finance"},
    {"symbol":"BHARTIARTL",  "name":"Bharti Airtel",              "sector":"Telecom"},
    {"symbol":"KOTAKBANK",   "name":"Kotak Mahindra Bank",        "sector":"Banking"},
    {"symbol":"WIPRO",       "name":"Wipro",                      "sector":"IT"},
    {"symbol":"AXISBANK",    "name":"Axis Bank",                  "sector":"Banking"},
    {"symbol":"MARUTI",      "name":"Maruti Suzuki",              "sector":"Auto"},
    {"symbol":"TATAMOTORS",  "name":"Tata Motors",                "sector":"Auto"},
    {"symbol":"SUNPHARMA",   "name":"Sun Pharmaceutical",         "sector":"Pharma"},
    {"symbol":"ULTRACEMCO",  "name":"UltraTech Cement",           "sector":"Cement"},
    {"symbol":"TITAN",       "name":"Titan Company",              "sector":"Consumer"},
    {"symbol":"NESTLEIND",   "name":"Nestle India",               "sector":"FMCG"},
    {"symbol":"TECHM",       "name":"Tech Mahindra",              "sector":"IT"},
    {"symbol":"POWERGRID",   "name":"Power Grid Corporation",     "sector":"Utilities"},
    {"symbol":"NTPC",        "name":"NTPC Limited",               "sector":"Energy"},
    {"symbol":"ONGC",        "name":"Oil & Natural Gas Corp",     "sector":"Energy"},
    {"symbol":"HCLTECH",     "name":"HCL Technologies",           "sector":"IT"},
    {"symbol":"JSWSTEEL",    "name":"JSW Steel",                  "sector":"Metals"},
    {"symbol":"TATASTEEL",   "name":"Tata Steel",                 "sector":"Metals"},
    {"symbol":"HINDALCO",    "name":"Hindalco Industries",        "sector":"Metals"},
    {"symbol":"ADANIENT",    "name":"Adani Enterprises",          "sector":"Conglomerate"},
    {"symbol":"ADANIPORTS",  "name":"Adani Ports",                "sector":"Infrastructure"},
    {"symbol":"BAJAJFINSV",  "name":"Bajaj Finserv",              "sector":"Finance"},
    {"symbol":"LT",          "name":"Larsen & Toubro",            "sector":"Infrastructure"},
    {"symbol":"DMART",       "name":"Avenue Supermarts (DMart)",  "sector":"Retail"},
    {"symbol":"ZOMATO",      "name":"Zomato",                     "sector":"Consumer Tech"},
    {"symbol":"PAYTM",       "name":"One97 Communications (Paytm)","sector":"Fintech"},
    {"symbol":"NYKAA",       "name":"FSN E-Commerce (Nykaa)",     "sector":"Consumer Tech"},
    {"symbol":"POLICYBZR",   "name":"PB Fintech (PolicyBazaar)",  "sector":"Fintech"},
    {"symbol":"REDINGTON",   "name":"Redington India",            "sector":"IT Distribution"},
    {"symbol":"DRREDDY",     "name":"Dr. Reddy's Laboratories",   "sector":"Pharma"},
    {"symbol":"CIPLA",       "name":"Cipla",                      "sector":"Pharma"},
    {"symbol":"DIVISLAB",    "name":"Divi's Laboratories",        "sector":"Pharma"},
    {"symbol":"APOLLOHOSP",  "name":"Apollo Hospitals",           "sector":"Healthcare"},
    {"symbol":"ASIANPAINT",  "name":"Asian Paints",               "sector":"Consumer"},
    {"symbol":"BERGERPAINTS","name":"Berger Paints",              "sector":"Consumer"},
    {"symbol":"PIDILITIND",  "name":"Pidilite Industries",        "sector":"Chemical"},
    {"symbol":"BOSCHLTD",    "name":"Bosch",                      "sector":"Auto Ancillary"},
    {"symbol":"HDFCLIFE",    "name":"HDFC Life Insurance",        "sector":"Insurance"},
    {"symbol":"SBILIFE",     "name":"SBI Life Insurance",         "sector":"Insurance"},
    {"symbol":"ICICIGI",     "name":"ICICI Lombard Insurance",    "sector":"Insurance"},
    {"symbol":"IRCTC",       "name":"Indian Railway Catering (IRCTC)","sector":"Travel"},
    {"symbol":"INDIGO",      "name":"IndiGo (InterGlobe Aviation)","sector":"Aviation"},
    {"symbol":"TATAPOWER",   "name":"Tata Power",                 "sector":"Energy"},
    {"symbol":"HAVELLS",     "name":"Havells India",              "sector":"Electricals"},
    {"symbol":"VOLTAS",      "name":"Voltas",                     "sector":"Consumer"},
    {"symbol":"MUTHOOTFIN",  "name":"Muthoot Finance",            "sector":"Finance"},
    {"symbol":"CHOLAFIN",    "name":"Cholamandalam Finance",      "sector":"Finance"},
    {"symbol":"BANDHANBNK",  "name":"Bandhan Bank",               "sector":"Banking"},
    {"symbol":"FEDERALBNK",  "name":"Federal Bank",               "sector":"Banking"},
    {"symbol":"IDFCFIRSTB",  "name":"IDFC First Bank",            "sector":"Banking"},
    {"symbol":"PNB",         "name":"Punjab National Bank",       "sector":"Banking"},
    {"symbol":"CANBK",       "name":"Canara Bank",                "sector":"Banking"},
    {"symbol":"BANKBARODA",  "name":"Bank of Baroda",             "sector":"Banking"},
    {"symbol":"JINDALSTEL",  "name":"Jindal Steel & Power",       "sector":"Metals"},
    {"symbol":"SAIL",        "name":"Steel Authority of India",   "sector":"Metals"},
    {"symbol":"COALINDIA",   "name":"Coal India",                 "sector":"Mining"},
    {"symbol":"GRASIM",      "name":"Grasim Industries",          "sector":"Cement"},
    {"symbol":"SHREECEM",    "name":"Shree Cement",               "sector":"Cement"},
    {"symbol":"AMBUJACEM",   "name":"Ambuja Cements",             "sector":"Cement"},
    {"symbol":"DABUR",       "name":"Dabur India",                "sector":"FMCG"},
    {"symbol":"MARICO",      "name":"Marico",                     "sector":"FMCG"},
    {"symbol":"GODREJCP",    "name":"Godrej Consumer Products",   "sector":"FMCG"},
    {"symbol":"MCDOWELL-N",  "name":"United Spirits",             "sector":"Beverages"},
    {"symbol":"JUBLFOOD",    "name":"Jubilant Foodworks",         "sector":"Food"},
    {"symbol":"TRENT",       "name":"Trent (Tata Retail)",        "sector":"Retail"},
    {"symbol":"VEDL",        "name":"Vedanta",                    "sector":"Metals"},
    {"symbol":"NMDC",        "name":"NMDC Steel",                 "sector":"Metals"},
    {"symbol":"MOTHERSON",   "name":"Samvardhana Motherson",      "sector":"Auto Ancillary"},
    {"symbol":"BALKRISIND",  "name":"Balkrishna Industries",      "sector":"Tyres"},
    {"symbol":"APOLLOTYRE",  "name":"Apollo Tyres",               "sector":"Tyres"},
    {"symbol":"CEAT",        "name":"CEAT Tyres",                 "sector":"Tyres"},
    {"symbol":"MFSL",        "name":"Max Financial Services",     "sector":"Insurance"},
    {"symbol":"PERSISTENT",  "name":"Persistent Systems",         "sector":"IT"},
    {"symbol":"MPHASIS",     "name":"Mphasis",                    "sector":"IT"},
    {"symbol":"LTIM",        "name":"LTIMindtree",                "sector":"IT"},
    {"symbol":"COFORGE",     "name":"Coforge",                    "sector":"IT"},
    {"symbol":"KPITTECH",    "name":"KPIT Technologies",          "sector":"IT"},
    {"symbol":"ZEEL",        "name":"Zee Entertainment",          "sector":"Media"},
    {"symbol":"SUNTV",       "name":"Sun TV Network",             "sector":"Media"},
    {"symbol":"PVR",         "name":"PVR INOX",                   "sector":"Entertainment"},
    {"symbol":"INOXWIND",    "name":"Inox Wind",                  "sector":"Renewable Energy"},
    {"symbol":"SUZLON",      "name":"Suzlon Energy",              "sector":"Renewable Energy"},
    {"symbol":"GOLDBEES",    "name":"Nippon Gold ETF (GoldBees)", "sector":"ETF"},
    {"symbol":"SILVERBEES",  "name":"Nippon Silver ETF (SilverBees)","sector":"ETF"},
    {"symbol":"COPPERBEES",  "name":"Nippon Copper ETF",          "sector":"ETF"},
    {"symbol":"NIFTYBEES",   "name":"Nippon Nifty ETF (NiftyBees)","sector":"ETF"},
    {"symbol":"JUNIORBEES",  "name":"Nippon Junior ETF",          "sector":"ETF"},
    {"symbol":"LIQUIDBEES",  "name":"Nippon Liquid ETF",          "sector":"ETF"},

    # ── NIFTY NEXT 50 ──
    {"symbol":"ABB",         "name":"ABB India",                  "sector":"Electricals"},
    {"symbol":"ADANIGREEN",  "name":"Adani Green Energy",         "sector":"Renewable Energy"},
    {"symbol":"ADANITRANS",  "name":"Adani Transmission",         "sector":"Utilities"},
    {"symbol":"ATGL",        "name":"Adani Total Gas",            "sector":"Energy"},
    {"symbol":"AWL",         "name":"Adani Wilmar",               "sector":"FMCG"},
    {"symbol":"ALKEM",       "name":"Alkem Laboratories",         "sector":"Pharma"},
    {"symbol":"AMBUJACEM",   "name":"Ambuja Cements",             "sector":"Cement"},
    {"symbol":"AUROPHARMA",  "name":"Aurobindo Pharma",           "sector":"Pharma"},
    {"symbol":"BAJAJ-AUTO",  "name":"Bajaj Auto",                 "sector":"Auto"},
    {"symbol":"BALKRISIND",  "name":"Balkrishna Industries",      "sector":"Tyres"},
    {"symbol":"BEL",         "name":"Bharat Electronics",         "sector":"Defence"},
    {"symbol":"BHEL",        "name":"Bharat Heavy Electricals",   "sector":"Capital Goods"},
    {"symbol":"BIOCON",      "name":"Biocon",                     "sector":"Pharma"},
    {"symbol":"BPCL",        "name":"BPCL",                       "sector":"Energy"},
    {"symbol":"BSE",         "name":"BSE Limited",                "sector":"Finance"},
    {"symbol":"COLPAL",      "name":"Colgate-Palmolive India",    "sector":"FMCG"},
    {"symbol":"CONCOR",      "name":"Container Corporation",      "sector":"Logistics"},
    {"symbol":"CROMPTON",    "name":"Crompton Greaves Consumer",  "sector":"Electricals"},
    {"symbol":"CUMMINSIND",  "name":"Cummins India",              "sector":"Capital Goods"},
    {"symbol":"DEEPAKNTR",   "name":"Deepak Nitrite",             "sector":"Chemical"},
    {"symbol":"DIXON",       "name":"Dixon Technologies",         "sector":"Electronics"},
    {"symbol":"DLF",         "name":"DLF Limited",                "sector":"Real Estate"},
    {"symbol":"EICHERMOT",   "name":"Eicher Motors",              "sector":"Auto"},
    {"symbol":"GAIL",        "name":"GAIL India",                 "sector":"Energy"},
    {"symbol":"GODREJPROP",  "name":"Godrej Properties",          "sector":"Real Estate"},
    {"symbol":"HDFCAMC",     "name":"HDFC AMC",                   "sector":"Finance"},
    {"symbol":"HEROMOTOCO",  "name":"Hero MotoCorp",              "sector":"Auto"},
    {"symbol":"HINDZINC",    "name":"Hindustan Zinc",             "sector":"Metals"},
    {"symbol":"ICICIGI",     "name":"ICICI Lombard Insurance",    "sector":"Insurance"},
    {"symbol":"ICICIPRULI",  "name":"ICICI Prudential Life",      "sector":"Insurance"},
    {"symbol":"INDUSTOWER",  "name":"Indus Towers",               "sector":"Telecom"},
    {"symbol":"IOC",         "name":"Indian Oil Corporation",     "sector":"Energy"},
    {"symbol":"IRFC",        "name":"Indian Railway Finance Corp","sector":"Finance"},
    {"symbol":"LUPIN",       "name":"Lupin",                      "sector":"Pharma"},
    {"symbol":"MANKIND",     "name":"Mankind Pharma",             "sector":"Pharma"},
    {"symbol":"MAXHEALTH",   "name":"Max Healthcare",             "sector":"Healthcare"},
    {"symbol":"MCX",         "name":"MCX India",                  "sector":"Finance"},
    {"symbol":"MFSL",        "name":"Max Financial Services",     "sector":"Insurance"},
    {"symbol":"NHPC",        "name":"NHPC Limited",               "sector":"Utilities"},
    {"symbol":"NYKAA",       "name":"FSN E-Commerce (Nykaa)",     "sector":"Consumer Tech"},
    {"symbol":"OBEROIRLTY",  "name":"Oberoi Realty",              "sector":"Real Estate"},
    {"symbol":"OFSS",        "name":"Oracle Financial Services",  "sector":"IT"},
    {"symbol":"OIL",         "name":"Oil India",                  "sector":"Energy"},
    {"symbol":"PAGEIND",     "name":"Page Industries",            "sector":"Consumer"},
    {"symbol":"PAYTM",       "name":"One97 Communications (Paytm)","sector":"Fintech"},
    {"symbol":"PIIND",       "name":"PI Industries",              "sector":"Chemical"},
    {"symbol":"POLYCAB",     "name":"Polycab India",              "sector":"Electricals"},
    {"symbol":"PRESTIGE",    "name":"Prestige Estates",           "sector":"Real Estate"},
    {"symbol":"RECLTD",      "name":"REC Limited",                "sector":"Finance"},
    {"symbol":"SAIL",        "name":"Steel Authority of India",   "sector":"Metals"},
    {"symbol":"SBICARD",     "name":"SBI Cards",                  "sector":"Finance"},
    {"symbol":"SHREECEM",    "name":"Shree Cement",               "sector":"Cement"},
    {"symbol":"SIEMENS",     "name":"Siemens India",              "sector":"Capital Goods"},
    {"symbol":"SOLARINDS",   "name":"Solar Industries",           "sector":"Defence"},
    {"symbol":"SRF",         "name":"SRF Limited",                "sector":"Chemical"},
    {"symbol":"STARHEALTH",  "name":"Star Health Insurance",      "sector":"Insurance"},
    {"symbol":"SUPREMEIND",  "name":"Supreme Industries",         "sector":"Plastics"},
    {"symbol":"TATACOMM",    "name":"Tata Communications",        "sector":"Telecom"},
    {"symbol":"TATACONSUM",  "name":"Tata Consumer Products",     "sector":"FMCG"},
    {"symbol":"TRENT",       "name":"Trent (Tata Retail)",        "sector":"Retail"},
    {"symbol":"TRIDENT",     "name":"Trident Limited",            "sector":"Textile"},
    {"symbol":"UNITDSPR",    "name":"United Spirits",             "sector":"Beverages"},
    {"symbol":"VEDL",        "name":"Vedanta",                    "sector":"Metals"},
    {"symbol":"VOLTAS",      "name":"Voltas",                     "sector":"Consumer"},
    {"symbol":"ZYDUSLIFE",   "name":"Zydus Lifesciences",         "sector":"Pharma"},

    # ── HIGH-LIQUIDITY MIDCAP F&O STOCKS ──
    {"symbol":"ABCAPITAL",   "name":"Aditya Birla Capital",       "sector":"Finance"},
    {"symbol":"ABFRL",       "name":"Aditya Birla Fashion",       "sector":"Retail"},
    {"symbol":"ASTRAL",      "name":"Astral Limited",             "sector":"Plastics"},
    {"symbol":"AUROPHARMA",  "name":"Aurobindo Pharma",           "sector":"Pharma"},
    {"symbol":"BALRAMCHIN",  "name":"Balrampur Chini",            "sector":"Sugar"},
    {"symbol":"BATAINDIA",   "name":"Bata India",                 "sector":"Consumer"},
    {"symbol":"BHARATFORG",  "name":"Bharat Forge",               "sector":"Auto Ancillary"},
    {"symbol":"CANFINHOME",  "name":"Can Fin Homes",              "sector":"Finance"},
    {"symbol":"CDSL",        "name":"CDSL",                       "sector":"Finance"},
    {"symbol":"CHAMBLFERT",  "name":"Chambal Fertilizers",        "sector":"Chemicals"},
    {"symbol":"COROMANDEL",  "name":"Coromandel International",   "sector":"Chemicals"},
    {"symbol":"DABUR",       "name":"Dabur India",                "sector":"FMCG"},
    {"symbol":"DELTACORP",   "name":"Delta Corp",                 "sector":"Hospitality"},
    {"symbol":"ESCORTS",     "name":"Escorts Kubota",             "sector":"Auto"},
    {"symbol":"FACT",        "name":"FACT",                       "sector":"Chemicals"},
    {"symbol":"FEDERALBNK",  "name":"Federal Bank",               "sector":"Banking"},
    {"symbol":"GMRINFRA",    "name":"GMR Airports Infrastructure","sector":"Infrastructure"},
    {"symbol":"GNFC",        "name":"Gujarat Narmada Valley",     "sector":"Chemicals"},
    {"symbol":"GRANULES",    "name":"Granules India",             "sector":"Pharma"},
    {"symbol":"GUJGASLTD",   "name":"Gujarat Gas",                "sector":"Energy"},
    {"symbol":"HAL",         "name":"Hindustan Aeronautics",      "sector":"Defence"},
    {"symbol":"HUDCO",       "name":"HUDCO",                      "sector":"Finance"},
    {"symbol":"IDFC",        "name":"IDFC Limited",               "sector":"Finance"},
    {"symbol":"IDFCFIRSTB",  "name":"IDFC First Bank",            "sector":"Banking"},
    {"symbol":"IGL",         "name":"Indraprastha Gas",           "sector":"Energy"},
    {"symbol":"INDIACEM",    "name":"India Cements",              "sector":"Cement"},
    {"symbol":"INDIAMART",   "name":"IndiaMART InterMESH",        "sector":"Consumer Tech"},
    {"symbol":"INDIANB",     "name":"Indian Bank",                "sector":"Banking"},
    {"symbol":"INDUSINDBK",  "name":"IndusInd Bank",              "sector":"Banking"},
    {"symbol":"INOXGREEN",   "name":"INOX Green Energy",          "sector":"Renewable Energy"},
    {"symbol":"IRCTC",       "name":"IRCTC",                      "sector":"Travel"},
    {"symbol":"ITC",         "name":"ITC Limited",                "sector":"FMCG"},
    {"symbol":"JSWENERGY",   "name":"JSW Energy",                 "sector":"Energy"},
    {"symbol":"JUBLFOOD",    "name":"Jubilant Foodworks",         "sector":"Food"},
    {"symbol":"KAJARIACER",  "name":"Kajaria Ceramics",           "sector":"Consumer"},
    {"symbol":"LALPATHLAB",  "name":"Dr Lal PathLabs",            "sector":"Healthcare"},
    {"symbol":"LAURUSLABS",  "name":"Laurus Labs",                "sector":"Pharma"},
    {"symbol":"LICHSGFIN",   "name":"LIC Housing Finance",        "sector":"Finance"},
    {"symbol":"LTTS",        "name":"L&T Technology Services",    "sector":"IT"},
    {"symbol":"M&M",         "name":"Mahindra & Mahindra",        "sector":"Auto"},
    {"symbol":"M&MFIN",      "name":"M&M Financial Services",     "sector":"Finance"},
    {"symbol":"MANAPPURAM",  "name":"Manappuram Finance",         "sector":"Finance"},
    {"symbol":"MARICO",      "name":"Marico",                     "sector":"FMCG"},
    {"symbol":"MCDOWELL-N",  "name":"United Spirits",             "sector":"Beverages"},
    {"symbol":"METROPOLIS",  "name":"Metropolis Healthcare",      "sector":"Healthcare"},
    {"symbol":"MFSL",        "name":"Max Financial Services",     "sector":"Insurance"},
    {"symbol":"MGL",         "name":"Mahanagar Gas",              "sector":"Energy"},
    {"symbol":"MMTC",        "name":"MMTC Limited",               "sector":"Metals"},
    {"symbol":"MOTHERSON",   "name":"Samvardhana Motherson",      "sector":"Auto Ancillary"},
    {"symbol":"MPHASIS",     "name":"Mphasis",                    "sector":"IT"},
    {"symbol":"NAVINFLUOR",  "name":"Navin Fluorine",             "sector":"Chemical"},
    {"symbol":"NIACL",       "name":"New India Assurance",        "sector":"Insurance"},
    {"symbol":"NMDC",        "name":"NMDC Limited",               "sector":"Metals"},
    {"symbol":"PERSISTENT",  "name":"Persistent Systems",         "sector":"IT"},
    {"symbol":"PETRONET",    "name":"Petronet LNG",               "sector":"Energy"},
    {"symbol":"PFC",         "name":"Power Finance Corporation",  "sector":"Finance"},
    {"symbol":"PHOENIXLTD",  "name":"Phoenix Mills",              "sector":"Real Estate"},
    {"symbol":"POLICYBZR",   "name":"PB Fintech (PolicyBazaar)",  "sector":"Fintech"},
    {"symbol":"RAYMOND",     "name":"Raymond Limited",            "sector":"Textile"},
    {"symbol":"RVNL",        "name":"Rail Vikas Nigam",           "sector":"Infrastructure"},
    {"symbol":"SONACOMS",    "name":"Sona BLW Precision",         "sector":"Auto Ancillary"},
    {"symbol":"SUNTV",       "name":"Sun TV Network",             "sector":"Media"},
    {"symbol":"SUZLON",      "name":"Suzlon Energy",              "sector":"Renewable Energy"},
    {"symbol":"TIINDIA",     "name":"Tube Investments",           "sector":"Auto Ancillary"},
    {"symbol":"TORNTPHARM",  "name":"Torrent Pharmaceuticals",    "sector":"Pharma"},
    {"symbol":"TORNTPOWER",  "name":"Torrent Power",              "sector":"Utilities"},
    {"symbol":"TVSMOTOR",    "name":"TVS Motor Company",          "sector":"Auto"},
    {"symbol":"UBL",         "name":"United Breweries",           "sector":"Beverages"},
    {"symbol":"ULTRACEMCO",  "name":"UltraTech Cement",           "sector":"Cement"},
    {"symbol":"UPL",         "name":"UPL Limited",                "sector":"Chemicals"},
    {"symbol":"VBL",         "name":"Varun Beverages",            "sector":"Beverages"},
    {"symbol":"WHIRLPOOL",   "name":"Whirlpool of India",         "sector":"Consumer"},
    {"symbol":"WIPRO",       "name":"Wipro",                      "sector":"IT"},
    {"symbol":"ZOMATO",      "name":"Zomato",                     "sector":"Consumer Tech"},
]

def run():
    port = int(os.environ.get("PORT") or 10000)
    srv  = HTTPServer(("0.0.0.0",port), Handler)
    print("="*60)
    print("  India Market Dashboard - Backend v5")
    print("="*60)
    print(f"  URL      : http://localhost:{port}")
    print(f"  Stocks   : Yahoo Finance + Technical Analysis (free)")
    print(f"  Commod.  : Twelve Data ({'KEY SET' if API_KEY else 'KEY NOT SET'})")
    print(f"  News     : Yahoo Finance RSS (free)")
    print("="*60)
    print("  Press Ctrl+C to stop\n")
    # Start background services
    threading.Thread(target=batch_scan_scheduler, daemon=True).start()
    threading.Thread(target=check_alerts, daemon=True).start()

    print("  [STARTUP] Server ready. Stocks load on first request (Stooq + TD + cached 30min)")

    # Run initial batch scan on startup (in background, non-blocking)
    saved = load_scan_results()
    if not saved.get('results'):
        print("  [STARTUP] No saved scan results — running initial scan in background…")
        threading.Thread(target=run_batch_scan, daemon=True).start()
    else:
        print(f"  [STARTUP] Loaded saved scan from {saved.get('timestamp','unknown')}")

    # ── KEEP-ALIVE PINGER ────────────────────────────────────────
    # Pings /health every 10 minutes so Render never puts server to sleep
    # Free plan sleeps after 15 min inactivity — this prevents that
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")

    def keep_alive():
        if not render_url:
            print("  [KEEP-ALIVE] No RENDER_EXTERNAL_URL set — skipping pinger")
            return
        print(f"  [KEEP-ALIVE] Started — pinging {render_url}/health every 10 min")
        while True:
            time.sleep(600)  # 10 minutes
            try:
                req = Request(f"{render_url}/health",
                              headers={"User-Agent": "KeepAlive/1.0"})
                with urlopen(req, timeout=10) as r:
                    print(f"  [KEEP-ALIVE] Ping OK — server awake")
            except Exception as e:
                print(f"  [KEEP-ALIVE] Ping failed: {e}")

    threading.Thread(target=keep_alive, daemon=True).start()

    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.")

if __name__ == "__main__": run()
