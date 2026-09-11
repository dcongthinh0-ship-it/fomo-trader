import pytest
from eth_account import Account

from trader.settings import Settings


def clear(monkeypatch):
    for name in ('LIVE_TRADING_ENABLED', 'BUY_AMOUNT_MODE', 'BUY_ASSET_ADDRESS',
                 'TRADER_WALLET_ADDRESS', 'TRADER_PRIVATE_KEY_FILE',
                 'ROBINHOOD_TRADING_RPC_PROVIDER', 'ROBINHOOD_PUBLIC_RPC_URL',
                 'ROBINHOOD_ALCHEMY_RPC_URL'):
        monkeypatch.delenv(name, raising=False)


def test_default_is_live_disabled_and_uses_verified_usdg(monkeypatch):
    clear(monkeypatch)
    settings = Settings()
    assert settings.live is False
    assert settings.buy_asset_symbol == 'USDG'
    assert settings.buy_asset_address == '0x5fc5360d0400a0fd4f2af552add042d716f1d168'


def test_live_wallet_key_mismatch_refuses_start(monkeypatch, tmp_path):
    clear(monkeypatch)
    key = Account.create().key.hex()
    secret = tmp_path / 'private-key'
    secret.write_text(key)
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    monkeypatch.setenv('TRADER_PRIVATE_KEY_FILE', str(secret))
    monkeypatch.setenv('TRADER_WALLET_ADDRESS', '0x' + '1' * 40)
    monkeypatch.setenv('ROBINHOOD_TRADING_RPC_URL', 'https://example.invalid')
    with pytest.raises(ValueError, match='does not match'):
        Settings()


def test_live_wallet_matching_random_test_key_is_accepted(monkeypatch, tmp_path):
    clear(monkeypatch)
    account = Account.create()
    secret = tmp_path / 'private-key'
    secret.write_text(account.key.hex())
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    monkeypatch.setenv('TRADER_PRIVATE_KEY_FILE', str(secret))
    monkeypatch.setenv('TRADER_WALLET_ADDRESS', account.address)
    monkeypatch.setenv('ROBINHOOD_TRADING_RPC_URL', 'https://example.invalid')
    assert Settings().wallet_address == account.address


@pytest.mark.parametrize(('provider', 'expected'), [
    ('public', 'https://public.example'),
    ('alchemy', 'https://alchemy.example/v2/test-key'),
])
def test_rpc_provider_can_switch_between_public_and_alchemy(monkeypatch, provider, expected):
    clear(monkeypatch)
    monkeypatch.setenv('ROBINHOOD_TRADING_RPC_PROVIDER', provider)
    monkeypatch.setenv('ROBINHOOD_PUBLIC_RPC_URL', 'https://public.example')
    monkeypatch.setenv('ROBINHOOD_ALCHEMY_RPC_URL', 'https://alchemy.example/v2/test-key')
    monkeypatch.setenv('ROBINHOOD_TRADING_RPC_URL', 'https://legacy.example')
    settings = Settings()
    assert settings.rpc_provider == provider
    assert settings.rpc_url == expected


def test_alchemy_provider_requires_its_url(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv('ROBINHOOD_TRADING_RPC_PROVIDER', 'alchemy')
    monkeypatch.setenv('ROBINHOOD_ALCHEMY_RPC_URL', '')
    with pytest.raises(ValueError, match='ROBINHOOD_ALCHEMY_RPC_URL'):
        Settings()
