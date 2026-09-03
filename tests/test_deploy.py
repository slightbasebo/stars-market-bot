from pathlib import Path


def test_install_strips_local_env_and_restarts_running_service():
    script = Path("deploy/install-app.sh").read_text(encoding="utf-8")

    assert 'rm -f "$source_dir/.env"' in script
    assert "systemctl restart stars-market-bot" in script
