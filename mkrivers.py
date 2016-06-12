#!/usr/bin/env python

import os
import glob
import json
import time
import uuid
import arrow
import urllib
import socket
import random
import logging
import cPickle
import sqlite3
import argparse
import requests
import threading
import feedparser
from collections import deque, Counter
from xml.etree import ElementTree as ET
from entry_utils import (
    entry_timestamp, entry_text
)

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
)
logging.getLogger('requests').setLevel(logging.WARNING)

__version__ = '0.10'

FEED_CHECK_INITIAL = (5, 30*60)    # min/max seconds before first check
FEED_CHECK_REGULAR = (15*60, 60*60) # min/max seconds for next check, after first check
FEED_REQUEST_TIMEOUT = 15          # HTTP timeout when requesting feed
FEED_HEALTH_MIN_CHECKS = 2         # min checks before check_feed_health will examine the feed
FEED_HEALTH_ERR_THRESHOLD = 0.8    # display warning if feed fails more than N percent of time
WATCH_INPUT_INTERVAL = 30*60       # check source file every N seconds
WATCH_DIR_INTERVAL = 5*60          # check input directory for source file updates every N seconds
RIVER_UPDATES_LIMIT = 300          # number of feed updates to include
RIVER_WRITE_INTERVAL = 60          # write river files every N seconds
RIVER_CACHE_DIR = '.mkrivers'      # where to store feed history
RIVER_FIRST_ITEMS_LIMIT = 5        # number of items to include on first run
RIVER_TIME_FMT = 'ddd, DD MMM YYYY HH:mm:ss Z'

class WebFeed(object):
    def __init__(self, url, source):
        self.url = url
        self.source = source
        self.request_headers = {}
        self.checks = 0
        self.status_codes = Counter()

    def check(self):
        "Check feed for new entries"
        if self.source.stopped.is_set():
            return

        try:
            response = self.request_feed()
        except (requests.exceptions.RequestException, socket.error) as e:
            self.log('could not retrieve feed: %s' % e, 'error')
        else:
            self.process_response(response)
        finally:
            self.checks += 1
            self.check_feed_health()
            self.schedule_next_check()

    def request_feed(self):
        "Make HTTP request for feed URL"
        default_headers = {
            'User-Agent': 'mkrivers/v%s (https://github.com/edavis/mkrivers)' % __version__,
        }

        headers = {}
        headers.update(default_headers)
        headers.update(self.request_headers)

        try:
            resp = requests.get(self.url, headers=headers, timeout=FEED_REQUEST_TIMEOUT)
        except (requests.exceptions.RequestException, socket.error):
            self.status_codes[500] += 1
            raise
        else:
            self.status_codes[resp.status_code] += 1

        try:
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

    def build_feed_portion(self, parsed):
        "Build the feed portion of the river update"
        return {
            'feedUrl': self.url,
            'feedTitle': parsed.feed.get('title', 'Default Title'),
            'feedDescription': parsed.feed.get('description', ''),
            'websiteUrl': parsed.feed.get('link', ''),
            'whenLastUpdate': arrow.utcnow().format(RIVER_TIME_FMT),
            'item': [],
        }

    def build_item_portion(self, entry):
        obj = {
            'permaLink': entry.get('guid', ''),
            'link': entry.get('link', ''),
            'pubDate': entry_timestamp(entry).format(RIVER_TIME_FMT),
        }

        if entry.get('comments'):
            obj['comments'] = entry.get('comments')

        text_info = entry_text(entry)
        if text_info is not None:
            obj.update(text_info)
        else:
            return

        return obj

    def run_callbacks(self, entry, river_item):
        try:
            import callbacks
        except ImportError:
            return

        try:
            for callback in callbacks.item_callbacks:
                callback(self.url, entry, river_item)
        except AttributeError:
            return

    def process_response(self, response):
        if response is None:
            self.log('feed returned 304, skipping')
            return

        # TODO do bozo checking
        parsed = feedparser.parse(response.text)

        river_obj = self.build_feed_portion(parsed)
        item_obj = []

        for entry in sorted(parsed.entries, key=lambda e: entry_timestamp(e)):
            if not self.source.include_entry(self.url, entry):
                continue

            item_portion = self.build_item_portion(entry)
            if item_portion is not None:
                self.run_callbacks(entry, item_portion)
                item_obj.insert(0, item_portion)

        if item_obj:
            self.log('found %s new items' % len(item_obj))

            if self.checks == 0:
                river_obj['feedTitle'] += '*'
                item_obj = item_obj[:RIVER_FIRST_ITEMS_LIMIT]

            for river_item in reversed(item_obj):
                with self.source.counter_lock:
                    self.source.counter += 1
                    river_item['id'] = str(self.source.counter).zfill(7)

                river_obj['item'].insert(0, river_item)

            self.source.insert_update(river_obj)

        else:
            self.log('no new items found')

    def check_feed_health(self):
        "Insert warning in river if feed appears broken"

        if self.checks < FEED_HEALTH_MIN_CHECKS:
            return

        failures = sum(v for k, v in self.status_codes.items() if k >= 400)
        err_rate = failures / float(self.checks)

        if err_rate < FEED_HEALTH_ERR_THRESHOLD:
            return

        self.log('feed has error rate of %.3f' % err_rate, 'warn')

        with self.source.counter_lock:
            self.source.counter += 1
            new_id = self.source.counter

        now = arrow.utcnow().format(RIVER_TIME_FMT)

        update_obj = {
            'feedUrl': self.url,
            'feedTitle': 'mkrivers: feed health',
            'feedDescription': '',
            'websiteUrl': self.url,
            'whenLastUpdate': now,
            'item': [{
                'id': str(new_id).zfill(7),
                'title': 'Error: %s' % self.url,
                'body': 'Error rate of %.3f after %d checks' % (err_rate, self.checks),
                'pubDate': now,
                'permaLink': '',
                'link': '',
            }],
        }

        self.source.insert_update(update_obj)

    def schedule_next_check(self):
        "Schedule feed for next check"
        interval = random.randint(*FEED_CHECK_REGULAR)
        new_timer = create_timer(self.check, interval, self.url)
        self.source.timers[self.url] = new_timer

    def log(self, msg, level='debug'):
        prefix = '(%s) %s' % (path_basename(self.source.fname), self.url)
        msg = ('[%-70s] ' % prefix[:70]) + msg
        func = getattr(logging, level)
        func(msg)

