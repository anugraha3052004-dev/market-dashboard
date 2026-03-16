"""
India Market Dashboard - Backend v4
Commodities  → Twelve Data + gold-api.com (free)
NSE Stocks   → Yahoo Finance (parallel + 5-min cache)
News         → Yahoo Finance RSS (free)
"""
import os, json, time, math, threading, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

API_KEY          = os.environ.get("TWELVE_DATA_API_KEY","")
TWELVE_DATA_BASE = "https://api.twelvedata.com"
YF_BASE          = "https://query1.finance.yahoo.com/v8/finance/chart"
YF_BASE2         = "https://query2.finance.yahoo.com/v8/finance/chart"
YF_HEADERS       = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept":"application/json,text/plain,*/*",
    "Accept-Language":"en-US,en;q=0.9",
    "Referer":"https://finance.yahoo.com",
    "Connection":"keep-alive",
}

# ── 5-MINUTE IN-MEMORY CACHE ────────────────────────────────────────────────
_cache = {}
_cache_lck = threading.Lock()
CACHE_TTL = 300

def cache_get(key):
    with _cache_lck:
        e = _cache.get(key)
        if e and (time.time() - e["ts"]) < CACHE_TTL:
            print(f"  [CACHE HIT] {key}")
            return e["data"]
    return None

def cache_set(key, data):
    with _cache_lck:
        _cache[key] = {"data": data, "ts": time.time()}

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

    print(f"\n  [YAHOO] Fetching {yf_sym}...")
    for attempt, base in enumerate([YF_BASE, YF_BASE2], 1):
        try:
            url = f"{base}/{yf_sym}?interval=1d&range={range_}"
            req = Request(url, headers=YF_HEADERS)
            with urlopen(req, timeout=8) as r:
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
    BATCH = 3  # 3 parallel at a time — sweet spot for Yahoo Finance

    def fetch_one(sym):
        result = fetch_yahoo(sym, range_)
        with lock:
            out[sym] = result

    # Split into batches
    batches = [symbols[i:i+BATCH] for i in range(0, len(symbols), BATCH)]
    for batch in batches:
        threads = [threading.Thread(target=fetch_one, args=(s,)) for s in batch]
        for t in threads: t.start()
        for t in threads: t.join(timeout=12)
        # Small pause between batches only if there are multiple batches
        if len(batches) > 1:
            time.sleep(0.2)

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
        # Try given path first, then fallback to root (for flat GitHub structure)
        paths_to_try = [fp]
        if fp.startswith("static/"):
            paths_to_try.append(fp[len("static/"):])  # try root
        for path in paths_to_try:
            try:
                body = open(path,"rb").read()
                self.send_response(200)
                self.send_header("Content-Type",ct)
                self.send_header("Content-Length",str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            except FileNotFoundError:
                continue
        self.send_response(404); self.end_headers()

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

        if path in ("/icon-192.png","/icon-512.png"):
            self.send_file("static"+path,"image/png"); return

        if path == "/health":
            self.send_json({"status":"ok","api_key_set":bool(API_KEY),
                "stocks":"Yahoo Finance","commodities":"Twelve Data"}); return

        # single stock with full indicators
        if path == "/stock":
            sym = flat.get("symbol","").strip()
            if not sym: self.send_json({"error":"Missing symbol"},400); return
            self.send_json(fetch_yahoo(sym, flat.get("range","60d"))); return

        # multiple stocks — parallel fetch, 20d range for speed
        if path == "/stocks":
            raw = flat.get("symbols","").strip()
            if not raw: self.send_json({"error":"Missing symbols"},400); return
            syms = [s.strip() for s in raw.split(",") if s.strip()][:12]
            self.send_json(fetch_yahoo_multi(syms, "20d")); return

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
            all_data = fetch_yahoo_multi(syms)
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

        # ── MARKET SCANNER with all 5 signals ────────────────────────────
        if path == "/scan":
            watchlist = [s.strip().upper() for s in flat.get("watchlist","").split(",") if s.strip()]
            candidates = [s["symbol"] for s in NSE_STOCKS if s["symbol"] not in watchlist][:10]
            print(f"\n  [SCAN] Scanning {len(candidates)} stocks…")
            results = fetch_yahoo_multi(candidates[:8], "20d")
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
]

def run():
    port = int(os.environ.get("PORT") or 10000)
    srv  = HTTPServer(("0.0.0.0",port), Handler)
    print("="*60)
    print("  India Market Dashboard - Backend v3")
    print("="*60)
    print(f"  URL      : http://localhost:{port}")
    print(f"  Stocks   : Yahoo Finance + Technical Analysis (free)")
    print(f"  Commod.  : Twelve Data ({'KEY SET' if API_KEY else 'KEY NOT SET'})")
    print(f"  News     : Yahoo Finance RSS (free)")
    print("="*60)
    print("  Press Ctrl+C to stop\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.")

if __name__ == "__main__": run()
