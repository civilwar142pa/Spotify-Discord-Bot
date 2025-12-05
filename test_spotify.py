# test_spotify.py
import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

try:
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=os.getenv('SPOTIFY_CLIENT_ID'),
        client_secret=os.getenv('SPOTIFY_CLIENT_SECRET'),
        redirect_uri=os.getenv('SPOTIFY_REDIRECT_URI'),
        scope='playlist-modify-public',
        cache_path='.spotify_cache'
    ))
    
    # Test getting your profile
    user = sp.current_user()
    print(f"✅ Spotify connected as: {user['display_name']}")
    
    # Test playlist access
    playlist_id = os.getenv('SPOTIFY_PLAYLIST_ID')
    if playlist_id:
        playlist = sp.playlist(playlist_id)
        print(f"✅ Playlist found: {playlist['name']}")
        
except Exception as e:
    print(f"❌ Spotify error: {e}")
    print("\nFirst-time auth needed! Run your bot and check the terminal.")