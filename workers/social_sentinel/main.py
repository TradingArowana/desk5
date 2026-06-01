"""
Sentinel main loop: orchestrates scrapers, bot detector, and DB persistence.
"""

import os
import sys
import json
import time
import yaml
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any

from workers.social_sentinel.nitter_scraper import NitterScraper
from workers.social_sentinel.twitter_bot_detector import TwitterBotDetector

logger = logging.getLogger(__name__)

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    source      TEXT,
    platform    TEXT,
    post_id     TEXT,
    author      TEXT,
    content     TEXT,
    url         TEXT,
    likes       INTEGER DEFAULT 0,
    retweets    INTEGER DEFAULT 0,
    replies     INTEGER DEFAULT 0,
    sentiment_score REAL,
    engagement_score  REAL,
    created_at  TEXT,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_social_posts_author ON social_posts(author);
CREATE INDEX IF NOT EXISTS idx_social_posts_platform ON social_posts(platform);
CREATE INDEX IF NOT EXISTS idx_social_posts_fetched ON social_posts(fetched_at);
"""


def get_db_connection(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str):
    conn = get_db_connection(db_path)
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def load_config(config_path: str = "config/sentinel.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def insert_tweets(conn: sqlite3.Connection, tweets: List[Dict[str, Any]]):
    cursor = conn.cursor()
    for t in tweets:
        cursor.execute(
            """
            INSERT OR IGNORE INTO social_posts
            (timestamp, source, platform, post_id, author, content, url,
             likes, retweets, replies, sentiment_score, engagement_score,
             created_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                t.get("source", "twitter"),
                t.get("platform", "twitter"),
                t.get("post_id", ""),
                t.get("author", ""),
                t.get("text", ""),
                t.get("url", ""),
                t.get("like_count", 0),
                t.get("retweet_count", 0),
                t.get("reply_count", 0),
                t.get("sentiment_score"),
                t.get("engagement_score"),
                t.get("created_at"),
                t.get("fetched_at", datetime.now(timezone.utc).isoformat()),
            ),
        )
    conn.commit()


def run_sentinel(config: dict):
    sentinel_cfg = config.get("sentinel", {})
    db_path = sentinel_cfg.get("database", {}).get("path", "data_store/sentinel.db")
    init_db(db_path)

    twitter_cfg = sentinel_cfg.get("twitter", {})
    nitter_cfg = sentinel_cfg.get("nitter", {})
    bot_cfg = sentinel_cfg.get("bot_detection", {})

    detector = TwitterBotDetector(
        min_followers=bot_cfg.get("min_followers", 10),
        max_following_ratio=bot_cfg.get("max_following_ratio", 50.0),
        enable_llm_check=bot_cfg.get("enable_llm_check", False),
    )

    scraper_type = twitter_cfg.get("type", "api")
    tweets: List[Dict[str, Any]] = []

    if scraper_type == "nitter":
        logger.info("Using Nitter scraper…")
        scraper = NitterScraper(
            instances=nitter_cfg.get("instances"),
            timeout=nitter_cfg.get("request_timeout", 15),
            retries=nitter_cfg.get("retries", 3),
            max_results_per_run=nitter_cfg.get("max_results_per_run", 50),
        )
        for keyword in twitter_cfg.get("keywords", []):
            try:
                found = scraper.search_tweets(keyword)
                tweets.extend(found)
                logger.info("Keyword '%s' → %s tweets", keyword, len(found))
            except Exception as exc:
                logger.error("Error searching keyword '%s': %s", keyword, exc)
        for account in twitter_cfg.get("watch_accounts", []):
            try:
                found = scraper.get_user_tweets(account)
                tweets.extend(found)
                logger.info("Account '%s' → %s tweets", account, len(found))
            except Exception as exc:
                logger.error("Error fetching user '%s': %s", account, exc)
    else:
        logger.info("Using Twitter API scraper (legacy)…")
        # API scraper not implemented here; falls through with 0 tweets.
        pass

    # Bot filter
    clean = detector.filter_tweets(tweets)
    logger.info("Bot filter: %s -> %s tweets", len(tweets), len(clean))

    if clean:
        conn = get_db_connection(db_path)
        try:
            insert_tweets(conn, clean)
        finally:
            conn.close()

    return clean


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    run_sentinel(cfg)
