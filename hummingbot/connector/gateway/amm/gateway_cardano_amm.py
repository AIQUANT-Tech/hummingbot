import asyncio
from hummingbot.connector.connector_base import ConnectorBase
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union, cast
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.client.settings import GatewayConnectionSetting
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.connector.client_order_tracker import ClientOrderTracker
from decimal import Decimal
import copy
import time

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

class GatewayCardanoAMM(ConnectorBase):
    _connector_name: str
    _name: str
    _chain: str
    _network: str
    _wallet_address: str
    _trading_pairs: List[str]
    _trading_required: bool
    _native_currency: str
    _last_balance_poll_timestamp: float
    _tokens: Set[str]
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
        self._last_balance_poll_timestamp = time.time()
        self._tokens = set()
        [self._tokens.update(set(trading_pair.split("_")[0].split("-"))) for trading_pair in trading_pairs]
        self._order_tracker: ClientOrderTracker = ClientOrderTracker(connector=self, lost_order_count_limit=10)
        super().__init__(client_config_map)



    @property
    def name(self) -> str:
        return self._name

    @property
    def chain(self) -> str:
        return self._chain

    @property
    def network(self) -> str:
        return self._network

    @property
    def address(self) -> str:
        return self._wallet_address

    @property
    def trading_pairs(self) -> List[str]:
        return self._trading_pairs

    @property
    def trading_required(self) -> bool:
        return self._trading_required
    
    @property
    def connector_name(self):
        """
        This returns the name of connector/protocol to be connected to on Gateway.
        """
        return self._connector_name

    def check_network(self) -> bool:
        # Implement logic to check Cardano network connection status
        # Example: Use Blockfrost API or another Cardano SDK to verify connection
        self.logger().info("Checking Cardano network status...")
        return True  # Replace with actual implementation

    async def start(self):
        self.logger().info("Starting Cardano AMM connector...")
        self._is_ready = self.check_network()

    async def stop(self):
        self.logger().info("Stopping Cardano AMM connector...")
        self._is_ready = False

    async def place_order(
        self, trading_pair: str, amount: float, price: Optional[float] = None, is_buy: bool = True
    ) -> str:
        # Implement logic for placing an order on a Cardano AMM
        # Example: Use a smart contract interaction or API call to place the order
        self.logger().info(f"Placing {'buy' if is_buy else 'sell'} order: {amount} of {trading_pair} at {price}")
        # Placeholder for transaction ID
        return "transaction_id"

    async def cancel_order(self, order_id: str):
        # Implement logic for canceling an order on a Cardano AMM
        self.logger().info(f"Canceling order: {order_id}")

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        # Implement logic to fetch the status of an order from the blockchain
        self.logger().info(f"Fetching status for order: {order_id}")
        return {"status": "open"}  # Replace with actual implementation

    async def fetch_trading_pairs(self):
        # Implement logic to fetch trading pairs available on the Cardano AMM
        self.logger().info("Fetching trading pairs...")
        self._trading_pairs = ["ADA-MIN"]  # Replace with actual implementation

    def is_ready(self) -> bool:
        return self._is_ready

    async def get_balance(self) -> Dict[str, float]:
        # Implement logic to fetch wallet balance from the Cardano blockchain
        self.logger().info("Fetching wallet balance...")
        return {"ADA": 1000.0}  # Replace with actual implementation

    async def update_balances(self):
        # Refresh wallet balances
        self.logger().info("Updating wallet balances...")
        await self.get_balance()

    @property
    def limit_orders(self) -> List[LimitOrder]:
        return [
            in_flight_order.to_limit_order()
            for in_flight_order in self.amm_orders
        ]
    
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
        print("Update balance called")
        await self.update_balances()

    def _get_gateway_instance(self) -> GatewayHttpClient:
        gateway_instance = GatewayHttpClient.get_instance(self._client_config)
        return gateway_instance


    
