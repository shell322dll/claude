import os
import re
import numpy as np
import cv2
from dataclasses import dataclass
from typing import Optional, List
from paddleocr import PaddleOCR

# Импортируем разрешённые префиксы (если config.py доступен)
try:
    from config import ALLOWED_SERIAL_PREFIXES
except ImportError:
    ALLOWED_SERIAL_PREFIXES = ["PC", "CE"]  # Дефолтное значение

DIGIT_SUBS = str.maketrans({
    'O':'0', 'o':'0', 'I':'1', 'l':'1', 'L':'1', 'i':'1', 'B':'8', 'S':'5', 'Z':'2',
})

def normalize_line(s: str) -> str:
    if s is None:
        return ""
    s = s.replace('\\', '/')
    s = s.replace('\u2013', '-')
    s = re.sub(r'[^A-Za-z0-9\s/:.\-]', ' ', s)
    return s.upper().strip()

def compact(s: str) -> str:
    return re.sub(r'[\s.:/\\\-]', '', s)

def fix_digits_mistakes(s: str) -> str:
    return s.translate(DIGIT_SUBS)

def is_valid_serial(sn: str) -> bool:
    """
    Проверяет валидность серийного номера:
    - Формат: 5 букв + 9 цифр
    - Префикс: ТОЛЬКО из списка ALLOWED_SERIAL_PREFIXES (по умолчанию: PC, CE)
    """
    if not re.fullmatch(r'[A-Z]{5}[0-9]{9}', sn):
        return False
    
    prefix = sn[:2]
    return prefix in ALLOWED_SERIAL_PREFIXES

def compute_bios_password_string(serial: str) -> str:
    if not is_valid_serial(serial):
        raise ValueError("Сериал не валидный для вычисления пароля")
    first_two = serial[:2]
    digits = serial[5:]
    number1 = int(digits[:3])
    number2 = int(digits[-3:])
    product = number1 * number2
    return f"{first_two}{product}"

def find_serial_near_sn_in_text(text: str) -> Optional[str]:
    norm = normalize_line(text)
    comp = compact(norm)

    # ИСПРАВЛЕНО: экранирование спецсимволов
    pat1 = re.compile(r'\bS[\s/\\\.\-]*N[\s:]*([A-Z0-9]{14})\b', re.IGNORECASE)
    pat2 = re.compile(r'\bSN[\s:]*([A-Z0-9]{14})\b', re.IGNORECASE)
    
    for pat in (pat1, pat2):
        for m in pat.finditer(norm):
            candidate_raw = m.group(1)
            letters_part = candidate_raw[:5]
            digits_part_raw = candidate_raw[5:]
            digits_fixed = fix_digits_mistakes(digits_part_raw)
            candidate = (letters_part + digits_fixed).upper()
            if is_valid_serial(candidate):
                return candidate

    m2 = re.search(r'(?:SN|S5N|5N)([A-Z0-9]{14})', comp, re.IGNORECASE)
    if m2:
        candidate_raw = m2.group(1)
        letters_part = candidate_raw[:5]
        digits_part_raw = candidate_raw[5:]
        digits_fixed = fix_digits_mistakes(digits_part_raw)
        candidate = (letters_part + digits_fixed).upper()
        if is_valid_serial(candidate):
            return candidate

    for m in re.finditer(r'\bS[\s/\\\.\-]*N\b|\bSN\b|\bS5N\b|\b5N\b', norm, re.IGNORECASE):
        start = m.end()
        window = norm[start:start + 80]
        joined = compact(window)
        mo = re.search(r'([A-Z0-9]{14})', joined)
        if mo:
            candidate_raw = mo.group(1)
            letters_part = candidate_raw[:5]
            digits_part_raw = candidate_raw[5:]
            digits_fixed = fix_digits_mistakes(digits_part_raw)
            candidate = (letters_part + digits_fixed).upper()
            if is_valid_serial(candidate):
                return candidate

    return None

def find_any_serial_in_text(text: str) -> Optional[str]:
    norm = normalize_line(text)
    
    m = re.search(r'([A-Z]{5}[0-9]{9})', norm)
    if m:
        return m.group(1).upper()

    for m in re.finditer(r'([A-Z]{5}[A-Z0-9]{9})', norm):
        letters = m.group(1)[:5]
        digits_raw = m.group(1)[5:]
        digits_fixed = fix_digits_mistakes(digits_raw)
        candidate = (letters + digits_fixed).upper()
        if is_valid_serial(candidate):
            return candidate

    comp = compact(norm)
    mo = re.search(r'([A-Z]{5}[A-Z0-9]{9})', comp)
    if mo:
        letters = mo.group(1)[:5]
        digits_raw = mo.group(1)[5:]
        digits_fixed = fix_digits_mistakes(digits_raw)
        candidate = (letters + digits_fixed).upper()
        if is_valid_serial(candidate):
            return candidate
    
    return None

def preprocess(img: np.ndarray) -> np.ndarray:
    """Предобработка изображения для улучшения OCR"""
    h, w = img.shape[:2]
    max_side = max(h, w)
    
    # Апскейл маленьких изображений
    if max_side < 1100:
        scale = min(1600 / max_side, 3.0)
        if scale > 1.05:
            img = cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_CUBIC)
    
    # Лёгкий шарпинг
    blur = cv2.GaussianBlur(img, (0, 0), 1.0)
    img = cv2.addWeighted(img, 1.5, blur, -0.5, 0)
    
    return img

@dataclass
class AnalyzeResult:
    found: bool
    serial: Optional[str] = None
    password: Optional[str] = None
    debug_text: Optional[str] = None

class AnalyzerSNService:
    def __init__(self, use_gpu: bool = False):
        self.ocr = PaddleOCR(
            use_angle_cls=True,
            lang='latin',
            show_log=False,
            det_limit_side_len=1920,
            rec_score_thresh=0.5,
        )

    def analyze_bytes(self, image_bytes: bytes) -> AnalyzeResult:
        """Анализирует изображение и ищет серийный номер"""
        try:
            arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            
            if img is None:
                return AnalyzeResult(found=False, debug_text="Не удалось декодировать изображение")

            img = preprocess(img)
            ocr_res = self.ocr.ocr(img, cls=True)

            texts: List[str] = []
            if isinstance(ocr_res, list):
                for page in ocr_res:
                    if not isinstance(page, list):
                        continue
                    words = []
                    for det in page:
                        try:
                            t = det[1][0]
                            if t:
                                words.append(str(t))
                        except (IndexError, TypeError, KeyError):
                            continue
                    if words:
                        texts.append(" ".join(words))

            full_text = "\n".join(texts)
            
            # Поиск серийного номера
            serial = find_serial_near_sn_in_text(full_text)
            if not serial:
                serial = find_any_serial_in_text(full_text)

            if serial:
                password = compute_bios_password_string(serial)
                return AnalyzeResult(found=True, serial=serial, password=password)

            # Не нашли
            if texts:
                dbg = "Не найден S/N. Распознанные строки:\n" + "\n".join(f"[{i+1:02d}] {t}" for i, t in enumerate(texts[:10]))
            else:
                dbg = "OCR не распознал текст на изображении."
            
            return AnalyzeResult(found=False, debug_text=dbg)
            
        except Exception as e:
            return AnalyzeResult(found=False, debug_text=f"Ошибка при анализе: {str(e)}")

# Создаём синглтон сервиса
service = AnalyzerSNService(use_gpu=bool(int(os.getenv("OCR_USE_GPU", "0"))))