import json
import re
import os
import requests
import subprocess
import tempfile
import uuid


from ..otsconfig import config
from ..runtimedata import get_logger, account_pool
from ..utils import make_call, conv_list_format

logger = get_logger("api.soundcloud")

BASE_URL = 'https://api-v2.soundcloud.com'
SC_SITE_URL = 'https://soundcloud.com'
VERIFY_AUTH_URL = "https://api-auth.soundcloud.com/connect/session"


def soundcloud_parse_url(url, token):
    """
    Uses the /resolve endpoint to determine if a URL is a track, playlist, etc.
    Returns the 'kind' and the ID.
    """
    headers = {"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/77.0.3865.90 Safari/537.36"
    )}
    params = {
        "client_id": token['client_id'],
        "app_version": token['app_version'],
        "app_locale": token['app_locale']
    }

    resp = make_call(f"{BASE_URL}/resolve?url={url}", headers=headers, params=params)
    item_id = str(resp["id"])
    item_type = resp["kind"]
    return item_type, item_id

def soundcloud_verify_oauth_token(client_id, token):
    """
    Verify the given OAuth token by sending it to the SoundCloud session endpoint.
    If valid, return True. Otherwise, return False.
    """
    if not token.strip():
        return False

    headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/84.0.4147.105 Safari/537.36"
    )
}
    headers["Content-Type"] = "application/json;charset=UTF-8"

    payload = {"session": {"access_token": token}}
    url = f"{VERIFY_AUTH_URL}?client_id={client_id}"

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=5)
        # If the token is valid, SoundCloud typically returns 200 and the user session info
        if resp.status_code == 200:
            return True
        else:
            logger.info(f"OAuth token invalid (status={resp.status_code}). Proceeding as guest.")
    except requests.RequestException as e:
        logger.info(f"Error verifying OAuth token: {e}")
    return False

def soundcloud_login_user(account):
    """
    Logs into a SoundCloud account and verifies OAuth tokens for premium accounts.
    Sets account status to "error" on validation failures.
    """
    logger.info('Logging into Soundcloud account...')
    try:
        if account['uuid'] not in ['public_soundcloud', 'premium_soundcloud']:
            logger.warning(f"Unhandled SoundCloud account type: {account['uuid']}")
            return False

        # Fetch main SoundCloud page
        response = requests.get("https://soundcloud.com")
        response.raise_for_status()
        page_text = response.text

        # Extract client_id URL from script tags
        client_id_url_match_iter = re.finditer(
            r'<script\s+crossorigin\s+src="([^"]+)"',
            page_text
        )
        *_, client_id_url_match = client_id_url_match_iter
        client_id_url = client_id_url_match.group(1)

        # Extract app_version
        app_version_match = re.search(
            r'<script>window\.__sc_version="(\d+)"</script>',
            page_text,
        )
        if not app_version_match:
            raise Exception("App version not found")
        app_version = app_version_match.group(1)

        # Fetch client_id from secondary script
        response2 = requests.get(client_id_url)
        response2.raise_for_status()
        client_id = re.search(r'client_id:\s*"(\w+)"', response2.text).group(1)

        # Token verification for premium accounts
        account_type = "public"
        bitrate = "128k"

        if account['uuid'] == "premium_soundcloud":
            token = account['login'].get('oauth_token')
            if not token:
                raise ValueError("Premium account missing OAuth token")
            
            if not soundcloud_verify_oauth_token(client_id, token):
                raise ValueError("OAuth token validation failed")

            account_type = "premium"
            bitrate = "256k"

        # Update account configuration
        account['login'].update({
            "client_id": client_id,
            "app_version": app_version,
            "app_locale": "en"
        })

        # Update global config
        cfg_copy = config.get('accounts').copy()
        for entry in cfg_copy:
            if entry['uuid'] == account['uuid']:
                entry.update({
                    "login": account['login'],
                    "account_type": account_type,
                    "bitrate": bitrate
                })
        config.set_('accounts', cfg_copy)
        config.update()

        # Update account pool
        account_pool.append({
            "uuid": account['uuid'],
            "username": client_id,
            "service": "soundcloud",
            "status": "active",
            "account_type": account_type,
            "bitrate": bitrate,
            "login": account['login']
        })

        logger.info(f"Soundcloud login successful ({account_type})")
        return True

    except Exception as e:
        logger.error(f"Login failed: {str(e)}")
        # Write error status to account pool
        account_pool.append({
            "uuid": account['uuid'],
            "username": account['login'].get('client_id', 'N/A'),
            "service": "soundcloud",
            "status": "error",
            "account_type": "N/A",
            "bitrate": "N/A",
            "login": {
                "client_id": "N/A",
                "app_version": "N/A",
                "app_locale": "en"
            }
        })
        return False


