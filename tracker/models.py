from django.db import models


class Wallet(models.Model):
    address = models.CharField(max_length=42, unique=True, db_index=True)
    label = models.CharField(max_length=100, blank=True)
    risk_score = models.FloatField(default=0.0)
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    tx_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.label or self.address

    class Meta:
        ordering = ["-updated_at"]


class Transaction(models.Model):
    tx_hash = models.CharField(max_length=66, unique=True, db_index=True)
    block_number = models.BigIntegerField()
    timestamp = models.DateTimeField()
    from_wallet = models.ForeignKey(
        Wallet, related_name="outgoing_txs", on_delete=models.CASCADE
    )
    to_wallet = models.ForeignKey(
        Wallet,
        related_name="incoming_txs",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    value_eth = models.DecimalField(max_digits=36, decimal_places=18, default=0)
    gas_used = models.BigIntegerField(default=0)
    gas_price_gwei = models.DecimalField(max_digits=20, decimal_places=9, default=0)
    is_error = models.BooleanField(default=False)
    method_id = models.CharField(max_length=10, blank=True)  # first 4 bytes of input
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.tx_hash

    class Meta:
        ordering = ["-timestamp"]


class WalletEdge(models.Model):
    """Represents a relationship between two wallets (for graph analysis)."""

    source = models.ForeignKey(
        Wallet, related_name="edges_out", on_delete=models.CASCADE
    )
    target = models.ForeignKey(
        Wallet, related_name="edges_in", on_delete=models.CASCADE
    )
    tx_count = models.IntegerField(default=1)
    total_value_eth = models.DecimalField(max_digits=36, decimal_places=18, default=0)
    first_interaction = models.DateTimeField(null=True, blank=True)
    last_interaction = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("source", "target")
        ordering = ["-tx_count"]

    def __str__(self):
        return f"{self.source.address[:8]}... → {self.target.address[:8]}..."
