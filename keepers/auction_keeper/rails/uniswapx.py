"""UniswapX publisher: v1 ``ExclusiveDutchOrder``s signed through the auction's
ERC-1271 router (``UniswapXAdapter``).

The order comes from ``UniswapXAdapter.order_for`` (fixed price at the live
quote, open to every filler, fill-or-kill), its deadline is shortened to the
keeper TTL (never past the lot end), and the pair ``(abi_encode(order),
adapter ++ abi_encode(order))`` is validated through ``isValidSignature`` with
the Permit2 witness digest, recorded and logged for fillers
(``reactor.execute(SignedOrder(order, sig))`` is permissionless).

KNOWN LIMITATION: the public Uniswap order service accepts only 65-byte
ECDSA signatures and only orders quoted by its own API, so the ERC-1271
``adapter ++ order`` signature cannot be posted to the public feed. With
``uniswapx.post: true`` the POST is attempted anyway and the backend's
rejection is reported in the log and the state details; the default
(``post: false``) skips the attempt. Either way the publisher is "build,
validate, log": orders are handed to fillers directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import (
    UNISWAPX_ORDER_ABI,
    AdapterConfig,
    Auction,
    LotView,
    UniswapXAdapterContract,
)
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.rails import AdapterPublisher, PublishResult
from keepers.auction_keeper.rails.common import ApiError, JsonClient, hex_bytes, random_salt

log = logging.getLogger(__name__)

ORDER_INFO_TYPE = (
    "OrderInfo(address reactor,address swapper,uint256 nonce,uint256 deadline,"
    "address additionalValidationContract,bytes additionalValidationData)"
)
DUTCH_OUTPUT_TYPE = (
    "DutchOutput(address token,uint256 startAmount,uint256 endAmount,address recipient)"
)
EXCLUSIVE_DUTCH_ORDER_TYPE = (
    "ExclusiveDutchOrder(OrderInfo info,uint256 decayStartTime,uint256 decayEndTime,"
    "address exclusiveFiller,uint256 exclusivityOverrideBps,address inputToken,"
    "uint256 inputStartAmount,uint256 inputEndAmount,DutchOutput[] outputs)"
)
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
ORDER_INFO_TYPE_HASH = keccak(text=ORDER_INFO_TYPE)
DUTCH_OUTPUT_TYPE_HASH = keccak(text=DUTCH_OUTPUT_TYPE)
ORDER_TYPE_HASH = keccak(text=EXCLUSIVE_DUTCH_ORDER_TYPE + DUTCH_OUTPUT_TYPE + ORDER_INFO_TYPE)
TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
PERMIT_WITNESS_TYPE_HASH = keccak(
    text=(
        "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
        "uint256 deadline,ExclusiveDutchOrder witness)"
        + DUTCH_OUTPUT_TYPE
        + EXCLUSIVE_DUTCH_ORDER_TYPE
        + ORDER_INFO_TYPE
        + TOKEN_PERMISSIONS_TYPE
    )
)


@dataclass(frozen=True)
class OrderInfo:
    reactor: str
    swapper: str
    nonce: int
    deadline: int
    additional_validation_contract: str
    additional_validation_data: bytes

    def as_tuple(self) -> tuple:
        return (
            self.reactor,
            self.swapper,
            self.nonce,
            self.deadline,
            self.additional_validation_contract,
            self.additional_validation_data,
        )


@dataclass(frozen=True)
class DutchOutput:
    token: str
    start_amount: int
    end_amount: int
    recipient: str

    def as_tuple(self) -> tuple:
        return (self.token, self.start_amount, self.end_amount, self.recipient)


@dataclass(frozen=True)
class ExclusiveDutchOrder:
    info: OrderInfo
    decay_start_time: int
    decay_end_time: int
    exclusive_filler: str
    exclusivity_override_bps: int
    input_token: str
    input_start_amount: int
    input_end_amount: int
    outputs: tuple[DutchOutput, ...]

    @classmethod
    def from_tuple(cls, values: tuple) -> ExclusiveDutchOrder:
        info, decay_start, decay_end, filler, override_bps, input_, outputs = values
        return cls(
            info=OrderInfo(
                to_checksum_address(info[0]),
                to_checksum_address(info[1]),
                info[2],
                info[3],
                to_checksum_address(info[4]),
                bytes(info[5]),
            ),
            decay_start_time=decay_start,
            decay_end_time=decay_end,
            exclusive_filler=to_checksum_address(filler),
            exclusivity_override_bps=override_bps,
            input_token=to_checksum_address(input_[0]),
            input_start_amount=input_[1],
            input_end_amount=input_[2],
            outputs=tuple(
                DutchOutput(to_checksum_address(o[0]), o[1], o[2], to_checksum_address(o[3]))
                for o in outputs
            ),
        )

    def as_tuple(self) -> tuple:
        return (
            self.info.as_tuple(),
            self.decay_start_time,
            self.decay_end_time,
            self.exclusive_filler,
            self.exclusivity_override_bps,
            (self.input_token, self.input_start_amount, self.input_end_amount),
            [o.as_tuple() for o in self.outputs],
        )

    def with_deadline(self, deadline: int) -> ExclusiveDutchOrder:
        return replace(self, info=replace(self.info, deadline=deadline))

    def encode(self) -> bytes:
        """``abi_encode(order)``: the reactor's ``SignedOrder.order`` and the bare signature payload."""
        return encode([UNISWAPX_ORDER_ABI], [self.as_tuple()])

    def signature(self, adapter: str) -> bytes:
        return bytes.fromhex(adapter[2:]) + self.encode()

    def hash(self) -> bytes:
        """ExclusiveDutchOrderLib.hash: the witness the reactor passes to Permit2."""
        info_hash = keccak(
            encode(
                ["bytes32", "address", "address", "uint256", "uint256", "address", "bytes32"],
                [
                    ORDER_INFO_TYPE_HASH,
                    *self.info.as_tuple()[:5],
                    keccak(self.info.additional_validation_data),
                ],
            )
        )
        output_hashes = b"".join(
            keccak(
                encode(
                    ["bytes32", "address", "uint256", "uint256", "address"],
                    [DUTCH_OUTPUT_TYPE_HASH, *o.as_tuple()],
                )
            )
            for o in self.outputs
        )
        return keccak(
            encode(
                ["bytes32"] * 2
                + ["uint256", "uint256", "address", "uint256", "address"]
                + ["uint256", "uint256", "bytes32"],
                [
                    ORDER_TYPE_HASH,
                    info_hash,
                    self.decay_start_time,
                    self.decay_end_time,
                    self.exclusive_filler,
                    self.exclusivity_override_bps,
                    self.input_token,
                    self.input_start_amount,
                    self.input_end_amount,
                    keccak(output_hashes),
                ],
            )
        )

    def permit_digest(self, domain_separator: bytes) -> bytes:
        """The Permit2 ``permitWitnessTransferFrom`` digest the swapper's
        ``isValidSignature`` receives: permit (input token, input endAmount),
        spender = the reactor, the order nonce and deadline, the order hash as witness."""
        permitted_hash = keccak(
            encode(
                ["bytes32", "address", "uint256"],
                [TOKEN_PERMISSIONS_TYPE_HASH, self.input_token, self.input_end_amount],
            )
        )
        struct_hash = keccak(
            encode(
                ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
                [
                    PERMIT_WITNESS_TYPE_HASH,
                    permitted_hash,
                    self.info.reactor,
                    self.info.nonce,
                    self.info.deadline,
                    self.hash(),
                ],
            )
        )
        return keccak(b"\x19\x01" + domain_separator + struct_hash)