class Source(object):
    def __init__(self, fname, output):
        self.fname = fname
        self.output = output
        self.urls = list(self.read_urls())
        self.timers = {}
        self.stopped = threading.Event()
        self.dirty = False
        self.counter_lock = threading.Lock()
        self.counter = 0
        self.started = arrow.utcnow()

        self.init_database()
        self.struct = self.read_struct()

        self.misc_timers = {
            'write_river': create_timer(self.write_river, RIVER_WRITE_INTERVAL),
            'watch_input': create_timer(self.watch_input, WATCH_INPUT_INTERVAL),
        }

    def read_urls(self):
        "Return feed URLs in source input"
        def is_skip_url(url): return not url or url.startswith('#')
        def is_include_url(url): return url.startswith('!')
        def is_remove_url(url): return url.startswith('-')

        def read_remote_txt(url, urls):
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            for url in r.iter_lines():
                url = url.strip()
                if is_skip_url(url): continue

                if is_include_url(url):
                    process_remote(url[1:], urls)
                else:
                    urls.add(url)

        def read_remote_opml(url, urls):
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            doc = ET.fromstring(r.text)
            head, body = doc
            for outline in body.findall('outline'):
                if outline.attrib.get('type', '') == 'include':
                    iurl = outline.attrib.get('url')
                    if iurl:
                        process_remote(iurl, urls)
                elif outline.attrib.get('xmlUrl'):
                    xmlUrl = outline.attrib.get('xmlUrl')
                    urls.add(xmlUrl)

        def process_remote(url, urls):
            if url.endswith('.txt'):
                read_remote_txt(url, urls)
            elif url.endswith('.opml'):
                read_remote_opml(url, urls)

        def read_local_txt(fname):
            urls = set()

            with open(fname) as fp:
                for url in fp:
                    url = url.strip()
                    if is_skip_url(url): continue

                    if is_include_url(url):
                        process_remote(url[1:], urls)
                    elif is_remove_url(url):
                        urls.remove(url[1:])
                    else:
                        urls.add(url)

            return urls

        return read_local_txt(self.fname)

    def execute_sql(self, statement, params=[]):
        "Execute a SQL statement against the database within a transaction"
        if self.stopped.is_set():
            return

        with self.db_lock:
            cursor = self.conn.cursor()
            with self.conn:
                cursor.execute(statement, params)
            return cursor.fetchone()

    def init_database(self):
        "Initialize the sqlite3 database"
        sqlite_fname = os.path.join(RIVER_CACHE_DIR, path_basename(self.fname)) + '.db'
        self.conn = sqlite3.connect(sqlite_fname, check_same_thread=False)
        self.db_lock = threading.Lock()
        self.execute_sql('create table if not exists history (id integer primary key, feed_url text, pub_date text, added text, fingerprint text);')
        self.execute_sql('create index if not exists fingerprint_idx on history (fingerprint);')
        self.execute_sql('create table if not exists cache (key text primary key, value blob);')

    def start_feeds(self):
        for url in self.urls:
            self.start_feed(url)

    def shutdown(self):
        logging.debug('shutdown: stopping feeds for %s' % self.fname)
        self.stopped.set() # set to True
        for url, timer in self.timers.items():
            timer.cancel()
        self.misc_timers['write_river'].cancel()
        self.misc_timers['watch_input'].cancel()

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

        self.schedule_watch_input()

    def include_entry(self, feed_url, entry):
        "Whether to include the parsed feed entry in the source."
        pub_date = entry_timestamp(entry)
        fingerprint = entry.get('guid') or entry.get('link')
        if not fingerprint:
            fingerprint = str(uuid.uuid4())

        # First, check if we've already seen this fingerprint.
        (count,) = self.execute_sql('select count(*) from history where fingerprint = ?', [fingerprint])

        # Then, insert it into the history table. We do this so the
        # history table accurately reflects all seen feed entries.
        self.execute_sql('insert into history (feed_url, pub_date, fingerprint, added) values (?, ?, ?, strftime("%Y-%m-%dT%H:%M:%f+00:00", "now"))', [feed_url, str(pub_date), fingerprint])

        # Finally, return True if the fingerprint wasn't in the table
        # when we first started.
        return int(count) == 0

    def insert_update(self, update):
        self.struct.appendleft(update)
        self.dirty = True

    def read_struct(self):
        result = self.execute_sql('select value from cache where key = "struct"')

        if result is None or result == '':
            return deque(maxlen=RIVER_UPDATES_LIMIT)

        (data,) = result

        try:
            return cPickle.loads(str(data))
        except cPickle.UnpicklingError:
            return deque(maxlen=RIVER_UPDATES_LIMIT)

    def write_struct(self, obj):
        pkl = cPickle.dumps(obj)
        self.execute_sql('insert or replace into cache (key, value) values ("struct", ?)', [sqlite3.Binary(pkl)])

    def serialize_struct(self, fp, obj):
        "Serialize struct to JSON and write to output."

        fp.write('onGetRiverStream(')
        json.dump(obj, fp, indent=2, sort_keys=True)
        fp.write(')\n')

        fp.flush()
        os.fsync(fp.fileno())

    def schedule_write_river(self):
        self.misc_timers['write_river'] = create_timer(self.write_river, RIVER_WRITE_INTERVAL)

    def schedule_watch_input(self):
        self.misc_timers['watch_input'] = create_timer(self.watch_input, WATCH_INPUT_INTERVAL)

    def write_river(self):
        "Generate river.js file"

        if not self.dirty:
            self.schedule_write_river()
            return

        if not os.path.isdir(os.path.dirname(self.output)):
            os.makedirs(os.path.dirname(self.output))

        obj = {
            'updatedFeeds': {
                'updatedFeed': list(self.struct),
            },
            'metadata': {
                'docs': 'http://riverjs.org/',
                'whenStarted': self.started.format(RIVER_TIME_FMT),
                'whenGMT': arrow.utcnow().format(RIVER_TIME_FMT),
                'whenLocal': arrow.now().format(RIVER_TIME_FMT),
                'aggregator': 'mkrivers v%s' % __version__,
            },
        }

        with open(self.output, 'w') as fp:
            self.serialize_struct(fp, obj)

        self.write_struct(self.struct)
        self.dirty = False
        self.schedule_write_river()

