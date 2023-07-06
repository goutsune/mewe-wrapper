from datetime import datetime
from flask import Flask, Response, render_template, request, abort
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
  '''Generates index page with latest medias and posts.
  Also shows user list and group list'''
  medias = c.prepare_media_feed(limit=10)
  posts = c.prepare_feed(limit=10, only_text=True)
  groups = c.get_groups()
  albums = c.get_albums()

  return render_template('wakaba_index.html', medias=medias, posts=posts, groups=groups, albums=albums)


@app.route('/viewpost/<string:post_id>')
def show_post(post_id):
  '''Processes post data and displays it as single imageboard thread'''

  result = c.get_post(post_id)
  users = {user['id']: user for user in result['users']}
  post_obj = c.prepare_single_post(result['post'], users, load_all_comments=True)
  return render_template('wakaba_thread.html', post=post_obj)


@app.route('/userfeed/<string:user_id>')
def retr_userfeed(user_id):
  '''Displays user feed as a thread list
  '''
  # TODO: Implement pagination
  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  feed, users = c.get_user_feed(user_id, limit=limit, pages=pages)
  posts, users = c.prepare_feed(feed, users, retrieve_medias=True)

  user = users[user_id]
  title = f'{user["name"]}'

  return render_template(
    'wakaba_board.html', contents=posts, title=title)


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
  if request.method == 'HEAD':
    return "OK"

  url = request.args.get('url')
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
     'Content-Length': content_length,
     'Content-Disposition': f'inline; filename={quote(name)}'}

# ###################### Webserver init
if __name__ == "__main__":
  startup()
  app.run(debug=True, host=host, port=port, use_reloader=False)
  cleanup()