class UniswapXPublisher(AdapterPublisher):
    version = "UniswapXAdapter"

    def __init__(
        self,
        client: ChainClient,
        adapter: AdapterConfig,
        network: NetworkConfig,
        *,
        http: Any = None,
    ):
        super().__init__(client, adapter, network)
        self.settings = network.uniswapx
        self.contract = UniswapXAdapterContract(client, adapter.adapter)
        self.reactor = self.contract.reactor()
        self.permit2 = self.contract.permit2()
        self.domain_separator = self.contract.domain_separator()
        self.http = http or JsonClient()
        if adapter.executor != self.permit2:
            log.warning(
                "UniswapXAdapter %s: registry executor %s differs from Permit2 %s",
                adapter.adapter,
                adapter.executor,
                self.permit2,
            )

    @classmethod
    def enabled(cls, network: NetworkConfig) -> bool:
        return network.uniswapx.enabled

    def publish(
        self, auction: Auction, lot: LotView, *, dry_run: bool = False
    ) -> PublishResult | None:
        if not lot.active:
            return None
        valid_to = self.valid_to(lot)
        if valid_to <= lot.timestamp:
            log.info("%s: lot window closes before an order could be valid", lot.token)
            return None
        # Permit2 burns the nonce on the fill: every (re)post needs a fresh one.
        try:
            built, _ = self.contract.order_for(auction.address, lot.token, random_salt(256))
        except CallReverted as exc:
            log.warning("%s: order_for rejected the lot: %s", lot.token, exc)
            return None
        order = ExclusiveDutchOrder.from_tuple(built).with_deadline(valid_to)
        signature = order.signature(self.address)
        reason = self.validate_signature(
            auction, order.permit_digest(self.domain_separator), signature
        )
        if reason is not None:
            log.warning("%s: UniswapX order rejected by the auction router: %s", lot.token, reason)
            return None
        if not self.ensure_allowance(auction, lot, self.permit2, dry_run=dry_run):
            return None

        order_hash = hex_bytes(order.hash())
        output = order.outputs[0]
        details: dict[str, Any] = {
            "orderHash": order_hash,
            "encodedOrder": hex_bytes(order.encode()),
            "signature": hex_bytes(signature),
            "nonce": str(order.info.nonce),
            "deadline": order.info.deadline,
        }
        summary = f"{lot.token} sells {order.input_end_amount} for >= {output.end_amount}"
        if dry_run:
            log.info("dry run: would publish UniswapX order %s (%s)", order_hash, summary)
        else:
            log.info(
                "UniswapX order %s built for fillers (%s): reactor.execute(SignedOrder(order=%s, sig=%s))",
                order_hash,
                summary,
                details["encodedOrder"],
                details["signature"],
            )
            if self.settings.post:
                details["orderService"] = self.try_post(order, signature)
        return PublishResult(
            uid=order_hash,
            valid_to=order.info.deadline,
            sell_amount=order.input_end_amount,
            buy_amount=output.end_amount,
            details=details,
        )

    def try_post(self, order: ExclusiveDutchOrder, signature: bytes) -> dict[str, Any]:
        """Attempt the order-service POST (``{encodedOrder, signature, chainId}``,
        the ``uniswapx-service`` body) and report what the backend said; the
        public service is expected to reject a non-ECDSA signature."""
        try:
            response = self.http.request(
                "POST",
                self.settings.order_service_url,
                json={
                    "encodedOrder": hex_bytes(order.encode()),
                    "signature": hex_bytes(signature),
                    "chainId": self.client.chain_id,
                },
            )
        except ApiError as exc:
            log.warning("UniswapX order service rejected the order (expected): %s", exc)
            return {"accepted": False, "status": exc.status, "body": exc.body}
        log.info("UniswapX order service accepted the order: %s", response)
        return {"accepted": True, "body": response}


__all__ = ["DutchOutput", "ExclusiveDutchOrder", "OrderInfo", "UniswapXPublisher"]
