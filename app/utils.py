from config import hostname
from datetime import datetime, timezone


# Here go various data converters and utility functions that do not directly relate to the MeWe API
def mongouuid_to_date(uuid):
  '''first 4 octets in mongodb UUIDs actually correspond to UNIX timestamp in BE format'''
  timestamp = int(uuid[:8], 16)
  date = datetime.fromtimestamp(timestamp)

  return date.strftime(r'%d %b %Y %H:%M:%S')


def prepare_media_feed(media_feed):
  '''Formats media feed into more convenient format for use with main page'''
  feed = []

  for item in media_feed['feed']:
    feed.append({
      'url': prepare_photo_url(item['photo'], thumb=True, thumb_size='400x400'),
      'date': mongouuid_to_date(item['mediaId']),
      'post_url': f'{hostname}/viewpost/{item["postItemId"]}',
    })

  return feed


def prepare_notifications(notification_feed):
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

    elif kind == 'mention':  # is this how post mentions work?
      author = users[item["postData"]['author']['id']]['name']
      who = users[item['commentData']['author']['id']]['name']

      notice.update({
        'headline': f'{who} mentioned you in a post by {author}',
        'message': item['commentData']['snippet'],
        'post_url': f'{hostname}/viewpost/{item["postData"]["postItemId"]}',
        'comment_id': item['commentData']['id']})

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

    elif kind == 'poll_ended':
      who = item['actingUsers'][0]['name']  # The first user in this list seems to be the one who reacted

      notice.update({
        'headline': f'Poll by {who} has ended',
        'message': item['pollData']['question'],
        'post_url': f'{hostname}/viewpost/{item["pollData"]["sharedPostId"]}',
        'comment_id': False})

    else:
      notice.update({
        'headline': f'Unknown notification type: {kind}',
        'message': str(item),
        'post_url': False,
        'comment_id': False})

    feed.append(notice)

  return feed


def gather_post_activity(posts, users):
  '''This takes a home feed and a user list, and returns a dictionary with recent active users'''
  headline = {}

  for item in posts:
    if item['userId'] not in headline:
      headline[item['userId']] = {
        'name': users[item['userId']]['name'],
        'user_id': item['userId'],
        'date': datetime.fromtimestamp(item['createdAt'], tz=timezone.utc)
                        .strftime(r'%d %b %Y %H:%M:%S'),
        'last_post': item['postItemId'],
      }

  return headline


def prepare_photo_url(photo, thumb=False, thumb_size='150x150', img_size='2000x2000'):
  # Known image sizes: 150, 400, 800, 1200, 2000
  if thumb:
    photo_url = photo['_links']['img']['href'].format(imageSize=thumb_size, static=1)
  else:
    photo_url = photo['_links']['img']['href'].format(imageSize=img_size, static=0)

  mime = photo['mime']
  name = photo['id']

  url = f'{hostname}/proxy?url={photo_url}&mime={mime}&name={name}'
  return url


def prepare_comment_photo(photo, thumb_size='150x150', img_size='2000x2000'):
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
