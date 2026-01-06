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
import csv
import io
import random
import aiohttp

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
        
    def get_cached_token(self):
        # Try to get from DB
        print("🔍 Checking MongoDB for cached token...")
        record = self.collection.find_one({'_id': 'main_token'})
        if record:
            return record['token_info']
            
        # Fallback: Check environment variable (migration helper)
        env_token = os.getenv('SPOTIFY_TOKEN_CACHE')
        if env_token:
            try:
                print("📥 Migrating token from Env Var to MongoDB...")
                token_info = json.loads(env_token)
                self.save_token_to_cache(token_info)
                return token_info
            except Exception as e:
                print(f"⚠️ Migration failed: {e}")
        return None

    def save_token_to_cache(self, token_info):
        # Upsert (update or insert) the token
        self.collection.update_one(
            {'_id': 'main_token'},
            {'$set': {'token_info': token_info}},
            upsert=True
        )

class GameHistoryManager:
    """Tracks which users have already been picked for the game"""
    def __init__(self, connection_string):
        self.collection = None
        self.local_history = set()
        
        if connection_string:
            try:
                client = MongoClient(connection_string)
                db = client.get_database('spotify_bot')
                self.collection = db.get_collection('game_history')
                print("✅ Game History Manager connected to MongoDB")
            except Exception as e:
                print(f"⚠️ Game History Manager failed to connect: {e}")

    def get_used_users(self):
        """Get list of names that have already been picked"""
        if self.collection is not None:
            return [doc['name'] for doc in self.collection.find()]
        return list(self.local_history)

    def mark_user(self, name):
        """Mark a user as picked"""
        if self.collection is not None:
            self.collection.update_one({'name': name}, {'$set': {'timestamp': time.time()}}, upsert=True)
        else:
            self.local_history.add(name)

    def reset(self):
        """Clear the history"""
        if self.collection is not None:
            self.collection.delete_many({})
        else:
            self.local_history.clear()

class SpotifyClient:
    def __init__(self):
        print("\n🔧 Initializing Spotify Client...")
        
        # Get environment variables
        self.client_id = os.getenv('SPOTIFY_CLIENT_ID')
        self.client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
        self.redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback')
        self.playlist_id = os.getenv('SPOTIFY_PLAYLIST_ID')
        
        # Debug: Show what we found
        print(f"Client ID: {'✅ Present' if self.client_id else '❌ MISSING'}")
        print(f"Client Secret: {'✅ Present' if self.client_secret else '❌ MISSING'}")
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
            except Exception as e:
                print(f"❌ MongoDB connection failed: {e}")
                # We will fail loudly later if auth_manager tries to use None
        else:
            print("⚠️ MONGODB_URI not found in environment variables")
            if os.getenv('SPOTIFY_TOKEN_CACHE'):
                print("❌ CRITICAL: SPOTIFY_TOKEN_CACHE is set, but MONGODB_URI is missing!")
                print("   The bot cannot migrate the token if it cannot connect to MongoDB.")
                print("   Please add MONGODB_URI to your Render environment variables.")
        
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
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
            
            # Test connection
            try:
                user = self.sp.current_user()
                print(f"✅ Connected to Spotify as: {user.get('display_name', user['id'])}")
            except Exception as e:
                print(f"⚠️ Spotify connection test failed: {e}")
                print("This might be okay - token might need refresh on first API call")
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

    def get_track_info(self, song_name):
        """Search for a song and return (name, artist, url)"""
        try:
            query = song_name
            # Handle "Song by Artist" format from spreadsheet
            if ' by ' in song_name.lower():
                split_idx = song_name.lower().rfind(' by ')
                track_name = song_name[:split_idx].strip()
                artist_name = song_name[split_idx + 4:].strip()
                query = f"track:{track_name} artist:{artist_name}"
            
            results = self.sp.search(q=query, type='track', limit=1)
            if results['tracks']['items']:
                track = results['tracks']['items'][0]
                name = track['name']
                artist = ', '.join([a['name'] for a in track['artists']])
                url = track['external_urls']['spotify']
                return {'name': name, 'artist': artist, 'url': url}
        except Exception as e:
            print(f"Error searching for {song_name}: {e}")
        return None

# Initialize Spotify client
try:
    spotify = SpotifyClient()
    print("✅ Spotify client initialized successfully!")
except Exception as e:
    print(f"❌ Failed to initialize Spotify client: {e}")
    spotify = None

