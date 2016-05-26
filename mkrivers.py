#!/usr/bin/env python

import os
import glob
import json
import time
import arrow
import urllib
import socket
import random
import bleach
import logging
import cPickle
import argparse
import requests
import threading
import feedparser
from datetime import datetime
from collections import deque

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
)
logging.getLogger('requests').setLevel(logging.WARNING)

__version__ = '0.3'

FEED_CHECK_INITIAL = (5, 5*60)     # min/max seconds before first check
FEED_CHECK_REGULAR = (5*60, 30*60) # min/max seconds for next check, after first check
FEED_REQUEST_TIMEOUT = 15          # HTTP timeout when requesting feed
WATCH_INPUT_INTERVAL = 15*60       # check source file every N seconds
RIVER_UPDATES_LIMIT = 300          # number of feed updates to include
RIVER_CHAR_LIMIT = 280             # character limit in item description
RIVER_WRITE_INTERVAL = 15          # write river files every N seconds
RIVER_CACHE_DIR = '.mkrivers'      # where to store feed history
RIVER_FIRST_ITEMS_LIMIT = 5        # number of items to include on first run
RIVER_TIME_FMT = 'ddd, DD MMMM YYYY HH:mm:ss Z'

class WebFeed(object):
    def __init__(self, url, source):
        self.url = url
        self.source = source
        self.request_headers = {}
        self.checks = 0
        self.history = self.read_pickle()

    def check(self):
        try:
            response = self.request_feed()
        except (requests.exceptions.RequestException, socket.error) as e:
            self.log('could not retrieve feed: %s' % e, 'error')
        else:
            self.process_response(response)
        finally:
            self.checks += 1
            self.schedule_next_check()
            self.write_pickle(self.history)

    def request_feed(self):
        "Make HTTP request for feed URL"
        default_headers = {
            'User-Agent': 'mkrivers/%s (https://github.com/edavis/mkrivers)' % __version__,
        }

        headers = {}
        headers.update(default_headers)
        headers.update(self.request_headers)

        self.log('requesting feed (headers = %r)' % self.request_headers)

        try:
            resp = requests.get(self.url, headers=headers, timeout=FEED_REQUEST_TIMEOUT)
            resp.raise_for_status()
        except (requests.exceptions.RequestException, socket.error):
            raise
        else:
            self.request_headers.update({
                'If-Modified-Since': resp.headers.get('last-modified'),
            })

            if resp.status_code == 304:
                return

            return resp

    def process_response(self, response):
        if response is None:
            self.log('feed returned 304, skipping')
            return

        parsed = feedparser.parse(response.text)

        def entry_fingerprint(entry):
            if entry.get('guid'):
                return entry.get('guid')
            else:
                return '|'.join([entry.get('title', ''), entry.get('link', '')])

        def entry_timestamp(entry):
            for key in ['published_parsed', 'updated_parsed', 'created_parsed']:
                if not entry.get(key):
                    continue

                obj = entry[key][:6]
                reported = arrow.get(datetime(*obj))

                # Return Jan 1, 1970 if item pubDate is pre-2000
                if reported < arrow.Arrow(2000, 1, 1):
                    return arrow.Arrow(1970, 1, 1)

                # Return reported pubDate only if in the past (i.e., nothing in future)
                elif reported < arrow.utcnow():
                    return reported

            # If all else fails, return current UTC as pubDate
            return arrow.utcnow()

        def clean_text(text, limit=RIVER_CHAR_LIMIT, suffix=u'\u2026'):
            "Trim text to limit, adding suffix if text is longer than limit."
            cleaned = bleach.clean(text, tags=[], strip=True).strip()

            if len(cleaned) > limit:
                # trim to first space rather than cutting a word off in the middle of it
                cur_idx = limit - 1
                cur_char = cleaned[cur_idx]
                while cur_char != ' ':
                    cur_idx -= 1
                    cur_char = cleaned[cur_idx]

                c = cleaned[:cur_idx].strip('.,')
                return c + suffix
            else:
                return cleaned

        def entry_text(entry):
            "Populate the title and body, depending on the existence and value of the other."
            obj = {}

            if not entry.get('title') and not entry.get('description'):
                return

            elif entry.get('title') and entry.get('description'):
                obj['title'] = entry.get('title')
                if entry.get('title') != entry.get('description'):
                    obj['body'] = entry.get('description')
                else:
                    obj['body'] = ''

            elif not entry.get('title') and entry.get('description'):
                obj = {
                    'title': entry.get('description'),
                    'body': '',
                }

            elif entry.get('title'):
                obj = {
                    'title': entry.get('title'),
                    'body': '',
                }

            return {
                'title': clean_text(obj['title']),
                'body': clean_text(obj['body']),
            }

        update_items = []
        update_obj = {
            'feedUrl': self.url,
            'feedTitle': parsed.feed.get('title', 'Default Title'),
            'feedDescription': parsed.feed.get('description', ''),
            'websiteUrl': parsed.feed.get('link', ''),
            'whenLastUpdate': arrow.utcnow().format(RIVER_TIME_FMT),
        }

        for entry in parsed.entries:
            fingerprint = entry_fingerprint(entry)
            if fingerprint in self.history:
                continue

            pub_date = entry_timestamp(entry)
            obj = {
                'permaLink': entry.get('guid', ''),
                'pubDate': pub_date.format(RIVER_TIME_FMT),
                'link': entry.get('link', ''),
            }

            text_info = entry_text(entry)
            if text_info is not None:
                obj.update(text_info)
            else:
                continue

            update_items.append(obj)

            self.history.appendleft(fingerprint)

        if update_items:
            self.log('found %s new items' % len(update_items))

            if self.checks == 0:
                update_items = update_items[:RIVER_FIRST_ITEMS_LIMIT]

            for item in reversed(update_items):
                with self.source.counter_lock:
                    self.source.counter += 1

                item['id'] = str(self.source.counter).zfill(7)

            update_obj['item'] = update_items
            self.source.struct.appendleft(update_obj)
            self.source.dirty = True
        else:
            self.log('no new items found')

    def schedule_next_check(self):
        "Schedule feed for next check"
        interval = random.randint(*FEED_CHECK_REGULAR)
        new_timer = create_timer(self.check, interval, self.url)
        self.source.timers[self.url] = new_timer

    ###########################################################################
    # Pickle utilities

    def pickle_path(self):
        "Where to store pickle object for this feed"
        if not os.path.isdir(self.source.source_cache):
            os.makedirs(self.source.source_cache)

        fname = urllib.quote(self.url, safe='')
        return os.path.join(self.source.source_cache, fname) + '.pkl'

    def write_pickle(self, obj):
        with open(self.pickle_path(), 'w') as fp:
            return cPickle.dump(obj, fp)

    def read_pickle(self):
        "Return history from pickle object or create it anew"
        try:
            with open(self.pickle_path()) as fp:
                return cPickle.load(fp)
        except (IOError, cPickle.UnpicklingError):
            return deque(maxlen=RIVER_UPDATES_LIMIT)

    def log(self, msg, level='debug'):
        msg = ('[%-50s] ' % self.url[:50]) + msg
        func = getattr(logging, level)
        func(msg)

