import os
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import webbrowser

load_dotenv()

print("🔐 SPOTIFY AUTHENTICATION REQUIRED")
print("=" * 50)

client_id = os.getenv('SPOTIFY_CLIENT_ID')
client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI')

print(f"Using redirect URI: {redirect_uri}")
print()

# Remove old cache if exists
cache_file = '.spotify_cache'
if os.path.exists(cache_file):
    print(f"🗑️  Removing old cache: {cache_file}")
    os.remove(cache_file)

# Create OAuth manager
sp_oauth = SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope='playlist-modify-public playlist-modify-private',
    cache_path=cache_file,
    show_dialog=True  # Always show consent screen
)

# Get authorization URL
auth_url = sp_oauth.get_authorize_url()
print(f"\n1️⃣  OPEN THIS URL IN YOUR BROWSER:")
print(f"\n{auth_url}\n")

# Try to open browser automatically
try:
    webbrowser.open(auth_url)
    print("✅ Browser opened automatically!")
except:
    print("⚠️  Could not open browser automatically")

print("\n2️⃣  AUTHORIZE THE APP:")
print("- Login with your Spotify account")
print("- Click 'Agree' to grant permissions")
print()

print("3️⃣  AFTER AUTHORIZATION:")
print("- You'll be redirected to a blank/error page (this is normal)")
print("- COPY THE ENTIRE URL FROM YOUR BROWSER'S ADDRESS BAR")
print("- It should start with your redirect URI")
print()

# Get the redirect URL from user
redirect_response = input("📝 PASTE THE REDIRECT URL HERE: ").strip()

print("\n🔄 Processing authorization...")

try:
    # Parse the code from redirect URL
    code = sp_oauth.parse_response_code(redirect_response)
    
    # Get access token
    token_info = sp_oauth.get_access_token(code)
    
    print(f"\n🎉 SUCCESS! Authentication complete!")
    print(f"✅ Token expires in: {token_info['expires_in']} seconds")
    print(f"✅ Token saved to: {cache_file}")
    
    # Test the token
    sp = spotipy.Spotify(auth=token_info['access_token'])
    user = sp.current_user()
    print(f"✅ Logged in as: {user.get('display_name', user['id'])}")
    
    # Show playlist info if available
    playlist_id = os.getenv('SPOTIFY_PLAYLIST_ID')
    if playlist_id:
        try:
            playlist = sp.playlist(playlist_id)
            print(f"✅ Playlist: {playlist['name']} ({playlist['tracks']['total']} tracks)")
        except:
            print("⚠️  Could not access playlist - check PLAYLIST_ID in .env")
    
except Exception as e:
    print(f"\n❌ AUTHENTICATION FAILED: {e}")
    print(f"\nDebug info:")
    print(f"- Redirect URI used: {redirect_uri}")
    print(f"- Redirect response: {redirect_response[:100]}...")
    
    if "invalid_grant" in str(e):
        print("\n💡 TIP: The authorization code might have expired.")
        print("Try the process again quickly.")
    elif "invalid_client" in str(e):
        print("\n💡 TIP: Check your SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env")