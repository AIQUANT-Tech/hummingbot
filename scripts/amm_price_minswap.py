import asyncio
from decimal import Decimal

from pydantic import Field  # type: ignore

from hummingbot.client.config.config_data_types import BaseClientModel, ClientFieldData
from hummingbot.core.event.events import TradeType
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class AmmPriceConfig(BaseClientModel):
    script_file_name: str = Field(default="amm_price_minswap.py")
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
        "SELL",
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Trade side (BUY or SELL)")
    )
    order_amount: Decimal = Field(
        Decimal("0.01"),
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Order amount for the trade")
    )


class AmmPriceMinswap(ScriptStrategyBase):
    on_going_task = False

    @classmethod
    def init_markets(cls, config: AmmPriceConfig):
        cls.markets = {config.connector_chain_network: {config.trading_pair}}

    def __init__(self, connectors, config: AmmPriceConfig):
        super().__init__(connectors)
        self.config = config

    def on_tick(self):
        if not self.on_going_task:
            self.on_going_task = True
            safe_ensure_future(self.async_task())

    async def async_task(self):
        base, quote = self.config.trading_pair.split("-")
        connector, chain, network = self.config.connector_chain_network.split("_")
        trade_type = TradeType.BUY if self.config.side.upper() == "BUY" else TradeType.SELL

        try:
            self.logger().info(f"Fetching price for {self.config.trading_pair}...")
            data = await GatewayHttpClient.get_instance().get_price(
                chain, network, connector, base, quote, self.config.order_amount, trade_type
            )
            price = data.get("price", "N/A")
            amount = data.get("amount", "N/A")
            rawAmount = data.get("rawAmount", "N/A")
            self.logger().info(f"Price: {price}, Amount: {amount}, rawAmount:{rawAmount}")
            self.logger().info("Waiting for 1 minutes before next fetch...")
            await asyncio.sleep(60)  # 1-minute sleep
        except Exception as e:
            self.logger().error(f"Error fetching price: {str(e)}")
        finally:
            self.on_going_task = False
