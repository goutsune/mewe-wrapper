from datetime import datetime
from flask import Flask, Response, render_template, request, abort
from requests.utils import quote
from time import sleep

from config import host, port, hostname
from mewe_api import Mewe

# ###################### Init
app = Flask(__name__)


# ###################### Quart setup
def startup():
  '''Check user session via cookies here, perhaps fetch new token
  '''
  print("Connecting...")
  global c
  c = Mewe()


def cleanup():
  '''prepare tokens
  '''
  print("Disconnecting...")
  c.refresh_session()


#@app.before_request
#def conn_check():
#  '''Fetch new token and perhaps update refresh token here
#  '''
#  if not c.session_ok():
#    print("Not connected, reconnecting...")
#    startup()


# ###################### App routes
@app.route('/myworld')
def retr_history():
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

  return render_template(
    'history.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)


@app.route('/userfeed/<string:user_id>')
def retr_userfeed(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  posts, users = c.prepare_feed(user_id, limit=limit, pages=pages, retrieve_medias=True)
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
    'history.html', contents=posts, info=info, title=title, link=link, avatar=avatar, build=build)


@app.route('/viewpost/<string:post_id>')
def show_post(post_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  post = c.prepare_single_post(post_id, load_all_comments=True)
  return render_template('wakaba.html', post=post)


@app.route('/userfeed_rss/<string:user_id>')
def retr_userfeed_rss(user_id):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = request.args.get('limit', '50')
  pages = int(request.args.get('pages', '1'))

  posts, users = c.prepare_feed(user_id, limit=limit, pages=pages, retrieve_medias=True)
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

  res = c.session.get(f'https://mewe.com{url}', stream=True)
  if not res.ok:
    return res.iter_content(), 500

  content_length = res.headers['content-length']
  c.last_streamed_response = res
  return res.iter_content(chunk_size=1024), {
     'Content-Type': mime,
     'Content-Length': content_length,
     'Content-Disposition': f'inline; filename={name}'}

# ###################### Webserver init
if __name__ == "__main__":
  startup()
  app.run(debug=True, host=host, port=port, use_reloader=False)
  cleanup()
