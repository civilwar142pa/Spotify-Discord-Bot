#!/usr/bin/env python3
"""
Run this locally to get Spotify credentials, then add to Render env vars
"""
import json
import os
from dotenv import load_dotenv

load_dotenv()

print("=" * 60)
print("SPOTIFY SETUP FOR RENDER DEPLOYMENT")
print("=" * 60)
print()

# First, run the authentication script
print("1. Run authenticate_spotify.py locally to get Spotify token")
input("Press Enter after you've run authenticate_spotify.py successfully...")

# Read the cache file
cache_file = '.spotify_cache'
if os.path.exists(cache_file):
    with open(cache_file, 'r') as f:
        token_data = json.load(f)
    
    print("\n✅ Token data retrieved!")
    print("\n2. Add this JSON as a RENDER environment variable:")
    print("   Variable name: SPOTIFY_TOKEN_CACHE")
    print("   Value (copy the entire JSON below):")
    print("-" * 40)
    print(json.dumps(token_data, indent=2))
    print("-" * 40)
    
    print("\n3. Also ensure these env vars are set in Render:")
    print("   - DISCORD_TOKEN")
    print("   - SPOTIFY_CLIENT_ID")
    print("   - SPOTIFY_CLIENT_SECRET")
    print("   - SPOTIFY_REDIRECT_URI (use: http://localhost:8888/callback)")
    print("   - SPOTIFY_PLAYLIST_ID")
else:
    print(f"\n❌ Cache file '{cache_file}' not found.")
    print("Run: python authenticate_spotify.py first.")