MeWe alternative frontend project
============================

An attempt to recreate more classic experience on top of a modern social network as backend.

What is done:
* Per-user RSS feeds with pretty complete post content parsing
* Viewing posts with comments in old-school imageboard style

Under the hood:
* Wrapper for working with MeWe auth and API based on observations from network usage
* Parser for mewe-style emojis
* File proxy to bypass internal auth mechanisms and simply serve media to the browser/feed viewer

Goals in no particular order:
* User feeds as boards
* Groups as boards
* Complete post schema conversion for use in thread viewer
* Ability to browse above directly from the UI
* Rudimentary posting functionality
* Emoji reaction display support
