"""Юнит-тесты карты размещений HH."""

import pytest

from hh_placement_map import (
    VacancyRef,
    VacancyPlacement,
    build_snapshot,
    is_transient_payload,
    parse_rk_ids,
    parse_vacancy_page,
)


def test_parse_rk_ids_deduplicates_positive_numbers():
    assert parse_rk_ids([1187, "42", 1187]) == [1187, 42]


def test_parse_rk_ids_rejects_invalid_value():
    with pytest.raises(ValueError, match="Некорректный"):
        parse_rk_ids([0])


def test_parse_vacancy_page_extracts_exact_address_and_coordinates():
    ref = VacancyRef(
        hh_id="135876370",
        rk=1187,
        rk_name="Тестовая РК",
        url="https://hh.ru/vacancy/135876370",
    )
    html = """
    <h1 data-qa="vacancy-title">Сборщик заказов</h1>
    <a href="/employer/11689143">ООО ЗДН</a>
    <span data-qa="vacancy-view-raw-address">Москва, Живописная улица, 6к3</span>
    <script>{"map":{"center":{"lat":55.781788,"zoom":17},
             "marker":{"lng":37.458957}}}</script>
    """
    item = parse_vacancy_page(html, ref)
    assert item.title == "Сборщик заказов"
    assert item.employer == "ООО ЗДН"
    assert item.employer_id == "11689143"
    assert item.city == "Москва"
    assert item.address == "Москва, Живописная улица, 6к3"
    assert item.latitude == pytest.approx(55.781788)
    assert item.longitude == pytest.approx(37.458957)


def test_parse_vacancy_page_keeps_missing_address_visible():
    ref = VacancyRef("1", 1187, "Тест", "https://hh.ru/vacancy/1")
    item = parse_vacancy_page("<h1>Без адреса</h1>", ref)
    assert item.address == ""
    assert item.latitude is None
    assert item.longitude is None


def test_build_snapshot_separates_markers_and_visible_issues(monkeypatch):
    placements = [
        VacancyPlacement(
            "10", 1187, "Тест", "https://hh.ru/vacancy/10", "С адресом", "Компания", "2",
            address="Москва, Тверская, 1", city="Москва",
            latitude=55.75, longitude=37.61,
        ),
        VacancyPlacement(
            "11", 1187, "Тест", "https://hh.ru/vacancy/11", "Без адреса", "Компания", "2",
        ),
        VacancyPlacement(
            "12", 1187, "Тест", "https://hh.ru/vacancy/12", "Закрыта", "Компания", "2",
            status="closed", error="вакансия закрыта или в архиве",
        ),
    ]
    monkeypatch.setattr(
        "hh_placement_map.load_vacancy_refs",
        lambda rk_ids: ([1187], [
            VacancyRef("10", 1187, "Тест", "https://hh.ru/vacancy/10"),
            VacancyRef("11", 1187, "Тест", "https://hh.ru/vacancy/11"),
            VacancyRef("12", 1187, "Тест", "https://hh.ru/vacancy/12"),
        ]),
    )
    monkeypatch.setattr(
        "web.db.get_cached_placements",
        lambda hh_ids: {},
    )
    monkeypatch.setattr("web.db.save_cached_placements", lambda payloads: None)
    monkeypatch.setattr("hh_placement_map.collect_vacancies", lambda refs: placements)

    snapshot = build_snapshot("Контроль", [1187])
    assert snapshot["summary"] == {
        "linked": 3, "active": 2, "closed": 1, "markers": 1,
        "issues": 2, "checked": 3, "cached": 0,
    }
    assert snapshot["markers"][0]["rk"] == 1187
    reasons = {item["hh_id"]: item["reasons"] for item in snapshot["issues"]}
    assert "нет адреса" in reasons["11"]
    assert "вакансия закрыта или в архиве" in reasons["12"]


def test_parse_vacancy_page_detects_archived_card():
    ref = VacancyRef("12", 1187, "Тест", "https://hh.ru/vacancy/12")
    item = parse_vacancy_page("<h1>Курьер</h1><div>Вакансия в архиве</div>", ref)
    assert item.status == "closed"
    assert "архиве" in item.error


def test_parse_vacancy_page_detects_hh_challenge():
    ref = VacancyRef("13", 1187, "Тест", "https://hh.ru/vacancy/13")
    item = parse_vacancy_page("<h1>Подтвердите, что вы не робот</h1>", ref)
    assert item.status == "error"
    assert item.title == ""
    assert "повторно" in item.error


def test_old_captcha_cache_is_transient():
    assert is_transient_payload({"status": "active", "title": "Подтвердите, что вы не робот"})
    assert is_transient_payload({"status": "error", "title": ""})
    assert not is_transient_payload({"status": "active", "title": "Курьер"})


def test_sqlite_cache_respects_ttl(tmp_path, monkeypatch):
    import web.db as db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cache.db"))
    db.init_db()
    payload = {
        "hh_id": "10", "rk": 1187, "rk_name": "Тест",
        "url": "https://hh.ru/vacancy/10", "title": "Курьер",
        "employer": "Компания", "employer_id": "2", "address": "Москва",
        "city": "Москва", "latitude": 55.75, "longitude": 37.61,
        "status": "active", "error": "",
    }
    db.save_cached_placements([payload])

    assert db.get_cached_placements(["10"], ttl_hours=6)["10"]["title"] == "Курьер"
    assert db.get_cached_placements(["10"], ttl_hours=-1) == {}


def test_build_snapshot_fetches_only_new_or_expired_ids(monkeypatch):
    refs = [
        VacancyRef("10", 1187, "Тест", "https://hh.ru/vacancy/10"),
        VacancyRef("11", 1187, "Тест", "https://hh.ru/vacancy/11"),
    ]
    cached = {
        "hh_id": "10", "rk": 1187, "rk_name": "Тест",
        "url": refs[0].url, "title": "Из кэша", "employer": "Компания",
        "employer_id": "2", "address": "Москва, Тверская, 1", "city": "Москва",
        "latitude": 55.75, "longitude": 37.61, "status": "active", "error": "",
    }
    fetched_ids = []

    monkeypatch.setattr("hh_placement_map.load_vacancy_refs", lambda rk_ids: ([1187], refs))
    monkeypatch.setattr("web.db.get_cached_placements", lambda hh_ids: {"10": cached})
    monkeypatch.setattr("web.db.save_cached_placements", lambda payloads: None)

    def collect(missing):
        fetched_ids.extend(ref.hh_id for ref in missing)
        return [
            VacancyPlacement(
                "11", 1187, "Тест", refs[1].url, "Новая", "Компания", "2",
                "Казань, Кремлёвская, 1", "Казань", 55.79, 49.12,
            )
        ]

    monkeypatch.setattr("hh_placement_map.collect_vacancies", collect)
    snapshot = build_snapshot("Дифф", [1187])

    assert fetched_ids == ["11"]
    assert snapshot["summary"]["checked"] == 1
    assert snapshot["summary"]["cached"] == 1
    assert {item["hh_id"] for item in snapshot["markers"]} == {"10", "11"}
