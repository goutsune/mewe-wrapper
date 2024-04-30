from config import hostname
from datetime import datetime, timezone
from os import path
from requests import get
from requests.adapters import HTTPAdapter
from json import load

# Here go various data converters and utility functions that do not directly relate to the MeWe API
def mongouuid_to_date(uuid):
  '''first 4 octets in mongodb UUIDs actually correspond to UNIX timestamp in BE format'''
  timestamp = int(uuid[:8], 16)
  date = datetime.fromtimestamp(timestamp)

  return date.strftime(r'%d %b %Y %H:%M:%S')


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
        pack_dict[pack_name] = load(file)

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


# https://github.com/psf/requests/issues/3070#issuecomment-205070203
class TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        if 'timeout' in kwargs:
            self.timeout = kwargs.pop('timeout')
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        timeout = kwargs.get('timeout')
        if timeout is None and hasattr(self, 'timeout'):
            kwargs['timeout'] = self.timeout
        return super().send(request, **kwargs)
