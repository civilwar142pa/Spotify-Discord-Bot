import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import sys
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler
from spotipy.exceptions import SpotifyException
from pymongo import MongoClient
from flask import Flask, request
import threading
import builtins
import time

# Prevent input() from blocking/crashing in non-interactive environments
def non_blocking_input(prompt=''):
    print(f"⚠️ BLOCKED INPUT REQUEST: {prompt}")
    print("❌ Authentication failed and bot tried to prompt for input.")
    raise EOFError("Non-interactive environment")
builtins.input = non_blocking_input

print("=" * 50)
print("🚀 Starting Spotify Discord Bot on Render")
print("=" * 50)

# Get Discord token from environment
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    print("❌ ERROR: DISCORD_TOKEN environment variable is not set!")
    print("Please add it in Render dashboard → Environment")
    sys.exit(1)

# Get Channel ID for restriction (optional)
CHANNEL_ID_ENV = os.getenv('DISCORD_CHANNEL_ID')
ALLOWED_CHANNEL_ID = None
if CHANNEL_ID_ENV and CHANNEL_ID_ENV.strip().isdigit():
    ALLOWED_CHANNEL_ID = int(CHANNEL_ID_ENV)
    print(f"🔒 Bot restricted to channel ID: {ALLOWED_CHANNEL_ID}")
else:
    print("🌍 Bot is active in ALL channels (Global Mode)")

# Intents setup
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.tree.interaction_check
async def check_channel(interaction: discord.Interaction) -> bool:
    """Global check to restrict commands to a specific channel if configured"""
    if ALLOWED_CHANNEL_ID and interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"⚠️ This bot is restricted to <#{ALLOWED_CHANNEL_ID}>.", 
            ephemeral=True
        )
        return False
    return True

class MongoDBCacheHandler(CacheHandler):
    """
    Custom handler to store Spotify token in MongoDB.
    Includes a fallback to load from env var for initial migration.
    """
    def __init__(self, connection_string):
        # Parse connection string and connect
        self.client = MongoClient(connection_string)
        # Use a specific database and collection
        self.db = self.client.get_database('spotify_bot')
        self.collection = self.db.get_collection('tokens')
        
        # Test connection immediately to fail fast if auth is bad
        try:
            self.client.admin.command('ping')
            print("✅ MongoDB connection verified")
        except Exception as e:
            raise Exception(f"MongoDB Auth/Connection Failed: {e}")
        
    def get_cached_token(self):
        # 1. Check MongoDB first
        print("🔍 Checking MongoDB for cached token...")
        db_token = None
        try:
            record = self.collection.find_one({'_id': 'main_token'})
            if record:
                db_token = record['token_info']
        except Exception as e:
            print(f"⚠️ MongoDB Read Error: {e}")
            
        # 2. Check Environment Variable (for manual updates/fixes)
        env_token_str = os.getenv('SPOTIFY_TOKEN_CACHE')
        if env_token_str:
            try:
                # Clean up potential copy-paste artifacts (extra quotes)
                env_token_str = env_token_str.strip().strip("'").strip('"')
                env_token = json.loads(env_token_str)
                
                # Logic: If Env Var has a DIFFERENT refresh token than DB, user likely updated it manually.
                # Or if DB is empty, we migrate. Or if FORCE_TOKEN_RESET is true.
                force_reset = os.getenv('FORCE_TOKEN_RESET', 'false').lower() == 'true'
                should_update = False
                
                if not db_token:
                    print("📥 Migrating token from Env Var to MongoDB...")
                    should_update = True
                elif force_reset:
                    print("🚨 FORCE_TOKEN_RESET enabled: Overwriting MongoDB cache with Environment Variable.")
                    should_update = True
                elif db_token and env_token.get('refresh_token') != db_token.get('refresh_token'):
                    print("♻️ Detected NEW token in Environment Variables. Overwriting MongoDB cache...")
                    should_update = True
                
                if should_update:
                    self.save_token_to_cache(env_token)
                    return env_token

            except Exception as e:
                print(f"❌ CRITICAL: Failed to parse SPOTIFY_TOKEN_CACHE: {e}")
                print("   Check for missing brackets {} or extra quotes in the Environment Variable.")

        # 3. Return DB token if we didn't use Env token
        if db_token:
            return db_token
            
        return None

    def save_token_to_cache(self, token_info):
        try:
            # Upsert (update or insert) the token
            self.collection.update_one(
                {'_id': 'main_token'},
                {'$set': {'token_info': token_info}},
                upsert=True
            )
        except Exception as e:
            print(f"⚠️ MongoDB Write Error: {e}")

