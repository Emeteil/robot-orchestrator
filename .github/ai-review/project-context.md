# Project context
`robot-orchestrator` — система развёртывания, обновления и супервизии для робота-консультанта «Избушка» на SBC Orange Pi 5 Plus. Отвечает за: приём секретов через QR, атомарные обновления репозиториев (`izbushka-web-core`, `izbushka-voice-interface`, `com-link-RT`) и их сабмодулей, прошивку и верификацию STM32 через ST-Link/OpenOCD, определение prod/non-prod режима, запуск и супервизию сервисов, kiosk-браузер и веб-админку.

## What to review and what to ignore
Ревьюить: `robot_orchestrator/`, `tools/`, `tests/`, `install/install.sh`.
Игнорировать: `.github/`, CI-конфиги, `scenarios/*.yml` (фикстуры для симуляции, не код).
Если в диффе нет ревьюабельного кода — так и напиши в summary, не выдумывай замечания.

## Always read the PR description and comments
Перед ревью прочитай PR DESCRIPTION из PR / GIT CONTEXT и комментарии в pr-comments/others/. Не поднимай повторно то, что там уже решено или объяснено; объяснение снимает придирку, но не отменяет реальный баг.

## Stack
Python 3.11+, FastAPI/uvicorn (веб-админка и boot-страница), pydantic v2 (конфиги, схемы), SQLite (WAL-журнал и состояние), asyncio (супервизор процессов), httpx (GitHub API, HTTP-пробы), PyJWT, gpiod v2 (GPIO), OpenCV/pyzbar (QR со камеры), sounddevice (микрофон).

## Code style
CI использует flake8 (`.github/workflows/lint.yml`, конфиг в `setup.cfg`: `max-line-length = 140`). Кодовая база написана без существенных комментариев в коде, кроме редких однострочных пояснений неочевидного WHY и `# noqa` для линтера — это осознанное решение проекта, не повод для замечания «добавьте комментарии/docstring».

## Architecture and patterns
- `wal/` — журнал операций (`intent → prepared → committing → committed/rolled_back/abandoned`) и восстановление после обрыва питания на любой фазе. Любая мутирующая операция (git-обновление, запись секретов, прошивка MCU) обязана идти через `Journal`.
- `hal/` — Protocol-интерфейсы (`hal/base.py`) с реальными (`hal/real/`) и фейковыми (`hal/fake/`) реализациями, выбираемыми через `hal/factory.build_hal()`. Реальные реализации лениво импортируют аппаратные библиотеки (`cv2`, `gpiod`, `sounddevice`), чтобы пакет импортировался и без них.
- `repos/manager.py` — атомарные git-обновления: fetch в mirror → stage в отдельный каталог → validate → атомарный rename + переключение `ReleasePointer` (symlink на POSIX, файл-указатель на Windows).
- `firmware/workflow.py` — состояние прошивки MCU: DETECT → DECIDE → ACQUIRE (CI-артефакт GitHub Actions → локальная сборка PlatformIO) → FLASH (OpenOCD) → VERIFY (сравнение версии) → RECORD.
- `boot/decision.py` — чистая функция `decide_mode(facts) -> ModeDecision`: prod/non-prod и capabilities (`kiosk_browser`, `wake_word`, `voice_interface`) выводятся из списка причин (`camera_unavailable`, `mic_unavailable`, `internet_down`, `stlink_missing`, `mcu_missing`, `secrets_missing:<KEY>`, `repo_update_failed:<repo>`, `mcu_flash_failed`, `mcu_verify_failed`, `mcu_client_incompatible`, `firmware_unavailable`, `gpio_forced`).
- `supervisor/` — асинхронный супервизор процессов с backoff/crash-loop, readiness-пробами, device leases и `enabled_when` по capabilities.
- `secrets/protocol.py` — кодирование/сборка секретов через 1..N QR-кодов (`RO1:k:n:digest:base45`); `PartialAssembly.add_part` обязан не бросать исключений на произвольном/битом входе с камеры.
- `network/wifi.py` — обёртка над `nmcli` (инжектируемый runner, как у git/openocd) для авто-подключения к известным Wi-Fi сетям при `internet_down`.
- `selfupdate/manager.py` — самообновление оркестратора: авто-стейджинг+валидация на старте (никогда не трогает live-состояние), применение (атомарный флип `self/current`+`self/current-venv`, свой WAL `op_type: self_update_swap`) — только по явному вызову (`apply`), никогда автоматически.
- `web/context.py:AdminContext` — держит живые ссылки (supervisor/repo_managers/firmware_workflows/db) для `web/routes_api.py`/`web/routes_sse.py`; когда `context` не передан, эти маршруты обязаны отвечать `503`, а не падать.

## Dependencies on other parts of the system
- [izbushka-web-core](https://github.com/Emeteil/izbushka-web-core), [izbushka-voice-interface](https://github.com/Emeteil/izbushka-voice-interface), [com-link-RT](https://github.com/Emeteil/COM-LINK-RT) — управляемые репозитории (клонируются, обновляются, супервизируются этим оркестратором).
- `com_link_rt` (сабмодуль web-core) — используется **только** через venv и коммит, закреплённый внутри самого web-core (`firmware/mcu_client.py` → `workers/mcu_probe.py`), никогда напрямую и никогда другой версией.
- GitHub Actions API — скачивание артефактов прошивки (`firmware/github_artifacts.py`); токен никогда не должен уходить на хост, куда ведёт redirect скачивания.

## Review checklist
- Любая новая мутация состояния/файлов без прохождения через `Journal` (intent/prepared/committing/committed) — вопрос атомарности при обрыве питания, это ядро требований проекта.
- HAL-реализации в `hal/real/*`, импортирующие аппаратные библиотеки на уровне модуля, а не лениво внутри функции — ломает импортируемость пакета без железа.
- Любая функция, читающая внешние/недоверенные данные (QR с камеры, ответ MCU, ответ GitHub API) и способная бросить необработанное исключение вместо возврата `None`/ошибки — эта кодовая база специально спроектирована не крашиться на кривом внешнем входе.
- `GithubArtifactClient.download` — если `Authorization`-заголовок попадёт в запрос после редиректа на другой хост, это утечка токена, критичная находка.
- Новый импорт без добавления зависимости в `pyproject.toml`/`requirements.txt`, либо удалённая зависимость, оставшаяся в файле.
- Секреты/токены в коде, логах или в git-журнале (`journal.payload` — там могут быть только пути/хеши, никогда значения секретов).
- Комментарии/docstring, добавленные не по WHY-исключению — противоречит стилю проекта (см. "Code style" выше), не обязательно замечание, но стоит отметить как несоответствие конвенции.
