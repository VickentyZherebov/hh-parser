import threading
import csv
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Callable

import flet as ft
import requests

from hh_companies_by_industry import (
    __version__,
    Industry,
    Company,
    scrape_companies,
    fetch_html,
    parse_industries,
    CATALOG_URL,
    UA,
)


def export_results(
    companies: list[Company],
    chosen_industries: list[Industry],
    area: int,
    min_vacancies: int,
    only_open: bool,
    output_dir: Path,
    export_json: bool,
    export_csv: bool,
) -> list[str]:
    """
    Экспортирует результаты парсинга в JSON и/или CSV.
    Возвращает список путей к созданным файлам.
    """
    ts = datetime.now(timezone.utc)
    scraped_at_utc = ts.isoformat()
    ts_compact = ts.strftime("%Y%m%d_%H%M%S")

    base_name = f"hh_companies_multi_area{area}_min{min_vacancies}_{ts_compact}"

    output_dir.mkdir(parents=True, exist_ok=True)
    out_json = output_dir / f"{base_name}.json"
    out_csv = output_dir / f"{base_name}.csv"

    written_files: list[str] = []

    if export_json:
        payload = {
            "scraped_at_utc": scraped_at_utc,
            "filters": {
                "min_vacancies": min_vacancies,
                "area": area,
                "only_with_open_vacancies": only_open,
                "industries_selected": [asdict(i) for i in chosen_industries],
            },
            "companies_count": len(companies),
            "companies": [asdict(c) for c in companies],
        }
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written_files.append(str(out_json.resolve()))

    if export_csv:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company_name", "vacancies", "company_url", "industry_name", "area", "scraped_at_utc"])
            for c in companies:
                w.writerow([c.name, c.vacancies, c.url, c.industry_name, area, scraped_at_utc])
        written_files.append(str(out_csv.resolve()))

    return written_files


@dataclass
class AppState:
    industries: List[Industry] = field(default_factory=list)
    selected: set[str] = field(default_factory=set)  # industry.slug_url
    min_vacancies: int = 10
    area: int = 113
    only_open: bool = True
    is_running: bool = False
    status: str = ""
    last_output_file: Optional[str] = None
    output_dir: Path = field(default_factory=Path)  # папка для сохранения результатов


