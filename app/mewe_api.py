import json
from http import cookiejar
from requests import Session, get, post
from requests_cache import CachedSession, DO_NOT_CACHE, NEVER_EXPIRE
from threading import Lock
from urllib import parse as p

from config import cookie_storage, proxy
from utils import TimeoutHTTPAdapter
from mewe_cfg import MeweConfig


class Mewe:
  '''Workhorse for storing web session and accessing MeWe API
  '''
  session = None
  identity = None
  base = 'https://mewe.com/api'
  last_streamed_response = None
  refresh_lock = None
  emojis = None

  _cache_defs = {
    '*/api/v2/mycontacts/user/*': 60 * 60 * 24 * 180,
    '*/api/v2/comments/*/photo/*': 60 * 60 * 24 * 30,
    '*/api/v2/photo/cm': DO_NOT_CACHE,
    '*/api/v2/photo/pt': DO_NOT_CACHE,
    '*/api/v2/photo/*': NEVER_EXPIRE,
    '*/api/v2/video/*': NEVER_EXPIRE,
    '*/api/v3/auth/identify': DO_NOT_CACHE,
    '*/api/v2/me/info': DO_NOT_CACHE,
    '*/api/v2/home/post/*': 5,  # Post and comments update cooldown
    '*/api/v2/comments/*/replies': 5,  # Replies update cooldown
    '*/api/v2/home/allfeed': 5,
    '*/api/v2/home/user/*/postsfeed': 5,  # For accidental F5's
    '*': 5,  # Prevent accidential re-requests e.g. when loading same preview image from post and board view
  }

  _ignores = (
    'access-token', 'cdn-exp', 'x-csrf-token', 'Cookie', 'trace-id', 'Via', 'X-Amz-Cf-Pop', 'X-Amz-Cf-Id'
  )

  def __init__(self):
    '''Things to do:
    1. Fetch cookies from cookie jar
    2. Init requests session with them, set headers
    3. Execute /identify method to update tokens
    3. Extract CSRF cookie and add it to headers as x-csrf-token
    4. Try executing /me method to ensure session is usable
    '''
    config = MeweConfig()

    cookie_jar = cookiejar.MozillaCookieJar(cookie_storage)
    cookie_jar.load(ignore_discard=True, ignore_expires=True)

    # session = CachedSession(
      # 'session_cache',
      # backend='sqlite',
      # cache_control=False,
      # ignored_parameters=self._ignores,
      # match_headers=True,
      # stale_if_error=True,
      # urls_expire_after=self._cache_defs,
    # )

    session = Session()

    session.cookies = cookie_jar
    session.headers['user-agent'] = config.user_agent

    # Is this an adequate way to provide session timeout setup?
    session.mount('http://', TimeoutHTTPAdapter(timeout=15))
    session.mount('https://', TimeoutHTTPAdapter(timeout=15))
    if proxy is not None:
      session.proxies.update(proxy)

    r = session.get(f'{self.base}/v3/auth/identify')
    if not r.ok or not r.json().get('authenticated', False):
      raise ValueError(f'Failed to identify user, are cookies fresh enough? Result: {r.text}')

    try:
      session.headers['x-csrf-token'] = session.cookies._cookies['.mewe.com']['/']['csrf-token'].value
    except KeyError:
      raise KeyError('Failed to extract CSRF token from /identify operation')

    self.identity = session.get(f'{self.base}/v2/me/info').json()
    self.session = session
    self.config = config
    self.refresh_lock = Lock()

  def _invoke(self, method, endpoint, **kwargs):
    '''Base method to wrap an arbitary requests http method with session session validation and some
    checks.
    '''
    self.refresh_session()
    response = method(endpoint, **kwargs)

    if not response.ok:
      if response.json().get('message', '') == 'Forbidden':
        # This path is intended to allow fallback processing, e.g. redirect user to login page and ask
        # for a new set of credential cookies. Right now we just inform user with exception, though.
        raise ValueError(f'Mewe Session died, please provide new credentials.\n{response.text}')
      else:
        response.raise_for_status()

    # Here we make an assumption that any non-zero length response is encoded as JSON, except for empty
    # responses. Anything else will be raised by code above.
    if response.text:
      return response.json()


  def is_token_expired(self):
    access_token = self.session.cookies._cookies['.mewe.com']['/'].get('access-token')
    return access_token and access_token.is_expired()

  def invoke_get(self, endpoint, payload=None):
    return self._invoke(self.session.get, endpoint, params=payload)

  def invoke_post(self, endpoint, **kwargs):
    return self._invoke(self.session.post, endpoint, **kwargs)

  def session_ok(self):
    '''Checks if current session is still usable (e.g. no logout occurred due to API abuse or refresh token
    expiry.
    '''
    r = self.session.get(f'{self.base}/v3/auth/identify')
    if r.ok and r.json().get('authenticated', False):
      return True
    else:
      print('Warning, unusable session:' + r.json())
      return False

  def reload_session(self):
    '''This loads new sets of cookies and recreates the session object
    '''
    cookie_jar = cookiejar.MozillaCookieJar(cookie_storage)
    cookie_jar.load(ignore_discard=True, ignore_expires=True)

    self.session.cookies = cookie_jar

    r = self.session.get(f'{self.base}/v3/auth/identify')
    if not r.ok or not r.json().get('authenticated', False):
      raise ValueError(f'Failed to identify user, are cookies fresh enough? Result: {r.text}')

    try:
      self.session.headers['x-csrf-token'] = self.session.cookies._cookies['.mewe.com']['/']['csrf-token'].value
    except KeyError:
      raise KeyError('Failed to extract CSRF token from /identify operation')

    r = self.session.get(f'{self.base}/v2/me/info')
    self.identity = r.json()

  def refresh_session(self):
    '''Checks current access token and receive new one accordingly faster than reloading session alltogether
    '''

    self.refresh_lock.acquire()
    try:
      if not self.is_token_expired():
        self.session.cookies.save(ignore_discard=True, ignore_expires=True)
        return

      # Force-close last streamed connection in case it is still hogging up session
      if self.last_streamed_response is not None:
        self.last_streamed_response.close()
        self.last_streamed_response = None

      r = self.session.get(f'{self.base}/v3/auth/identify')

      if not r.ok or not r.json().get('authenticated', False):
        raise ConnectionError('Failed to identify user, are cookies fresh enough?')

      try:
        self.session.headers['x-csrf-token'] = \
          self.session.cookies._cookies['.mewe.com']['/']['csrf-token'].value

      except KeyError:
        if self.session.headers.get('x-csrf-token') is None:
          raise EnvironmentError(
            'Failed to extract CSRF token from auth/identify operation and no usable '
            'token exists in current session.')

      self.session.cookies.save(ignore_discard=True, ignore_expires=True)

    finally:
      self.refresh_lock.release()

  def whoami(self):
    '''Invokes me/info method to update info on current user. Useful to check API usability.
    '''
    self.refresh_session()
    r = self.invoke_get(f'{self.base}/v2/me/info')
    return r

  def get_user_info(self, user_id):
    '''Invokes mycontacts/user/{user_id} method to fetch information about a user by their ID, contacts only.
    '''
    r = self.invoke_get(f'{self.base}/v2/mycontacts/user/{user_id}')
    return r

  def _get_feed(self, endpoint, limit=None, pages=1, before=None):
    '''Method to loop through pages that return feed objects along with respective users.
    For the time being at least 4 endpoints return that type:
      Main feed, Group feed, User feed, Post comments (lol)
    '''

    feed = []
    users = {}  # We'll store users in a dictionary for convenience
    payload = {}

    for page in range(pages):
      # We'll loop through requested number of pages filling global feed/users objects here.
      if not page and limit:  # range start from 0 so, eeh
        payload['limit'] = limit
        if before:
          payload['b'] = before

      response = self.invoke_get(endpoint, payload)

      if response['feed'] == []:
        raise IndexError('Empty, either you\'ve reached the end, or this is a private profile.')

      page_feed = response['feed']
      page_users_list = response.get('users', [])

      next_link = response['_links'].get('nextPage', {'href': None})['href']

      # Our usable list will be accessible by user_id
      users = {user['id']: user for user in page_users_list}
      feed.extend(page_feed)

      if next_link is None:
        break

      payload = p.parse_qs(p.urlsplit(next_link).query)
      if limit:
        payload['limit'] = [limit]

    return feed, users

  def get_feed(self, limit=30, pages=1, before=None):
    '''Invokes home/allfeed method to fetch home feed.
    '''
    endpoint = f'{self.base}/v2/home/allfeed'
    return self._get_feed(endpoint, limit, pages, before)

  def get_user_feed(self, user_id, limit=30, pages=1, before=None):
    '''Invokes home/user/{user_id}/postsfeed method to fetch single user posts
    '''
    endpoint = f'{self.base}/v2/home/user/{user_id}/postsfeed'
    return self._get_feed(endpoint, limit, pages, before)

  def get_post(self, post_id):
    '''Invokes home/post/{post_id} method to fetch single post.
    '''
    endpoint = f'{self.base}/v2/home/post/{post_id}'

    return self.invoke_get(endpoint)

  def get_post_comments(self, post_id, limit=100):
    '''Invokes home/post/{post_id}/comments method to fetch single user posts
    '''
    endpoint = f'{self.base}/v2/home/post/{post_id}/comments'
    payload = {'maxResults': limit}

    return self.invoke_get(endpoint, payload)

  def get_comment_replies(self, comment_id, limit=100):
    '''Invokes comments/{comment_id}/replies method to fetch single comment replies
    '''
    endpoint = f'{self.base}/v2/comments/{comment_id}/replies'
    payload = {'maxResults': limit}

    return self.invoke_get(endpoint, payload)

  def get_post_medias(self, post, limit=100):
    '''Invokes home/user/{user_id}/media request to fetch media from associated post
    '''

    user_id = post['userId']
    post_id = post['postItemId']
    first_media_id = post['medias'][0]['postItemId']

    endpoint = f'{self.base}/v2/home/user/{user_id}/media'
    payload = {
      'skipVideos': 0,
      'postItemId': first_media_id,
      'before': 0,
      'multiPostId': post_id,
      'after': limit,
      'order': 1}

    response = self.invoke_get(endpoint, payload)

    medias = [x['medias'][0] for x in response['feed']]
    users = {user['id']: user for user in response['users']}

    return medias, users

  def get_media_feed(self, limit=30, order=0):
    '''Retrieves media object feed
    '''
    endpoint = f'{self.base}/v2/home/mediastream'
    payload = {
      'limit': limit,
      'order': order}

    return self.invoke_get(endpoint, payload)

  def get_notifications(self, limit=30):
    '''Retreives notification feed for recent mentions, replies etc'''
    endpoint = f'{self.base}/v2/notifications/feed'
    payload = {'maxResults': limit}

    return self.invoke_get(endpoint, payload)

  def mark_as_seen(self, notify_id=None, mark_all=None):
    '''Marks a single notification or all of them as seen'''
    endpoint = f'{self.base}/v2/notifications/markVisited'

    if notify_id is not None:
      payload = {'notificationId': notify_id}
    elif mark_all is not None:
      payload = {'all': True}
    else:
      raise ValueError('Either notify_id of mark_all needs to be set!')

    return self.invoke_post(endpoint, data=payload)

  # ################### Data posting helpers

  def make_post(self, text, everyone=False, friends_only=False, medias=None):
    endpoint = f'{self.base}/v2/home/post'

    payload = {
      'text': text,
      'everyone': everyone,
      'closeFriends': friends_only,
    }
    if medias is not None:
      if all(['image' in x['type'] for x in medias]):
        payload['imageIds'] = [x['id'] for x in medias]
      else:
        # FIXME: check how other media types are uploaded
        raise ProgrammingError('HOW DO I NONIMAGES')

    return self.invoke_post(endpoint, json=payload)

  def _post_to_thread(self, endpoint, text, media):
    if media is not None:
      if 'image' in media['type']:
        comment_type = 'photo'
      else:
        # TODO: check how other media types are uploaded
        raise ProgrammingError('HOW DO I NONIMAGES')
      payload = {
        'text': text,
        "fileId": media['id'],
        "commentType": comment_type}
    else:
      payload = {'text': text}

    return self.invoke_post(endpoint, json=payload)

  def post_comment(self, post_id, text, media=None):
    endpoint = f'{self.base}/v2/home/post/{post_id}/comments'

    return self._post_to_thread(endpoint, text, media)

  def post_reply(self, comment_id, text, media=None):
    endpoint = f'{self.base}/v2/comments/{comment_id}/reply'

    return self._post_to_thread(endpoint, text, media)

  def upload_photo(self, file_obj):
    endpoint = f'{self.base}/v2/photo/pt'

    file_dict = {'file': (
      file_obj.filename,
      file_obj.stream,
      file_obj.content_type,
    )}
    return self.invoke_post(endpoint, files=file_dict)

  def upload_comment_photo(self, file_obj):
    endpoint = f'{self.base}/v2/photo/cm'

    file_dict = {'file': (
      file_obj.filename,
      file_obj.stream,
      file_obj.content_type,
    )}
    return self.invoke_post(endpoint, files=file_dict)
