from datetime import datetime
from flask import Flask, Response, render_template, request, abort, redirect
from requests.utils import quote
from time import sleep

from config import host, port, hostname
from data_proc import DataProcessor
from mewe_api import Mewe
from mewe_cfg import MeweConfig

# ###################### Init
app = Flask(__name__)
# TODO: Implement session loading from inside the application
api = Mewe()
processor = DataProcessor(api)


# ###################### Quart setup
def startup():
  '''Check user session via cookies here, perhaps fetch new token
  '''
  print("Connecting...")


def cleanup():
  '''prepare tokens
  '''
  print("Disconnecting...")
  api.refresh_session()

@app.context_processor
def inject_mewe_settings():
    # Intentionally return uninitialized class, so only static variables are set
    return { 'mewe_cfg': MeweConfig }

# ###################### App routes
@app.route('/')
def make_index():
  '''Generates index page with latest medias and posts.
  Also shows user list and group list'''
  raw_medias = api.get_media_feed(limit=100)
  medias = processor.prepare_media_feed(raw_medias)

  raw_notifies = api.get_notifications()
  notifies = processor.prepare_notifications(raw_notifies)

  posts, users = api.get_feed(limit=50)
  last_active = processor.gather_post_activity(posts, users)

  return render_template('wakaba_index.html', medias=medias, notifies=notifies, last_active=last_active)

@app.route('/markallread', methods=('POST',))
def mark_all_as_read():
  '''Mark all pending notifications as seen'''
  api.mark_as_seen(mark_all=True)
  return redirect('/')


@app.route('/viewpost/<string:post_id>')
def show_post(post_id):
  '''Processes post data and displays it as single imageboard thread'''

  result = api.get_post(post_id)
  users = {user['id']: user for user in result['users']}
  post_obj = processor.prepare_single_post(
    result['post'], users, load_all_comments=True, retrieve_medias=True)

  markread = request.args.get('markread', None)
  if markread is not None:
    api.mark_as_seen(notify_id=markread)

  return render_template(
    'wakaba_viewthread.html',
    post=post_obj,
    hide_images=MeweConfig.hide_post_images)


@app.route('/reply', methods=('POST',))
def post_reply():
  '''Processes form submitted from a thread and adds a comment or comment reply to the post'''

  post_id = request.form['post_id']
  reply_to = request.form.get('reply_to')
  text = request.form['text']
  postredir = int(request.form.get('postredir', '0'))
  if 'file' in request.files and request.files['file'].filename:
    media = api.upload_comment_photo(request.files['file'])
  else:
    media = None

  try:
    if reply_to:
      api.post_reply(reply_to, text, media)

    else:
      api.post_comment(post_id, text, media)

  except ValueError as e:
    return str(e)

  if postredir == 1:
    return redirect(f'/viewpost/{post_id}')
  else:
    return redirect(f'/feed')


@app.route('/newpost', methods=('POST',))
def new_post():
  '''Processes form submitted from feed view and creates new post for the logged-in user'''

  group = request.form['group']
  text = request.form['text']
  postredir = int(request.form['postredir'])
  visibility = request.form['visibility']

  if 'file' in request.files and request.files['file'].filename:
    medias = [api.upload_photo(x) for x in request.files.getlist('file')]
  else:
    medias = None

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

      res = api.make_post(text, everyone, friends_only, medias)

  except ValueError as e:
    return str(e)

  post_id = res['post']['postItemId']

  if postredir == 1:
    return redirect(f'/viewpost/{post_id}')
  else:
    return redirect(f'/feed/')


@app.route('/feed/')
def retr_feed():
  '''Displays subscription feed as a thread list
  '''
  limit = request.args.get('limit', '30')
  pages = int(request.args.get('pages', '1'))
  before = request.args.get('b')

  feed, users = api.get_feed(limit=limit, pages=pages, before=before)
  posts, users = processor.prepare_feed(feed, users)

  title = 'Подписки'

  return render_template(
    'wakaba_board.html',
    contents=posts,
    title=title,
    can_post=True,
    hide_images=MeweConfig.hide_thread_images)


@app.route('/userfeed/<string:user_id>')
def retr_userfeed(user_id):
  '''Displays user feed as a thread list
  '''
  limit = request.args.get('limit', '30')
  pages = int(request.args.get('pages', '1'))
  before = request.args.get('b')

  try:
    feed, users = api.get_user_feed(user_id, limit=limit, pages=pages, before=before)
  except IndexError as e:
    return render_template('wakaba_base.html', msg=e)
  posts, users = processor.prepare_feed(feed, users)

  user = users[user_id]
  title = f'{user["name"]}'

  return render_template(
    'wakaba_board.html',
    contents=posts,
    title=title,
    can_post=False,
    hide_images=MeweConfig.hide_thread_images)


@app.route('/userfeed_rss/<string:user_id>')
def retr_userfeed_rss(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))
  feed, users = api.get_user_feed(user_id, limit=limit, pages=pages)
  posts, users = processor.prepare_feed(feed, users, retrieve_medias=True)

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

  res = api.proxy_stream(url)
  if not res.ok:
    return res.iter_content(), res.status_code

  api.last_streamed_response = res
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
