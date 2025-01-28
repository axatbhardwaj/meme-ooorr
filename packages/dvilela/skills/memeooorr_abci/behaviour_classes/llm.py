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
from typing import Generator, Optional, Tuple, Type

from packages.dvilela.skills.memeooorr_abci.behaviour_classes.base import (
    MemeooorrBaseBehaviour,
)
from packages.dvilela.skills.memeooorr_abci.prompts import (
    TOKEN_DECISION_PROMPT,
    build_token_action_schema,
)
from packages.dvilela.skills.memeooorr_abci.rounds import (
    ActionDecisionPayload,
    ActionDecisionRound,
    Event,
)
from packages.valory.skills.abstract_round_abci.base import AbstractRound


JSON_RESPONSE_REGEX = r"json.?({.*})"

# fmt: off
TOKEN_SUMMARY = (  # nosec
    """
    token nonce: {token_nonce}
    token address: {token_address}
    token name: {token_name}
    token symbol: {token_ticker}
    heart count: {heart_count}
    available actions: {available_actions}
    """
)
# fmt: on


class ActionDecisionBehaviour(
    MemeooorrBaseBehaviour
):  # pylint: disable=too-many-ancestors
    """ActionDecisionBehaviour"""

    matching_round: Type[AbstractRound] = ActionDecisionRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            (
                event,
                action,
                token_address,
                token_nonce,
                token_name,
                token_ticker,
                token_supply,
                amount,
                tweet,
                new_persona,
            ) = yield from self.get_event()

            payload = ActionDecisionPayload(
                sender=self.context.agent_address,
                event=event,
                action=action,
                token_address=token_address,
                token_nonce=token_nonce,
                token_name=token_name,
                token_ticker=token_ticker,
                token_supply=token_supply,
                amount=amount,
                tweet=tweet,
                new_persona=new_persona,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-statements
        self,
    ) -> Generator[
        None,
        None,
        Tuple[
            str,
            Optional[str],
            Optional[str],
            Optional[int],
            Optional[str],
            Optional[str],
            Optional[int],
            Optional[float],
            Optional[str],
            Optional[str],
        ],
    ]:
        """Get the next event"""

        meme_coins = "\n".join(
            TOKEN_SUMMARY.format(**meme_coin)
            for meme_coin in self.synchronized_data.meme_coins
            if meme_coin[
                "available_actions"
            ]  # Filter out tokens with no available actions
        )

        self.context.logger.info(f"Action options:\n{meme_coins}")

        valid_nonces = [c["token_nonce"] for c in self.synchronized_data.meme_coins]

        native_balances = yield from self.get_native_balance()
        safe_native_balance = native_balances["safe"]
        if not safe_native_balance:
            safe_native_balance = 0

        tweets = yield from self.get_tweets_from_db()
        latest_tweet = tweets[-1]["text"] if tweets else "No previous tweet"

        tweet_responses = "\n\n".join(
            [
                f"tweet: {t['text']}\nviews: {t['view_count']}\nquotes: {t['quote_count']}\nretweets{t['retweet_count']}"
                for t in self.synchronized_data.feedback
            ]
        )

        prompt_data = {
            "meme_coins": meme_coins,
            "latest_tweet": latest_tweet,
            "tweet_responses": tweet_responses,
            "balance": safe_native_balance,
            "ticker": "ETH" if self.params.home_chain_id == "BASE" else "CELO",
        }

        llm_response = yield from self._call_genai(
            prompt=TOKEN_DECISION_PROMPT.format(**prompt_data),
            schema=build_token_action_schema(),
        )
        self.context.logger.info(f"LLM response: {llm_response}")

        # We didnt get a response
        if llm_response is None:
            self.context.logger.error("Error getting a response from the LLM.")
            return (
                Event.WAIT.value,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )

        try:
            response = json.loads(llm_response)
            action_name = response.get("action_name", "none")
            action = response.get(action_name, {})

            token_name = action.get("token_name", None)
            token_ticker = action.get("token_ticker", None)
            token_supply = action.get("token_supply", None)
            amount = int(action.get("amount", 0))
            token_nonce = action.get("token_nonce", None)
            token_address = action.get("token_address", None)
            tweet = response.get("tweet", None)

            if isinstance(token_nonce, str) and token_nonce.isdigit():
                token_nonce = int(token_nonce)

            if isinstance(token_supply, str) and token_supply.isdigit():
                token_supply = int(token_supply)

            if action_name == "none":
                self.context.logger.info("Action is none")
                return (
                    Event.WAIT.value,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )

            if action_name in ["heart", "unleash"] and token_nonce not in valid_nonces:
                self.context.logger.info(
                    f"Token nonce {token_nonce} is not in valid_nonces={valid_nonces}"
                )
                return (
                    Event.WAIT.value,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )

            available_actions = []
            for t in self.synchronized_data.meme_coins:
                if t["token_nonce"] == token_nonce:
                    available_actions = t["available_actions"]
                    break

            if action_name != "summon" and action_name not in available_actions:
                self.context.logger.info(
                    f"Action [{action_name}] is not in available_actions={available_actions}"
                )
                return (
                    Event.WAIT.value,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )

            if action_name == "summon":
                token_name = response.get("token_name", None)
                token_ticker = response.get("token_ticker", None)
                token_supply = response.get("token_supply", None)
                if isinstance(token_supply, str) and token_supply.isdigit():
                    token_supply = int(token_supply)
            else:
                token_name = None
                token_ticker = None
                token_supply = None

            new_persona = response.get("new_persona", None)

            if not tweet:
                self.context.logger.info("Tweet is none")
                return (
                    Event.WAIT.value,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )

            # Fix amount if it is lower than the min required amount
            if action_name == "summon":
                amount = max(
                    amount,
                    int(0.01e18),  # 0.01 ETH
                )

            if action_name == "heart":
                amount = max(
                    amount,
                    1,  # 1 wei
                )

            self.context.logger.info("The LLM returned a valid response")
            if new_persona:
                yield from self._write_kv({"persona": new_persona})
            return (
                Event.DONE.value,
                action_name,
                token_address,
                token_nonce,
                token_name,
                token_ticker,
                token_supply,
                amount,
                tweet,
                new_persona,
            )

        # The response is not a valid json
        except (json.JSONDecodeError, ValueError) as e:
            self.context.logger.error(f"Error loading the LLM response: {e}")
            return (
                Event.WAIT.value,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
