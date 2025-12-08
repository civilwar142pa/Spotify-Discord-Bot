import os
import json
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler

class NonInteractiveCacheHandler(CacheHandler):
    """A cache handler that never prompts for user input"""
    def __init__(self, cache_path=None):
        self.cache_path = cache_path
        self._token_info = None
        
    def get_cached_token(self):
        if self._token_info:
            return self._token_info
            
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    self._token_info = json.load(f)
                return self._token_info
            except:
                pass
        return None
    
    def save_token_to_cache(self, token_info):
        self._token_info = token_info
        if self.cache_path:
            try:
                with open(self.cache_path, 'w') as f:
                    json.dump(token_info, f)
            except:
                pass
    
    def get_access_token(self):
        token_info = self.get_cached_token()
        return token_info.get('access_token') if token_info else None