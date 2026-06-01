"""
Twitter bot / fake account detector.
Receives tweet-like dictionaries and returns is_bot flag.
"""

import re
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class TwitterBotDetector:
    def __init__(
        self,
        min_followers: int = 10,
        max_following_ratio: float = 50.0,
        enable_llm_check: bool = False,
    ):
        self.min_followers = min_followers
        self.max_following_ratio = max_following_ratio
        self.enable_llm_check = enable_llm_check

    def is_bot(self, tweet: Dict[str, Any]) -> bool:
        """Heuristic bot detection based on account metadata."""
        followers = tweet.get("followers_count", 0)
        following = tweet.get("following_count", 0)

        if followers < self.min_followers:
            # May still be a new genuine account, keep as non-bot
            pass

        if following > 0 and (following / max(followers, 1)) > self.max_following_ratio:
            return True

        text = tweet.get("text", "")
        if self._is_obvious_spam(text):
            return True

        return False

    def _is_obvious_spam(self, text: str) -> bool:
        spammy = [
            "free airdrop",
            "claim now",
            "send dm",
            "1000x guaranteed",
            "double your bitcoin",
            "click here to win",
        ]
        lower = text.lower()
        for phrase in spammy:
            if phrase in lower:
                return True

        # Emoji/fake engagement floods
        if text.count("🚀") > 5 or text.count("💰") > 5:
            return True

        # Excessive URL / mention ratio
        url_count = len(re.findall(r"http[s]?://", lower))
        mention_count = len(re.findall(r"@\w+", text))
        token_count = len(text.split())
        if token_count > 0 and (url_count + mention_count) / token_count > 0.5:
            return True

        return False

    def filter_tweets(self, tweets: list) -> list:
        return [t for t in tweets if not self.is_bot(t)]
