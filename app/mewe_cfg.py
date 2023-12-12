class MeweConfig:
  # Thumbnail size as displayed in thread
  thumb_size = '200'
  # Requested thumbnail size from API, sizes below 400 are not always respected
  # 150x150 seems to return 400x400 in some cases, while 50x50 returns 150x150
  thumb_load_size = '400x400'