# Initialize Game History
game_history = GameHistoryManager(os.getenv('MONGODB_URI'))

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

# --- GUESSING GAME LOGIC ---

# Global state for the current game round
active_game = None

async def fetch_game_data():
    """Fetch and parse the Google Sheet CSV data"""
    sheet_id = '1u0tu93AseqxiG9faphmfDK8w1bWYGNsNsdKxkRkuSHo'
    # Add timestamp to prevent caching
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&t={int(time.time())}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"❌ Failed to fetch CSV: {resp.status}")
                    return []
                text = await resp.text()
                
        f = io.StringIO(text)
        reader = csv.DictReader(f)
        
        # Normalize headers to handle variations (case, whitespace)
        headers = reader.fieldnames
        if not headers:
            return []
            
        # Create a map of normalized keys to actual headers
        # e.g. "song 1" -> "Song 1 "
        header_map = {h.strip().lower(): h for h in headers}
        
        users = []
        
        for row in reader:
            # 1. Find Name
            # Check for specific keys first
            name_key = None
            if 'your name' in header_map:
                name_key = header_map['your name']
            elif 'name' in header_map:
                name_key = header_map['name']
            
            name = row.get(name_key) if name_key else None
            
            # Fallback: Use 2nd column (index 1) if name key not found
            if not name:
                values = list(row.values())
                if len(values) > 1:
                    name = values[1]
            
            if not name or not name.strip():
                continue
                
            # 2. Find Songs
            songs = []
            # Look for "song 1", "song 2", etc.
            for i in range(1, 5):
                key = f"song {i}"
                if key in header_map:
                    s = row.get(header_map[key])
                    if s and s.strip():
                        songs.append(s.strip())
            
            # Fallback: Use columns 3-6 (indices 2-5)
            if not songs:
                values = list(row.values())
                # Assuming: Timestamp, Name, Song1, Song2, Song3, Song4
                if len(values) >= 6:
                    for v in values[2:6]:
                        if v and v.strip():
                            songs.append(v.strip())
                        
            if songs:
                users.append({'name': name.strip(), 'songs': songs})
        
        # Deduplicate users by name (keep latest entry)
        unique_map = {u['name']: u for u in users}
        users = list(unique_map.values())
        
        print(f"📊 Fetched {len(users)} unique users from spreadsheet")
        return users
    except Exception as e:
        print(f"❌ Error fetching game data: {e}")
        return []

class GuessButton(discord.ui.Button):
    def __init__(self, label):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
    
    async def callback(self, interaction: discord.Interaction):
        view = self.view
        user_id = interaction.user.id
        
        if user_id in view.votes:
            if view.votes[user_id] == self.label:
                await interaction.response.send_message(f"You already voted for **{self.label}**!", ephemeral=True)
            else:
                view.votes[user_id] = self.label
                await interaction.response.send_message(f"🔄 Vote changed to **{self.label}**!", ephemeral=True)
        else:
            view.votes[user_id] = self.label
            await interaction.response.send_message(f"🗳️ Voted for **{self.label}**!", ephemeral=True)

class GuessGameView(discord.ui.View):
    def __init__(self, correct_answer, options):
        super().__init__(timeout=30)
        self.correct_answer = correct_answer
        self.votes = {}
        self.message = None
        for option in options:
            self.add_item(GuessButton(option))
            
    async def on_timeout(self):
        if not self.message:
            return
            
        # Tally votes
        vote_counts = {}
        for vote in self.votes.values():
            vote_counts[vote] = vote_counts.get(vote, 0) + 1
            
        # Disable buttons and show results
        for child in self.children:
            child.disabled = True
            count = vote_counts.get(child.label, 0)
            
            if child.label == self.correct_answer:
                child.style = discord.ButtonStyle.success
                child.label = f"{child.label} (Correct! - {count})"
            else:
                child.style = discord.ButtonStyle.secondary
                child.label = f"{child.label} ({count})"
        
        # Create result text
        winners = [f"<@{uid}>" for uid, vote in self.votes.items() if vote == self.correct_answer]
        
        result_text = f"⏰ **Poll Ended!**\n\n✅ The correct answer was: **{self.correct_answer}**"
        if winners:
            result_text += f"\n🎉 **Winners:** {', '.join(winners)}"
        else:
            result_text += "\n❌ No one guessed correctly!"
            
        try:
            await self.message.edit(content=result_text, view=self)
        except Exception as e:
            print(f"Error updating game message: {e}")

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
            "**/guess**\n"
            "Play the music guessing game\n\n"
            "**/random**\n"
            "Generate a mystery playlist with Spotify links\n\n"
            "**/resetgame**\n"
            "Reset the list of picked users\n\n"
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
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
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
        track, result = await bot.loop.run_in_executor(None, spotify.search_and_add_top_result, song, artist)
        
        if track:
            # Get playlist link
            playlist_link = await bot.loop.run_in_executor(None, spotify.get_playlist_link)
            
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
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
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
        track, result = await bot.loop.run_in_executor(None, spotify.remove_song, song, artist)
        
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
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        link = await bot.loop.run_in_executor(None, spotify.get_playlist_link)
        
        if "https://open.spotify.com" in link:
            # Create embed with playlist link
            embed = discord.Embed(
                title="🎵 Spotify Playlist",
                description=f"Click the link below to open the playlist in Spotify!",
                color=discord.Color.green()
            )
            
            # Try to get playlist info for nicer display
            try:
                playlist = await bot.loop.run_in_executor(None, lambda: spotify.sp.playlist(spotify.playlist_id))
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

