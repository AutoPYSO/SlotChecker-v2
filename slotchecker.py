"""
Zbiorczy checker slotów IKEA – 4 usługi:

1) Plan w sklepie
2) Fin w sklepie
3) Online (Plan / Fin) – RCMP
4) PUK – Planowanie kuchni w domu klienta

Na starcie:
- wybierasz usługę
- wybierasz jednostkę z listy (nazwa + kod pocztowy) lub "wszystkie"

Wszystko zapisuje / dopisuje do:
    ikea_slots_results.csv

Interpretacja kolumn:
- 'slot_any_first'     = liczba dni do najbliższego slotu (dowolnego):
                           0  -> slot dziś,
                           1+ -> slot w przyszłości,
                           26 -> brak slotów w ogóle (dziś i w oknie analizy).
- 'slot_16plus_first'  = liczba dni do najbliższego slotu >=16:00:
                           0  -> slot >=16 dziś,
                           1+ -> slot >=16 w przyszłości,
                           26 -> brak slotów >=16 lub brak slotów w ogóle.
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from datetime import datetime, date
from typing import Optional, List, Dict
import pandas as pd
import time
import logging
import os
import sys

# ---------- LOGOWANIE WSPÓLNE ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

RESULT_CSV = "ikea_slots_results.csv"
SENTINEL_NO_SLOT = 26


# ---------- FUNKCJA CZYSZCZĄCA KOMUNIKAT BŁĘDU ----------

def sanitize_error_message(msg: str, max_len: int = 300) -> str:
    """
    Czyści komunikat błędu, żeby nie psuł struktury CSV:
    - usuwa entery (\n, \r),
    - zamienia je na ' | ',
    - usuwa cudzysłowy podwójne,
    - przycina bardzo długie stacktrace'y.
    """
    if msg is None:
        return ""
    msg = str(msg)

    # Usuń CR, zamień LF na separator
    msg = msg.replace("\r", " ").replace("\n", " | ")

    # Usuń podwójne cudzysłowy, żeby nie psuły cytowania CSV
    msg = msg.replace('"', "'")

    msg = msg.strip()

    if len(msg) > max_len:
        msg = msg[:max_len] + "..."

    return msg


def sanitize_location_name(name: str) -> str:
    """
    Czyści nazwę lokalizacji tak, żeby była bezpieczna dla CSV:
    - usuwa przecinki (dzięki temu nie trzeba cudzysłowów w CSV),
    - obcina spacje na brzegach.
    Przykład:
        "Studio ..., Warszawie, Wola Park" -> "Studio ..., Warszawie Wola Park"
    """
    if not name:
        return ""
    return name.replace(",", "").strip()


# ======================================================================
# BAZOWA KLASA Z WSPÓLNYMI FUNKCJAMI
# ======================================================================

class BaseIkeaChecker:
    def __init__(self, headless: bool = True):
        self.driver = None
        self.headless = headless

    # ---------- DRIVER ----------

    def setup_driver(self):
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=pl-PL")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        self.driver = webdriver.Chrome(options=options)
        if not self.headless:
            self.driver.maximize_window()
        logger.info("WebDriver uruchomiony")

    def teardown_driver(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
        logger.info("WebDriver zamknięty")

    # ---------- POPUPY / MODALE ----------

    def close_possible_popups(self):
        """Zamyka widoczne modale/popupy blokujące kliknięcia."""
        try:
            modal_selectors = [
                "//div[contains(@class,'nfw-modal')]",
                "//div[contains(@class,'nfw-modal-body')]",
                "//div[contains(@class,'modal') and not(contains(@style,'display: none'))]",
            ]
            modals = []
            for sel in modal_selectors:
                try:
                    found = self.driver.find_elements(By.XPATH, sel)
                    modals.extend(found)
                except Exception:
                    continue

            modals = [m for m in modals if m.is_displayed()]
            if not modals:
                return

            logger.info(f"Wykryto {len(modals)} potencjalnych popupów – próbuję zamknąć")

            for modal in modals:
                try:
                    close_btn = None
                    close_selectors = [
                        ".//button[contains(@class,'close')]",
                        ".//button[contains(@aria-label,'Zamknij') or contains(@aria-label,'Close')]",
                        ".//button[contains(text(),'Zamknij')]",
                        ".//button[contains(text(),'Nie teraz')]",
                        ".//button[contains(text(),'OK')]",
                        ".//button[contains(text(),'Rozumiem')]",
                    ]
                    for csel in close_selectors:
                        try:
                            btns = modal.find_elements(By.XPATH, csel)
                            btns = [b for b in btns if b.is_displayed()]
                            if btns:
                                close_btn = btns[0]
                                break
                        except Exception:
                            continue

                    if close_btn:
                        try:
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block: 'center'});",
                                close_btn,
                            )
                        except Exception:
                            pass

                        try:
                            close_btn.click()
                        except Exception:
                            try:
                                self.driver.execute_script("arguments[0].click();", close_btn)
                            except Exception:
                                continue

                        logger.info("Popup zamknięty")
                        time.sleep(0.3)
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Błąd w close_possible_popups: {e}")

    # ---------- COOKIES ----------

    def accept_cookies(self, short_wait: bool = False):
        timeout = 2 if short_wait else 3
        selectors = [
            "//button[@id='onetrust-accept-btn-handler']",
            "//button[contains(text(), 'Akceptuj')]",
            "//button[contains(text(), 'Akceptuję')]",
            "//button[contains(text(), 'Zgadzam się')]",
        ]
        for sel in selectors:
            try:
                btn = WebDriverWait(self.driver, timeout).until(
                    EC.element_to_be_clickable((By.XPATH, sel))
                )
                btn.click()
                logger.info("Cookies zaakceptowane")
                time.sleep(0.3)
                return
            except Exception:
                continue
        logger.debug("Brak bannera cookies")

    # ---------- KALENDARZ – CZEKAJ NA DNI ----------

    def wait_for_calendar_days(self, timeout: int = 10) -> bool:
        """
        Czeka aż w kalendarzu pojawią się przyciski dni (Week__StyledDateButton).
        Zwraca True jeśli są dni, False jeśli po czasie nadal brak przycisków.
        """
        try:
            self.close_possible_popups()
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_all_elements_located(
                    (By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]")
                )
            )
            logger.info("Dni w kalendarzu dostępne (Week__StyledDateButton)")
            return True
        except TimeoutException:
            day_buttons = self.driver.find_elements(
                By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]"
            )
            if day_buttons:
                logger.info("Dni kalendarza pojawiły się dopiero po timeoucie, kontynuuję")
                return True
            logger.warning("Brak przycisków dni w kalendarzu")
            return False

    # ---------- SLOTY ----------

    def _parse_hour_to_float(self, text: str) -> Optional[float]:
        """Parsuje godzinę początku slotu z napisu '12:00 - 13:30' -> 12.0"""
        try:
            text = text.strip()
            for part in text.split():
                if ":" in part:
                    h, m = part.split(":")
                    return int(h) + int(m) / 60.0
            return None
        except Exception:
            return None

    def _get_slots_for_current_day(self) -> List[float]:
        """Zwraca listę godzin (float) dla aktualnie zaznaczonego dnia.
        Liczy tylko AKTYWNE (niedisabled) sloty.
        """
        try:
            self.close_possible_popups()

            slot_buttons = self.driver.find_elements(
                By.XPATH,
                "//button[contains(@data-cy,'time-slot-') "
                "and not(@disabled) and not(@aria-disabled='true')]",
            )

            slots: List[float] = []
            for btn in slot_buttons:
                if not btn.is_displayed():
                    continue

                disabled_attr = btn.get_attribute("disabled")
                aria_disabled = btn.get_attribute("aria-disabled")
                if disabled_attr is not None or (aria_disabled and aria_disabled.lower() == "true"):
                    continue

                text = (btn.text or "").strip()
                if not text:
                    text = (btn.get_attribute("data-cy") or "")
                if not text:
                    continue

                h = self._parse_hour_to_float(text)
                if h is not None:
                    slots.append(h)

            logger.info(f"  -> Aktywne sloty w bieżącym dniu: {slots}")
            return slots

        except Exception as e:
            logger.error(f"Błąd przy pobieraniu slotów: {e}")
            return []

    # ---------- ANALIZA KALENDARZA (DZISIAJ + PRZYSZŁOŚĆ) ----------

    def analyze_calendar(
        self,
        max_lookahead_days: int = 30,
        sleep_after_day_click: float = 0.3,
        fast_probe: bool = False,
    ):
        """
        Zwraca:
            days_any  - liczba dni do najbliższego slotu (dowolnego), 0 = dziś, None = brak
            days_16   - liczba dni do najbliższego slotu >=16:00, 0 = dziś, None = brak

        fast_probe=True  -> najpierw szybkie sprawdzenie dni bez długiego wait,
                            jeśli brak – krótki wait (5s) i ponowne sprawdzenie.
        """
        today = date.today()
        logger.info("=== ANALIZA KALENDARZA (priorytet: DZISIAJ) ===")
        logger.info(f"Dzisiejsza data: {today}")

        try:
            # Pobranie przycisków dni
            if fast_probe:
                self.close_possible_popups()
                day_buttons = self.driver.find_elements(
                    By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]"
                )
                if not day_buttons:
                    logger.info("Brak dni przy pierwszej próbie, czekam krótko (5s)...")
                    try:
                        WebDriverWait(self.driver, 5).until(
                            EC.presence_of_all_elements_located(
                                (By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]")
                            )
                        )
                        day_buttons = self.driver.find_elements(
                            By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]"
                        )
                    except TimeoutException:
                        day_buttons = []
            else:
                if not self.wait_for_calendar_days(timeout=10):
                    logger.warning("Brak dni w kalendarzu – uznaję brak terminów")
                    return None, None
                day_buttons = self.driver.find_elements(
                    By.XPATH, "//button[contains(@class,'Week__StyledDateButton')]"
                )

            if not day_buttons:
                logger.warning("Brak przycisków dni w kalendarzu – uznaję brak terminów")
                return None, None

            logger.info(f"Znaleziono {len(day_buttons)} przycisków dni w kalendarzu")

            # Kandydaci z datami i stanem disabled
            candidates = []
            for btn in day_buttons:
                data_cy = btn.get_attribute("data-cy") or ""
                if "slot-" not in data_cy:
                    continue

                disabled_attr = btn.get_attribute("disabled")
                aria_disabled = btn.get_attribute("aria-disabled")
                is_disabled = disabled_attr is not None or (
                    aria_disabled and aria_disabled.lower() == "true"
                )

                date_part = data_cy.split("slot-")[-1][:10]
                try:
                    d = datetime.strptime(date_part, "%Y-%m-%d").date()
                    candidates.append((btn, d, is_disabled))
                except ValueError:
                    continue

            if not candidates:
                logger.warning("Brak kandydatów dni (brak slot-YYYY-MM-DD)")
                return None, None

            candidates.sort(key=lambda x: x[1])

            days_any: Optional[int] = None
            days_16: Optional[int] = None

            # ---------- KROK 1: DZISIAJ ----------
            today_entry = None
            for (btn, d, is_disabled) in candidates:
                if d == today:
                    today_entry = (btn, d, is_disabled)
                    break

            if today_entry is not None:
                btn, d, is_disabled = today_entry
                logger.info(f"Sprawdzam dzień dzisiejszy: {d}, disabled={is_disabled}")

                if not is_disabled:
                    try:
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            btn,
                        )
                    except Exception:
                        pass

                    try:
                        WebDriverWait(self.driver, 3).until(
                            EC.element_to_be_clickable(btn)
                        )
                    except TimeoutException:
                        pass

                    try:
                        btn.click()
                    except Exception:
                        try:
                            self.driver.execute_script("arguments[0].click();", btn)
                        except Exception:
                            logger.debug("  -> Nie udało się kliknąć dzisiejszej daty")
                    else:
                        time.sleep(sleep_after_day_click)
                        slots_today = self._get_slots_for_current_day()
                        if slots_today:
                            days_any = 0
                            if any(h >= 16.0 for h in slots_today):
                                days_16 = 0
                                logger.info(
                                    "  -> DZISIAJ jest slot >=16:00, "
                                    "days_any=0, days_16=0 – kończę analizę."
                                )
                                return days_any, days_16
                            else:
                                logger.info(
                                    "  -> DZISIAJ są sloty <16:00 (days_any=0), "
                                    "szukam slotu >=16 w przyszłości"
                                )
                        else:
                            logger.info("  -> DZISIAJ brak aktywnych slotów")
                else:
                    logger.info("  -> Dziś: przycisk disabled, więc brak slotów")
            else:
                logger.info("Brak dnia dzisiejszego w widocznym kalendarzu (tylko przyszłość?)")

            # ---------- KROK 2: PRZYSZŁOŚĆ ----------
            for idx, (btn, d, is_disabled) in enumerate(candidates, start=1):
                delta = (d - today).days
                logger.info(
                    f"--- [{idx}] Sprawdzam dzień: {d} (delta = {delta}, disabled={is_disabled}) ---"
                )

                if delta <= 0:
                    logger.info("  -> Delta <= 0 – pomijam (dziś lub przeszłość)")
                    continue
                if delta > max_lookahead_days:
                    logger.info("  -> Delta > MAX_LOOKAHEAD_DAYS – przerywam pętlę")
                    break
                if is_disabled:
                    logger.info("  -> Dzień przyszły, ale disabled – pomijam (brak terminów)")
                    continue

                if days_any is not None and days_16 is not None:
                    logger.info("  -> Mamy już najbliższy slot 'any' i '>=16' – przerywam analizę")
                    break

                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        btn,
                    )
                except Exception:
                    pass

                try:
                    WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable(btn)
                    )
                except TimeoutException:
                    pass

                try:
                    btn.click()
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].click();", btn)
                    except Exception:
                        logger.debug(f"  -> Nie udało się kliknąć dnia {d}")
                        continue

                time.sleep(sleep_after_day_click)
                slots = self._get_slots_for_current_day()

                if not slots:
                    logger.info("  -> Brak aktywnych slotów – pomijam ten dzień")
                    continue

                if days_any is None:
                    days_any = delta

                if days_16 is None and any(h >= 16.0 for h in slots):
                    days_16 = delta

                if days_any is not None and days_16 is not None:
                    logger.info("  -> Ustalono najbliższe 'any' i '>=16' – koniec pętli")
                    break

            logger.info(f"Rezultat analizy: days_any={days_any}, days_16={days_16}")
            return days_any, days_16

        except Exception as e:
            logger.error(f"Błąd przy analizie kalendarza: {e}")
            return None, None

    # ---------- CSV (APPEND + fallback) + data_dzien ----------

    def save_results(self, rows: List[Dict], fallback_suffix: str = ""):
        """
        Zapisuje wyniki do RESULT_CSV, przy błędzie tworzy plik awaryjny.
        Dodaje kolumnę data_dzien (tylko data, bez godziny).
        Czyści komunikat_bledu, żeby nie psuł CSV.
        """
        if not rows:
            logger.info("Brak wierszy do zapisania – pomijam zapis CSV")
            return

        df = pd.DataFrame(rows)

        # NOWOŚĆ: kolumna "data_dzien" – sama data bez godziny
        if "data_sprawdzenia" in df.columns:
            df["data_dzien"] = pd.to_datetime(df["data_sprawdzenia"]).dt.date

        # Wygładzenie komunikatów błędów, żeby nie rozwalały struktury CSV
        if "komunikat_bledu" in df.columns:
            df["komunikat_bledu"] = (
                df["komunikat_bledu"]
                .fillna("")
                .astype(str)
                .apply(sanitize_error_message)
            )

        # Uporządkowana kolejność kolumn
        preferred_cols = [
            "Usługa",
            "kod_pocztowy",
            "lokalizacja",
            "slot_any_first",
            "slot_16plus_first",
            "data_sprawdzenia",
            "data_dzien",
            "status",
            "komunikat_bledu",
        ]
        existing_pref = [c for c in preferred_cols if c in df.columns]
        other_cols = [c for c in df.columns if c not in existing_pref]
        df = df[existing_pref + other_cols]

        file_exists = os.path.exists(RESULT_CSV)

        try:
            df.to_csv(
                RESULT_CSV,
                index=False,
                encoding="utf-8-sig",
                mode="a" if file_exists else "w",
                header=not file_exists,
            )
            if file_exists:
                logger.info(f"Wyniki dopisane do {RESULT_CSV}")
            else:
                logger.info(f"Wyniki zapisane do nowego pliku {RESULT_CSV}")

        except PermissionError:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = "ikea_slots_results"
            if fallback_suffix:
                prefix += f"_{fallback_suffix}"
            alt_name = f"{prefix}_{ts}.csv"

            df.to_csv(
                alt_name,
                index=False,
                encoding="utf-8-sig",
                mode="w",
                header=True,
            )
            logger.error(
                f"Nie mogłem dopisać do {RESULT_CSV} (prawdopodobnie otwarty w Excelu). "
                f"Wyniki zapisane do pliku awaryjnego: {alt_name}"
            )

        except Exception as e:
            logger.error(f"Błąd przy zapisie CSV: {e}")


# ======================================================================
# 1) PLAN – W SKLEPIE
# ======================================================================

PLANOWANIE_POSTAL_CODES = [
    "05-090",  # Janki
    "80-298",  # Gdańsk
    "31-356",  # Kraków
    "61-285",  # Poznań
    "55-040",  # Wrocław
    "40-203",  # Katowice
    "03-290",  # Warszawa Targówek
    "20-147",  # Lublin
    "93-457",  # Łódź
    "85-776",  # Bydgoszcz
    "71-010",  # Szczecin
    "15-277",  # Białystok
]

PLANOWANIE_KITCHENSTORE_URL = "https://www.ikea.com/pl/pl/appointment/kitchenstore/"
PLANOWANIE_SERVICE_NAME = "Plan"
PLANOWANIE_MAX_LOOKAHEAD_DAYS = 30
PLANOWANIE_SLEEP_AFTER_DAY_CLICK = 0.3


class IkeaStorePlanningChecker(BaseIkeaChecker):
    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)

    def enter_postal_code(self, postal_code: str):
        from selenium.webdriver.common.keys import Keys

        try:
            postal_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "zip"))
            )
            logger.info("Pole kodu pocztowego znalezione (Plan)")

            postal_input.clear()
            postal_input.send_keys(postal_code)
            postal_input.send_keys(Keys.TAB)
            logger.info(f"[Plan] Wpisano kod pocztowy: {postal_code}")

            self.close_possible_popups()

            next_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info("[Plan] Przycisk 'Dalej' (kod) znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info("[Plan] Kliknięto 'Dalej' po kodzie pocztowym")

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(@class,'services__ChoiceItemContainer')]")
                )
            )
            logger.info("[Plan] Przejście do wyboru usługi zakończone")
        except Exception as e:
            logger.error(f"[Plan][{postal_code}] Błąd przy etapie kodu pocztowego: {e}")
            raise

    def select_kitchen_planning(self, postal_code: str):
        try:
            planning_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(@class,'choice-item__action')]"
                        "[.//div[contains(@class,'choice-item__title') "
                        "and normalize-space(text())='Planowanie kuchni']]",
                    )
                )
            )
            logger.info(f"[Plan][{postal_code}] 'Planowanie kuchni' znalezione")

            self.close_possible_popups()

            try:
                planning_btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", planning_btn)

            logger.info(f"[Plan][{postal_code}] Kliknięto 'Planowanie kuchni'")

            next_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info(f"[Plan][{postal_code}] Drugi 'Dalej' znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info(f"[Plan][{postal_code}] Kliknięto drugi 'Dalej'")

            self.wait_for_calendar_days(timeout=15)
        except Exception as e:
            logger.error(f"[Plan][{postal_code}] Błąd przy wyborze 'Planowanie kuchni': {e}")
            raise

    def get_store_select_and_options(self):
        try:
            self.close_possible_popups()
            select_elem = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "store"))
            )
            store_select = Select(select_elem)
            options = []
            for opt in store_select.options:
                value = (opt.get_attribute("value") or "").strip()
                raw_label = (opt.text or "").strip()
                # Czyścimy nazwę lokalizacji z przecinków,
                # żeby w CSV nie trzeba było używać cudzysłowów.
                label = sanitize_location_name(raw_label)
                if value:
                    options.append((value, label))
            logger.info(f"[Plan] Znaleziono {len(options)} lokalizacji w dropdownie")
            return store_select, options
        except Exception as e:
            logger.error(f"[Plan] Nie udało się odczytać listy sklepów: {e}")
            raise

    def run(self, unit_postal_filter: Optional[str] = None):
        logger.info("=== START – Plan w sklepie ===")
        error_rows: List[Dict] = []
        store_cache: Dict[str, Dict] = {}

        try:
            self.setup_driver()

            postal_codes = PLANOWANIE_POSTAL_CODES
            if unit_postal_filter:
                postal_codes = [pc for pc in PLANOWANIE_POSTAL_CODES if pc == unit_postal_filter]
                if not postal_codes:
                    logger.info(
                        f"[Plan] Kod {unit_postal_filter} nie występuje na liście – pomijam moduł"
                    )
                    return

            for postal_code in postal_codes:
                logger.info(f"\n=== [Plan] Kod pocztowy: {postal_code} ===")
                try:
                    self.driver.get(PLANOWANIE_KITCHENSTORE_URL)
                    self.accept_cookies()
                    self.enter_postal_code(postal_code)
                    self.select_kitchen_planning(postal_code)
                except Exception as e:
                    logger.error(
                        f"[Plan][{postal_code}] Nie udało się przejść do kalendarza: {e}"
                    )
                    error_rows.append(
                        {
                            "Usługa": PLANOWANIE_SERVICE_NAME,
                            "kod_pocztowy": postal_code,
                            "lokalizacja": "",
                            "slot_any_first": "",
                            "slot_16plus_first": "",
                            "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "błąd",
                            "komunikat_bledu": f"Błąd etapu przejścia do kalendarza: {e}",
                        }
                    )
                    continue

                try:
                    store_select, options = self.get_store_select_and_options()
                except Exception as e:
                    logger.error(
                        f"[Plan][{postal_code}] Nie udało się pobrać listy sklepów: {e}"
                    )
                    error_rows.append(
                        {
                            "Usługa": PLANOWANIE_SERVICE_NAME,
                            "kod_pocztowy": postal_code,
                            "lokalizacja": "",
                            "slot_any_first": "",
                            "slot_16plus_first": "",
                            "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "błąd",
                            "komunikat_bledu": f"Błąd listy sklepów: {e}",
                        }
                    )
                    continue

                for value, label in options:
                    logger.info(
                        f"=== [Plan][{postal_code}] Lokalizacja: {label} (value={value}) ==="
                    )

                    if value in store_cache:
                        logger.info(
                            f"[Plan][{postal_code}] Sklep {label} już w cache – pomijam analizę"
                        )
                        continue

                    status = "błąd"
                    error_message = ""
                    dni_any = SENTINEL_NO_SLOT
                    dni_16 = SENTINEL_NO_SLOT

                    try:
                        self.close_possible_popups()
                        store_select.select_by_value(value)
                        logger.info(
                            f"[Plan][{postal_code}] Wybrano lokalizację z dropdownu: {label}"
                        )

                        if not self.wait_for_calendar_days(timeout=12):
                            logger.warning(
                                f"[Plan][{postal_code}] [{label}] "
                                "Brak dni w kalendarzu po zmianie lokalizacji"
                            )
                            error_message = "Brak przycisków dni w kalendarzu (brak terminów / błąd)"
                        else:
                            days_any, days_16 = self.analyze_calendar(
                                max_lookahead_days=PLANOWANIE_MAX_LOOKAHEAD_DAYS,
                                sleep_after_day_click=PLANOWANIE_SLEEP_AFTER_DAY_CLICK,
                                fast_probe=False,
                            )

                            if days_any is None:
                                status = "błąd"
                                error_message = "Brak slotów"
                                dni_any = SENTINEL_NO_SLOT
                                dni_16 = SENTINEL_NO_SLOT
                            else:
                                status = "sukces"
                                error_message = ""
                                dni_any = days_any
                                dni_16 = days_16 if days_16 is not None else SENTINEL_NO_SLOT

                    except Exception as e:
                        status = "błąd"
                        error_message = str(e)
                        dni_any = SENTINEL_NO_SLOT
                        dni_16 = SENTINEL_NO_SLOT
                        logger.error(
                            f"[Plan][{postal_code}] [{label}] Błąd główny: {error_message}"
                        )

                    store_cache[value] = {
                        "Usługa": PLANOWANIE_SERVICE_NAME,
                        "kod_pocztowy": postal_code,
                        "lokalizacja": label,
                        "dni_any": dni_any,
                        "dni_16": dni_16,
                        "status": status,
                        "komunikat_bledu": error_message,
                    }

            rows: List[Dict] = []
            rows.extend(error_rows)
            for data in store_cache.values():
                rows.append(
                    {
                        "Usługa": data["Usługa"],
                        "kod_pocztowy": data["kod_pocztowy"],
                        "lokalizacja": data["lokalizacja"],
                        "slot_any_first": data["dni_any"],
                        "slot_16plus_first": data["dni_16"],
                        "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": data["status"],
                        "komunikat_bledu": data["komunikat_bledu"],
                    }
                )

            self.save_results(rows, fallback_suffix="plan")

        finally:
            self.teardown_driver()
            logger.info("=== KONIEC – Plan w sklepie ===")


# ======================================================================
# 2) FIN – W SKLEPIE
# ======================================================================

FINALIZACJA_POSTAL_CODES = [
    "05-090",  # Janki
    "80-298",  # Gdańsk
    "31-356",  # Kraków
    "61-285",  # Poznań
    "55-040",  # Wrocław
    "40-203",  # Katowice
    "03-290",  # Warszawa Targówek
    "20-147",  # Lublin
    "93-457",  # Łódź
    "85-776",  # Bydgoszcz
    "71-010",  # Szczecin
    "15-277",  # Białystok
]

FINALIZACJA_KITCHENSTORE_URL = "https://www.ikea.com/pl/pl/appointment/kitchenfinalizestore/"
FINALIZACJA_SERVICE_NAME = "Fin"
FINALIZACJA_MAX_LOOKAHEAD_DAYS = 30
FINALIZACJA_SLEEP_AFTER_DAY_CLICK = 0.3


class IkeaStoreFinalizationChecker(BaseIkeaChecker):
    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)

    def enter_postal_code(self, postal_code: str):
        from selenium.webdriver.common.keys import Keys

        try:
            postal_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "zip"))
            )
            logger.info("Pole kodu pocztowego znalezione (Fin)")

            postal_input.clear()
            postal_input.send_keys(postal_code)
            postal_input.send_keys(Keys.TAB)
            logger.info(f"[Fin] Wpisano kod pocztowy: {postal_code}")

            self.close_possible_popups()

            next_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info("[Fin] Przycisk 'Dalej' (kod) znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info("[Fin] Kliknięto 'Dalej' po kodzie pocztowym")

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(@class,'services__ChoiceItemContainer')]")
                )
            )
            logger.info("[Fin] Przejście do wyboru usługi zakończone")
        except Exception as e:
            logger.error(f"[Fin][{postal_code}] Błąd przy etapie kodu pocztowego: {e}")
            raise

    def select_kitchen_finalization(self, postal_code: str):
        try:
            final_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(@class,'choice-item__action')]"
                        "[.//div[contains(@class,'choice-item__title') "
                        "and normalize-space(text())='Finalizacja projektu kuchni']]",
                    )
                )
            )
            logger.info(f"[Fin][{postal_code}] 'Finalizacja projektu kuchni' znalezione")

            self.close_possible_popups()

            try:
                final_btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", final_btn)

            logger.info(f"[Fin][{postal_code}] Kliknięto 'Finalizacja projektu kuchni'")

            next_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info(f"[Fin][{postal_code}] Drugi 'Dalej' znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info(f"[Fin][{postal_code}] Kliknięto drugi 'Dalej'")

            self.wait_for_calendar_days(timeout=15)
        except Exception as e:
            logger.error(
                f"[Fin][{postal_code}] Błąd przy wyborze 'Finalizacja projektu kuchni': {e}"
            )
            raise

    def get_store_select_and_options(self):
        try:
            self.close_possible_popups()
            select_elem = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "store"))
            )
            store_select = Select(select_elem)
            options = []
            for opt in store_select.options:
                value = (opt.get_attribute("value") or "").strip()
                raw_label = (opt.text or "").strip()
                # Czyścimy nazwę lokalizacji z przecinków,
                # żeby w CSV nie trzeba było używać cudzysłowów.
                label = sanitize_location_name(raw_label)
                if value:
                    options.append((value, label))
            logger.info(f"[Fin] Znaleziono {len(options)} lokalizacji w dropdownie")
            return store_select, options
        except Exception as e:
            logger.error(f"[Fin] Nie udało się odczytać listy sklepów: {e}")
            raise

    def run(self, unit_postal_filter: Optional[str] = None):
        logger.info("=== START – Fin w sklepie ===")
        error_rows: List[Dict] = []
        store_cache: Dict[str, Dict] = {}

        try:
            self.setup_driver()

            postal_codes = FINALIZACJA_POSTAL_CODES
            if unit_postal_filter:
                postal_codes = [pc for pc in FINALIZACJA_POSTAL_CODES if pc == unit_postal_filter]
                if not postal_codes:
                    logger.info(
                        f"[Fin] Kod {unit_postal_filter} nie występuje na liście – pomijam moduł"
                    )
                    return

            for postal_code in postal_codes:
                logger.info(f"\n=== [Fin] Kod pocztowy: {postal_code} ===")
                try:
                    self.driver.get(FINALIZACJA_KITCHENSTORE_URL)
                    self.accept_cookies()
                    self.enter_postal_code(postal_code)
                    self.select_kitchen_finalization(postal_code)
                except Exception as e:
                    logger.error(
                        f"[Fin][{postal_code}] Nie udało się przejść do kalendarza: {e}"
                    )
                    error_rows.append(
                        {
                            "Usługa": FINALIZACJA_SERVICE_NAME,
                            "kod_pocztowy": postal_code,
                            "lokalizacja": "",
                            "slot_any_first": "",
                            "slot_16plus_first": "",
                            "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "błąd",
                            "komunikat_bledu": f"Błąd etapu przejścia do kalendarza: {e}",
                        }
                    )
                    continue

                try:
                    store_select, options = self.get_store_select_and_options()
                except Exception as e:
                    logger.error(
                        f"[Fin][{postal_code}] Nie udało się pobrać listy sklepów: {e}"
                    )
                    error_rows.append(
                        {
                            "Usługa": FINALIZACJA_SERVICE_NAME,
                            "kod_pocztowy": postal_code,
                            "lokalizacja": "",
                            "slot_any_first": "",
                            "slot_16plus_first": "",
                            "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "błąd",
                            "komunikat_bledu": f"Błąd listy sklepów: {e}",
                        }
                    )
                    continue

                for value, label in options:
                    logger.info(
                        f"=== [Fin][{postal_code}] Lokalizacja: {label} (value={value}) ==="
                    )

                    if value in store_cache:
                        logger.info(
                            f"[Fin][{postal_code}] Sklep {label} już w cache – pomijam analizę"
                        )
                        continue

                    status = "błąd"
                    error_message = ""
                    dni_any = SENTINEL_NO_SLOT
                    dni_16 = SENTINEL_NO_SLOT

                    try:
                        self.close_possible_popups()
                        store_select.select_by_value(value)
                        logger.info(
                            f"[Fin][{postal_code}] Wybrano lokalizację z dropdownu: {label}"
                        )

                        if not self.wait_for_calendar_days(timeout=12):
                            logger.warning(
                                f"[Fin][{postal_code}] [{label}] "
                                "Brak dni w kalendarzu po zmianie lokalizacji"
                            )
                            error_message = "Brak przycisków dni w kalendarzu (brak terminów / błąd)"
                        else:
                            days_any, days_16 = self.analyze_calendar(
                                max_lookahead_days=FINALIZACJA_MAX_LOOKAHEAD_DAYS,
                                sleep_after_day_click=FINALIZACJA_SLEEP_AFTER_DAY_CLICK,
                                fast_probe=False,
                            )

                            if days_any is None:
                                status = "błąd"
                                error_message = "Brak slotów"
                                dni_any = SENTINEL_NO_SLOT
                                dni_16 = SENTINEL_NO_SLOT
                            else:
                                status = "sukces"
                                error_message = ""
                                dni_any = days_any
                                dni_16 = days_16 if days_16 is not None else SENTINEL_NO_SLOT

                    except Exception as e:
                        status = "błąd"
                        error_message = str(e)
                        dni_any = SENTINEL_NO_SLOT
                        dni_16 = SENTINEL_NO_SLOT
                        logger.error(
                            f"[Fin][{postal_code}] [{label}] Błąd główny: {error_message}"
                        )

                    store_cache[value] = {
                        "Usługa": FINALIZACJA_SERVICE_NAME,
                        "kod_pocztowy": postal_code,
                        "lokalizacja": label,
                        "dni_any": dni_any,
                        "dni_16": dni_16,
                        "status": status,
                        "komunikat_bledu": error_message,
                    }

            rows: List[Dict] = []
            rows.extend(error_rows)
            for data in store_cache.values():
                rows.append(
                    {
                        "Usługa": data["Usługa"],
                        "kod_pocztowy": data["kod_pocztowy"],
                        "lokalizacja": data["lokalizacja"],
                        "slot_any_first": data["dni_any"],
                        "slot_16plus_first": data["dni_16"],
                        "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": data["status"],
                        "komunikat_bledu": data["komunikat_bledu"],
                    }
                )

            self.save_results(rows, fallback_suffix="fin")

        finally:
            self.teardown_driver()
            logger.info("=== KONIEC – Fin w sklepie ===")


# ======================================================================
# 3) ONLINE – PLAN / FIN (RCMP)
# ======================================================================

ONLINE_LOCATION_NAME = "RCMP"
ONLINE_POSTAL_CODE = "03-089"

ONLINE_SERVICES = [
    {
        "service_code": "Plan online",
        "service_title": "Planowanie kuchni online",
        "url": "https://www.ikea.com/pl/pl/appointment/kitchenremote/timeslots/?storeselector=false",
    },
    {
        "service_code": "Fin online",
        "service_title": "Finalizacja projektu kuchni online",
        "url": "https://www.ikea.com/pl/pl/appointment/kitchenfinalizeremote/timeslots/?storeselector=false",
    },
]

ONLINE_MAX_LOOKAHEAD_DAYS = 30
ONLINE_SLEEP_AFTER_DAY_CLICK = 0.3


class IkeaOnlineChecker(BaseIkeaChecker):
    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)

    def enter_postal_code(self, postal_code: str):
        from selenium.webdriver.common.keys import Keys

        try:
            postal_input = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "zip"))
            )
            logger.info("[Online] Pole kodu pocztowego znalezione")

            postal_input.clear()
            postal_input.send_keys(postal_code)
            postal_input.send_keys(Keys.TAB)
            logger.info(f"[Online] Wpisano kod pocztowy: {postal_code}")

            self.close_possible_popups()

            next_button = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info("[Online] Przycisk 'Dalej' (kod) znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info("[Online] Kliknięto 'Dalej' po kodzie pocztowym")

            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(@class,'services__ChoiceItemContainer')]")
                )
            )
            logger.info("[Online] Przejście do wyboru usługi zakończone")
        except Exception as e:
            logger.error(f"[Online][{postal_code}] Błąd przy etapie kodu pocztowego: {e}")
            raise

    def select_online_service(self, postal_code: str, service_title: str):
        try:
            btn = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(@class,'choice-item__action')]"
                        "[.//div[contains(@class,'choice-item__title') "
                        f"and normalize-space(text())='{service_title}']]",
                    )
                )
            )
            logger.info(f"[Online][{postal_code}] '{service_title}' znalezione")

            self.close_possible_popups()

            try:
                btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", btn)

            logger.info(f"[Online][{postal_code}] Kliknięto '{service_title}'")

            next_button = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info(f"[Online][{postal_code}] Drugi 'Dalej' znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info(f"[Online][{postal_code}] Kliknięto drugi 'Dalej'")

            self.wait_for_calendar_days(timeout=10)
        except Exception as e:
            logger.error(f"[Online][{postal_code}] Błąd przy wyborze '{service_title}': {e}")
            raise

    def run(self, unit_postal_filter: Optional[str] = None):
        logger.info("=== START – Online (Plan / Fin) ===")

        # jeśli użytkownik wybrał konkretną jednostkę i to NIE jest kod RCMP – nic nie robimy
        if unit_postal_filter and unit_postal_filter != ONLINE_POSTAL_CODE:
            logger.info(
                f"[Online] Wybrany kod {unit_postal_filter} nie dotyczy usługi online (RCMP: {ONLINE_POSTAL_CODE}) – pomijam moduł"
            )
            return

        rows: List[Dict] = []

        try:
            self.setup_driver()

            for service in ONLINE_SERVICES:
                service_code = service["service_code"]
                service_title = service["service_title"]
                url = service["url"]

                logger.info(
                    f"\n=== START – {service_title} ({service_code}) dla {ONLINE_LOCATION_NAME} ==="
                )

                status = "błąd"
                error_message = ""
                dni_any = SENTINEL_NO_SLOT
                dni_16 = SENTINEL_NO_SLOT

                try:
                    self.driver.get(url)
                    self.accept_cookies(short_wait=True)

                    self.enter_postal_code(ONLINE_POSTAL_CODE)
                    self.select_online_service(ONLINE_POSTAL_CODE, service_title)

                    days_any, days_16 = self.analyze_calendar(
                        max_lookahead_days=ONLINE_MAX_LOOKAHEAD_DAYS,
                        sleep_after_day_click=ONLINE_SLEEP_AFTER_DAY_CLICK,
                        fast_probe=True,
                    )

                    if days_any is None:
                        status = "błąd"
                        error_message = "Brak slotów"
                        dni_any = SENTINEL_NO_SLOT
                        dni_16 = SENTINEL_NO_SLOT
                    else:
                        status = "sukces"
                        error_message = ""
                        dni_any = days_any
                        dni_16 = days_16 if days_16 is not None else SENTINEL_NO_SLOT

                except Exception as e:
                    status = "błąd"
                    error_message = str(e)
                    dni_any = SENTINEL_NO_SLOT
                    dni_16 = SENTINEL_NO_SLOT
                    logger.error(f"[Online][{service_code}] Błąd główny: {error_message}")

                rows.append(
                    {
                        "Usługa": service_code,
                        "kod_pocztowy": ONLINE_POSTAL_CODE,
                        "lokalizacja": ONLINE_LOCATION_NAME,
                        "slot_any_first": dni_any,
                        "slot_16plus_first": dni_16,
                        "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": status,
                        "komunikat_bledu": error_message,
                    }
                )

            self.save_results(rows, fallback_suffix="online")

        finally:
            self.teardown_driver()
            logger.info("=== KONIEC – Online (Plan / Fin) ===")


# ======================================================================
# 4) LISTA JEDNOSTEK + PUK – PLANOWANIE W DOMU
# ======================================================================

UNIT_LIST = [
    ("IKEA Bielsko - Biała", "43-300"),
    ("IKEA Bydgoszcz", "85-776"),
    ("IKEA Gdańsk", "80-298"),
    ("IKEA Katowice", "40-203"),
    ("IKEA Kraków", "31-356"),
    ("IKEA Lublin", "20-147"),
    ("IKEA Łódź", "93-457"),
    ("IKEA Ostrów Wielkopolski - Galeria Ostrovia", "61-285"),
    ("IKEA Poznań", "61-285"),
    ("IKEA Szczecin", "71-010"),
    ("IKEA Warszawa Janki", "05-090"),
    ("IKEA Warszawa Targówek", "03-290"),
    ("IKEA Wrocław", "55-040"),
    ("RCMP", "03-089"),
    ("Studio planowania i zamowień w Lubinie", "59-300"),
    ("Studio planowania i zamówień w Białymstoku", "15-277"),
    ("Studio planowania i zamówień w Gliwicach", "31-356"),
    ("Studio planowania i zamówień w Kielcach", "25-406"),
    ("Studio planowania i zamówień w Koszalinie", "75-452"),
    ("Studio planowania i zamówień w Olsztynie", "10-748"),
    ("Studio planowania i zamówień w Opolu", "46-022"),
    ("Studio planowania i zamówień w Radomiu", "20-147"),
    ("Studio planowania i zamówień w Rumi", "84-230"),
    ("Studio planowania i zamówień w Rzeszowie", "35-315"),
    ("Studio planowania i zamówień w Toruniu", "80-298"),
    ("Studio planowania i zamówień w Warszawie, Promenada", "05-090"),
    ("Studio planowania i zamówień w Warszawie, Westfield Mokotów", "05-090"),
    ("Studio planowania i zamówień w Warszawie, Wola Park", "05-090"),
    ("Studio planowania i zamówień w Zielonej Górze", "65-427"),
    ("Studio planowania i zamówień we Włocławku", "93-457"),
]

# PUK-lokalizacje = wszystkie powyższe, oprócz RCMP (online)
PUK_LOCATIONS = [
    (sanitize_location_name(name), code)
    for (name, code) in UNIT_LIST
    if name != "RCMP"
]

PUK_KITCHENSTORE_URL = "https://www.ikea.com/pl/pl/appointment/planningathome//"
PUK_SERVICE_NAME = "PUK"
PUK_MAX_LOOKAHEAD_DAYS = 30
PUK_SLEEP_AFTER_DAY_CLICK = 0.3


class IkeaPUKChecker(BaseIkeaChecker):
    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)

    def enter_postal_code(self, postal_code: str):
        from selenium.webdriver.common.keys import Keys

        try:
            postal_input = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "zip"))
            )
            logger.info("[PUK] Pole kodu pocztowego znalezione")

            postal_input.clear()
            postal_input.send_keys(postal_code)
            postal_input.send_keys(Keys.TAB)
            logger.info(f"[PUK] Wpisano kod pocztowy: {postal_code}")

            self.close_possible_popups()

            next_button = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info("[PUK] Przycisk 'Dalej' (kod) znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info("[PUK] Kliknięto 'Dalej' po kodzie pocztowym")

            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(@class,'services__ChoiceItemContainer')]")
                )
            )
            logger.info("[PUK] Przejście do wyboru usługi zakończone")
        except Exception as e:
            logger.error(f"[PUK][{postal_code}] Błąd przy etapie kodu pocztowego: {e}")
            raise

    def select_puk_service(self, postal_code: str):
        try:
            puk_btn = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(@class,'choice-item__action')]"
                        "[.//div[contains(@class,'choice-item__title') "
                        "and normalize-space(text())='Planowanie kuchni w domu klienta']]",
                    )
                )
            )
            logger.info(
                f"[PUK][{postal_code}] 'Planowanie kuchni w domu klienta' – przycisk znaleziony"
            )

            self.close_possible_popups()

            try:
                puk_btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", puk_btn)

            logger.info(f"[PUK][{postal_code}] Kliknięto 'Planowanie kuchni w domu klienta'")

            next_button = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable((By.ID, "nextbutton"))
            )
            logger.info(f"[PUK][{postal_code}] Drugi 'Dalej' znaleziony")

            self.close_possible_popups()

            try:
                next_button.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", next_button)

            logger.info(f"[PUK][{postal_code}] Kliknięto drugi 'Dalej'")

            self.wait_for_calendar_days(timeout=10)
        except Exception as e:
            logger.error(
                f"[PUK][{postal_code}] Błąd przy wyborze 'Planowanie kuchni w domu klienta': {e}"
            )
            raise

    def run(self, unit_postal_filter: Optional[str] = None):
        logger.info("=== START – PUK (Planowanie kuchni w domu klienta) ===")
        rows: List[Dict] = []

        try:
            self.setup_driver()

            locations = PUK_LOCATIONS
            if unit_postal_filter:
                locations = [loc for loc in PUK_LOCATIONS if loc[1] == unit_postal_filter]
                if not locations:
                    logger.info(
                        f"[PUK] Kod {unit_postal_filter} nie występuje na liście PUK – pomijam moduł"
                    )
                    return

            for location_name, postal_code in locations:
                logger.info(f"\n=== [PUK] {location_name} ({postal_code}) ===")

                status = "błąd"
                error_message = ""
                dni_any = SENTINEL_NO_SLOT
                dni_16 = SENTINEL_NO_SLOT

                try:
                    self.driver.get(PUK_KITCHENSTORE_URL)
                    self.accept_cookies(short_wait=True)
                    self.enter_postal_code(postal_code)
                    self.select_puk_service(postal_code)

                    days_any, days_16 = self.analyze_calendar(
                        max_lookahead_days=PUK_MAX_LOOKAHEAD_DAYS,
                        sleep_after_day_click=PUK_SLEEP_AFTER_DAY_CLICK,
                        fast_probe=True,
                    )

                    if days_any is None:
                        status = "błąd"
                        error_message = "Brak slotów"
                        dni_any = SENTINEL_NO_SLOT
                        dni_16 = SENTINEL_NO_SLOT
                    else:
                        status = "sukces"
                        error_message = ""
                        dni_any = days_any
                        dni_16 = days_16 if days_16 is not None else SENTINEL_NO_SLOT

                except Exception as e:
                    status = "błąd"
                    error_message = str(e)
                    dni_any = SENTINEL_NO_SLOT
                    dni_16 = SENTINEL_NO_SLOT
                    logger.error(
                        f"[PUK][{postal_code}][{location_name}] Błąd główny: {error_message}"
                    )

                rows.append(
                    {
                        "Usługa": PUK_SERVICE_NAME,
                        "kod_pocztowy": postal_code,
                        "lokalizacja": location_name,
                        "slot_any_first": dni_any,
                        "slot_16plus_first": dni_16,
                        "data_sprawdzenia": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": status,
                        "komunikat_bledu": error_message,
                    }
                )

            self.save_results(rows, fallback_suffix="puk")

        finally:
            self.teardown_driver()
            logger.info("=== KONIEC – PUK ===")


# ======================================================================
# MENU / WYBÓR JEDNOSTKI / PUNKT WEJŚCIA
# ======================================================================

def choose_unit_postal_code() -> Optional[str]:
    """
    Wybór jednostki z listy (sklepy / studia / RCMP online).

    Zwraca:
      - None      -> jeśli użytkownik wybierze "wszystkie jednostki"
      - 'XX-XXX'  -> konkretny kod pocztowy wybranej jednostki
    """
    print("\n=== Wybór jednostki (sklep / studio / online) ===")
    print("0 – WSZYSTKIE jednostki")

    for idx, (name, code) in enumerate(UNIT_LIST, start=1):
        print(f"{idx} – {name} ({code})")

    choice = input("Wybierz numer jednostki [0-n] (Enter = 0): ").strip()

    # 0 lub Enter -> wszystkie
    if choice == "" or choice == "0":
        return None

    # Konkretny numer z listy
    try:
        idx = int(choice)
        if 1 <= idx <= len(UNIT_LIST):
            name, selected_code = UNIT_LIST[idx - 1]
            print(f"Wybrano: {name} ({selected_code})")
            return selected_code
    except ValueError:
        pass

    print("Nieprawidłowy wybór – używam wszystkich jednostek.")
    return None


def main():
    print("=== IKEA Slot Checker – program zbiorczy ===")
    print("Wybierz usługę:")
    print("1 – Plan w sklepie")
    print("2 – Fin w sklepie")
    print("3 – Online (Plan / Fin)")
    print("4 – PUK (Planowanie w domu klienta)")
    print("5 – Wszystko po kolei (1 → 2 → 3 → 4)")
    service_choice = input("Twój wybór [1-5]: ").strip()

    # wybór jednostki (kod pocztowy)
    unit_postal = choose_unit_postal_code()

    if service_choice == "1":
        IkeaStorePlanningChecker(headless=True).run(unit_postal_filter=unit_postal)
    elif service_choice == "2":
        IkeaStoreFinalizationChecker(headless=True).run(unit_postal_filter=unit_postal)
    elif service_choice == "3":
        IkeaOnlineChecker(headless=True).run(unit_postal_filter=unit_postal)
    elif service_choice == "4":
        IkeaPUKChecker(headless=True).run(unit_postal_filter=unit_postal)
    elif service_choice == "5":
        IkeaStorePlanningChecker(headless=True).run(unit_postal_filter=unit_postal)
        IkeaStoreFinalizationChecker(headless=True).run(unit_postal_filter=unit_postal)
        IkeaOnlineChecker(headless=True).run(unit_postal_filter=unit_postal)
        IkeaPUKChecker(headless=True).run(unit_postal_filter=unit_postal)
    else:
        print("Nieprawidłowy wybór – kończę.")


if __name__ == "__main__":
    # Tryb automatyczny: python slotchecker.py --auto
    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        logger.info("Tryb AUTOMATYCZNY: sprawdzam WSZYSTKIE usługi i WSZYSTKIE jednostki")

        # brak filtra = wszystkie jednostki
        unit_postal = None

        # 1) Plan w sklepie
        IkeaStorePlanningChecker(headless=True).run(unit_postal_filter=unit_postal)
        # 2) Fin w sklepie
        IkeaStoreFinalizationChecker(headless=True).run(unit_postal_filter=unit_postal)
        # 3) Online (Plan / Fin) – RCMP
        IkeaOnlineChecker(headless=True).run(unit_postal_filter=unit_postal)
        # 4) PUK – Planowanie w domu klienta
        IkeaPUKChecker(headless=True).run(unit_postal_filter=unit_postal)

        logger.info("Tryb AUTOMATYCZNY zakończony")
    else:
        # Tryb interaktywny (lokalnie na Twoim komputerze)
        main()