def create_timer(func, interval, name=None, args=[], kwargs={}):
    "Return daemonized Timer object"
    t = threading.Timer(interval, func, args, kwargs)
    if name is not None:
        t.name = name
    t.start()
    return t

def path_basename(p):
    "Return the given path stripped of leading directories and its file extension"
    b = os.path.basename(p)
    b, _ = os.path.splitext(b)
    return b

def load_sources(args, sources={}):
    current_fnames = glob.glob(args.input + '/*.txt')
    added_fnames = filter(lambda f: f not in sources, current_fnames)
    removed_fnames = filter(lambda f: f not in current_fnames, sources)

    for fname in added_fnames:
        logging.debug('load_sources: adding %s' % fname)
        output = os.path.join(args.output, path_basename(fname)) + '.js'
        s = Source(fname, output)
        s.start_feeds()
        sources[fname] = s

    for fname in removed_fnames:
        logging.debug('load_sources: removing %s' % fname)
        s = sources[fname]
        s.shutdown()
        del sources[fname]

    create_timer(load_sources, WATCH_DIR_INTERVAL, args=[args, sources])

def main(args):
    load_sources(args)

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        logging.debug('Shutting down...')

        while threading.active_count() > 1:
            for thread in threading.enumerate():
                try:
                    thread.cancel()
                except AttributeError:
                    pass

            logging.debug('%d threads remaining...' % threading.active_count())
            time.sleep(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output')
    parser.add_argument('input')
    args = parser.parse_args()
    main(args)
