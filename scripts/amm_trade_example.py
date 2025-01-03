import asyncio
from decimal import Decimal
from pydantic import Field
from hummingbot.client.settings import GatewayConnectionSetting
from hummingbot.core.event.events import TradeType
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.client.config.config_data_types import BaseClientModel, ClientFieldData


class AmmTradeConfig(BaseClientModel):
    script_file_name: str = Field(default="amm_trade_example.py")
    connector_chain_network: str = Field(
        "minswap_cardano_preprod",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Connector, chain, and network (e.g., uniswap_ethereum_goerli)"
        )
    )
    trading_pair: str = Field(
        "ADA-MIN",
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Trading pair (e.g., WETH-DAI)")
    )
    side: str = Field(
        "BUY",
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Trade side (BUY or SELL)")
    )
    order_amount: Decimal = Field(
        Decimal("0.01"),
        client_data=ClientFieldData(prompt_on_new=True, prompt=lambda mi: "Order amount for the trade")
    )


class AmmTradeExample(ScriptStrategyBase):
    slippage_buffer = 0.01
    on_going_task = False
    trade_executed = False  # New flag to ensure only one trade is executed

    @classmethod
    def init_markets(cls, config: AmmTradeConfig):
        cls.markets = {config.connector_chain_network: {config.trading_pair}}

    def __init__(self, connectors, config: AmmTradeConfig):
        super().__init__(connectors)
        self.config = config

    def on_tick(self):
        if not self.on_going_task and not self.trade_executed:  # Prevent further trades
            self.on_going_task = True
            safe_ensure_future(self.async_task())

    async def async_task(self):
        base, quote = self.config.trading_pair.split("-")
        connector, chain, network = self.config.connector_chain_network.split("_")
        trade_type = TradeType.BUY if self.config.side.upper() == "BUY" else TradeType.SELL

        try:
            # Fetch current price
            self.logger().info(f"Fetching price for {self.config.trading_pair}...")
            price_data = await GatewayHttpClient.get_instance().get_price(
                chain, network, connector, base, quote, self.config.order_amount, trade_type
            )
            price = float(price_data.get("price", 0))
            self.logger().info(f"Current Price: {price}")

            # Apply slippage buffer
            adjusted_price = price * (1 + self.slippage_buffer) if trade_type == TradeType.BUY else price * (1 - self.slippage_buffer)
            self.logger().info(f"Adjusted Price with Slippage: {adjusted_price}")

            # Fetch wallet details
            gateway_connections_conf = GatewayConnectionSetting.load()
            wallet = next(
                (w for w in gateway_connections_conf if w["chain"] == chain and w["connector"] == connector and w["network"] == network), None
            )
            if not wallet:
                self.notify("No wallet configured for the specified chain, connector, and network.")
                return

            address = wallet["wallet_address"]
            await self.get_balance(chain, network, address, base, quote)

            # Execute trade
            self.logger().info(f"Executing trade...")
            order_amount = int(self.config.order_amount)
            trade_data = await GatewayHttpClient.get_instance().amm_trade(
                chain, network, connector, address, base, quote, trade_type, order_amount, Decimal(adjusted_price)
            )

            # Poll transaction status
            await self.poll_transaction(chain, network, trade_data.get("txHash"))
            await self.get_balance(chain, network, address, base, quote)

            # Mark trade as executed
            self.trade_executed = True  # Prevent future trades
            self.logger().info("Trade executed successfully. No further trades will be made.")

        except Exception as e:
            self.logger().error(f"Error in async_task: {str(e)}")
        finally:
            self.on_going_task = False

    async def get_balance(self, chain, network, address, base, quote):
        try:
            self.logger().info(f"Fetching balances for {address}...")
            balance_data = await GatewayHttpClient.get_instance().get_balances(
                chain, network, address, [base, quote]
            )
            self.logger().info(f"Balances: {balance_data['balances']}")
        except Exception as e:
            self.logger().error(f"Error fetching balances: {str(e)}")

    async def poll_transaction(self, chain, network, tx_hash):
        try:
            self.logger().info(f"Polling transaction status for {tx_hash}...")
            
            # Start a ticker to log "Fetching transaction, please wait..." every 5 seconds
            async def show_fetching_message():
                while True:
                    self.logger().info("Fetching transaction, please wait...")
                    await asyncio.sleep(5)
            
            ticker_task = asyncio.create_task(show_fetching_message())

            # Initial delay to give the transaction some time to process
            await asyncio.sleep(30)  # Wait 30 seconds before the first poll

            while True:
                try:
                    poll_data = await GatewayHttpClient.get_instance().get_transaction_status(
                        chain, network, tx_hash
                    )
                    
                    # Parse response
                    status = poll_data.get("status")
                    block = poll_data.get("block")
                    block_height = poll_data.get("blockHeight")
                    fees = poll_data.get("fees")
                    valid_contract = poll_data.get("validContract")
                    
                    if status == "confirmed":
                        self.logger().info(f"Transaction {tx_hash} confirmed successfully.")
                        self.logger().info(f"Block: {block}, Block Height: {block_height}, Fees: {fees}")
                        if valid_contract:
                            self.logger().info(f"Transaction is valid and successfully processed.")
                        else:
                            self.logger().warning(f"Transaction was processed but is invalid.")
                        
                        # Cancel the ticker and exit the loop
                        ticker_task.cancel()
                        break
                    elif status == "pending":
                        self.logger().info(f"Transaction {tx_hash} is still pending...")
                    else:
                        self.logger().warning(f"Unknown transaction status: {status}")

                except Exception as poll_error:
                    self.logger().error(f"Error while polling transaction: {str(poll_error)}")
                    self.logger().info(f"Retrying polling for transaction {tx_hash}...")

                # Wait before the next polling attempt
                await asyncio.sleep(30)

        except Exception as e:
            self.logger().error(f"Error in poll_transaction: {str(e)}")
        finally:
            # Ensure ticker is canceled in case of unexpected exit
            if not ticker_task.done():
                ticker_task.cancel()
