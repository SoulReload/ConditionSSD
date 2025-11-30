import subprocess
import json
import smtplib
import socket
import logging
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
# Импортируем исключение таймаута для его обработки
from subprocess import TimeoutExpired 

# --- НАСТРОЙКИ ---

# 1. Основные параметры
THRESHOLD_PERCENT = 60       # Если здоровья меньше 60%, бьем тревогу
LOG_FILE = 'disk_monitor.log'
SMARTCTL_CMD = "smartctl" 

# 2. Настройки почты (Безопасный метод)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
ADMIN_EMAIL = "admin@yourcompany.com" 

# Берем учетные данные из переменных среды
SMTP_USER = os.getenv('DISK_MONITOR_USER') 
SMTP_PASSWORD = os.getenv('DISK_MONITOR_PASS') 

# --- НАСТРОЙКА ЛОГОВ ---
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def check_setup():
    """Проверка перед запуском: есть ли пароли и программа"""
    if not SMTP_USER or not SMTP_PASSWORD:
        error_msg = (
            "ОШИБКА БЕЗОПАСНОСТИ: Не найдены логин или пароль в переменных среды.\n"
            "Пожалуйста, создайте переменные среды 'DISK_MONITOR_USER' и 'DISK_MONITOR_PASS'."
        )
        print(error_msg)
        logging.critical("Credentials not found in environment variables. Script exiting.")
        sys.exit(1)

def get_machine_info():
    """Узнаем имя компьютера"""
    return socket.gethostname()

def run_smartctl_scan():
    """Ищем диски в системе"""
    try:
        cmd = [SMARTCTL_CMD, "--scan", "--json"]
        # УЛУЧШЕНИЕ: Добавлен timeout и кодировка
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=45, 
            encoding='utf-8'
        )
        
        if result.returncode != 0:
            # УЛУЧШЕНИЕ: Логируем детальный вывод ошибки
            logging.error(f"smartctl вернул ошибку при сканировании. Код: {result.returncode}. Вывод: {result.stderr.strip()}")
            return []
            
        data = json.loads(result.stdout)
        return data.get('devices', [])
        
    except FileNotFoundError:
        print(f"ОШИБКА: Программа {SMARTCTL_CMD} не найдена. Установите Smartmontools.")
        logging.critical("Smartmontools not found.")
        sys.exit(1)
    except TimeoutExpired:
        logging.error("Превышено время ожидания (45 сек) при сканировании дисков.")
        return []
    except json.JSONDecodeError:
        logging.error("Ошибка чтения JSON от smartctl.")
        return []

def get_disk_health_data(device_name):
    """Спрашиваем у конкретного диска его состояние"""
    try:
        cmd = [SMARTCTL_CMD, "--all", "--json", device_name]
        # УЛУЧШЕНИЕ: Добавлен timeout и кодировка
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            check=False, 
            timeout=60, # Немного больше времени для чтения данных
            encoding='utf-8'
        )
        return json.loads(result.stdout)
    except TimeoutExpired:
        logging.error(f"Превышено время ожидания (60 сек) при чтении данных диска {device_name}.")
        return None
    except Exception as e:
        logging.error(f"Ошибка проверки диска {device_name}: {e}")
        return None

def analyze_health(smart_json):
    """
    Анализируем JSON и пытаемся понять процент жизни.
    """
    if not smart_json:
        return 0, "Read Error"

    smart_status = smart_json.get('smart_status', {}).get('passed')
    if smart_status is False:
        return 0, "SMART FAILED (CRITICAL)"

    # NVMe SSD
    if smart_json.get('device', {}).get('type') == 'nvme':
        nvme_log = smart_json.get('nvme_smart_health_information_log', {})
        used = nvme_log.get('percentage_used')
        if used is not None:
            return 100 - used, "NVMe Health"

    # SATA SSD (ищем специфические атрибуты)
    ata_attrs = smart_json.get('ata_smart_attributes', {}).get('table', [])
    for attr in ata_attrs:
        id_num = attr.get('id')
        if id_num in [177, 231, 233]:
            return attr.get('value'), f"SATA Attribute {id_num}"

    # HDD или неизвестный тип
    return 100, "SMART Passed (No % data)"

def send_email_alert(hostname, disk_name, health, reason):
    """Отправка письма"""
    subject = f"⚠️ WARNING: Низкое здоровье диска на {hostname}"
    
    body = f"""
    ВНИМАНИЕ!
    
    Компьютер: {hostname}
    Диск: {disk_name}
    Состояние: {health}%
    Причина: {reason}
    
    Требуется проверка администратора.
    """

    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = ADMIN_EMAIL
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logging.info(f"Письмо отправлено для диска {disk_name}")
        print("  -> Письмо отправлено администратору.")
    except Exception as e:
        logging.error(f"Не удалось отправить письмо: {e}", exc_info=True)
        print(f"  -> Ошибка отправки почты: {e}")

def main():
    # УЛУЧШЕНИЕ: Весь код обернут в try/except для глобального отлова ошибок
    try:
        # 1. Проверяем переменные среды
        check_setup()
        
        hostname = get_machine_info()
        print(f"--- Запуск проверки на {hostname} ---")
        
        # 2. Ищем диски
        devices = run_smartctl_scan()
        if not devices:
            print("Диски не найдены (возможно нужны права Администратора).")
            return

        # 3. Проходим по каждому диску
        for dev in devices:
            name = dev.get('name')
            print(f"Проверка: {name}...")
            
            data = get_disk_health_data(name)
            health_percent, reason = analyze_health(data)
            
            print(f"  -> Здоровье: {health_percent}% ({reason})")

            # 4. Если здоровье ниже порога - шлем письмо
            if health_percent < THRESHOLD_PERCENT:
                logging.warning(f"Disk {name} LOW HEALTH: {health_percent}%")
                send_email_alert(hostname, name, health_percent, reason)

    except Exception as main_e:
        # ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
        error_message = f"КРИТИЧЕСКАЯ НЕОЖИДАННАЯ ОШИБКА: {main_e.__class__.__name__}: {main_e}"
        print(f"❌ {error_message}")
        # exc_info=True позволяет записать полный стек вызовов в лог
        logging.critical(error_message, exc_info=True)

if __name__ == "__main__":
    main()