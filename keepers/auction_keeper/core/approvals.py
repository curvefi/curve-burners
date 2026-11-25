"""Executor allowances, synced once and in batches.

An adapter's executor (the CoW vault relayer, ...) pulls a sold lot from the
auction, so the auction has to approve it for the lot's token. The grant is
permissionless (``sync_executor_approvals``) and goes from zero to infinity,
so a token needs it once for as long as the executor stays active.

``ApprovalSync`` does that once per token instead of before every order:
the first cycle reads the allowance of every lot the keeper may sell and
sends the missing grants ``BATCH`` tokens per transaction. A token that was
looked at is never looked at again by this process; a lot staged later is
new to it and joins the next batch. A token whose ``approve`` reverts is
found by halving the failing batch and is left out for good (``blocked``:
no order is published for it).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import Auction, Erc20

log = logging.getLogger(__name__)

# Tokens per sync_executor_approvals call (constants.MAX_COINS).
BATCH = 64


class ApprovalSync:
    def __init__(self, client: ChainClient, *, send: bool, dry_run: bool = False, workers: int = 4):
        self.client = client
        # False (offline mode): the missing grants are only reported.
        self.send = send
        self.dry_run = dry_run
        self.workers = workers
        # (auction, executor, token) looked at already, and the ones that cannot be approved.
        self._done: set[tuple[str, str, str]] = set()
        self._blocked: set[tuple[str, str, str]] = set()

    @staticmethod
    def _key(auction: str, executor: str, token: str) -> tuple[str, str, str]:
        return auction.lower(), executor.lower(), token.lower()

    def blocked(self, auction: str, executor: str, token: str) -> bool:
        """Whether the token's grant to the executor could not be made."""
        return self._key(auction, executor, token) in self._blocked

    def sync(self, auction: Auction, executor: str, amounts: Mapping[str, int]) -> int:
        """Grant ``executor`` the tokens of ``amounts`` (token -> the lot's
        amount) that this process has not looked at yet and whose allowance
        does not cover the lot. Returns how many tokens were granted."""
        fresh = [
            token
            for token in amounts
            if self._key(auction.address, executor, token) not in self._done
        ]
        if not fresh:
            return 0

        def missing(token: str) -> bool | None:
            try:
                allowance = Erc20(self.client, token).allowance(auction.address, executor)
            except Exception as exc:  # noqa: BLE001 - asked again next cycle
                log.warning("%s: allowance read failed: %s", token, exc)
                return None
            return allowance < amounts[token]

        workers = min(self.workers, len(fresh))
        if workers <= 1 or not getattr(self.client, "concurrent", False):
            answers = [missing(token) for token in fresh]
        else:
            with ThreadPoolExecutor(workers, thread_name_prefix="allowance") as pool:
                answers = list(pool.map(missing, fresh))
        todo = []
        for token, answer in zip(fresh, answers, strict=True):
            if answer:
                todo.append(token)
            elif answer is False:
                self._done.add(self._key(auction.address, executor, token))
        if not todo:
            return 0
        if not self.send or self.dry_run:
            log.warning(
                "%s: %s token(s) lack the allowance of executor %s (%s); "
                "run sync_executor_approvals(%s, [...]) before a fill: %s",
                auction.address,
                len(todo),
                executor,
                "dry run" if self.send else "offline mode",
                executor,
                ", ".join(todo),
            )
            self._done.update(self._key(auction.address, executor, token) for token in todo)
            return 0
        granted = 0
        for start in range(0, len(todo), BATCH):
            granted += self._grant(auction, executor, todo[start : start + BATCH])
        return granted

    def _grant(self, auction: Auction, executor: str, tokens: list[str]) -> int:
        try:
            self.client.send(
                auction.address, auction.sync_executor_approvals_calldata(executor, tokens)
            )
        except CallReverted as exc:
            if len(tokens) == 1:
                log.warning(
                    "%s: allowance for executor %s cannot be synced (%s); no orders for it",
                    tokens[0],
                    executor,
                    exc,
                )
                key = self._key(auction.address, executor, tokens[0])
                self._done.add(key)
                self._blocked.add(key)
                return 0
            # One token of the batch reverts: halve until it stands alone.
            half = len(tokens) // 2
            return self._grant(auction, executor, tokens[:half]) + self._grant(
                auction, executor, tokens[half:]
            )
        except Exception as exc:  # noqa: BLE001 - not included, a dead node: tried again next cycle
            log.warning("%s: allowance sync of %s token(s) failed: %s", executor, len(tokens), exc)
            return 0
        log.info(
            "%s: synced the allowance of %s token(s) to %s", auction.address, len(tokens), executor
        )
        self._done.update(self._key(auction.address, executor, token) for token in tokens)
        return len(tokens)


__all__ = ["BATCH", "ApprovalSync"]
