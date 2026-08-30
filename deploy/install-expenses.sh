#!/usr/bin/env bash
#
# Установка месячного отчёта по расходам на сервер (Contabo, Ubuntu/Debian).
# Запускать от root на самом сервере:
#
#   sudo bash deploy/install-expenses.sh
#
# Скрипт идемпотентный: повторный запуск обновляет код и юниты, данные и
# /etc/expenses.env не трогает.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/vadimsheinberg-beep/USREAL.git}"
BRANCH="${BRANCH:-claude/personal-expense-analyzer-rnewlb}"
APP_DIR="${APP_DIR:-/opt/usreal}"
ENV_FILE="/etc/expenses.env"
SERVICE_USER="expenses"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0" >&2
  exit 1
fi

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> Пользователь $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  # Системный пользователь без входа в систему: отчёту нужен только диск и сеть
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
if [[ ! -f "$APP_DIR/expenses.toml" ]]; then
  cp "$APP_DIR/expenses.example.toml" "$APP_DIR/expenses.toml"
  echo "    создан $APP_DIR/expenses.toml — включите нужные источники"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/data/inbox"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/expenses.toml"
# В data лежат все ваши операции — читать их посторонним незачем
chmod 700 "$APP_DIR/data"

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 -o root -g root "$APP_DIR/deploy/expenses.env.example" "$ENV_FILE"
  echo "    создан $ENV_FILE — впишите токены и перезапустите скрипт"
fi
# Юнит читает файл от имени $SERVICE_USER, root-only здесь не годится
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> systemd"
install -m 644 "$APP_DIR/deploy/expenses.service" /etc/systemd/system/expenses.service
install -m 644 "$APP_DIR/deploy/expenses.timer" /etc/systemd/system/expenses.timer
systemctl daemon-reload
systemctl enable --now expenses.timer

echo
echo "Готово. Дальше:"
echo "  1. впишите токены:      sudo nano $ENV_FILE"
echo "  2. включите источники:  sudo nano $APP_DIR/expenses.toml"
echo "  3. проверьте Telegram:  sudo -u $SERVICE_USER $APP_DIR/.venv/bin/expenses \\"
echo "                            --config $APP_DIR/expenses.toml telegram-test"
echo "  4. прогон без отправки: sudo systemd-run --pipe --wait \\"
echo "                            --property=EnvironmentFile=$ENV_FILE \\"
echo "                            --uid=$SERVICE_USER $APP_DIR/.venv/bin/expenses \\"
echo "                            --config $APP_DIR/expenses.toml monthly --dry-run"
echo "  5. следующий запуск:    systemctl list-timers expenses.timer"
