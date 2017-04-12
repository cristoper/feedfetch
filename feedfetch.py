"""
The FeedCache class provided by this module adds [thread- and multiprocess-safe]
caching to Mark Pilgrim's feedparser (https://pypi.python.org/pypi/feedparser)

feedparser on github: https://github.com/kurtmckee/feedparser
"""

import feedparser
import os.path
import datetime
from http.client import NOT_MODIFIED
import re
import logging
from feedfetch.locked_shelf import MutexShelf, RWShelf
from typing import Union, Type, Callable, Optional

locked_shelf_t = Union[Type[RWShelf], Type[MutexShelf]]

logger = logging.getLogger(__name__)


class FeedCache:
    """A wrapper for feedparser which handles caching using a locking wrapper
    around the standard shelve library. Thread and multiprocess safe."""

    class Feed:
        """A wrapper class around a parsed feed so we can add some metadata (like
        an expire time)."""
        def __init__(self, feed: feedparser.util.FeedParserDict, expire_dt:
                     datetime.datetime = datetime.datetime.utcnow()) -> None:
            self.feed = feed
            self.expire_dt = expire_dt

    class ParseError(Exception):
        pass

    class FetchError(Exception):
        def __init__(self, message: str, statuscode: Optional[int] =None):
            super().__init__(message)
            self.status = statuscode

    def __init__(self, db_path: str, min_age: int = 1200,
                 shelf_t: locked_shelf_t=RWShelf,
                 parse: Callable=feedparser.parse) -> None:
        """
        Args:
            db_path: Path to the dbm file which holds the cache
            min_age: Minimum time (seconds) to keep feed in hard cache. This is
            overridden by a smaller max-age attribute in the received
            cache-control http header (default: 1200)
        """
        self.shelf_t = shelf_t
        self.path = db_path
        self.min_age = min_age
        self.parse = parse

    def __get(self, url: str) -> feedparser.util.FeedParserDict:
        """Get a feed from the cache db by its url."""
        if os.path.exists(self.path):
            with self.shelf_t(self.path, flag='r') as shelf:
                return shelf.get(url)
        else:
            logger.info("Cache db file does not exist at {}".format(self.path))
        return None

    def __update(self, url: str, feed: feedparser.util.FeedParserDict):
        """Update a feed in the cache db."""
        with self.shelf_t(self.path, flag='c') as shelf:
            logger.info("Updated feed for url: {}".format(url))
            shelf[url] = feed

    def fetch(self, url) -> feedparser.util.FeedParserDict:
        """Fetch an RSS/Atom feed given a URL.

        If the feed is in the cache and it is still fresh (younger than
        `min_age`), then it is returned directly.

        If the feed is older than `min_age`, it is re-fetched from the remote
        server (using etag and/or last-modified headers if available so that the
        server can return a cached version).

        When the response is received from the server, then the feed is updated
        in the on-disk cache.

        Args:
            url: the url of the feed to fetch

        Returns:
            The parsed feed (or throws an exception if the feed couldn't be
            fetched/parsed)

        Throws:
            URLError: This is propagated from feedparser/urllib if the domain
            name cannot be resolved
            FetchError: If there was an HTTP error (the http status code is
            returned in the exception object's `status` attribute)
            ParseError: If feedparser successfully fetched a resource over http,
            but wasn't able to parse it as a feed.
        """
        etag = None
        lastmod = None
        now = datetime.datetime.now()

        logger.info("Fetching feed for url: {}".format(url))
        cached = self.__get(url)
        if cached:
            logger.info("Got feed from cache for url: {}".format(url))
            if now < cached.expire_dt:
                # If cache is fresh, use it without further ado
                logger.info("Fresh feed found in cache: {}".format(url))
                return cached.feed

            logger.info("Stale feed found in cache: {}".format(url))
            etag = cached.feed.get('etag')
            etag = etag.lstrip('W/') if etag else None  # strip weak etag
            lastmod = cached.feed.get('modified')
        else:
            logger.info("No feed in cache for url: {}".format(url))

        # Cache wasn't fresh in db, so we'll request it, but give origin etag
        # and/or last-modified headers (if available) so we only fetch and
        # parse it if it is new/updated.
        logger.info("Fetching from remote {}".format(url))
        feed = self.parse(url, etag=etag, modified=lastmod)

        fetched = FeedCache.Feed(feed)

        if feed is None or feed.get('status') is None:
            logger.info("Failed to fetch feed ({})".format(url))
            raise self.FetchError("Failed to fetch feed")
        elif feed.get('status') > 399:
            logger.info("HTTP error {} ({})".format(feed.get('status'), url))
            raise self.FetchError("HTTP error", feed.get('status'))
        elif (feed.get('status') < 399 and len(feed.get('entries')) == 0 and
              feed.get('bozo')):
            logger.info("Parse error ({})".format(feed.get('bozo_exception')))
            raise self.ParseError("Parse error: {}".format(feed.get('bozo_exception')))

        logger.info("Got feed from feedparser {}".format(url))
        logger.debug("Feed: {}".format(feed))
        if feed.get('status') == NOT_MODIFIED:
            # Source says feed is still fresh
            logger.info("Server says feed is still fresh: {}".format(url))
            fetched.feed = cached.feed

        # Add to/update cache with new expire_dt
        # Using max-age parsed from cache-control header, if it exists
        cc_header = fetched.feed.get('headers').get('cache-control') or ''
        ma_match = re.search('max-age=(\d+)', cc_header)
        if ma_match:
            min_age = min(int(ma_match.group(1)), self.min_age)
        else:
            min_age = self.min_age
        fetched.expire_dt = now + datetime.timedelta(seconds=min_age)
        self.__update(url, fetched)
        return fetched.feed