def soundcloud_add_account(token):
    """
    Create a new 'premium_soundcloud' account in config/account_pool.
    1) Fetch client_id, app_version from the official site.
    2) Store the user-supplied OAuth token.
    3) Mark the account as 'premium' with 'bitrate' = '256k'.
    """
    logger.info("Adding premium SoundCloud account...")

    try:
        # 1) Same approach as the public login to fetch the client_id, app_version
        response = requests.get(SC_SITE_URL)
        response.raise_for_status()
        page_text = response.text

        # script that might contain the client_id
        client_id_url_match_iter = re.finditer(
            r'<script\s+crossorigin\s+src="([^"]+)"',
            page_text
        )
        *_, client_id_url_match = client_id_url_match_iter
        client_id_url = client_id_url_match.group(1)

        app_version_match = re.search(
            r'<script>window\.__sc_version="(\d+)"</script>',
            page_text,
        )
        if app_version_match is None:
            raise Exception("Could not find app version on the main page (premium).")
        app_version = app_version_match.group(1)

        response2 = requests.get(client_id_url)
        response2.raise_for_status()
        page_text2 = response2.text

        client_id_match = re.search(r'client_id:\s*"(\w+)"', page_text2)
        if not client_id_match:
            raise Exception("Could not parse client_id from script (premium).")
        client_id = client_id_match.group(1)

        # 2) Create a new premium entry in config
        cfg_copy = config.get('accounts').copy()
        new_user = {
            "uuid": "premium_soundcloud",
            "service": "soundcloud",
            "active": True,
            "account_type": "premium",
            "bitrate": "256k",  # default for premium
            "login": {
                "client_id": client_id,
                "app_version": app_version,
                "app_locale": "en",
                "oauth_token": token  # store the user-supplied token
            }
        }
        cfg_copy.append(new_user)
        config.set_('accounts', cfg_copy)
        config.update()

        # 3) Also push into account_pool
        account_pool.append({
            "uuid": "premium_soundcloud",
            "username": client_id,
            "service": "soundcloud",
            "status": "active",
            "account_type": "premium",
            "bitrate": "256k",
            "login": {
                "client_id": client_id,
                "app_version": app_version,
                "app_locale": "en",
                "oauth_token": token
            }
        })
        logger.info("Premium SoundCloud account added successfully.")

    except Exception as exc:
        logger.error(f"Failed to add premium SoundCloud account: {exc}")


def soundcloud_get_token(parsing_index):
    """
    Returns the token dictionary from account_pool.
    Possibly includes 'oauth_token' if it's a premium account.
    """
    client_id = account_pool[parsing_index]['login']["client_id"]
    app_version = account_pool[parsing_index]['login']["app_version"]
    app_locale = account_pool[parsing_index]['login']["app_locale"]
    # optional
    oauth_token = account_pool[parsing_index]['login'].get("oauth_token")

    return {
        "client_id": client_id,
        "app_version": app_version,
        "app_locale": app_locale,
        "oauth_token": oauth_token,
    }


