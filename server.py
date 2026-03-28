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
YF_LIB = False  # Using direct Yahoo URL
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
def fetch_yahoo(symbol, range_="30d"):
    clean  = symbol.upper().replace(".NS","").replace(".BO","").strip()
    yf_sym = clean + ".NS"

    # Check cache first
    cache_key = f"{clean}:{range_}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    # Direct Yahoo Finance URL - confirmed working on Render
    print(f"\n  [YAHOO] Fetching {yf_sym}...")
    bases = YF_BASES.copy()
    random.shuffle(bases)
    for attempt, base in enumerate(bases, 1):
        try:
            url = f"{base}/{yf_sym}?interval=1d&range={range_}&includePrePost=false&events=div"
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

            # On holidays: regularMarketPrice may be 0/None — use last close
            price  = float(meta.get("regularMarketPrice") or 0)
            if not price and closes_c:
                price = closes_c[-1]  # last trading day close
            if not price:
                raise ValueError("No price data available")
            prev   = float(meta.get("chartPreviousClose") or meta.get("previousClose") or (closes_c[-2] if len(closes_c)>1 else price))
            volume = int(meta.get("regularMarketVolume") or (volumes_c[-1] if volumes_c else 0))
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

    # Retry with shorter range (5d) - sometimes Yahoo rejects long ranges
    if range_ != "5d":
        print(f"  [YAHOO] Retrying {yf_sym} with range=5d...")
        for base in YF_BASES:
            try:
                url = f"{base}/{yf_sym}?interval=1d&range=5d"
                req = Request(url, headers=get_yf_headers())
                with urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
                meta = data["chart"]["result"][0]["meta"]
                price = float(meta.get("regularMarketPrice") or 0)
                # Holiday fallback: use last close from history
                if not price:
                    q5 = data["chart"]["result"][0]["indicators"]["quote"][0]
                    cl5 = [v for v in q5.get("close",[]) if v]
                    price = cl5[-1] if cl5 else 0
                prev  = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
                if price > 0:
                    chg = round(price - prev, 2)
                    pct = round((chg/prev*100) if prev else 0, 2)
                    result = {
                        "symbol":clean,"name":meta.get("shortName") or clean,
                        "price":round(price,2),"change":chg,"changePct":pct,
                        "prevClose":round(prev,2),"volume":int(meta.get("regularMarketVolume") or 0),
                        "high52w":meta.get("fiftyTwoWeekHigh",0),"low52w":meta.get("fiftyTwoWeekLow",0),
                        "history":[],"indicators":{"rsi":None,"sma20":None,"sma50":None,"ema9":None,
                            "macd":None,"macdSignal":None,"macdHist":None,
                            "support":None,"resistance":None,"volumeSignal":"normal"},
                        "recommendation":"HOLD","sentiment":"Neutral","confidence":50,
                        "reasons":["Limited data — load individual stock for full analysis."],
                        "strategyPoints":[],"tradeLevels":calc_trade_levels(price,None,None,"HOLD",None,None),
                        "status":"ok"
                    }
                    print(f"  [YAHOO] Short-range OK: {clean} ₹{price}")
                    cache_set(cache_key, result)
                    return result
            except Exception as e2:
                print(f"  [YAHOO] Short-range failed: {e2}")

    print(f"  [YAHOO] ALL ATTEMPTS FAILED for {yf_sym}")
    return {"symbol":clean,"status":"error","error":f"Could not load {clean} from Yahoo Finance"}

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


