from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
import xml.etree.ElementTree as etree


class EmojiInlineProcessor(InlineProcessor):

  def __init__(self, config):
    super().__init__(r'(:[a-z_-]+:)')
    self.config = config

  def handleMatch(self, m, data):
    emojis = self.config['emoji_dict']
    shortcode = m.group(1)

    # Fail shortly on unknown shortcode, this is done by passing None as match start/end parameter
    if shortcode not in emojis:
      return None, None, None

    el = etree.Element('img')
    el.set('alt', shortcode)
    el.set('class', 'mewe-emoji')
    el.set('src', emojis[shortcode])

    return el, m.start(0), m.end(0)


class MeweEmojiExtension(Extension):

    def __init__(self, **kwargs):
      self.config = {'emoji_dict': [{}, 'Emoji dict object used for substitution']}
      super().__init__(**kwargs)

    def extendMarkdown(self, md):
      md.inlinePatterns.register(EmojiInlineProcessor(self.getConfigs()), 'mewe-emoji', 175)