@bot.tree.command(name="random", description="Pick a random user's songs for the guessing game")
async def random_songs(interaction: discord.Interaction):
    """Generate a mystery playlist with Spotify links"""
    global active_game
    await interaction.response.defer()
    
    users = await fetch_game_data()
    if len(users) < 4:
        await interaction.followup.send("❌ Not enough data in the spreadsheet to play (need at least 4 users).")
        return

    # Filter out users who have already been picked
    used_names = game_history.get_used_users()
    available_users = [u for u in users if u['name'] not in used_names]
    
    if not available_users:
        await interaction.followup.send("⚠️ **All users have been picked!**\nRun `/resetgame` to clear the history and start over.")
        return

    target = random.choice(available_users)
    game_history.mark_user(target['name'])
    
    others = [u for u in users if u['name'] != target['name']]
    if len(others) < 3:
         await interaction.followup.send("❌ Not enough unique users to generate decoys.")
         return

    decoys = random.sample(others, 3)
    options = [target['name']] + [d['name'] for d in decoys]
    random.shuffle(options)
    
    # Get Spotify Links
    songs_display = []
    for song in target['songs']:
        info = None
        if spotify:
            info = await bot.loop.run_in_executor(None, spotify.get_track_info, song)
        
        if info:
            songs_display.append(f"• [{info['name']} - {info['artist']}]({info['url']})")
        else:
            songs_display.append(f"• {song}")

    active_game = {
        'target_name': target['name'],
        'options': options,
        'songs_display': songs_display
    }
    
    # Create Embed
    embed = discord.Embed(
        title="🎲 Mystery Playlist Generated!",
        description="Listen to the songs below and use `/guess` to vote for who picked them!",
        color=discord.Color.gold()
    )
    embed.add_field(name="The Songs", value="\n".join(songs_display), inline=False)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="resetgame", description="Reset the list of users who have been picked")
async def resetgame(interaction: discord.Interaction):
    """Clear the game history so users can be picked again"""
    game_history.reset()
    await interaction.response.send_message("🔄 **Game History Reset!**\nAll users in the spreadsheet can now be picked again.")

@bot.tree.command(name="guess", description="Start the voting poll for the current mystery playlist")
async def guess(interaction: discord.Interaction):
    """Start the voting poll"""
    global active_game
    
    if not active_game:
        await interaction.response.send_message("❌ No active game! Run `/random` first to generate a playlist.", ephemeral=True)
        return

    await interaction.response.defer()

    embed = discord.Embed(
        title="🎵 Who's Playlist Is This?",
        description="Vote for the person you think chose the songs!",
        color=discord.Color.purple()
    )
    
    # Show songs again for context
    embed.add_field(name="The Songs", value="\n".join(active_game['songs_display']), inline=False)
    embed.set_footer(text="Vote by clicking a button below! Results in 30s.")
    
    view = GuessGameView(active_game['target_name'], active_game['options'])
    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg

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
    spotify_status = "✅ Connected" if spotify else "❌ Not connected"
    embed.add_field(name="Spotify API", value=spotify_status, inline=True)
    
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
        "/guess - Play guessing game",
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
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ An error occurred while processing the command. (Check logs)", ephemeral=True)
        else:
            await interaction.response.send_message("❌ An error occurred while processing the command. (Check logs)", ephemeral=True)
    except Exception as e:
        print(f"❌ Could not send error message to user: {e}")

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