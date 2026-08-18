from __future__ import annotations

import hashlib

import pytest

import prices
from prices import PriceFetchError, PriceService
from values import DailyClose

from conftest import flat_closes


class TestNormalizeCloses:
    def test_sorts_and_dedupes_by_date(self):
        rows = [
            DailyClose("2026-01-03", 3.0, "a"),
            DailyClose("2026-01-01", 1.0, "a"),
            DailyClose("2026-01-02", 2.0, "a"),
            DailyClose("2026-01-02", 2.5, "b"),  # 뒤에 온 값이 이긴다
        ]
        result = prices.normalize_closes(rows, min_rows=3)
        assert [row.date for row in result] == ["2026-01-01", "2026-01-02", "2026-01-03"]
        assert result[1].close == 2.5
        assert result[1].source == "b"

    def test_drops_non_positive_closes(self):
        rows = [DailyClose("2026-01-01", 0.0, "a"), DailyClose("2026-01-02", -1.0, "a"), DailyClose("2026-01-03", 5.0, "a")]
        assert len(prices.normalize_closes(rows, min_rows=1)) == 1

    def test_enforces_minimum_rows(self):
        with pytest.raises(ValueError, match="120"):
            prices.normalize_closes(flat_closes(10.0, count=119))


class TestParseYahoo:
    def test_parses_close_series(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1767225600, 1767312000],
                        "indicators": {"quote": [{"close": [10.5, 11.0]}]},
                    }
                ]
            }
        }
        rows = prices.parse_yahoo_chart(payload)
        assert [row.close for row in rows] == [10.5, 11.0]
        assert {row.source for row in rows} == {"yahoo"}
        assert all(len(row.date) == 10 for row in rows)

    def test_skips_null_closes(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1767225600, 1767312000, 1767398400],
                        "indicators": {"quote": [{"close": [10.5, None, 12.0]}]},
                    }
                ]
            }
        }
        assert [row.close for row in prices.parse_yahoo_chart(payload)] == [10.5, 12.0]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"chart": {}},
            {"chart": {"result": []}},
            {"chart": {"result": [{"timestamp": [], "indicators": {}}]}},
            {"chart": {"result": [{"timestamp": [1767225600], "indicators": {"quote": []}}]}},
        ],
    )
    def test_rejects_malformed_payload(self, payload):
        with pytest.raises(ValueError):
            prices.parse_yahoo_chart(payload)


class TestParseNasdaq:
    def test_parses_us_date_and_dollar_sign(self):
        payload = {
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "08/15/2026", "close": "$61.20"},
                        {"date": "08/14/2026", "close": "1,060.50"},
                    ]
                }
            }
        }
        rows = prices.parse_nasdaq_historical(payload)
        assert [(row.date, row.close) for row in rows] == [
            ("2026-08-15", 61.20),
            ("2026-08-14", 1060.50),
        ]

    def test_skips_unparsable_rows(self):
        payload = {
            "data": {
                "tradesTable": {
                    "rows": [
                        {"date": "2026-08-15", "close": "$61.20"},  # 포맷 불일치
                        {"date": "08/14/2026", "close": ""},
                        "문자열 row",
                        {"date": "08/13/2026", "close": "$60.00"},
                    ]
                }
            }
        }
        rows = prices.parse_nasdaq_historical(payload)
        assert [row.date for row in rows] == ["2026-08-13"]

    def test_rejects_empty_rows(self):
        with pytest.raises(ValueError):
            prices.parse_nasdaq_historical({"data": {"tradesTable": {"rows": []}}})


class TestParseStooq:
    csv_text = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-08-14,60.0,61.0,59.0,60.5,1000\n"
        "2026-08-15,60.5,62.0,60.0,61.75,1200\n"
    )

    def test_parses_csv(self):
        rows = prices.parse_stooq_csv(self.csv_text)
        assert [(row.date, row.close, row.source) for row in rows] == [
            ("2026-08-14", 60.5, "stooq"),
            ("2026-08-15", 61.75, "stooq"),
        ]

    def test_rejects_html_body(self):
        with pytest.raises(ValueError, match="헤더"):
            prices.parse_stooq_csv("<!DOCTYPE html><html>verify your browser</html>")


class TestPowChallenge:
    def test_finds_matching_nonce(self):
        nonce = prices.solve_pow_challenge("abc", 2)
        digest = hashlib.sha256(f"abc{nonce}".encode()).hexdigest()
        assert digest.startswith("00")

    def test_zero_difficulty_is_trivial(self):
        assert prices.solve_pow_challenge("abc", 0) == 0


class TestPriceServiceFallback:
    async def test_uses_first_successful_source(self):
        calls: list[str] = []

        async def failing(symbol: str):
            calls.append("failing")
            raise RuntimeError("차단됨")

        async def working(symbol: str):
            calls.append("working")
            return flat_closes(100.0)

        async def never(symbol: str):  # pragma: no cover - 호출되면 실패
            calls.append("never")
            raise AssertionError("앞선 소스가 성공했으면 호출되지 않아야 한다")

        service = PriceService([failing, working, never])
        closes = await service.daily_closes("TQQQ")

        assert len(closes) == 120
        assert calls == ["failing", "working"]

    async def test_skips_source_with_insufficient_rows(self):
        async def too_short(symbol: str):
            return flat_closes(100.0, count=50)

        async def enough(symbol: str):
            return flat_closes(100.0)

        service = PriceService([too_short, enough])
        assert len(await service.daily_closes("TQQQ")) == 120

    async def test_raises_when_all_sources_fail(self):
        async def failing(symbol: str):
            raise RuntimeError("실패")

        service = PriceService([failing, failing])
        with pytest.raises(PriceFetchError) as exc_info:
            await service.daily_closes("TQQQ")
        assert "TQQQ" in str(exc_info.value)

    async def test_caches_within_ttl(self):
        calls = 0

        async def counting(symbol: str):
            nonlocal calls
            calls += 1
            return flat_closes(100.0)

        clock = [0.0]
        service = PriceService([counting], ttl_seconds=100, monotonic=lambda: clock[0])

        await service.daily_closes("TQQQ")
        await service.daily_closes("tqqq")  # 대소문자 무관하게 같은 키
        assert calls == 1

        clock[0] = 101.0
        await service.daily_closes("TQQQ")
        assert calls == 2

    async def test_clear_forces_refetch(self):
        calls = 0

        async def counting(symbol: str):
            nonlocal calls
            calls += 1
            return flat_closes(100.0)

        service = PriceService([counting])
        await service.daily_closes("TQQQ")
        service.clear()
        await service.daily_closes("TQQQ")
        assert calls == 2

    async def test_separate_symbols_are_fetched_separately(self):
        seen: list[str] = []

        async def counting(symbol: str):
            seen.append(symbol)
            return flat_closes(100.0)

        service = PriceService([counting])
        await service.daily_closes("TQQQ")
        await service.daily_closes("QLD")
        assert seen == ["TQQQ", "QLD"]
