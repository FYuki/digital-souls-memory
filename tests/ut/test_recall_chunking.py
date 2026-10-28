import pytest

from digital_souls_memory.recall.settings import RecallSettings

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "text,expected",
    [
        ("abcdefg", [(0, 7, "abcdefg")]),
        ("abcdefgh", [(0, 8, "abcdefgh")]),
        ("abcdefghi", [(0, 8, "abcdefgh"), (6, 9, "ghi")]),
        ("abcdefghijklmn", [(0, 8, "abcdefgh"), (6, 14, "ghijklmn")]),
        ("abcdefghijklmno", [(0, 8, "abcdefgh"), (6, 14, "ghijklmn"), (12, 15, "mno")]),
        ("Ａ🙂e\u0301ｶﾞ日本語끝", [(0, 8, "Ａ🙂e\u0301ｶﾞ日本"), (6, 10, "日本語끝")]),
    ],
)
def test_chunks_cover_raw_text_with_overlap_and_codepoint_offsets(
    text: str, expected: list[tuple[int, int, str]]
) -> None:
    from digital_souls_memory.recall.chunking import split_chunks

    chunks = split_chunks(text, length=8, overlap=2)

    assert [(c.start, c.end, c.text) for c in chunks] == expected
    assert [c.index for c in chunks] == list(range(len(expected)))
    assert all(c.text == text[c.start : c.end] for c in chunks)


@pytest.mark.parametrize("size", [100, 256])
def test_default_settings_keep_short_documents_in_one_unchanged_chunk(size: int) -> None:
    from digital_souls_memory.recall.chunking import split_chunks

    settings = RecallSettings()
    text = ("Ａ🙂e\u0301" * size)[:size]
    (chunk,) = split_chunks(text, length=settings.chunk_length, overlap=settings.chunk_overlap)
    assert (chunk.start, chunk.end, chunk.text) == (0, len(text), text)


def test_zero_overlap_does_not_drop_the_tail() -> None:
    from digital_souls_memory.recall.chunking import split_chunks

    chunks = split_chunks("abcdefghi", length=8, overlap=0)
    assert [(c.start, c.end, c.text) for c in chunks] == [(0, 8, "abcdefgh"), (8, 9, "i")]


@pytest.mark.parametrize("length,overlap", [(0, 0), (-1, 0), (8, -1), (8, 8), (8, 9)])
def test_chunk_settings_reject_nonprogressing_or_negative_ranges(length: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        RecallSettings(chunk_length=length, chunk_overlap=overlap)
