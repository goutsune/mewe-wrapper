import markdown
import json
from datetime import datetime, timedelta
from http import cookiejar
from os import path
from requests import Session, get, post
from requests_cache import CachedSession, DO_NOT_CACHE, NEVER_EXPIRE
from requests.utils import quote
from threading import Lock
from urllib import parse as p

from config import cookie_storage, user_agent, hostname, proxy
from markdown_tools import MeweEmojiExtension, MeweMentionExtension
from utils import prepare_photo_url, prepare_comment_photo, TimeoutHTTPAdapter
from mewe_cfg import MeweConfig


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
  emojis = None
  mime_mapping = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/bmp': 'bmp',
    'video/mp4': 'mp4',
    'video/webm': 'webm',
  }

  rev_mime = dict(map(reversed, mime_mapping.items()))

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
    '*': 5,  # Prevent accidential re-requests eg when loading same preview image from post and board view
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

    cookie_jar = cookiejar.MozillaCookieJar(cookie_storage)
    cookie_jar.load(ignore_discard=True, ignore_expires=True)

    session = CachedSession(
      'session_cache',
      backend='sqlite',
      cache_control=False,
      ignored_parameters=self._ignores,
      match_headers=True,
      stale_if_error=True,
      urls_expire_after=self._cache_defs,
    )

    session.cookies = cookie_jar
    session.headers['user-agent'] = user_agent
    # Is this an adequate way to provide session timeout setup?
    session.mount('http://', TimeoutHTTPAdapter(timeout=5))
    session.mount('https://', TimeoutHTTPAdapter(timeout=5))
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
    self.emojis = generate_emoji_dict()

    # We need custom markdown parser with HeaderProcessor unregistered, so let's store it here.
    # Lets also add hard line breaks while we're at it.
    markdown_instance = markdown.Markdown(
      extensions=[
        'nl2br',
        'sane_lists',
        'mdx_linkify',
        MeweEmojiExtension(emoji_dict=self.emojis),
        MeweMentionExtension()]
    )
    markdown_instance.parser.blockprocessors.deregister('hashheader')  # breaks hashtags
    self.markdown = markdown_instance.convert

    self.refresh_lock = Lock()
    self.config = MeweConfig()

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

  def invoke_post(self, endpoint, **kwargs):
    self.refresh_session()
    r = self.session.post(endpoint, **kwargs)
    if not r.ok:
      if r.json().get('message', '') == 'Forbidden':
        try:
          # Silly retry code, we can do better, but not this time
          print('Session died, attempting restart')
          self.reload_session()

          r = self.session.post(endpoint, **kwargs)
          if not r.ok:
            raise ValueError(f'Failed to invoke request after reload: {r.text}')

        except Exception as e:
          raise ValueError(f'Failed reload session: {e}')
      else:
        raise ValueError(f'Failed to invoke request: {r.text}')
    try:
      return r.json()
    except Exception as e:
      return r.text

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
    try:
      return f"{user_list[user_id]['name']} ({user_list[user_id]['contactInviteId']})"
    except KeyError:
      return user_id

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

  # ################### Formatting helpers



  def _prepare_video(self, video):
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

  def _prepare_link(self, link):
    prepared_link = {
      'title': link.get('title', 'No Title'),
      'url': link['_links']['url']['href'],
      'name': link['_links']['urlHost']['href'],
      'text': link.get('description', ''),
      # For some reason link thumbnails are stored on sepparated server with full URI, no auth required
      'thumb': link['_links'].get('thumbnail', {'href': ''})['href'],
    }

    return prepared_link

  def _prepare_poll(self, poll):
    total_votes = sum([x['votes'] for x in poll['options']])

    prepared_poll = {
      'text': poll['question'],
      'total_votes': total_votes,
      'options': [],
    }

    for vote in poll['options']:
      vote_dict = {
        'percent': round(vote['votes'] / total_votes * 100),
        'votes': vote['votes'],
        'text': vote['text'],
      }

      prepared_poll['options'].append(vote_dict)

    return prepared_poll

  def prepare_emojis(self, emoji_dict):
    emojis = []

    for code, count in emoji_dict['counts'].items():
      emojis.append({
        'code': code,
        'url': self.emojis[code],
        'count': count
      })
    return emojis

  def prepare_post_contents(self, post, user_list):
    '''Reserializes MeWe post object for more convenient use with template output.
    '''
    message = {
      'text': self.markdown(post.get('text', '')),
      'album': post.get('album', ''),
      'link': {},
      'poll': {},
      'repost': None,
      'images': [],
      'videos': [],
      'files': [],
    }

    # Link
    if link := post.get('link'):
      message['link'] = self._prepare_link(link)

    # Poll
    if poll := post.get('poll'):
      message['poll'] = self._prepare_poll(poll)

    # Medias (e.g. video or photo)
    if medias := post.get('medias'):
      for media in medias:

        # Video with associated photo object
        if video := media.get('video'):
          prepared_url, prepared_name = self._prepare_video(video)
          media_photo_size = media['photo']['size']

          video_dict = {
            'thumb': prepare_photo_url(media['photo'], thumb=True, thumb_size=self.config.thumb_load_size),
            'url': prepared_url,
            'name': prepared_name,
            'width': min(media['photo']['size']['width'], 640),
            'size': f'{media["photo"]["size"]["width"]}x{media["photo"]["size"]["height"]}',
            'duration': video.get('duration', '???'),
            'thumb_vertical': True if media_photo_size['width'] < media_photo_size['height'] else False,
          }

          message['videos'].append(video_dict)

        # Image with no *known* associated media object
        elif photo := media.get('photo'):
          image_dict = {
            'url': prepare_photo_url(photo),
            'thumb': prepare_photo_url(photo, thumb=True, thumb_size=self.config.thumb_load_size),
            'thumb_vertical': True if photo["size"]["width"] < photo["size"]["height"] else False,
            'id': photo['id'],
            'mime': photo['mime'],
            'size': f'{photo["size"]["width"]}x{photo["size"]["height"]}',
            # TODO: Add image captions, need more data
            'text': '',
          }

          if ext := self.mime_mapping.get(photo['mime']):
            image_dict['name'] = f'{photo["id"]}.{ext}'
          else:
            image_dict['name'] = photo['id']

          message['images'].append(image_dict)

    # Attachments
    if files := post.get('files'):
      for document in files:
        doc_url, doc_name = self._prepare_document(document)
        doc_dict = {
          'url': doc_url,
          'name': doc_name,
          'mime': document['mime'],
          'size': document['length'],
        }

        message['files'].append(doc_dict)

    # Referenced message
    if ref_post := post.get('refPost'):
      message['repost'] = self.prepare_post_contents(ref_post, user_list)
      repost_date = datetime.fromtimestamp(ref_post['createdAt'])

      message['repost'].update({
        'author': self.resolve_user(ref_post['userId'], user_list),
        'author_id': ref_post['userId'],
        'id': ref_post['postItemId'],
        'date': repost_date.strftime(r'%d %b %Y %H:%M:%S')
      })

    # Deleted reference is marked with this flag outside refPost object
    if post.get('refRemoved'):
      message['repost'] = {'deleted': True}

    return message

  def prepare_feed(self, feed, users, retrieve_medias=False, with_message_only=False):
    '''Helper function to iterate over feed object and prepare rss-esque data set
    '''
    posts = []

    for post in feed:
      # TODO: Filter out posts that contain no text, or contain only emojis
      msg = self.prepare_single_post(post, users, retrieve_medias=retrieve_medias)
      posts.append(msg)

    return posts, users

  def prepare_post_comments(self, comments_feed, users):
    '''Prepares nested list of comment message objects in a manner similar to prepare_post_contents
    '''
    comments = []
    for raw_comment in comments_feed:
      comment_date = datetime.fromtimestamp(raw_comment['createdAt'])
      comment = {
        'text': self.markdown(raw_comment.get('text', '')),
        'user_id': raw_comment['userId'],
        'id': raw_comment['id'],
        'date': comment_date.strftime(r'%d %b %Y %H:%M:%S'),
        'timestamp': raw_comment['createdAt'],
        'images': [],
        'reply_count': raw_comment.get('repliesCount', 0),
        'subscribed': raw_comment['follows'],
        'emojis': self.prepare_emojis(raw_comment['emojis']) if raw_comment.get('emojis') else None,
      }

      if owner := raw_comment.get('owner'):
        comment['user'] = owner['name']
      else:
        comment['user'] = users[raw_comment['userId']]['name']

      if photo_obj := raw_comment.get('photo'):
        comment['images'].append(prepare_comment_photo(photo_obj, thumb_size=self.config.thumb_load_size))

      if document_obj := raw_comment.get('document'):
        #FIXME: Either put this into separate document object inside comment, or revise comment schema
        url = document_obj['_links']['url']['href']
        thumb = document_obj['_links']['iconUrl']['href']
        mime = self.rev_mime[document_obj['type']]
        comment['images'].append({
          'url': f'{hostname}/proxy?url={url}&mime={mime}&name={document_obj["name"]}',
          'thumb': f'https://cdn.mewe.com/assets/icons/file-type/{document_obj["type"]}.png',
          'thumb_vertical': False,
          'id': document_obj['id'],
          'name': document_obj['name'],
          'size': f'{document_obj["size"]} bytes',
          'mime': mime,
        })

      if link_obj := raw_comment.get('link'):
        comment['link'] = self._prepare_link(link_obj)

      if raw_comment.get('repliesCount') and 'replies' in raw_comment:
        comment['replies'] = self.prepare_post_comments(raw_comment['replies'], users)

      comments.append(comment)

    # Comments seem to arrive in date-descending order, however sometimes that rule is broken, so
    # we can't just reverse the list. Let's sort them once again by timestamp field
    return sorted(comments, key=lambda k: k['timestamp'])

  def prepare_single_post(self, post, users, load_all_comments=False, retrieve_medias=False):
    '''Prepares post and it's comments into simple dictionary following
    the same rules as used for feed preparation.
    '''
    # Retrieve extra media elements from post if there are more than 4
    if post.get('mediasCount', 0) > 4 and retrieve_medias:
      extra_medias, extra_users = self.get_post_medias(post)
      post['medias'] = extra_medias  # FIXME: Only fetch remaining objects to save data?
      users.update(extra_users)

    # Load up to 500 comments from the post
    if post.get('comments') and load_all_comments:
      response = self.get_post_comments(post['postItemId'], limit=500)

      # Let's iterate over that response body some more and fill in comment replies if there are any
      for comment in response['feed']:
        if comment.get('repliesCount'):
          comment_response = self.get_comment_replies(comment['id'], limit=500)
          comment['replies'] = comment_response['comments']

      post['comments']['feed'] = response['feed']

    post_date = datetime.fromtimestamp(post['createdAt'])
    if post.get('comments'):
      missing_comment_count = post['comments']['total'] - len(post['comments']['feed'])
      reply_count = sum((x.get('repliesCount', 0) for x in post['comments']['feed']))
    else:
      missing_comment_count = 0
      reply_count = 0

    prepared_post = {
      'content': self.prepare_post_contents(post, users),
      # Message schema is a bit different for comments, so we can't just reuse prepare_post_contents
      'author': users[post['userId']]['name'],
      'author_id': post['userId'],
      'id': post['postItemId'],
      'date': post_date.strftime(r'%d %b %Y %H:%M:%S'),

      'comments': self.prepare_post_comments(post['comments']['feed'], users)
                  if post.get('comments') else [],
      'missing_count': missing_comment_count + reply_count,
      'subscribed': post['follows'],
      'emojis': self.prepare_emojis(post['emojis']) if post.get('emojis') else None,

      # Extra meta for RSS
      'categories': [x for x in post.get('hashTags', [])],
      'author_rss': self.resolve_user(post['userId'], users),
      'date_rss': post_date.strftime(r'%Y-%m-%dT%H:%M:%S%z'),
      'link': f'{hostname}/viewpost/{post["postItemId"]}',
    }

    if album := post.get('album'):
      prepared_post['categories'].insert(0, album)
    if post['text'] and len(post['text']) > 60:
      prepared_post['title'] = post['text'][0:60] + '…'
    else:
      prepared_post['title'] = post['text']
    if not prepared_post['title']:
      prepared_post['title'] = post_date.strftime(r'%d %b %Y %H:%M:%S')

    return prepared_post


