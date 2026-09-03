# MCS Glove

Программный интерфейс и приложения для управления MCS Glove — устройством для реабилитации с восемью независимо управляемыми каналами вибрации.

## Возможности

- Подключение к MCS Glove по Bluetooth Low Energy
- Независимое управление восемью каналами вибрации
- Непрерывная вибрация с плавным нарастанием и спадом амплитуды
- Воспроизведение эффектов встроенной библиотеки драйвера DRV2605L
- Проведение сеансов стимуляции по встроенным и пользовательским сценариям
- Диагностика вибромоторов и автоматическая калибровка драйверов
- Контроль состояния аккумулятора
- Обновление встроенного программного обеспечения устройства по BLE

## Установка и настройка

### Запуск из исходного кода 

Если вы хотите запустить приложение из исходного кода или внести изменения:

**Требования**

- Python 3.10 или выше
- Git (для клонирования репозитория)

**Шаги по установке**

1. Клонируйте репозиторий:

```
git clone git@github.com:mcsltd/mcs_glove.git
```

2. Создайте виртуальное окружение:

```
python -m venv venv
venv\Scripts\activate
```

3. Установите зависимости:

```
pip install -r requirements.txt
```

4. Запустите приложение пользователя:

```
python userapp.py
```

Для запуска инструмента разработчика используйте `python devtool.py`.

## Использование программного интерфейса

Устройство управляется из собственных программ через класс `GloveClient`. Все методы блокирующие, поэтому в приложениях с графическим интерфейсом их следует вызывать из рабочих потоков.

```python
import time
from glove_client import GloveClient
from models import Finger

glove = GloveClient()

try:
    glove.connect(name="MCS Glove", timeout=10.0)
    print(glove.device_info["model"], glove.device_info["fw_rev"])

    batt = glove.battery_info()
    print(f"Заряд {batt['percent']} %, {batt['voltage_mv']} мВ")

    glove.vibration_on(Finger.INDEX, intensity_pct=70)
    time.sleep(2.0)
    glove.vibration_off(Finger.INDEX)

    glove.tick(Finger.MIDDLE, effect_id=1)

except (RuntimeError, ValueError) as e:
    print(f"Ошибка: {e}")

finally:
    glove.all_off()
    glove.close()
```

Полное описание методов, структур данных и устройства приложения пользователя приведено в руководстве прикладного программиста.

## Copyrights

Copyright © 2026, Medical Computer Systems Ltd

## Лицензия

Этот проект лицензирован под [MIT License](https://github.com/mcsltd/mcs_glove/blob/main/LICENSE).

## Используемые сторонние библиотеки

- PySide6 (Qt for Python) используется под лицензией LGPL
- bleak используется под лицензией MIT
- Другие библиотеки под MIT, BSD, Apache 2.0

Подробности в [NOTICE](https://github.com/mcsltd/mcs_glove/blob/main/NOTICE).
