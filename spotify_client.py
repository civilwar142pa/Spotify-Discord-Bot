import spotipy
from spotipy.oauth2 import SpotifyOAuth
from config import Config

class SpotifyClient:
    def __init__(self):
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=Config.SPOTIFY_CLIENT_ID,
            client_secret=Config.SPOTIFY_CLIENT_SECRET,
            redirect_uri=Config.SPOTIFY_REDIRECT_URI,
            scope='playlist-modify-public playlist-modify-private',
            cache_path='.spotify_cache'
        ))
        self.playlist_id = Config.PLAYLIST_ID
    
    def search_song(self, query):
        """Search for a song on Spotify"""
        results = self.sp.search(q=query, type='track', limit=5)
        return results['tracks']['items']
    
    def add_to_playlist(self, track_uri):
        """Add a track to the playlist"""
        self.sp.playlist_add_items(self.playlist_id, [track_uri])
    
    def remove_from_playlist(self, track_uri):
        """Remove a track from the playlist"""
        self.sp.playlist_remove_all_occurrences_of_items(
            self.playlist_id, [track_uri]
        )
    
    def get_playlist_link(self):
        """Get the public playlist link"""
        playlist = self.sp.playlist(self.playlist_id)
        return playlist['external_urls']['spotify']