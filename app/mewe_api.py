import markdown
from datetime import datetime
from http import cookiejar
from requests import Session, get, post
from requests.utils import quote
from threading import Lock
from urllib import parse as p

from config import cookie_storage, user_agent, hostname


class Mewe:
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
  last_streamed_response = None
  refresh_lock = None

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
    # Lets also add hard line breaks while we're at it.
    markdown_instance = markdown.Markdown(extensions=['nl2br'])
    markdown_instance.parser.blockprocessors.deregister('hashheader')  # breaks hashtags
    self.markdown = markdown_instance.convert
    # TODO: Needs block processor for user links.
    # Example: 'Для \ufeff@{{u_5c25c5da3c8bb1088cb5f62e}Naru Ootori}\ufeff приготовила.'

    self.refresh_lock = Lock()

  def is_token_expired(self):
    access_token = self.session.cookies._cookies['.mewe.com']['/'].get('access-token')
    return access_token and access_token.is_expired()

  def invoke_get(self, endpoint, payload=None):
    self.refresh_session()
    r = self.session.get(endpoint, params=payload)
    if not r.ok:
      if r.json().get('message', '') == 'Forbidden':
        try:
          # Silly retry code, we can do better, but not this time
          print('Session died, attempting restart')
          self.reload_session()

          r = self.session.get(endpoint, params=payload)
          if not r.ok:
            raise ValueError(f'Failed to invoke request after reload: {r.text}')

        except Exception as e:
          raise ValueError(f'Failed reload session: {e}')
      else:
        raise ValueError(f'Failed to invoke request: {r.text}')

    return r.json()

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

    if self.refresh_lock.locked():
      print("Waiting for session refresh to finish")

    self.refresh_lock.acquire()
    if not self.is_token_expired():
      self.session.cookies.save(ignore_discard=True, ignore_expires=True)
      self.refresh_lock.release()
      return

    # Force-close last streamed connection in case it is still hogging up session
    if self.last_streamed_response is not None:
      self.last_streamed_response.close()
      self.last_streamed_response = None

    r = self.session.get(f'{self.base}/v3/auth/identify')

    if not r.ok or not r.json().get('authenticated', False):
      self.refresh_lock.release()
      raise ConnectionError('Failed to identify user, are cookies fresh enough?')

    try:
      self.session.headers['x-csrf-token'] = \
        self.session.cookies._cookies['.mewe.com']['/']['csrf-token'].value

    except KeyError:
      self.refresh_lock.release()
      if self.session.headers.get('x-csrf-token') is None:
        raise EnvironmentError(
          'Failed to extract CSRF token from auth/identify operation and no usable '
          'token exists in current session.')

    self.session.cookies.save(ignore_discard=True, ignore_expires=True)
    print("Session refreshed")
    self.refresh_lock.release()

  def whoami(self):
    '''Invokes me/info method to update info on current user. Useful to check API usability.
    '''
    self.refresh_session()
    r = self.invoke_get(f'{self.base}/v2/me/info')
    return r

  @staticmethod
  def resolve_user(user_id, user_list):
    '''Formats username by combining full name with invite identifier
    '''
    return f"{user_list[user_id]['name']} ({user_list[user_id]['contactInviteId']})"

  def get_user_info(self, user_id):
    '''Invokes mycontacts/user/{user_id} method to fetch information about a user by their ID, contacts only.
    '''
    r = self.invoke_get(f'{self.base}/v2/mycontacts/user/{user_id}')
    return r

  def _get_feed(self, endpoint, limit=None, pages=1):
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
        payload = {'limit': [limit]}

      response = self.invoke_get(endpoint, payload)

      page_feed = response['feed']
      page_users_list = response['users']

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
      'order': 1,}

    response = self.invoke_get(endpoint, payload)

    medias = [x['medias'][0] for x in response['feed']]
    users = {user['id']: user for user in response['users']}

    return medias, users

  # ################### Formatting helpers

  def _prepare_photo_media(self, photo, thumb=False):
    if thumb:
      photo_url = photo['_links']['img']['href'].format(imageSize='400x400', static=1)
    else:
      photo_url = photo['_links']['img']['href'].format(imageSize='2000x2000', static=0)
    quoted_url = quote(photo_url, safe='')
    mime = photo['mime']
    name = photo['id']

    url = f'{hostname}/proxy?url={quoted_url}&mime={mime}&name={name}'
    return url

  def _prepare_video_media(self, video):
    video_url = video['_links']['linkTemplate']['href'].format(resolution='original')
    quoted_url = quote(video_url, safe='')
    name = video['name']

    url = f'{hostname}/proxy?url={quoted_url}&mime=video/mp4&name={name}'
    return url, name

  def _prepare_document(self, doc):
    file_url = doc['_links']['url']['href']
    quoted_url = quote(file_url, safe='')
    name = doc['fileName']
    mime = doc['mime']

    url = f'{hostname}/proxy?url={quoted_url}&mime={mime}&name={name}'
    return url, name

  def prepare_post_message(self, post, user_list):
    '''Formats MeWe post object for use with template output.
    '''
    message = {}
    message['text'] = self.markdown(post.get('text', ''))

    message['link'] = {}
    message['poll'] = {}
    message['repost'] = None
    message['images'] = []
    message['videos'] = []
    message['files'] = []

    # Link
    if link := post.get('link'):
      message['link']['title'] = link.get('title', '')
      message['link']['url'] = link['_links']['url']['href']
      message['link']['text'] = link.get('description', '')
      # For some reason link thumbnails are stored on sepparated server with full URI, no auth required
      message['link']['thumb'] = link['_links'].get('thumbnail', {'href': ''})['href']

    # Poll
    if poll := post.get('poll'):
      message['poll']['text'] = poll['question']
      total_votes = sum([x['votes'] for x in poll['options']])
      message['poll']['total_votes'] = total_votes
      message['poll']['options'] = []

      for vote in poll['options']:
        vote_dict = {}
        vote_dict['percent'] = round(vote['votes'] / total_votes * 100)
        vote_dict['votes'] = vote['votes']
        vote_dict['text'] = vote['text']

        message['poll']['options'].append(vote_dict)

    # Medias (e.g. video or photo)
    if medias := post.get('medias'):
      for media in medias:

        # Video with associated photo object
        if video := media.get('video'):
          video_dict = {}
          video_dict['thumb'] = self._prepare_photo_media(media['photo'])
          video_dict['url'], video_dict['name'] = self._prepare_video_media(video)
          video_dict['width'] = min(media['photo']['size']['width'], 640)

          message['videos'].append(video_dict)

        # Image with no *known* associated media object
        elif photo := media.get('photo'):
          image_dict = {}
          image_dict['url'] = self._prepare_photo_media(photo)
          image_dict['thumb'] = self._prepare_photo_media(photo, thumb=True)
          # TODO: Add image captions, need more data
          image_dict['text'] = ''

          message['images'].append(image_dict)

    # Attachments
    if files := post.get('files'):
      for document in files:
        doc_dict = {}
        doc_dict['url'], doc_dict['name'] = self._prepare_document(document)

        message['files'].append(doc_dict)

    # Referenced message
    if ref_post := post.get('refPost'):
      message['repost'] = self.prepare_post_message(ref_post, user_list)
      message['repost']['author'] = self.resolve_user(ref_post['userId'], user_list)
      repost_date = datetime.fromtimestamp(ref_post['createdAt'])
      message['repost']['date'] = repost_date.strftime(r'%d %b %Y %H:%M:%S')

    return message

  def prepare_feed(self, user_id, limit=30, pages=1, retrieve_medias=False):
    '''Helper function to iterate over feed object and prepare rss-esque data set
    '''
    posts = []
    feed, users = self.get_user_feed(user_id, limit=limit, pages=pages)

    for post in feed:
      # Retrieve extra media elements from post if there are more than 4
      if post.get('mediasCount', 0) > 4 and retrieve_medias:
        extra_medias, extra_users = self.get_post_medias(post)
        post['medias'] = extra_medias  # FIXME: Only fetch remaining objects to save data?
        users.update(extra_users)

      msg = {}
      msg['content'] = self.prepare_post_message(post, users)
      msg['author'] = self.resolve_user(post['userId'], users)
      msg['guid'] = f'{post["postItemId"]}'
      msg['categories'] = [x for x in post.get('hashTags', [])]
      if album := post.get('album'):
        msg['categories'].insert(0, album)

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

    return posts, users

  def _prepare_comment_photo(self, photo):
    prepared = {}

    url_template = photo['_links']['img']['href']
    mime = photo['mime']
    if mime == 'image/jpeg':
      name = f'{photo["id"]}.jpg'
    elif mime == 'image/png':
      name = f'{photo["id"]}.png'
    elif mime == 'image/gif':
      name = f'{photo["id"]}.gif'
    elif mime == 'image/webp':
      name = f'{photo["id"]}.webp'
    else:
      name = photo['id']

    prepared_url = url_template.format(imageSize=f'{photo["size"]["width"]}x{photo["size"]["height"]}')
    prepared_thumb = url_template.format(imageSize='400x400')

    prepared['url'] = f'{hostname}/proxy?url={prepared_url}&mime={mime}&name={name}'
    prepared['thumb'] = f'{hostname}/proxy?url={prepared_thumb}&mime={mime}&name={name}'
    prepared['thumb_vertical'] = True if photo['size']['width'] < photo['size']['height'] else False
    prepared['name'] = name

    return prepared


  def prepare_post_comments(self, comments_feed, users):
    '''Prepares nested list of comment message objects in a manner similar to prepare_post_message
    '''
    comments = []
    for raw_comment in comments_feed:
      comment = {}
      comment['text'] = self.markdown(raw_comment.get('text', ''))
      if owner := raw_comment.get('owner'):
        comment['user'] = owner['name']
      else:
        comment['user'] = users[raw_comment['userId']]['name']
      comment['id'] = raw_comment['id']
      comment_date = datetime.fromtimestamp(raw_comment['createdAt'])
      comment['date'] = comment_date.strftime(r'%d %b %Y %H:%M:%S')
      comment['timestamp'] = raw_comment['createdAt']
      if photo_obj := raw_comment.get('photo'):
        comment['photo'] = self._prepare_comment_photo(photo_obj)

      if raw_comment.get('repliesCount'):
        comment['replies'] = self.prepare_post_comments(raw_comment['replies'], users)

      comments.append(comment)

    # Comments seem to arrive in date-descending order, however sometimes that rule is broken, so
    # we can't just reverse the list. Let's sort them once again by timestamp field
    return sorted(comments, key=lambda k: k['timestamp'])


  def prepare_single_post(self, post_id, load_all_comments=False):
    '''Prepares post and it's comments into simple dictionary following
    the same rules as used for feed preparation.
    '''
    response = self.get_post(post_id)
    post = response['post']
    users = {user['id']: user for user in response['users']}

    # Load up to 500 comments from the post
    if post.get('comments') and load_all_comments \
     and len(post['comments']['feed']) < post['comments']['total']:
      response = self.get_post_comments(post_id, limit=500)

      # Let's iterate over that response body some more and fill in comment replies if there are any
      for comment in response['feed']:
        if comment.get('repliesCount'):
          comment_response = self.get_comment_replies(comment['id'], limit=500)
          comment['replies'] = comment_response['comments']

      post['comments']['feed'] = response['feed']

    prepared_post = self.prepare_post_message(post, users)
    # Message schema is a bit different for comments, so we can't just reuse prepare_post_message
    if post.get('comments'):
      prepared_post['comments'] = self.prepare_post_comments(post['comments']['feed'], users)
    prepared_post['author'] = users[post['userId']]['name']
    prepared_post['id'] = post_id
    post_date = datetime.fromtimestamp(post['createdAt'])
    prepared_post['date'] = post_date.strftime(r'%d %b %Y %H:%M:%S')
    return prepared_post
