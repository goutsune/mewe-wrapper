class MeweConfig:
  '''This configuration class defines some of the common constants used by both
  Mewe API class and data processor class.
  '''
  # Default user agent, this one is set Android client
  user_agent = 'MeWeAndroid/8.0.0'
  # Thumbnail size as displayed in thread template
  thumb_size = '200'
  # Requested thumbnail size from API, sizes below 400 are not always respected
  # 150x150 seems to return 400x400 in some cases, while 50x50 returns 150x150
  thumb_load_size = '400x400'
  # Max image size as requested, the actual resolution can be larger and smaller, in general 2000x2000 seems
  # to return original picture size
  image_load_size = '2000x2000'
  # Collapse multi-image view when viewing thread
  hide_post_images = False
  # Collapse multi-image view when viewing board
  hide_thread_images = True