class Source(object):
    counter = 0

    def __init__(self, fname, output):
        self.fname = fname
        self.output = output
        self.urls = list(self.read_urls())
        self.timers = {}
        self.source_cache = os.path.join(RIVER_CACHE_DIR, path_basename(fname))
        self.struct = self.read_pickle()
        self.dirty = False
        self.counter_lock = threading.Lock()

        self.misc_timers = {
            'write_river': create_timer(self.write_river, RIVER_WRITE_INTERVAL),
            'watch_input': create_timer(self.watch_input, WATCH_INPUT_INTERVAL),
        }

    def read_urls(self):
        "Return feed URLs in source input"
        with open(self.fname) as fp:
            for url in fp:
                url = url.strip()
                if not url or url.startswith('#'): continue
                yield url

    def start_feeds(self):
        for url in self.urls:
            self.start_feed(url)

    def shutdown(self):
        while threading.active_count() > 1:
            for thread in threading.enumerate():
                try:
                    thread.cancel()
                except AttributeError: # MainThread has no cancel
                    pass

            time.sleep(1)

    def start_feed(self, url):
        "Start monitoring feed"
        feed = WebFeed(url, self)
        interval = random.randint(*FEED_CHECK_INITIAL)
        timer = create_timer(feed.check, interval, url)
        self.timers[url] = timer

    def stop_feed(self, url):
        "Stop monitoring feed"
        logging.debug('stop_feed: stopping %s' % url)
        self.timers[url].cancel()
        del self.timers[url]

    def watch_input(self):
        "Re-scan source input for added/removed feeds"
        logging.debug('watch_input: re-scanning %s for new urls' % self.fname)
        prev_urls = set(self.urls)
        new_urls = set(self.read_urls())
        added_urls = filter(lambda url: url not in prev_urls, new_urls)
        removed_urls = filter(lambda url: url not in new_urls, prev_urls)

        if added_urls:
            logging.debug('added_urls = %r' % added_urls)
            for url in added_urls:
                self.start_feed(url)

        if removed_urls:
            logging.debug('removed_urls = %r' % removed_urls)
            for url in removed_urls:
                self.stop_feed(url)

        self.urls = list(new_urls)

        self.misc_timers['watch_input'] = create_timer(self.watch_input, WATCH_INPUT_INTERVAL)

    def write_river(self):
        "Generate river.js file"

        if not self.dirty:
            self.misc_timers['write_river'] = create_timer(self.write_river, RIVER_WRITE_INTERVAL)
            return

        if not os.path.isdir(os.path.dirname(self.output)):
            os.makedirs(os.path.dirname(self.output))

        obj = {
            'updatedFeeds': {
                'updatedFeed': list(self.struct),
            },
            'metadata': {
                'docs': 'http://riverjs.org/',
                'secs': '',
                'version': '3',
                'whenGMT': arrow.utcnow().format(RIVER_TIME_FMT),
                'whenLocal': arrow.now().format(RIVER_TIME_FMT),
                'aggregator': 'mkrivers %s' % __version__,
            },
        }

        with open(self.output, 'w') as fp:
            fp.write('onGetRiverStream(')
            json.dump(obj, fp, indent=2, sort_keys=True)
            fp.write(')\n')

            fp.flush()
            os.fsync(fp.fileno())

        self.write_pickle(self.struct)
        self.dirty = False
        self.misc_timers['write_river'] = create_timer(self.write_river, RIVER_WRITE_INTERVAL)

    ###########################################################################
    # Pickle utilities

    def pickle_path(self):
        "Where to store pickle object for this river"
        if not os.path.isdir(self.source_cache):
            os.makedirs(self.source_cache)

        fname = path_basename(self.output)
        return os.path.join(self.source_cache, fname) + '.pkl'

    def write_pickle(self, obj):
        with open(self.pickle_path(), 'w') as fp:
            return cPickle.dump(obj, fp)

    def read_pickle(self):
        try:
            with open(self.pickle_path()) as fp:
                return cPickle.load(fp)
        except (IOError, cPickle.UnpicklingError):
            return deque(maxlen=RIVER_UPDATES_LIMIT)

def create_timer(func, interval, name=None):
    "Return daemonized Timer object"
    t = threading.Timer(interval, func)
    if name is not None:
        t.name = name
    t.start()
    return t

def path_basename(p):
    "Return the given path stripped of leading directories and its file extension"
    b = os.path.basename(p)
    b, _ = os.path.splitext(b)
    return b

def river_output(filename, output, suffix='.js'):
    "Return river.js file path from input filename."
    b = os.path.basename(filename)
    b, _ = os.path.splitext(b)
    return os.path.join(output, b) + suffix

def main(args):
    sources = []

    for fname in glob.iglob(args.input + '/*.txt'):
        output = river_output(fname, args.output)
        s = Source(fname, output)
        logging.debug('main: %s has %d feeds' % (fname, len(s.urls)))
        s.start_feeds()
        sources.append(s)

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        logging.debug('Shutting down...')

        for source in sources:
            source.shutdown()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output')
    parser.add_argument('input')
    args = parser.parse_args()
    main(args)