class EnvCacheHandler(CacheHandler):
    """
    Fallback handler that reads from SPOTIFY_TOKEN_CACHE environment variable.
    Used when MongoDB connection fails.
    """
    def get_cached_token(self):
        env_token_str = os.getenv('SPOTIFY_TOKEN_CACHE')
        if env_token_str:
            try:
                # Clean up potential copy-paste artifacts
                env_token_str = env_token_str.strip().strip("'").strip('"')
                print("⚠️ Using fallback token from SPOTIFY_TOKEN_CACHE")
                return json.loads(env_token_str)
            except Exception as e:
                print(f"❌ Failed to parse SPOTIFY_TOKEN_CACHE: {e}")
        return None

    def save_token_to_cache(self, token_info):
        # Cannot save to env var
        pass

class SpotifyClient:
    def __init__(self):
        print("\n🔧 Initializing Spotify Client...")
        self.storage_mode = "Unknown"
        
        # Get environment variables
        # Added .strip() to remove accidental spaces from copy-pasting
        self.client_id = os.getenv('SPOTIFY_CLIENT_ID', '').strip()
        self.client_secret = os.getenv('SPOTIFY_CLIENT_SECRET', '').strip()
        self.redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback').strip()
        self.playlist_id = os.getenv('SPOTIFY_PLAYLIST_ID', '').strip()
        
        # Debug: Show what we found
        cid_masked = f"{self.client_id[:4]}...{self.client_id[-4:]}" if self.client_id else "MISSING"
        sec_masked = f"{self.client_secret[:4]}...{self.client_secret[-4:]}" if self.client_secret else "MISSING"
        print(f"Client ID: {cid_masked}")
        print(f"Client Secret: {sec_masked}")
        print(f"Redirect URI: {self.redirect_uri}")
        print(f"Playlist ID: {'✅ Present' if self.playlist_id else '❌ MISSING'}")
        
        # Check for missing variables
        missing_vars = []
        if not self.client_id:
            missing_vars.append("SPOTIFY_CLIENT_ID")
        if not self.client_secret:
            missing_vars.append("SPOTIFY_CLIENT_SECRET")
        if not self.playlist_id:
            missing_vars.append("SPOTIFY_PLAYLIST_ID")
        
        if missing_vars:
            error_msg = f"❌ Missing Spotify environment variables: {', '.join(missing_vars)}"
            print(error_msg)
            print("\n📝 Add these in Render dashboard → Environment")
            for var in missing_vars:
                print(f"   - {var}")
            raise ValueError(error_msg)
        
        # Initialize MongoDB Cache Handler
        mongo_uri = os.getenv('MONGODB_URI')
        cache_handler = None
        
        if mongo_uri:
            try:
                print("🍃 Connecting to MongoDB...")
                cache_handler = MongoDBCacheHandler(mongo_uri)
                print("✅ MongoDB Cache Handler initialized")
                self.storage_mode = "✅ MongoDB Atlas"
            except Exception as e:
                print(f"❌ MongoDB connection failed: {e}")
                print("💡 TIP: Check your MONGODB_URI password. If it has special characters, URL encode them.")
                print("⚠️ Falling back to Environment Variable (SPOTIFY_TOKEN_CACHE)...")
                cache_handler = EnvCacheHandler()
                self.storage_mode = "⚠️ Local Fallback (DB Error)"
        else:
            print("⚠️ MONGODB_URI not found in environment variables")
            print("⚠️ Using Environment Variable fallback.")
            cache_handler = EnvCacheHandler()
            self.storage_mode = "⚠️ Local Fallback (No DB)"
        
        # Initialize Spotify OAuth
        self.auth_manager = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope='playlist-modify-public playlist-modify-private',
            cache_handler=cache_handler,
            open_browser=False,
            show_dialog=False
        )

        # Check if we have a cached token
        token_info = self.auth_manager.get_cached_token()
        if token_info:
            print(f"✅ Found cached token (expires at: {token_info.get('expires_at', 'N/A')})")
            
            # DEBUG: Attempt manual refresh if expired to catch specific errors
            if self.auth_manager.is_token_expired(token_info):
                print("⌛ Token is expired. Attempting manual refresh to debug...")
                try:
                    self.auth_manager.refresh_access_token(token_info['refresh_token'])
                    print("✅ Manual refresh successful")
                except Exception as e:
                    print(f"❌ CRITICAL REFRESH FAILURE: {e}")
                    print("   Check for trailing spaces in SPOTIFY_CLIENT_SECRET in Render.")
            
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
            
            # Test connection
            try:
                user = self.sp.current_user()
                print(f"✅ Connected to Spotify as: {user.get('display_name', user['id'])}")
            except Exception as e:
                if "Non-interactive environment" in str(e):
                    print("❌ CRITICAL: Cached token is invalid and cannot be refreshed.")
                    print("   Likely cause: SPOTIFY_CLIENT_ID in Render does not match the token's Client ID.")
                else:
                    print(f"⚠️ Spotify connection test failed: {e}")
        else:
            print("❌ No cached token found.")
            print("💡 Ensure SPOTIFY_TOKEN_CACHE is set for initial migration, or MONGODB_URI is correct.")
            print("⚠️ Bot will require authentication via /spotifyauth command")
            # Create Spotify client without auth for now
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
    
    def _refresh_token(self):
        """Force refresh the Spotify token"""
        print("🔄 Forcing token refresh...")
        max_retries = 3
        for attempt in range(max_retries):
            try:
                token_info = self.auth_manager.get_cached_token()
                if token_info and 'refresh_token' in token_info:
                    new_token = self.auth_manager.refresh_access_token(token_info['refresh_token'])
                    print("✅ Token refreshed successfully")
                    print("✅ New token saved to MongoDB automatically")
                    return
                else:
                    print("⚠️ No refresh token found in cache")
                    return
            except Exception as e:
                print(f"⚠️ Error refreshing token (Attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
        print("❌ Failed to refresh token after multiple attempts.")

    def search_and_add_top_result(self, song_query, artist_query=None):
        """Search for a song and add the top result to playlist"""
        attempts = 0
        while attempts < 2:
            try:
                # Build search query
                if artist_query:
                    search_query = f"{song_query} artist:{artist_query}"
                else:
                    search_query = song_query
                
                print(f"🔍 Searching: {search_query}")
                
                # Search for the song
                results = self.sp.search(q=search_query, type='track', limit=5)
                
                if not results['tracks']['items']:
                    return None, "No songs found with that search."
                
                # Get the top result
                track = results['tracks']['items'][0]
                track_uri = track['uri']
                track_name = track['name']
                artists = ', '.join([artist['name'] for artist in track['artists']])
                
                # Check for duplicates before adding
                print("🔍 Checking for duplicates in playlist...")
                current_tracks = self.sp.playlist_items(self.playlist_id, fields='items.track.uri')
                track_uris = {item['track']['uri'] for item in current_tracks['items'] if item['track']}
                
                if track_uri in track_uris:
                    print(f"⚠️ Duplicate found: {track_name} by {artists}")
                    return track, f"'{track_name}' by {artists} is already in the playlist."
                
                # --- End of duplicate check ---
                
                print(f"✅ Top result: {track_name} by {artists}")
                
                # Add to playlist
                self.sp.playlist_add_items(self.playlist_id, [track_uri])
                print(f"✅ Added to playlist: {self.playlist_id}")
                
                return track, f"Added '{track_name}' by {artists} to the playlist!"
                
            except SpotifyException as e:
                if e.http_status == 401 and attempts == 0:
                    print("🔄 Token expired. Refreshing...")
                    self._refresh_token()
                    attempts += 1
                    continue
                print(f"❌ Spotify error: {e}")
                return None, f"Spotify error: {e}"
            except EOFError:
                print("❌ Authorization required (EOF error)")
                return None, "❌ Bot is not authenticated. Please run `/spotifyauth` first."
            except Exception as e:
                # Handle connection resets/aborts by retrying once
                if attempts == 0 and ("Connection aborted" in str(e) or "Connection reset" in str(e)):
                    print(f"⚠️ Connection error detected: {e}")
                    print("🔄 Retrying operation...")
                    attempts += 1
                    continue
                print(f"❌ Error: {e}")
                return None, f"Error: {e}"
    
    def remove_song(self, song_query, artist_query=None):
        """Find and remove a song by searching within the playlist."""
        attempts = 0
        while attempts < 2:
            try:
                print(f"🔍 Searching playlist for '{song_query}' by '{artist_query or 'any artist'}'")

                # Fetch all tracks from the playlist, handling pagination
                all_items = []
                results = self.sp.playlist_items(self.playlist_id)
                all_items.extend(results['items'])
                while results['next']:
                    results = self.sp.next(results)
                    all_items.extend(results['items'])

                # Find the first matching track in the playlist
                track_to_remove = None
                for item in all_items:
                    if not item or not item.get('track'):
                        continue
                    
                    track = item['track']
                    track_name = track['name'].lower()
                    artist_names = [a['name'].lower() for a in track['artists']]

                    # Check for a match
                    song_matches = song_query.lower() in track_name
                    artist_matches = True  # Assume artist matches if none is provided
                    if artist_query:
                        artist_matches = any(artist_query.lower() in name for name in artist_names)

                    if song_matches and artist_matches:
                        track_to_remove = track
                        break  # Found our song, stop searching

                if track_to_remove:
                    track_uri = track_to_remove['uri']
                    track_name = track_to_remove['name']
                    artists = ', '.join([a['name'] for a in track_to_remove['artists']])
                    
                    self.sp.playlist_remove_all_occurrences_of_items(self.playlist_id, [track_uri])
                    print(f"✅ Removed: {track_name} by {artists}")
                    return track_to_remove, f"Removed '{track_name}' by {artists}"
                
                return None, f"Could not find a song matching '{song_query}' in the playlist."
                
            except SpotifyException as e:
                if e.http_status == 401 and attempts == 0:
                    print("🔄 Token expired. Refreshing...")
                    self._refresh_token()
                    attempts += 1
                    continue
                print(f"❌ Spotify error during removal: {e}")
                return None, f"A Spotify API error occurred: {e.msg}"
            except EOFError:
                print("❌ Authorization required (EOF error)")
                return None, "❌ Bot is not authenticated. Please run `/spotifyauth` first."
            except Exception as e:
                # Handle connection resets/aborts by retrying once
                if attempts == 0 and ("Connection aborted" in str(e) or "Connection reset" in str(e)):
                    print(f"⚠️ Connection error detected: {e}")
                    print("🔄 Retrying operation...")
                    attempts += 1
                    continue
                print(f"❌ Error removing song: {e}")
                return None, f"Error: {e}"
    
    def get_playlist_link(self):
        """Get the public playlist link"""
        attempts = 0
        while attempts < 2:
            try:
                playlist = self.sp.playlist(self.playlist_id)
                return playlist['external_urls']['spotify']
            except SpotifyException as e:
                if e.http_status == 401 and attempts == 0:
                    print("🔄 Token expired. Refreshing...")
                    self._refresh_token()
                    attempts += 1
                    continue
                print(f"❌ Error getting playlist link: {e}")
                return f"Error: {e}"
            except EOFError:
                return "❌ Bot is not authenticated. Please run `/spotifyauth` first."
            except Exception as e:
                # Handle connection resets/aborts by retrying once
                if attempts == 0 and ("Connection aborted" in str(e) or "Connection reset" in str(e)):
                    print(f"⚠️ Connection error detected: {e}")
                    print("🔄 Retrying operation...")
                    attempts += 1
                    continue
                print(f"❌ Error getting playlist link: {e}")
                return f"Error: {e}"

# Initialize Spotify client
spotify_init_error = None
try:
    spotify = SpotifyClient()
    print("✅ Spotify client initialized successfully!")
except Exception as e:
    print(f"❌ Failed to initialize Spotify client: {e}")
    spotify_init_error = str(e)
    spotify = None

# Create Flask app for health checks (required for Render web service)
app = Flask(__name__)

@app.route('/')
def health_check():
    return "✅ Spotify Discord Bot is running!", 200

@app.route('/health')
def health():
    return {"status": "healthy", "service": "spotify-discord-bot"}, 200

@app.route('/callback')
def spotify_callback():
    """Handle Spotify OAuth callback"""
    try:
        code = request.args.get('code')
        error = request.args.get('error')
        
        if error:
            return f"❌ Spotify authorization failed: {error}", 400
        
        if code:
            print(f"✅ Received Spotify authorization code")
            # The SpotifyOAuth instance in your main code should handle this automatically
            return "✅ Spotify authorization successful! You can close this window. The bot should now have access.", 200
        else:
            return "⚠️ No authorization code received", 400
            
    except Exception as e:
        print(f"❌ Error in callback: {e}")
        return f"Error: {str(e)}", 500

# Run Flask in a separate thread
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()
print("✅ Flask health check server started")

@tasks.loop(minutes=5)
async def spotify_keep_alive():
    """Periodically ping Spotify to keep the connection alive"""
    if spotify and hasattr(spotify, 'sp'):
        try:
            # Run blocking call in executor to avoid blocking Discord bot
            await bot.loop.run_in_executor(None, spotify.sp.current_user)
        except Exception:
            pass # Keep-alive failed, not critical

@bot.event
async def on_ready():
    print(f'\n✅ {bot.user} has connected to Discord!')
    print(f'✅ Bot ID: {bot.user.id}')
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} slash command(s)')
        
        # List available commands
        print("\n📋 Available slash commands:")
        for cmd in synced:
            print(f"   /{cmd.name} - {cmd.description}")
    except Exception as e:
        print(f'❌ Error syncing commands: {e}')
    
    # Start keep-alive loop
    if not spotify_keep_alive.is_running():
        spotify_keep_alive.start()
        print("💓 Spotify keep-alive task started")