def _get_request_headers(token):
    """Helper to build headers (including OAuth if premium)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/77.0.3865.90 Safari/537.36"
        )
    }
    # If we have an OAuth token, add an Authorization header.
    if token.get('oauth_token'):
        headers["Authorization"] = f"OAuth {token['oauth_token']}"
    return headers


def soundcloud_get_search_results(token, search_term, content_types):
    """
    Example search function for tracks/playlists using /search endpoints.
    """
    logger.info(f"Searching SoundCloud for: {search_term}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/77.0.3865.90 Safari/537.36"
        )
    }
    params = {
        "client_id": token['client_id'],
        "app_version": token['app_version'],
        "app_locale": token['app_locale'],
        "q": search_term
    }

    search_results = []
    if 'track' in content_types:
        track_url = f"{BASE_URL}/search/tracks"
        track_search = requests.get(track_url, headers=headers, params=params).json()
        for track in track_search.get('collection', []):
            search_results.append({
                'item_id': track['id'],
                'item_name': track['title'],
                'item_by': track['user']['username'],
                'item_type': "track",
                'item_service': "soundcloud",
                'item_url': track['permalink_url'],
                'item_thumbnail_url': track.get("artwork_url")
            })

    if 'playlist' in content_types:
        playlist_url = f"{BASE_URL}/search/playlists"
        playlist_search = requests.get(playlist_url, headers=headers, params=params).json()
        for playlist in playlist_search.get('collection', []):
            search_results.append({
                'item_id': playlist['id'],
                'item_name': playlist['title'],
                'item_by': playlist['user']['username'],
                'item_type': "playlist",
                'item_service': "soundcloud",
                'item_url': playlist['permalink_url'],
                'item_thumbnail_url': playlist.get("artwork_url")
            })

    logger.info(search_results)
    return search_results


def soundcloud_get_set_items(token, url):
    """
    For a /sets (playlist) URL, returns the raw JSON from /resolve.
    """
    logger.info(f"Getting set items for {url}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/77.0.3865.90 Safari/537.36"
        )
    }
    params = {
        "client_id": token['client_id'],
        "app_version": token['app_version'],
        "app_locale": token['app_locale']
    }

    try:
        set_data = make_call(f"{BASE_URL}/resolve?url={url}", headers=headers, params=params, skip_cache=True)
        return set_data
    except (TypeError, KeyError):
        logger.info(f"Failed to parse tracks for set: {url}")


def soundcloud_get_track_metadata(token, item_id):
    """
    Fetch track metadata, parse additional info such as album name/artist, etc.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/77.0.3865.90 Safari/537.36"
        )
    }
    params = {
        "client_id": token['client_id'],
        "app_version": token['app_version'],
        "app_locale": token['app_locale']
    }

    track_data = make_call(f"{BASE_URL}/tracks/{item_id}", headers=headers, params=params)

    # Attempt to parse album from track's webpage
    track_webpage = make_call(f"{track_data['permalink_url']}/albums", text=True)
    start_index = track_webpage.find('<h2>Appears in albums</h2>')
    album_data = None
    if start_index != -1:
        album_href = re.search(r'href="([^"]*)"', track_webpage[start_index:])
        if album_href:
            album_data = make_call(
                f"{BASE_URL}/resolve?url=https://soundcloud.com{album_href.group(1)}",
                headers=headers,
                params=params
            )

    # Artists
    artists = []
    try:
        for item in track_data.get('publisher_metadata', {}).get('artist', '').split(','):
            artists.append(item.strip())
    except AttributeError:
        pass
    artists = conv_list_format(artists)
    if not artists:
        artists = track_data.get('user', {}).get('username', '')

    # Track Number
    try:
        total_tracks = album_data['track_count']
        track_number = 0
        for trk in album_data['tracks']:
            track_number += 1
            if trk['id'] == track_data['id']:
                break
        album_type = 'album'
    except (KeyError, TypeError):
        total_tracks = '1'
        track_number = '1'
        album_type = 'single'

    # Album Name
    album_name = ""
    try:
        album_name = track_data['publisher_metadata']['album_name']
    except (KeyError, TypeError):
        if start_index != -1:
            a_tag_match = re.search(r'<a[^>]*>(.*?)</a>', track_webpage[start_index:])
            if a_tag_match:
                album_name = a_tag_match.group(1)
        if album_name.startswith("Users who like"):
            album_name = track_data['title']

    # Copyright
    publisher_metadata = track_data.get('publisher_metadata', {})
    if publisher_metadata and publisher_metadata.get('c_line'):
        copyright_list = [
            item.strip() for item in publisher_metadata.get('c_line', '').split(',')
        ]
    else:
        copyright_list = ''
    copyright_data = conv_list_format(copyright_list)

    info = {}
    info['image_url'] = track_data.get("artwork_url", "")
    info['description'] = str(track_data.get("description", ""))
    info['genre'] = conv_list_format([track_data.get('genre', [])])

    label = track_data.get('label_name', "")
    if label:
        info['label'] = label
    info['item_url'] = track_data.get('permalink_url', "")

    release_date = track_data.get("release_date", "")
    last_modified = track_data.get("last_modified", "")
    info['release_year'] = release_date.split("-")[0] if release_date else last_modified.split("-")[0]

    info['title'] = track_data.get("title", "")
    info['track_number'] = track_number
    info['total_tracks'] = total_tracks
    info['length'] = str(track_data.get("media", {}).get("transcodings", [{}])[0].get("duration", 0))
    info['artists'] = artists
    info['album_name'] = album_name
    info['album_type'] = album_type
    info['album_artists'] = track_data.get('user', {}).get('username', '')
    #info['explicit'] = publisher_metadata.get('explicit', False)                   #triggered an error so I removed it
    info['copyright'] = copyright_data
    info['is_playable'] = track_data.get('streamable', '')
    info['item_id'] = track_data.get('id', '')

    return info


# -----------------------
# NEW FUNCTIONS FOR HQ, HLS, ETC.
# -----------------------

