#!/usr/bin/env bash
#
# Установка ежедневного трекера земельных тендеров на сервер (Ubuntu/Debian).
# Запускать от root на самом сервере:
#
#   sudo bash deploy/install-landtender.sh
#
# Скрипт идемпотентный: повторный запуск обновляет код и юниты, базу и
# /etc/landtender.env не трогает.
#
# Каталог намеренно НЕ /opt/usreal — там живёт отчёт по расходам из этого же
# репозитория, но с другой ветки. Две программы в одном каталоге затирали бы
# друг другу код при обновлении.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/vadimsheinberg-beep/USREAL.git}"
BRANCH="${BRANCH:-claude/israel-land-tenders-tracker-sz1k6y}"
APP_DIR="${APP_DIR:-/opt/landtender}"
ENV_FILE="/etc/landtender.env"
SERVICE_USER="landtender"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0" >&2
  exit 1
fi

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> Пользователь $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  # Системный пользователь без входа в систему: трекеру нужны только диск и сеть
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Код в $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
else
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Виртуальное окружение"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

echo "==> Конфигурация"
if [[ ! -f "$APP_DIR/landtender.toml" ]]; then
  cp "$APP_DIR/landtender.example.toml" "$APP_DIR/landtender.toml"
  echo "    создан $APP_DIR/landtender.toml"
fi
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/landtender.toml"

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 -o root -g root "$APP_DIR/deploy/landtender.env.example" "$ENV_FILE"
  echo "    создан $ENV_FILE — впишите токен бота и id канала"
fi
# Юнит читает файл от имени $SERVICE_USER, root-only здесь не годится
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> systemd"
install -m 644 "$APP_DIR/deploy/landtender.service" /etc/systemd/system/landtender.service
install -m 644 "$APP_DIR/deploy/landtender.timer" /etc/systemd/system/landtender.timer
systemctl daemon-reload
systemctl enable --now landtender.timer

echo
echo "Готово. Дальше:"
echo "  1. впишите токен и канал: sudo nano $ENV_FILE"
echo "  2. проверьте Telegram:    sudo -u $SERVICE_USER $APP_DIR/.venv/bin/landtender \\"
echo "                              --config $APP_DIR/landtender.toml telegram-test"
echo "  3. наполнить базу молча:  sudo systemd-run --pipe --wait \\"
echo "                              --property=EnvironmentFile=$ENV_FILE \\"
echo "                              --uid=$SERVICE_USER $APP_DIR/.venv/bin/landtender \\"
echo "                              --config $APP_DIR/landtender.toml run --no-notify"
echo "     (в первый раз новыми считаются все лоты — сводку слать незачем)"
echo "  4. прогон со сводкой:     sudo systemctl start landtender.service"
echo "  5. журнал последнего:     journalctl -u landtender.service -n 50 --no-pager"
echo "  6. следующий запуск:      systemctl list-timers landtender.timer"