def fetch_stocks_master(symbols):
    """
    MASTER STOCK FETCHER - Yahoo Finance (confirmed working on Render)
    Cache 5 min → Yahoo parallel threads → return
    """
    if not symbols:
        return {}

    syms = [s.upper().replace(".NS","").replace(".BO","").strip() for s in symbols]
    result, need_fetch = {}, []

    for sym in syms:
        cached = cache_get(f"{sym}:master")
        if cached and cached.get("status") == "ok":
            result[sym] = cached
        else:
            need_fetch.append(sym)

    if not need_fetch:
        print(f"  [MASTER] {len(result)} stocks from cache")
        return result

    print(f"  [MASTER] Yahoo fetch: {need_fetch}")

    out, lock = {}, threading.Lock()
    def fetch_one(sym):
        d = fetch_yahoo(sym, "6mo")
        with lock:
            out[sym] = d

    threads = [threading.Thread(target=fetch_one, args=(s,), daemon=True) for s in need_fetch]
    for t in threads: t.start()
    for t in threads: t.join(timeout=25)

    for sym in need_fetch:
        d = out.get(sym, {})
        if d.get("status") == "ok":
            cache_set(f"{sym}:master", d, ttl=300)
            result[sym] = d
            print(f"  [MASTER] OK: {sym} ₹{d.get('price')}")
        else:
            print(f"  [MASTER] FAIL: {sym} error={d.get('error','timeout')}")

    print(f"  [MASTER] Result: {len(result)}/{len(syms)}")
    return result


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
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Accept,X-Requested-With,Authorization")

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

    def do_POST(self):
        """Handle POST — /ai-chat"""
        p    = urlparse(self.path)
        path = p.path
        if path == "/ai-chat":
            self._handle_ai_chat()
        else:
            self.send_json({"error":"POST not supported for this path"},405)

    def _handle_ai_chat(self):
        """Real LLM-powered chat about a stock using Claude API"""
        try:
            content_len = int(self.headers.get('Content-Length',0))
            body = json.loads(self.rfile.read(content_len).decode()) if content_len > 0 else {}
        except:
            body = {}

        question  = (body.get('question') or '').strip()
        context   = (body.get('context')  or '').strip()
        history   = body.get('history', [])

        if not question:
            self.send_json({"answer":"Please ask a question."}); return

        # ── Try Claude API first ─────────────────────────────────────────
        ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")
        if ANTHROPIC_KEY:
            try:
                system_prompt = (
                    "You are STOCKSENSE AI, an expert NSE/BSE stock market analyst specialising in "
                    "short-term BTST (Buy Today Sell Tomorrow) and 2-3 day swing trades on Indian equities. "
                    "You speak like a sharp, experienced trader — concise, direct, numbers-first. "
                    "Format responses clearly with bullet points and rupee amounts where relevant. "
                    "Never hedge excessively. Give a clear recommendation with reasoning.\n\n"
                    "Current stock analysis context:\n" + context
                )
                messages_payload = []
                # Include recent chat history for continuity
                for h in history[-6:]:
                    if h.get('role') in ('user','assistant'):
                        messages_payload.append({"role":h['role'],"content":h['content']})
                messages_payload.append({"role":"user","content":question})

                req_body = json.dumps({
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "system": system_prompt,
                    "messages": messages_payload
                }).encode()

                req = Request(
                    "https://api.anthropic.com/v1/messages",
                    data=req_body,
                    headers={
                        "Content-Type":"application/json",
                        "x-api-key": ANTHROPIC_KEY,
                        "anthropic-version":"2023-06-01",
                        "User-Agent":"StockSense/1.0"
                    },
                    method="POST"
                )
                with urlopen(req, timeout=20) as r:
                    resp = json.loads(r.read().decode())
                answer = resp["content"][0]["text"]
                self.send_json({"answer": answer, "source":"claude"}); return
            except Exception as e:
                print(f"  [AI-CHAT] Claude API error: {e} — falling back to rule engine")

        # ── Rule-based fallback (no API key) ───────────────────────────
        answer = self._rule_chat(question, context)
        self.send_json({"answer": answer, "source":"rule_engine"}); return

    def _rule_chat(self, question, context):
        """Smart rule-based trading advisor used when no Claude API key is set"""
        import re as _re
        q = question.lower().strip()

        def _num(label, default=0):
            m = _re.search(rf'{label}[:\s₹]*([0-9,]+\.?[0-9]*)', context, _re.I)
            return float(m.group(1).replace(',','')) if m else default

        price    = _num('Price', 0)
        sl       = _num('SL', 0)
        t1       = _num('T1', 0)
        t2       = _num('T2', 0)
        t3       = _num('T3', t2 * 1.015 if t2 else 0)
        qty      = int(_num(r'Qty.*?shares|(\d+) shares', 0))
        invest   = _num('Capital', 0)
        max_loss = _num('Max loss', 0)
        rr_m     = _re.search(r'R:R.*?1:([0-9.]+)', context)
        rr       = float(rr_m.group(1)) if rr_m else 0
        sig_m    = _re.search(r'Signal:\s*([A-Z ]+?)[\n(]', context)
        signal   = sig_m.group(1).strip() if sig_m else 'WATCH'
        conf_m   = _re.search(r'Confidence:\s*(\d+)%', context)
        conf     = int(conf_m.group(1)) if conf_m else 50
        sym_m    = _re.search(r'\(([A-Z]+)\)', context)
        sym      = sym_m.group(1) if sym_m else 'this stock'
        name_m   = _re.search(r'Stock:\s*(.+?)\s*\(', context)
        name     = name_m.group(1).strip() if name_m else sym
        setup_m  = _re.search(r'Setup:\s*(.+)', context)
        setup    = setup_m.group(1).strip() if setup_m else 'Consolidation'
        rsi_m    = _re.search(r'RSI:\s*([0-9.]+)', context)
        rsi      = float(rsi_m.group(1)) if rsi_m else 50
        sent_m   = _re.search(r'News sentiment:\s*(.+)', context)
        sent     = sent_m.group(1).strip() if sent_m else 'Neutral'
        mkt_m    = _re.search(r'Market context:\s*(.+)', context)
        mkt      = mkt_m.group(1).strip() if mkt_m else 'Neutral'
        move_m   = _re.search(r'Expected.*?([+\-][0-9–%]+)', context)
        move     = move_m.group(1) if move_m else '+1-2%'
        days_m   = _re.search(r'in ([0-9–]+ trading days)', context)
        days     = days_m.group(1) if days_m else '2-3 trading days'

        no_stock = 'No stock analysed' in context or not context.strip()
        if no_stock:
            return "⚠️ Please run a Deep Analysis on a stock first, then ask your question here."

        is_buy = 'BUY' in signal
        risk_per = max(price - sl, 1) if price and sl else price * 0.025

        # Match intent broadly
        if _re.search(r'share|qty|quantity|how many|how much to buy|units|lot', q):
            return (f"📊 **Position Sizing — {sym}**\n\n"
                    f"• Buy **{qty} shares** @ ₹{price:.2f}\n"
                    f"• Capital deployed: ₹{invest:,.0f}\n"
                    f"• Risk per share: ₹{risk_per:.2f} (SL @ ₹{sl:.0f})\n"
                    f"• Max loss at SL: ₹{max_loss:,.0f}\n\n"
                    f"This is based on the **2% capital risk rule** — never risk more than 2% of your total capital on a single trade. "
                    f"For a different budget, scale proportionally: e.g. ₹50,000 → {int(qty*2)} shares.")

        if _re.search(r'wait|when|enter|entry|timing|buy now|right time|should i|now or later', q):
            if is_buy:
                return (f"⏰ **Entry Timing — {sym}**\n\n"
                        f"Signal is **{signal}** at {conf}% confidence — setup looks ready.\n\n"
                        f"• **Ideal entry:** ₹{price:.2f}–₹{price*1.005:.2f} (within 0.5%)\n"
                        f"• **Best time:** 3:00–3:20 PM IST for BTST, or 9:15–9:45 AM on strength\n"
                        f"• **Do not chase** if price already moved >1% from current level\n"
                        f"• Market context: {mkt}\n"
                        f"• Setup: {setup} | RSI: {rsi:.0f}\n\n"
                        f"If buying BTST: exit next morning 9:15–10:30 AM regardless.")
            else:
                return (f"⏰ **Entry Timing — {sym}**\n\n"
                        f"Signal is **{signal}** ({conf}% confidence) — **not ideal to enter yet.**\n\n"
                        f"• RSI at {rsi:.0f} — {'building momentum, needs confirmation' if rsi < 50 else 'may need a pullback first'}\n"
                        f"• Wait for price to hold ₹{sl:.0f} as support for 1-2 sessions\n"
                        f"• Look for a volume spike (≥1.5× average) to confirm buying interest\n"
                        f"• Market context: {mkt} — factor this before entry\n\n"
                        f"Patience is the edge. A missed trade is better than a bad one.")

        if _re.search(r'stop.?loss|stop|sl|stoploss|protect|exit loss|where.*stop', q):
            return (f"🛡️ **Stop Loss — {sym}**\n\n"
                    f"• **Hard SL:** ₹{sl:.0f} — place as a bracket or cover order immediately\n"
                    f"• That's {(price-sl)/price*100:.1f}% below entry (₹{price:.2f})\n"
                    f"• Risk per share: ₹{risk_per:.2f} | Max loss on {qty} shares: ₹{max_loss:,.0f}\n\n"
                    f"**Trailing SL strategy:**\n"
                    f"  → T1 hit (₹{t1:.0f}) → Move SL to entry ₹{price:.2f} (risk-free)\n"
                    f"  → T2 hit (₹{t2:.0f}) → Move SL to T1 ₹{t1:.0f} (locked profit)\n\n"
                    f"⚠️ Always use a hard stop order — not a mental stop. Emotions will fail you.")

        if _re.search(r'target|profit|sell|exit|t1|t2|t3|book|where.*exit|how.*exit', q):
            return (f"🎯 **Profit Targets — {sym}**\n\n"
                    f"• **T1:** ₹{t1:.0f} (+1.5%) — Book **50%** of position here\n"
                    f"• **T2:** ₹{t2:.0f} (+2.5%) — Book another **40%** here\n"
                    f"• **T3:** ₹{t3:.0f} (+4.0%) — Trail stop for remaining **10%**\n\n"
                    f"Expected move: **{move}** in {days}\n"
                    f"Risk:Reward = **1:{rr:.1f}** {'✅ Good' if rr>=2 else '🟡 Acceptable' if rr>=1.5 else '⚠️ Poor'}\n\n"
                    f"For BTST: target T1 next morning. If gap-up, sell immediately at open.")

        if _re.search(r'btst|tomorrow|next.?day|overnight|hold.*night|sell.*morning', q):
            return (f"🌙 **BTST Trade Plan — {sym}**\n\n"
                    f"**TODAY (Entry):**\n"
                    f"• Buy **{qty} shares** @ ₹{price:.2f} between **3:00–3:20 PM IST**\n"
                    f"• Place SL order at ₹{sl:.0f} immediately after buying\n"
                    f"• Capital: ₹{invest:,.0f} | Max overnight risk: ₹{max_loss:,.0f}\n\n"
                    f"**TOMORROW (Exit):**\n"
                    f"• Exit window: **9:15–10:30 AM IST**\n"
                    f"• Gap-up open → sell at open\n"
                    f"• Flat open → wait till 10 AM, exit if target not hit\n"
                    f"• Target: ₹{t1:.0f} (T1) → ₹{t2:.0f} (T2)\n\n"
                    f"Setup: {setup} | News: {sent} | Confidence: {conf}%")

        if _re.search(r'invalid|break|fail|wrong|stop.*work|exit.*setup|what.*go wrong|risk.*scenario', q):
            return (f"⚠️ **Setup Invalidation — {sym}**\n\n"
                    f"Exit the trade immediately if any of these occur:\n\n"
                    f"• Price **closes below ₹{sl:.0f}** (hard stop — no exceptions)\n"
                    f"• Nifty 50 falls **>1.5% intraday** on heavy volume\n"
                    f"• India VIX spikes **above 20** (fear event)\n"
                    f"• High-impact negative news: earnings miss, SEBI action, penalty, management exit\n"
                    f"• Volume collapses after entry (no follow-through buyers)\n"
                    f"• {'RSI breaks below 40 with selling volume' if rsi < 55 else 'RSI drops sharply from current ' + str(int(rsi))}\n\n"
                    f"**Rule:** Once SL is hit → exit clean. No averaging down. Re-evaluate fresh.")

        if _re.search(r'confidence|sure|probability|reliable|accuracy|strong|how good|score', q):
            bar_filled = '█' * (conf // 10) + '░' * (10 - conf // 10)
            grade = '🟢 Strong' if conf >= 70 else '🟡 Moderate' if conf >= 55 else '🔴 Weak'
            return (f"📊 **Confidence Score — {sym}: {conf}%**\n\n"
                    f"[{bar_filled}] {conf}% — {grade}\n\n"
                    f"**5-Layer breakdown:**\n"
                    f"• Market environment: {mkt}\n"
                    f"• Setup pattern: {setup}\n"
                    f"• RSI: {rsi:.0f} ({'Oversold — good zone 🟢' if rsi < 45 else 'Neutral ⚪' if rsi < 65 else 'Overbought 🔴'})\n"
                    f"• News sentiment: {sent}\n\n"
                    f"{'✅ High confidence — trade with full position size.' if conf >= 70 else '🟡 Medium confidence — use 50-75% of planned position size.' if conf >= 55 else '⚠️ Low confidence — paper trade or skip. Wait for a cleaner setup.'}")

        if _re.search(r'risk.?reward|rr|r:r|reward|risk ratio', q):
            verdict = '✅ Excellent R:R — worth taking.' if rr >= 2.5 else '✅ Good R:R — proceed.' if rr >= 2 else '🟡 Acceptable R:R — manage carefully.' if rr >= 1.5 else '⚠️ Poor R:R — risk not justified, consider skipping.'
            return (f"⚖️ **Risk:Reward — {sym}**\n\n"
                    f"• Entry: ₹{price:.2f}\n"
                    f"• Stop Loss: ₹{sl:.0f} → **Risk = ₹{price-sl:.2f}/share** ({(price-sl)/price*100:.1f}%)\n"
                    f"• Target T2: ₹{t2:.0f} → **Reward = ₹{t2-price:.2f}/share** ({(t2-price)/price*100:.1f}%)\n"
                    f"• **R:R Ratio = 1:{rr:.1f}**\n\n"
                    f"{verdict}\n\n"
                    f"Total max loss on {qty} shares: ₹{max_loss:,.0f}\n"
                    f"Total max gain at T2: ₹{max(0, int((t2-price)*qty)):,}")

        if _re.search(r'news|catalyst|event|announcement|reason|why.*moving|fundamental', q):
            return (f"📰 **News & Catalyst — {sym}**\n\n"
                    f"Current sentiment: **{sent}**\n"
                    f"Market backdrop: {mkt}\n\n"
                    f"**Before entering, manually check:**\n"
                    f"• NSE corporate announcements for {sym}\n"
                    f"• BSE filings (bseindia.com → search {sym})\n"
                    f"• Any board meeting, earnings, or ex-dividend dates\n"
                    f"• Sector news (budget, FII flow, commodity prices)\n\n"
                    f"**Rule:** If you find HIGH-IMPACT negative news after this analysis, skip the trade even if technicals look perfect. **News always overrides technicals for BTST.**")

        if _re.search(r'sector|industry|peer|compare|vs |similar|related stock', q):
            return (f"🏭 **Sector Context — {sym}**\n\n"
                    f"For a complete picture, also check peer stocks in the same sector.\n\n"
                    f"**General check:**\n"
                    f"• If Nifty IT is strong → check INFY, TCS, WIPRO together\n"
                    f"• If Banking sector has momentum → HDFCBANK, ICICIBANK, SBIN move together\n"
                    f"• Sector ETFs (NIFTYBEES, JUNIORBEES) can confirm sector momentum\n\n"
                    f"Use the Smart Scanner on NSE Top 30 to find the strongest stock in the same sector.")

        # Default smart summary
        return (f"🤖 **STOCKSENSE — {name} ({sym})**\n\n"
                f"**Signal:** {signal} ({conf}% confidence)\n"
                f"**Price:** ₹{price:.2f} | **Setup:** {setup}\n\n"
                f"**Trade Plan:**\n"
                f"• Entry: ₹{price:.2f} | {qty} shares = ₹{invest:,.0f}\n"
                f"• SL: ₹{sl:.0f} | T1: ₹{t1:.0f} | T2: ₹{t2:.0f}\n"
                f"• R:R = 1:{rr:.1f} | Expected: {move} in {days}\n"
                f"• Max loss: ₹{max_loss:,.0f}\n\n"
                f"**Ask me anything about this trade:**\n"
                f"\"when to buy?\" · \"how many shares?\" · \"what's the SL?\" · \"BTST plan\" · \"what invalidates this?\" · \"confidence level?\" · \"risk:reward?\"")

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
                "stocks":"Yahoo Finance","commodities":"Twelve Data","server_version":"v5.5-holiday-fix","fetch_method":"yahoo_parallel"}); return

        # ── DEBUG ENDPOINT — tests every data source live ──────────
        if path == "/debug":
            report = {
                "api_key_set": bool(API_KEY),
                "api_key_prefix": API_KEY[:8]+"..." if API_KEY else "NOT SET",
                "python_version": __import__("sys").version[:10],
                "tests": {}
            }

            # Test 1: Stooq
            try:
                url = "https://stooq.com/q/d/l/?s=reliance.ns&i=d"
                req = Request(url, headers={"User-Agent":"Mozilla/5.0"})
                with urlopen(req, timeout=10) as r:
                    text = r.read().decode()[:300]
                report["tests"]["stooq"] = {"status":"OK","response":text[:200]}
            except Exception as e:
                report["tests"]["stooq"] = {"status":"FAIL","error":str(e)}

            # Test 2: Twelve Data /quote
            if API_KEY:
                try:
                    url = f"{TWELVE_DATA_BASE}/quote?symbol=RELIANCE:NSE&dp=2&apikey={API_KEY}"
                    req = Request(url, headers={"User-Agent":"MarketDashboard/1.0"})
                    with urlopen(req, timeout=12) as r:
                        raw = json.loads(r.read().decode())
                    report["tests"]["td_quote"] = {
                        "status":"OK" if raw.get("close") else "ERROR",
                        "close": raw.get("close"),
                        "message": raw.get("message",""),
                        "status_field": raw.get("status","")
                    }
                except Exception as e:
                    report["tests"]["td_quote"] = {"status":"FAIL","error":str(e)}
            else:
                report["tests"]["td_quote"] = {"status":"SKIP","reason":"No API key"}

            # Test 3: Twelve Data /time_series
            if API_KEY:
                try:
                    url = f"{TWELVE_DATA_BASE}/time_series?symbol=RELIANCE:NSE&interval=1day&outputsize=5&apikey={API_KEY}"
                    req = Request(url, headers={"User-Agent":"MarketDashboard/1.0"})
                    with urlopen(req, timeout=12) as r:
                        raw = json.loads(r.read().decode())
                    report["tests"]["td_timeseries"] = {
                        "status":"OK" if "values" in raw else "ERROR",
                        "has_values": "values" in raw,
                        "row_count": len(raw.get("values",[])),
                        "message": raw.get("message",""),
                        "status_field": raw.get("status","")
                    }
                except Exception as e:
                    report["tests"]["td_timeseries"] = {"status":"FAIL","error":str(e)}

            # Test 4: Yahoo Finance
            try:
                url = "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?interval=1d&range=5d"
                req = Request(url, headers=get_yf_headers())
                with urlopen(req, timeout=10) as r:
                    raw = json.loads(r.read().decode())
                price = raw["chart"]["result"][0]["meta"].get("regularMarketPrice")
                report["tests"]["yahoo"] = {"status":"OK","price":price}
            except Exception as e:
                report["tests"]["yahoo"] = {"status":"FAIL","error":str(e)}

            # Test 5: Cache status
            report["cache_size"] = len(_cache)
            report["cached_stocks"] = [k for k in _cache if ":master" in k]

            self.send_json(report); return

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
            # NSE ETF symbols — some may not be on Yahoo Finance, use fallbacks
            # Primary symbols tried first, alternate tried if primary fails
            ETF_CONFIGS = [
                {"symbol":"GOLDBEES",   "name":"Nippon Gold ETF",   "alt":"GLD"},
                {"symbol":"SILVERBEES", "name":"Nippon Silver ETF",  "alt":"SLV"},
                {"symbol":"COPPERBEES", "name":"Nippon Copper ETF",  "alt":"COPX"},
                {"symbol":"NIFTYBEES",  "name":"Nippon Nifty 50 ETF","alt":None},
                {"symbol":"JUNIORBEES", "name":"Nippon Junior BeES", "alt":None},
                {"symbol":"LIQUIDBEES", "name":"Nippon Liquid ETF",  "alt":None},
            ]
            etf_syms = [c["symbol"] for c in ETF_CONFIGS]
            results  = fetch_yahoo_multi(etf_syms, "10d")
            # For any ETF that returned price=0 or error, mark unavailable cleanly
            for cfg in ETF_CONFIGS:
                sym = cfg["symbol"]
                r   = results.get(sym, {})
                if r.get("status") != "ok" or not r.get("price"):
                    results[sym] = {
                        "symbol":    sym,
                        "name":      cfg["name"],
                        "price":     0,
                        "change":    0,
                        "changePct": 0,
                        "status":    "unavailable",
                        "indicators":{"rsi":None,"sma20":None,"sma50":None,"ema9":None,
                                      "macd":None,"macdSignal":None,"macdHist":None,
                                      "support":None,"resistance":None,"volumeSignal":"normal"},
                        "recommendation":"HOLD","sentiment":"Neutral","confidence":50,
                        "reasons":["Data not available from Yahoo Finance for this ETF ticker."],
                    }
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
            watch  = [s for s in scored if s["signal"] == "WATCH"]
            others = [s for s in scored if s["signal"] not in ("STRONG BUY","BUY","WEAK BUY","WATCH")]

            # When fewer than 3 BUY picks, pad with best WATCH stocks so scanner is never empty
            picks_final = list(buys[:10])
            if len(picks_final) < 3 and watch:
                extras = watch[:max(0, 5 - len(picks_final))]
                picks_final.extend(extras)

            # Market health for context display
            mkt_cached   = cache_get("market-data")
            mkt_score    = 3.0
            mkt_verdict  = "CAUTION"
            if mkt_cached:
                vix   = float((mkt_cached.get("vix")   or {}).get("price",15))
                crude = float((mkt_cached.get("crude") or {}).get("price",78))
                nchg  = float((mkt_cached.get("nifty") or {}).get("changePct",0))
                s5chg = float((mkt_cached.get("sp500") or {}).get("changePct",0))
                raw = ( (2 if nchg>=1 else 1 if nchg>=0.3 else 0 if nchg>-0.3 else -1 if nchg>=-1 else -2)
                       +(1 if vix<13 else 0.5 if vix<16 else 0 if vix<20 else -1)
                       +(1 if crude<80 else 0.5 if crude<90 else 0 if crude<100 else -1)
                       +(1 if s5chg>=0.5 else 0.5 if s5chg>=0 else 0 if s5chg>=-1 else -1) + 3 )
                mkt_score   = round(max(0, min(5, raw)), 1)
                mkt_verdict = ("BULLISH" if mkt_score>=4 else "CAUTION" if mkt_score>=3
                               else "BEARISH" if mkt_score>=2 else "UNSAFE")

            result = {
                "picks":          picks_final,
                "others":         others[:5],
                "watch_picks":    watch[:5],
                "scanned":        len(all_data),
                "qualified":      len(buys),
                "watch_count":    len(watch),
                "mode":           mode,
                "timestamp":      time.strftime("%H:%M IST"),
                "data_source":    "twelvedata+yahoo" if API_KEY else "yahoo",
                "market_score":   mkt_score,
                "market_verdict": mkt_verdict,
            }

            # Cache result for 30 minutes — next click is INSTANT
            cache_set(cache_key, result)
            print(f"  [SCAN-FAST] Done. {len(buys)} buys + {len(watch)} watch. Cached 30 min.")
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

        # ── ADVANCED AI MULTI-LAYER ANALYSIS ─────────────────────────────
        if path == "/ai-analyse":
            sym    = flat.get("symbol","").strip().upper().replace(".NS","").replace(".BO","")
            budget = float(flat.get("budget","25000") or 25000)
            if not sym:
                self.send_json({"error":"Missing symbol"},400); return

            # Fetch stock data (uses cache if available)
            stock_data = fetch_yahoo(sym, "30d")
            if stock_data.get("status") != "ok":
                self.send_json({"error":f"Could not load {sym}","status":"error"}); return

            # Fetch news
            news_items = fetch_news(sym, limit=8)

            # Fetch market health
            mkt_cached = cache_get("market-data")
            # Run 5-layer score
            score_result = score_stock_python(stock_data)
            if not score_result:
                self.send_json({"error":"Scoring failed","status":"error"}); return

            ind      = stock_data.get("indicators", {})
            price    = stock_data.get("price", 0)
            prev     = stock_data.get("prevClose", price)
            chg_pct  = stock_data.get("changePct", 0)
            rsi      = ind.get("rsi") or 50
            sma20    = ind.get("sma20") or 0
            sma50    = ind.get("sma50") or 0
            ema9     = ind.get("ema9") or 0
            macd     = ind.get("macd") or 0
            macd_h   = ind.get("macdHist") or 0
            support  = ind.get("support") or 0
            resist   = ind.get("resistance") or 0
            vol_sig  = ind.get("volumeSignal","normal")
            h52      = stock_data.get("high52w", price * 1.3)
            l52      = stock_data.get("low52w", price * 0.7)

            # ── Layer 1: Market Context ──────────────────────────────────
            mkt_score = score_result.get("l1", 3)
            mkt_verdict = "STRONG TAILWIND" if mkt_score >= 4 else ("SUPPORTIVE" if mkt_score >= 3 else ("NEUTRAL" if mkt_score >= 2 else "HEADWIND"))
            vix_val = crude_val = nifty_chg = sp500_chg = None
            if mkt_cached:
                vix_val   = (mkt_cached.get("vix") or {}).get("price")
                crude_val = (mkt_cached.get("crude") or {}).get("price")
                nifty_chg = (mkt_cached.get("nifty") or {}).get("changePct")
                sp500_chg = (mkt_cached.get("sp500") or {}).get("changePct")

            # ── Layer 2: Price Action ────────────────────────────────────
            pct_above_low = round(((price - l52) / l52 * 100), 1) if l52 > 0 else 0
            pct_below_hi  = round(((h52 - price) / h52 * 100), 1) if h52 > 0 else 10
            trend_7d = chg_pct  # proxy (today's change vs last close)
            # 30d trend from history
            hist = stock_data.get("history", [])
            trend_30d = 0
            if len(hist) >= 20:
                try: trend_30d = round((price - hist[-20]["close"]) / hist[-20]["close"] * 100, 1)
                except: pass
            # Pattern detection
            pattern = "Consolidation"
            if resist and price >= resist * 0.998: pattern = "Breakout 🚀"
            elif resist and price >= resist * 0.97 and chg_pct > 0: pattern = "Near Breakout ⚡"
            elif support and price <= support * 1.025 and chg_pct >= 0: pattern = "Support Bounce 📈"
            elif rsi < 38: pattern = "Oversold Reversal 🔄"
            elif chg_pct > 1.5 and vol_sig == "high": pattern = "Momentum Surge 🔥"
            elif sma20 and abs(price - sma20) / sma20 < 0.015 and chg_pct > 0: pattern = "Pullback Buy 💡"

            # ── Layer 3: Technical Confluence ───────────────────────────
            bullish_t = bearish_t = 0
            t_signals = []
            if rsi < 40: bullish_t += 2; t_signals.append(f"RSI {rsi:.0f} — oversold")
            elif rsi < 55: bullish_t += 1; t_signals.append(f"RSI {rsi:.0f} — neutral-bullish")
            elif rsi > 70: bearish_t += 2; t_signals.append(f"RSI {rsi:.0f} — overbought ⚠")
            else: t_signals.append(f"RSI {rsi:.0f} — neutral")
            if macd_h > 0: bullish_t += 1; t_signals.append("MACD positive ✓")
            else: bearish_t += 1; t_signals.append("MACD negative ✗")
            if sma20 and price > sma20: bullish_t += 1; t_signals.append(f"Above SMA20 ₹{sma20}")
            elif sma20: bearish_t += 1; t_signals.append(f"Below SMA20 ₹{sma20} ⚠")
            if sma50 and price > sma50: bullish_t += 1; t_signals.append(f"Above SMA50 ₹{sma50}")
            elif sma50: bearish_t += 1; t_signals.append(f"Below SMA50 ₹{sma50} ⚠")
            if vol_sig == "high": bullish_t += 1; t_signals.append("Volume spike — institutional interest 🔥")
            tech_score = score_result.get("l2", 0)

            # ── Layer 4: News & Sentiment ────────────────────────────────
            pos_news = [n for n in news_items if n.get("sentiment") == "positive"]
            neg_news = [n for n in news_items if n.get("sentiment") == "negative"]
            neu_news = [n for n in news_items if n.get("sentiment") == "neutral"]
            high_imp = [n for n in news_items if n.get("impact") == "HIGH"]
            news_tone = "BULLISH 🟢" if len(pos_news) > len(neg_news) + 1 else \
                        ("BEARISH 🔴" if len(neg_news) > len(pos_news) + 1 else "NEUTRAL 🟡")
            news_score = score_result.get("l4", 0)

            # ── Layer 5: Risk Assessment ─────────────────────────────────
            sl_pct  = score_result.get("risk_pct", 2.5)
            rr      = score_result.get("rr", 1.5)
            sl_val  = score_result.get("sl", round(price * 0.975, 0))
            t1_val  = score_result.get("t1", round(price * 1.015, 0))
            t2_val  = score_result.get("t2", round(price * 1.025, 0))
            t3_val  = score_result.get("t3", round(price * 1.040, 0))
            reward  = score_result.get("reward_pct", 2.5)
            # Position sizing
            risk_per_share = max(price - sl_val, 1)
            risk_capital   = budget * 0.02  # 2% capital risk
            qty            = int(risk_capital / risk_per_share)
            qty            = max(1, min(qty, int(budget / price)))
            qty_invest     = round(qty * price, 0)
            max_loss       = round(qty * (price - sl_val), 0)
            max_gain_t2    = round(qty * (t2_val - price), 0)

            # ── Overall Signal ───────────────────────────────────────────
            signal       = score_result.get("signal", "WATCH")
            total        = score_result.get("total", 10)
            confidence   = score_result.get("confidence", 50)
            setup        = score_result.get("setup", "Consolidation")
            rec_original = stock_data.get("recommendation","HOLD")
            reasons      = stock_data.get("reasons", [])
            strat_pts    = stock_data.get("strategyPoints", [])

            # ── Expected move ────────────────────────────────────────────
            expected_move = "+1–2%" if signal in ("BUY","STRONG BUY") else ("Flat/±1%" if signal in ("WEAK BUY","WATCH") else "–1–2%")
            days_est      = "2–3 trading days"

            # ── Risk level ───────────────────────────────────────────────
            if rr >= 2.5 and sl_pct <= 2.5: risk_level = "LOW 🟢"
            elif rr >= 1.5 and sl_pct <= 3.5: risk_level = "MEDIUM 🟡"
            else: risk_level = "HIGH 🔴"

            # Build response
            response = {
                "symbol":     sym,
                "name":       stock_data.get("name", sym),
                "price":      round(price, 2),
                "changePct":  chg_pct,
                "signal":     signal,
                "setup":      pattern,
                "confidence": confidence,
                "total_score": total,
                "expected_move": expected_move,
                "days_est":     days_est,
                "risk_level":   risk_level,

                # Layers
                "layer1": {
                    "score":   mkt_score,
                    "verdict": mkt_verdict,
                    "vix":     vix_val,
                    "crude":   crude_val,
                    "nifty_chg": nifty_chg,
                    "sp500_chg": sp500_chg,
                },
                "layer2": {
                    "pattern":   pattern,
                    "trend_7d":  trend_7d,
                    "trend_30d": trend_30d,
                    "pct_above_low": pct_above_low,
                    "pct_below_hi":  pct_below_hi,
                    "support":   support,
                    "resistance": resist,
                    "high52w":   h52,
                    "low52w":    l52,
                },
                "layer3": {
                    "score":       tech_score,
                    "rsi":         round(rsi,1),
                    "macd":        macd,
                    "macd_hist":   macd_h,
                    "sma20":       sma20,
                    "sma50":       sma50,
                    "ema9":        ema9,
                    "vol_signal":  vol_sig,
                    "bullish":     bullish_t,
                    "bearish":     bearish_t,
                    "signals":     t_signals,
                },
                "layer4": {
                    "score":     news_score,
                    "tone":      news_tone,
                    "positive":  len(pos_news),
                    "negative":  len(neg_news),
                    "neutral":   len(neu_news),
                    "high_impact": len(high_imp),
                    "top_news":  news_items[:5],
                },
                "layer5": {
                    "sl":         sl_val,
                    "t1":         t1_val,
                    "t2":         t2_val,
                    "t3":         t3_val,
                    "risk_pct":   sl_pct,
                    "reward_pct": reward,
                    "rr_ratio":   rr,
                    "risk_level": risk_level,
                    "qty":        qty,
                    "qty_invest": qty_invest,
                    "max_loss":   max_loss,
                    "max_gain":   max_gain_t2,
                    "budget":     budget,
                },

                # Narrative
                "reasons":     reasons,
                "strategy":    strat_pts,
                "indicators":  ind,
                "history":     hist[-15:] if hist else [],
                "status":      "ok",
            }
            cache_set(f"{sym}:ai-analyse", response, ttl=300)
            self.send_json(response); return

        # ── AI CHAT via GET (fallback when POST not available) ────────
        if path == "/ai-chat":
            question = flat.get("q","").strip() or flat.get("question","").strip()
            context  = flat.get("context","").strip()
            if not question:
                self.send_json({"answer":"Please ask a question."}); return
            answer = self._rule_chat(question, context)
            self.send_json({"answer": answer, "source":"rule_engine"}); return

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
