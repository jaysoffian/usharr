from usharr.mediainfo import parse_text_report

REPORT = """General
Complete name                            : /media/Movies/A (2000)/A (2000).mkv
Format                                   : Matroska

Audio #2
ID                                       : 3
Title                                    : Commentary: Director

Menu
00:09:22.353                             : en:Chapter 02

"""


def test_parse_text_report() -> None:
    assert parse_text_report(REPORT) == [
        (
            "General",
            [
                ("Complete name", "/media/Movies/A (2000)/A (2000).mkv"),
                ("Format", "Matroska"),
            ],
        ),
        ("Audio #2", [("ID", "3"), ("Title", "Commentary: Director")]),
        ("Menu", [("00:09:22.353", "en:Chapter 02")]),
    ]
