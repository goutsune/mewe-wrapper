import asyncio
import markdown
from datetime import datetime
from http import cookiejar
from hypercorn.asyncio import serve
from hypercorn import Config
from quart import Quart, Response, render_template, request, abort
from requests import Session, get, post
from requests.utils import quote
from time import sleep
from urllib import parse as p

from config import cookie_storage, listen_hosts, user_agent, hostname


class MadMachine:
  '''Workhorse for storing web session and accessing MeWe API
  session:  An requests Session object
  identity: Dictionary to store information about currently logged-in user
  base:     Base MeWe API path
  markdown: Markdown class instance customized for MeWe
  '''
  session = None
  identity = None
  base = 'https://mewe.com/api'
  markdown = None

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

    # We need custom markdown parser with HeaderProcessor unregistered, so let's store it here.
    # Lets also add hard line breaks while we're ar it.
    markdown_instance = markdown.Markdown(extensions=['nl2br'])
    markdown_instance.parser.blockprocessors.deregister('hashheader')
    self.markdown = markdown_instance.convert
    # TODO: Needs block processor for user links.
    # Example: 'Для \ufeff@{{u_5c25c5da3c8bb1088cb5f62e}Naru Ootori}\ufeff приготовила.'

  def is_token_expired(self):
    access_token = self.session.cookies._cookies['.mewe.com']['/']['access-token']
    return access_token.is_expired()

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
          'Failed to extract CSRF token from auth/identify operation and no usable '
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

  @staticmethod
  def resolve_user(user_id, user_list):
    '''Formats username by combining full name with invite identifier
    '''
    return f"{user_list[user_id]['name']} ({user_list[user_id]['contactInviteId']})"

  def get_user_info(self, user_id):
    '''Invokes mycontacts/user/{user_id} method to fetch information about a user by their ID, contacts only.
    '''
    self.refresh_session()
    r = self.session.get(f'{self.base}/v2/mycontacts/user/{user_id}')
    if not r.ok:
      raise ValueError(r.text)
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
        raise ValueError(r.text)

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

    return feed, users

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

  # ################### Formatting helpers

  def _prepare_photo_media(self, photo):
    photo_size = f'{photo["size"]["width"]}x{photo["size"]["height"]}'
    photo_url = photo['_links']['img']['href'].format(imageSize=photo_size, static=0)
    quoted_url = quote(photo_url, safe='')
    mime = photo['mime']
    name = photo['id']

    return quoted_url, mime, name

  def _prepare_video_media(self, video):
    video_url = video['_links']['linkTemplate']['href'].format(resolution='original')
    quoted_url = quote(video_url, safe='')
    name = video['name']

    return quoted_url, name

  def format_post_text(self, post, user_list):
    '''Formats MeWe post object as HTML
    '''
    base_text = c.markdown(post.get('text', ''))
    media_text = ''
    link_text = ''
    poll_text = ''

    # Render post links
    if link := post.get('link'):
      link_title = link.get('title', '')
      link_title_tag = f'<b>{link_title}</b><br/>' if link_title else ''
      link_url = link['_links']['url']['href']
      link_tag = f'<a href="{link_url}">{link_url}</a><br/>'
      link_description = link.get('description', '')
      link_description_tag = f'<p style="white-space: pre-line">\
        {link_description}</p><br/>' if link_description else ''
      link_thumbnail = link['_links'].get('thumbnail', {'href': ''})['href']
      link_thumbnail_tag = f'<img src="{link_thumbnail}"></img><br/>' if link_thumbnail else ''

      link_text = link_title_tag if link_title_tag else ''
      link_text += link_tag

      link_text += link_thumbnail_tag if link_thumbnail_tag else ''
      link_text += link_description_tag if link_description_tag else ''

      link_text = f'<blockquote>{link_text}</blockquote>'

    # Render post poll
    if poll := post.get('poll'):
      poll_text = f'<p>Poll: {poll["question"]}</p><p>Results:</p><ul>'
      total_votes = sum([x['votes'] for x in poll['options']])

      for vote in poll['options']:
        vote_percent = round(vote['votes'] / total_votes * 100)
        poll_text += f'<li>{vote["text"]} — {vote["votes"]} ({vote_percent}%)</li>'

      poll_text += '</ul><br/>'

    # Render post media (e.g. video or music)
    if medias := post.get('medias'):
      for media in medias:

        # Video with associated photo object
        if video := media.get('video'):
          p_url, p_mime, p_name = self._prepare_photo_media(media['photo'])
          v_url, v_name = self._prepare_video_media(video)
          width = min(media['photo']['size']['width'], 640)
          media_text += \
            f'<video width="{width}" height="auto" controls=1'\
            f'poster="{hostname}/proxy?url={p_url}&mime={p_mime}&name={p_name}"\>'\
            f'<source src="{hostname}/proxy?url={v_url}&mime=video/mp4&name={v_name}" type="video/mp4" />'\
            '</video>'

        # Image with no associated video object
        # TODO: MeWe won't return links to all photo objects in post, so they need to be requested through
        # mediafeed method
        elif photo := media.get('photo'):
          url, mime, name = self._prepare_photo_media(photo)
          media_text += f'<img src="{hostname}/proxy?url={url}&mime={mime}&name={name}"></img><br/>'

    prepared_post = base_text + '<br/>' if base_text else ''
    prepared_post += poll_text if poll_text else ''
    prepared_post += media_text if media_text else ''
    prepared_post += link_text if link_text else ''
    return prepared_post

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


