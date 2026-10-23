"""Account-scoped publication facts and durable comment processing receipts."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing


class SpaceActivityStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS publications (
                    account TEXT NOT NULL, post_id TEXT NOT NULL, content TEXT NOT NULL,
                    url TEXT NOT NULL, kind TEXT NOT NULL, published_at REAL NOT NULL,
                    PRIMARY KEY(account,post_id)
                );
                CREATE TABLE IF NOT EXISTS scanned_posts (
                    account TEXT NOT NULL, post_id TEXT NOT NULL, scanned_at REAL NOT NULL,
                    PRIMARY KEY(account,post_id)
                );
                CREATE TABLE IF NOT EXISTS activity_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS comment_receipts (
                    account TEXT NOT NULL, key TEXT NOT NULL, post_id TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                    PRIMARY KEY(account,key)
                );
            """)
            db.execute("UPDATE comment_receipts SET state='failed' WHERE state='judging'")
            db.execute("UPDATE comment_receipts SET state='unknown' WHERE state='sending'")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def publication(self, account, post, kind='observed', published_at=None):
        if not post.get('id') or str(post.get('qq', account)) != account:
            return
        try:
            moment = published_at or float(post.get('time') or 0) or time.time()
            if moment > 10**12:
                moment /= 1000
        except (TypeError, ValueError):
            moment = time.time()
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO publications VALUES (?,?,?,?,?,?)
                ON CONFLICT(account,post_id) DO UPDATE SET content=excluded.content,
                url=excluded.url, kind=CASE WHEN excluded.kind='observed' THEN publications.kind ELSE excluded.kind END
                """, (account, str(post['id']), str(post.get('text', ''))[:2000],
                      str(post.get('url', '')), kind, moment))

    def recent(self, account, count=3):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT post_id,content,url,kind,published_at FROM publications WHERE account=? ORDER BY published_at DESC LIMIT ?",
                (account, count))]

    @staticmethod
    def key(post_id, parent, comment):
        identity = [post_id, parent.get('id'), comment.get('id'), comment.get('qq'),
                    comment.get('time'), comment.get('text')]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def observe(self, account, post):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO activity_meta VALUES (?,?)", ('watch_started:' + account, time.time()))
            started = db.execute("SELECT value FROM activity_meta WHERE key=?", ('watch_started:' + account,)).fetchone()[0]
            scanned = db.execute("SELECT 1 FROM scanned_posts WHERE account=? AND post_id=?",
                                 (account, post['id'])).fetchone()
            own_publication = db.execute("SELECT kind,published_at FROM publications WHERE account=? AND post_id=?",
                                        (account, post['id'])).fetchone()
            fresh = (own_publication and own_publication['kind'] != 'observed' and own_publication['published_at'] >= started)
            baseline = not scanned and not fresh
            for parent in post.get('comments', []):
                for comment in [parent, *parent.get('replies', [])]:
                    usable = (parent.get('id') and comment.get('qq') and comment.get('text')
                              and str(comment['qq']) != account and not comment.get('private')
                              and not parent.get('private'))
                    state = 'seen' if baseline or not usable else 'new'
                    def brief(item):
                        return {key: str(item.get(key) or '')[:size] for key, size in
                                [('id', 128), ('qq', 32), ('name', 80), ('text', 1000), ('time', 80)]}
                    thread = brief(parent)
                    thread['replies'] = [brief(item) for item in parent.get('replies', [])[-8:] if not item.get('private')]
                    payload = dict(post={key: post.get(key, '') for key in ('id', 'qq', 'text', 'time', 'url')},
                                   parent=thread, comment=brief(comment))
                    db.execute("INSERT OR IGNORE INTO comment_receipts VALUES (?,?,?,?,?,'','',?)",
                               (account, self.key(post['id'], parent, comment), post['id'], state,
                                json.dumps(payload, ensure_ascii=False), time.time()))
            db.execute("INSERT OR REPLACE INTO scanned_posts VALUES (?,?,?)", (account, post['id'], time.time()))

    def claim(self, account, limit):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT key,payload FROM comment_receipts WHERE account=? AND state='new' ORDER BY updated_at LIMIT ?",
                              (account, limit)).fetchall()
            for row in rows:
                db.execute("UPDATE comment_receipts SET state='judging',updated_at=? WHERE account=? AND key=? AND state='new'",
                           (time.time(), account, row['key']))
            return [(row['key'], json.loads(row['payload'])) for row in rows]

    def finish(self, account, key, state, reply='', result=''):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE comment_receipts SET state=?,reply=?,result=?,updated_at=? WHERE account=? AND key=?",
                       (state, reply, result, time.time(), account, key))

    def counts(self):
        with closing(self.connect()) as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM comment_receipts GROUP BY state")}
