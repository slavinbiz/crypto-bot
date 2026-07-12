import json
import crypto_bot
import ema_trend


def test_load_settings_reads_ema_threshold(tmp_path, monkeypatch):
    settings_file = tmp_path / "bot_settings.json"
    settings_file.write_text(json.dumps({"EMA_DISTANCE_THRESHOLD_PCT": 1.2}))
    monkeypatch.setattr(crypto_bot, "SETTINGS_FILE", str(settings_file))
    crypto_bot.load_settings()
    assert ema_trend.EMA_DISTANCE_THRESHOLD_PCT == 1.2


def test_save_settings_writes_ema_threshold(tmp_path, monkeypatch):
    settings_file = tmp_path / "bot_settings.json"
    monkeypatch.setattr(crypto_bot, "SETTINGS_FILE", str(settings_file))
    ema_trend.EMA_DISTANCE_THRESHOLD_PCT = 0.8
    crypto_bot.save_settings()
    saved = json.loads(settings_file.read_text())
    assert saved["EMA_DISTANCE_THRESHOLD_PCT"] == 0.8
