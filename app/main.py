from datetime import datetime
from flask import Flask, Response, render_template, request, abort, redirect
from requests.utils import quote
from time import sleep

from config import host, port, hostname
from mewe_api import Mewe

# ###################### Init
app = Flask(__name__)
c = Mewe()


# ###################### Quart setup
def startup():
  '''Check user session via cookies here, perhaps fetch new token
  '''
  print("Connecting...")


def cleanup():
  '''prepare tokens
  '''
  print("Disconnecting...")
  c.refresh_session()


# ###################### App routes
@app.route('/')
def make_index():
  # FIXME: Finish main page display someday, lol
  '''Generates index page with latest medias and posts.
  Also shows user list and group list'''
  medias = c.prepare_media_feed(limit=10)
  posts = c.prepare_feed(limit=10, with_message=True)

  return render_template('wakaba_index.html', medias=medias, posts=posts, last_active=last_active)


@app.route('/viewpost/<string:post_id>')
def show_post(post_id):
  '''Processes post data and displays it as single imageboard thread'''

  result = c.get_post(post_id)
  users = {user['id']: user for user in result['users']}
  post_obj = c.prepare_single_post(result['post'], users, load_all_comments=True, retrieve_medias=True)
  return render_template('wakaba_viewthread.html', post=post_obj)


@app.route('/reply', methods=('POST',))
def post_reply():
  # TODO: Process media
  '''Processes form submitted from a thread and adds a comment or comment reply to the post'''

  post_id = request.form['post_id']
  reply_to = request.form.get('reply_to')
  text = request.form['text']
  postredir = int(request.form.get('postredir', '0'))
  if 'file' in request.files and request.files['file'].filename:
    media = c.upload_comment_photo(request.files['file'])
  else:
    media = None

  try:
    if reply_to:
      c.post_reply(reply_to, text, media)

    else:
      c.post_comment(post_id, text, media)

  except ValueError as e:
    return str(e)

  if postredir == 1:
    return redirect(f'/viewpost/{post_id}')
  else:
    return redirect(f'/feed')


@app.route('/newpost', methods=('POST',))
def new_post():
  # TODO: Process media
  '''Processes form submitted from feed view and creates new post for the logged-in user'''

  group = request.form['group']
  text = request.form['text']
  postredir = int(request.form['postredir'])
  visibility = request.form['visibility']
  if 'file' in request.files and request.files['file'].filename:
    media = c.upload_photo(request.files['file'])
  else:
    media = None

  try:
      if visibility == 'all':
        everyone = True
        friends_only = False
      elif visibility == 'friends':
        everyone = False
        friends_only = True
      else:
        everyone = False
        friends_only = False

      res = c.make_post(text, everyone, friends_only, media)

  except ValueError as e:
    return str(e)

  post_id = res['post']['postItemId']

  if postredir == 1:
    return redirect(f'/viewpost/{post_id}')
  else:
    return redirect(f'/feed')


@app.route('/feed/')
def retr_feed():
  '''Displays subscription feed as a thread list
  '''
  limit = request.args.get('limit', '30')
  pages = int(request.args.get('pages', '1'))
  before = request.args.get('b')

  feed, users = c.get_feed(limit=limit, pages=pages, before=before)
  posts, users = c.prepare_feed(feed, users)

  title = 'Подписки'

  return render_template(
    'wakaba_board.html', contents=posts, title=title, can_post=True)


@app.route('/userfeed/<string:user_id>')
def retr_userfeed(user_id):
  '''Displays user feed as a thread list
  '''
  limit = request.args.get('limit', '30')
  pages = int(request.args.get('pages', '1'))
  before = request.args.get('b')

  feed, users = c.get_user_feed(user_id, limit=limit, pages=pages, before=before)
  posts, users = c.prepare_feed(feed, users)

  user = users[user_id]
  title = f'{user["name"]}'

  return render_template(
    'wakaba_board.html', contents=posts, title=title, can_post=False)


@app.route('/userfeed_rss/<string:user_id>')
def retr_userfeed_rss(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))
  feed, users = c.get_user_feed(user_id, limit=limit, pages=pages)
  posts, users = c.prepare_feed(feed, users, retrieve_medias=True)

  user = users[user_id]

  # TODO: Fetch user info here to prepare some nice channel description
  info = ''

  title = f'{user["name"]}'

  link = f'https://mewe.com/i/{user["contactInviteId"]}'
  profile_pic = user['_links']['avatar']['href'].format(imageSize='1280x1280')
  pp_quoted = quote(profile_pic, safe='')
  avatar = f'{hostname}/proxy?url={pp_quoted}&mime=image/jpeg&name={user["id"]}'
  build = datetime.now().strftime(r'%Y-%m-%dT%H:%M:%S%z')

  return render_template(
    'rss.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)


@app.route('/proxy')
def proxy_media():

  url = request.args.get('url')
  # MeWe reports some of types as octet-stream, hence this passing back and forth
  mime = request.args.get('mime', 'application/octet-stream')
  name = request.args.get('name')

  c.refresh_session()
  res = c.session.get(f'https://mewe.com{url}', stream=True)
  if not res.ok:
    return res.iter_content(), 500

  content_length = res.headers['content-length']
  c.last_streamed_response = res
  return res.iter_content(chunk_size=1024), {
     'Content-Type': mime,
     'Content-Length': res.headers['content-length'],
     'Cache-Control': res.headers.get('cache-control', ''),
     'Expires': res.headers.get('expires', ''),
     'Content-Disposition': f'inline; filename={quote(name)}'}


# ###################### Webserver init
if __name__ == "__main__":
  startup()
  app.run(debug=True, host=host, port=port, use_reloader=False)
  cleanup()
