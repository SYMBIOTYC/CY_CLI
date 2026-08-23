# CY-CLI Releases

Бинарные релизы CY-CLI для всех платформ.

## Установка

### macOS / Linux (self-updating installer v2)

```bash
curl -fsSL https://raw.githubusercontent.com/SYMBIOTYC/CY-CLI-releases/main/install-v2.sh | bash
```

### Windows (self-updating installer v2)

```powershell
irm https://raw.githubusercontent.com/SYMBIOTYC/CY-CLI-releases/main/install-v2.ps1 | iex
```

### macOS (.dmg)

Скачайте `.dmg` из [Releases](https://github.com/SYMBIOTYC/CY-CLI/releases) и перетащите `CY-CLI.app` в `/Applications`.

## Поддерживаемые платформы

- Linux: `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`
- macOS: `x86_64-apple-darwin`, `aarch64-apple-darwin`
- Windows: `x86_64-pc-windows-msvc`, `aarch64-pc-windows-msvc`

## Проверка

```bash
cy --version
cy m
cy ls
cy hist
```

## Лицензия

Apache-2.0