def generate_emoji_dict():
  '''Helper function to convert mewe-specific emoji codes into links

  Roadmap:
    * Load all JSONs from CDN and compile that information into 'code': 'URL' substitution dict
    * Convert to class and provide dict-like object that dynamically handles emoji pack updates
    * Set up rudimentary caching in form of JSON dump with timestamp with periodical checks
  '''

  _base = 'https://cdn.mewe.com'
  r = get(f'{_base}/emoji/build-info.json')
  r.raise_for_status()

  # Build info contains information on used emoji packs and their locations
  print(f'Fetching build info')
  build_info = r.json()
  with open(f'cache/build_info.json', 'w') as file:
    file.write(r.text)

  # TODO: Use this to periodically check and rebuild emoji database
  mewe_version = build_info['version']

  packs = build_info['packs']

  # Now let's load content of each emoji pack
  pack_dict = {}
  for pack_name, url in packs.items():
    if path.exists(f'cache/emojis/{pack_name}.json'):
      print(f'Using cached {pack_name}.json')
      with open(f'cache/emojis/{pack_name}.json', 'r') as file:
        pack_dict[pack_name] = json.load(file)

    else:
      print(f'Fetching {pack_name}')
      r = get(f'{_base}{url}')
      r.raise_for_status()

      pack_dict[pack_name] = r.json()
      with open(f'cache/emojis/{pack_name}.json', 'w') as file:
        file.write(r.text)

  # Finally let's pack all that into simple flat dictionary for lookup table
  emoji_dict = {}

  for pack in pack_dict.values():
    emoji_list = pack['emoji']

    for emoji in emoji_list:
      # Mewe uses both unicode and shortcodes for message formatting
      if 'unicode' in emoji:
        emoji_dict[emoji['unicode']] = f'{_base}{emoji["png"]["default"]}'

      emoji_dict[emoji['shortname']] = f'{_base}{emoji["png"]["default"]}'

  return emoji_dict
