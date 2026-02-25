from django.urls import path
from . import views

urlpatterns = [
    path("wallets/", views.list_wallets),
    path("wallets/sync/", views.sync_wallet),
    path("wallets/<str:address>/", views.get_wallet),
    path("wallets/<str:address>/graph/", views.get_wallet_graph),
    path("wallets/<str:address>/connections/", views.get_wallet_connections),
    path("eth-price/", views.get_eth_price),
]
