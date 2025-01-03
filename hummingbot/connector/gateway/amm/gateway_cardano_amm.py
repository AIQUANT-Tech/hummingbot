import asyncio
import copy
import itertools as it
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union, cast

from hummingbot.client.settings import GatewayConnectionSetting
from hummingbot.connector.client_order_tracker import ClientOrderTracker
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.gateway.gateway_in_flight_order import GatewayInFlightOrder
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.event.events import (
    OrderType,
   TradeType
)
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.logger import HummingbotLogger
from hummingbot.core.data_type.cancellation_result import CancellationResult


if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter
    
s_logger = None
s_decimal_0 = Decimal("0")

class GatewayCardanoAMM(ConnectorBase):
    POLL_INTERVAL = 1.0
    UPDATE_BALANCE_INTERVAL = 30.0

    _connector_name: str
    _name: str
    _chain: str
    _network: str
    _wallet_address: str
    _trading_pairs: List[str]
    _trading_required: bool
    _chain_info: Dict[str, Any]
    _native_currency: str
    _last_balance_poll_timestamp: float
    _tokens: Set[str]
    _amount_quantum_dict: Dict[str, Decimal]
    _last_poll_timestamp: float
    def __init__(self,
                client_config_map: "ClientConfigAdapter",
                 connector_name: str,
                 chain: str,
                 network: str,
                 address: str,
                 additional_spenders: List[str] = [],
                 trading_pairs: List[str] = [],
                 trading_required: bool = True
                 ):
        self._connector_name = connector_name
        self._chain = chain
        self._network = network
        self._wallet_address = address
        self._trading_pairs = trading_pairs
        self._trading_required = trading_required
        self._native_currency = None
        self._name = "_".join([connector_name, chain, network])
        self._last_poll_timestamp = 0.0
        self._chain_info = {}
        self._last_balance_poll_timestamp = time.time()
        self._tokens = set()
        [self._tokens.update(set(trading_pair.split("_")[0].split("-"))) for trading_pair in trading_pairs]
        self._order_tracker: ClientOrderTracker = ClientOrderTracker(connector=self, lost_order_count_limit=10)
        self._amount_quantum_dict = {}
        safe_ensure_future(self.load_token_data())
        super().__init__(client_config_map)

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global s_logger
        if s_logger is None:
            s_logger = logging.getLogger(cls.__name__)
        return cast(HummingbotLogger, s_logger)

    @property
    def connector_name(self):
        """
        This returns the name of connector/protocol to be connected to on Gateway.
        """
        return self._connector_name
    
    @property
    def chain(self) -> str:
        return self._chain
    
    @property
    def network(self) -> str:
        return self._network
    
    @property
    def name(self):
        return self._name
    
    @property
    def address(self) -> str:
        return self._wallet_address
    
    async def all_trading_pairs(self) -> List[str]:
        """
        Calls the tokens endpoint on Gateway.
        """
        try:
            tokens = await GatewayHttpClient.get_instance().get_tokens(self._chain, self._network)
            token_symbols = [t["symbol"] for t in tokens["tokens"]]
            trading_pairs = []
            for base, quote in it.permutations(token_symbols, 2):
                trading_pairs.append(f"{base}-{quote}")
            return trading_pairs
        except Exception:
            return []
        
    @property
    def approval_orders(self) -> List[GatewayInFlightOrder]:
        return [
            approval_order
            for approval_order in self._order_tracker.active_orders.values()
            if approval_order.is_approval_request
        ]

    @property
    def amm_orders(self) -> List[GatewayInFlightOrder]:
        return [
            in_flight_order
            for in_flight_order in self._order_tracker.active_orders.values()
            if in_flight_order.is_open
        ]
    
    @property
    def canceling_orders(self) -> List[GatewayInFlightOrder]:
        return [
            cancel_order
            for cancel_order in self.amm_orders
            if cancel_order.is_pending_cancel_confirmation
        ]
    
    @property
    def limit_orders(self) -> List[LimitOrder]:
        return [
            in_flight_order.to_limit_order()
            for in_flight_order in self.amm_orders
        ]
    
    @property
    def in_flight_orders(self) -> Dict[str, GatewayInFlightOrder]:
        return self._order_tracker.active_orders
    
    @property
    def tracking_states(self) -> Dict[str, Any]:
        """
        Returns a dictionary associating current active orders client id to their JSON representation
        """
        return {
            key: value.to_json()
            for key, value in self.in_flight_orders.items()
        }
    
    def restore_tracking_states(self, saved_states: Dict[str, any]):
        """
        *required
        Updates inflight order statuses from API results
        This is used by the MarketsRecorder class to orchestrate market classes at a higher level.
        """
        self._order_tracker._in_flight_orders.update({
            key: GatewayInFlightOrder.from_json(value)
            for key, value in saved_states.items()
        })
    
    @staticmethod
    def create_market_order_id(side: TradeType, trading_pair: str) -> str:
        return f"{side.name.lower()}-{trading_pair}"
    
    async def load_token_data(self):
        tokens = await GatewayHttpClient.get_instance().get_tokens(self.chain, self.network)
        for t in tokens.get("tokens", []):
            self._amount_quantum_dict[t["symbol"]] = Decimal(str(10 ** -t["decimals"]))

    async def get_chain_info(self):
        """
        Calls the base endpoint of the connector on Gateway to know basic info about chain being used.
        """
        try:
            self._chain_info = await self._get_gateway_instance().get_network_status(
                chain=self.chain, network=self.network
            )
            
            if type(self._chain_info) is not list:
                self._native_currency = self._chain_info.get("nativeCurrency", "ADA")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().network(
                "Error fetching chain info",
                exc_info=True,
                app_warning_msg=str(e)
            ) 
    
        
    async def update_balances(self, on_interval: bool = False):
        """
        Calls Cardano API to update total and available balances.
        """
        if self._native_currency is None:
            await self.get_chain_info()
        connector_tokens = GatewayConnectionSetting.get_connector_spec_from_market_name(self._name).get("tokens", "").split(",")
        last_tick = self._last_balance_poll_timestamp
        current_tick = self.current_timestamp
        if not on_interval or (current_tick - last_tick) > self.UPDATE_BALANCE_INTERVAL:
            self._last_balance_poll_timestamp = current_tick
            local_asset_names = set(self._account_balances.keys())
            remote_asset_names = set()
            token_list = list(self._tokens) + [self._native_currency] + connector_tokens
            resp_json: Dict[str, Any] = await self._get_gateway_instance().get_balances(
                chain=self.chain,
                network=self.network,
                address=self.address,
                token_symbols=token_list
            )
            for token, bal in resp_json["balances"].items():
                self._account_available_balances[token] = Decimal(str(bal))
                self._account_balances[token] = Decimal(str(bal))
                remote_asset_names.add(token)
            asset_names_to_remove = local_asset_names.difference(remote_asset_names)
            for asset_name in asset_names_to_remove:
                del self._account_available_balances[asset_name]
                del self._account_balances[asset_name]
            self._in_flight_orders_snapshot = {k: copy.copy(v) for k, v in self._order_tracker.all_orders.items()}
            self._in_flight_orders_snapshot_timestamp = self.current_timestamp
        
    async def _update_balances(self):
        """
        This is called by UserBalances.
        """
        await self.update_balances()
    
    
    def buy(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Buys an amount of base token for a given price (or cheaper).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The maximum price for the order.
        :return: A newly created order id (internal).
        """
        return self.place_order(True, trading_pair, amount, price)
    
    def sell(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Sells an amount of base token for a given price (or at a higher price).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The minimum price for the order.
        :return: A newly created order id (internal).
        """
        return self.place_order(False, trading_pair, amount, price)
    
    
    def place_order(self, is_buy: bool, trading_pair: str, amount: Decimal, price: Decimal, **request_args) -> str:
        """
        Places an order.
        :param is_buy: True for buy order
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param price: The minimum price for the order.
        :return: A newly created order id (internal).
        """
        side: TradeType = TradeType.BUY if is_buy else TradeType.SELL
        order_id: str = self.create_market_order_id(side, trading_pair)
        safe_ensure_future(self._create_order(side, order_id, trading_pair, amount, price, **request_args))
        return order_id
    

    async def _create_order(
            self,
            trade_type: TradeType,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            price: Decimal,
            **request_args
    ):
        """
        Calls buy or sell API end point to place an order, starts tracking the order and triggers relevant order events.
        :param trade_type: BUY or SELL
        :param order_id: Internal order id (also called client_order_id)
        :param trading_pair: The market to place order
        :param amount: The order amount (in base token value)
        :param price: The order price
        """
        pool_id = None

        amount = self.quantize_order_amount(trading_pair, amount)
        price = self.quantize_order_price(trading_pair, price)
        try:
            trading_pair, pool_id = trading_pair.split("_")
        except Exception:
            pass
        base, quote = trading_pair.split("-")
        self.start_tracking_order(order_id=order_id,
                                  trading_pair=trading_pair,
                                  trade_type=trade_type,
                                  price=price,
                                  amount=amount)
        try:
            order_result: Dict[str, Any] = await self._get_gateway_instance().amm_trade(
                self.chain,
                self.network,
                self.connector_name,
                self.address,
                base,
                quote,
                trade_type,
                amount,
                price,
                pool_id=pool_id,
                **request_args
            )
            transaction_hash: Optional[str] = order_result.get("txHash")
            if transaction_hash is not None and transaction_hash != "":
                gas_cost: Decimal = Decimal(order_result.get("gasCost"))
                gas_price_token: str = order_result.get("gasPriceToken")
                self.network_transaction_fee = TokenAmount(gas_price_token, gas_cost)

                order_update: OrderUpdate = OrderUpdate(
                    client_order_id=order_id,
                    exchange_order_id=transaction_hash,
                    trading_pair=trading_pair,
                    update_timestamp=self.current_timestamp,
                    new_state=OrderState.OPEN,  # Assume that the transaction has been successfully mined.
                    misc_updates={
                        "nonce": order_result.get("nonce"),
                        "gas_price": Decimal(order_result.get("gasPrice")),
                        "gas_limit": int(order_result.get("gasLimit")),
                        "gas_cost": Decimal(order_result.get("gasCost")),
                        "gas_price_token": order_result.get("gasPriceToken"),
                        "fee_asset": self._native_currency
                    }
                )
                self._order_tracker.process_order_update(order_update)
            else:
                raise ValueError

        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                f"Error submitting {trade_type.name} swap order to {self.connector_name} on {self.network} for "
                f"{amount} {trading_pair} "
                f"{price}.",
                exc_info=True
            )
            order_update: OrderUpdate = OrderUpdate(
                client_order_id=order_id,
                trading_pair=trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=OrderState.FAILED
            )
            self._order_tracker.process_order_update(order_update)

    
    def start_tracking_order(self,
                             order_id: str,
                             exchange_order_id: Optional[str] = None,
                             trading_pair: str = "",
                             trade_type: TradeType = TradeType.BUY,
                             price: Decimal = s_decimal_0,
                             amount: Decimal = s_decimal_0,
                             gas_price: Decimal = s_decimal_0,
                             is_approval: bool = False):
        """
        Starts tracking an order by simply adding it into _in_flight_orders dictionary in ClientOrderTracker.
        """
        self._order_tracker.start_tracking_order(
            GatewayInFlightOrder(
                client_order_id=order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=trading_pair,
                order_type=OrderType.LIMIT,
                trade_type=trade_type,
                price=price,
                amount=amount,
                gas_price=gas_price,
                creation_timestamp=self.current_timestamp,
                initial_state=OrderState.PENDING_APPROVAL if is_approval else OrderState.PENDING_CREATE
            )
        )

    @property
    def ready(self):
        return all(self.status_dict.values())
    
    @property
    def status_dict(self) -> Dict[str, bool]:
        return {
            "account_balance": len(self._account_balances) > 0 if self._trading_required else True,
            "native_currency": self._native_currency is not None,
        }
    
    async def update_order_status(self, tracked_orders: List[GatewayInFlightOrder]):
        """
        Calls REST API to get status update for each in-flight amm orders.
        """
        if len(tracked_orders) < 1:
            return

        # split canceled and non-canceled orders
        tx_hash_list: List[str] = await safe_gather(
            *[tracked_order.get_exchange_order_id() for tracked_order in tracked_orders]
        )
        self.logger().debug(
            "Polling for order status updates of %d orders.",
            len(tracked_orders)
        )
        update_results: List[Union[Dict[str, Any], Exception]] = await safe_gather(*[
            self._get_gateway_instance().get_transaction_status(
                self.chain,
                self.network,
                tx_hash
            )
            for tx_hash in tx_hash_list
        ], return_exceptions=True)
        for tracked_order, tx_details in zip(tracked_orders, update_results):
            if isinstance(tx_details, Exception):
                self.logger().error(f"An error occurred fetching transaction status of {tracked_order.client_order_id}")
                continue
            if "txHash" not in tx_details:
                self.logger().error(f"No txHash field for transaction status of {tracked_order.client_order_id}: "
                                    f"{tx_details}.")
                continue
            tx_status: int = tx_details["txStatus"]
            tx_receipt: Optional[Dict[str, Any]] = tx_details["txReceipt"]
            if tx_status == 1 and (tx_receipt is not None and tx_receipt.get("status") == 1):
                gas_used: int = tx_receipt["gasUsed"]
                gas_price: Decimal = tracked_order.gas_price
                fee: Decimal = Decimal(str(gas_used)) * Decimal(str(gas_price)) / Decimal(str(1e9))

                self.processs_trade_fill_update(tracked_order=tracked_order, fee=fee)

                order_update: OrderUpdate = OrderUpdate(
                    client_order_id=tracked_order.client_order_id,
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=self.current_timestamp,
                    new_state=OrderState.FILLED,
                )
                self._order_tracker.process_order_update(order_update)
            elif tx_status in [0, 2, 3]:
                # 0: in the mempool but we dont have data to guess its status
                # 2: in the mempool and likely to succeed
                # 3: in the mempool and likely to fail
                pass

            elif tx_status == -1 or (tx_receipt is not None and tx_receipt.get("status") == 0):
                self.logger().network(
                    f"Error fetching transaction status for the order {tracked_order.client_order_id}: {tx_details}.",
                    app_warning_msg=f"Failed to fetch transaction status for the order {tracked_order.client_order_id}."
                )
                await self._order_tracker.process_order_not_found(tracked_order.client_order_id)

    
    async def start_network(self):
        if self._trading_required:
            self._status_polling_task = safe_ensure_future(self._status_polling_loop())
        self._get_chain_info_task = safe_ensure_future(self.get_chain_info())

    async def stop_network(self):
        if self._status_polling_task is not None:
            self._status_polling_task.cancel()
            self._status_polling_task = None
        if self._get_chain_info_task is not None:
            self._get_chain_info_task.cancel()
            self._get_chain_info_task = None

    async def check_network(self) -> NetworkStatus:
        try:
            if await self._get_gateway_instance().ping_gateway():
                return NetworkStatus.CONNECTED
        except asyncio.CancelledError:
            raise
        except Exception:
            return NetworkStatus.NOT_CONNECTED
        return NetworkStatus.NOT_CONNECTED

    def tick(self, timestamp: float):
        """
        Is called automatically by the clock for each clock's tick (1 second by default).
        It checks if status polling task is due for execution.
        """
        if time.time() - self._last_poll_timestamp > self.POLL_INTERVAL:
            if self._poll_notifier is not None and not self._poll_notifier.is_set():
                self._poll_notifier.set() 
    
    async def _status_polling_loop(self):
        await self.update_balances(on_interval=False)
        while True:
            try:
                self._poll_notifier = asyncio.Event()
                await self._poll_notifier.wait()
                await safe_gather(
                    self.update_balances(on_interval=True),
                    self.update_order_status(self.amm_orders)
                )
                self._last_poll_timestamp = self.current_timestamp
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(str(e), exc_info=True)
    
    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
        """
        This is intentionally left blank, because cancellation is expensive on blockchains. It's not worth it for
        Hummingbot to force cancel all orders whenever Hummingbot quits.
        """
        return []
    
    def _get_gateway_instance(self) -> GatewayHttpClient:
        gateway_instance = GatewayHttpClient.get_instance(self._client_config)
        return gateway_instance
