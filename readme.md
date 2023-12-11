MeWe alternative frontend project
============================

How would a modern day obscure social network look if reimagined as Wakaba-like imageboard? Well, see for yourself.

What is done:
* Per-user RSS feeds with pretty complete post content parsing
* Viewing posts with comments in old-school imageboard style
* User feeds as boards
* Rudimentary posting functionality
* Main page with recent media, images and notifications

Under the hood:
* Wrapper for working with MeWe auth and API based on observations from network usage
* Parser for mewe-style emojis, highlights and markdown
* Complete post schema conversion for use in thread viewer
* File proxy to bypass internal auth mechanisms and simply serve media to the browser/feed viewer

Goals in no particular order:
* Groups as boards
* Emoji reaction display support
* More complete posting support (especially gallery uploads)
* Chat support
* AJAX in frontend
* Reply counts in comments to give a better overview on activity
* Manual subscriptions
* Local bookmarks
* Configuration system, including in-browser options e.g. for image quality and amount of posts per-page
