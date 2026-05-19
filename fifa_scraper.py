import os
import requests
from datetime import datetime, timezone
from supabase import create_client, Client

# Pull credentials from GitHub Secrets
YOUTUBE_API_KEY = os.environ['YOUTUBE_API_KEY']
SUPABASE_URL    = os.environ['SUPABASE_URL']
SUPABASE_KEY    = os.environ['SUPABASE_KEY']

# Connect to Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_channel_stats(channel_id):
    # Fetches subscriber count and total view count for a channel
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {'part': 'statistics', 'id': channel_id, 'key': YOUTUBE_API_KEY}
    try:
        data = requests.get(url, params=params).json()
        if not data.get('items'):
            print(f"    ⚠️  No items in channel response for {channel_id}")
            return None
        stats = data['items'][0]['statistics']
        return {
            'subscribers': int(stats.get('subscriberCount', 0)),
            'total_views':  int(stats.get('viewCount', 0))
        }
    except Exception as e:
        print(f"    ❌ Error fetching channel stats: {e}")
        return None

def get_recent_engagement(channel_id):
    # Step A: Get the channel's uploads playlist ID
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {'part': 'contentDetails', 'id': channel_id, 'key': YOUTUBE_API_KEY}
    try:
        data = requests.get(url, params=params).json()
        if not data.get('items'):
            print(f"    ⚠️  No items in engagement response for {channel_id}")
            return {'views': 0, 'likes': 0}

        uploads_id = data['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        # Step B: Get the 5 most recent videos
        url2 = "https://www.googleapis.com/youtube/v3/playlistItems"
        params2 = {'part': 'contentDetails', 'playlistId': uploads_id,
                   'maxResults': 5, 'key': YOUTUBE_API_KEY}
        data2 = requests.get(url2, params2).json()
        if not data2.get('items'):
            print(f"    ⚠️  No recent videos found for {channel_id}")
            return {'views': 0, 'likes': 0}

        video_ids = [i['contentDetails']['videoId'] for i in data2['items']]

        # Step C: Get stats for those videos
        url3 = "https://www.googleapis.com/youtube/v3/videos"
        params3 = {'part': 'statistics', 'id': ','.join(video_ids), 'key': YOUTUBE_API_KEY}
        data3 = requests.get(url3, params3).json()

        total_views = sum(int(v['statistics'].get('viewCount', 0))
                          for v in data3.get('items', []))
        total_likes = sum(int(v['statistics'].get('likeCount', 0))
                          for v in data3.get('items', []))
        return {'views': total_views, 'likes': total_likes}
    except Exception as e:
        print(f"    ❌ Error fetching engagement: {e}")
        return {'views': 0, 'likes': 0}

def calculate_price(channel_stats, engagement):
    # Price formula: weighted score from subscribers, recent views, recent likes
    # Every 100k subscribers = 1 point (weight 30%)
    # Every 10k recent views  = 1 point (weight 50%)
    # Every 1k recent likes   = 1 point (weight 20%)
    if not channel_stats:
        return 100.0
    sub_score  = channel_stats['subscribers'] / 100000
    view_score = engagement['views'] / 10000
    like_score = engagement['likes'] / 1000
    raw = (sub_score * 0.3) + (view_score * 0.5) + (like_score * 0.2)
    # Keep price between 10 and 500
    return round(max(10.0, min(500.0, raw)), 2)

def main():
    teams = supabase.table('fifa_teams').select('*').eq('is_active', True).execute()
    now   = datetime.now(timezone.utc).isoformat()
    
    print(f"🔍 Found {len(teams.data)} active teams")

    for team in teams.data:
        print(f"\nScraping {team['team_name']}...")
        cid       = team['youtube_channel_id']
        old_price = float(team.get('current_price') or 100.0)

        ch_stats   = get_channel_stats(cid)
        engagement = get_recent_engagement(cid)
        new_price  = calculate_price(ch_stats, engagement)

        # Work out % change vs previous price
        change_pct = round(((new_price - old_price) / old_price) * 100, 2) \
                     if old_price > 0 else 0.0

        # Update the team row
        supabase.table('fifa_teams').update({
            'current_price':    new_price,
            'price_24h_change': change_pct,
            'last_updated':     now
        }).eq('team_name', team['team_name']).execute()

        # Save to price history (powers the trend charts)
        supabase.table('fifa_price_history').insert({
            'team_name':     team['team_name'],
            'price':       new_price,
            'recorded_at': now
        }).execute()

        # Save raw metrics log
        if ch_stats:
            try:
                result = supabase.table('fifa_team_metrics').insert({
                    'team_name':          team['team_name'],
                    'recorded_at':        now,
                    'subscriber_count': ch_stats['subscribers'],
                    'recent_views_24h': engagement['views'],
                    'recent_likes_24h': engagement['likes'],
                    'calculated_price': new_price
                }).execute()
                print(f"  ✅ Metrics saved for {team['team_name']}")
            except Exception as e:
                print(f"  ❌ Metrics insert failed for {team['team_name']}: {e}")
        else:
            print(f"  ⚠️  Skipped metrics for {team['team_name']} (no channel stats)")

if __name__ == "__main__":
    main()
