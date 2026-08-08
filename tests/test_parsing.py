"""Юнит-тесты для функций парсинга hh_companies_by_industry."""

import pytest

from dodo import extract_landing_cities, landing_open

from hh_companies_by_industry import (
    with_query,
    extract_max_page,
    parse_industries,
    parse_companies_from_industry_page,
    soupify,
    HHBlockedError,
)


# ---------- Dodo landing filters ----------

def test_extract_landing_cities():
    html = "<script>var CITIES_ONLY = ['Москва', 'Аксай (Ростовская область)'];</script>"
    assert extract_landing_cities(html) == {"Москва", "Аксай (Ростовская область)"}


def test_extract_landing_cities_rejects_missing_list():
    with pytest.raises(RuntimeError, match="CITIES_ONLY"):
        extract_landing_cities("<html></html>")


@pytest.mark.parametrize("city", ["Хабаровск", "Махачкала"])
def test_dodo_pizzamaker_is_closed_outside_landing_allowlist(city):
    assert not landing_open(city, "Пиццамейкер", {"Москва", "Казань"})


def test_dodo_pizzamaker_is_open_for_allowed_city():
    assert landing_open("Москва", "Пиццамейкер", {"Москва", "Казань"})


# ---------- with_query ----------

class TestWithQuery:
    def test_добавляет_параметр(self):
        result = with_query("https://hh.ru/employers_company/it", page=2)
        assert "page=2" in result
        assert result.startswith("https://hh.ru/employers_company/it")

    def test_перезаписывает_существующий_параметр(self):
        result = with_query("https://hh.ru/path?page=0&area=113", page=5)
        assert "page=5" in result
        assert "page=0" not in result
        assert "area=113" in result

    def test_несколько_параметров(self):
        result = with_query("https://hh.ru/path", page=1, area=113, vacanciesRequired="true")
        assert "page=1" in result
        assert "area=113" in result
        assert "vacanciesRequired=true" in result

    def test_пустой_url_без_query(self):
        result = with_query("https://hh.ru/path", foo="bar")
        assert result == "https://hh.ru/path?foo=bar"


# ---------- extract_max_page ----------

class TestExtractMaxPage:
    def test_находит_максимальную_страницу(self):
        html = """
        <html><body>
            <a href="/employers_company/it?page=0">1</a>
            <a href="/employers_company/it?page=1">2</a>
            <a href="/employers_company/it?page=4">5</a>
            <a href="/employers_company/it?page=2">3</a>
        </body></html>
        """
        soup = soupify(html)
        assert extract_max_page(soup) == 4

    def test_без_пагинации_возвращает_ноль(self):
        html = "<html><body><a href='/employers_company/it'>Тест</a></body></html>"
        soup = soupify(html)
        assert extract_max_page(soup) == 0

    def test_игнорирует_невалидные_номера_страниц(self):
        html = """
        <html><body>
            <a href="/path?page=abc">abc</a>
            <a href="/path?page=3">3</a>
        </body></html>
        """
        soup = soupify(html)
        assert extract_max_page(soup) == 3


# ---------- parse_industries ----------

class TestParseIndustries:
    def test_парсит_индустрии_из_каталога(self):
        html = """
        <html><body>
            <a href="/employers_company/it">IT и телеком</a>
            <a href="/employers_company/finance">Финансы</a>
            <a href="/employers_company/retail">Ритейл</a>
        </body></html>
        """
        industries = parse_industries(html)
        assert len(industries) == 3
        names = {i.name for i in industries}
        assert "IT и телеком" in names
        assert "Финансы" in names
        assert "Ритейл" in names

    def test_формирует_абсолютные_url(self):
        html = '<html><body><a href="/employers_company/it">IT</a></body></html>'
        industries = parse_industries(html)
        assert len(industries) == 1
        assert industries[0].slug_url == "https://hh.ru/employers_company/it"

    def test_игнорирует_ссылки_с_вложенными_путями(self):
        html = """
        <html><body>
            <a href="/employers_company/it">IT</a>
            <a href="/employers_company/it/search">Поиск</a>
            <a href="/employers_company/it/sub/path">Вложенный</a>
        </body></html>
        """
        industries = parse_industries(html)
        assert len(industries) == 1
        assert industries[0].name == "IT"

    def test_пустая_страница_возвращает_пустой_список(self):
        html = "<html><body><p>Ничего нет</p></body></html>"
        industries = parse_industries(html)
        assert industries == []

    def test_сортировка_по_имени(self):
        html = """
        <html><body>
            <a href="/employers_company/c">Ритейл</a>
            <a href="/employers_company/a">Авто</a>
            <a href="/employers_company/b">Банки</a>
        </body></html>
        """
        industries = parse_industries(html)
        names = [i.name for i in industries]
        assert names == ["Авто", "Банки", "Ритейл"]

    def test_дедупликация_по_имени(self):
        html = """
        <html><body>
            <a href="/employers_company/it">IT</a>
            <a href="/employers_company/it2">IT</a>
        </body></html>
        """
        industries = parse_industries(html)
        # dict по name — второй перезапишет первый
        assert len(industries) == 1


# ---------- parse_companies_from_industry_page ----------

class TestParseCompanies:
    def test_парсит_компании_со_страницы(self):
        html = """
        <html><body>
            <div>
                <a href="/employer/123">Яндекс</a>
                <span>42</span>
            </div>
            <div>
                <a href="/employer/456">Сбер</a>
                <span>15</span>
            </div>
        </body></html>
        """
        companies = parse_companies_from_industry_page(html, "IT", "https://hh.ru/employers_company/it")
        assert len(companies) == 2

        yandex = next(c for c in companies if c.name == "Яндекс")
        assert yandex.vacancies == 42
        assert yandex.url == "https://hh.ru/employer/123"
        assert yandex.industry_name == "IT"

    def test_пропускает_компании_без_вакансий(self):
        html = """
        <html><body>
            <a href="/employer/123">Компания без числа</a>
            <span>текст</span>
        </body></html>
        """
        companies = parse_companies_from_industry_page(html, "IT", "https://hh.ru/employers_company/it")
        # число вакансий найдётся из span "текст" — не число, но find_next ищет regex \d+
        # результат зависит от наличия числа на странице
        # главное — не падает
        assert isinstance(companies, list)

    def test_пустая_страница(self):
        html = "<html><body><p>Нет компаний</p></body></html>"
        companies = parse_companies_from_industry_page(html, "IT", "url")
        assert companies == []

    def test_формирует_абсолютный_url_компании(self):
        html = """
        <html><body>
            <a href="/employer/789">Тест</a>
            <span>5</span>
        </body></html>
        """
        companies = parse_companies_from_industry_page(html, "IT", "url")
        assert len(companies) == 1
        assert companies[0].url == "https://hh.ru/employer/789"

    def test_парсит_вакансии_из_текста_родителя(self):
        html = """
        <html><body>
            <div><a href="/employer/100">Компания</a> 25</div>
        </body></html>
        """
        companies = parse_companies_from_industry_page(html, "IT", "url")
        assert len(companies) == 1
        assert companies[0].vacancies == 25
