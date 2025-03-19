import asyncio
from decimal import ROUND_DOWN, Decimal

from pydantic import Field  # type: ignore

from hummingbot.client.config.config_data_types import BaseClientModel, ClientFieldData
from hummingbot.client.settings import GatewayConnectionSetting
from hummingbot.core.event.events import LPType
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class liquidityBotConfig(BaseClientModel):
    script_file_name: str = Field(default="liquidity_bot_sundaeswap.py")
    connector_chain_network: str = Field(
        "sundaeswap_cardano_preview",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Connector, chain, and network (e.g., sundaeswap_cardano_preview)"
        )
    )

    trading_pair: str = Field(
        "SBERRY-ADA",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Trading pair (e.g., SBERRY-ADA)"
        )
    )

    side: str = Field(
        "ADD",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Side (ADD/REMOVE) Liquidity"
        )
    )

    # Token and amount fields will be included only when side is ADD
    token0: str = Field(
        "SBERRY",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Token to Add or remove from liquidity pool (e.g., SBERRY)" if mi.side.upper() == "ADD" else None
        ),
    )

    token1: str = Field(
        "ADA",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Token to Add or remove from liquidity pool (e.g., ADA)" if mi.side.upper() == "ADD" else None
        ),
    )

    amount0: int = Field(
        10000,
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Amount to Add or remove from liquidity pool (e.g., 10000)" if mi.side.upper() == "ADD" else None
        ),
    )

    # when side is REMOVE we don't need decrease percentage
    decrease_percentage: float = Field(
        50,
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Decrease percentage for REMOVE liquidity (e.g., 50 for 50%)" if mi.side.upper() == "REMOVE" else None
        ),
        ge=0,  # Ensure it's a non-negative value
        le=100,  # Ensure it's between 0 and 100
    )


class liquidityBotSundaeswap(ScriptStrategyBase):
    on_going_task = False
    liquidity_flow_executed = False
    balance_data = None
    tx_hash = None

    @classmethod
    def init_markets(cls, config: liquidityBotConfig):
        cls.markets = {config.connector_chain_network: {config.trading_pair}}

    def __init__(self, connectors, config: liquidityBotConfig):
        super().__init__(connectors)
        self.config = config
        self.balance_last_updated = None

    def on_tick(self):
        if not self.on_going_task and not self.liquidity_flow_executed:
            self.on_going_task = True
            safe_ensure_future(self.async_task())

    async def async_task(self):
        connector, chain, network = self.config.connector_chain_network.split("_")
        token0, token1 = self.config.token0, self.config.token1
        amount0 = self.config.amount0
        lp_type = LPType.ADD if self.config.side.upper() == "ADD" else LPType.REMOVE
        tokenLp = "LP"

        try:
            if lp_type == LPType.ADD:
                self.logger().info("Calculating amount1 based on price...")
            # Fetch the required amount1
            price_data = await GatewayHttpClient.get_instance().amm_lp_price(
                chain, network, connector, token0, token1, fee="LOW"
            )
            price_SBERRY_ADA = Decimal(price_data["prices"][0])  # Assuming SBERRY/ADA price

            amount1 = (Decimal(amount0) * price_SBERRY_ADA).quantize(Decimal("1.00000000"), rounding=ROUND_DOWN)

            if lp_type == LPType.ADD:
                self.logger().info(f"Calculated amount1: {amount1}")

            # Update balances
            gateway_connections_conf = GatewayConnectionSetting.load()
            wallet = next(
                (w for w in gateway_connections_conf if w["chain"] == chain and w["connector"] == connector and w["network"] == network), None
            )
            address = wallet["wallet_address"]
            await self.update_balance(chain, network, address, token0, token1, tokenLp)

            # Fetch current price of the liquidity pool
            self.logger().info(f"Fetching price from liquidity pool {self.config.trading_pair}...")
            price_SBERRY_ADA = Decimal(price_data["prices"][0])

            self.logger().info(f"Price SBERRY/ADA: {price_SBERRY_ADA}")

            # Execute the liquidity action
            self.logger().info(f"Executing {self.config.side} liquidity action...")
            if lp_type == LPType.ADD:
                transaction_result = await GatewayHttpClient.get_instance().amm_lp_add(
                    chain, network, connector, address, token0, token1, amount0, int(amount1), fee="LOW", lowerPrice=0.5, upperPrice=1
                )
                tx_hash = transaction_result["txHash"]

            elif lp_type == LPType.REMOVE:
                decrease_percentage = self.config.decrease_percentage
                transaction_result = await GatewayHttpClient.get_instance().amm_lp_remove(
                    chain, network, connector, address, token_id=0, decreasePercent=decrease_percentage
                )
                tx_hash = transaction_result["txHash"]

            self.logger().info(f"Liquidity transaction submitted with txHash: {tx_hash}")

            # Poll transaction status
            await self.poll_transaction(chain, network, tx_hash)

            # Update balances
            await self.update_balance(chain, network, address, token0, token1, tokenLp)

            # Mark the task as completed
            self.liquidity_flow_executed = True
            self.logger().info("Liquidity flow executed successfully.")

        except Exception as e:
            self.logger().error(f"Error in async_task: {str(e)}")
        finally:
            self.on_going_task = False

    async def update_balance(self, chain, network, address, base, quote, lp):
        try:
            self.logger().info(f"Fetching balances for {address}...")
            self.balance_data = await GatewayHttpClient.get_instance().get_balances(
                chain, network, address, [base, quote, lp]
            )
            self.logger().info(f"Updated Balances: {self.balance_data['balances']}")
            self.balance_last_updated = asyncio.get_event_loop().time()
        except Exception as e:
            self.logger().error(f"Error fetching balances: {str(e)}")

    async def poll_transaction(self, chain, network, tx_hash):
        try:
            self.logger().info(f"Polling transaction status for {tx_hash}...")

            async def show_fetching_message():
                while True:
                    self.logger().info("Fetching transaction, please wait...")
                    await asyncio.sleep(5)

            ticker_task = asyncio.create_task(show_fetching_message())

            await asyncio.sleep(40)

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
            self.logger().info("Retrying polling transaction...")
        finally:
            if not ticker_task.done():
                ticker_task.cancel()
