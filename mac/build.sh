#!/bin/bash
# Собирает Trade Journal.app — нативную оболочку вокруг локального дневника.
#
# Xcode не нужен: swiftc, iconutil и codesign входят в Command Line Tools.
# Проект остаётся без внешних зависимостей — компилятор Apple и системные
# фреймворки это не «зависимость», которую надо ставить, а часть машины.
#
# Использование:
#   mac/build.sh              # собрать в mac/build/
#   mac/build.sh --install    # собрать и положить в /Applications

set -euo pipefail

cd "$(dirname "$0")/.."
BUILD="mac/build"
APP="$BUILD/Trade Journal.app"

rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "иконка"
swift mac/make-icon.swift "$BUILD/AppIcon.iconset" >/dev/null
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

echo "сборка"
swiftc -O -target arm64-apple-macos13 \
       -o "$APP/Contents/MacOS/TradeJournal" \
       mac/TradeJournal.swift \
       -framework AppKit -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Trade Journal</string>
  <key>CFBundleDisplayName</key>       <string>Trade Journal</string>
  <key>CFBundleIdentifier</key>        <string>com.knokd.trade-journal</string>
  <key>CFBundleExecutable</key>        <string>TradeJournal</string>
  <key>CFBundleIconFile</key>          <string>AppIcon</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key>    <string>13.0</string>
  <key>NSHighResolutionCapable</key>   <true/>

  <!-- Дневник живёт на 127.0.0.1 по обычному HTTP: TLS для петлевого
       интерфейса не нужен, а App Transport Security по умолчанию такие
       загрузки режет. Исключение выдано только петле, не всему миру. -->
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSExceptionDomains</key>
    <dict>
      <key>127.0.0.1</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key> <true/>
      </dict>
      <key>localhost</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key> <true/>
      </dict>
    </dict>
  </dict>
</dict>
</plist>
PLIST

# Подпись ad-hoc: приложение собрано на этой машине и не скачано из сети,
# поэтому карантина на нём нет и нотаризация не требуется. Без подписи вовсе
# macOS на Apple Silicon бинарник не запустит.
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1

rm -rf "$BUILD/AppIcon.iconset"
echo "собрано: $APP"

if [[ "${1:-}" == "--install" ]]; then
  rm -rf "/Applications/Trade Journal.app"
  cp -R "$APP" /Applications/
  echo "установлено: /Applications/Trade Journal.app"
fi
