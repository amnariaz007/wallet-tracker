"""
Etherscan API Integration Service
Handles all communication with the Etherscan API and persists data to Django models.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import requests
from django.conf import settings
from django.db import transaction as db_transaction

from .models import Transaction, Wallet, WalletEdge

logger = logging.getLogger(__name__)

WEI_TO_ETH = Decimal("1e18")
WEI_TO_GWEI = Decimal("1e9")

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"


@dataclass
class EtherscanConfig:
    api_key: str
    base_url: str = ETHERSCAN_BASE_URL
    timeout: int = 15
    max_retries: int = 3


class EtherscanError(Exception):
    pass


class EtherscanRateLimitError(EtherscanError):
    pass


class EtherscanClient:
    """Low-level Etherscan API client."""

    def __init__(self, config: Optional[EtherscanConfig] = None):
        self.config = config or EtherscanConfig(
            api_key=getattr(settings, "ETHERSCAN_API_KEY", "")
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "WalletIntel/1.0"})

    def _get(self, params: dict) -> dict:
        params["apikey"] = self.config.api_key
        params["chainid"] = 1
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.get(
                    self.config.base_url,
                    params=params,
                    timeout=self.config.timeout,
                )
                response.raise_for_status()
                data = response.json()

                

                if data.get("status") == "0":
                    msg = data.get("message", "Unknown error")
                    result = data.get("result", "")
                    # "No transactions found" is not a real error
                    if "No transactions found" in str(result):
                        return {"status": "1", "result": []}
                    if "rate limit" in str(result).lower():
                        raise EtherscanRateLimitError(result)
                    raise EtherscanError(f"Etherscan API error: {msg} — {result}")

                return data

            except requests.RequestException as e:
                if attempt == self.config.max_retries - 1:
                    raise EtherscanError(f"Request failed after {self.config.max_retries} attempts: {e}")
                logger.warning(f"Request attempt {attempt + 1} failed: {e}")

    # ── Account endpoints ────────────────────────────────────────────────────

    def get_eth_balance(self, address: str) -> Decimal:
        data = self._get({
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
        })
        return Decimal(data["result"]) / WEI_TO_ETH

    def get_normal_transactions(
        self,
        address: str,
        start_block: int = 0,
        end_block: int = 99999999,
        page: int = 1,
        offset: int = 100,
        sort: str = "desc",
    ) -> list[dict]:
        data = self._get({
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": sort,
        })

        return data.get("result", [])

    def get_internal_transactions(
        self,
        address: str,
        start_block: int = 0,
        end_block: int = 99999999,
        page: int = 1,
        offset: int = 100,
    ) -> list[dict]:
        data = self._get({
            "module": "account",
            "action": "txlistinternal",
            "address": address,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": "desc",
        })
        return data.get("result", [])

    def get_erc20_transfers(
        self,
        address: str,
        contract_address: Optional[str] = None,
        page: int = 1,
        offset: int = 100,
    ) -> list[dict]:
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": page,
            "offset": offset,
            "sort": "desc",
        }
        if contract_address:
            params["contractaddress"] = contract_address
        data = self._get(params)
        return data.get("result", [])

    # ── Contract endpoints ───────────────────────────────────────────────────

    def get_contract_abi(self, address: str) -> str:
        data = self._get({
            "module": "contract",
            "action": "getabi",
            "address": address,
        })
        return data.get("result", "")

    def is_contract(self, address: str) -> bool:
        """Check if address is a smart contract via bytecode."""
        data = self._get({
            "module": "proxy",
            "action": "eth_getCode",
            "address": address,
            "tag": "latest",
        })
        code = data.get("result", "0x")
        return code != "0x" and len(code) > 2

    # ── Stats endpoints ──────────────────────────────────────────────────────

    def get_eth_price(self) -> dict:
        data = self._get({
            "module": "stats",
            "action": "ethprice",
        })
        return data.get("result", {})


class WalletService:
    """
    High-level service that fetches wallet data from Etherscan
    and persists it to the Django database.
    """

    def __init__(self, client: Optional[EtherscanClient] = None):
        self.client = client or EtherscanClient()

    def _get_or_create_wallet(self, address: str) -> tuple[Wallet, bool]:
        address = address.lower()
        return Wallet.objects.get_or_create(address=address)

    def _parse_tx_row(self, row: dict) -> dict:
        """Normalize a raw Etherscan transaction row."""
        ts = datetime.fromtimestamp(int(row["timeStamp"]), tz=timezone.utc)
        value_eth = Decimal(row.get("value", "0")) / WEI_TO_ETH
        gas_price_gwei = Decimal(row.get("gasPrice", "0")) / WEI_TO_GWEI
        gas_used = int(row.get("gasUsed", row.get("gas", 0)))
        input_data = row.get("input", "")
        method_id = input_data[:10] if len(input_data) >= 10 else ""

        return {
            "tx_hash": row["hash"],
            "block_number": int(row["blockNumber"]),
            "timestamp": ts,
            "from_address": row["from"].lower(),
            "to_address": (row.get("to") or "").lower(),
            "value_eth": value_eth,
            "gas_used": gas_used,
            "gas_price_gwei": gas_price_gwei,
            "is_error": row.get("isError", "0") == "1",
            "method_id": method_id,
        }

    @db_transaction.atomic
    def sync_wallet(self, address: str, max_pages: int = 5) -> dict:
        """
        Main entry point. Fetches and stores all transaction data
        for a given wallet address.

        Returns a summary dict with stats.
        """
        address = address.lower()
        wallet, _ = self._get_or_create_wallet(address)

        all_txs = []
        for page in range(1, max_pages + 1):
            rows = self.client.get_normal_transactions(
                address, page=page, offset=100
            )
            if not rows:
                break
            all_txs.extend(rows)
            if len(rows) < 100:
                break  # last page

        if not all_txs:
            return {"address": address, "synced": 0, "connections": 0}

        # Parse and persist
        parsed = [self._parse_tx_row(r) for r in all_txs]
        self._persist_transactions(wallet, parsed)
        self._update_wallet_stats(wallet, parsed)
        self._build_edges(wallet, parsed)

        return {
            "address": address,
            "synced": len(parsed),
            "connections": WalletEdge.objects.filter(source=wallet).count()
            + WalletEdge.objects.filter(target=wallet).count(),
        }

    def _persist_transactions(self, wallet: Wallet, parsed: list[dict]):
        """Bulk-create transactions, skipping duplicates."""
        existing_hashes = set(
            Transaction.objects.filter(
                tx_hash__in=[p["tx_hash"] for p in parsed]
            ).values_list("tx_hash", flat=True)
        )

        to_create = []
        for p in parsed:
            if p["tx_hash"] in existing_hashes:
                continue

            from_wallet, _ = self._get_or_create_wallet(p["from_address"])
            to_wallet = None
            if p["to_address"]:
                to_wallet, _ = self._get_or_create_wallet(p["to_address"])

            to_create.append(
                Transaction(
                    tx_hash=p["tx_hash"],
                    block_number=p["block_number"],
                    timestamp=p["timestamp"],
                    from_wallet=from_wallet,
                    to_wallet=to_wallet,
                    value_eth=p["value_eth"],
                    gas_used=p["gas_used"],
                    gas_price_gwei=p["gas_price_gwei"],
                    is_error=p["is_error"],
                    method_id=p["method_id"],
                )
            )

        if to_create:
            Transaction.objects.bulk_create(to_create, ignore_conflicts=True)

    def _update_wallet_stats(self, wallet: Wallet, parsed: list[dict]):
        timestamps = [p["timestamp"] for p in parsed]
        wallet.tx_count = len(parsed)
        wallet.first_seen = min(timestamps)
        wallet.last_seen = max(timestamps)
        wallet.save(update_fields=["tx_count", "first_seen", "last_seen", "updated_at"])

    def _build_edges(self, wallet: Wallet, parsed: list[dict]):
        """
        Update or create WalletEdge records representing
        the directional relationship between wallets.
        """
        edge_map: dict[tuple, dict] = {}

        for p in parsed:
            from_addr = p["from_address"]
            to_addr = p["to_address"]
            if not to_addr:
                continue

            key = (from_addr, to_addr)
            if key not in edge_map:
                edge_map[key] = {
                    "count": 0,
                    "value": Decimal("0"),
                    "first": p["timestamp"],
                    "last": p["timestamp"],
                }

            edge_map[key]["count"] += 1
            edge_map[key]["value"] += p["value_eth"]
            edge_map[key]["first"] = min(edge_map[key]["first"], p["timestamp"])
            edge_map[key]["last"] = max(edge_map[key]["last"], p["timestamp"])

        for (from_addr, to_addr), stats in edge_map.items():
            from_w, _ = self._get_or_create_wallet(from_addr)
            to_w, _ = self._get_or_create_wallet(to_addr)

            edge, created = WalletEdge.objects.get_or_create(
                source=from_w,
                target=to_w,
                defaults={
                    "tx_count": stats["count"],
                    "total_value_eth": stats["value"],
                    "first_interaction": stats["first"],
                    "last_interaction": stats["last"],
                },
            )

            if not created:
                edge.tx_count += stats["count"]
                edge.total_value_eth += stats["value"]
                edge.last_interaction = max(edge.last_interaction, stats["last"])
                edge.save(update_fields=[
                    "tx_count", "total_value_eth", "last_interaction"
                ])

    def get_wallet_graph(self, address: str, depth: int = 1) -> dict:
        """
        Returns a graph dict with nodes and edges for visualization.
        depth=1 means direct connections only.
        """
        address = address.lower()
        try:
            wallet = Wallet.objects.get(address=address)
        except Wallet.DoesNotExist:
            return {"nodes": [], "edges": []}

        visited = {address}
        nodes = [self._wallet_to_node(wallet)]
        edges = []

        queue = [wallet]

        for _ in range(depth):
            next_queue = []
            for w in queue:
                out_edges = WalletEdge.objects.filter(source=w).select_related("target")
                in_edges = WalletEdge.objects.filter(target=w).select_related("source")

                for edge in list(out_edges) + list(in_edges):
                    peer = edge.target if edge.source == w else edge.source
                    edge_dict = {
                        "source": edge.source.address,
                        "target": edge.target.address,
                        "tx_count": edge.tx_count,
                        "value_eth": float(edge.total_value_eth),
                    }
                    if edge_dict not in edges:
                        edges.append(edge_dict)

                    if peer.address not in visited:
                        visited.add(peer.address)
                        nodes.append(self._wallet_to_node(peer))
                        next_queue.append(peer)

            queue = next_queue

        return {"nodes": nodes, "edges": edges}

    def _wallet_to_node(self, wallet: Wallet) -> dict:
        return {
            "id": wallet.address,
            "label": wallet.label or wallet.address[:8] + "...",
            "risk_score": wallet.risk_score,
            "tx_count": wallet.tx_count,
        }
