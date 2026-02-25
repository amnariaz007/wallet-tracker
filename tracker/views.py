"""
Django REST Framework views for the Wallet Intelligence API.
"""

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .etherscan_service import EtherscanClient, EtherscanError, WalletService
from .models import Transaction, Wallet, WalletEdge


# ── Serializers ──────────────────────────────────────────────────────────────

class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = [
            "address", "label", "risk_score",
            "first_seen", "last_seen", "tx_count", "updated_at",
        ]


class TransactionSerializer(serializers.ModelSerializer):
    from_address = serializers.CharField(source="from_wallet.address")
    to_address = serializers.CharField(source="to_wallet.address", allow_null=True)

    class Meta:
        model = Transaction
        fields = [
            "tx_hash", "block_number", "timestamp",
            "from_address", "to_address",
            "value_eth", "gas_used", "gas_price_gwei",
            "is_error", "method_id",
        ]


class WalletEdgeSerializer(serializers.ModelSerializer):
    source = serializers.CharField(source="source.address")
    target = serializers.CharField(source="target.address")

    class Meta:
        model = WalletEdge
        fields = [
            "source", "target", "tx_count",
            "total_value_eth", "first_interaction", "last_interaction",
        ]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _validate_address(address: str) -> tuple[bool, str]:
    address = address.strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return False, address
    return True, address


# ── Views ────────────────────────────────────────────────────────────────────

@api_view(["POST"])
def sync_wallet(request):
    """
    POST /api/wallets/sync/
    Body: { "address": "0x...", "max_pages": 5 }

    Fetches transactions from Etherscan and stores them.
    """
    address = request.data.get("address", "")
    max_pages = int(request.data.get("max_pages", 5))

    valid, address = _validate_address(address)
    if not valid:
        return Response(
            {"error": "Invalid Ethereum address."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        service = WalletService()
        result = service.sync_wallet(address, max_pages=max_pages)
        return Response(result, status=status.HTTP_200_OK)
    except EtherscanError as e:
        return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception as e:
        return Response(
            {"error": "Internal error", "detail": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
def get_wallet(request, address: str):
    """
    GET /api/wallets/<address>/

    Returns wallet details + recent transactions.
    """
    valid, address = _validate_address(address)
    if not valid:
        return Response({"error": "Invalid address."}, status=400)

    try:
        wallet = Wallet.objects.get(address=address)
    except Wallet.DoesNotExist:
        return Response({"error": "Wallet not found. Sync it first."}, status=404)

    transactions = Transaction.objects.filter(
        from_wallet=wallet
    ).union(
        Transaction.objects.filter(to_wallet=wallet)
    ).order_by("-timestamp")[:50]

    return Response({
        "wallet": WalletSerializer(wallet).data,
        "transactions": TransactionSerializer(transactions, many=True).data,
    })


@api_view(["GET"])
def get_wallet_graph(request, address: str):
    """
    GET /api/wallets/<address>/graph/?depth=1

    Returns nodes and edges for D3 / graph visualization.
    """
    valid, address = _validate_address(address)
    if not valid:
        return Response({"error": "Invalid address."}, status=400)

    depth = int(request.query_params.get("depth", 1))
    depth = min(depth, 3)  # cap depth to prevent huge queries

    service = WalletService()
    graph = service.get_wallet_graph(address, depth=depth)

    if not graph["nodes"]:
        return Response(
            {"error": "Wallet not found or has no connections."},
            status=404,
        )

    return Response(graph)


@api_view(["GET"])
def get_wallet_connections(request, address: str):
    """
    GET /api/wallets/<address>/connections/

    Returns direct connections (edges) for a wallet.
    """
    valid, address = _validate_address(address)
    if not valid:
        return Response({"error": "Invalid address."}, status=400)

    try:
        wallet = Wallet.objects.get(address=address)
    except Wallet.DoesNotExist:
        return Response({"error": "Wallet not found."}, status=404)

    out_edges = WalletEdge.objects.filter(source=wallet).select_related("target")
    in_edges = WalletEdge.objects.filter(target=wallet).select_related("source")

    return Response({
        "address": address,
        "outgoing": WalletEdgeSerializer(out_edges, many=True).data,
        "incoming": WalletEdgeSerializer(in_edges, many=True).data,
        "total_connections": out_edges.count() + in_edges.count(),
    })


@api_view(["GET"])
def get_eth_price(request):
    """
    GET /api/eth-price/
    Returns current ETH price from Etherscan.
    """
    try:
        client = EtherscanClient()
        price = client.get_eth_price()
        return Response(price)
    except EtherscanError as e:
        return Response({"error": str(e)}, status=502)


@api_view(["GET"])
def list_wallets(request):
    """
    GET /api/wallets/?search=0x...

    List all tracked wallets, optionally filtered by address prefix.
    """
    search = request.query_params.get("search", "").lower()
    qs = Wallet.objects.all()
    if search:
        qs = qs.filter(address__startswith=search)
    wallets = qs[:100]
    return Response(WalletSerializer(wallets, many=True).data)
