import discord
from discord import app_commands
from discord.ext import commands
import os
import sys
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException
from flask import Flask, request
import threading

print("=" * 50)
print("🚀 Starting Spotify Discord Bot on Render")
print("=" * 50)

# Get Discord token from environment
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    print("❌ ERROR: DISCORD_TOKEN environment variable is not set!")
    print("Please add it in Render dashboard → Environment")
    sys.exit(1)

# Intents setup
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

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
        
        # Handle token cache for Render
        cache_path = '/tmp/.spotify_cache'
        token_json = os.getenv('SPOTIFY_TOKEN_CACHE')
        
        if token_json:
            try:
                print("📥 Loading token from SPOTIFY_TOKEN_CACHE environment variable")
                # Parse and save token to file
                token_info = json.loads(token_json)
                with open(cache_path, 'w') as f:
                    json.dump(token_info, f)
                print(f"✅ Token saved to {cache_path}")
            except Exception as e:
                print(f"⚠️ Could not write token cache: {e}")
        else:
            print("ℹ️ No SPOTIFY_TOKEN_CACHE found in environment")
        
        # Initialize Spotify OAuth
        self.auth_manager = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope='playlist-modify-public playlist-modify-private',
            cache_path=cache_path,
            open_browser=False,
            show_dialog=False
        )

        # Check if we have a cached token
        token_info = self.auth_manager.get_cached_token()
        if token_info:
            print(f"✅ Found cached token (expires at: {token_info.get('expires_at', 'N/A')})")
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
        else:
            print("❌ No cached token found.")
            print("💡 You need to provide a token via SPOTIFY_TOKEN_CACHE environment variable.")
            print("Run locally: python authenticate_spotify.py")
            print("Then copy the JSON output to Render as SPOTIFY_TOKEN_CACHE")
            # Create Spotify client without auth for now
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
        
        # Test connection
        try:
            user = self.sp.current_user()
            print(f"✅ Connected to Spotify as: {user.get('display_name', user['id'])}")
        except Exception as e:
            print(f"⚠️ Spotify connection test failed: {e}")
            print("This might be okay - token might need refresh on first API call")
    
    def search_and_add_top_result(self, song_query, artist_query=None):
        """Search for a song and add the top result to playlist"""
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
            
            print(f"✅ Top result: {track_name} by {artists}")
            
            # Add to playlist
            self.sp.playlist_add_items(self.playlist_id, [track_uri])
            print(f"✅ Added to playlist: {self.playlist_id}")
            
            return track, f"Added '{track_name}' by {artists} to the playlist!"
            
        except SpotifyException as e:
            print(f"❌ Spotify error: {e}")
            return None, f"Spotify error: {e}"
        except Exception as e:
            print(f"❌ Error: {e}")
            return None, f"Error: {e}"
    
    def remove_song(self, song_query, artist_query=None):
        """Remove a song from the playlist by searching"""
        try:
            # Build search query for removal
            search_text = song_query
            if artist_query:
                search_text += f" {artist_query}"
            
            # Get current playlist tracks
            playlist = self.sp.playlist(self.playlist_id)
            tracks = playlist['tracks']['items']
            
            print(f"🔍 Searching playlist for: {search_text}")
            
            # Search through playlist tracks
            for item in tracks:
                track = item['track']
                track_name = track['name'].lower()
                artist_names = ' '.join([a['name'].lower() for a in track['artists']])
                
                # Check if query matches track name or artist
                if (search_text.lower() in track_name or 
                    search_text.lower() in artist_names or
                    (artist_query and artist_query.lower() in artist_names)):
                    
                    # Remove the track
                    self.sp.playlist_remove_all_occurrences_of_items(
                        self.playlist_id, [track['uri']]
                    )
                    
                    print(f"✅ Removed: {track['name']} by {', '.join([a['name'] for a in track['artists']])}")
                    return track, f"Removed '{track['name']}' by {', '.join([a['name'] for a in track['artists']])}"
            
            return None, "Song not found in playlist."
            
        except Exception as e:
            print(f"❌ Error removing song: {e}")
            return None, f"Error: {e}"
    
    def get_playlist_link(self):
        """Get the public playlist link"""
        try:
            playlist = self.sp.playlist(self.playlist_id)
            return playlist['external_urls']['spotify']
        except Exception as e:
            print(f"❌ Error getting playlist link: {e}")
            return f"Error: {e}"

# Initialize Spotify client
try:
    spotify = SpotifyClient()
    print("✅ Spotify client initialized successfully!")
except Exception as e:
    print(f"❌ Failed to initialize Spotify client: {e}")
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
    app.run(host='0.0.0.0', port=8080, debug=False)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()
print("✅ Flask health check server started on port 8080")

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
            "Get the link to the Spotify playlist"
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
    song="Name of the song",
    artist="Artist name (optional)"
)
async def addsong(interaction: discord.Interaction, song: str, artist: str = None):
    """Add the top search result to the playlist"""
    if spotify is None:
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
        return
    
    # Defer response since Spotify API might take time
    await interaction.response.defer()
    
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
                description=f"**{track['name']}** has been added to the playlist!"
            )
            
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
    song="Name of the song to remove",
    artist="Artist name (optional)"
)
async def deletesong(interaction: discord.Interaction, song: str, artist: str = None):
    """Remove a song from the playlist"""
    if spotify is None:
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
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

@bot.tree.command(name="spotifylink", description="Get the link to the Spotify playlist")
async def spotifylink(interaction: discord.Interaction):
    """Get the playlist link"""
    if spotify is None:
        await interaction.response.send_message("❌ Spotify client is not initialized. Check bot logs.", ephemeral=True)
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
        "/spotifylink - Get playlist link",
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
                       value="`/addsong` - Add a song\n`/deletesong` - Remove a song\n`/spotifylink` - Get playlist link\n`/botstatus` - Check bot status\n`/spotifyauth` - Get auth URL\n`/commands` - Show all commands",
                       inline=False)
        await ctx.send(embed=embed, delete_after=15)

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