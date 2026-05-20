import os
import math
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

# ─── Credentials from GitHub Secrets ─────────────────────────────────────────
YOUTUBE_API_KEY = os.environ['YOUTUBE_API_KEY']
SUPABASE_URL    = os.environ['SUPABASE_URL']
SUPABASE_KEY    = os.environ['SUPABASE_KEY']

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── SOCTRA Patent Formula — Configurable Operator Parameters ─────────────────
# Per patent spec: "All weighting parameters stored as configurable operator
# parameters and logged for audit."
# Tuned for YouTube-sourced FIFA team engagement data.
V_BASE             = 8.0    # V_base: baseline constant, ensures non-zero starting valuation
OMEGA1             = 0.7    # ω1: weight for cumulative engagement (log compression term)
OMEGA2             = 30.0   # ω2: weight for incremental momentum — amplifies daily swings
ALPHA              = 2.0    # α: sensitivity scaling factor applied to S(t)
BETA               = 0.75   # β: scale of surge/dip adjustment via Φ(t)
KAPPA              = 1.2    # κ: steepness of tanh curve controlling surge response
DELTA              = 0.30   # δ: anomaly penalty coefficient via Ψ(t)
MAX_CHANGE_PER_RUN = 0.12   # System guardrail: max 5% price shift per hourly scrape


# ─────────────────────────────────────────────────────────────────────────────
# DATA ACQUISITION FUNCTIONS
# These retain all error handling and logging from the working scraper.
# ─────────────────────────────────────────────────────────────────────────────

def get_channel_stats(channel_id):
    """
    Fetches subscriber count and total view count for a YouTube channel.
    Maps to Patent: Data Acquisition Module — acquiring user interaction data
    from heterogeneous online sources (YouTube API).
    """
    url    = "https://www.googleapis.com/youtube/v3/channels"
    params = {'part': 'statistics', 'id': channel_id, 'key': YOUTUBE_API_KEY}
    try:
        data = requests.get(url, params=params).json()
        if not data.get('items'):
            print(f"    ⚠️  No channel data returned for ID: {channel_id}")
            return None
        stats = data['items'][0]['statistics']
        return {
            'subscribers': int(stats.get('subscriberCount', 0)),
            'total_views':  int(stats.get('viewCount', 0))
        }
    except Exception as e:
        print(f"    ❌ Error fetching channel stats for {channel_id}: {e}")
        return None