def soundcloud_fetch_track_data(token, item_id):
    """
    Retrieve the full JSON from SoundCloud /tracks/<item_id>,
    including track_authorization, media transcodings, etc.
    """
    headers = _get_request_headers(token)
    params = {
        "client_id": token['client_id'],
    }
    url = f"{BASE_URL}/tracks/{item_id}"
    track_data = requests.get(url, params=params, headers=headers)
    if track_data.status_code == 401 or track_data.status_code == 403:
        logger.error("Unauthorized to view this track. Possibly private or invalid token.")
    track_data.raise_for_status()
    track_data = track_data.json()

    return track_data


def guess_container_from_mime(mime_type):
    """
    Rough guess: 'audio/mpeg' -> 'mp3',
                 'audio/aac'  -> 'aac' (or 'm4a'),
                 etc.
    """
    mime_type = mime_type.lower()
    if "mpeg" in mime_type:  # e.g. "audio/mpeg"
        return "mp3"
    elif "mp4" in mime_type:
        return "m4a"
    elif "ogg" in mime_type:
        return "ogg"
    return "mp3" 

def filter_transcodings(transcodings_list, quality=None, preset_prefix=None):
    """Filter out encrypted transcodings"""
    filtered = []
    for t in transcodings_list:
        protocol = t.get('format', {}).get('protocol', '')
        # Skip encrypted transcodings
        if 'encrypted' in protocol:
            continue
        # Apply quality filter if specified
        if quality is not None and t.get('quality') != quality:
            continue
        # Apply preset prefix filter if specified
        if preset_prefix is not None and not t.get('preset', '').startswith(preset_prefix):
            continue
        filtered.append(t)

    return filtered


def soundcloud_download_track(token, item_id, hq=True, output_path=None):
    """
    Download a track from SoundCloud with optional HQ preference.
    Now with extra logging to see what's going on internally.
    """
    # 1) Retrieve track data
    track_data = soundcloud_fetch_track_data(token, item_id)
    transcodings = track_data.get("media", {}).get("transcodings", [])
    track_auth = track_data.get("track_authorization")

    # Log presence of OAuth token
    if token.get('oauth_token'):
        logger.info(f"[soundcloud_download_track] OAuth token FOUND. Trying HQ={hq}.")
    else:
        logger.info(f"[soundcloud_download_track] No OAuth token. HQ={hq} might not work if track is Go+ only.")

    # Log all available transcodings
    if not transcodings:
        logger.error("No transcodings found or track not streamable. Aborting.")
        raise Exception("No transcodings found or track not streamable.")

    logger.debug(f"Found {len(transcodings)} transcodings:")
    for t in transcodings:
        logger.debug(
            f"  preset={t.get('preset')} | "
            f"quality={t.get('quality')} | "
            f"mime={t.get('format',{}).get('mime_type')} | "
            f"protocol={t.get('format',{}).get('protocol')} | "
            f"url={t.get('url')}"
        )

    # 2) Choose the best transcoding
    chosen = None

    if hq:
        # Get HQ candidates (non-encrypted)
        hq_candidates = filter_transcodings(transcodings, quality='hq')
        if hq_candidates:
            chosen = hq_candidates[0]
        else:
            logger.info("[soundcloud_download_track] HQ requested but no valid HQ transcoding found. Falling back.")

    if not chosen:
        # AAC candidates (non-encrypted)
        aac_candidates = filter_transcodings(transcodings, preset_prefix='aac_')
        if aac_candidates:
            chosen = aac_candidates[0]
        else:
            # MP3 candidates (non-encrypted)
            mp3_candidates = filter_transcodings(transcodings, preset_prefix='mp3_')
            if mp3_candidates:
                chosen = mp3_candidates[0]
            else:
                # Opus candidates (non-encrypted)
                opus_candidates = filter_transcodings(transcodings, preset_prefix='opus_')
                if opus_candidates:
                    chosen = opus_candidates[0]

    if not chosen:
        logger.error("Could not find a suitable non-encrypted transcoding. Aborting.")
        raise Exception("No suitable transcoding found.")

    logger.info(f"[soundcloud_download_track] Chosen transcoding => preset={chosen['preset']} | "
                f"quality={chosen.get('quality')} | protocol={chosen['format']['protocol']} | "
                f"mime={chosen['format']['mime_type']}")

    mime_type = chosen.get("format", {}).get("mime_type", "")
    container = guess_container_from_mime(mime_type)
    
    base_path, original_ext = os.path.splitext(output_path or "")
    if not output_path:
        output_path = f"soundcloud_{item_id}.{container}"
    else:
        output_path = f"{base_path}.{container}"

        return output_path, chosen
