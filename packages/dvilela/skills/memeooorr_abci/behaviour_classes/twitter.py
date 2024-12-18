# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024 David Vilela Freire
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This package contains round behaviours of MemeooorrAbciApp."""

import json
import re
from datetime import datetime
from typing import Dict, Generator, List, Optional, Tuple, Type, Union

from twitter_text import parse_tweet  # type: ignore

from packages.dvilela.skills.memeooorr_abci.behaviour_classes.base import (
    MemeooorrBaseBehaviour,
)
from packages.dvilela.skills.memeooorr_abci.prompts import (
    DEFAULT_TWEET_PROMPT,
    ENGAGEMENT_TWEET_PROMPT,
)
from packages.dvilela.skills.memeooorr_abci.rounds import (
    ActionTweetPayload,
    ActionTweetRound,
    CollectFeedbackPayload,
    CollectFeedbackRound,
    EngagePayload,
    EngageRound,
    Event,
    PostAnnouncementRound,
    PostTweetPayload,
    PostTweetRound,
)
from packages.valory.skills.abstract_round_abci.base import AbstractRound


MAX_TWEET_CHARS = 280
JSON_RESPONSE_REGEXES = [r"json({.*})", r"\`\`\`json(.*)\`\`\`"]
MAX_TWEET_PREPARATIONS_RETRIES = 3


def parse_json_from_llm(response: str) -> Optional[Union[Dict, List]]:
    """Parse JSON from LLM response"""
    for JSON_RESPONSE_REGEX in JSON_RESPONSE_REGEXES:
        match = re.search(JSON_RESPONSE_REGEX, response, re.DOTALL)
        if not match:
            continue

        try:
            loaded_response = json.loads(match.groups()[0])
            return loaded_response
        except json.JSONDecodeError:
            continue
    return None


def is_tweet_valid(tweet: str) -> bool:
    """Checks a tweet length"""
    return parse_tweet(tweet).asdict()["weightedLength"] <= MAX_TWEET_CHARS


class PostTweetBehaviour(MemeooorrBaseBehaviour):  # pylint: disable=too-many-ancestors
    """PostTweetBehaviour"""

    matching_round: Type[AbstractRound] = PostTweetRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            latest_tweet = yield from self.decide_post_tweet()

            payload = PostTweetPayload(
                sender=self.context.agent_address,
                latest_tweet=json.dumps(latest_tweet, sort_keys=True),
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def decide_post_tweet(  # pylint: disable=too-many-locals
        self,
    ) -> Generator[None, None, Optional[Dict]]:
        """Post a tweet"""

        pending_tweet = self.synchronized_data.pending_tweet

        # If there is a pending tweet, we send it
        if pending_tweet:
            self.context.logger.info("Sending a pending tweet...")
            latest_tweet = yield from self.post_tweet(tweet=[pending_tweet])
            return latest_tweet

        # If we have not posted before, we prepare and send a new tweet
        if self.synchronized_data.latest_tweet == {}:
            self.context.logger.info("Creating a new tweet for the first time...")
            latest_tweet = yield from self.post_tweet(tweet=None)
            return latest_tweet

        # Calculate time since the latest tweet
        latest_tweet_time = datetime.fromtimestamp(
            self.synchronized_data.latest_tweet["timestamp"]
        )
        now = datetime.fromtimestamp(self.get_sync_timestamp())
        hours_since_last_tweet = (now - latest_tweet_time).total_seconds() / 3600

        # Too much time has passed since last tweet without feedback, tweet again
        if hours_since_last_tweet >= self.params.feedback_period_max_hours:
            self.context.logger.info(
                "Too much time has passed since last tweet. Creating a new tweet..."
            )
            latest_tweet = yield from self.post_tweet(tweet=None)
            return latest_tweet

        # If we have posted befored, but not enough time has passed to collect feedback, we wait
        if hours_since_last_tweet < self.params.feedback_period_min_hours:
            self.context.logger.info(
                f"{hours_since_last_tweet:.1f} hours have passed since last tweet. Awaiting for the feedback period..."
            )
            return {"wait": True}

        # Enough time has passed, collect feedback
        if self.synchronized_data.feedback is None:
            self.context.logger.info(
                "Feedback period has finished. Collecting feedback..."
            )
            return {}

        # Not enough feedback, prepare and send a new tweet
        self.context.logger.info("Feedback was not enough. Creating a new tweet...")
        latest_tweet = yield from self.post_tweet(tweet=None)
        return latest_tweet

    def prepare_tweet(self) -> Generator[None, None, Optional[str]]:
        """Prepare a tweet"""

        self.context.logger.info("Preparing tweet...")
        persona = self.get_persona()

        retries = 0

        while retries < MAX_TWEET_PREPARATIONS_RETRIES:
            llm_response = yield from self._call_genai(
                prompt=DEFAULT_TWEET_PROMPT.format(persona=persona)
            )
            self.context.logger.info(f"LLM response: {llm_response}")

            if llm_response is None:
                self.context.logger.error("Error getting a response from the LLM.")
                retries += 1
                continue

            if not is_tweet_valid(llm_response):
                self.context.logger.error("The tweet is too long.")
                retries += 1
                continue

            self.context.logger.info("Tweet is OK!")
            tweet = llm_response
            return tweet

        self.context.logger.error("Max retries reached.")
        return None

    def post_tweet(
        self, tweet: Optional[List] = None, store: bool = True
    ) -> Generator[None, None, Optional[Dict]]:
        """Post a tweet"""
        # Prepare a tweet if needed
        if tweet is None:
            new_tweet = yield from self.prepare_tweet()

            # We fail to prepare the tweet
            if not new_tweet:
                return None

            tweet = [new_tweet]

        # Post the tweet
        tweet_ids = yield from self._call_twikit(
            method="post",
            tweets=[{"text": t} for t in tweet],
        )

        if not tweet_ids:
            self.context.logger.error("Failed posting to Twitter.")
            return None

        latest_tweet = {
            "tweet_id": tweet_ids[0],
            "text": tweet,
            "timestamp": self.get_sync_timestamp(),
        }

        # Write latest tweet to the database
        if store:
            yield from self._write_kv(
                {"latest_tweet": json.dumps(latest_tweet, sort_keys=True)}
            )
            self.context.logger.info("Wrote latest tweet to db")

        return latest_tweet


class PostAnnouncementBehaviour(
    PostTweetBehaviour
):  # pylint: disable=too-many-ancestors
    """PostAnnouncementBehaviour"""

    matching_round: Type[AbstractRound] = PostAnnouncementRound


class CollectFeedbackBehaviour(
    MemeooorrBaseBehaviour
):  # pylint: disable=too-many-ancestors
    """CollectFeedbackBehaviour"""

    matching_round: Type[AbstractRound] = CollectFeedbackRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            feedback = yield from self.get_feedback()

            payload = CollectFeedbackPayload(
                sender=self.context.agent_address,
                feedback=json.dumps(feedback, sort_keys=True),
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_feedback(self) -> Generator[None, None, Optional[List]]:
        """Get the responses"""

        # Search new replies
        latest_tweet = self.synchronized_data.latest_tweet
        query = f"conversation_id:{latest_tweet['tweet_id']}"
        feedback = yield from self._call_twikit(method="search", query=query, count=100)

        if feedback is None:
            self.context.logger.error(
                "Could not retrieve any replies due to an API error"
            )
            return None

        if not feedback:
            self.context.logger.error("No tweets match the query")
            return []

        self.context.logger.info(f"Retrieved {len(feedback)} replies")

        # Sort tweets by popularity using a weighted sum (views + quotes + retweets)
        feedback = list(
            sorted(
                feedback,
                key=lambda t: int(t["view_count"])
                + 3 * int(t["retweet_count"])
                + 5 * int(t["quote_count"]),
                reverse=True,
            )
        )

        # Keep only the most relevant tweet to avoid sending too many tokens to the LLM
        feedback = feedback[:10]

        return feedback


class ActionTweetBehaviour(PostTweetBehaviour):  # pylint: disable=too-many-ancestors
    """ActionTweetBehaviour"""

    matching_round: Type[AbstractRound] = ActionTweetRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            event = yield from self.get_event()

            payload = ActionTweetPayload(
                sender=self.context.agent_address,
                event=event,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self) -> Generator[None, None, str]:
        """Get the next event"""
        pending_tweet = self.synchronized_data.token_action["tweet"]
        self.context.logger.info("Sending the action tweet...")
        latest_tweet = yield from self.post_tweet(tweet=[pending_tweet], store=False)
        return Event.DONE.value if latest_tweet else Event.ERROR.value


class EngageBehaviour(PostTweetBehaviour):  # pylint: disable=too-many-ancestors
    """EngageBehaviour"""

    matching_round: Type[AbstractRound] = EngageRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            event = yield from self.get_event()

            payload = EngagePayload(
                sender=self.context.agent_address,
                event=event,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self) -> Generator[None, None, str]:
        """Get the next event"""

        # Get other memeooorr handles
        agent_handles = yield from self.get_memeooorr_handles_from_chain()

        # Load previously responded tweets
        db_data = yield from self._read_kv(keys=("responded_tweet_ids",))

        if db_data is None:
            self.context.logger.error("Error while loading the database")
            responded_tweet_ids = []
        else:
            responded_tweet_ids = json.loads(db_data["responded_tweet_ids"] or "[]")

        # Get their latest tweet
        tweet_id_to_response = {}
        for agent_handle in agent_handles:
            # By default only 1 tweet is retrieved (the latest one)
            latest_tweets = yield from self._call_twikit(
                method="get_user_tweets",
                twitter_handle=agent_handle,
            )
            if not latest_tweets:
                self.context.logger.info("Couldn't get any tweets")
                continue
            tweet_id = latest_tweets[0]["id"]

            # Only respond to not previously responded tweets
            if tweet_id in responded_tweet_ids:
                self.context.logger.info("Tweet was already responded")
                continue

            # Like the tweet right away
            yield from self.like_tweet(tweet_id)
            tweet_id_to_response[tweet_id] = latest_tweets[0]["text"]

        if not tweet_id_to_response:
            self.context.logger.info("There are no tweets from other agents yet")
            return Event.DONE.value

        # Build and post responses
        event, new_responded_tweet_ids = yield from self.respond_tweet(
            tweet_id_to_response
        )

        if event == Event.DONE.value:
            responded_tweet_ids.extend(new_responded_tweet_ids)
            # Write latest responded tweets to the database
            yield from self._write_kv(
                {"responded_tweet_ids": json.dumps(responded_tweet_ids, sort_keys=True)}
            )
            self.context.logger.info("Wrote latest tweet to db")

        return event

    def respond_tweet(
        self, tweet_id_to_response: Dict
    ) -> Generator[None, None, Tuple[str, List]]:
        """Respond to tweets"""

        self.context.logger.info("Preparing tweet responses...")
        new_responded_tweet_ids: List[str] = []
        persona = self.get_persona()
        tweets = "\n\n".join(
            [
                f"tweet_id: {t_id}\ntweet: {t}"
                for t_id, t in tweet_id_to_response.items()
            ]
        )

        llm_response = yield from self._call_genai(
            prompt=ENGAGEMENT_TWEET_PROMPT.format(persona=persona, tweets=tweets)
        )
        self.context.logger.info(f"LLM response: {llm_response}")

        if llm_response is None:
            self.context.logger.error("Error getting a response from the LLM.")
            return Event.ERROR.value, new_responded_tweet_ids

        json_response = parse_json_from_llm(llm_response)

        if not json_response:
            return Event.ERROR.value, new_responded_tweet_ids

        for response in json_response:
            self.context.logger.info(f"Processing response: {response}")

            if not is_tweet_valid(llm_response):
                self.context.logger.error("The tweet is too long.")
                continue

            # Post the tweet
            tweet_ids = yield from self._call_twikit(
                method="post",
                tweets=[{"text": response["text"], "reply_to": response["tweet_id"]}],
            )
            if tweet_ids:
                new_responded_tweet_ids.append(response["tweet_id"])

        return Event.DONE.value, new_responded_tweet_ids

    def like_tweet(self, tweet_id: str) -> Generator[None, None, bool]:
        """Like a tweet"""
        self.context.logger.info(f"Liking tweet with ID: {tweet_id}")
        response = yield from self._call_twikit(method="like_tweet", tweet_id=tweet_id)
        return response["success"]