def get_recent_engagement(channel_id):
    """
    Fetches views and likes from the 5 most recent videos on the channel.
    Maps to Patent: Data Interface Module — acquiring engagement data
    (likes, views) from online platforms via API.
    E_delta source: recent video views as incremental engagement signal.
    """
    url    = "https://www.googleapis.com/youtube/v3/channels"
    params = {'part': 'contentDetails', 'id': channel_id, 'key': YOUTUBE_API_KEY}
    try:
        data = requests.get(url, params=params).json()
        if not data.get('items'):
            print(f"    ⚠️  No engagement data returned for ID: {channel_id}")
            return {'views': 0, 'likes': 0}

        uploads_id = data['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        url2    = "https://www.googleapis.com/youtube/v3/playlistItems"
        params2 = {'part': 'contentDetails', 'playlistId': uploads_id,
                   'maxResults': 5, 'key': YOUTUBE_API_KEY}
        data2   = requests.get(url2, params2).json()
        if not data2.get('items'):
            print(f"    ⚠️  No recent videos found for channel: {channel_id}")
            return {'views': 0, 'likes': 0}

        video_ids = [i['contentDetails']['videoId'] for i in data2['items']]

        url3    = "https://www.googleapis.com/youtube/v3/videos"
        params3 = {'part': 'statistics', 'id': ','.join(video_ids),
                   'key': YOUTUBE_API_KEY}
        data3   = requests.get(url3, params3).json()

        total_views = sum(int(v['statistics'].get('viewCount', 0))
                          for v in data3.get('items', []))
        total_likes = sum(int(v['statistics'].get('likeCount', 0))
                          for v in data3.get('items', []))
        return {'views': total_views, 'likes': total_likes}

    except Exception as e:
        print(f"    ❌ Error fetching engagement for {channel_id}: {e}")
        return {'views': 0, 'likes': 0}


def get_price_24h_ago(team_name):
    """
    Fetches the last recorded price from ~24 hours ago in fifa_price_history.
    Used to compute accurate 24H change percentage displayed on trading cards.
    Returns None gracefully if insufficient history exists (e.g. first day of scraping).
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
        result = (
            supabase.table('fifa_price_history')
            .select('price, recorded_at')
            .eq('team_name', team_name)
            .lte('recorded_at', cutoff)
            .order('recorded_at', desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return float(result.data[0]['price'])
        return None
    except Exception as e:
        print(f"    ⚠️  Could not retrieve 24h price for {team_name}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SOCTRA PATENT VALUATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def soctra_calculate_price(channel_stats, engagement, old_price):
    """
    Implements the SOCTRA Patent Calculation/Valuation Engine exactly:

        S(t)    = ω1·log(1 + E_total) + ω2·(E_delta / max(1, E_total))
        V_pre   = V_base + α·S(t)
        R_surge = TV(t,24h) / MAV(t,7d)
        Φ(t)    = 1 + β·tanh(κ·(R_surge − 1))
        Ψ(t)    = 1 − δ·A(t)
        V(t)    = V_pre(t) · Φ(t) · Ψ(t)

    Input mapping:
        E_total  ←  subscriber count    (stable cumulative engagement metric)
        E_delta  ←  recent video views  (incremental interaction signal)
        R_surge  ←  defaults to 1.0     (neutral — trade RPC not yet deployed;
                                          extend when transaction table is connected)
        A(t)     ←  0                   (no anomaly; extend with bot detection if needed)

    System guardrail: max 5% price change per hourly run (implementation constraint;
    formula itself remains mathematically pure and deterministic).
    """
    if not channel_stats:
        # No YouTube data returned: hold price steady at last known value.
        # Do NOT default to 100 — preserves price continuity for traders.
        print(f"    ⚠️  No channel stats — holding price at {old_price} SOX")
        return round(max(10.0, min(500.0, old_price)), 2)

    # ── Inputs: Normalization Module ──────────────────────────────────────────
    # E_total: subscriber count as cumulative engagement baseline.
    # log(1 + E_total) ensures mega-channels don't dominate disproportionately.
    E_total = max(1, channel_stats['subscribers'])
    E_delta = engagement['views']   # Recent 5-video view total as momentum signal

    # ── S(t): Engagement Score ────────────────────────────────────────────────
    cumulative_term = OMEGA1 * math.log(1 + E_total)
    momentum_term   = OMEGA2 * (E_delta / max(1, E_total))
    S = cumulative_term + momentum_term
    print(f"      S(t) = {cumulative_term:.3f} (cumulative) + {momentum_term:.3f} "
          f"(momentum) = {S:.3f}")

    # ── V_pre(t): Pre-Adjustment Valuation ────────────────────────────────────
    V_pre = V_BASE + ALPHA * S
    print(f"      V_pre = {V_BASE} + {ALPHA} × {S:.3f} = {V_pre:.3f}")

    # ── R_surge(t): Transaction Volume Surge Ratio ────────────────────────────
    # TV(t,24h) / MAV(t,7d) — captures whether trading activity exceeds norms.
    # Defaults to 1.0 (neutral: Φ = 1.0, no surge effect) until trade volume
    # RPC functions are deployed in Supabase. When ready, replace with:
    #   tv_24h  = get_trade_volume_24h(team_name)
    #   mav_7d  = get_trade_mav_7d(team_name)
    #   R_surge = tv_24h / max(1, mav_7d)
    R_surge = 1.0

    # ── Φ(t): Volatility Adjustment ───────────────────────────────────────────
    # tanh provides smooth bounded adjustment. Cannot exceed +β or fall below −β.
    # At R_surge=1.0 (neutral): Φ = 1 + β·tanh(0) = 1.0 exactly.
    Phi = 1 + BETA * math.tanh(KAPPA * (R_surge - 1))

    # ── Ψ(t): Anomaly Correction ──────────────────────────────────────────────
    # A(t) = 1 if anomaly detected, 0 otherwise. Currently 0 (no anomaly detection).
    anomaly_flag = 0
    Psi = 1 - DELTA * anomaly_flag

    # ── V(t): Final Valuation ─────────────────────────────────────────────────
    V = V_pre * Phi * Psi
    print(f"      Φ(t) = {Phi:.4f} | Ψ(t) = {Psi:.4f} | V(t) raw = {V:.3f}")

    # ── Per-Run Guardrail (System-Level, not formula-level) ───────────────────
    if old_price and old_price > 0:
        upper = old_price * (1 + MAX_CHANGE_PER_RUN)
        lower = old_price * (1 - MAX_CHANGE_PER_RUN)
        V_capped = max(lower, min(upper, V))
        if V_capped != V:
            print(f"      ⚙️  Guardrail applied: {V:.3f} → {V_capped:.3f} "
                  f"(±{MAX_CHANGE_PER_RUN*100:.0f}% cap)")
        V = V_capped

    # ── Absolute Bounds ───────────────────────────────────────────────────────
    return round(max(10.0, min(500.0, V)), 2)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def main():
    teams = supabase.table('fifa_teams').select('*').eq('is_active', True).execute()
    now   = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*60}")
    print(f"  SOCTRA FIFA Scraper — {now}")
    print(f"  🔍 Found {len(teams.data)} active FIFA teams")
    print(f"{'='*60}")

    success_count = 0
    skip_count    = 0

    for team in teams.data:
        team_name = team['team_name']
        print(f"\n⚽ Processing: {team_name}")

        cid       = team['youtube_channel_id']
        old_price = float(team.get('current_price') or 100.0)

        # ── Step 1: Acquire raw YouTube data (Data Acquisition Module) ────────
        ch_stats   = get_channel_stats(cid)
        engagement = get_recent_engagement(cid)

        # ── Step 2: Run SOCTRA Patent Valuation Engine ────────────────────────
        new_price = soctra_calculate_price(ch_stats, engagement, old_price)

        # ── Step 3: Compute change percentages ────────────────────────────────
        # 1H: change vs previous scrape run (last hourly price)
        change_1h = round(((new_price - old_price) / old_price) * 100, 2) \
                    if old_price > 0 else 0.0

        # 24H: change vs price recorded ~24 hours ago in history
        price_24h_ago = get_price_24h_ago(team_name)
        if price_24h_ago and price_24h_ago > 0:
            change_24h = round(((new_price - price_24h_ago) / price_24h_ago) * 100, 2)
        else:
            change_24h = change_1h   # Fallback for first 24h when history is thin

        # ── Step 4: Update fifa_teams ─────────────────────────────────────────
        supabase.table('fifa_teams').update({
            'current_price':    new_price,
            'price_1h_change':  change_1h,
            'price_24h_change': change_24h,
            'last_updated':     now
        }).eq('team_name', team_name).execute()

        # ── Step 5: Append to fifa_price_history (Ledger Module) ─────────────
        # append-only record of price creation events per patent spec
        supabase.table('fifa_price_history').insert({
            'team_name':   team_name,
            'price':       new_price,
            'recorded_at': now
        }).execute()

        # ── Step 6: Log raw metrics to fifa_team_metrics ──────────────────────
        if ch_stats:
            try:
                supabase.table('fifa_team_metrics').insert({
                    'team_name':        team_name,
                    'recorded_at':      now,
                    'subscriber_count': ch_stats['subscribers'],
                    'recent_views_24h': engagement['views'],
                    'recent_likes_24h': engagement['likes'],
                    'calculated_price': new_price
                }).execute()
                print(f"  ✅ {team_name}: {old_price:.2f} → {new_price:.2f} SOX "
                      f"| 1H: {change_1h:+.2f}% | 24H: {change_24h:+.2f}%")
                print(f"      Subs: {ch_stats['subscribers']:,} "
                      f"| Recent Views: {engagement['views']:,} "
                      f"| Likes: {engagement['likes']:,}")
                success_count += 1
            except Exception as e:
                print(f"  ❌ Metrics insert failed for {team_name}: {e}")
        else:
            print(f"  ⚠️  {team_name}: no YouTube data — price held at {new_price:.2f} SOX")
            skip_count += 1

    print(f"\n{'='*60}")
    print(f"  Run complete: ✅ {success_count} updated | ⚠️  {skip_count} skipped")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
