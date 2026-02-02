import asyncio
import json
import os
from datetime import datetime
from typing import Dict, Optional, List
import random

from web3 import Web3
from web3.exceptions import Web3Exception
from telegram import Bot
from telegram.error import TelegramError


class AAVEMonitorEnhanced:
    """AAVE monitor with enhanced display and liquidation price calculation"""

    POOL_ABI = [
        {
            "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
            "name": "getUserAccountData",
            "outputs": [
                {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
                {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
                {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
                {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
                {"internalType": "uint256", "name": "ltv", "type": "uint256"},
                {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}
            ],
            "stateMutability": "view",
            "type": "function"
        }
    ]

    # Oracle ABI for getting asset prices
    ORACLE_ABI = [
        {
            "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
            "name": "getAssetPrice",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function"
        }
    ]

    # Multiple RPC providers for each network with failover
    NETWORKS = {
        "ethereum": {
            "rpcs": [
                "https://eth.llamarpc.com",
                "https://rpc.ankr.com/eth",
                "https://ethereum.publicnode.com",
                "https://eth.drpc.org",
                "https://1rpc.io/eth",
                "https://eth.meowrpc.com"
            ],
            "pool": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
            "oracle": "0x54586bE62E3c3580375aE3723C145253060Ca0C2",
            "chain_id": 1
        },
        "polygon": {
            "rpcs": [
                "https://polygon.llamarpc.com",
                "https://rpc.ankr.com/polygon",
                "https://polygon.drpc.org",
                "https://polygon-bor-rpc.publicnode.com",
                "https://1rpc.io/matic"
            ],
            "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
            "oracle": "0xb023e699F5a33916Ea823A16485e259257cA8Bd1",
            "chain_id": 137
        },
        "arbitrum": {
            "rpcs": [
                "https://arbitrum.llamarpc.com",
                "https://rpc.ankr.com/arbitrum",
                "https://arbitrum.drpc.org",
                "https://arbitrum-one-rpc.publicnode.com",
                "https://1rpc.io/arb"
            ],
            "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
            "oracle": "0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7",
            "chain_id": 42161
        },
        "optimism": {
            "rpcs": [
                "https://optimism.llamarpc.com",
                "https://rpc.ankr.com/optimism",
                "https://optimism.drpc.org",
                "https://optimism-rpc.publicnode.com",
                "https://1rpc.io/op"
            ],
            "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
            "oracle": "0xD81eb3728a631871a7eBBaD631b5f424909f0c77",
            "chain_id": 10
        }
    }

    def __init__(self, config_path: str = "config.json"):
        """Initialize monitor"""
        self.config = self._load_config(config_path)
        self.network = self.config.get("network", "ethereum")
        self.addresses = self.config.get("addresses", [])
        self.telegram_token = self.config.get("telegram_token")
        self.telegram_chat_id = self.config.get("telegram_chat_id")
        self.update_interval = self.config.get("update_interval", 60)

        # Get network config
        self.network_config = self.NETWORKS[self.network]

        # Initialize Web3 connections
        self.w3_providers = []
        self.current_rpc_index = 0
        self._init_web3_providers()

        # Initialize Telegram bot
        self.bot = Bot(token=self.telegram_token)
        self.message_id = None

    def _load_config(self, config_path: str) -> dict:
        """Load configuration"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, 'r') as f:
            return json.load(f)

    def _init_web3_providers(self):
        """Initialize Web3 providers for all RPCs"""
        rpcs = self.network_config["rpcs"].copy()
        random.shuffle(rpcs)  # Randomize to distribute load

        for rpc_url in rpcs:
            try:
                w3 = Web3(Web3.HTTPProvider(
                    rpc_url,
                    request_kwargs={'timeout': 30}
                ))

                pool_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(self.network_config["pool"]),
                    abi=self.POOL_ABI
                )

                self.w3_providers.append({
                    'w3': w3,
                    'contract': pool_contract,
                    'url': rpc_url,
                    'failed_count': 0
                })

            except Exception as e:
                print(f"⚠️  Could not initialize RPC {rpc_url}: {e}")

        if not self.w3_providers:
            raise Exception("No working RPC providers found!")

        print(f"✓ Initialized {len(self.w3_providers)} RPC provider(s)")

    def _get_working_provider(self):
        """Get a working Web3 provider with failover"""
        attempts = 0
        max_attempts = len(self.w3_providers) * 2

        while attempts < max_attempts:
            provider = self.w3_providers[self.current_rpc_index]

            # Skip providers that failed too many times
            if provider['failed_count'] < 3:
                try:
                    # Quick connectivity check
                    if provider['w3'].is_connected():
                        return provider
                except:
                    pass

            # Move to next provider
            self.current_rpc_index = (self.current_rpc_index + 1) % len(self.w3_providers)
            attempts += 1

        # Reset fail counts and try again
        for p in self.w3_providers:
            p['failed_count'] = 0

        return self.w3_providers[0]

    def _calculate_liquidation_price(self, collateral_usd: float, debt_usd: float,
                                     liquidation_threshold: float, health_factor: float) -> Optional[float]:
        """
        Calculate estimated liquidation price

        Liquidation occurs when: HF < 1.0
        HF = (Collateral * Price * LiqThreshold) / Debt

        At liquidation (HF = 1.0):
        1.0 = (Collateral * LiqPrice * LiqThreshold) / Debt
        LiqPrice = Debt / (Collateral * LiqThreshold)

        Current price can be derived from:
        CurrentPrice = (Debt * HF) / (Collateral * LiqThreshold)

        Price drop to liquidation = CurrentPrice - LiqPrice
        """

        if collateral_usd == 0 or debt_usd == 0 or liquidation_threshold == 0:
            return None

        try:
            # Calculate current implied price (normalized to 1.0)
            current_price_normalized = 1.0

            # Calculate liquidation price as percentage of current price
            # When HF = 1.0: LiqPrice = Debt / (Collateral * LiqThreshold)
            # Current: CurrentPrice = (Debt * HF) / (Collateral * LiqThreshold)
            # Ratio: LiqPrice/CurrentPrice = 1/HF

            liquidation_price_ratio = 1.0 / health_factor if health_factor > 0 else 0

            # Percentage drop needed to reach liquidation
            price_drop_pct = (1.0 - liquidation_price_ratio) * 100

            return {
                'liquidation_price_ratio': liquidation_price_ratio,
                'price_drop_to_liquidation_pct': price_drop_pct,
                'current_price_normalized': current_price_normalized
            }
        except Exception as e:
            print(f"Error calculating liquidation price: {e}")
            return None

    async def get_position_data(self, address: str) -> Optional[Dict]:
        """Get position data with RPC failover"""
        max_retries = len(self.w3_providers)

        for retry in range(max_retries):
            provider = self._get_working_provider()

            try:
                checksum_address = Web3.to_checksum_address(address)

                # Get user account data
                account_data = provider['contract'].functions.getUserAccountData(
                    checksum_address
                ).call()

                # Reset fail count on success
                provider['failed_count'] = 0

                # Parse data
                total_collateral = account_data[0] / 1e8
                total_debt = account_data[1] / 1e8
                available_borrows = account_data[2] / 1e8
                liquidation_threshold = account_data[3] / 1e4  # Percentage (e.g., 82.5%)
                ltv = account_data[4] / 1e4
                health_factor = account_data[5] / 1e18

                # Calculate liquidation price
                liq_price_data = self._calculate_liquidation_price(
                    total_collateral,
                    total_debt,
                    liquidation_threshold / 100,  # Convert to decimal
                    health_factor
                )

                return {
                    "address": address,
                    "collateral_usd": total_collateral,
                    "debt_usd": total_debt,
                    "available_borrows_usd": available_borrows,
                    "health_factor": health_factor,
                    "liquidation_threshold": liquidation_threshold,
                    "ltv": ltv,
                    "liquidation_price_data": liq_price_data,
                    "timestamp": datetime.now().isoformat(),
                    "rpc_used": provider['url']
                }

            except Exception as e:
                provider['failed_count'] += 1
                print(f"⚠️  RPC {provider['url']} failed (attempt {retry + 1}/{max_retries}): {str(e)[:100]}")

                # Try next provider
                self.current_rpc_index = (self.current_rpc_index + 1) % len(self.w3_providers)

                if retry < max_retries - 1:
                    await asyncio.sleep(2)  # Wait before retry
                    continue

        print(f"❌ All RPC providers failed for address {address}")
        return None

    def _format_number(self, num: float, decimals: int = 2) -> str:
        """Format number"""
        if num >= 1_000_000:
            return f"${num / 1_000_000:.{decimals}f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.{decimals}f}K"
        else:
            return f"${num:.{decimals}f}"

    def _format_message(self, positions: list) -> str:
        """Format message with new layout"""
        network_emoji = {
            "ethereum": "🔷",
            "polygon": "🟣",
            "arbitrum": "🔵",
            "optimism": "🔴"
        }

        emoji = network_emoji.get(self.network, "📊")
        message = f"{emoji} <b>AAVE Monitor - {self.network.upper()}</b>\n"
        message += f"🕐 <i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</i>\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for i, pos in enumerate(positions, 1):
            if pos is None:
                continue

            addr = pos['address']
            hf = pos['health_factor']

            # Health factor emoji
            if hf < 1.1:
                hf_emoji = "🔴"
            elif hf < 1.5:
                hf_emoji = "🟡"
            else:
                hf_emoji = "🟢"

            # Format position
            message += f"<b>📍 Address:</b>\n"
            message += f"<code>{addr}</code>\n\n"

            # Links
            message += f"<b>🔗 Links:</b>\n"
            message += f"• <a href='https://debank.com/profile/{addr}'>DeBank</a>\n"
            message += f"• <a href='https://defisim.xyz/ru?address={addr}'>DeFiSim</a>\n\n"

            # Metrics
            message += f"<b>💰 Collateral:</b> {self._format_number(pos['collateral_usd'])}\n"
            message += f"<b>📉 Debt:</b> {self._format_number(pos['debt_usd'])}\n"
            message += f"<b>{hf_emoji} Health Factor:</b> {hf:.4f}\n"

            # Liquidation price
            if pos['liquidation_price_data']:
                liq_data = pos['liquidation_price_data']
                price_drop = liq_data['price_drop_to_liquidation_pct']

                if price_drop > 0:
                    message += f"<b>⚠️ Liquidation Price:</b> -{price_drop:.2f}% from current\n"
                else:
                    message += f"<b>⚠️ Liquidation Price:</b> Already below (HF < 1.0)\n"

            # Separator between positions
            if i < len(positions):
                message += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        return message

    async def send_or_update_message(self, text: str):
        """Send or update message"""
        try:
            if self.message_id is None:
                msg = await self.bot.send_message(
                    chat_id=self.telegram_chat_id,
                    text=text,
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
                self.message_id = msg.message_id
                print(f"✓ Sent new message (ID: {self.message_id})")
            else:
                await self.bot.edit_message_text(
                    chat_id=self.telegram_chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
                print(f"✓ Updated message (ID: {self.message_id})")
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                print("ℹ No changes to update")
            elif "message to edit not found" in str(e).lower():
                print("⚠ Message not found, sending new one")
                self.message_id = None
                await self.send_or_update_message(text)
            else:
                print(f"✗ Telegram error: {e}")

    async def monitor_loop(self):
        """Main monitoring loop"""
        print(f"🚀 Starting AAVE Monitor (Enhanced)")
        print(f"📡 Network: {self.network}")
        print(f"👥 Monitoring {len(self.addresses)} address(es)")
        print(f"⏱ Update interval: {self.update_interval}s")
        print(f"🔄 RPC providers: {len(self.w3_providers)}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━\n")

        while True:
            try:
                positions = []
                for address in self.addresses:
                    print(f"📊 Fetching data for {address[:8]}...")
                    pos_data = await self.get_position_data(address)
                    if pos_data:
                        positions.append(pos_data)
                        print(f"   ✓ Using RPC: {pos_data['rpc_used']}")

                if positions:
                    message = self._format_message(positions)
                    await self.send_or_update_message(message)
                else:
                    print("⚠ No position data fetched")

                await asyncio.sleep(self.update_interval)

            except KeyboardInterrupt:
                print("\n⏹ Stopping monitor...")
                break
            except Exception as e:
                print(f"✗ Error in monitoring loop: {e}")
                await asyncio.sleep(self.update_interval)

    async def run(self):
        """Run the monitor"""
        await self.monitor_loop()


async def main():
    """Main entry point"""
    monitor = AAVEMonitorEnhanced("config.json")
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())