def main(page: ft.Page):
    page.title = f"HH: Парсер базы компаний v{__version__}"
    page.window_width = 1100
    page.window_height = 750

    state = AppState()

    # ---------- helpers (paths) ----------
    def get_default_desktop() -> Path:
        home = Path.home()
        candidates = [
            home / "Desktop",
            home / "Рабочий стол",
            home / "OneDrive" / "Desktop",
            home / "OneDrive" / "Рабочий стол",
        ]
        for p in candidates:
            if p.exists():
                return p
        return home

    state.output_dir = get_default_desktop()

    # ---------- UI controls ----------
    title = ft.Text("Сбор базы компаний с HH", size=24, weight=ft.FontWeight.BOLD)

    status_text = ft.Text("")
    progress_industries = ft.ProgressBar(value=0, visible=False)
    progress_pages = ft.ProgressBar(value=0, visible=False)
    stats_text = ft.Text("")

    start_btn = ft.ElevatedButton("Старт", disabled=True)
    reload_btn = ft.OutlinedButton("Обновить индустрии")

    min_vac_field = ft.TextField(label="Минимум вакансий у компании", value="10", width=260)
    area_field = ft.TextField(label="Area (ENTER = 113 Россия)", value="113", width=260)
    only_open_switch = ft.Switch(label="Только компании с открытыми вакансиями", value=True)

    export_json_cb = ft.Checkbox(label="JSON", value=False)
    export_csv_cb = ft.Checkbox(label="CSV", value=True)

    # Папка сохранения
    output_dir_field = ft.TextField(
        label="Папка сохранения",
        value=str(state.output_dir),
        read_only=True,
        expand=True,
    )
    choose_dir_btn = ft.OutlinedButton("Выбрать…")

    # Directory picker (живёт в overlay)
    def on_dir_picked(e: ft.FilePickerResultEvent):
        if e.path:
            state.output_dir = Path(e.path)
            output_dir_field.value = str(state.output_dir)
            page.update()

    dir_picker = ft.FilePicker(on_result=on_dir_picked)
    page.overlay.append(dir_picker)

    def on_choose_dir(_):
        dir_picker.get_directory_path(
            dialog_title="Выбери папку для сохранения",
            initial_directory=str(state.output_dir),
        )

    choose_dir_btn.on_click = on_choose_dir

    search_field = ft.TextField(label="Поиск по индустриям", width=520)
    select_all_btn = ft.TextButton("Выбрать все")
    clear_all_btn = ft.TextButton("Снять все")

    industries_column = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    result_info = ft.Text("")
    open_file_btn = ft.TextButton("Открыть файл", visible=False)
    open_folder_btn = ft.TextButton("Открыть папку", visible=False)

    # ---------- helpers ----------
    def set_status(text: str):
        state.status = text
        status_text.value = text
        page.update()

    def set_running(running: bool):
        state.is_running = running

        progress_industries.visible = running
        progress_pages.visible = running

        if running:
            progress_industries.value = 0
            progress_pages.value = 0
            stats_text.value = ""

        start_btn.disabled = running or len(state.selected) == 0
        reload_btn.disabled = running
        page.update()

    def build_industry_list(filter_text: str = ""):
        industries_column.controls.clear()
        ft_filter = filter_text.strip().lower()

        for ind in state.industries:
            if ft_filter and ft_filter not in ind.name.lower():
                continue

            checked = ind.slug_url in state.selected

            def on_change_factory(url: str):
                def _on_change(e):
                    if e.control.value:
                        state.selected.add(url)
                    else:
                        state.selected.discard(url)
                    start_btn.disabled = state.is_running or len(state.selected) == 0
                    page.update()

                return _on_change

            industries_column.controls.append(
                ft.Checkbox(
                    label=ind.name,
                    value=checked,
                    on_change=on_change_factory(ind.slug_url),
                )
            )

        start_btn.disabled = state.is_running or len(state.selected) == 0
        page.update()

    def load_industries():
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})

        set_status("Загружаю список индустрий...")
        html = fetch_html(CATALOG_URL, sess)
        inds = parse_industries(html)

        state.industries = inds
        state.selected.clear()

        set_status(f"Индустрий найдено: {len(inds)}. Выбери нужные и нажми Старт.")
        build_industry_list(search_field.value)

    def on_reload(_):
        set_running(True)
        try:
            load_industries()
        except Exception as ex:
            set_status(f"Ошибка загрузки индустрий: {ex}")
        finally:
            set_running(False)

    def on_search(e):
        build_industry_list(e.control.value)

    def on_select_all(_):
        state.selected = {i.slug_url for i in state.industries}
        build_industry_list(search_field.value)

    def on_clear_all(_):
        state.selected.clear()
        build_industry_list(search_field.value)

    def run_scrape_job():
        # читаем поля (с валидацией)
        try:
            state.min_vacancies = int(min_vac_field.value.strip())
        except Exception:
            state.min_vacancies = 10
            min_vac_field.value = "10"

        try:
            state.area = int(area_field.value.strip() or "113")
        except Exception:
            state.area = 113
            area_field.value = "113"

        state.only_open = bool(only_open_switch.value)

        chosen = [i for i in state.industries if i.slug_url in state.selected]
        total = len(chosen)
        if total == 0:
            raise RuntimeError("Не выбрано ни одной индустрии.")

        # --- подготовка UI к запуску ---
        progress_industries.visible = True
        progress_pages.visible = True
        progress_industries.value = 0
        progress_pages.value = 0

        stats_text.value = ""
        result_info.value = ""
        open_file_btn.visible = False
        open_folder_btn.visible = False
        page.update()

        all_companies: list = []
        total_found_all = 0
        total_added_all = 0

        for idx, ind in enumerate(chosen, start=1):
            set_status(f"Индустрия {idx}/{total}: {ind.name}")

            found_in_industry = 0
            added_in_industry = 0

            def on_progress(p):
                nonlocal found_in_industry, added_in_industry
                found_in_industry = p.get("companies_found_total", found_in_industry)
                added_in_industry = p.get("companies_added_total", added_in_industry)

                pages_total = max(1, int(p.get("pages_total", 1)))
                page_no = int(p.get("page", 1))
                progress_pages.value = page_no / pages_total

                stats_text.value = (
                    f"Индустрия: {p.get('industry_name', ind.name)} | "
                    f"Страница: {page_no}/{pages_total} | "
                    f"Найдено: {found_in_industry} | "
                    f"В итог: {added_in_industry}"
                )
                page.update()

            companies = scrape_companies(
                ind,
                min_vacancies=state.min_vacancies,
                area=state.area,
                only_with_open_vacancies=state.only_open,
                progress_cb=on_progress,
            )
            all_companies.extend(companies)

            total_added_all += len(companies)
            total_found_all += found_in_industry

            progress_industries.value = idx / total
            progress_pages.value = 0

            set_status(
                f"Индустрия {idx}/{total} готова: добавлено {len(companies)} компаний "
                f"(собрано: {found_in_industry}, итого добавлено: {total_added_all})"
            )
            page.update()

        progress_pages.visible = False
        progress_industries.visible = False
        page.update()

        # ---------- export ----------
        written_files = export_results(
            companies=all_companies,
            chosen_industries=chosen,
            area=state.area,
            min_vacancies=state.min_vacancies,
            only_open=state.only_open,
            output_dir=state.output_dir,
            export_json=bool(export_json_cb.value),
            export_csv=bool(export_csv_cb.value),
        )

        if not written_files:
            result_info.value = "Нечего сохранять: выбери хотя бы один формат (JSON/CSV)."
            open_file_btn.visible = False
            open_folder_btn.visible = False
            state.last_output_file = None
        else:
            state.last_output_file = written_files[0]
            result_info.value = "Готово ✅\n" + "\n".join([f"Файл: {p}" for p in written_files])
            open_file_btn.visible = True
            open_folder_btn.visible = True

        set_status("Готово.")
        page.update()

    def on_start(_):
        if state.is_running:
            return
        set_running(True)

        def worker():
            try:
                run_scrape_job()
            except Exception as ex:
                set_status(f"Ошибка парсинга: {ex}")
            finally:
                set_running(False)

        threading.Thread(target=worker, daemon=True).start()

    def on_open_file(_):
        if not state.last_output_file:
            return
        p = Path(state.last_output_file)
        if p.exists():
            page.launch_url(p.resolve().as_uri())

    def on_open_folder(_):
        if not state.last_output_file:
            return
        p = Path(state.last_output_file).parent
        if p.exists():
            page.launch_url(p.resolve().as_uri())

    # ---------- wire events ----------
    reload_btn.on_click = on_reload
    search_field.on_change = on_search
    select_all_btn.on_click = on_select_all
    clear_all_btn.on_click = on_clear_all
    start_btn.on_click = on_start
    open_file_btn.on_click = on_open_file
    open_folder_btn.on_click = on_open_folder

    # ---------- layout ----------
    page.add(
        ft.Column(
            [
                title,
                ft.Row([reload_btn, start_btn]),
                ft.Row(
                    [
                        min_vac_field,
                        area_field,
                        only_open_switch,
                        ft.Container(expand=True),
                        export_json_cb,
                        export_csv_cb,
                    ]
                ),
                ft.Row([output_dir_field, choose_dir_btn]),
                ft.Divider(),
                ft.Row([search_field, select_all_btn, clear_all_btn]),
                ft.Container(industries_column, border=ft.border.all(1), padding=10, expand=True),
                ft.Divider(),
                progress_industries,
                progress_pages,
                stats_text,
                status_text,
                result_info,
                ft.Row([open_file_btn, open_folder_btn]),
            ],
            expand=True,
        )
    )

    # initial load
    on_reload(None)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    import flet as ft
    ft.app(target=main)