#@app.before_request
#async def conn_check():
#  '''Fetch new token and perhaps update refresh token here
#  '''
#  if not c.session_ok():
#    print("Not connected, reconnecting...")
#    await startup()


# ###################### Utils
def process_feed(feed, users):
  '''Helper function to iterate over feed object and prepare rss-esque data set
  '''
  posts = []
  for post in feed:
    msg = {}
    msg['text'] = c.format_post_text(post, users)
    msg['author'] = c.resolve_user(post['userId'], users)
    msg['guid'] = f'post["postItemId"]/post["updatedAt"]'
    msg['categories'] = [x for x in post.get('hashTags', [])]

    post_date = datetime.fromtimestamp(post['createdAt'])
    msg['date'] = post_date.strftime(r'%Y-%m-%dT%H:%M:%S%z')
    if post['text'] and len(post['text']) > 60:
      msg['title'] = post['text'][0:60] + '…'
    else:
      msg['title'] = post['text']
    if not msg['title']:
      msg['title'] = post_date.strftime(r'%d %b %Y %H:%M:%S')

    msg['link'] = f'{hostname}/viewpost/{post["postItemId"]}'

    posts.append(msg)

  return(posts)


# ###################### App routes
@app.route('/myworld')
async def retr_history():
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  feed, users = c.get_feed(limit=limit, pages=pages)
  if feed[0].get('error', False):
    return users['error'], 500

  posts = process_feed(feed, users)

  title = f'{c.identity["firstName"]} {c.identity["lastName"]}\'s world feed'
  info = title
  link = 'https://mewe.com/myworld'
  profile_pic = c.identity['_links']['avatar']['href'].format(imageSize='1280x1280')
  pp_quoted = quote(profile_pic, safe='')
  avatar = f'{hostname}/proxy?url={pp_quoted}&mime=image/jpeg&name={c.identity["id"]}'
  build = datetime.now().strftime(r'%Y-%m-%dT%H:%M:%S%z')

  return await render_template(
    'history.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)


@app.route('/userfeed/<string:user_id>')
async def retr_userfeed(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  feed, users = c.get_user_feed(user_id, limit=limit, pages=pages)
  posts = process_feed(feed, users)
  user = users[user_id]

  # TODO: Fetch user info here to prepare some nice channel description
  info = ''

  title = f'{user["name"]}'

  link = f'https://mewe.com/i/{user["contactInviteId"]}'
  profile_pic = user['_links']['avatar']['href'].format(imageSize='1280x1280')
  pp_quoted = quote(profile_pic, safe='')
  avatar = f'{hostname}/proxy?url={pp_quoted}&mime=image/jpeg&name={user["id"]}'
  build = datetime.now().strftime(r'%Y-%m-%dT%H:%M:%S%z')

  return await render_template(
    'history.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)

@app.route('/userfeed_rss/<string:user_id>')
async def retr_userfeed_rss(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  import pudb;pu.db
  print('retr_userfeed_rss got called')
  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  feed, users = c.get_user_feed(user_id, limit=limit, pages=pages)
  posts = process_feed(feed, users)
  user = users[user_id]

  # TODO: Fetch user info here to prepare some nice channel description
  info = ''

  title = f'{user["name"]}'

  link = f'https://mewe.com/i/{user["contactInviteId"]}'
  profile_pic = user['_links']['avatar']['href'].format(imageSize='1280x1280')
  pp_quoted = quote(profile_pic, safe='')
  avatar = f'{hostname}/proxy?url={pp_quoted}&mime=image/jpeg&name={user["id"]}'
  build = datetime.now().strftime(r'%Y-%m-%dT%H:%M:%S%z')

  return await render_template(
    'rss.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)

@app.route('/proxy')
def proxy_media():
  if request.method == 'HEAD':
    return "OK"

  url = request.args.get('url')
  mime = request.args.get('mime', 'application/octet-stream')
  name = request.args.get('name')

  res = c.session.get(f'https://mewe.com{url}', stream=True)

  return res.iter_content(chunk_size=1024), {
     'Content-Type': mime,
     'Content-Disposition': f'inline; filename={name}'}

# ###################### Webserver init
async def main():
  config = Config()
  config.bind = listen_hosts
  await serve(app, config)

if __name__ == '__main__':
  loop = asyncio.get_event_loop()
  loop.run_until_complete(main())
