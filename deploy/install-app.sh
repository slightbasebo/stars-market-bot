#!/bin/sh
set -eu

release_id="${1:?release id required}"
source_dir="/tmp/stars-market-bot-${release_id}"
release_dir="/opt/stars-market-bot-releases/${release_id}"

rm -f "$source_dir/.env"
rm -rf "$release_dir"
mv "$source_dir" "$release_dir"
chown -R root:root "$release_dir"
chmod 755 "$release_dir"
python3 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/pip" install -q --disable-pip-version-check "$release_dir"
ln -sfn "$release_dir" /opt/stars-market-bot

install -o root -g root -m 644 \
    /opt/stars-market-bot/deploy/stars-market-bot.service \
    /etc/systemd/system/stars-market-bot.service

systemd-analyze verify /etc/systemd/system/stars-market-bot.service
systemctl daemon-reload
systemctl enable stars-market-bot
systemctl restart stars-market-bot
sleep 8
systemctl is-active --quiet stars-market-bot
echo DEPLOYED
