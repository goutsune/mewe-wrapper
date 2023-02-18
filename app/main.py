import asyncio
from datetime import datetime
from http import cookiejar
from hypercorn.asyncio import serve
from hypercorn import Config
from quart import Quart, Response, render_template, request, abort
from requests import Session, get, post
from urllib import parse as p

from .config import cookie_storage, listen_hosts, user_agent


class MadMachine:
  '''Workhorse for storing web session and accessing MeWe API
  session:  An requests Session object
  identity: Dictionary to store information about currently logged-in user
  base:     Base MeWe API path
  is_token_expired: Callable that checks if access-token cookie has expired
  '''
  session = None
  identity = None
  base = 'https://mewe.com/api'
  profile_size = '1280x1280'
  image_size = '4096x4096'

  def __init__(self):
    '''Things to do:
    1. Fetch cookies from cookie jar
    2. Init requests session with them, set headers
    3. Execute /identify method to update tokens
    3. Extract CSRF cookie and add it to headers as x-csrf-token
    4. Try executing /me method to ensure session is usable
    '''

    cookie_jar = cookiejar.MozillaCookieJar(cookie_storage)
    cookie_jar.load(ignore_discard=True, ignore_expires=True)
    session = Session()

    session.cookies = cookie_jar
    session.headers['user-agent'] = user_agent

    r = session.get(f'{self.base}/v3/auth/identify')
    if not r.ok or not r.json().get('authenticated', False):
      raise ValueError(f'Failed to identify user, are cookies fresh enough? Result: {r.text}')

    try:
      session.headers['x-csrf-token'] = session.cookies._cookies['.mewe.com']['/']['csrf-token'].value
    except KeyError:
      raise KeyError('Failed to extract CSRF token from /identify operation')

    self.identity = session.get(f'{self.base}/v2/me/info').json()
    self.session = session

  def is_token_expired(self):
    cookie = self.session.cookies._cookies['.mewe.com']['/']['access-token']
    return cookie.is_expired()

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

  def refresh_session(self):
    '''Checks current access token and receive new one accordingly
    '''
    if not self.is_token_expired():
      self.session.cookies.save(ignore_discard=True, ignore_expires=True)
      return

    r = self.session.get(f'{self.base}/v3/auth/identify')

    if not r.ok or not r.json().get('authenticated', False):
      raise ConnectionError('Failed to identify user, are cookies fresh enough?')

    try:
      self.session.headers['x-csrf-token'] = \
        self.session.cookies._cookies['.mewe.com']['/']['csrf-token'].value

    except KeyError:
      if self.session.headers.get('x-csrf-token') is None:
        raise EnvironmentError(
          'Failed to extract CSRF token from /identify operation and no usable '
          'token exists in current session.')

    self.session.cookies.save(ignore_discard=True, ignore_expires=True)

  def whoami(self):
    '''Invokes me/info method to update info on current user. Useful to check API usability.
    '''
    self.refresh_session()
    r = self.session.get(f'{self.base}/v2/me/info')
    if not r.ok:
      return {'error': True}
    return r.json()

  def get_user_info(self, user_id):
    '''Invokes mycontacts/user/{user_id} method to fetch information about a user by their ID, contacts only.
    '''
    self.refresh_session()
    r = self.session.get(f'{self.base}/v2/mycontacts/user/{user_id}')  # TODO: Sanity checks, any?
    if not r.ok:
      return {'error': True}
    return r.json()

  def _get_feed(self, endpoint, limit, pages):
    '''Method to loop through pages that return feed objects along with respective users.
    For the time being at least 4 endpoints return that type:
      Main feed, Group feed, User feed, Post comments (lol)
    '''
    self.refresh_session()

    feed = []
    users = {}  # We'll store users in a dictionary for convenience

    for page in range(pages):
      # We'll loop through requested number of pages filling global feed/users objects here.
      if not page:  # range start from 0 so, eeh
        payload = {'limit': [limit]}

      r = self.session.get(endpoint, params=payload)
      if not r.ok:
        return {'error': True}

      page_feed = r.json()['feed']
      page_users_list = r.json()['users']

      next_link = r.json()['_links'].get('nextPage', {'href': None})['href']

      # Our usable list will be accessible by user_id
      users.update({user['id']: user for user in page_users_list})
      feed.extend(page_feed)

      if next_link is None:
        break

      payload = p.parse_qs(p.urlsplit(next_link).query)
      payload['limit'] = [limit]

    return {'feed': feed, 'users': users}

  def get_feed(self, limit=30, pages=1):
    '''Invokes home/allfeed method to fetch home feed.
    '''
    endpoint = f'{self.base}/v2/home/allfeed'
    return self._get_feed(endpoint, limit, pages)

  def get_user_feed(self, user_id, limit=30, pages=1):
    '''Invokes home/user/{user_id}/postsfeed method to fetch single user posts
    '''
    endpoint = f'{self.base}/v2/home/user/{user_id}/postsfeed'
    return self._get_feed(endpoint, limit, pages)

  def get_post(self, post_id):
    '''Invokes home/post/{post_id} method to fetch single post
    '''
    endpoint = f'{self.base}/v2/home/post/{post_id}'
    return self._get_feed(endpoint, limit, pages)

  def get_post_comments(self, post_id, limit=100, pages=1):
    '''Invokes home/post/{post_id}/comments method to fetch single user posts
    '''
    endpoint = f'{self.base}/v2/home/post/{post_id}/comments'
    return self._get_feed(endpoint, limit, pages)


# ###################### Init
app = Quart(__name__)


# ###################### Quart setup
@app.before_serving
async def startup():
  '''Check user session via cookies here, perhaps fetch new token
  '''
  print("Connecting...")
  global c
  c = MadMachine()


@app.after_serving
async def cleanup():
  '''prepare tokens
  '''
  print("Disconnecting...")
  c.refresh_session()


@app.before_request
async def conn_check():
  '''Fetch new token and perhaps update refresh token here
  '''
  if not c.session_ok():
    print("Not connected, reconnecting...")
    await startup()

# ###################### App routes

# ###################### Webserver init
async def main():
  config = Config()
  config.bind = listen_hosts
  await serve(app, config)

if __name__ == '__main__':
  loop = asyncio.get_event_loop()
  loop.run_until_complete(main())
