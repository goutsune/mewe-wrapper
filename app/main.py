import asyncio
from datetime import datetime
from http import cookiejar
from hypercorn.asyncio import serve
from hypercorn import Config
from quart import Quart, Response, render_template, request, abort
from requests import Session, get, post

from .config import cookie_storage, listen_hosts, user_agent


class MadMachine:
  '''Workhorse for storing web session and accessing MeWe API
  '''
  session = None
  identity = None
  base = 'https://mewe.com/api'

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
      raise ValueError('Failed to identify user, are cookies fresh enough?')

    try:
      session.headers['x-csrf-token'] = session.cookies._cookies['.mewe.com']['/']['csrf-token'].value
    except KeyError:
      raise KeyError('Failed to extract CSRF token from /identify operation')

    self.identity = session.get(f'{self.base}/v2/me/info').json()
    self.session = session

  def session_ok(self):
    r = self.session.get(f'{self.base}/v3/auth/identify')
    if r.ok and r.json().get('authenticated', False):
      return True
    else:
      return False

  def whoami(self):
    self.update_tokens()
    r = self.session.get(f'{self.base}/v2/me/info')
    return r.json()

  def update_tokens(self):
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
  c.update_tokens()


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
