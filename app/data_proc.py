import markdown
import mimetypes
from datetime import datetime, timedelta
from requests.utils import quote

from utils import generate_emoji_dict, mongouuid_to_date
from markdown_tools import MeweEmojiExtension, MeweMentionExtension
from mewe_api import Mewe
from mewe_cfg import MeweConfig
from config import hostname


class DataProcessor:
  '''This class deals with data returned from the MeWe API, the end result is a set of
  methods to generate data that can be used as base for an RSS feed or a forum thread
  '''

  base = 'https://mewe.com/api'
  markdown = None
  emojis = None

  def __init__(self, mewe_instance):

    # We need custom markdown parser with HeaderProcessor unregistered, so let's store it here.
    # Lets also add hard line breaks while we're at it.
    self.emojis = generate_emoji_dict()
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
    self.mewe = mewe_instance
    self.config = MeweConfig()

  def prepare_video(self, video):
    video_url = video['_links']['linkTemplate']['href'].format(resolution='original')
    quoted_url = quote(video_url, safe='')
    name = video['name']

    url = f'{hostname}/proxy?url={quoted_url}&mime=video/mp4&name={name}'
    return url, name

  def prepare_document(self, doc):
    file_url = doc['_links']['url']['href']
    quoted_url = quote(file_url, safe='')
    name = doc['fileName']
    mime = doc['mime']

    url = f'{hostname}/proxy?url={quoted_url}&mime={mime}&name={name}'
    return url, name

  def prepare_link(self, link):
    prepared_link = {
      'title': link.get('title', 'No Title'),
      'url': link['_links']['url']['href'],
      'name': link['_links']['urlHost']['href'],
      'text': link.get('description', ''),
      # For some reason link thumbnails are stored on sepparated server with full URI, no auth required
      'thumb': link['_links'].get('thumbnail', {'href': ''})['href'],
    }

    return prepared_link

  def prepare_poll(self, poll):
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
        'url': self.emojis.get(code, '#'),
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
      message['link'] = self.prepare_link(link)

    # Poll
    if poll := post.get('poll'):
      message['poll'] = self.prepare_poll(poll)

    # Medias (e.g. video or photo)
    if medias := post.get('medias'):
      for media in medias:

        # Video with associated photo object
        if video := media.get('video'):
          prepared_url, prepared_name = self.prepare_video(video)
          media_photo_size = media['photo']['size']

          video_dict = {
            'thumb': self.prepare_photo_url(media['photo'], thumb=True, thumb_size=self.config.thumb_load_size),
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
            'url': self.prepare_photo_url(photo),
            'thumb': self.prepare_photo_url(photo, thumb=True, thumb_size=self.config.thumb_load_size),
            'thumb_vertical': True if photo["size"]["width"] < photo["size"]["height"] else False,
            'id': photo['id'],
            'mime': photo['mime'],
            'size': f'{photo["size"]["width"]}x{photo["size"]["height"]}',
            # TODO: Add image captions, need more data
            'text': '',
          }

          if ext := mimetypes.guess_extension(photo['mime']):
            image_dict['name'] = f'{photo["id"]}{ext}'
          else:
            image_dict['name'] = photo['id']

          message['images'].append(image_dict)

    # Attachments
    if files := post.get('files'):
      for document in files:
        doc_url, doc_name = self.prepare_document(document)
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

  def prepare_media_feed(self, media_feed):
    '''Formats media feed into more convenient format for use with main page'''
    feed = []

    for item in media_feed['feed']:
      feed.append({
        'url': self.prepare_photo_url(item['photo'], thumb=True, thumb_size=self.config.thumb_load_size),
        'date': mongouuid_to_date(item['mediaId']),
        'post_url': f'{hostname}/viewpost/{item["postItemId"]}',
      })

    return feed

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
        comment['images'].append(self.prepare_comment_photo(photo_obj, thumb_size=self.config.thumb_load_size))

      if document_obj := raw_comment.get('document'):
        # FIXME: Either put this into separate document object inside comment, or revise comment schema
        url = document_obj['_links']['url']['href']
        thumb = document_obj['_links']['iconUrl']['href']
        mime = mimetypes.types_map.get(f'.{document_obj["type"]}', 'application/octet-stream')
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
        comment['link'] = self.prepare_link(link_obj)

      if raw_comment.get('repliesCount') and 'replies' in raw_comment:
        comment['replies'] = self.prepare_post_comments(raw_comment['replies'], users)

      comments.append(comment)

    # Comments seem to arrive in date-descending order, however sometimes that rule is broken, so
    # we can't just reverse the list. Let's sort them once again by timestamp field
    return sorted(comments, key=lambda k: k['timestamp'])

  def prepare_notifications(self, notification_feed):
    '''Formats notification feed into more convenient format for use with main page'''
    feed = []

    for item in notification_feed['feed']:
      kind = item['notificationType']
      users = {x['id']: x for x in item['actingUsers']}

      notice = {
        'type': kind,
        'new': not item['visited'],
        'date': datetime.fromtimestamp(item['createdAt'])
                        .strftime(r'%d %b %Y %H:%M:%S'),
        'notify_id': item['id']}

      if kind == 'comment' and 'replyTo' in item['commentData']:  # Comment reply
        author = users[item['commentData']['parentAuthor']]['name']
        who = users[item['commentData']['author']['id']]['name']

        notice.update({
          'headline': f'{who} replied to comment by {author}',
          'message': item['commentData']['snippet'],
          'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
          'comment_id': item['commentData']['id']})

      elif kind == 'comment':  # Post comment
        author = item["postData"]['author']['name']
        who = users[item['commentData']['author']['id']]['name']

        notice.update({
          'headline': f'{who} commented on post by {author}',
          'message': item['commentData']['snippet'],
          'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
          'comment_id': item['commentData']['id']})

      elif kind == 'mention' and 'commentData' in item:  # mention in comment
        who = users[item['commentData']['author']['id']]['name']

        notice.update({
          'headline': f'{who} mentioned you in a comment',
          'message': item['commentData']['snippet'],
          'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
          'comment_id': item['commentData']['id']})

      elif kind == 'mention':
        if 'commentData' in item:  # Mention inside a comment
          author = users[item["postData"]['author']['id']]['name']
          who = users[item['commentData']['author']['id']]['name']

          notice.update({
            'headline': f'{who} mentioned you in a post by {author}',
            'message': item['commentData']['snippet'],
            'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
            'comment_id': item['commentData']['id']})

        else:
          who = users[item["postData"]['author']['id']]['name']

          notice.update({
            'headline': f'{who} mentioned you in a post',
            'message': item['postData']['snippet'],
            'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
            'comment_id': False})

      elif kind == 'emojis' and 'commentData' in item:  # reaction to comment
        if item['commentData']['author']['id'] not in users:
          author = 'you'  # Apparently this is intentional
        else:
          author = users[item['commentData']['author']['id']]['name']
        who = item['actingUsers'][0]['name']  # The first user in this list seems to be the one who reacted

        notice.update({
          'headline': f'{who} reacted to comment by {author}',
          'message': item['commentData']['snippet'],
          'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
          'comment_id': item['commentData']['id']})

      elif kind == 'emojis':  # reaction to post
        if item['postData']['author']['id'] not in users:
          author = 'you'  # Apparently this is intentional
        else:
          author = users[item['postData']['author']['id']]['name']
        who = item['actingUsers'][0]['name']  # The first user in this list seems to be the one who reacted

        notice.update({
          'headline': f'{who} reacted to post by {author}',
          'message': item['postData']['snippet'],
          'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
          'comment_id': False})

      elif kind == 'follow_request_accepted':
        who = item['actingUsers'][0]['name']  # The first user in this list seems to be the one who reacted

        notice.update({
          'headline': f'{who} accepted your follow request',
          'message': '',
          'post_url': False,
          'comment_id': False})

      elif kind == 'new_follow_request':
        who = item['actingUsers'][0]['name']

        notice.update({
          'headline': f'{who} wants to follow you!',
          'message': '',
          'post_url': False,
          'comment_id': False})

      elif kind == 'poll_ended':
        who = item['actingUsers'][0]['name']  # The first user in this list seems to be the one who reacted

        notice.update({
          'headline': f'Poll by {who} has ended',
          'message': item['pollData']['question'],
          'post_url': f'{hostname}/viewpost/{item["pollData"]["sharedPostId"]}',
          'comment_id': False})

      elif kind == 'contact_birthday':
        who = item['actingUsers'][0]['name']
        date = f'{item["birthDayData"]["month"]}.{item["birthDayData"]["day"]}'

        notice.update({
          'headline': f'{who} has a birthday on {date}!',
          'message': '',
          'post_url': f'{hostname}/userfeed/{item["actingUsers"][0]["id"]}',
          'comment_id': False})

      else:
        notice.update({
          'headline': f'Unknown notification type: {kind}',
          'message': str(item),
          'post_url': False,
          'comment_id': False})

      feed.append(notice)

    return feed

  def prepare_single_post(self, post, users, load_all_comments=False, retrieve_medias=False):
    '''Prepares post and it's comments into simple dictionary following
    the same rules as used for feed preparation.
    '''
    # Retrieve extra media elements from post if there are more than 4
    if post.get('mediasCount', 0) > 4 and retrieve_medias:
      extra_medias, extra_users = self.mewe.get_post_medias(post)
      post['medias'] = extra_medias  # FIXME: Only fetch remaining objects to save data?
      users.update(extra_users)

    # Load up to 500 comments from the post
    if post.get('comments') and load_all_comments:
      response = self.mewe.get_post_comments(post['postItemId'], limit=500)

      # Let's iterate over that response body some more and fill in comment replies if there are any
      for comment in response['feed']:
        if comment.get('repliesCount'):
          comment_response = self.mewe.get_comment_replies(comment['id'], limit=500)
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

    # Some silly heuristics to generate item title with regex
    if album := post.get('album'):
      prepared_post['categories'].insert(0, album)
    if post['text'] and len(post['text']) > 60:
      prepared_post['title'] = post['text'][0:60] + '…'
    else:
      prepared_post['title'] = post['text']
    if not prepared_post['title']:
      prepared_post['title'] = post_date.strftime(r'%d %b %Y %H:%M:%S')

    return prepared_post

  def resolve_user(self, user_id, user_list):
    '''Formats username by combining full name with invite identifier
    '''
    try:
      return f"{user_list[user_id]['name']} ({user_list[user_id]['contactInviteId']})"
    except KeyError:
      return user_id

  def gather_post_activity(self, posts, users):
    '''This takes a home feed and a user list, and returns a dictionary with recent active users'''
    headline = {}

    for item in posts:
      if item['userId'] not in headline:
        headline[item['userId']] = {
          'name': users[item['userId']]['name'],
          'user_id': item['userId'],
          'date': datetime.fromtimestamp(item['createdAt'])
                          .strftime(r'%d %b %Y %H:%M:%S'),
          'last_post': item['postItemId'],
        }

    return headline

  def prepare_photo_url(self, photo, thumb=False, thumb_size=None, img_size=None):

    if thumb_size is None: thumb_size = self.config.thumb_load_size
    if thumb_size is None: img_size = self.config.image_load_size

    # Known image sizes: 50, 150, 400, 800, 1200, 2000
    if thumb:
      photo_url = photo['_links']['img']['href'].format(imageSize=thumb_size, static=1)
    else:
      photo_url = photo['_links']['img']['href'].format(imageSize=img_size, static=0)

    mime = photo['mime']
    name = photo['id']

    url = f'{hostname}/proxy?url={photo_url}&mime={mime}&name={name}'
    return url

  def prepare_comment_photo(self, photo, thumb_size=None, img_size=None):

    if thumb_size is None: thumb_size = self.config.thumb_load_size
    if thumb_size is None: img_size = self.config.image_load_size

    url_template = photo['_links']['img']['href']
    mime = photo['mime']
    name = photo['name']

    size = f'{photo["size"]["width"]}x{photo["size"]["height"]}'
    if photo.get('animated'):
      prepared_url = url_template.format(imageSize=img_size, static=0)
      prepared_thumb = url_template.format(imageSize=thumb_size, static=1)
    else:
      prepared_url = url_template.format(imageSize=img_size)
      prepared_thumb = url_template.format(imageSize=thumb_size)

    prepared = {
      'url': f'{hostname}/proxy?url={prepared_url}&mime={mime}&name={name}',
      'thumb': f'{hostname}/proxy?url={prepared_thumb}&mime={mime}&name={name}',
      'thumb_vertical': True if photo['size']['width'] < photo['size']['height'] else False,
      'id': photo['id'],
      'name': name,
      'size': size,
      'mime': mime,
    }

    return prepared
