import asyncio
from decimal import Decimal
from pydantic import Field # type: ignore
from hummingbot.client.config.config_data_types import BaseClientModel, ClientFieldData
from hummingbot.core.event.events import TradeType
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.client.settings import GatewayConnectionSetting

class AmmTradeConfig(BaseClientModel):
    script_file_name: str = Field(default="amm_price_trade.py")
    connector_chain_network: str = Field(
        "minswap_cardano_preprod",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Connector, chain, and network (e.g., minswap_cardano_preprod)"
        )
    )
    trading_pair: str = Field(
        "ADA-MIN",
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Trading pair (e.g., ADA-MIN)")
    )
    side: str = Field(
        "BUY",
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Trade side (BUY or SELL)")
    )
    order_amount: Decimal = Field(
        Decimal("10"),
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Order amount for the trade")
    )
    slippage_buffer: Decimal = Field(
        Decimal("0.01"),
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Slippage buffer (e.g., 0.01 for 1%)")
    )
    price_range: Decimal = Field(
        Decimal("50986605.19"),
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Price of ADA-MIN in which you want to buy.")
    )
class AmmTrade(ScriptStrategyBase):
    on_going_task = False
    trade_executed = False
    balance_data = None

    @classmethod
    def init_markets(cls, config: AmmTradeConfig):
        cls.markets = {config.connector_chain_network: {config.trading_pair}}

    def __init__(self, connectors, config: AmmTradeConfig):
        super().__init__(connectors)
        self.config = config
        self.balance_last_updated = None

    def on_tick(self):
        if not self.on_going_task and not self.trade_executed:
            self.on_going_task = True
            safe_ensure_future(self.async_task())

    async def async_task(self):
        base, quote = self.config.trading_pair.split("-")
        connector, chain, network = self.config.connector_chain_network.split("_")
        trade_type = TradeType.BUY if self.config.side.upper() == "BUY" else TradeType.SELL

        try:
            # Update balance every minute
            if self.balance_last_updated is None or (asyncio.get_event_loop().time() - self.balance_last_updated) > 60:
                gateway_connections_conf = GatewayConnectionSetting.load()
                wallet = next(
                    (w for w in gateway_connections_conf if w["chain"] == chain and w["connector"] == connector and w["network"] == network), None
                )
                if not wallet:
                    self.notify("No wallet configured for the specified chain, connector, and network.")
                    return

                address = wallet["wallet_address"]
                await self.update_balance(chain, network, address, base, quote)

            # Fetch current price
            self.logger().info(f"Fetching price for {self.config.trading_pair}...")
            price_data = await GatewayHttpClient.get_instance().get_price(
                chain, network, connector, base, quote, self.config.order_amount, trade_type
            )
            price = float(price_data.get("price", 0))
            self.logger().info(f"Current Price: {price}")

            # Apply slippage buffer
            adjusted_price = Decimal(price) * (1 + self.config.slippage_buffer) if trade_type == TradeType.BUY else Decimal(price) * (1 - self.config.slippage_buffer)

            self.logger().info(f"Adjusted Price with Slippage: {adjusted_price}")

            user_specified_price = self.config.price_range
            self.logger().info(f"User Specified Price: {user_specified_price}")

            # Check if the price matches user's specified price range
            if self.is_trade_allowed(price):
                # Execute trade
                self.logger().info(f"Executing trade...")
                order_amount = int(self.config.order_amount)
                trade_data = await GatewayHttpClient.get_instance().amm_trade(
                    chain, network, connector, wallet["wallet_address"], base, quote, trade_type, order_amount, Decimal(adjusted_price)
                )

                # Poll transaction status
                await self.poll_transaction(chain, network, trade_data.get("txHash"))
                await self.update_balance(chain, network, wallet["wallet_address"], base, quote)

                # Mark trade as executed
                self.trade_executed = True
                self.logger().info("Trade executed successfully. No further trades will be made.")
            else:
                self.logger().info("Trade conditions not met. Skipping execution.")
                # Wait for 5 minutes before checking again
                self.logger().info("Waiting for 1 minutes before next fetch...")
                await asyncio.sleep(60)  # 1 minutes sleep

        except Exception as e:
            self.logger().error(f"Error in async_task: {str(e)}")
        finally:
            self.on_going_task = False


    async def update_balance(self, chain, network, address, base, quote):
        try:
            self.logger().info(f"Fetching balances for {address}...")
            self.balance_data = await GatewayHttpClient.get_instance().get_balances(
                chain, network, address, [base, quote]
            )
            self.logger().info(f"Updated Balances: {self.balance_data['balances']}")
            self.balance_last_updated = asyncio.get_event_loop().time()
        except Exception as e:
            self.logger().error(f"Error fetching balances: {str(e)}")

    def is_trade_allowed(self, current_price):
        """ Check if the current price is within the user's specified price range """
        price_range = self.config.price_range
        if self.config.side.upper() == "BUY":
            return current_price <= price_range
        elif self.config.side.upper() == "SELL":
            return current_price >= price_range
        return False

    async def poll_transaction(self, chain, network, tx_hash):
        try:
            self.logger().info(f"Polling transaction status for {tx_hash}...")

            async def show_fetching_message():
                while True:
                    self.logger().info("Fetching transaction, please wait...")
                    await asyncio.sleep(5)

            ticker_task = asyncio.create_task(show_fetching_message())

            await asyncio.sleep(30)

            while True:
                try:
                    poll_data = await GatewayHttpClient.get_instance().get_transaction_status(
                        chain, network, tx_hash
                    )
                    status = poll_data.get("status")
                    if status == "confirmed":
                        self.logger().info(f"Transaction {tx_hash} confirmed successfully.")
                        ticker_task.cancel()
                        break
                    elif status == "pending":
                        self.logger().info(f"Transaction {tx_hash} is still pending...")
                    else:
                        self.logger().warning(f"Unknown transaction status: {status}")
                except Exception as poll_error:
                    self.logger().error(f"Error while polling transaction: {str(poll_error)}")

                await asyncio.sleep(30)

        except Exception as e:
            self.logger().error(f"Error in poll_transaction: {str(e)}")
        finally:
            if not ticker_task.done():
                ticker_task.cancel()