# SLASH COMMANDS

@bot.tree.command(name="commands", description="Show all available commands")
async def show_commands(interaction: discord.Interaction):
    """Show all available commands"""
    embed = discord.Embed(
        title="🤖 Spotify Discord Bot Commands",
        description="Here are all the commands you can use:",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🎵 Music Commands",
        value=(
            "**/addsong** `<song>` `<artist(optional)>`\n"
            "Add a song to the Spotify playlist\n\n"
            "**/deletesong** `<song>` `<artist(optional)>`\n"
            "Remove a song from the playlist\n\n"
            "**/spotifylink**\n"
            "Get the link to the Spotify playlist\n\n"
            "**/link**\n"
            "Alias for /spotifylink"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Utility Commands",
        value=(
            "**/botstatus**\n"
            "Check bot and Spotify connection status\n\n"
            "**/spotifyauth**\n"
            "Get Spotify authentication URL if needed\n\n"
            "**/commands**\n"
            "Show this help message"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💡 Usage Tips",
        value=(
            "• Type `/` to see all commands\n"
            "• Most commands take a few seconds to process\n"
            "• Use specific song/artist names for best results"
        ),
        inline=False
    )
    
    embed.set_footer(text="Made with ❤️ using Discord.py and Spotipy")
    
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="addsong", description="Add a song to the Spotify playlist")
@app_commands.describe(
    query="Song name, optionally with artist (e.g., 'Bohemian Rhapsody by Queen')"
)
async def addsong(interaction: discord.Interaction, query: str):
    """Add the top search result to the playlist"""
    if spotify is None:
        msg = "❌ Spotify client is not initialized."
        if spotify_init_error:
            msg += f"\nReason: {spotify_init_error}"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    
    # Defer response since Spotify API might take time
    await interaction.response.defer()

    # Parse the query to separate song and artist
    song = query
    artist = None
    
    # Handle "Song by Artist" format (case-insensitive)
    if ' by ' in query.lower():
        split_idx = query.lower().rfind(' by ')
        song = query[:split_idx].strip()
        artist = query[split_idx + 4:].strip()
    
    try:
        # Search and add song
        track, result = spotify.search_and_add_top_result(song, artist)
        
        if track:
            # Get playlist link
            playlist_link = spotify.get_playlist_link()
            
            # Create a beautiful embed
            embed = discord.Embed(
                title="✅ Song Added to Playlist",
                color=discord.Color.green(),
                description=f"**{track['name']}** has been added!"
            )
            # Custom message for duplicates
            if result and "already in the playlist" in result:
                embed.title = "ℹ️ Song Already in Playlist"
                embed.description = f"**{track['name']}** is already on the playlist."
            
            # Add fields
            embed.add_field(name="🎵 Song", value=track['name'], inline=True)
            embed.add_field(name="🎤 Artist", value=', '.join([a['name'] for a in track['artists']]), inline=True)
            embed.add_field(name="💿 Album", value=track['album']['name'], inline=True)
            
            # Add duration
            duration_ms = track['duration_ms']
            duration_min = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            embed.add_field(name="⏱️ Duration", value=duration_min, inline=True)
            
            # Add album art thumbnail
            if track['album']['images']:
                embed.set_thumbnail(url=track['album']['images'][0]['url'])
            
            # Add Spotify links section
            links_text = f"[Open Song]({track['external_urls']['spotify']})"
            
            # Add playlist link if available
            if playlist_link and "https://open.spotify.com" in playlist_link:
                links_text += f"\n[Open Playlist]({playlist_link})"
            
            embed.add_field(
                name="🔗 Spotify Links",
                value=links_text,
                inline=False
            )
            
            # Set footer with user who added it
            embed.set_footer(text=f"Added by {interaction.user.display_name}", 
                           icon_url=interaction.user.display_avatar.url)
            
            await interaction.followup.send(embed=embed)
        else:
            # Error case
            error_embed = discord.Embed(
                title="❌ Could Not Add Song",
                description=result,
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=error_embed)
            
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An error occurred: {str(e)[:200]}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="deletesong", description="Remove a song from the playlist")
@app_commands.describe(
    query="Song name, optionally with artist (e.g., 'Bohemian Rhapsody by Queen')"
)
async def deletesong(interaction: discord.Interaction, query: str):
    """Remove a song from the playlist"""
    if spotify is None:
        msg = "❌ Spotify client is not initialized."
        if spotify_init_error:
            msg += f"\nReason: {spotify_init_error}"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    
    await interaction.response.defer()

    # Parse the query to separate song and artist
    song = query
    artist = None
    
    # Handle "Song by Artist" format (case-insensitive)
    if ' by ' in query.lower():
        split_idx = query.lower().rfind(' by ')
        song = query[:split_idx].strip()
        artist = query[split_idx + 4:].strip()

    try:
        track, result = spotify.remove_song(song, artist)
        
        if track:
            # Success embed
            embed = discord.Embed(
                title="✅ Song Removed from Playlist",
                color=discord.Color.orange(),
                description=f"**{track['name']}** has been removed from the playlist."
            )
            
            embed.add_field(name="🎵 Song", value=track['name'], inline=True)
            embed.add_field(name="🎤 Artist", value=', '.join([a['name'] for a in track['artists']]), inline=True)
            
            if track['album']['images']:
                embed.set_thumbnail(url=track['album']['images'][0]['url'])
            
            embed.set_footer(text=f"Removed by {interaction.user.display_name}",
                           icon_url=interaction.user.display_avatar.url)
            
            await interaction.followup.send(embed=embed)
        else:
            # Not found
            embed = discord.Embed(
                title="❌ Song Not Found",
                description=result,
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An error occurred: {str(e)[:200]}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="link", description="Get the link to the Spotify playlist")
async def link(interaction: discord.Interaction):
    """Get the playlist link"""
    if spotify is None:
        msg = "❌ Spotify client is not initialized."
        if spotify_init_error:
            msg += f"\nReason: {spotify_init_error}"
        await interaction.response.send_message(msg, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        link = spotify.get_playlist_link()
        
        if "https://open.spotify.com" in link:
            # Create embed with playlist link
            embed = discord.Embed(
                title="🎵 Spotify Playlist",
                description=f"Click the link below to open the playlist in Spotify!",
                color=discord.Color.green()
            )
            
            # Try to get playlist info for nicer display
            try:
                playlist = spotify.sp.playlist(spotify.playlist_id)
                if playlist['name']:
                    embed.title = f"🎵 {playlist['name']}"
                
                if playlist['description']:
                    embed.description = playlist['description'][:200]
                
                if playlist['images']:
                    embed.set_thumbnail(url=playlist['images'][0]['url'])
                
                embed.add_field(name="📊 Total Tracks", value=playlist['tracks']['total'], inline=True)
                embed.add_field(name="👤 Owner", value=playlist['owner']['display_name'], inline=True)
                
            except:
                pass  # Just show link if we can't get details
            
            embed.add_field(
                name="🔗 Playlist Link", 
                value=f"[Open in Spotify]({link})", 
                inline=False
            )
            
            await interaction.followup.send(embed=embed)
        else:
            # Error getting link
            embed = discord.Embed(
                title="❌ Error",
                description=link,
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An error occurred: {str(e)[:200]}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="spotifyauth", description="Get Spotify authentication URL if needed")
async def spotifyauth(interaction: discord.Interaction):
    """Get authentication URL"""
    await interaction.response.defer(ephemeral=True)  # Only visible to user
    
    try:
        if spotify is None:
            await interaction.followup.send("❌ Spotify client is not initialized.", ephemeral=True)
            return
            
        auth_url = spotify.auth_manager.get_authorize_url()
        
        embed = discord.Embed(
            title="🔐 Spotify Authentication",
            description="If you're getting authentication errors, click the link below:",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="🔗 Authentication Link", 
            value=f"[Click here to authenticate]({auth_url})", 
            inline=False
        )
        
        embed.add_field(
            name="📝 After Authorizing",
            value="1. You'll be redirected to a page\n2. Copy the ENTIRE URL from your browser\n3. Run the authentication script or contact bot owner",
            inline=False
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Could not get auth URL: {str(e)[:200]}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=error_embed, ephemeral=True)

@bot.tree.command(name="botstatus", description="Check bot status")
async def botstatus(interaction: discord.Interaction):
    """Check bot status"""
    await interaction.response.defer(ephemeral=True)
    
    embed = discord.Embed(
        title="🤖 Bot Status",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    
    # Bot info
    embed.add_field(name="Bot Name", value=bot.user.name, inline=True)
    embed.add_field(name="Bot ID", value=bot.user.id, inline=True)
    embed.add_field(name="Discord API", value="✅ Connected", inline=True)
    
    # Spotify status
    spotify_status = "❌ Not initialized"
    if spotify:
        try:
            # Perform a real API call to verify token validity
            user = await bot.loop.run_in_executor(None, spotify.sp.current_user)
            spotify_status = f"✅ Connected as {user.get('display_name', 'Unknown')}"
        except Exception as e:
            error_msg = str(e)
            if "Non-interactive environment" in error_msg:
                spotify_status = "❌ Token Invalid / Credentials Mismatch"
                spotify_status += "\n⚠️ Check: Client ID in Render must match the one used to generate the token."
            else:
                spotify_status = f"❌ Auth Failed: {error_msg}"
            
    embed.add_field(name="Spotify API", value=spotify_status, inline=True)
    
    if spotify:
        embed.add_field(name="Token Storage", value=spotify.storage_mode, inline=True)
    
    # Environment check
    env_vars = ['DISCORD_TOKEN', 'SPOTIFY_CLIENT_ID', 'SPOTIFY_CLIENT_SECRET', 'SPOTIFY_PLAYLIST_ID']
    missing = [var for var in env_vars if not os.getenv(var)]
    
    if missing:
        embed.add_field(name="Environment", value=f"❌ Missing: {', '.join(missing)}", inline=False)
    else:
        embed.add_field(name="Environment", value="✅ All variables set", inline=False)
    
    # Available commands
    commands_list = [
        "/addsong - Add a song",
        "/deletesong - Remove a song",
        "/link - Get playlist link",
        "/botstatus - Check status",
        "/spotifyauth - Get auth URL",
        "/commands - Show all commands"
    ]
    
    embed.add_field(
        name="Available Commands",
        value="\n".join(commands_list),
        inline=False
    )
    
    embed.set_footer(text="Spotify Discord Bot")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.event
async def on_command_error(ctx, error):
    """Handle prefix command errors (for any ! commands)"""
    if isinstance(error, commands.CommandNotFound):
        # Suggest using slash commands
        embed = discord.Embed(
            title="⚠️ Use Slash Commands",
            description="This bot uses **slash commands** (/). Type `/` to see available commands.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Available Commands", 
                       value="`/addsong` - Add a song\n`/deletesong` - Remove a song\n`/link` - Get playlist link\n`/botstatus` - Check bot status\n`/spotifyauth` - Get auth URL\n`/commands` - Show all commands",
                       inline=False)
        await ctx.send(embed=embed, delete_after=15)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle errors in slash commands"""
    if isinstance(error, app_commands.CommandInvokeError):
        print(f"❌ Command Error in /{interaction.command.name}: {error.original}")
    else:
        print(f"❌ Slash Command Error: {error}")
    
    if not interaction.response.is_done():
        await interaction.response.send_message("❌ An error occurred while processing the command. (Check logs)", ephemeral=True)

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("📋 Starting bot with configuration:")
    print(f"✅ Discord Bot: {'Ready' if TOKEN else 'Missing token'}")
    print(f"✅ Flask Server: Running on port 8080")
    print("=" * 50 + "\n")
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
        sys.exit(1)