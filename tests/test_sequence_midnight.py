import pytest


def calc_sequence(start, dur, count):
    def parse(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m
    def to_hhmm(total):
        h = (total // 60) % 24
        m = total % 60
        return f"{h:02}:{m:02}"
    cur = parse(start)
    out = []
    for _ in range(count):
        on = cur % (24 * 60)
        off = on + dur
        cur += dur
        out.append((to_hhmm(on), to_hhmm(off % (24 * 60))))
    return out


def test_sequence_across_midnight():
    result = calc_sequence("23:50", 22, 3)
    assert result == [
        ("23:50", "00:12"),
        ("00:12", "00:34"),
        ("00:34", "00:56"),
    ]
